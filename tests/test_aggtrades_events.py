import pandas as pd
from backtest.aggtrades import compress_events, estimate_spread


def test_compress_merges_same_price_runs():
    df = pd.DataFrame({"ts_ms": [1, 2, 3, 4, 5], "price": [100.0, 100.0, 100.1, 100.1, 100.0],
                       "qty": [1, 2, 3, 4, 5], "is_buyer_maker": [True] * 5})
    ev = compress_events(df)
    assert list(ev["price"]) == [100.0, 100.1, 100.0]
    assert list(ev["qty"]) == [3, 7, 5]          # 同價段 qty 加總
    assert list(ev["ts_ms"]) == [1, 3, 5]        # 段首 ts

def test_compress_preserves_single_events():
    df = pd.DataFrame({"ts_ms": [1], "price": [100.0], "qty": [1.0], "is_buyer_maker": [True]})
    assert len(compress_events(df)) == 1

def test_estimate_spread_from_side_flips():
    # bid=100.00 / ask=100.05 的交替成交 → spread 5bps
    df = pd.DataFrame({"ts_ms": [1, 100, 200, 300], "price": [100.00, 100.05, 100.00, 100.05],
                       "qty": [1] * 4, "is_buyer_maker": [True, False, True, False]})
    est = estimate_spread(df)
    assert est["n_pairs"] == 3
    assert abs(est["median_bps"] - 5.0) < 0.1

def test_estimate_spread_skips_stale_pairs():
    df = pd.DataFrame({"ts_ms": [1, 5_000], "price": [100.00, 100.05],
                       "qty": [1, 1], "is_buyer_maker": [True, False]})
    assert estimate_spread(df)["n_pairs"] == 0   # 相鄰對時差 >1s 不計
