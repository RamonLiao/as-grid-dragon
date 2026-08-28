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

    def test_switch_rejected_by_exchange_aborts_startup(self):
        """帳戶有持倉/掛單時幣安會拒絕 dualSidePosition 切換 —— 原本這被
        `except Exception: pass` 吞掉，bot 帶著錯誤的模式假設繼續啟動。"""
        bot = _make_bot()
        bot.exchange = _exchange([False], switch_error=RuntimeError("-4068"))
        with pytest.raises(RuntimeError, match="切換持倉模式被交易所拒絕"):
            bot._check_hedge_mode()

    def test_switch_then_verify_succeeds(self, _no_real_sleep):
        """切換在交易所端非同步生效：第一次複驗仍讀到舊值，第二次才確認。"""
        bot = _make_bot()
        bot.exchange = _exchange([False, False, True])
        bot._check_hedge_mode()
        bot.exchange.fapiPrivatePostPositionSideDual.assert_called_once()
        assert bot.exchange.fetch_position_mode.call_count == 3
        assert _no_real_sleep == [1.0], "第二次複驗前必須有間隔，否則等於沒複驗"

    def test_verify_never_confirms_aborts_startup(self, _no_real_sleep):
        """切換呼叫沒拋錯，但模式實際沒變 —— 這正是「不複驗就會漏掉」的形態。"""
        bot = _make_bot()
        bot.exchange = _exchange([False])
        with pytest.raises(RuntimeError, match="複驗"):
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

        hits = [r for r in caplog.records if "單向持倉模式" in r.getMessage()]
        assert len(hits) == 1, "同一 symbol 的重複事件不得洗版"
        assert (st.buy_long_orders, st.ws_seq) == (3, 7), "第二筆一樣不得套用"
