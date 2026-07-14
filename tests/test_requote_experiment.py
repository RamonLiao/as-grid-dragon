"""Task 10 實驗矩陣 runner 的純函數測試（cell 建構 / 事件數守門 / §6 判準預判）。

判準預判（verdict_preview）是 §6.1-6.6 逐條 PASS/FAIL/inconclusive 的唯一權威；
script 本體（下載/模擬/彙總/報告）為薄殼，實跑於 Step 5，不在單元測試覆蓋。

mutation 目標（dev-rules L4：每個守衛先紅一次）：
- bps_to_fraction 的 1e-4 → 1e-3 必紅（test_bps_to_fraction_pins_1bp）
- verdict_preview §6.3「達標段 < 2 → inconclusive」→ 改 < 1 必紅
  （test_verdict_63_inconclusive_single_segment）
"""
import pytest

from scripts.requote_experiment import (
    bps_to_fraction, build_matrix, gate_cells, verdict_preview,
)


# ---------------------------------------------------------------------------
# bps_to_fraction：底層 fee/slip 儲存為 fraction，展開必乘 1e-4（SF-6 latent bug）
# ---------------------------------------------------------------------------
def test_bps_to_fraction_pins_1bp():
    # mutation：1e-4 -> 1e-3 這行紅
    assert bps_to_fraction(1) == pytest.approx(0.0001)


def test_bps_to_fraction_zero():
    assert bps_to_fraction(0) == 0.0


def test_bps_to_fraction_matches_prod_baseline():
    # PROD fee_pct=0.0002(=2bps)、slippage_bps=0.0001(=1bp)：基準 cost 對得上
    assert bps_to_fraction(2) == pytest.approx(0.0002)
    assert bps_to_fraction(4) == pytest.approx(0.0004)


# ---------------------------------------------------------------------------
# build_matrix：組合數正確、不含 holdout（05-01~06-05）、factor 集合正確
# ---------------------------------------------------------------------------
def _windows():
    return {
        "W1": ("2026-06-06", "2026-06-18"),
        "W2": ("2026-06-19", "2026-06-30"),
        "W3": ("2026-07-01", "2026-07-10"),
        "full": ("2026-06-06", "2026-07-13"),
    }


def test_build_matrix_combination_count():
    cells = build_matrix(_windows())
    # main: 3 factor × 2 scenario × 4 window × 6 cost = 144
    # delay sweep: 3 factor × 2 scenario × full × baseline cost × {0,1000}ms = 12
    main = [c for c in cells if c.group == "main"]
    delay = [c for c in cells if c.group == "delay"]
    assert len(main) == 144
    assert len(delay) == 12
    assert len(cells) == 156


def test_build_matrix_no_holdout_dates():
    # holdout 05-01~06-05 絕不出現在任何 cell（被讀過一次即失效）
    for c in build_matrix(_windows()):
        assert c.win_start >= "2026-06-06", f"cell 觸及 holdout: {c}"


def test_build_matrix_factor_set():
    assert {c.factor for c in build_matrix(_windows())} == {0.5, 1.0, 1.5}


def test_build_matrix_main_cells_default_delay():
    for c in build_matrix(_windows()):
        if c.group == "main":
            assert c.delay_ms == 500
            assert c.cooldown_sec == 5.0


def test_build_matrix_delay_cells_full_window_baseline():
    for c in build_matrix(_windows()):
        if c.group == "delay":
            assert c.window == "full"
            assert (c.fee_bps, c.slip_bps) == (2, 1)   # 基準 cost
            assert c.delay_ms in (0, 1000)


# ---------------------------------------------------------------------------
# gate_cells：獨立事件數（round_trips）< min_events 過濾（spec §5 / §6.3）
# ---------------------------------------------------------------------------
def _res(**kw):
    base = dict(factor=1.0, scenario="A", window="full", group="main",
                fee_bps=2, slip_bps=1, delay_ms=500, cooldown_sec=5.0,
                final_equity=200.0, delta_eq=0.0, max_dd=0.0, liquidated=False,
                round_trips=50, rejected_rate=0.0, requote_count=0)
    base.update(kw)
    return base


def test_gate_cells_filters_below_min():
    results = [_res(round_trips=30), _res(round_trips=29), _res(round_trips=100)]
    kept = gate_cells(results, min_events=30)
    assert len(kept) == 2
    assert all(r["round_trips"] >= 30 for r in kept)


def test_gate_cells_boundary_inclusive():
    assert len(gate_cells([_res(round_trips=30)], min_events=30)) == 1
    assert len(gate_cells([_res(round_trips=29)], min_events=30)) == 0


# ---------------------------------------------------------------------------
# verdict_preview §6.2：factor 1.0 零強平（兩場景全窗口）
# ---------------------------------------------------------------------------
def test_verdict_62_pass_no_liquidation():
    results = [_res(factor=1.0, liquidated=False), _res(factor=1.0, window="W1", liquidated=False)]
    assert verdict_preview(results)["6.2"] == "PASS"


def test_verdict_62_fail_any_liquidation():
    results = [_res(factor=1.0, liquidated=False), _res(factor=1.0, window="W1", liquidated=True)]
    assert verdict_preview(results)["6.2"] == "FAIL"


# ---------------------------------------------------------------------------
# verdict_preview §6.3：新語意 Δeq W1/W2/W3 全 ≥ 舊 + 全程為正；
#   只計事件≥30 的 cell；達標段 < 2 → inconclusive（PASS/FAIL/inconclusive 三態）
# ---------------------------------------------------------------------------
def _seg(window, delta_eq, round_trips=50, scenario="A"):
    return _res(factor=1.0, scenario=scenario, window=window,
                fee_bps=2, slip_bps=1, delta_eq=delta_eq, round_trips=round_trips)


def test_verdict_63_pass():
    results = [
        _seg("W1", 5.0), _seg("W2", 3.0), _seg("W3", 1.0),
        _seg("full", 10.0),
    ]
    assert verdict_preview(results)["6.3"] == "PASS"


def test_verdict_63_fail_negative_segment():
    results = [
        _seg("W1", 5.0), _seg("W2", -2.0), _seg("W3", 1.0),
        _seg("full", 10.0),
    ]
    assert verdict_preview(results)["6.3"] == "FAIL"


def test_verdict_63_fail_full_not_positive():
    results = [
        _seg("W1", 5.0), _seg("W2", 3.0),
        _seg("full", -1.0),
    ]
    assert verdict_preview(results)["6.3"] == "FAIL"


def test_verdict_63_inconclusive_single_segment():
    # 只有 1 段達 ≥30 事件 → 達標段 < 2 → inconclusive
    # mutation：門檻 2 -> 1 時，此案例會被判成別的結果 → 紅
    results = [
        _seg("W1", 5.0, round_trips=50),
        _seg("W2", 3.0, round_trips=10),   # 事件不足，不計
        _seg("W3", 1.0, round_trips=5),    # 事件不足，不計
        _seg("full", 10.0, round_trips=50),
    ]
    assert verdict_preview(results)["6.3"] == "inconclusive"


def test_verdict_63_inconclusive_no_qualifying_segment():
    results = [
        _seg("W1", 5.0, round_trips=10),
        _seg("full", 10.0, round_trips=50),
    ]
    assert verdict_preview(results)["6.3"] == "inconclusive"


# ---------------------------------------------------------------------------
# verdict_preview §6.5：場景 A 拒單率 > 30% → 降級（DEGRADE），否則 PASS
# ---------------------------------------------------------------------------
def test_verdict_65_pass_low_reject():
    results = [_res(factor=1.0, scenario="A", window="W1", rejected_rate=0.1)]
    assert verdict_preview(results)["6.5"] == "PASS"


def test_verdict_65_degrade_high_reject():
    results = [_res(factor=1.0, scenario="A", window="W2", rejected_rate=0.35)]
    assert verdict_preview(results)["6.5"] == "DEGRADE"


# ---------------------------------------------------------------------------
# verdict_preview §6.6：優勝者穩健掃描未提供 → inconclusive；提供則 PASS/FAIL
# ---------------------------------------------------------------------------
def test_verdict_66_inconclusive_without_sweep():
    assert verdict_preview([_res()])["6.6"] == "inconclusive"


def test_verdict_66_pass_with_sweep_ok():
    assert verdict_preview([_res()], winner_sweep={"no_lone_peak": True})["6.6"] == "PASS"


def test_verdict_66_fail_with_lone_peak():
    assert verdict_preview([_res()], winner_sweep={"no_lone_peak": False})["6.6"] == "FAIL"


# ---------------------------------------------------------------------------
# summarize_sweep：孤峰判定（spec §5/§6.6：任一擾動下 Δeq 排序翻轉或衰減 >50% = 孤峰）
#   逐場景判定，不得跨場景加總掩蓋翻轉
# ---------------------------------------------------------------------------
from scripts.requote_experiment import summarize_sweep


def _sw(factor, scenario, cooldown, delta):
    return _res(factor=factor, scenario=scenario, window="full", group="winner",
                cooldown_sec=cooldown, delta_eq=delta)


def _main_winner(scenario, delta):
    return _res(factor=1.0, scenario=scenario, window="full", group="main",
                fee_bps=2, slip_bps=1, delta_eq=delta)


def test_sweep_no_lone_peak_when_robust():
    main = [_main_winner("A", 10.0), _main_winner("B", 9.0)]
    sweep = [_sw(0.8, "A", 5.0, 8.0), _sw(1.2, "A", 5.0, 7.0),
             _sw(1.0, "A", 2.5, 9.0), _sw(1.0, "A", 10.0, 8.5),
             _sw(0.8, "B", 5.0, 8.0), _sw(1.2, "B", 5.0, 7.0),
             _sw(1.0, "B", 2.5, 9.0), _sw(1.0, "B", 10.0, 8.0)]
    summary, ok = summarize_sweep(sweep, main, 1.0)
    assert ok is True


def test_sweep_ranking_flip_neighbor_beats_winner():
    # 鄰點 factor 0.8 的 Δeq 遠高於優勝 → 排序翻轉 → 孤峰（不採納單點）
    main = [_main_winner("A", 0.3), _main_winner("B", 9.0)]
    sweep = [_sw(0.8, "A", 5.0, 14.9), _sw(1.2, "A", 5.0, 0.1),
             _sw(0.8, "B", 5.0, 14.5), _sw(1.2, "B", 5.0, 9.0)]
    summary, ok = summarize_sweep(sweep, main, 1.0)
    assert ok is False
    assert "翻轉" in summary


def test_sweep_cooldown_sign_flip_per_scenario_not_masked():
    # 場景 A 在 cooldown 擾動下 Δeq 翻負（輸給 0.5），不得被 B 的大正值加總掩蓋
    main = [_main_winner("A", 0.3), _main_winner("B", 9.2)]
    sweep = [_sw(1.0, "A", 10.0, -0.5), _sw(1.0, "B", 10.0, 8.4)]
    summary, ok = summarize_sweep(sweep, main, 1.0)
    assert ok is False
    # 翻負必須報成「排序翻轉」而非只報衰減（守衛與衰減分支的可辨識差異）
    assert "翻轉" in summary and "[A]" in summary


def test_sweep_decay_over_half_is_lone_peak():
    main = [_main_winner("A", 10.0)]
    sweep = [_sw(0.8, "A", 5.0, 4.0)]   # 4.0 < 0.5*10.0 → 衰減 >50%
    summary, ok = summarize_sweep(sweep, main, 1.0)
    assert ok is False


# ---------------------------------------------------------------------------
# verdict_preview §6.1：校準 gate（外部輸入）
# ---------------------------------------------------------------------------
def test_verdict_61_reflects_calib_pass():
    assert verdict_preview([_res()], calib_pass=True)["6.1"] == "PASS"
    assert verdict_preview([_res()], calib_pass=False)["6.1"] == "FAIL"
