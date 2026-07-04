import math
import pytest
from grid_engine import decision as d


def _inp(**kw):
    from tests.test_decision import _inputs  # 複用
    return _inputs(**kw)


@pytest.mark.parametrize("price", [0.0, -1.0, 1e12])
def test_decide_extreme_price_no_crash(price):
    dec = d.decide(_inp(price=price, long_position=10))
    assert isinstance(dec.long.orders, tuple)


def test_decide_position_far_over_limit():
    dec = d.decide(_inp(long_position=1e9, sell_long_orders=0))
    assert dec.long.enter_dead_mode is True


def test_decide_zero_anchor_forces_adjust():
    assert d.should_adjust(_inp(last_grid_price_long=0, buy_long_orders=1, sell_long_orders=1), "long") is True


def test_glft_extreme_gamma_still_clamped():
    q = d.glft_quantity(3, "long", 100, 0, glft_enabled=True, gamma=1e6)
    assert q == pytest.approx(1.5)  # adjust clamp 上限 → 3*0.5? 下限0.5 → 1.5
