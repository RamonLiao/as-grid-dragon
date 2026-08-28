"""持倉模式（position mode）守衛測試。

兩道守衛守同一個前提：**這隻 bot 只能在 hedge（雙向持倉）模式下運作**。
order_executor.place_order 對每一張網格單都帶 positionSide（order_executor.py:90-91），
而 position mode 是幣安帳戶層設定 —— one-way 模式下這些單會被整批拒絕，
bot 一張單都下不出去，只會一路撞下單斷路器。

  守衛 1（啟動）：_check_hedge_mode 確立不了 hedge 就 raise，由 run() 內那個
                  except Exception 區塊（送 notify_crash(f"初始化失敗: {e}")
                  的那一段）接成乾淨返回，不啟動。
  守衛 2（運行期）：_handle_order_update 對 ps 非 LONG/SHORT 的成交事件早退，
                  不重置掛單計數、不餵 bandit、不重掛網格。
"""
import ccxt
import pytest
from unittest.mock import AsyncMock, MagicMock

from grid_engine import clock
from grid_engine.bot import MaxGridBot
from grid_engine.config import GlobalConfig, SymbolConfig

SYMBOL = "XRP/USDC:USDC"


def _make_bot(enabled=True):
    cfg = GlobalConfig()
    cfg.symbols = {SYMBOL: SymbolConfig(
        symbol="XRPUSDC", ccxt_symbol=SYMBOL, enabled=enabled,
        take_profit_spacing=0.003, grid_spacing=0.003, initial_quantity=0.02,
        limit_multiplier=5.0, threshold_multiplier=20.0,
    )}
    cfg.bandit.enabled = False
    bot = MaxGridBot(cfg)
    bot.order_executor.place_order = AsyncMock()
    return bot


def _exchange(mode_results, switch_error=None):
    """mode_results: fetch_position_mode 依序回傳的 hedged 值（True/False/None），
    或 Exception 實例（該次呼叫拋出）。清單耗盡後重複最後一項。"""
    ex = MagicMock()
    seq = list(mode_results)

    def _fetch(symbol=None, **kw):
        item = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(item, Exception):
            raise item
        return {"info": {}, "hedged": item}

    ex.fetch_position_mode.side_effect = _fetch
    if switch_error is not None:
        ex.fapiPrivatePostPositionSideDual.side_effect = switch_error
    return ex


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """複驗間隔不要真的睡 —— 但要留下呼叫紀錄，證明間隔存在。"""
    calls = []
    monkeypatch.setattr("grid_engine.bot.time.sleep", lambda s: calls.append(s))
    yield calls
    clock.reset_clock()
    clock.reset_guard_clock()


class TestCheckHedgeMode:
    def test_already_hedged_passes_without_switching(self):
        bot = _make_bot()
        bot.exchange = _exchange([True])
        bot._check_hedge_mode()
        bot.exchange.fapiPrivatePostPositionSideDual.assert_not_called()

    def test_fetch_failure_aborts_startup(self):
        """查不到就不啟動（使用者 2026-08-28 裁決：不寬容「查不到」）。"""
        bot = _make_bot()
        bot.exchange = _exchange([RuntimeError("network down")])
        with pytest.raises(RuntimeError, match="查詢持倉模式失敗"):
            bot._check_hedge_mode()
        bot.exchange.fapiPrivatePostPositionSideDual.assert_not_called()

    def test_hedged_none_aborts_startup(self):
        """ccxt safe_bool 在 dualSidePosition 缺失時回 None —— 未知不等於 False，
        不得當成「非 hedge」去切換，也不得當成「是 hedge」放行。"""
        bot = _make_bot()
        bot.exchange = _exchange([None])
        with pytest.raises(RuntimeError, match="未回報持倉模式"):
            bot._check_hedge_mode()
        bot.exchange.fapiPrivatePostPositionSideDual.assert_not_called()

    def test_switch_rejected_and_still_not_hedged_aborts_startup(self):
        """帳戶有持倉/掛單時幣安會拒絕 dualSidePosition 切換 —— 原本這被
        `except Exception: pass` 吞掉，bot 帶著錯誤的模式假設繼續啟動。

        2026-08-28 修訂（security F2）：切換報錯**不再**直接判死，而是落到複驗、
        以最終實際狀態裁決。所以這條測試守的東西也跟著變精確：
        「切換被拒 **且複驗確認帳戶實際仍非雙向** ⇒ 才 raise」。
        它現在同時釘住三件事，缺一都算守衛被弱化：
          1. 切換失敗 + 複驗全失敗 ⇒ 仍然 raise（硬失敗裁決沒有被 F2 放寬）；
          2. 複驗**真的有跑**（call_count == 4 = 1 次初查 + 3 次複驗），
             不是「報錯就跳過複驗直接 raise」的舊行為；
          3. 交易所原始錯誤原文（-4068）出現在訊息裡 —— 操作員要能據原文判斷
             成因，而不是被寫死的「去平倉」結論牽著走（I-3）。
        """
        bot = _make_bot()
        bot.exchange = _exchange([False], switch_error=RuntimeError("-4068"))
        with pytest.raises(RuntimeError, match="切換持倉模式被交易所拒絕") as ei:
            bot._check_hedge_mode()
        assert "-4068" in str(ei.value), "訊息必須帶交易所原始錯誤原文"
        assert bot.exchange.fetch_position_mode.call_count == 4

    def test_switch_error_but_verify_confirms_hedged_starts_normally(self):
        """幣安 -4059「No need to change position side」＝帳戶本來就已經是目標
        狀態（ccxt 映射成 BadRequest）；「POST 已生效但回應 timeout」同理。
        以呼叫回傳裁決會把這兩種良性結果判死 ⇒ 假陽性擋下啟動，而擋下啟動的
        代價是既有持倉同時失去追蹤止盈與網格管理。以最終狀態裁決才是對的。"""
        bot = _make_bot()
        bot.exchange = _exchange(
            [False, True],
            switch_error=ccxt.BadRequest("-4059 No need to change position side"))
        bot._check_hedge_mode()  # 不得 raise
        bot.exchange.fapiPrivatePostPositionSideDual.assert_called_once()

    def test_initial_fetch_retries_network_error_then_succeeds(self, _no_real_sleep):
        """初查的網路瞬斷不等於「確認不了」。複驗容忍 3 次而初查零重試是不對稱的，
        且代價不只是 bot 沒跑：sync_service 在 raise 之後才啟動，追蹤止盈只由它
        驅動 ⇒ 交易所上的既有持倉會同時失去風控。"""
        bot = _make_bot()
        bot.exchange = _exchange([
            ccxt.RequestTimeout("read timeout"),
            ccxt.RequestTimeout("read timeout"),
            True,
        ])
        bot._check_hedge_mode()  # 不得 raise
        assert bot.exchange.fetch_position_mode.call_count == 3
        assert _no_real_sleep == [1.0, 1.0], "重試之間必須有間隔，否則等於沒重試"
        bot.exchange.fapiPrivatePostPositionSideDual.assert_not_called()

    def test_initial_fetch_network_error_exhausted_still_aborts(self, _no_real_sleep):
        """重試只是不把一次抖動當成「查不到」；重試耗盡仍然 raise —— 使用者
        2026-08-28 裁決「確認不了就不啟動」不因 F1 而被放寬。"""
        bot = _make_bot()
        bot.exchange = _exchange([ccxt.RequestTimeout("read timeout")])
        with pytest.raises(RuntimeError, match="查詢持倉模式失敗"):
            bot._check_hedge_mode()
        assert bot.exchange.fetch_position_mode.call_count == 3
        assert _no_real_sleep == [1.0, 1.0]
        bot.exchange.fapiPrivatePostPositionSideDual.assert_not_called()

    def test_initial_fetch_non_network_error_does_not_retry(self, _no_real_sleep):
        """權限不足、參數錯誤（ccxt.ExchangeError 家族，含 BadRequest）重試三次
        只會多送兩次注定失敗的簽名請求並多花 2 秒 —— 這類必須第一次就 raise。"""
        bot = _make_bot()
        bot.exchange = _exchange([ccxt.BadRequest("-1022 Signature not valid")])
        with pytest.raises(RuntimeError, match="查詢持倉模式失敗"):
            bot._check_hedge_mode()
        assert bot.exchange.fetch_position_mode.call_count == 1
        assert _no_real_sleep == []

    def test_switch_then_verify_succeeds(self, _no_real_sleep):
        """切換在交易所端非同步生效：第一次複驗仍讀到舊值，第二次才確認。"""
        bot = _make_bot()
        bot.exchange = _exchange([False, False, True])
        bot._check_hedge_mode()
        bot.exchange.fapiPrivatePostPositionSideDual.assert_called_once()
        assert bot.exchange.fetch_position_mode.call_count == 3
        assert _no_real_sleep == [1.0], "第二次複驗前必須有間隔，否則等於沒複驗"

    def test_verify_never_confirms_aborts_startup(self, _no_real_sleep):
        """切換呼叫沒拋錯，但模式實際沒變 —— 這正是「不複驗就會漏掉」的形態。

        match 用「切換呼叫沒報錯但實際未生效」而不是「複驗」：F2 之後兩條 raise
        都含「複驗」二字，只 match 它就分不出走的是哪條路徑。
        """
        bot = _make_bot()
        bot.exchange = _exchange([False])
        with pytest.raises(RuntimeError, match="切換呼叫沒報錯但實際未生效"):
            bot._check_hedge_mode()
        assert bot.exchange.fetch_position_mode.call_count == 4  # 1 次初查 + 3 次複驗
        assert _no_real_sleep == [1.0, 1.0]

    def test_no_enabled_symbol_skips_check(self):
        """沒有啟用中的 symbol，本來就不會下單 —— 不該因為查不到模式而擋下啟動。"""
        bot = _make_bot(enabled=False)
        bot.exchange = _exchange([RuntimeError("should not be called")])
        bot._check_hedge_mode()
        bot.exchange.fetch_position_mode.assert_not_called()

    def test_fetch_returns_non_dict_aborts_startup(self):
        """fetch_position_mode 回的東西不是 dict（例如 None、字串）—— 這不是
        「沒回報 dualSidePosition 欄位」以外的第三種未知形態，同樣不得放行。
        `_fetch_hedged` 的 `if not isinstance(mode, dict): return None, None`
        分支必須真的被行使到，不能只靠 dict-但欄位缺失 的測試間接覆蓋。"""
        bot = _make_bot()
        ex = MagicMock()
        ex.fetch_position_mode.return_value = None  # 非 dict 回應
        bot.exchange = ex
        with pytest.raises(RuntimeError, match="未回報持倉模式"):
            bot._check_hedge_mode()
        bot.exchange.fapiPrivatePostPositionSideDual.assert_not_called()


def _filled_event(position_side, side="BUY", realized_pnl="1.5"):
    return {"o": {
        "s": "XRPUSDC", "X": "FILLED", "S": side,
        "ps": position_side, "rp": realized_pnl,
        "p": "0.5", "q": "10",
    }}


@pytest.fixture
def order_bot():
    """掛單計數初值刻意設成非 0：若設 0，「沒重置」與「重置了」不可分辨
    （lessons 通則 3.3：fixture 不得把待測維度壓成退化值）。"""
    bot = _make_bot()
    bot.adjust_grid = AsyncMock()
    bot.bandit_optimizer.record_trade = MagicMock()
    st = bot.state.symbols[SYMBOL]
    st.buy_long_orders = 3
    st.sell_long_orders = 4
    st.buy_short_orders = 5
    st.sell_short_orders = 6
    st.ws_seq = 7
    return bot


class TestOrderUpdatePositionSideGuard:
    @pytest.mark.asyncio
    async def test_both_position_side_is_not_applied(self, order_bot):
        """ps='BOTH' ⇒ 帳戶在單向持倉模式，分側狀態沒有正確映射。
        套用會把成交記到錯的一側、重置錯的掛單計數。"""
        st = order_bot.state.symbols[SYMBOL]
        await order_bot._handle_order_update(_filled_event("BOTH"))

        assert (st.buy_long_orders, st.sell_long_orders) == (3, 4)
        assert (st.buy_short_orders, st.sell_short_orders) == (5, 6)
        assert st.ws_seq == 7, "早退必須發生在 ws_seq 遞增之前"
        order_bot.bandit_optimizer.record_trade.assert_not_called()
        order_bot.adjust_grid.assert_not_called()

    @pytest.mark.asyncio
    async def test_both_is_not_recorded_as_short_in_bandit(self, order_bot):
        """改動前 `trade_side = 'long' if ps == 'LONG' else 'short'` 會把
        BOTH 靜默記成 short，汙染 bandit 的分側統計。"""
        await order_bot._handle_order_update(_filled_event("BOTH"))
        order_bot.bandit_optimizer.record_trade.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_position_side_is_not_applied(self, order_bot):
        st = order_bot.state.symbols[SYMBOL]
        await order_bot._handle_order_update(_filled_event("SIDEWAYS"))
        assert (st.buy_long_orders, st.ws_seq) == (3, 7)
        order_bot.adjust_grid.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_position_side_is_not_applied(self, order_bot):
        """事件缺 `ps` 欄位 ⇒ `order_data.get('ps', '')` 給出空字串。改動前它會落到
        `trade_side = 'long' if ps == 'LONG' else 'short'` 被記成 short、遞增
        ws_seq、重置掛單計數並重掛網格 —— 空字串必須跟 BOTH 一樣走早退。
        （最終 review MUT-3：把 `''` 加進放行清單原本 866 全綠。）"""
        st = order_bot.state.symbols[SYMBOL]
        await order_bot._handle_order_update(_filled_event(""))

        assert (st.buy_long_orders, st.sell_long_orders) == (3, 4)
        assert (st.buy_short_orders, st.sell_short_orders) == (5, 6)
        assert st.ws_seq == 7
        order_bot.bandit_optimizer.record_trade.assert_not_called()
        order_bot.adjust_grid.assert_not_called()

    @pytest.mark.asyncio
    async def test_warning_names_the_raw_symbol_and_the_actual_ps(self, order_bot, caplog):
        """告警要指得出「哪個 symbol、收到什麼值」，否則操作員拿它沒辦法診斷。

        釘住的是訊息裡印的是 **交易所原始 symbol**（XRPUSDC）而不是 ccxt symbol
        （XRP/USDC:USDC）—— 節流 key 用 ccxt_symbol、訊息用 symbol_raw，兩者
        對調過去測不出來（最終 review MUT-4：參數對調 866 全綠）。
        斷言整段連續字面值，才能讓「印成 ccxt symbol」這個 mutation 紅。
        """
        with caplog.at_level("WARNING"):
            await order_bot._handle_order_update(_filled_event("BOTH"))

        msgs = [r.getMessage() for r in caplog.records if "非 LONG/SHORT" in r.getMessage()]
        assert len(msgs) == 1
        assert "[userData] XRPUSDC positionSide='BOTH' 非 LONG/SHORT" in msgs[0]

    @pytest.mark.asyncio
    async def test_long_still_applied_after_guard(self, order_bot):
        """守衛不得誤傷正常路徑。"""
        st = order_bot.state.symbols[SYMBOL]
        await order_bot._handle_order_update(_filled_event("LONG", side="BUY"))
        assert st.buy_long_orders == 0
        assert st.sell_long_orders == 4, "只該重置本次成交的那一格"
        assert st.ws_seq == 8
        order_bot.bandit_optimizer.record_trade.assert_called_once_with(1.5, 'long')
        order_bot.adjust_grid.assert_awaited_once_with(SYMBOL)

    @pytest.mark.asyncio
    async def test_short_still_applied_after_guard(self, order_bot):
        st = order_bot.state.symbols[SYMBOL]
        await order_bot._handle_order_update(_filled_event("SHORT", side="SELL"))
        assert st.sell_short_orders == 0
        assert st.buy_short_orders == 5
        order_bot.bandit_optimizer.record_trade.assert_called_once_with(1.5, 'short')

    @pytest.mark.asyncio
    async def test_warning_is_throttled_but_guard_is_not(self, order_bot, caplog):
        """節流只准影響 log，不准影響早退 —— 第二筆事件一樣不得被套用。"""
        st = order_bot.state.symbols[SYMBOL]
        with caplog.at_level("WARNING"):
            await order_bot._handle_order_update(_filled_event("BOTH"))
            await order_bot._handle_order_update(_filled_event("BOTH"))

        hits = [r for r in caplog.records if "非 LONG/SHORT" in r.getMessage()]
        assert len(hits) == 1, "同一 symbol 的重複事件不得洗版"
        assert (st.buy_long_orders, st.ws_seq) == (3, 7), "第二筆一樣不得套用"


@pytest.fixture
def fake_guard_clock():
    """可推進的假守衛時鐘（比照 tests/test_price_staleness_guard.py 的 fake_clock）。

    注入 set_guard_clock 而非 set_clock：節流用的是 clock.guard_now()。
    """
    t = {"now": 1_000_000.0}
    clock.set_guard_clock(lambda: t["now"])

    def advance(seconds):
        t["now"] += seconds
    yield advance
    clock.reset_guard_clock()


class TestUnknownPositionSideLogThrottleWindow:
    @pytest.mark.asyncio
    async def test_warning_resumes_after_throttle_window(self, order_bot, caplog,
                                                            fake_guard_clock):
        """節流窗口過後，告警必須恢復 —— 不是「第一次之後永遠靜音」。
        只釘「窗口內至多一次」測不出「節流條件被改成永久靜音」這種 mutation
        （例如把門檻改成一個天文數字）。期望值寫死秒數，不從常數換算，
        避免常數改了期望值跟著位移、斷言失去意義。
        """
        with caplog.at_level("WARNING"):
            await order_bot._handle_order_update(_filled_event("BOTH"))
            fake_guard_clock(3601.0)
            await order_bot._handle_order_update(_filled_event("BOTH"))

        hits = [r for r in caplog.records if "非 LONG/SHORT" in r.getMessage()]
        assert len(hits) == 2


class TestStartupWiring:
    """守衛 1 的「接線」：run() 必須讓 _check_hedge_mode 的 raise 真的擋下啟動。

    這個 class 釘的不是 _check_hedge_mode 本身（上面 TestCheckHedgeMode 已經釘了），
    而是 spec Goal 1 的不變式：**確立不了 hedge 就不啟動**。少了它，任何人把
    run() 裡那句 gateway.call(self._check_hedge_mode) 包回 `except Exception: pass`
    （＝這條 branch 要修掉的 anti-pattern）都能全套綠燈通過。
    """

    @pytest.mark.asyncio
    async def test_hedge_check_failure_stops_startup_before_listen_key(self):
        bot = _make_bot()
        bot._init_exchange = MagicMock()
        bot._check_hedge_mode = MagicMock(
            side_effect=RuntimeError("[MAX] 查詢持倉模式失敗（測試注入）"))
        bot.ws_client.acquire_listen_key = AsyncMock()
        bot.sync_service.sync_once = AsyncMock()
        bot.notifier.notify_crash = AsyncMock()

        await bot.run()

        # acquire_listen_key 是 _check_hedge_mode 之後的下一步：只要它被呼叫，
        # 就代表 raise 沒有擋住啟動序列（例外被吞掉了）。
        bot.ws_client.acquire_listen_key.assert_not_called()
        bot.sync_service.sync_once.assert_not_called()
        assert bot.state.running is False
        bot.notifier.notify_crash.assert_awaited_once()
        assert not bot.tasks, "初始化失敗不得 create_task 任何常駐 task"
