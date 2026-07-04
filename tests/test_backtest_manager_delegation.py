"""
Task 8b: grid_engine/backtest.py::BacktestManager 委派給 backtest/backtester.py::GridBacktester。

修 Task 7 引入的 regression: run_backtest 原本走 GridStrategy.get_grid_decision（已刪的
strategy.py shim），實際執行會拋 NotImplementedError。改委派後應直接復用已遷移到
decide() 的 GridBacktester，兩套引擎共用同一決策路徑。
"""
import inspect

import numpy as np
import pandas as pd
import pytest

from grid_engine.backtest import BacktestManager
from grid_engine.config import SymbolConfig


def _make_df(n=300, start=100.0, seed=42):
    rng = np.random.default_rng(seed)
    prices = start + np.cumsum(rng.normal(0, 0.3, n))
    prices = np.clip(prices, 1.0, None)
    return pd.DataFrame({
        "open_time": pd.date_range("2026-01-01", periods=n, freq="1min"),
        "open": prices,
        "high": prices * 1.001,
        "low": prices * 0.999,
        "close": prices,
        "volume": rng.uniform(1, 10, n),
    })


def _make_config(**overrides):
    kwargs = dict(
        symbol="XRPUSDC",
        ccxt_symbol="XRP/USDC:USDC",
        take_profit_spacing=0.004,
        grid_spacing=0.006,
        initial_quantity=3,
        leverage=20,
    )
    kwargs.update(overrides)
    return SymbolConfig(**kwargs)


REQUIRED_KEYS = {
    "final_equity", "return_pct", "max_drawdown", "realized_pnl",
    "unrealized_pnl", "trades_count", "win_rate", "profit_factor",
}


def test_run_backtest_no_longer_raises():
    """Task 7 regression: run_backtest 曾因 GridStrategy shim 拋 NotImplementedError。"""
    mgr = BacktestManager()
    cfg = _make_config()
    df = _make_df()

    result = mgr.run_backtest(cfg, df)

    assert isinstance(result, dict)
    assert REQUIRED_KEYS.issubset(result.keys())


def test_run_backtest_routes_through_griddecide():
    """委派後 run_backtest 原始碼應含 GridBacktester、不再有已刪的 get_grid_decision 手寫決策迴圈。"""
    source = inspect.getsource(BacktestManager.run_backtest)
    assert "GridBacktester" in source
    assert "get_grid_decision" not in source


def test_optimize_params_returns_sorted_with_params():
    mgr = BacktestManager()
    cfg = _make_config()
    df = _make_df()

    results = mgr.optimize_params(cfg, df)

    assert isinstance(results, list)
    assert len(results) > 0
    for r in results:
        assert "take_profit_spacing" in r
        assert "grid_spacing" in r
        assert REQUIRED_KEYS.issubset(r.keys())

    return_pcts = [r["return_pct"] for r in results]
    assert return_pcts == sorted(return_pcts, reverse=True)


def test_optimize_params_progress_callback_invoked():
    mgr = BacktestManager()
    cfg = _make_config()
    df = _make_df(n=60)

    calls = []
    mgr.optimize_params(cfg, df, progress_callback=lambda i, total: calls.append((i, total)))

    assert len(calls) > 0
    assert calls[-1][0] == calls[-1][1]
