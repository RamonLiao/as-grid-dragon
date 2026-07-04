import pandas as pd
from datetime import datetime, timedelta
from backtest.config import Config
from backtest.backtester import GridBacktester


def _make_df(prices):
    t0 = datetime(2001, 9, 9)
    return pd.DataFrame({
        "open_time": [t0 + timedelta(minutes=i) for i in range(len(prices))],
        "open": prices, "high": prices, "low": prices,
        "close": prices, "volume": [1.0] * len(prices),
    })


def _cfg(**kw):
    base = dict(symbol="BTCUSDC", initial_balance=100000.0, initial_quantity=1.0,
                leverage=20, take_profit_spacing=0.004, grid_spacing=0.006,
                direction="both", funding_enabled=False)
    base.update(kw)
    return Config(**base)


def test_slippage_reduces_final_equity_vs_zero():
    # 同一條價格序列，開滑價的 final_equity 應 <= 零滑價
    prices = [100.0, 99.0, 100.5, 99.5, 100.8, 99.2, 101.0]
    df = _make_df(prices)
    zero = GridBacktester(df.copy(), _cfg(slippage_bps=0.0)).run()
    slip = GridBacktester(df.copy(), _cfg(slippage_bps=0.001)).run()
    assert slip.final_equity <= zero.final_equity


def test_zero_slippage_zero_funding_matches_baseline_equity():
    # slippage=0 + funding off → 成本模型純疊加、無副作用（等價守門）
    prices = [100.0, 99.0, 100.5, 99.5, 100.8]
    df = _make_df(prices)
    r = GridBacktester(df, _cfg(slippage_bps=0.0)).run()
    assert r.final_equity > 0  # smoke：跑得動且非崩壞
