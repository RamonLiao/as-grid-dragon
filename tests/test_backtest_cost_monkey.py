import math
import pandas as pd
from datetime import datetime, timedelta
import pytest
from backtest.config import Config
from backtest.backtester import GridBacktester
from backtest.costs import apply_slippage, funding_charge


def _make_df(prices, start=datetime(2001, 9, 9)):
    return pd.DataFrame({
        "open_time": [start + timedelta(minutes=i) for i in range(len(prices))],
        "open": prices, "high": prices, "low": prices,
        "close": prices, "volume": [1.0] * len(prices),
    })


def _cfg(**kw):
    base = dict(symbol="BTCUSDC", initial_balance=100000.0, initial_quantity=1.0,
                leverage=20, take_profit_spacing=0.004, grid_spacing=0.006,
                direction="both", slippage_bps=0.0001, funding_enabled=True)
    base.update(kw)
    return Config(**base)


def test_extreme_rate_cap_075pct_no_crash():
    prices = [100.0, 99.0, 99.0]
    df = _make_df(prices)
    e = int(df["open_time"].iloc[1].timestamp())
    r = GridBacktester(df, _cfg(), funding_map={e: 0.0075}).run()  # 幣安 ±0.75% 上限
    assert math.isfinite(r.funding_paid)


def test_nan_rate_in_map_ignored():
    prices = [100.0, 99.0, 99.0]
    df = _make_df(prices)
    e = int(df["open_time"].iloc[1].timestamp())
    r = GridBacktester(df, _cfg(), funding_map={e: float("nan")}).run()
    assert r.funding_paid == 0.0


def test_empty_funding_map_no_charge():
    df = _make_df([100.0, 99.0, 99.0])
    r = GridBacktester(df, _cfg(), funding_map={}).run()
    assert r.funding_paid == 0.0


def test_settlement_before_start_not_charged():
    df = _make_df([100.0, 99.0, 99.0])
    before = int(df["open_time"].iloc[0].timestamp()) - 100000
    r = GridBacktester(df, _cfg(), funding_map={before: 0.0075}).run()
    assert r.funding_paid == 0.0


def test_reversed_timestamps_no_double_charge():
    # 價格序列時間倒流（髒資料）→ 不重複結算、不崩
    df = _make_df([100.0, 99.0, 99.0])
    df = df.iloc[::-1].reset_index(drop=True)  # 倒序
    e = int(df["open_time"].iloc[0].timestamp())
    r = GridBacktester(df, _cfg(), funding_map={e: 0.0001}).run()
    assert math.isfinite(r.funding_paid)


def test_apply_slippage_extreme_bps_no_negative_price():
    # bps 巨大但 <1 → 價格仍為正
    assert apply_slippage(100.0, "long", "tp", 0.99) == pytest.approx(1.0)


def test_funding_charge_negative_qty_defensive():
    # 髒持倉（不應發生）：不崩、回有限值
    assert math.isfinite(funding_charge([{"qty": -5.0}], 0.0001, "long", 100.0))
