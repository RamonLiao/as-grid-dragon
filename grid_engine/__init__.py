"""
AS Grid Engine - MAX 版本
拆分自 as_terminal_max.py
"""

from .utils import SYMBOL_MAP, normalize_symbol, console, logger, CONFIG_DIR, CONFIG_FILE, DATA_DIR
from . import decision as _decision


class GridStrategy:
    """相容 shim：舊引用（as_terminal_max.py、grid_engine/backtest.py）轉呼叫 decision 純函數。

    strategy.py 已刪（#4 回測/實盤策略脫鉤計畫 Task 7）。純計算改走 grid_engine/decision.py。
    get_grid_decision 未遷移（grid_engine/backtest.py 待 Task 8 處理），呼叫即拋錯。
    """
    is_dead_mode = staticmethod(_decision.is_dead_mode)
    calculate_grid_prices = staticmethod(_decision.grid_prices)

    @staticmethod
    def calculate_dead_mode_price(base_price, my_position, opposite_position, side):
        return _decision.dead_mode_price(base_price, my_position, opposite_position, side)

    @staticmethod
    def get_grid_decision(*a, **k):
        raise NotImplementedError("改用 grid_engine.decision.decide()")


from .enhancements import (
    MaxEnhancement, BanditConfig, MarketContext, ParameterArm, UCBBanditOptimizer,
    DGTConfig, DGTBoundaryManager, FundingRateManager, GLFTController,
    DynamicGridManager, LeadingIndicatorConfig, LeadingIndicatorManager
)
from .config import SymbolConfig, RiskConfig, GlobalConfig
from .state import SymbolState, AccountBalance, GlobalState
from .backtest import BacktestManager
from .bot import CustomExchange, MaxGridBot
from .notifier import TelegramNotifier
from .ui import TerminalUI

# 選幣模組 (從 coin_selection 包導入)
try:
    from coin_selection import (
        CoinScorer, CoinRanker, SymbolScanner,
        scan_grid_candidates, format_scan_report,
        SymbolInfo, AmplitudeStats, CoinScore, CoinRank,
    )
    _COIN_SELECTION_AVAILABLE = True
except ImportError:
    _COIN_SELECTION_AVAILABLE = False

__all__ = [
    # utils
    'SYMBOL_MAP', 'normalize_symbol', 'console', 'logger',
    'CONFIG_DIR', 'CONFIG_FILE', 'DATA_DIR',
    # strategy
    'GridStrategy',
    # enhancements
    'MaxEnhancement', 'BanditConfig', 'MarketContext', 'ParameterArm',
    'UCBBanditOptimizer', 'DGTConfig', 'DGTBoundaryManager',
    'FundingRateManager', 'GLFTController', 'DynamicGridManager',
    'LeadingIndicatorConfig', 'LeadingIndicatorManager',
    # config
    'SymbolConfig', 'RiskConfig', 'GlobalConfig',
    # state
    'SymbolState', 'AccountBalance', 'GlobalState',
    # backtest
    'BacktestManager',
    # bot
    'CustomExchange', 'MaxGridBot',
    # notifier
    'TelegramNotifier',
    # ui
    'TerminalUI',
    # coin selection
    'CoinScorer', 'CoinRanker', 'SymbolScanner',
    'scan_grid_candidates', 'format_scan_report',
    'SymbolInfo', 'AmplitudeStats', 'CoinScore', 'CoinRank',
]
