import pandas as pd, pytest
from backtest.tick_sim import TickSimConfig, run_tick_sim


def _ev(*rows):   # rows: (ts_ms, price)
    return pd.DataFrame({"ts_ms": [r[0] for r in rows], "price": [r[1] for r in rows],
                         "qty": [1.0] * len(rows)})


BASE = dict(grid_spacing=0.003, take_profit_spacing=0.003, initial_quantity=0.02,
            leverage=5.0, initial_balance=1000.0, fee_pct=0.0, slippage_bps=0.0,
            cooldown_sec=0.0, decision_delay_ms=0)


def test_strict_crossing_fills_entry():
    # anchor 100 → buy entry @ 99.7；價格打到 99.69（嚴格低於）→ 成交
    cfg = TickSimConfig(**BASE, requote_threshold_factor=1.0)
    r = run_tick_sim(_ev((0, 100.0), (1000, 99.75), (2000, 99.69)), cfg)
    assert any(f["kind"] == "entry" and f["side"] == "long" for f in r.fills)


def test_touch_does_not_fill():
    # 恰好 99.7（== limit）→ 不成交（V2 保守界）
    cfg = TickSimConfig(**BASE, requote_threshold_factor=1.0)
    r = run_tick_sim(_ev((0, 100.0), (1000, 99.70)), cfg)
    assert r.fills == []


def test_chasing_requotes_before_fill_factor_half():
    """舊語意病理重現：0.15% 一到就重掛 → 緩跌路徑永不成交"""
    cfg = TickSimConfig(**BASE, requote_threshold_factor=0.5)
    # 每步跌 0.16%（觸發 requote）連續 10 步 → 掛單一路被搬走
    rows, p = [], 100.0
    for i in range(10):
        p *= (1 - 0.0016); rows.append((i * 1000, round(p, 6)))
    r = run_tick_sim(_ev((0, 100.0), *rows), cfg)
    assert [f for f in r.fills if f["kind"] == "entry"] == []
    assert r.requote_count >= 10


def test_resting_order_fills_same_path_factor_one():
    """同一路徑，factor=1.0 → 掛單活到被穿越（新語意的核心主張）"""
    cfg = TickSimConfig(**BASE, requote_threshold_factor=1.0)
    rows, p = [], 100.0
    for i in range(10):
        p *= (1 - 0.0016); rows.append((i * 1000, round(p, 6)))
    r = run_tick_sim(_ev((0, 100.0), *rows), cfg)
    assert any(f["kind"] == "entry" for f in r.fills)


def test_cooldown_caps_requote_rate():
    cfg = TickSimConfig(**{**BASE, "cooldown_sec": 5.0}, requote_threshold_factor=0.5)
    # 1 秒內三次 0.2% 跳動：cooldown 5s → 只允許第一次 requote
    r = run_tick_sim(_ev((0, 100.0), (200, 100.2), (400, 100.4), (600, 100.6)), cfg)
    assert r.requote_count <= 2      # 初始佈網 1 次 + 至多 1 次


def test_decision_delay_keeps_old_order_alive():
    """延遲窗口內舊單仍可成交（cancel 未落地）——lookahead 防禦的行為面"""
    cfg = TickSimConfig(**{**BASE, "decision_delay_ms": 500}, requote_threshold_factor=0.5)
    # t=1000 觸發 requote（0.16% 跌）；t=1200（延遲窗內）價格穿越舊 buy 單 99.7
    r = run_tick_sim(_ev((0, 100.0), (1000, 99.84), (1200, 99.69)), cfg)
    assert any(f["kind"] == "entry" and f["price"] == pytest.approx(99.7) for f in r.fills)


def test_margin_rejection_counted():
    # 開一層需 margin = 0.02*99.7/5 ≈ 0.399 → balance 0.1 必拒
    # （plan review SF-3：原 fixture 0.5 > 0.399 會開倉成功，fixture 退化）
    cfg = TickSimConfig(**{**BASE, "initial_balance": 0.1}, requote_threshold_factor=1.0)
    r = run_tick_sim(_ev((0, 100.0), (1000, 99.69)), cfg)
    assert r.rejected_entries >= 1 and r.fills == []


def test_liquidation_terminates():
    cfg = TickSimConfig(**{**BASE, "initial_balance": 21.0}, requote_threshold_factor=1.0,
                        seed_long_qty=1.0, seed_long_price=100.0)     # margin 20，權益薄
    # 價格崩 30% → 權益穿透維持保證金
    r = run_tick_sim(_ev((0, 100.0), (1000, 70.0)), cfg)
    assert r.liquidated is True


def test_round_trip_counting():
    cfg = TickSimConfig(**BASE, requote_threshold_factor=1.0)
    # 完整往返：進場 99.69 成交 → TP sell @ entry*1.003 → 價格上穿 → TP 成交
    r = run_tick_sim(_ev((0, 100.0), (1000, 99.69), (2000, 100.05)), cfg)
    assert r.round_trips == 1
