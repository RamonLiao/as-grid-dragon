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


def test_tp_quantity_doubles_only_for_the_net_exposure_side():
    """止盈量加倍只給「淨曝險方向」那側（spec §3.1）。

    對沖側（較小側）維持 1×，否則 0.02進/0.04出 的不對稱會系統性拆掉人工建立的
    對沖（2026-07-26 實盤 11 天實證：空頭 0.36→0.20）。
    """
    # my > limit 且 my > opposite → 加倍
    assert d.tp_quantity(3, 20, 10, 15) == 6
    assert d.tp_quantity(3, 20, 0, 15) == 6

    # my > limit 但 my <= opposite（我是對沖側）→ 不加倍
    assert d.tp_quantity(3, 20, 20, 15) == 3, "兩側相等時不得加倍（嚴格大於）"
    assert d.tp_quantity(3, 20, 30, 15) == 3, "我是較小側 → 不得加倍（否則拆對沖）"

    # my <= limit → 不加倍（無論對手側多大）
    assert d.tp_quantity(3, 15, 0, 15) == 3, "position_limit 是嚴格大於"
    assert d.tp_quantity(3, 10, 0, 15) == 3

    # spec §4 向量 3：裝死側反而是較小側（對手更大）→ 新規則不加倍。
    # 這是**刻意的行為變更**（裝死出清變慢）；舊規則會因 my>limit 而加倍。
    assert d.tp_quantity(3, 61, 70, 15) == 3, (
        "裝死側若同時是較小側，止盈量不再加倍——刻意的行為變更（spec §4 向量 3）"
    )


def test_tp_quantity_no_longer_doubles_on_large_opposite():
    """已刪除 `or opposite_position >= position_threshold` 子條件（spec §1）。

    該子條件唯一可達且有效的情形是「我不是淨曝險側時仍加倍我」= 最大化拆對沖；
    全量 logs/decisions.jsonl（99,270 筆）實測 98,399 筆屬此類。
    """
    # 舊規則會因 opposite=60 >= threshold=60 而回 6；新規則不看對手側是否超門檻
    assert d.tp_quantity(3, 10, 60, 15) == 3


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
    # 進入裝死 → 接管整側（撤舊單）+ 掛出 dead_mode_price 的特殊止盈
    dec = d.decide(_inputs(long_position=70, sell_long_orders=0, long_dead_mode=False))
    s = dec.long
    assert s.enter_dead_mode is True and s.cancel_side is True
    assert len(s.orders) == 1 and s.orders[0].price == pytest.approx(2.625)


def test_entering_dead_mode_takes_over_stale_tp_so_position_can_escape():
    """進入裝死模式時，帳上可能還有正常模式掛的止盈單（價格用 grid_prices 算，
    與 dead_mode_price 無關）。裝死模式必須接管止盈單的所有權：撤掉舊的、
    掛上自己依失衡比例算出的那張。

    否則殘留單讓 pending_tp > 0，裝死分支的 `if pending_tp <= 0` 永不成立，
    dead_mode_price() 一次都不會執行；而 cancel=False 又不撤舊單。倉位因此
    永遠降不到 threshold 以下，exit_dead_mode 永不觸發 —— 該側網格永久停擺。

    生產實證：logs/decisions.jsonl 63619 筆 / 104.5h，long 側 has_tp=0%、
    exit_dead=0、long_position 恆為 0.58（threshold=0.4）。
    """
    dec = d.decide(_inputs(
        long_position=70,        # > threshold(60) → 裝死
        short_position=0,        # 無對手倉 → fallback 1.05 → 2.5*1.05 = 2.625
        buy_long_orders=0,       # 裝死不補倉（與生產一致），順帶讓 should_adjust=True
        sell_long_orders=1,      # ← 正常模式殘留的止盈單
        long_dead_mode=False,    # 這個 tick 才進入裝死
    ))
    s = dec.long

    assert s.enter_dead_mode is True
    assert s.cancel_side is True, "進入裝死必須撤掉正常模式殘留的止盈單"

    tps = [o for o in s.orders if o.reduce_only]
    assert len(tps) == 1, "進入裝死必須掛出自己的特殊止盈單"
    assert tps[0].price == pytest.approx(2.625)
    assert tps[0].quantity == pytest.approx(6.0)   # tp_quantity: 70 > limit(15) → 3*2

    assert not [o for o in s.orders if not o.reduce_only], "裝死模式不得補倉"


def test_decide_side_not_adjust_returns_empty():
    # 有掛單且零偏離 → should_adjust False → 無 orders、無 transition
    dec = d.decide(_inputs(long_position=10, price=2.5, last_grid_price_long=2.5))
    assert dec.long.should_adjust is False
    assert dec.long.orders == () and dec.long.new_anchor_price is None
