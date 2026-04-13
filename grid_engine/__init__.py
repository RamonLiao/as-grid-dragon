"""
AS Grid Engine - MAX 版本
拆分自 as_terminal_max.py
"""

from .utils import SYMBOL_MAP, normalize_symbol, console, logger, CONFIG_DIR, CONFIG_FILE, DATA_DIR
from .strategy import GridStrategy
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
