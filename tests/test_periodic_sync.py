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
    """把五個子項全換成成功的 no-op，測試各自再覆寫要失敗的那一個。"""
    s = bot.sync_service
    s._sync_positions = AsyncMock()
    s._sync_orders = AsyncMock()
    s._sync_account = AsyncMock()
    s._sync_funding_rates = AsyncMock()
    s._sync_trade_stats = AsyncMock()
    return s


@pytest.mark.asyncio
async def test_sync_all_reports_all_ok(sync):
    outcome = await sync.sync_all()
    assert isinstance(outcome, SyncOutcome)
    assert outcome.positions_ok and outcome.account_ok
    assert outcome.critical_ok
    assert not outcome.skipped


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


from grid_engine.sync_service import SYNC_FAILURE_THRESHOLD


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


@pytest.mark.parametrize("bad", [0, -5, float("nan"), "abc", None])
def test_loop_interval_clamps_illegal_values(sync, bad):
    """sleep(0) 會變成忙迴圈打爆 REST 配額。夾到下限而非 fallback 預設值：
    使用者刻意調小是合法意圖，只有非法值才需要糾正。
    """
    sync.config.sync_interval = bad
    assert sync._loop_interval() >= 1.0


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

    鑑別力：`_loop_interval` 的 `except Exception` 縮回 `(TypeError, ValueError)`
    時，AttributeError 逃出去，這個 0.3 秒窗口內會累積上萬次迭代，下面的
    `<= 2` 立刻紅。有限窗口內斷言迭代次數上限，是「沒有 sleep 的一輪」唯一
    測得到的可觀測後果。
    """
    sync.config = _BrokenConfig()
    task = asyncio.create_task(sync.run())
    await asyncio.sleep(0.3)
    sync.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    # 每輪至少睡 MIN_SYNC_INTERVAL(1.0s) ⇒ 0.3s 內最多推進一次計數
    assert sync._consecutive_failures <= 2


@pytest.mark.asyncio
async def test_loop_interval_raising_does_not_busy_loop(sync):
    """同上，但直接讓 `_loop_interval()` 拋例外——守的是 run() 裡那道
    「本輪沒 sleep 到就補睡 MIN_SYNC_INTERVAL」的保險本身。

    兩道防禦是刻意重疊的：`_loop_interval` 變成 total function 是「不要製造
    沒有 sleep 的一輪」，這裡守的是「就算製造了也不能變忙迴圈」。拿掉 run()
    裡 `if not slept:` 那段，這條會在 0.3 秒內累積上萬次迭代而紅。
    """
    def boom():
        raise RuntimeError("interval 炸了")

    sync._loop_interval = boom
    task = asyncio.create_task(sync.run())
    await asyncio.sleep(0.3)
    sync.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert sync._consecutive_failures <= 2
