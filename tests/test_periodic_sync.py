"""週期性 REST 同步 task：sync_all 回報成敗、告警狀態機、常駐 loop。

驅動源從 _handle_ticker 移到常駐 task 後，「同步有沒有在跑」不再有 tick 當
不在場證明——失敗必須自己會說話，否則只是把靜默停擺換了個位置重演。
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

from grid_engine import clock
from grid_engine.bot import MaxGridBot
from grid_engine.config import GlobalConfig, SymbolConfig
from grid_engine.state import SymbolState
from grid_engine.sync_service import SyncOutcome

SYMBOL = "XRP/USDC:USDC"


def _make_bot():
    """最小 bot fixture，沿用 tests/test_price_staleness_guard.py 的模式。"""
    cfg = GlobalConfig()
    cfg.symbols = {SYMBOL: SymbolConfig(
        symbol="XRPUSDC", ccxt_symbol=SYMBOL, enabled=True,
        take_profit_spacing=0.003, grid_spacing=0.003, initial_quantity=0.02,
        limit_multiplier=5.0, threshold_multiplier=20.0,
    )}
    cfg.bandit.enabled = False
    bot = MaxGridBot(cfg)
    bot.order_executor.place_order = AsyncMock()
    bot.order_executor.cancel_orders_for_side = AsyncMock()
    return bot


@pytest.fixture
def bot():
    b = _make_bot()
    yield b
    clock.reset_clock()
    clock.reset_guard_clock()


@pytest.fixture
def sync(bot):
    """把五個子項全換成成功的 no-op，測試各自再覆寫要失敗的那一個。

    `return_value=True` 不可省（dual-review M9）：裸 `AsyncMock()` 回的是一個
    truthy 的 MagicMock，`SyncOutcome.positions_ok` 會是那個 mock 而不是 True，
    下面所有斷言就只驗到「truthy」，驗不到「這個子項回報成功」——真實子項回
    一個非 True 的 truthy 值（例如回了個物件）也照樣綠。
    """
    s = bot.sync_service
    s._sync_positions = AsyncMock(return_value=True)
    s._sync_orders = AsyncMock(return_value=True)
    s._sync_account = AsyncMock(return_value=True)
    s._sync_funding_rates = AsyncMock(return_value=True)
    s._sync_trade_stats = AsyncMock(return_value=True)
    return s


@pytest.mark.asyncio
async def test_sync_all_reports_all_ok(sync):
    outcome = await sync.sync_all()
    assert isinstance(outcome, SyncOutcome)
    # `is True` 而不是 truthy（M9）：五個子項回的必須是布林 True 本身
    assert outcome.positions_ok is True and outcome.account_ok is True
    assert outcome.orders_ok is True and outcome.funding_ok is True
    assert outcome.trade_stats_ok is True
    assert outcome.critical_ok is True
    assert outcome.skipped is False


@pytest.mark.asyncio
async def test_sync_all_reports_skipped_when_lock_held(sync):
    """_sync_lock 已被持有 ⇒ early-return，回 skipped=True 且不參與判定。

    這個 early-return 是既有語意（tests/test_async_offload.py 三條並發測試在守），
    回傳值的加入不得改變它。
    """
    async with sync._sync_lock:
        outcome = await sync.sync_all()
    assert outcome.skipped is True
    assert outcome.critical_ok is True      # skipped 不算失敗
    sync._sync_positions.assert_not_called()


@pytest.fixture
def fake_clock():
    """可推進的假守衛時鐘。注入 set_guard_clock 而非 set_clock：

    live bot 與 backtester 同行程，clock.now() 會被 backtester 換成歷史 epoch。
    同步節流量的是「本機牆鐘」，與情境時鐘是不同的物理量，混用是分類錯誤。
    """
    t = {"now": 1_000_000.0}
    clock.set_guard_clock(lambda: t["now"])

    def advance(seconds):
        t["now"] += seconds
    yield advance
    clock.reset_guard_clock()


def test_maybe_sync_is_gone(sync):
    """`maybe_sync()` 已刪除（spec §10 修訂紀錄）：loop 的 sleep 已經是節流器，
    第二把用不同時鐘的閘門只提供失效模式；移除 ticker driver 後它也沒有其他
    呼叫端。這條擋「有人為了相容又把它加回來」——加回來就會有人去呼叫它。
    """
    assert not hasattr(sync, "maybe_sync")


@pytest.mark.asyncio
async def test_sync_all_stamps_heartbeat(sync, fake_clock):
    """`sync_all()` 成功結束才蓋章。這個時戳是每日摘要判斷「同步是不是整條
    停擺」的唯一來源（notifier._format_sync_line 的心跳分支），蓋章沒發生
    等於那道儀器永遠讀到 0。用 guard_now()（牆鐘）而非 now()（情境時鐘）。
    """
    assert sync.last_sync_time == 0
    await sync.sync_all()
    assert sync.last_sync_time == clock.guard_now()

    fake_clock(123.0)
    await sync.sync_all()
    assert sync.last_sync_time == clock.guard_now()


@pytest.mark.asyncio
async def test_skipped_sync_all_does_not_stamp_heartbeat(sync, fake_clock):
    """鎖被佔住的 early-return 不得蓋章：那一輪根本沒同步，蓋了就是把
    「卡住」偽裝成「剛同步過」，心跳儀器直接失效。
    """
    fake_clock(0)                       # 進假時鐘，確保 last_sync_time 可辨識
    await sync.sync_all()
    stamped = sync.last_sync_time
    fake_clock(600.0)
    async with sync._sync_lock:
        outcome = await sync.sync_all()
    assert outcome.skipped is True
    assert sync.last_sync_time == stamped


def test_module_time_helper_still_exists():
    """_time 不得整個刪除：test_trade_stats_sync.py 正在 monkeypatch 它，
    那是 TRADE_STATS_INTERVAL 在用的計時來源。
    """
    from grid_engine import sync_service
    assert callable(sync_service._time)


from grid_engine.sync_service import SYNC_FAILURE_THRESHOLD, SYNC_INTERVAL_FALLBACK


@pytest.fixture
def notified(sync):
    """攔截告警文字。_notify 是同步方法（內部 create_task），直接換掉。"""
    sent = []
    sync._notify = lambda msg: sent.append(msg)
    return sent


def test_two_failures_do_not_alert(sync, notified):
    for _ in range(SYNC_FAILURE_THRESHOLD - 1):
        sync._evaluate(SyncOutcome(positions_ok=False))
    assert notified == []
    assert sync._degraded is False


def test_third_failure_alerts_once(sync, notified):
    for _ in range(SYNC_FAILURE_THRESHOLD):
        sync._evaluate(SyncOutcome(positions_ok=False))
    assert len(notified) == 1
    assert "降級" in notified[0]
    assert sync._degraded is True
    assert sync._degraded_total == 1


def test_degraded_does_not_repeat_alert(sync, notified):
    for _ in range(SYNC_FAILURE_THRESHOLD + 5):
        sync._evaluate(SyncOutcome(account_ok=False))
    assert len(notified) == 1


def test_recovery_alerts_once_and_resets(sync, notified):
    for _ in range(SYNC_FAILURE_THRESHOLD):
        sync._evaluate(SyncOutcome(positions_ok=False))
    sync._evaluate(SyncOutcome())            # 全綠
    assert len(notified) == 2
    assert "恢復" in notified[1]
    assert sync._degraded is False
    assert sync._consecutive_failures == 0
    sync._evaluate(SyncOutcome())            # 再全綠不得重發
    assert len(notified) == 2


def test_non_critical_failures_never_alert(sync, notified):
    """掛單/funding/成交統計失敗只留 log，不進計數——它們失敗不影響交易安全。"""
    for _ in range(10):
        sync._evaluate(SyncOutcome(orders_ok=False, funding_ok=False, trade_stats_ok=False))
    assert notified == []
    assert sync._consecutive_failures == 0


def test_none_and_skipped_do_not_move_counter(sync, notified):
    """None（呼叫端沒帶 loop_error 的保守路徑）與 lock 佔用(skipped) 都不算
    成功也不算失敗。skipped 尤其重要：把「鎖被佔住、其實沒同步」算成一次
    成功，會讓卡死的那一輪反而把失敗計數清零。"""
    sync._evaluate(SyncOutcome(positions_ok=False))
    assert sync._consecutive_failures == 1
    sync._evaluate(None)
    sync._evaluate(SyncOutcome(skipped=True))
    assert sync._consecutive_failures == 1   # 沒被推進，也沒被歸零
    assert notified == []


def test_loop_error_counts_as_failure(sync, notified):
    """loop 級例外也算一次失敗——否則「sync_all 整條炸掉」會完全不計數。"""
    for _ in range(SYNC_FAILURE_THRESHOLD):
        sync._evaluate(None, loop_error=True)
    assert len(notified) == 1


def test_alert_text_contains_no_external_data(sync, notified):
    """告警文案只用常數與數字：notifier 用 parse_mode=HTML，未跳脫的外部資料
    會壞掉整封訊息，且例外原文可能帶憑證片段。
    """
    for _ in range(SYNC_FAILURE_THRESHOLD):
        sync._evaluate(SyncOutcome(positions_ok=False))
    assert "<" not in notified[0] and ">" not in notified[0]


def test_threshold_literal_fires_on_third_not_second(sync, notified):
    """釘住門檻值本身（生產參數，不是實作細節）：故意寫死次數 2 / 3，
    不引用 SYNC_FAILURE_THRESHOLD——迴圈次數綁常數的測試對常數的值不敏感，
    常數被手滑改成 2 或 5 時全部照樣綠燈（見 review Important）。這條測試
    改常數就該紅：它驗的是「3」這個字面值本身。
    """
    sync._evaluate(SyncOutcome(positions_ok=False))
    sync._evaluate(SyncOutcome(positions_ok=False))
    assert notified == []

    sync._evaluate(SyncOutcome(positions_ok=False))
    assert len(notified) == 1


@pytest.mark.asyncio
async def test_run_syncs_while_ticker_is_completely_silent(sync):
    """本改動的核心主張：_handle_ticker 一次都不被呼叫，同步照樣進行。

    這是整份計畫存在的理由——今天 maybe_sync 只掛在 ticker handler 上，
    bookTicker 一斷，持倉同步/保證金告警/訂單對帳全部靜默停擺。
    """
    sync.config.sync_interval = 0.01
    task = asyncio.create_task(sync.run())
    await asyncio.sleep(0.1)
    sync.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert sync._sync_positions.call_count >= 2


@pytest.mark.asyncio
async def test_run_exits_cleanly_on_cancel(sync):
    """CancelledError 必須穿過去：bot.stop() 靠 cancel + await 收尾，
    被 except Exception 吃掉會讓關機卡住。

    task.cancelled() 而不只是 task.done() 才能分辨兩種退出路徑：CancelledError
    若沒被 run() 內部 `except asyncio.CancelledError: break` 接住，它會直接穿出
    run()，task 進入 cancelled 狀態——用 gather(return_exceptions=True) 收尾時
    兩種路徑都不會 hang、task.done() 兩者皆真，只有 task.cancelled() 能照出
    「有沒有接住」的差異（asyncio.CancelledError 繼承 BaseException，不會被
    `except Exception` 攔到，所以順序本身不會讓程式掛住，但沒接住會讓 task
    帶著 cancelled 狀態退出，與『乾淨退出』的預期不符）。
    """
    sync.config.sync_interval = 0.01
    task = asyncio.create_task(sync.run())
    await asyncio.sleep(0.03)
    task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=1.0)
    assert task.done()
    assert not task.cancelled()


@pytest.mark.asyncio
async def test_run_survives_exception_and_counts_it(sync, notified):
    """loop 內例外不得殺掉 task——修一個靜默故障的改動自己不能靜默死掉。"""
    sync.config.sync_interval = 0.01
    sync.sync_all = AsyncMock(side_effect=RuntimeError("boom"))
    task = asyncio.create_task(sync.run())
    await asyncio.sleep(0.15)
    sync.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert sync._consecutive_failures >= SYNC_FAILURE_THRESHOLD
    assert len(notified) == 1


@pytest.mark.parametrize(
    "bad", [0, -5, float("nan"), float("inf"), float("-inf"), "abc", None])
def test_loop_interval_clamps_illegal_values(sync, bad):
    """非法值一律換成 SYNC_INTERVAL_FALLBACK，不是「夾到某個下限」。

    `sleep(0)` 會變成忙迴圈打爆 REST 配額；`sleep(inf)` 是反方向的同一件事——
    永遠不醒、`_stop_event` 也叫不醒它（sleep 不受 event 中斷）、執行中把設定
    改回正常值同樣救不回來（每輪才重讀 config，而這一輪永遠不會結束）⇒ REST
    同步整條停擺、降級狀態機一次都不會被推進 = 完全靜默（dual-review B3）。

    斷言 `== SYNC_INTERVAL_FALLBACK` 而不是舊的 `>= 1.0`：`float("inf") >= 1.0`
    為真，舊斷言對 inf 這個最危險的輸入恆綠。
    mutation（實跑過）：把 `_loop_interval` 的 `not math.isfinite(interval)` 換回
    `math.isnan(interval)` ⇒ 紅在 `bad=inf` 這個 case（回傳 inf 而非 10.0）。
    """
    sync.config.sync_interval = bad
    assert sync._loop_interval() == SYNC_INTERVAL_FALLBACK


def test_loop_interval_respects_legal_small_value(sync):
    sync.config.sync_interval = 2.5
    assert sync._loop_interval() == 2.5


class _BrokenConfig:
    """`self.config` 壞掉的最小重現：屬性存取直接拋 AttributeError。

    生產上等價於 config 物件被換掉／欄位被移除；重點是那個例外**不是**
    TypeError/ValueError，原版 `_loop_interval` 的 except 接不到它。
    """
    @property
    def sync_interval(self):
        raise AttributeError("sync_interval 不見了")


@pytest.mark.asyncio
async def test_broken_config_does_not_busy_loop(sync):
    """config 壞掉時 loop 不得變成沒有 sleep 的忙迴圈（review I3）。

    `await asyncio.sleep(self._loop_interval())` 整句在 try 內：`_loop_interval()`
    求值失敗 ⇒ sleep 根本沒被執行 ⇒ 例外被 loop 的 except Exception 接住 ⇒
    立刻下一輪 ⇒ 再拋 ⇒ 100% CPU、每輪一行 logger.error（實盤會以幾十萬行/秒
    寫 log）。

    鑑別力（實測過，不是推理）：斷言的是 `_consecutive_failures == 0`，不是
    「次數上限」。理由是這裡有兩道重疊的防禦，只挑一道拿掉的話另一道會接住：
    - 只把 `_loop_interval` 的 `except Exception` 縮回 `(TypeError, ValueError)`：
      AttributeError 逃出去 → run() 的 `except Exception` 接住 → `slept` 為 False
      → 補睡 SYNC_INTERVAL_FALLBACK(10s) ⇒ **不會**變成忙迴圈，但會被記成一次 loop 級失敗
      ⇒ `_consecutive_failures` 變 1 ⇒ 這一行紅。這正是「config 壞掉該在
      `_loop_interval` 就被吸收成 fallback，不該冒充成一次同步失敗」的語意。
    - 兩道一起拿掉：0.3 秒內累積上萬次迭代，同一行以更誇張的數字紅。
    （只拿掉 `slept` 那道由下一條測試守。）
    """
    sync.config = _BrokenConfig()
    task = asyncio.create_task(sync.run())
    await asyncio.sleep(0.3)
    sync.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert sync._consecutive_failures == 0
    sync._sync_positions.assert_not_called()   # fallback 10s > 窗口，本來就不該同步到


@pytest.mark.asyncio
async def test_loop_interval_raising_does_not_busy_loop(sync):
    """同上，但直接讓 `_loop_interval()` 拋例外——守的是 run() 裡那道
    「本輪沒 sleep 到就補睡 SYNC_INTERVAL_FALLBACK」的保險本身。

    兩道防禦是刻意重疊的：`_loop_interval` 變成 total function 是「不要製造
    沒有 sleep 的一輪」，這裡守的是「就算製造了也不能變忙迴圈」。

    測法：數 `_loop_interval` 被呼叫幾次。有補睡時，0.3 秒的窗口內最多一次
    （每輪至少睡 SYNC_INTERVAL_FALLBACK=10s）；沒有補睡時，run() 這一輪完全沒有
    await，event loop 被**完全餓死**（連測試自己的 `await asyncio.sleep(0.3)`
    都永遠不會恢復），所以不能靠時間窗口收尾——`_ESCAPE_AFTER` 那道逃生門
    存在的唯一理由就是讓 mutation 下這條測試**紅**而不是**hang**。
    鑑別力（實測過）：拿掉 run() 裡 `if not slept:` 那段，calls 會在一瞬間衝到
    逃生門的 51，下面的 `<= 2` 立刻紅。
    """
    _ESCAPE_AFTER = 50
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        if calls["n"] > _ESCAPE_AFTER:
            sync.stop()          # 逃生門：忙迴圈餓死 event loop，只能從裡面自己踩煞車
            return 0.01
        raise RuntimeError("interval 炸了")

    sync._loop_interval = boom
    task = asyncio.create_task(sync.run())
    await asyncio.sleep(0.3)
    sync.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert calls["n"] <= 2


# --- C1（dual-review Critical）：REST 的 fetch→apply 窗口 vs WS handler ---------
#
# 背景：`_sync_positions` / `_sync_orders` 從 REST 讀回資料到寫進 state 之間隔著
# 一整趟 round-trip 的 await。改動前 `sync_all()` 被 await 在 `_handle_ticker` 內、
# 而 ws_client 的 recv 迴圈一次只跑一個 handler ⇒ 那個 await 期間沒有任何 WS
# handler 能執行。搬成獨立 task 之後這個天然序列化消失了，而 WS handler 不取
# symbol lock ⇒ REST 會拿過期快照蓋掉成交後的新狀態。
# 守衛：SymbolState.ws_seq（WS 端遞增，REST 端在 symbol lock 內比對）。

class _FakeExchange:
    """只提供這兩條測試用得到的兩個方法。"""
    def __init__(self, positions=None, open_orders=None):
        self._positions = positions or []
        self._open_orders = open_orders or []
        self.on_fetch = None

    async def fetch_positions(self, params=None):
        if self.on_fetch:
            await self.on_fetch()
        return self._positions

    async def fetch_open_orders(self, symbol=None):
        if self.on_fetch:
            await self.on_fetch()
        return self._open_orders


async def _inline_gateway_call(fn, *args, **kwargs):
    """把 RestGateway.call 換成「同一個 event loop 內直接跑」。

    真的 gateway 把同步 ccxt 丟到 worker thread，沒辦法在裡面 await 一個 WS
    handler。這個替身保留了唯一重要的性質——`await self.gateway.call(...)` 是一個
    真正的讓出點，其他 task（WS handler）可以在那期間跑完。
    """
    result = fn(*args, **kwargs)
    if asyncio.iscoroutine(result):
        result = await result
    return result


@pytest.mark.asyncio
async def test_ws_position_write_during_fetch_is_not_overwritten(bot):
    """成交後 WS 寫進來的持倉，不得被同一輪 REST 的舊快照蓋回去。

    會動錢的路徑：`_grid_step` 用 `sym_state.long_position == 0` 分岔
    （bot.py 多頭/空頭兩段）。WS 把它寫成 0.02、REST 舊快照蓋回 0 ⇒ 下一 tick
    走「無倉位」分支 ⇒ `cancel_orders_for_side('long')` 撤掉剛掛好的網格 +
    `place_order` 再開一次倉，而且完全靜默。

    mutation（實跑過）：拿掉 `_sync_positions` apply 迴圈裡的
    `if st.ws_seq != seq_before.get(symbol): continue`
    ⇒ 紅在 `assert st.long_position == 0.02`（實際變成 REST 的 0.0）。
    """
    st = bot.state.symbols[SYMBOL]
    st.long_position = 0.0

    # REST 回的是「成交之前」的快照：沒有任何持倉
    ex = _FakeExchange(positions=[
        {"symbol": SYMBOL, "contracts": 0.0, "side": "long", "unrealizedPnl": 0.0},
    ])

    async def ws_fill_arrives():
        # REST 還在路上時，userData 推來成交後的新持倉（不取 symbol lock）
        await bot._handle_account_update({"a": {"B": [], "P": [
            {"s": "XRPUSDC", "pa": "0.02", "up": "1.5", "ps": "LONG"},
        ]}})

    ex.on_fetch = ws_fill_arrives
    bot.ctx.exchange = ex
    bot.gateway.call = _inline_gateway_call

    seq_at_start = st.ws_seq
    ok = await bot.sync_service._sync_positions()

    assert ok is True, "丟棄過期快照不算同步失敗——state 裡的值是對的（且更新）"
    assert st.ws_seq > seq_at_start, "WS handler 必須遞增 ws_seq，否則守衛根本沒被觸發"
    assert st.long_position == 0.02, "REST 舊快照蓋掉了 WS 剛寫進來的持倉"
    assert st.unrealized_pnl == 1.5


@pytest.mark.asyncio
async def test_ws_order_write_during_fetch_is_not_overwritten(bot):
    """WS 把某側掛單計數歸零、正要重掛時，不得被同一輪 REST 的舊快照寫回非 0。

    反方向但同樣靜默的後果：`_should_adjust_grid`（bot.py）看到
    `sell_long_orders > 0` 就回 False ⇒ 該側網格漏掛，最長一整個 sync_interval。

    mutation（實跑過）：拿掉 `_sync_orders` apply 區塊裡的
    `if state.ws_seq != seq_before: continue`
    ⇒ 紅在 `assert st.sell_long_orders == 0`（實際變成 REST 的 0.02）。
    """
    st = bot.state.symbols[SYMBOL]
    st.sell_long_orders = 0.02
    bot.adjust_grid = AsyncMock()      # handler 尾端會呼叫，這裡不是待驗行為

    # REST 回的是「成交之前」的快照：那張止盈單還掛著
    ex = _FakeExchange(open_orders=[
        {"side": "sell", "info": {"origQty": "0.02", "positionSide": "LONG"}},
    ])

    async def ws_fill_arrives():
        await bot._handle_order_update({"o": {
            "s": "XRPUSDC", "X": "FILLED", "S": "SELL", "ps": "LONG",
            "rp": "0.3", "p": "100", "q": "0.02",
        }})

    ex.on_fetch = ws_fill_arrives
    bot.ctx.exchange = ex
    bot.gateway.call = _inline_gateway_call

    seq_at_start = st.ws_seq
    ok = await bot.sync_service._sync_orders()

    assert ok is True
    assert st.ws_seq > seq_at_start, "WS handler 必須遞增 ws_seq，否則守衛根本沒被觸發"
    assert st.sell_long_orders == 0, "REST 舊快照把剛成交歸零的掛單計數寫回去了"


@pytest.mark.asyncio
async def test_rest_snapshot_still_applied_when_ws_is_silent(bot):
    """守衛不得變成「REST 永遠不生效」：WS 沒動過就照常 apply。

    這條是上面兩條的對照組——只斷言「丟棄」而不斷言「不丟棄時要寫入」，
    等於一個 `return` 就能讓兩條測試全綠。
    """
    st = bot.state.symbols[SYMBOL]
    ex = _FakeExchange(
        positions=[{"symbol": SYMBOL, "contracts": 0.05, "side": "long", "unrealizedPnl": 2.0}],
        open_orders=[{"side": "sell", "info": {"origQty": "0.02", "positionSide": "LONG"}}],
    )
    bot.ctx.exchange = ex
    bot.gateway.call = _inline_gateway_call

    assert await bot.sync_service._sync_positions() is True
    assert await bot.sync_service._sync_orders() is True
    assert st.long_position == 0.05
    assert st.unrealized_pnl == 2.0
    assert st.sell_long_orders == 0.02


@pytest.mark.asyncio
async def test_position_snapshot_discard_is_per_symbol(bot):
    """丟棄粒度是單一 symbol，不是整輪：`_sync_positions` 先把所有 symbol 聚合
    成 agg 再逐一 apply，整輪丟棄會讓一個活躍 symbol 餓死其他所有 symbol 的對帳。
    """
    other = "SOL/USDC:USDC"
    bot.state.symbols[other] = SymbolState(symbol=other)
    st = bot.state.symbols[SYMBOL]
    st.long_position = 0.0

    ex = _FakeExchange(positions=[
        {"symbol": SYMBOL, "contracts": 0.0, "side": "long", "unrealizedPnl": 0.0},
        {"symbol": other, "contracts": 3.0, "side": "long", "unrealizedPnl": 7.0},
    ])

    async def ws_fill_arrives():
        # 只動 XRP（SOL 不在 P 陣列裡），但 handler 會把兩個 symbol 的 ws_seq 都推進
        await bot._handle_account_update({"a": {"B": [], "P": [
            {"s": "XRPUSDC", "pa": "0.02", "up": "1.5", "ps": "LONG"},
        ]}})

    ex.on_fetch = ws_fill_arrives
    bot.ctx.exchange = ex
    bot.gateway.call = _inline_gateway_call

    await bot.sync_service._sync_positions()

    assert st.long_position == 0.02          # WS 的值保住
    # SOL 不在 P 陣列裡 ⇒ 它的 ws_seq 沒被推進 ⇒ 它的 REST 快照照常寫入。
    #
    # 2026-08-26 修訂（原本這裡斷言的是 `== 0.0`，理由寫「handler 會把每個
    # symbol 的 unrealized_pnl 歸零 = 全部都髒」）：那個歸零本身就是 bug——
    # ACCOUNT_UPDATE 的 P 是增量而非全量快照，把 P 以外的 symbol 歸零會讓它們
    # 停在假的 upnl=0，而 C1 守衛連帶讓 REST 治不回來 ⇒ 追蹤止盈對健康倉位送
    # 市價平倉單（見 test_account_update_must_not_zero_untouched_symbol_upnl）。
    # 根因修掉之後，這條測試才真的在測它 docstring 說的那件事：丟棄粒度是單一
    # symbol，一個活躍 symbol 不會餓死其他 symbol 的對帳。
    assert bot.state.symbols[other].long_position == 3.0
    assert bot.state.symbols[other].unrealized_pnl == 7.0


# --- B2（dual-review Important）：sleep 之後、sync_all 之前的停機檢查 ----------

@pytest.mark.asyncio
async def test_stop_set_during_sleep_skips_that_round(sync):
    """`run()` 裡 sleep 之後那句 `if self._stop_event.is_set(): break` 是唯一擋住
    「共享停機訊號已 set，卻又跑一輪 `_sync_account → check_trailing_stop →
    close_symbol_positions`（**送市價平倉單**）」的東西。

    為什麼不能用「被呼叫時就 set() 的 fake `_sync_positions`」來測（外部
    reviewer 的建議寫法）：那樣 stop 是在**這一輪已經開始之後**才被 set 的，
    第二輪根本不會開始——因為 `while not self._stop_event.is_set()` 自己就擋掉
    了。刪掉那兩行照樣全綠，等於一條會執行的註解。有鑑別力的情境只有一個：
    **stop 落在 sleep 中途**，while 的條件已經檢查完了。

    mutation（實跑過）：刪掉 run() 裡 sleep 之後的那兩行
    ⇒ 紅在 `sync._sync_positions.assert_not_called()`（實際被呼叫 1 次）。
    """
    sync.config.sync_interval = 0.3
    task = asyncio.create_task(sync.run())
    await asyncio.sleep(0.05)               # 落在第一輪 sleep 中間
    assert sync._sync_positions.call_count == 0, "前置條件：這時第一輪還在睡"
    sync._stop_event.set()                  # 共享停機訊號（bot.stop() 會做的事）
    await asyncio.wait_for(task, timeout=2.0)

    sync._sync_positions.assert_not_called()
    sync._sync_account.assert_not_called()  # 這條才是會下市價單的那一個
    assert task.done() and task.exception() is None


# --- B5（dual-review Important）：sync_once ------------------------------------

@pytest.mark.asyncio
async def test_sync_once_evaluates_the_outcome(sync, notified):
    """`sync_once()` = `sync_all()` + `_evaluate()`。「一輪同步的結果必須被評估」
    這條不變式收在 SyncService 內一處，不再散到 bot.py（那裡呼叫私有 `_evaluate`）。
    """
    sync._sync_positions = AsyncMock(return_value=False)
    for _ in range(SYNC_FAILURE_THRESHOLD):
        outcome = await sync.sync_once()
    assert outcome.positions_ok is False
    assert sync._consecutive_failures == SYNC_FAILURE_THRESHOLD
    assert len(notified) == 1               # 有被評估過才會發降級告警


def test_bot_uses_sync_once_not_private_evaluate():
    """bot.run() 不得再直接呼叫私有 `_evaluate()`（B5 的 tripwire）。"""
    import inspect
    src = inspect.getsource(MaxGridBot.run)
    assert "sync_once" in src
    # 比對「呼叫」而不是裸字串 `_evaluate`：檔內註解會提到這個名字。
    assert "sync_service._evaluate" not in src


# --- M8：狀態轉換一律留 log，與 notifier 是否啟用無關 --------------------------

def test_state_transition_logs_even_when_notifier_disabled(sync, caplog):
    """沒設 Telegram 的部署，降級/恢復原本連一行 log 都沒有（`_notify` 直接
    return）⇒ 整個降級狀態機不可觀測。"""
    assert sync.notifier.enabled is False
    with caplog.at_level("WARNING"):
        for _ in range(SYNC_FAILURE_THRESHOLD):
            sync._evaluate(SyncOutcome(positions_ok=False))
        sync._evaluate(SyncOutcome())
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "降級" in text
    assert "恢復" in text


# --- M7：fallback 值釘住 config 預設 ------------------------------------------

def test_fallback_equals_config_default():
    """非法 config 的 fallback = `GlobalConfig.sync_interval` 預設值。

    刻意不 import GlobalConfig 進 sync_service（避免循環相依），改由這條測試
    釘住兩者相等：預設值日後被改而 fallback 沒跟上就會紅。
    fallback 不可比預設值更小——config 已經壞掉的情境下把 REST 頻率拉高，
    而 RestGateway 是單 worker、與 place_order 共用同一條 queue（見 M7）。
    """
    from grid_engine.sync_service import SYNC_INTERVAL_FALLBACK
    assert SYNC_INTERVAL_FALLBACK == GlobalConfig().sync_interval == 10.0


# --- 2026-08-26 re-review Critical：ACCOUNT_UPDATE 的 P 是增量，不是全量 ------
#
# C1 的 ws_seq 守衛把一個既有的潛伏 bug 變成活的：`_handle_account_update` 原本
# 會把**所有** symbol 的 unrealized_pnl 歸零、再只還原 P 陣列裡有的那些。改動前
# 被漏掉的 symbol 那個假的 0 會由下一輪 REST 快照治好；守衛把那次治療也丟棄了
# （而且 handler 對每個 symbol 都遞增 ws_seq ⇒ 整個帳戶的持倉對帳都被丟棄）。

OTHER = "SOL/USDC:USDC"


def _two_symbol_bot():
    """兩個 symbol 的 bot：一個會出現在 P 陣列裡，一個不會。"""
    cfg = GlobalConfig()
    cfg.symbols = {
        SYMBOL: SymbolConfig(
            symbol="XRPUSDC", ccxt_symbol=SYMBOL, enabled=True,
            take_profit_spacing=0.003, grid_spacing=0.003, initial_quantity=0.02,
            limit_multiplier=5.0, threshold_multiplier=20.0),
        OTHER: SymbolConfig(
            symbol="SOLUSDC", ccxt_symbol=OTHER, enabled=True,
            take_profit_spacing=0.003, grid_spacing=0.003, initial_quantity=0.02,
            limit_multiplier=5.0, threshold_multiplier=20.0),
    }
    cfg.bandit.enabled = False
    b = MaxGridBot(cfg)
    b.order_executor.place_order = AsyncMock()
    b.order_executor.cancel_orders_for_side = AsyncMock()
    b.order_executor.close_symbol_positions = AsyncMock()
    return b


@pytest.mark.asyncio
async def test_account_update_must_not_zero_untouched_symbol_upnl():
    """P 陣列漏掉的 symbol，其 unrealized_pnl 不得被歸零。

    Binance 的 ACCOUNT_UPDATE 只帶「有變動的」持倉；FUNDING_FEE 事件甚至沒有 P。
    把 P 以外的 symbol 歸零 = 憑空宣稱那些倉位的浮盈是 0。

    mutation（實跑過）：把 handler 改回「先對 self.state.symbols.values() 全部
    歸零、再 `+=`」⇒ 紅在 `assert st_other.unrealized_pnl == 7.0`（實際 0.0）。
    """
    bot = _two_symbol_bot()
    bot.adjust_grid = AsyncMock()
    st_other = bot.state.symbols[OTHER]
    st_other.long_position = 3.0
    st_other.unrealized_pnl = 7.0
    seq_before = st_other.ws_seq

    # 只有 XRP 成交
    await bot._handle_account_update({"a": {"B": [], "P": [
        {"s": "XRPUSDC", "pa": "0.02", "up": "1.5", "ps": "LONG"},
    ]}})

    assert bot.state.symbols[SYMBOL].unrealized_pnl == 1.5
    assert st_other.unrealized_pnl == 7.0, "P 沒帶到的 symbol 被憑空歸零"
    assert st_other.long_position == 3.0
    assert st_other.ws_seq == seq_before, \
        "沒被這次事件動到的 symbol 不得遞增 ws_seq——否則它的 REST 快照會被白白丟棄"

    # FUNDING_FEE 形態：完全沒有 P
    await bot._handle_account_update({"a": {"B": [{"a": "USDC", "wb": "100"}]}})
    assert st_other.unrealized_pnl == 7.0
    assert bot.state.symbols[SYMBOL].unrealized_pnl == 1.5


@pytest.mark.asyncio
async def test_account_update_same_symbol_long_and_short_accumulate():
    """同一個 symbol 在單一事件內出現 LONG/SHORT 兩筆 ⇒ 兩筆 up 相加。

    但語意是「以本次事件為單位」重算，不是無腦 `+=` 到舊值上——後者會讓每次
    ACCOUNT_UPDATE 把新浮盈疊到上一次的浮盈上、無限累加。

    mutation（實跑過）：把 `sym_state.unrealized_pnl = unrealized_pnl` 那條分支
    改成 `+=` ⇒ 紅在第二次事件後的 `assert st.unrealized_pnl == 3.0`（實際 6.0）。
    """
    bot = _two_symbol_bot()
    bot.adjust_grid = AsyncMock()
    st = bot.state.symbols[SYMBOL]

    payload = {"a": {"B": [], "P": [
        {"s": "XRPUSDC", "pa": "0.02", "up": "1.0", "ps": "LONG"},
        {"s": "XRPUSDC", "pa": "-0.01", "up": "2.0", "ps": "SHORT"},
    ]}}
    await bot._handle_account_update(payload)
    assert st.long_position == 0.02
    assert st.short_position == 0.01
    assert st.unrealized_pnl == 3.0, "同一事件的 LONG/SHORT 兩筆要相加"

    # 同樣的事件再來一次：結果必須相同（冪等），不是累加成 6.0
    await bot._handle_account_update(payload)
    assert st.unrealized_pnl == 3.0, "跨事件不得累加——那會讓浮盈無限膨脹"


@pytest.mark.asyncio
async def test_account_update_gap_does_not_trigger_spurious_market_close():
    """端到端：ACCOUNT_UPDATE 落在 fetch_positions 窗口內、且 P 漏掉某 symbol，
    不得導致對那個健康倉位送出市價平倉單。

    後果鏈（re-review 的 probe 端到端重現過）：漏掉的 symbol 停在假的
    unrealized_pnl=0 ⇒ REST 快照被 C1 守衛丟棄、治不回來 ⇒ 同一輪稍後
    `check_trailing_stop()` 看到 drawdown = peak - 0 >= max(2.0, peak*0.10)
    ⇒ `close_symbol_positions()`。預設值讓它是活的：risk.enabled=True、
    margin_threshold=0.5、trailing_start_profit=5.0 > trailing_min_drawdown=2.0。

    mutation（實跑過）：把 handler 改回「全部歸零再 `+=`」
    ⇒ 紅在 `assert st_other.unrealized_pnl == 7.0`，拿掉那條中途斷言後紅在
    `assert close.call_count == 0`（實際 1）。
    """
    bot = _two_symbol_bot()
    bot.adjust_grid = AsyncMock()
    st_other = bot.state.symbols[OTHER]
    st_other.long_position = 3.0
    st_other.unrealized_pnl = 7.0

    # 追蹤止盈已經在追這個 symbol，峰值 7.0
    bot.state.trailing_active[OTHER] = True
    bot.state.peak_pnl[OTHER] = 7.0
    bot.state.margin_usage = 1.0            # 高於 risk.margin_threshold=0.5
    assert bot.config.risk.enabled is True  # 預設就是開的，不是測試自己打開的

    ex = _FakeExchange(positions=[
        {"symbol": SYMBOL, "contracts": 0.02, "side": "long", "unrealizedPnl": 0.1},
        {"symbol": OTHER, "contracts": 3.0, "side": "long", "unrealizedPnl": 7.0},
    ])

    async def ws_fill_arrives():
        # 只有 XRP 成交；SOL 不在 P 裡
        await bot._handle_account_update({"a": {"B": [], "P": [
            {"s": "XRPUSDC", "pa": "0.04", "up": "0.2", "ps": "LONG"},
        ]}})

    ex.on_fetch = ws_fill_arrives
    bot.ctx.exchange = ex
    bot.gateway.call = _inline_gateway_call

    await bot.sync_service._sync_positions()
    assert st_other.unrealized_pnl == 7.0, "SOL 的浮盈被假的 0 取代了"

    bot.state.margin_usage = 1.0   # _sync_account 之後的真實槓桿水位
    await bot.risk_monitor.check_trailing_stop()

    close = bot.order_executor.close_symbol_positions
    assert close.call_count == 0, \
        f"對健康倉位送出市價平倉單: {close.call_args_list}"


# --- Ruling 11：per-symbol 連續丟棄計數 ---------------------------------------

@pytest.mark.asyncio
async def test_consecutive_snapshot_discards_warn(bot, caplog):
    """快照被連續丟棄 N 輪 = 一種新的靜默停擺：該 symbol 的 REST 對帳實質失效，
    而 sync_all() 仍回 True、心跳照蓋、降級狀態機一次都不會被推進。

    刻意只記 log、不推降級計數：丟棄是設計中的正常結果，WS 活躍期就會發生，
    拿它去推狀態機會在最健康的時候誤報降級並送 Telegram。

    mutation（實跑過）：把 `_sync_positions` 丟棄分支裡的 `self._record_discard(...)`
    刪掉 ⇒ 紅在 `assert "連續" in text`（一行 warning 都沒有）。
    """
    from grid_engine.sync_service import SNAPSHOT_DISCARD_WARN_THRESHOLD as N

    st = bot.state.symbols[SYMBOL]
    ex = _FakeExchange(positions=[
        {"symbol": SYMBOL, "contracts": 0.0, "side": "long", "unrealizedPnl": 0.0},
    ])

    async def ws_fill_arrives():
        await bot._handle_account_update({"a": {"B": [], "P": [
            {"s": "XRPUSDC", "pa": "0.02", "up": "1.5", "ps": "LONG"},
        ]}})

    ex.on_fetch = ws_fill_arrives
    bot.ctx.exchange = ex
    bot.gateway.call = _inline_gateway_call
    svc = bot.sync_service

    with caplog.at_level("WARNING", logger="as_grid_max"):
        for _ in range(N - 1):
            await svc._sync_positions()
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "連續" not in text, f"未達門檻({N})就告警 = 正常 WS 活動會洗版: {text}"

        await svc._sync_positions()      # 第 N 輪
        text = "\n".join(r.getMessage() for r in caplog.records)

    assert "連續" in text and SYMBOL in text, f"連續 {N} 輪被丟棄卻沒留下 warning: {text}"
    assert str(N) in text
    # 不得污染降級狀態機——丟棄不是失敗
    assert svc._consecutive_failures == 0
    assert svc._degraded is False

    # 成功套用一次就歸零
    ex.on_fetch = None
    await svc._sync_positions()
    assert svc._discard_streak.get(("持倉", SYMBOL)) is None, "成功一次必須把連續計數歸零"
    assert st.long_position == 0.0   # 這輪 REST 快照真的寫進去了
