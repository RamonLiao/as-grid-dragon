import math
import pytest
from backtest.costs import apply_slippage


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
