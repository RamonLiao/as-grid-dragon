"""雙向減倉的三條不變式（spec §3.2）。

斷言不變式而非逐案例列舉——v2 的「大側固定 2×」在 gap < reduce_qty/2 時會把
|delta| 推大（反例 0.66/0.64：0.02 → 0.06），而觸發後雙側都掉到門檻以下、
沒有下一輪可以修正。
"""
import asyncio

import pytest

from grid_engine.config import SymbolConfig
from grid_engine.risk_monitor import RiskMonitor
from grid_engine.state import GlobalState, SymbolState

# ⚠️ 測試值不可用 0.64：local_threshold = initial_quantity*threshold_multiplier*0.8
#    = 0.02*40.0*0.8 = 0.6400000000000001（浮點），字面值 0.64 < 它 ⇒ 觸發條件不成立、
#    整個減倉場景不會發生。所有測試值取 0.66 以上，安全高於邊界。


class _RecordingExecutor:
    def __init__(self):
        self.orders = []

    async def place_order(self, symbol, side, price, quantity,
                          reduce_only, position_side, order_type):
        self.orders.append({
            "side": side, "quantity": quantity, "reduce_only": reduce_only,
            "position_side": position_side, "order_type": order_type,
        })
        return {"id": "x"}


def _run(long_pos, short_pos):
    """觸發一次雙向減倉，回傳 (減多量, 減空量, reduce_qty)。"""
    # initial_quantity=0.02 × threshold_multiplier=40 → position_threshold=0.8
    # 兩者皆非 SymbolConfig 預設（預設 3 / 20），避免測試值 == 預設值的假綠
    cfg = SymbolConfig(symbol="BNBUSDC", ccxt_symbol="BNB/USDC:USDC",
                       initial_quantity=0.02, threshold_multiplier=40.0)
    assert cfg.position_threshold == pytest.approx(0.8)
    reduce_qty = cfg.position_threshold * 0.1          # 0.08
    local_threshold = cfg.position_threshold * 0.8     # 0.64
    assert long_pos >= local_threshold and short_pos >= local_threshold, "測試狀態必須能觸發"

    state = GlobalState()
    sym_state = SymbolState(symbol="BNBUSDC",
                            long_position=long_pos, short_position=short_pos)
    ex = _RecordingExecutor()
    rm = RiskMonitor(config=None, state=state, order_executor=ex, notifier=None)
    asyncio.run(rm.check_and_reduce_positions(cfg, sym_state))

    cut_long = sum(o["quantity"] for o in ex.orders if o["position_side"] == "long")
    cut_short = sum(o["quantity"] for o in ex.orders if o["position_side"] == "short")
    for o in ex.orders:
        assert o["reduce_only"] is True and o["order_type"] == "market"
    return cut_long, cut_short, reduce_qty


@pytest.mark.parametrize("long_pos,short_pos", [
    (0.68, 0.66),                    # gap 0.02 < reduce_qty/2 → v2 的固定 2× 在這裡 overshoot
    (0.72, 0.66),                    # gap 0.06
    (0.82, 0.66),                    # gap 0.16 > reduce_qty
    (0.66, 0.82),                    # short 為大側
    (0.66, 0.66),                    # gap == 0
    (0.6600000000000001, 0.66),      # 浮點雜訊：不得落進錯誤分支
])
def test_reduce_never_increases_abs_delta_and_never_flips_sign(long_pos, short_pos):
    cut_long, cut_short, _ = _run(long_pos, short_pos)
    old_delta = long_pos - short_pos
    new_delta = (long_pos - cut_long) - (short_pos - cut_short)

    assert abs(new_delta) <= abs(old_delta) + 1e-12, (
        f"|delta| 變大了：{abs(old_delta):.6f} → {abs(new_delta):.6f}"
    )
    assert old_delta * new_delta >= -1e-12, (
        f"delta 變號（overshoot）：{old_delta:+.6f} → {new_delta:+.6f}"
    )


@pytest.mark.parametrize("long_pos,short_pos", [
    (0.68, 0.66), (0.82, 0.66), (0.66, 0.82), (0.66, 0.66),
])
def test_gross_strictly_decreases(long_pos, short_pos):
    cut_long, cut_short, reduce_qty = _run(long_pos, short_pos)
    gap = abs(long_pos - short_pos)
    expected_total = 2 * reduce_qty + min(reduce_qty, gap)
    assert cut_long + cut_short == pytest.approx(expected_total), "gross 下降量不符 spec §3.2"
    assert cut_long + cut_short > 0


def test_equal_sides_fall_back_to_symmetric_reduction():
    cut_long, cut_short, reduce_qty = _run(0.66, 0.66)
    assert cut_long == pytest.approx(reduce_qty)
    assert cut_short == pytest.approx(reduce_qty), "gap == 0 必須退回現行的雙側等量減倉"


def test_no_order_when_below_local_threshold():
    cfg = SymbolConfig(symbol="BNBUSDC", ccxt_symbol="BNB/USDC:USDC",
                       initial_quantity=0.02, threshold_multiplier=40.0)
    sym_state = SymbolState(symbol="BNBUSDC", long_position=0.60, short_position=0.20)
    ex = _RecordingExecutor()
    rm = RiskMonitor(config=None, state=GlobalState(), order_executor=ex, notifier=None)
    asyncio.run(rm.check_and_reduce_positions(cfg, sym_state))
    assert ex.orders == [], "生產現況（0.60/0.20）不該觸發雙向減倉"
