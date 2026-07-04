import math
import pytest
from backtest.costs import apply_slippage, funding_charge


def test_apply_slippage_long_entry_buys_worse():
    # long 進場：買貴 → price*(1+bps)
    assert apply_slippage(100.0, "long", "entry", 0.0001) == pytest.approx(100.01)


def test_apply_slippage_long_tp_sells_worse():
    # long 止盈：賣便宜 → price*(1-bps)
    assert apply_slippage(100.0, "long", "tp", 0.0001) == pytest.approx(99.99)


def test_apply_slippage_short_entry_sells_worse():
    # short 進場（賣）：賣便宜 → price*(1-bps)
    assert apply_slippage(100.0, "short", "entry", 0.0001) == pytest.approx(99.99)


def test_apply_slippage_short_tp_buys_worse():
    # short 止盈（買回）：買貴 → price*(1+bps)
    assert apply_slippage(100.0, "short", "tp", 0.0001) == pytest.approx(100.01)


def test_apply_slippage_zero_bps_no_shift():
    assert apply_slippage(100.0, "long", "entry", 0.0) == 100.0


def test_apply_slippage_negative_bps_treated_as_zero():
    assert apply_slippage(100.0, "long", "entry", -0.5) == 100.0


def test_apply_slippage_nan_bps_treated_as_zero():
    assert apply_slippage(100.0, "long", "entry", float("nan")) == 100.0


def test_funding_charge_long_positive_rate_pays():
    # notional = 10 * 100 = 1000；rate 0.0001 → long 付 0.1
    assert funding_charge([{"qty": 10.0}], 0.0001, "long", 100.0) == pytest.approx(0.1)


def test_funding_charge_short_positive_rate_receives():
    # 正 rate 空頭收錢 → charge 為負（呼叫端 balance -= 負 = 增加）
    assert funding_charge([{"qty": 10.0}], 0.0001, "short", 100.0) == pytest.approx(-0.1)


def test_funding_charge_long_negative_rate_receives():
    assert funding_charge([{"qty": 10.0}], -0.0001, "long", 100.0) == pytest.approx(-0.1)


def test_funding_charge_sums_notional_across_positions():
    # (4+6)*100*0.0001 = 0.1
    assert funding_charge([{"qty": 4.0}, {"qty": 6.0}], 0.0001, "long", 100.0) == pytest.approx(0.1)


def test_funding_charge_empty_positions_zero():
    assert funding_charge([], 0.0001, "long", 100.0) == 0.0


def test_funding_charge_nan_rate_zero():
    assert funding_charge([{"qty": 10.0}], float("nan"), "long", 100.0) == 0.0
