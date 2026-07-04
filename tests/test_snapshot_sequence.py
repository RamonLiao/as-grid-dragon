"""斷言 build_snapshot 的 manager 呼叫序列 == 現行 bot._get_dynamic_spacing（bot.py:450-515）。
用真 manager 實例 + call-recording，並比對搬移後回傳的 dynamic_tp/gs。
funding_manager.get_position_bias 在現行 bot.py 是在 _get_adjusted_quantity 呼叫，
不在 _get_dynamic_spacing 裡；本測試的 funding 呼叫是 Task 3 decision.py 既定設計
（EnhancementSnapshot 把 funding bias 併入單一快照），非對 _get_dynamic_spacing 逐字複刻範圍。
"""
import pytest
from grid_engine.snapshot import ManagerBundle, build_snapshot
from grid_engine.enhancements import (
    LeadingIndicatorManager, LeadingIndicatorConfig, DynamicGridManager,
    GLFTController, MaxEnhancement,
)


def _bundle(enh=None):
    return ManagerBundle(
        leading_indicator=LeadingIndicatorManager(LeadingIndicatorConfig(enabled=True)),
        dynamic_grid_manager=DynamicGridManager(),
        glft_controller=GLFTController(),
        funding_manager=None,
        max_enhancement=enh or MaxEnhancement(),
        leading_enabled=True,
    )


def test_snapshot_neutral_when_all_disabled():
    """manager 全中性（無數據）：dynamic == base，funding bias 1.0。"""
    snap = build_snapshot(_bundle(), "XRP/USDC:USDC", 0.004, 0.006)
    assert snap.dynamic_take_profit == pytest.approx(0.004)
    assert snap.dynamic_grid_spacing == pytest.approx(0.006)
    assert snap.funding_long_bias == 1.0 and snap.funding_short_bias == 1.0


def test_get_signals_call_count_recorded():
    """記錄 leading.get_signals 呼叫次數：enabled + 無 pause + 無 signals →
    直接 get_signals 1 次 + should_pause 內 1 次 = 現行序列。"""
    b = _bundle()
    calls = []
    orig = b.leading_indicator.get_signals
    b.leading_indicator.get_signals = lambda s: (calls.append(s) or orig(s))
    build_snapshot(b, "XRP/USDC:USDC", 0.004, 0.006)
    # 現行：get_signals(464) + should_pause→get_signals(1140)；無 signals 不進 get_spacing_adjustment
    assert len(calls) == 2


def test_pause_branch_doubles_base_and_skips_dynamic_spacing(monkeypatch):
    """should_pause_trading 回 (True, ...) → base_tp*2, base_gs*2，
    且 leading_reason != "" 且 != "正常" → 不呼叫 get_dynamic_spacing（維持 bot.py:487 條件）。"""
    b = _bundle()
    monkeypatch.setattr(
        b.leading_indicator, "should_pause_trading",
        lambda sym: (True, "極端波動")
    )

    dgm_calls = []
    orig_get_dynamic_spacing = b.dynamic_grid_manager.get_dynamic_spacing
    b.dynamic_grid_manager.get_dynamic_spacing = lambda *a, **kw: (
        dgm_calls.append((a, kw)) or orig_get_dynamic_spacing(*a, **kw)
    )

    snap = build_snapshot(b, "XRP/USDC:USDC", 0.004, 0.006)

    assert snap.dynamic_take_profit == pytest.approx(0.004 * 2)
    assert snap.dynamic_grid_spacing == pytest.approx(0.006 * 2)
    assert dgm_calls == []


def test_spacing_adjustment_branch_scales_tp_by_same_ratio(monkeypatch):
    """signals 非空 + get_spacing_adjustment 回 (base_gs*1.2, "放量") →
    gs 變 base*1.2，tp 乘同 ratio，且不呼叫 get_dynamic_spacing（leading_reason="放量" != "正常"）。"""
    b = _bundle()
    base_tp, base_gs = 0.004, 0.006

    monkeypatch.setattr(
        b.leading_indicator, "get_signals",
        lambda sym: (["VOLUME_SURGE"], {"ofi": 0.0, "volume_ratio": 3.0, "spread_ratio": 1.0})
    )
    monkeypatch.setattr(
        b.leading_indicator, "should_pause_trading",
        lambda sym: (False, "")
    )
    monkeypatch.setattr(
        b.leading_indicator, "get_spacing_adjustment",
        lambda sym, base_spacing: (base_spacing * 1.2, "放量")
    )

    dgm_calls = []
    orig_get_dynamic_spacing = b.dynamic_grid_manager.get_dynamic_spacing
    b.dynamic_grid_manager.get_dynamic_spacing = lambda *a, **kw: (
        dgm_calls.append((a, kw)) or orig_get_dynamic_spacing(*a, **kw)
    )

    snap = build_snapshot(b, "XRP/USDC:USDC", base_tp, base_gs)

    ratio = 1.2
    assert snap.dynamic_grid_spacing == pytest.approx(base_gs * ratio)
    assert snap.dynamic_take_profit == pytest.approx(base_tp * ratio)
    assert dgm_calls == []
