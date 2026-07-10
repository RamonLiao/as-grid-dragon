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


# ──────────── 裝死模式：接管掛單所有權的極端情境 ────────────

def test_dead_mode_replaces_tp_after_place_failed_without_recancelling():
    """裝死中掛單數歸零（前一 tick 撤單成功但 place_order 回 None，或止盈已成交）：
    必須重掛，且不得再撤單。

    這是「進入裝死時撤單成功、掛單失敗」的唯一自我修復路徑。`_execute_side_decision`
    先設 dead_flag=True 再撤單/掛單（bot.py:457-473），所以下個 tick entering=False；
    若此時 `pending_tp <= 0` 不重掛，該側就零掛單且永遠不再 entering → 新死鎖。
    重撤單則會在每個 tick（生產約 6 秒）打斷路器。
    """
    dec = d.decide(_inp(long_position=70, short_position=0,
                        buy_long_orders=0, sell_long_orders=0, long_dead_mode=True))
    s = dec.long
    assert s.enter_dead_mode is False and s.exit_dead_mode is False
    assert s.cancel_side is False, "已在裝死中不得重複撤單（每 tick 撤單會觸發斷路器）"
    assert len(s.orders) == 1 and s.orders[0].reduce_only is True


def test_dead_mode_entry_emits_exactly_one_tp_regardless_of_pending():
    """entering 時無論帳上有無殘留止盈單，都只掛一張特殊止盈（不重複掛）。"""
    for pending in (0, 1e-12, 0.02, 1e9):
        dec = d.decide(_inp(long_position=70, short_position=0, buy_long_orders=0,
                            sell_long_orders=pending, long_dead_mode=False))
        s = dec.long
        assert s.enter_dead_mode is True and s.cancel_side is True
        tps = [o for o in s.orders if o.reduce_only]
        assert len(tps) == 1, f"pending={pending} 掛出 {len(tps)} 張止盈"
        assert not [o for o in s.orders if not o.reduce_only], "裝死不得補倉"


def test_dead_mode_boundary_position_equals_threshold_is_not_dead():
    """my_pos == threshold 不算裝死（is_dead_mode 嚴格 >）→ 走正常分支、照常補倉。"""
    dec = d.decide(_inp(long_position=60, position_threshold=60,
                        buy_long_orders=0, sell_long_orders=0, long_dead_mode=False))
    s = dec.long
    assert s.enter_dead_mode is False
    assert [o for o in s.orders if not o.reduce_only], "未達門檻應照常掛開倉單"


@pytest.mark.parametrize("opp", [1e-9, 1e-3, 1e9])
def test_dead_mode_entry_price_finite_for_extreme_opposite_position(opp):
    """dead_mode_price 的 r = (my/opp)/100 + 1：對手倉極小 → r 爆炸。
    價格必須有限且方向正確（long 掛在現價之上），不得 inf/nan 溢出到下單路徑。
    """
    dec = d.decide(_inp(long_position=70, short_position=opp, buy_long_orders=0,
                        sell_long_orders=0, long_dead_mode=False))
    tp = [o for o in dec.long.orders if o.reduce_only][0]
    assert math.isfinite(tp.price) and tp.price > 2.5


def test_dead_mode_exit_resumes_normal_grid_and_cancels():
    """倉位降回 threshold 以下 → exit_dead_mode + 撤單 + 恢復止盈/補倉雙單。
    死鎖的反面：證明離開裝死這條路存在且會重建網格。"""
    dec = d.decide(_inp(long_position=10, position_threshold=60,
                        buy_long_orders=0, sell_long_orders=0, long_dead_mode=True))
    s = dec.long
    assert s.exit_dead_mode is True and s.cancel_side is True
    assert len([o for o in s.orders if o.reduce_only]) == 1
    assert len([o for o in s.orders if not o.reduce_only]) == 1
