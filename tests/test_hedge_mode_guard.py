"""持倉模式（position mode）守衛測試。

兩道守衛守同一個前提：**這隻 bot 只能在 hedge（雙向持倉）模式下運作**。
order_executor.place_order 對每一張網格單都帶 positionSide（order_executor.py:90-91），
而 position mode 是幣安帳戶層設定 —— one-way 模式下這些單會被整批拒絕，
bot 一張單都下不出去，只會一路撞下單斷路器。

  守衛 1（啟動）：_check_hedge_mode 確立不了 hedge 就 raise，由 run() 的
                  except（bot.py:938-944）接成乾淨返回，不啟動。
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
