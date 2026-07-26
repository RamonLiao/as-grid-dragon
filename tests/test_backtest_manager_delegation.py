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
        # 7 是刻意選的非預設值（SymbolConfig.assumed_leverage 預設 20）：
        # 若 run_backtest/optimize_params 映射被寫死或落回預設，
        # test_run_backtest_maps_assumed_leverage_to_backtest_config /
        # test_optimize_params_relay_config_preserves_assumed_leverage 會紅。
        assumed_leverage=7,
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


def test_run_backtest_maps_assumed_leverage_to_backtest_config(monkeypatch):
    """守衛 grid_engine/backtest.py:146 的
    BacktestConfig(leverage=config.assumed_leverage, ...) 映射。

    cfg.assumed_leverage=7（非 SymbolConfig 預設值 20，見 _make_config），
    攔截實際傳入 GridBacktester 的 BacktestConfig，斷言其 leverage 真的
    等於 7；若映射被寫死（例如 leverage=20）本測試會紅。
    """
    import backtest.backtester as backtester_mod

    captured = {}
    real_init = backtester_mod.GridBacktester.__init__

    def fake_init(self, df, config, funding_map=None):
        captured["leverage"] = config.leverage
        real_init(self, df, config, funding_map=funding_map)

    monkeypatch.setattr(backtester_mod.GridBacktester, "__init__", fake_init)

    mgr = BacktestManager()
    cfg = _make_config(assumed_leverage=7)
    df = _make_df()

    result = mgr.run_backtest(cfg, df)

    assert isinstance(result, dict)
    assert captured.get("leverage") == 7


def test_optimize_params_relay_config_preserves_assumed_leverage(monkeypatch):
    """守衛 optimize_params 內建中繼 SymbolConfig（grid_engine/backtest.py:174）：
    assumed_leverage=config.assumed_leverage，不得被寫死成常數。

    攔截 BacktestManager.run_backtest 收到的中繼 config，斷言
    assumed_leverage 真的等於傳入 config 的 7（非 SymbolConfig 預設值 20）。
    """
    mgr = BacktestManager()
    cfg = _make_config(assumed_leverage=7)
    df = _make_df(n=30)  # 小 df 加速；只需驗證關聯欄位透傳

    captured_leverages = []
    real_run_backtest = BacktestManager.run_backtest

    def fake_run_backtest(self, test_config, df):
        captured_leverages.append(test_config.assumed_leverage)
        result = real_run_backtest(self, test_config, df)
        return result

    monkeypatch.setattr(BacktestManager, "run_backtest", fake_run_backtest)

    mgr.optimize_params(cfg, df)

    assert len(captured_leverages) > 0
    assert all(lv == 7 for lv in captured_leverages)
