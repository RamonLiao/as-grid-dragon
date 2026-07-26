"""回測服務層：SymbolConfig→Config 映射 + 回測/優化執行 + 結果歸一。

所有轉換集中在此（可脫離 Streamlit 單測），頁3 只做渲染。
兩種優化器（GridOptimizer / SmartOptimizer）結果歸一成同一張
DataFrame，頁面單一渲染路徑。
"""
from typing import Callable, Dict, List, Optional

import pandas as pd

from grid_engine.config import SymbolConfig
from backtest.config import Config
from backtest.backtester import GridBacktester, BacktestResult
from backtest.optimizer import GridOptimizer

try:
    from backtest.smart_optimizer import (
        SmartOptimizer, TradingMode, OptimizationObjective,
    )
    SMART_AVAILABLE = True
except ImportError:
    SMART_AVAILABLE = False


def to_backtest_config(sym: SymbolConfig, *,
                       initial_balance: float = 1000.0,
                       zero_costs: bool = False) -> Config:
    """SymbolConfig → backtest.Config。

    initial_quantity<=0 會讓 GridBacktester 落入 deprecated legacy 路徑
    （使用 position_threshold/limit 絕對值預設 500/100），直接拒絕。
    multiplier 必須帶入：backtester.run() 以
    initial_quantity×multiplier 計算閾值（backtester.py:541-542）。
    """
    if sym.initial_quantity <= 0:
        raise ValueError(
            f"initial_quantity 必須 > 0（{sym.symbol} 現值 "
            f"{sym.initial_quantity}），否則回測落入 legacy 絕對值路徑")
    cfg = Config(
        symbol=sym.symbol,
        initial_balance=initial_balance,
        initial_quantity=sym.initial_quantity,
        leverage=sym.assumed_leverage,
        take_profit_spacing=sym.take_profit_spacing,
        grid_spacing=sym.grid_spacing,
        limit_multiplier=sym.limit_multiplier,
        threshold_multiplier=sym.threshold_multiplier,
        position_threshold=0.0,   # 明確歸零：主路徑本就不讀，防 legacy 誤用
        position_limit=0.0,
    )
    if zero_costs:
        cfg.fee_pct = 0.0
        cfg.slippage_bps = 0.0
        cfg.funding_enabled = False
    return cfg


def _ensure_open_time(df: pd.DataFrame) -> pd.DataFrame:
    """GridBacktester 讀 `open_time` 欄（backtester.py:559,642）。

    與 DataLoader 的欄位相容邏輯一致（data_loader.py:140-143）：
    缺 open_time 但有 timestamp 時補一份，不改動呼叫端 df。
    """
    if "open_time" not in df.columns and "timestamp" in df.columns:
        df = df.copy()
        df["open_time"] = pd.to_datetime(df["timestamp"])
    return df


LIQUIDATION_WARNING = (
    "⚠️ 此參數組在回測期間觸發強平（spec §7 一票否決）：結果不可用作最佳解，"
    "final_equity 與 max_drawdown 皆不具參考價值。\n"
)


def backtest_result_to_view(result: BacktestResult) -> dict:
    """BacktestResult → 頁面渲染用 dict。

    liquidated=True 時在 notes 最前面插入一票否決警告（LIQUIDATION_WARNING）。
    notes 是前端已經在渲染的欄位，這個警告不需要改任何 UI 程式碼就會出現在畫面上：
    不變式由持有它的模組（本函式）保證，不外包給消費端（Streamlit 頁面）自行判讀
    liquidated flag。liquidated 本身也一併暴露在 view dict 裡供未來 UI 使用，但
    即使沒有任何 UI 改動，警告文字已經生效。
    """
    notes = result.notes
    if result.liquidated:
        notes = LIQUIDATION_WARNING + notes
    return {
        "return_pct": result.return_pct,
        "max_drawdown": result.max_drawdown,
        "realized_pnl": result.realized_pnl,
        "unrealized_pnl": result.unrealized_pnl,
        "total_pnl": result.total_pnl,
        "trades_count": result.trades_count,
        "win_rate": result.win_rate,
        "profit_factor": result.profit_factor,
        "sharpe_ratio": result.sharpe_ratio,
        "final_equity": result.final_equity,
        "trade_history": result.trade_history,
        "equity_curve": result.equity_curve,
        "notes": notes,
        "liquidated": result.liquidated,
        "peak_margin_usage": result.peak_margin_usage,
    }


def run_single_backtest(sym: SymbolConfig, df: pd.DataFrame) -> dict:
    cfg = to_backtest_config(sym)
    result = GridBacktester(_ensure_open_time(df), cfg).run()
    return backtest_result_to_view(result)


def run_grid_optimization(
        sym: SymbolConfig, df: pd.DataFrame,
        param_ranges: Optional[Dict[str, List]] = None,
        progress_callback: Optional[Callable[[int, int], None]] = None,
) -> pd.DataFrame:
    """網格搜尋。回傳 all_results DataFrame（含各參數欄 + 指標欄）。"""
    base = to_backtest_config(sym)
    optimizer = GridOptimizer(_ensure_open_time(df), base_config=base,
                              param_ranges=param_ranges)
    result = optimizer.run(progress_callback=progress_callback)
    return result.all_results


def run_smart_optimization(
        sym: SymbolConfig, df: pd.DataFrame, *,
        n_trials: int = 100, objective: str = "sharpe",
        trading_mode: Optional[str] = None,
        progress_callback: Optional[Callable] = None,
) -> pd.DataFrame:
    """Optuna TPE。結果歸一成 DataFrame：每 trial 一列，
    參數欄 + objective_value 欄，與網格搜尋同構供頁面單一渲染。"""
    if not SMART_AVAILABLE:
        raise RuntimeError("Optuna 未安裝（uv add optuna）")
    base = to_backtest_config(sym)
    mode = TradingMode(trading_mode) if trading_mode else None
    objective_map = {
        "return": OptimizationObjective.RETURN,
        "sharpe": OptimizationObjective.SHARPE,
        "sortino": OptimizationObjective.SORTINO,
        "calmar": OptimizationObjective.CALMAR,
        "profit_factor": OptimizationObjective.PROFIT_FACTOR,
        "risk_adjusted": OptimizationObjective.RISK_ADJUSTED,
    }
    optimizer = SmartOptimizer(_ensure_open_time(df), base_config=base,
                               trading_mode=mode)
    smart = optimizer.optimize(
        n_trials=n_trials,
        objective=objective_map.get(objective, OptimizationObjective.SHARPE),
        progress_callback=progress_callback,
        show_progress=False,
    )
    # all_trials 元素為 TrialResult dataclass（smart_optimizer.py:133）：
    # .params / .objective_value，無 dict fallback 需求。
    rows = []
    for t in smart.all_trials:
        row = dict(t.params)
        row["objective_value"] = t.objective_value
        rows.append(row)
    out = pd.DataFrame(rows)
    out.attrs["best_params"] = smart.best_params
    out.attrs["best_metrics"] = smart.best_metrics
    out.attrs["param_importance"] = smart.param_importance
    return out
