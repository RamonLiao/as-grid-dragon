"""雙向減倉的三條不變式（spec §3.2）。

斷言不變式而非逐案例列舉——v2 的「大側固定 2×」在 gap < reduce_qty/2 時會把
|delta| 推大（反例 0.68/0.66：0.02 → 0.06），而觸發後雙側都掉到門檻以下、
沒有下一輪可以修正。

⚠️ 三條不變式成立於量化前；`place_order` 的 round/min_amount 之後，實際下單量的
|delta| 最壞可差一個 amount tick。本檔不覆蓋量化路徑。

⚠️ 本觸發路徑在現行資本下不可達（雙側同時 ≥0.64 所需保證金遠超可用餘額）⇒
本檔是 regression guard，不是 live fix 的證據。
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
    reduce_qty = cfg.position_threshold * 0.1
    local_threshold = cfg.position_threshold * 0.8
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


@pytest.mark.parametrize("long_pos,short_pos", [
    (0.68, 0.66), (0.72, 0.66), (0.82, 0.66), (0.66, 0.82),
])
def test_reduce_strictly_converges_when_gap_is_nonzero(long_pos, short_pos):
    """gap > 0 時必須嚴格收斂——這是本改動的核心賣點，需要一條非套套邏輯的守衛。

    test_gross_strictly_decreases 是把實作公式抄一遍，只能防回歸；本條用「收斂」
    這個獨立敘述表達，能殺掉 extra=0（退回等量減倉）與 extra=gap/2（收斂不足）等所有 mutation。
    """
    cut_long, cut_short, _ = _run(long_pos, short_pos)
    old_delta = long_pos - short_pos
    new_delta = (long_pos - cut_long) - (short_pos - cut_short)
    assert abs(new_delta) < abs(old_delta) - 1e-12, (
        f"gap>0 卻沒有嚴格收斂：|{old_delta:+.6f}| → |{new_delta:+.6f}|"
    )


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


def test_no_order_when_only_one_side_exceeds_threshold():
    """觸發條件是 and 不是 or：單側過大不是「雙向對沖過大」，不得減倉。

    若誤寫成 or，long=0.70/short=0.60 會觸發並送出一張 sell 2×reduce_qty 的市價單，
    把單側倉位當成對沖來砍。
    """
    cfg = SymbolConfig(symbol="BNBUSDC", ccxt_symbol="BNB/USDC:USDC",
                       initial_quantity=0.02, threshold_multiplier=40.0)
    sym_state = SymbolState(symbol="BNBUSDC", long_position=0.70, short_position=0.60)
    ex = _RecordingExecutor()
    rm = RiskMonitor(config=None, state=GlobalState(), order_executor=ex, notifier=None)
    asyncio.run(rm.check_and_reduce_positions(cfg, sym_state))
    assert ex.orders == [], "單側超過門檻不得觸發雙向減倉（and 不是 or）"


def test_reduce_records_cooldown_timestamp_and_second_call_is_throttled():
    """last_reduce_time 是唯一的節流，而 bot.py 每個 ticker tick 都會呼叫。

    少了它，60s 內的每一次 tick 都會再送兩張市價單——風險是 gross 被重複削減、
    繞過節流（delta 不會失控：min(reduce_qty, gap) 的夾制保證收斂到 0 不會越過）。
    """
    cfg = SymbolConfig(symbol="BNBUSDC", ccxt_symbol="BNB/USDC:USDC",
                       initial_quantity=0.02, threshold_multiplier=40.0)
    state = GlobalState()
    sym_state = SymbolState(symbol="BNBUSDC", long_position=0.82, short_position=0.66)
    ex = _RecordingExecutor()
    rm = RiskMonitor(config=None, state=state, order_executor=ex, notifier=None)

    asyncio.run(rm.check_and_reduce_positions(cfg, sym_state))
    assert len(ex.orders) == 2, "首次觸發應送出兩張單"
    assert state.last_reduce_time.get(cfg.ccxt_symbol), "必須寫回 last_reduce_time，否則沒有節流"

    # 立即第二次呼叫：cooldown 內不得再下單（倉位刻意維持在觸發條件之上）
    asyncio.run(rm.check_and_reduce_positions(cfg, sym_state))
    assert len(ex.orders) == 2, "cooldown 內不得重複下單"


def test_no_order_when_threshold_is_non_positive():
    """`position_threshold <= 0` 時一張單都不能送（dual-review 外部輪 Nit）。

    threshold 為 0 或負時 local_threshold <= 0 ⇒ 任何非負持倉都滿足觸發條件，
    而 reduce_qty <= 0 會讓 place_order 收到 0 或負的量——`order_executor` 的
    precision fallback 是 `min_amount: 1`，等於送出 1 顆 BNB 的市價單（≈570 USDC）。
    不對稱減倉還會把負值的量放大成 2×，所以守衛必須擋在計算之後、下單之前。
    """
    for initial_quantity, threshold_multiplier in ((0.0, 40.0), (0.02, 0.0), (0.02, -40.0)):
        cfg = SymbolConfig(symbol="BNBUSDC", ccxt_symbol="BNB/USDC:USDC",
                           initial_quantity=initial_quantity,
                           threshold_multiplier=threshold_multiplier)
        assert cfg.position_threshold <= 0, f"前置條件：{initial_quantity}×{threshold_multiplier}"

        # 持倉刻意取非零正值：threshold <= 0 時它必然「超過」門檻，是最容易誤觸的狀態
        sym_state = SymbolState(symbol="BNBUSDC", long_position=0.42, short_position=0.18)
        ex = _RecordingExecutor()
        rm = RiskMonitor(config=None, state=GlobalState(), order_executor=ex, notifier=None)
        asyncio.run(rm.check_and_reduce_positions(cfg, sym_state))
        assert ex.orders == [], (
            f"position_threshold={cfg.position_threshold} 不得送出任何減倉單，實際 {ex.orders}")
