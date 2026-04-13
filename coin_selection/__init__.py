"""
選幣輪動系統 (Coin Selection & Rotation)
=========================================

提供科學化的選幣機制，包含：
- CoinScore: 幣種評分資料結構
- CoinScorer: 多維度評分引擎
- CoinRanker: 即時排名系統
- CoinRotator: 智能輪動策略
- RotationTracker: 輪動歷史追蹤
- SymbolScanner: 動態交易對掃描與振幅篩選 (新)
- WebSocketDataProvider: 即時數據流 (CCXT Pro)
- HybridDataProvider: 混合數據提供者

完整選幣流程:
    from coin_selection import SymbolScanner, CoinScorer, CoinRanker

    # Step 1: 掃描全部交易對並振幅篩選
    scanner = SymbolScanner()
    candidates = await scanner.scan_with_amplitude(exchange, 'USDC', top_n=20)

    # Step 2: 對候選進行多維度評分
    scorer = CoinScorer()
    symbols = [sym.ccxt_symbol for sym, stats in candidates]
    scores = await scorer.score_all(symbols, exchange)

    # Step 3: 排名
    ranker = CoinRanker(scorer)
    rankings = await ranker.get_rankings(symbols, exchange)

振幅篩選 (參考 FMZ):
    from coin_selection import scan_grid_candidates, format_scan_report

    # 快速掃描適合網格的幣種
    results = await scan_grid_candidates(
        exchange,
        quote_currency='USDC',
        top_n=15,
        min_amplitude=3.0,    # 最低日均振幅 3%
        max_total_change=50.0  # 30日累計漲跌幅 < 50%
    )

    # 格式化報告
    print(format_scan_report(results))

WebSocket 使用 (可選，需 ccxt.pro):
    from coin_selection import WebSocketDataProvider, HybridDataProvider

    ws_provider = WebSocketDataProvider(exchange_pro)
    await ws_provider.start(symbols=["BTC/USDT", "ETH/USDT"])
    ticker = ws_provider.get_ticker("BTC/USDT")
"""

from .models import (
    CoinScore, CoinRank, RotationSignal, RotationConfig,
    RotationLog, ActionType, TrendType
)
from .scorer import CoinScorer, set_cache_ttl, clear_cache, get_cache_info
from .ranker import CoinRanker
from .rotator import CoinRotator, quick_rotation_check
from .tracker import RotationTracker, RotationHistoryDisplay
from .symbol_scanner import (
    SymbolScanner, SymbolInfo, AmplitudeStats,
    scan_grid_candidates, format_scan_report
)

# WebSocket 數據提供者 (可選，需要 ccxt.pro)
try:
    from .ws_provider import (
        WebSocketDataProvider,
        HybridDataProvider,
        TickerData,
        KlineData,
        create_ws_provider
    )
    _WS_AVAILABLE = True
except ImportError:
    _WS_AVAILABLE = False
    WebSocketDataProvider = None
    HybridDataProvider = None
    TickerData = None
    KlineData = None
    create_ws_provider = None

__all__ = [
    # 資料模型
    'CoinScore',
    'CoinRank',
    'RotationSignal',
    'RotationConfig',
    'RotationLog',
    'ActionType',
    'TrendType',
    # 核心引擎
    'CoinScorer',
    'CoinRanker',
    'CoinRotator',
    'RotationTracker',
    # 動態掃描
    'SymbolScanner',
    'SymbolInfo',
    'AmplitudeStats',
    'scan_grid_candidates',
    'format_scan_report',
    # 快速函數
    'quick_rotation_check',
    # 快取控制
    'set_cache_ttl',
    'clear_cache',
    'get_cache_info',
    # 顯示輔助
    'RotationHistoryDisplay',
    # WebSocket (可選)
    'WebSocketDataProvider',
    'HybridDataProvider',
    'TickerData',
    'KlineData',
    'create_ws_provider',
]

__version__ = '1.3.0'


def is_websocket_available() -> bool:
    """檢查 WebSocket 功能是否可用 (需要 ccxt.pro)"""
    return _WS_AVAILABLE
