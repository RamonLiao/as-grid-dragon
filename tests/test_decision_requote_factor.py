"""requote_threshold_factor：追價門檻參數化。

門檻語意：偏離 anchor >= grid_spacing * factor 才撤單重掛。
factor=0.5 是現行 hardcode 行為；1.0 = 掛單掛到價格走完整個 spacing 才動。
"""
import dataclasses
from grid_engine.decision import DecisionInputs, EnhancementSnapshot, should_adjust, decide

def _inputs(price, anchor, factor=None, **kw):
    enh = EnhancementSnapshot(dynamic_take_profit=0.003, dynamic_grid_spacing=0.003,
                              funding_long_bias=1.0, funding_short_bias=1.0)
    base = dict(price=price, long_position=0.1, short_position=0.1,
                buy_long_orders=0.02, sell_long_orders=0.02,
                buy_short_orders=0.02, sell_short_orders=0.02,
                last_grid_price_long=anchor, last_grid_price_short=anchor,
                long_dead_mode=False, short_dead_mode=False,
                grid_spacing=0.003, take_profit_spacing=0.003,
                initial_quantity=0.02, position_threshold=0.8, position_limit=0.1,
                glft_enabled=False, gamma=0.1, enh=enh)
    base.update(kw)
    if factor is not None:
        base["requote_threshold_factor"] = factor
    return DecisionInputs(**base)

def test_default_factor_is_half():
    """不傳 factor → 預設 0.5 = 現行為（bit-identical 保證的基石）"""
    assert DecisionInputs.__dataclass_fields__["requote_threshold_factor"].default == 0.5

def test_factor_half_adjusts_at_015pct():
    # 偏離 0.16% > 0.003*0.5=0.15% → 觸發
    assert should_adjust(_inputs(price=100.16, anchor=100.0), "long") is True

def test_factor_one_holds_at_015pct():
    # 同樣偏離 0.16%，factor=1.0 門檻 0.3% → 不觸發（新語意的核心差異）
    assert should_adjust(_inputs(price=100.16, anchor=100.0, factor=1.0), "long") is False

def test_factor_one_adjusts_at_031pct():
    assert should_adjust(_inputs(price=100.31, anchor=100.0, factor=1.0), "long") is True

def test_missing_orders_adjusts_regardless_of_factor():
    # 單側掛單缺失 → 無條件重掛，factor 不擋（否則成交後永不補掛）
    assert should_adjust(_inputs(price=100.0, anchor=100.0, factor=1.0,
                                 buy_long_orders=0.0), "long") is True

def test_decide_bit_identical_with_default_factor():
    """同 inputs 加不加顯式 factor=0.5，decide() 全欄位相同"""
    a = decide(_inputs(price=100.2, anchor=100.0))
    b = decide(_inputs(price=100.2, anchor=100.0, factor=0.5))
    assert dataclasses.asdict(a) == dataclasses.asdict(b)
