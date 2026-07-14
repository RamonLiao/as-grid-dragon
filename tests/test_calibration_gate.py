"""Task 9 校準 gate：三個 judge 純函數的邊界測試 + Step 0（factor 接進 1m backtester）接線證明。

judge 函數是 go/no-go 判定的唯一權威；script 本體是薄殼（下載/模擬/彙總），
不在單元測試覆蓋範圍（實跑於 Step 5）。
"""
import datetime as dt

import pandas as pd
import pytest

from scripts.calibration_gate import (
    judge_low_gate, judge_high_gate, judge_june_alignment,
)


# ---------------------------------------------------------------------------
# judge_low_gate: sim_fills_per_day <= 2.0（live≈0，sim 不得虛增成交）
# ---------------------------------------------------------------------------
def test_low_gate_pass_at_boundary():
    assert judge_low_gate(2.0) is True


def test_low_gate_fail_just_over():
    assert judge_low_gate(2.1) is False


def test_low_gate_pass_zero():
    assert judge_low_gate(0.0) is True


# ---------------------------------------------------------------------------
# judge_high_gate: 0.2*bar <= tick <= 1.0*bar；bar==0 -> False（要求換窗口）
# ---------------------------------------------------------------------------
def test_high_gate_pass_lower_boundary():
    # bar=10 -> 0.2*10=2.0；tick=2 恰在下界
    assert judge_high_gate(2, 10) is True


def test_high_gate_fail_below_lower_boundary():
    assert judge_high_gate(1, 10) is False


def test_high_gate_pass_upper_boundary():
    # tick=bar 恰在上界（tick 不得多於 bar）
    assert judge_high_gate(10, 10) is True


def test_high_gate_fail_above_upper_boundary():
    assert judge_high_gate(11, 10) is False


def test_high_gate_bar_zero_forces_false():
    # bar==0：分母失效，強制 False（mutation 目標：改成 True 必紅）
    assert judge_high_gate(0, 0) is False
    assert judge_high_gate(5, 0) is False


# ---------------------------------------------------------------------------
# judge_june_alignment:
#   (live>0 的日子 sim 也 >0 的比例 >= 0.5) AND (sim 月總量 <= 10x live 月總量)
# ---------------------------------------------------------------------------
def test_june_alignment_pass_ratio_boundary():
    # live 活躍 2 天，sim 命中 1 天 -> ratio 0.5 恰達門檻；量級 2 <= 10*2
    live = {"2026-06-17": 1, "2026-06-19": 1}
    sim = {"2026-06-17": 1, "2026-06-20": 1}
    assert judge_june_alignment(sim, live) is True


def test_june_alignment_fail_ratio_below():
    # live 活躍 2 天，sim 命中 0 天 -> ratio 0.0
    live = {"2026-06-17": 1, "2026-06-19": 1}
    sim = {"2026-06-01": 1}
    assert judge_june_alignment(sim, live) is False


def test_june_alignment_fail_magnitude():
    # ratio 命中（1/1）但 sim 月總量 21 > 10*live 總量 2 -> FAIL
    live = {"2026-06-17": 2}
    sim = {"2026-06-17": 21}
    assert judge_june_alignment(sim, live) is False


def test_june_alignment_pass_magnitude_boundary():
    # sim 總量 20 == 10*live 總量 2 恰在上界
    live = {"2026-06-17": 1, "2026-06-19": 1}
    sim = {"2026-06-17": 10, "2026-06-19": 10}
    assert judge_june_alignment(sim, live) is True


def test_june_alignment_no_live_activity_false():
    # live 全 0 -> 無可對齊基準，保守 False
    assert judge_june_alignment({"2026-06-17": 5}, {"2026-06-17": 0}) is False


# ---------------------------------------------------------------------------
# Step 0：Config.requote_threshold_factor 接進 1m backtester 的 DecisionInputs
# ---------------------------------------------------------------------------
def _tiny_df() -> pd.DataFrame:
    base = dt.datetime(2026, 6, 6, tzinfo=dt.timezone.utc)
    rows = []
    for i in range(30):
        px = 600.0 + (i % 5) * 0.5   # 有微幅波動，觸發 should_adjust 分支
        rows.append({
            "open_time": base + dt.timedelta(minutes=i),
            "open": px, "high": px + 0.3, "low": px - 0.3,
            "close": px, "volume": 100.0,
        })
    return pd.DataFrame(rows)


def test_step0_backtester_threads_requote_factor(monkeypatch):
    """spy decide()：捕捉每次 DecisionInputs.requote_threshold_factor。
    接線正確 -> 全部等於 cfg.requote_threshold_factor(1.0)。
    mutation：拔掉 backtester.py 的 requote_threshold_factor=cfg.xxx 一行
    -> 退回 DecisionInputs 預設 0.5 -> 本測試紅。"""
    import backtest.backtester as bt
    from grid_engine.decision import decide as real_decide
    from backtest.config import Config

    seen = []

    def spy(inputs):
        seen.append(inputs.requote_threshold_factor)
        return real_decide(inputs)

    monkeypatch.setattr(bt, "decide", spy)

    cfg = Config(symbol="BNBUSDC", initial_quantity=0.02, initial_balance=1000.0,
                 leverage=5, direction="both", requote_threshold_factor=1.0,
                 funding_enabled=False)
    bt.GridBacktester(_tiny_df(), cfg, funding_map={}).run()

    assert seen, "decide 未被呼叫，測試無效"
    assert all(f == 1.0 for f in seen), \
        f"requote_threshold_factor 未接線：見到 {set(seen)}，預期全 1.0"


def test_step0_default_factor_is_half(monkeypatch):
    """未指定時 backtester 傳入 0.5（歷史 hardcode）——保證預設 bit-identical 語意。"""
    import backtest.backtester as bt
    from grid_engine.decision import decide as real_decide
    from backtest.config import Config

    seen = []
    monkeypatch.setattr(bt, "decide",
                        lambda inp: (seen.append(inp.requote_threshold_factor), real_decide(inp))[1])
    cfg = Config(symbol="BNBUSDC", initial_quantity=0.02, initial_balance=1000.0,
                 leverage=5, direction="both", funding_enabled=False)
    bt.GridBacktester(_tiny_df(), cfg, funding_map={}).run()
    assert seen and all(f == 0.5 for f in seen)
