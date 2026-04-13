"""
SymbolScanner - 動態交易對掃描與振幅篩選
==========================================

功能:
1. 從交易所獲取全部合約交易對
2. 按振幅、漲跌幅進行初步篩選
3. 過濾低流動性幣種
4. 輸出適合網格交易的候選列表

參考:
- FMZ 振幅篩選策略 (https://www.fmz.com/digest-topic/10585)
- U本位網格振幅篩選 (https://www.fmz.com/strategy/364968)

核心指標:
- 振幅 = (最高價 - 最低價) / 開盤價 × 100%
- 漲跌幅 = (收盤價 - 開盤價) / 開盤價 × 100%

篩選邏輯:
1. 振幅 > 閾值 (預設 3%)：確保有足夠波動
2. |累計漲跌幅| < 閾值 (預設 50%)：避免單邊趨勢
3. 24h 交易量 > 閾值：確保流動性
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from collections import defaultdict
import numpy as np

# 嘗試使用改進的錯誤處理
try:
    from core.error_handler import CCXTErrorHandler, retry_on_network_error
    from core.logging_setup import get_logger
    logger = get_logger("symbol_scanner")
    _error_handler = CCXTErrorHandler(logger)
    CORE_AVAILABLE = True
except ImportError:
    logger = logging.getLogger(__name__)
    _error_handler = None
    CORE_AVAILABLE = False
    # Fallback: 簡單重試裝飾器
    def retry_on_network_error(max_retries=3, base_delay=1.0, max_delay=60.0, exponential=True):
        def decorator(func):
            async def wrapper(*args, **kwargs):
                for attempt in range(max_retries + 1):
                    try:
                        return await func(*args, **kwargs)
                    except Exception as e:
                        if attempt >= max_retries:
                            raise
                        await asyncio.sleep(base_delay * (2 ** attempt) if exponential else base_delay)
            return wrapper
        return decorator


@dataclass
class SymbolInfo:
    """交易對信息"""
    symbol: str                    # 原始 symbol (如 BTCUSDT)
    ccxt_symbol: str              # CCXT 格式 (如 BTC/USDT:USDT)
    base: str                      # 基礎貨幣 (如 BTC)
    quote: str                     # 計價貨幣 (如 USDT)
    contract_type: str = "perpetual"  # 合約類型
    status: str = "trading"        # 交易狀態
    min_notional: float = 0.0      # 最小名義價值
    price_precision: int = 2       # 價格精度
    qty_precision: int = 3         # 數量精度


@dataclass
class AmplitudeStats:
    """振幅統計數據"""
    symbol: str
    avg_amplitude: float          # 平均振幅 (%)
    max_amplitude: float          # 最大振幅 (%)
    min_amplitude: float          # 最小振幅 (%)
    total_change: float           # 累計漲跌幅 (%)
    avg_daily_change: float       # 平均日漲跌幅 (%)
    volume_24h: float             # 24h 交易量 (USDT)
    days_analyzed: int            # 分析天數
    last_price: float = 0.0       # 最新價格
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def amplitude_score(self) -> float:
        """振幅評分 (0-100)"""
        # 最佳振幅區間: 3%-8%
        if 3 <= self.avg_amplitude <= 8:
            return 80 + (1 - abs(self.avg_amplitude - 5.5) / 2.5) * 20
        elif self.avg_amplitude > 8:
            return max(40, 80 - (self.avg_amplitude - 8) * 5)
        else:
            return max(0, self.avg_amplitude / 3 * 60)

    @property
    def trend_score(self) -> float:
        """趨勢評分 (0-100，低趨勢得高分)"""
        abs_change = abs(self.total_change)
        if abs_change < 10:
            return 100
        elif abs_change < 30:
            return 80 + (30 - abs_change) / 20 * 20
        elif abs_change < 50:
            return 60 + (50 - abs_change) / 20 * 20
        else:
            return max(0, 60 - (abs_change - 50) * 1.5)

    @property
    def grid_suitability(self) -> float:
        """網格適合度 (0-100)"""
        return self.amplitude_score * 0.6 + self.trend_score * 0.4


class SymbolScanner:
    """
    動態交易對掃描器

    掃描交易所所有合約交易對，篩選適合網格交易的幣種
    """

    # 預設參數
    DEFAULT_CONFIG = {
        'quote_currencies': ['USDT', 'USDC'],  # 支援的計價貨幣
        'min_amplitude': 3.0,       # 最低平均振幅 (%)
        'max_amplitude': 15.0,      # 最高平均振幅 (%)
        'max_total_change': 50.0,   # 最大累計漲跌幅 (%)
        'min_volume_24h': 10_000_000,  # 最低 24h 交易量 (USDT)
        'analysis_days': 30,        # 分析天數
        'exclude_symbols': [        # 排除的幣種
            'LUNA', 'UST', 'FTT',   # 問題幣種
            '1000', 'BIFI',          # 特殊格式
        ],
    }

    def __init__(self, config: Optional[Dict] = None):
        """
        初始化掃描器

        Args:
            config: 自定義配置
        """
        self.config = {**self.DEFAULT_CONFIG, **(config or {})}
        self._cache: Dict[str, AmplitudeStats] = {}
        self._cache_time: Optional[datetime] = None
        self._cache_ttl = timedelta(hours=4)  # 快取 4 小時

    async def scan_all_symbols(
        self,
        exchange: Any,
        quote_currency: str = 'USDC'
    ) -> List[SymbolInfo]:
        """
        掃描交易所所有合約交易對

        Args:
            exchange: CCXT 交易所實例
            quote_currency: 計價貨幣 (USDT/USDC)

        Returns:
            SymbolInfo 列表
        """
        symbols = []

        try:
            # 載入市場信息
            if not exchange.markets:
                await exchange.load_markets()

            for symbol, market in exchange.markets.items():
                # 只處理永續合約
                if not market.get('swap', False):
                    continue

                # 只處理指定計價貨幣
                quote = market.get('quote', '')
                settle = market.get('settle', '')
                if quote != quote_currency and settle != quote_currency:
                    continue

                # 只處理活躍交易對
                if not market.get('active', True):
                    continue

                # 提取基礎貨幣
                base = market.get('base', '')
                if not base:
                    continue

                # 排除問題幣種
                if any(ex in base.upper() for ex in self.config['exclude_symbols']):
                    continue

                # 構建 SymbolInfo
                info = SymbolInfo(
                    symbol=f"{base}{quote}",
                    ccxt_symbol=symbol,
                    base=base,
                    quote=quote,
                    contract_type='perpetual',
                    status='trading',
                    min_notional=market.get('limits', {}).get('cost', {}).get('min', 0),
                    price_precision=market.get('precision', {}).get('price', 8),
                    qty_precision=market.get('precision', {}).get('amount', 8)
                )
                symbols.append(info)

            logger.info(f"掃描到 {len(symbols)} 個 {quote_currency} 永續合約")
            return symbols

        except Exception as e:
            logger.error(f"掃描交易對失敗: {e}")
            return []

    async def calculate_amplitude(
        self,
        exchange: Any,
        symbol: str,
        days: int = 30
    ) -> Optional[AmplitudeStats]:
        """
        計算單個交易對的振幅統計

        Args:
            exchange: CCXT 交易所實例
            symbol: CCXT 格式交易對
            days: 分析天數

        Returns:
            AmplitudeStats 或 None
        """
        try:
            # 獲取日線 K 線
            ohlcv = await exchange.fetch_ohlcv(symbol, '1d', limit=days + 1)

            if not ohlcv or len(ohlcv) < 10:
                return None

            # 計算每日振幅和漲跌幅
            amplitudes = []
            changes = []

            for candle in ohlcv:
                timestamp, open_p, high, low, close, volume = candle

                if open_p <= 0:
                    continue

                # 振幅 = (最高 - 最低) / 開盤 × 100%
                amplitude = (high - low) / open_p * 100
                amplitudes.append(amplitude)

                # 漲跌幅 = (收盤 - 開盤) / 開盤 × 100%
                change = (close - open_p) / open_p * 100
                changes.append(change)

            if not amplitudes:
                return None

            # 獲取 24h 交易量
            try:
                ticker = await exchange.fetch_ticker(symbol)
                volume_24h = ticker.get('quoteVolume', 0) or 0
            except Exception:
                volume_24h = 0

            # 統計
            stats = AmplitudeStats(
                symbol=symbol,
                avg_amplitude=np.mean(amplitudes),
                max_amplitude=np.max(amplitudes),
                min_amplitude=np.min(amplitudes),
                total_change=sum(changes),
                avg_daily_change=np.mean(changes),
                volume_24h=volume_24h,
                days_analyzed=len(amplitudes),
                last_price=ohlcv[-1][4] if ohlcv else 0
            )

            return stats

        except Exception as e:
            # 使用改進的錯誤處理
            if _error_handler:
                error_info = _error_handler.handle_error(e, f"計算振幅:{symbol}")
                if error_info.severity.value in ("low", "medium"):
                    return None  # 可忽略的錯誤
            else:
                logger.debug(f"計算 {symbol} 振幅失敗: {e}")
            return None

    async def scan_with_amplitude(
        self,
        exchange: Any,
        quote_currency: str = 'USDC',
        top_n: int = 20,
        use_cache: bool = True
    ) -> List[Tuple[SymbolInfo, AmplitudeStats]]:
        """
        掃描並計算所有交易對的振幅，返回排名前 N 的候選

        Args:
            exchange: CCXT 交易所實例
            quote_currency: 計價貨幣
            top_n: 返回前 N 個候選
            use_cache: 是否使用快取

        Returns:
            [(SymbolInfo, AmplitudeStats), ...] 按網格適合度排序
        """
        # 檢查快取
        if use_cache and self._cache_time:
            if datetime.now() - self._cache_time < self._cache_ttl:
                cached_results = [
                    (info, self._cache[info.ccxt_symbol])
                    for info in await self.scan_all_symbols(exchange, quote_currency)
                    if info.ccxt_symbol in self._cache
                ]
                if cached_results:
                    logger.info("使用快取的振幅數據")
                    return sorted(
                        cached_results,
                        key=lambda x: x[1].grid_suitability,
                        reverse=True
                    )[:top_n]

        # 掃描所有交易對
        symbols = await self.scan_all_symbols(exchange, quote_currency)
        logger.info(f"開始計算 {len(symbols)} 個交易對的振幅...")

        # 批量計算振幅 (限制並發，增大批次以加速)
        results = []
        batch_size = 15  # 增大批次大小
        total = len(symbols)

        for i in range(0, total, batch_size):
            batch = symbols[i:i + batch_size]
            tasks = [
                self.calculate_amplitude(
                    exchange,
                    sym.ccxt_symbol,
                    self.config['analysis_days']
                )
                for sym in batch
            ]

            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            for sym, stats in zip(batch, batch_results):
                if isinstance(stats, AmplitudeStats):
                    results.append((sym, stats))
                    self._cache[sym.ccxt_symbol] = stats

            # 進度
            progress = min(i + batch_size, total)
            logger.info(f"振幅計算進度: {progress}/{total} ({progress * 100 // total}%)")

            # 短暫延遲避免 rate limit (減少等待時間)
            await asyncio.sleep(0.2)

        self._cache_time = datetime.now()

        # 篩選
        filtered = self._filter_candidates(results)
        logger.info(f"篩選後剩餘 {len(filtered)} 個候選")

        # 按網格適合度排序
        filtered.sort(key=lambda x: x[1].grid_suitability, reverse=True)

        return filtered[:top_n]

    def _filter_candidates(
        self,
        candidates: List[Tuple[SymbolInfo, AmplitudeStats]]
    ) -> List[Tuple[SymbolInfo, AmplitudeStats]]:
        """
        根據配置條件篩選候選

        篩選條件:
        1. 平均振幅在 [min_amplitude, max_amplitude] 範圍內
        2. 累計漲跌幅絕對值 < max_total_change
        3. 24h 交易量 > min_volume_24h
        """
        filtered = []

        min_amp = self.config['min_amplitude']
        max_amp = self.config['max_amplitude']
        max_change = self.config['max_total_change']
        min_vol = self.config['min_volume_24h']

        for sym, stats in candidates:
            # 振幅過濾
            if stats.avg_amplitude < min_amp:
                continue
            if stats.avg_amplitude > max_amp:
                continue

            # 趨勢過濾
            if abs(stats.total_change) > max_change:
                continue

            # 流動性過濾
            if stats.volume_24h < min_vol:
                continue

            filtered.append((sym, stats))

        return filtered

    def get_quick_candidates(
        self,
        exchange: Any,
        quote_currency: str = 'USDC',
        limit: int = 10
    ) -> List[str]:
        """
        快速獲取候選列表 (從快取)

        Returns:
            CCXT 格式交易對列表
        """
        if not self._cache:
            return []

        # 過濾快取中的數據
        cached_items = [
            (sym, stats) for sym, stats in self._cache.items()
            if quote_currency in sym
        ]

        # 篩選並排序
        filtered = []
        for sym, stats in cached_items:
            if stats.avg_amplitude >= self.config['min_amplitude']:
                if abs(stats.total_change) <= self.config['max_total_change']:
                    if stats.volume_24h >= self.config['min_volume_24h']:
                        filtered.append((sym, stats))

        filtered.sort(key=lambda x: x[1].grid_suitability, reverse=True)
        return [sym for sym, _ in filtered[:limit]]


# ========== 便捷函數 ==========

async def scan_grid_candidates(
    exchange: Any,
    quote_currency: str = 'USDC',
    top_n: int = 15,
    min_amplitude: float = 3.0,
    max_total_change: float = 50.0,
    min_volume: float = 10_000_000
) -> List[Tuple[SymbolInfo, AmplitudeStats]]:
    """
    掃描適合網格交易的幣種候選

    Args:
        exchange: CCXT 交易所實例
        quote_currency: 計價貨幣
        top_n: 返回前 N 個
        min_amplitude: 最低平均振幅 (%)
        max_total_change: 最大累計漲跌幅 (%)
        min_volume: 最低 24h 交易量 (USDT)

    Returns:
        [(SymbolInfo, AmplitudeStats), ...]

    Example:
        import ccxt.async_support as ccxt

        exchange = ccxt.bitget({'options': {'defaultType': 'swap'}})
        candidates = await scan_grid_candidates(exchange, 'USDC', top_n=10)

        for sym, stats in candidates:
            print(f"{sym.symbol}: 振幅 {stats.avg_amplitude:.1f}%, "
                  f"趨勢 {stats.total_change:+.1f}%, "
                  f"適合度 {stats.grid_suitability:.0f}")
    """
    scanner = SymbolScanner({
        'min_amplitude': min_amplitude,
        'max_total_change': max_total_change,
        'min_volume_24h': min_volume
    })

    return await scanner.scan_with_amplitude(
        exchange,
        quote_currency,
        top_n
    )


def format_scan_report(
    results: List[Tuple[SymbolInfo, AmplitudeStats]]
) -> str:
    """格式化掃描報告"""
    if not results:
        return "無符合條件的交易對"

    lines = [
        "=" * 70,
        "網格交易候選幣種掃描報告",
        "=" * 70,
        f"{'排名':<4} {'幣種':<12} {'振幅':<8} {'趨勢':<10} {'交易量':<12} {'適合度':<6}",
        "-" * 70
    ]

    for i, (sym, stats) in enumerate(results, 1):
        vol_str = f"${stats.volume_24h/1e6:.1f}M"
        lines.append(
            f"{i:<4} {sym.symbol:<12} "
            f"{stats.avg_amplitude:>5.1f}%  "
            f"{stats.total_change:>+7.1f}%  "
            f"{vol_str:<12} "
            f"{stats.grid_suitability:>5.0f}"
        )

    lines.append("=" * 70)
    lines.append(f"振幅: 日均波動幅度 | 趨勢: {results[0][1].days_analyzed}日累計漲跌幅")
    lines.append("適合度: 綜合評分 (0-100)，高分表示更適合網格交易")

    return "\n".join(lines)
