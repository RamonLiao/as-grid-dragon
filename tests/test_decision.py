import pytest
from grid_engine import decision as d


def test_is_dead_mode_strict_gt():
    assert d.is_dead_mode(61, 60) is True
    assert d.is_dead_mode(60, 60) is False


def test_dead_mode_price_no_opposite_uses_fallback():
    assert d.dead_mode_price(2.5, 70, 0, "long") == pytest.approx(2.625)   # *1.05
    assert d.dead_mode_price(2.5, 70, 0, "short") == pytest.approx(2.375)  # *0.95


def test_dead_mode_price_with_opposite():
    # r = (70/35)/100 + 1 = 1.02
    assert d.dead_mode_price(2.5, 70, 35, "long") == pytest.approx(2.55)
    assert d.dead_mode_price(2.5, 70, 35, "short") == pytest.approx(2.5 / 1.02)


def test_grid_prices_long_short():
    assert d.grid_prices(2.5, 0.004, 0.006, "long") == pytest.approx((2.51, 2.485))
    assert d.grid_prices(2.5, 0.004, 0.006, "short") == pytest.approx((2.49, 2.515))


def test_tp_quantity_doubles_over_limit():
    assert d.tp_quantity(3, 20, 0, 15, 60) == 6      # my>limit
    assert d.tp_quantity(3, 10, 60, 15, 60) == 6     # opp>=threshold
    assert d.tp_quantity(3, 10, 0, 15, 60) == 3      # 不加倍


def test_glft_quantity_disabled_passthrough():
    assert d.glft_quantity(3, "long", 100, 0, glft_enabled=False, gamma=0.1) == 3


def test_glft_quantity_clamped():
    # inventory=1, long: adjust=1-1*0.1=0.9 → 2.7
    assert d.glft_quantity(3, "long", 100, 0, glft_enabled=True, gamma=0.1) == pytest.approx(2.7)


def _inputs(**kw):
    base = dict(
        price=2.5, long_position=10, short_position=0,
        buy_long_orders=1, sell_long_orders=1, buy_short_orders=1, sell_short_orders=1,
        last_grid_price_long=2.5, last_grid_price_short=2.5,
        long_dead_mode=False, short_dead_mode=False,
        grid_spacing=0.006, take_profit_spacing=0.004,
        initial_quantity=3, position_threshold=60, position_limit=15,
        glft_enabled=False, gamma=0.1,
        enh=d.EnhancementSnapshot(dynamic_take_profit=0.004, dynamic_grid_spacing=0.006,
                                  funding_long_bias=1.0, funding_short_bias=1.0),
    )
    base.update(kw)
    return d.DecisionInputs(**base)


def test_should_adjust_no_orders():
    assert d.should_adjust(_inputs(buy_long_orders=0), "long") is True


def test_should_adjust_deviation():
    assert d.should_adjust(_inputs(price=2.5, last_grid_price_long=2.5), "long") is False
    assert d.should_adjust(_inputs(price=2.52, last_grid_price_long=2.5), "long") is True


def test_compute_quantity_tp_double_then_funding_clamp():
    inp = _inputs(long_position=20, enh=d.EnhancementSnapshot(
        0.004, 0.006, funding_long_bias=1.2, funding_short_bias=0.8))
    # tp: double(20>15) →6 ×1.2 =7.2
    assert d.compute_quantity(inp, "long", True) == pytest.approx(7.2)


def test_decide_normal_long_full():
    # sell_long_orders=0（無掛單）觸發 should_adjust True；price 維持 2.5 供 tp/entry 價驗算
    dec = d.decide(_inputs(long_position=10, sell_long_orders=0))
    s = dec.long
    assert s.should_adjust is True and s.cancel_side is True
    assert s.new_anchor_price == pytest.approx(2.5)
    intents = {(o.side, o.reduce_only): o for o in s.orders}
    assert intents[("sell", True)].price == pytest.approx(2.51)   # tp
    assert intents[("buy", False)].price == pytest.approx(2.485)  # entry


def test_decide_dead_long_enter():
    dec = d.decide(_inputs(long_position=70, sell_long_orders=0, long_dead_mode=False))
    s = dec.long
    assert s.enter_dead_mode is True and s.cancel_side is False
    assert len(s.orders) == 1 and s.orders[0].price == pytest.approx(2.625)


def test_decide_side_not_adjust_returns_empty():
    # 有掛單且零偏離 → should_adjust False → 無 orders、無 transition
    dec = d.decide(_inputs(long_position=10, price=2.5, last_grid_price_long=2.5))
    assert dec.long.should_adjust is False
    assert dec.long.orders == () and dec.long.new_anchor_price is None
