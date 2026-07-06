"""backtest_service 黃金測試。

為什麼重要：SymbolConfig→backtest.Config 映射錯了不會炸，
只會默默給錯回測結論（量化系統最貴的一類 bug）。
已知輸入→已知輸出鎖死每個欄位。
"""
import pandas as pd
import numpy as np
import pytest

from web.services import backtest_service
from grid_engine.config import SymbolConfig


SYM = SymbolConfig(
    symbol="XRPUSDC", ccxt_symbol="XRP/USDC:USDC", enabled=True,
    take_profit_spacing=0.004, grid_spacing=0.006,
    initial_quantity=3.0, leverage=20,
    limit_multiplier=5.0, threshold_multiplier=20.0,
)


def test_to_backtest_config_golden():
    cfg = backtest_service.to_backtest_config(SYM)
    assert cfg.symbol == "XRPUSDC"
    assert cfg.initial_quantity == 3.0          # 預設 0.0=空回測，必須帶入
    assert cfg.leverage == 20
    assert cfg.take_profit_spacing == 0.004     # 兩邊皆小數比例，1:1
    assert cfg.grid_spacing == 0.006
    assert cfg.limit_multiplier == 5.0          # 不帶 → backtester 用預設 5/14
    assert cfg.threshold_multiplier == 20.0
    assert cfg.initial_balance == 1000.0
    # 成本模型：單次回測用引擎預設（保真）
    assert cfg.fee_pct == 0.0004
    assert cfg.funding_enabled is True


def test_to_backtest_config_rejects_zero_quantity():
    """initial_quantity<=0 會落入 legacy 絕對值路徑（500/100 預設）→ 直接拒絕。"""
    bad = SymbolConfig(symbol="X", ccxt_symbol="X/USDC:USDC", initial_quantity=0)
    with pytest.raises(ValueError):
        backtest_service.to_backtest_config(bad)


def test_to_backtest_config_zero_costs():
    """新舊引擎對比模式：成本全歸零。"""
    cfg = backtest_service.to_backtest_config(SYM, zero_costs=True)
    assert cfg.fee_pct == 0.0
    assert cfg.slippage_bps == 0.0
    assert cfg.funding_enabled is False


def _make_df(n=300, price=1.0):
    """合成 1m K 線：正弦波動保證網格有成交。"""
    ts = pd.date_range("2026-01-01", periods=n, freq="1min")
    wave = price * (1 + 0.02 * np.sin(np.arange(n) / 20))
    return pd.DataFrame({
        "timestamp": ts, "open": wave, "high": wave * 1.001,
        "low": wave * 0.999, "close": wave, "volume": 100.0,
    })


def test_run_single_backtest_returns_view_dict():
    view = backtest_service.run_single_backtest(SYM, _make_df())
    for key in ("return_pct", "max_drawdown", "total_pnl", "trades_count",
                "win_rate", "profit_factor", "sharpe_ratio", "final_equity",
                "trade_history", "equity_curve"):
        assert key in view, f"view 缺 {key}"
    assert isinstance(view["trades_count"], int)


def test_backtest_result_to_view_full_keyset():
    """view dict 是頁面渲染契約：13 個 key 一個不能少。"""
    view = backtest_service.run_single_backtest(SYM, _make_df())
    assert set(view.keys()) == {
        "return_pct", "max_drawdown", "realized_pnl", "unrealized_pnl",
        "total_pnl", "trades_count", "win_rate", "profit_factor",
        "sharpe_ratio", "final_equity", "trade_history", "equity_curve",
        "notes",
    }


def test_grid_optimization_returns_dataframe():
    param_ranges = {"take_profit_spacing": [0.003, 0.004],
                    "grid_spacing": [0.005, 0.006]}
    df = backtest_service.run_grid_optimization(
        SYM, _make_df(), param_ranges=param_ranges)
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 4  # 2x2 組合
    assert "take_profit_spacing" in df.columns
    assert "return_pct" in df.columns


def test_smart_optimization_returns_dataframe():
    pytest.importorskip("optuna")
    df = backtest_service.run_smart_optimization(
        SYM, _make_df(), n_trials=3, objective="sharpe", trading_mode="swing")
    assert isinstance(df, pd.DataFrame)
    assert len(df) >= 1
    assert "objective_value" in df.columns
