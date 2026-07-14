import pandas as pd, pytest
from backtest.aggtrades import AggTradesLoader

def _mk_day(first_ms, last_ms, n=10, monotonic=True):
    ts = list(range(first_ms, first_ms + n - 1)) + [last_ms]
    if not monotonic:
        ts[2], ts[3] = ts[3], ts[2]
    return pd.DataFrame({"agg_id": range(n), "price": [100.0 + i * 0.01 for i in range(n)],
                         "qty": [1.0] * n, "first_id": range(n), "last_id": range(n),
                         "ts_ms": ts, "is_buyer_maker": [i % 2 == 0 for i in range(n)]})

DAY0 = 1780704000000   # 2026-06-06 00:00:00 UTC（測試錨定 UTC 日界，非本地時區）

def test_validate_full_day_passes():
    df = _mk_day(DAY0 + 60_000, DAY0 + 86_395_000)   # 首筆 <00:05、末筆 >23:55
    AggTradesLoader().validate_day(df, "2026-06-06")

def test_validate_rejects_late_start():
    df = _mk_day(DAY0 + 400_000, DAY0 + 86_395_000)  # 首筆 00:06:40 → 缺頭
    with pytest.raises(ValueError, match="首筆"):
        AggTradesLoader().validate_day(df, "2026-06-06")

def test_validate_rejects_early_end():
    df = _mk_day(DAY0 + 60_000, DAY0 + 80_000_000)   # 末筆 22:13 → 缺尾（部分日毒快取的形態）
    with pytest.raises(ValueError, match="末筆"):
        AggTradesLoader().validate_day(df, "2026-06-06")

def test_validate_rejects_non_monotonic():
    df = _mk_day(DAY0 + 60_000, DAY0 + 86_395_000, monotonic=False)
    with pytest.raises(ValueError, match="單調"):
        AggTradesLoader().validate_day(df, "2026-06-06")

def test_validate_rejects_empty():
    with pytest.raises(ValueError, match="空"):
        AggTradesLoader().validate_day(pd.DataFrame(), "2026-06-06")

def test_download_skips_today_utc(tmp_path, monkeypatch):
    """未過完的當日不入快取（07-10 kline 部分日教訓）"""
    loader = AggTradesLoader(data_dir=str(tmp_path))
    called = []
    monkeypatch.setattr(loader, "_fetch_zip", lambda s, d: called.append(d) or b"")
    import datetime
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    loader.download("BNBUSDC", today, today)
    assert called == []
