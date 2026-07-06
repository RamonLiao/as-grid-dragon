"""
AS 網格交易回測系統
==================

模組化回測系統，支援多交易對、參數優化

使用方式:
    from backtest_system import GridBacktester, DataLoader, Config

    # 載入數據
    loader = DataLoader()
    df = loader.load("XRPUSDC", "2025-10-27", "2025-11-25")

    # 配置參數
    config = Config(
        symbol="XRPUSDC",
        take_profit=0.004,
        grid_spacing=0.006
    )

    # 運行回測
    bt = GridBacktester(df, config)
    result = bt.run()

    # 智能優化 (使用 Optuna TPE)
    from backtest_system import SmartOptimizer
    optimizer = SmartOptimizer(df, config)
    result = optimizer.quick_optimize(n_trials=50, objective="sharpe")
"""

from .config import Config
from .data_loader import DataLoader
from .backtester import GridBacktester, BacktestResult
from .optimizer import GridOptimizer

# 智能優化器 (需要 optuna)
try:
    from .smart_optimizer import (
        SmartOptimizer,
        SmartOptimizationResult,
        OptimizationObjective,
        OptimizationMethod,
        smart_optimize_grid
    )
    SMART_OPTIMIZER_AVAILABLE = True
except ImportError:
    SmartOptimizer = None
    SmartOptimizationResult = None
    OptimizationObjective = None
    OptimizationMethod = None
    smart_optimize_grid = None
    SMART_OPTIMIZER_AVAILABLE = False

__all__ = [
    'Config',
    'DataLoader',
    'GridBacktester',
    'BacktestResult',
    'GridOptimizer',
    # 智能優化
    'SmartOptimizer',
    'SmartOptimizationResult',
    'OptimizationObjective',
    'OptimizationMethod',
    'smart_optimize_grid',
    'SMART_OPTIMIZER_AVAILABLE',
]
__version__ = '1.1.0'
