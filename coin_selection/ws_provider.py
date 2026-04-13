"""
WebSocket 數據提供者
====================

使用 CCXT Pro WebSocket API 提供即時數據流，
替代傳統 REST API 輪詢，實現更快更穩定的數據獲取。

功能:
- 即時 Ticker 數據流 (watch_tickers)
- 即時 K 線數據流 (watch_ohlcv)
- 自動重連機制
- 數據快取整合

使用方式:
    provider = WebSocketDataProvider(exchange)
    await provider.start(symbols=["BTC/USDT", "ETH/USDT"])

    # 獲取最新 ticker
    ticker = provider.get_ticker("BTC/USDT")

    # 獲取最新 K 線
    klines = provider.get_klines("BTC/USDT", "1h")

    # 停止
    await provider.stop()
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Callable, Set
from dataclasses import dataclass, field
from collections import defaultdict

# 嘗試使用改進的日誌和錯誤處理
try:
    from core.logging_setup import get_logger
    from core.error_handler import CCXTErrorHandler, ErrorSeverity
    from core.constants import Constants
    logger = get_logger("ws_provider")
    _error_handler = CCXTErrorHandler(logger)
    # 使用統一常量
    DEFAULT_RECONNECT_DELAY = Constants.WS_RECONNECT_DELAY
    DEFAULT_MAX_RECONNECT = Constants.WS_MAX_RECONNECT_ATTEMPTS
    DEFAULT_PING_INTERVAL = Constants.WS_PING_INTERVAL
    CORE_AVAILABLE = True
except ImportError:
    logger = logging.getLogger(__name__)
    _error_handler = None
    DEFAULT_RECONNECT_DELAY = 5
    DEFAULT_MAX_RECONNECT = 10
    DEFAULT_PING_INTERVAL = 20
    CORE_AVAILABLE = False


@dataclass
class TickerData:
    """Ticker 數據結構"""
    symbol: str
    last: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    high: float = 0.0
    low: float = 0.0
    volume: float = 0.0           # 基礎貨幣成交量
    quote_volume: float = 0.0     # 計價貨幣成交量 (USDT)
    change_24h: float = 0.0       # 24h 漲跌幅
    timestamp: datetime = field(default_factory=datetime.now)

    @classmethod
    def from_ccxt(cls, symbol: str, data: dict) -> 'TickerData':
        """從 CCXT ticker 格式轉換"""
        return cls(
            symbol=symbol,
            last=data.get('last', 0) or 0,
            bid=data.get('bid', 0) or 0,
            ask=data.get('ask', 0) or 0,
            high=data.get('high', 0) or 0,
            low=data.get('low', 0) or 0,
            volume=data.get('baseVolume', 0) or 0,
            quote_volume=data.get('quoteVolume', 0) or 0,
            change_24h=data.get('percentage', 0) or 0,
            timestamp=datetime.now()
        )


@dataclass
class KlineData:
    """K 線數據結構"""
    symbol: str
    timeframe: str
    timestamp: int          # 毫秒時間戳
    open: float
    high: float
    low: float
    close: float
    volume: float

    @classmethod
    def from_ccxt(cls, symbol: str, timeframe: str, ohlcv: list) -> 'KlineData':
        """從 CCXT OHLCV 格式轉換"""
        return cls(
            symbol=symbol,
            timeframe=timeframe,
            timestamp=ohlcv[0],
            open=ohlcv[1],
            high=ohlcv[2],
            low=ohlcv[3],
            close=ohlcv[4],
            volume=ohlcv[5]
        )


class WebSocketDataProvider:
    """
    WebSocket 數據提供者

    使用 CCXT Pro 的 WebSocket API 提供即時數據流
    """

    def __init__(
        self,
        exchange: Any,
        on_ticker_update: Optional[Callable[[str, TickerData], None]] = None,
        on_kline_update: Optional[Callable[[str, str, KlineData], None]] = None,
        reconnect_delay: float = DEFAULT_RECONNECT_DELAY,
        max_reconnect_attempts: int = DEFAULT_MAX_RECONNECT,
        ping_interval: float = DEFAULT_PING_INTERVAL
    ):
        """
        初始化 WebSocket 數據提供者

        Args:
            exchange: CCXT Pro 交易所實例 (必須支持 WebSocket)
            on_ticker_update: Ticker 更新回調
            on_kline_update: K 線更新回調
            reconnect_delay: 重連延遲 (秒)
            max_reconnect_attempts: 最大重連次數
        """
        self.exchange = exchange
        self.on_ticker_update = on_ticker_update
        self.on_kline_update = on_kline_update
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_attempts = max_reconnect_attempts
        self.ping_interval = ping_interval

        # 數據快取
        self._tickers: Dict[str, TickerData] = {}
        self._klines: Dict[str, Dict[str, List[KlineData]]] = defaultdict(lambda: defaultdict(list))

        # 訂閱狀態
        self._subscribed_symbols: Set[str] = set()
        self._subscribed_klines: Dict[str, Set[str]] = defaultdict(set)  # {symbol: {timeframes}}

        # 運行狀態
        self._running = False
        self._tasks: List[asyncio.Task] = []
        self._reconnect_count = 0
        self._healthy = True  # 健康狀態

        # 統計
        self._ticker_updates = 0
        self._kline_updates = 0
        self._last_update: Optional[datetime] = None
        self._connection_errors = 0  # 連接錯誤計數

    # ========== 啟動/停止 ==========

    async def start(
        self,
        symbols: List[str],
        kline_timeframes: Optional[Dict[str, List[str]]] = None
    ):
        """
        啟動 WebSocket 連接

        Args:
            symbols: 要訂閱的交易對列表
            kline_timeframes: K 線訂閱配置，格式 {symbol: [timeframes]}
                             例: {"BTC/USDT": ["1h", "4h"], "ETH/USDT": ["1h"]}
        """
        if self._running:
            logger.warning("WebSocket 已在運行中")
            return

        # 檢查交易所是否支持 WebSocket
        if not self._check_ws_support():
            logger.error("交易所不支持 CCXT Pro WebSocket API")
            return

        self._running = True
        self._subscribed_symbols = set(symbols)

        # 設置 K 線訂閱
        if kline_timeframes:
            for symbol, timeframes in kline_timeframes.items():
                self._subscribed_klines[symbol] = set(timeframes)

        # 啟動 Ticker 監聽
        ticker_task = asyncio.create_task(
            self._watch_tickers_loop(),
            name="ws_tickers"
        )
        self._tasks.append(ticker_task)

        # 啟動 K 線監聽
        for symbol, timeframes in self._subscribed_klines.items():
            for tf in timeframes:
                kline_task = asyncio.create_task(
                    self._watch_klines_loop(symbol, tf),
                    name=f"ws_klines_{symbol}_{tf}"
                )
                self._tasks.append(kline_task)

        logger.info(
            f"WebSocket 已啟動: {len(symbols)} 個交易對, "
            f"{sum(len(tfs) for tfs in self._subscribed_klines.values())} 個 K 線訂閱"
        )

    async def stop(self):
        """停止 WebSocket 連接"""
        if not self._running:
            return

        self._running = False

        # 取消所有任務
        for task in self._tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._tasks.clear()

        # 關閉 WebSocket 連接
        try:
            if hasattr(self.exchange, 'close'):
                await self.exchange.close()
        except Exception as e:
            logger.warning(f"關閉 WebSocket 時發生錯誤: {e}")

        logger.info("WebSocket 已停止")

    def _check_ws_support(self) -> bool:
        """檢查交易所是否支持 WebSocket"""
        return (
            hasattr(self.exchange, 'watch_ticker') or
            hasattr(self.exchange, 'watch_tickers')
        )

    # ========== 數據訂閱循環 ==========

    async def _watch_tickers_loop(self):
        """Ticker 監聽循環"""
        while self._running:
            try:
                symbols = list(self._subscribed_symbols)

                # 使用 watch_tickers 批量訂閱 (更高效)
                if hasattr(self.exchange, 'watch_tickers'):
                    tickers = await self.exchange.watch_tickers(symbols)
                    for symbol, ticker_data in tickers.items():
                        self._process_ticker(symbol, ticker_data)
                else:
                    # 回退到單個訂閱
                    for symbol in symbols:
                        ticker_data = await self.exchange.watch_ticker(symbol)
                        self._process_ticker(symbol, ticker_data)

                # 成功接收數據，重置計數並標記健康
                self._reconnect_count = 0
                self._healthy = True
                self._connection_errors = 0

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._healthy = False
                self._connection_errors += 1
                # 使用改進的錯誤處理
                if _error_handler:
                    error_info = _error_handler.handle_error(e, "Ticker WebSocket")
                    if error_info.retryable:
                        await self._handle_reconnect(error_info.retry_delay)
                    else:
                        logger.critical(f"WebSocket 不可恢復錯誤: {e}")
                        self._running = False
                        break
                else:
                    logger.error(f"Ticker WebSocket 錯誤: {e}")
                    await self._handle_reconnect()

    async def _watch_klines_loop(self, symbol: str, timeframe: str):
        """K 線監聽循環"""
        while self._running:
            try:
                if hasattr(self.exchange, 'watch_ohlcv'):
                    ohlcv_list = await self.exchange.watch_ohlcv(symbol, timeframe)
                    for ohlcv in ohlcv_list:
                        self._process_kline(symbol, timeframe, ohlcv)
                else:
                    logger.warning(f"交易所不支持 watch_ohlcv")
                    break

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"K線 WebSocket 錯誤 ({symbol} {timeframe}): {e}")
                await asyncio.sleep(self.reconnect_delay)

    def _process_ticker(self, symbol: str, data: dict):
        """處理 Ticker 數據"""
        ticker = TickerData.from_ccxt(symbol, data)
        self._tickers[symbol] = ticker
        self._ticker_updates += 1
        self._last_update = datetime.now()

        # 調用回調
        if self.on_ticker_update:
            try:
                self.on_ticker_update(symbol, ticker)
            except Exception as e:
                logger.error(f"Ticker 回調錯誤: {e}")

    def _process_kline(self, symbol: str, timeframe: str, ohlcv: list):
        """處理 K 線數據"""
        kline = KlineData.from_ccxt(symbol, timeframe, ohlcv)

        # 更新或追加 K 線
        klines = self._klines[symbol][timeframe]
        if klines and klines[-1].timestamp == kline.timestamp:
            # 更新最後一根 K 線
            klines[-1] = kline
        else:
            # 追加新 K 線 (保留最近 500 根)
            klines.append(kline)
            if len(klines) > 500:
                klines.pop(0)

        self._kline_updates += 1
        self._last_update = datetime.now()

        # 調用回調
        if self.on_kline_update:
            try:
                self.on_kline_update(symbol, timeframe, kline)
            except Exception as e:
                logger.error(f"K線 回調錯誤: {e}")

    async def _handle_reconnect(self, suggested_delay: float = None):
        """
        處理重連（指數退避）

        Args:
            suggested_delay: 建議的延遲時間（來自錯誤處理器）
        """
        self._reconnect_count += 1

        if self._reconnect_count > self.max_reconnect_attempts:
            logger.error(f"達到最大重連次數 ({self.max_reconnect_attempts})，停止 WebSocket")
            self._running = False
            return

        # 使用建議的延遲或計算指數退避延遲
        if suggested_delay:
            delay = suggested_delay
        else:
            delay = self.reconnect_delay * (2 ** min(self._reconnect_count - 1, 5))
        logger.info(f"將在 {delay:.1f} 秒後嘗試重連 (第 {self._reconnect_count} 次)")
        await asyncio.sleep(delay)

    # ========== 數據獲取 ==========

    def get_ticker(self, symbol: str) -> Optional[TickerData]:
        """
        獲取 Ticker 數據

        Args:
            symbol: 交易對名稱

        Returns:
            TickerData 或 None
        """
        return self._tickers.get(symbol)

    def get_all_tickers(self) -> Dict[str, TickerData]:
        """獲取所有 Ticker 數據"""
        return self._tickers.copy()

    def get_klines(
        self,
        symbol: str,
        timeframe: str,
        limit: Optional[int] = None
    ) -> List[KlineData]:
        """
        獲取 K 線數據

        Args:
            symbol: 交易對名稱
            timeframe: 時間週期
            limit: 返回數量限制

        Returns:
            KlineData 列表
        """
        klines = self._klines.get(symbol, {}).get(timeframe, [])
        if limit:
            return klines[-limit:]
        return klines.copy()

    def get_klines_as_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: Optional[int] = None
    ) -> List[List]:
        """
        獲取 K 線數據 (CCXT OHLCV 格式)

        Args:
            symbol: 交易對名稱
            timeframe: 時間週期
            limit: 返回數量限制

        Returns:
            [[timestamp, open, high, low, close, volume], ...]
        """
        klines = self.get_klines(symbol, timeframe, limit)
        return [
            [k.timestamp, k.open, k.high, k.low, k.close, k.volume]
            for k in klines
        ]

    # ========== 訂閱管理 ==========

    def add_symbol(self, symbol: str):
        """添加交易對訂閱"""
        self._subscribed_symbols.add(symbol)
        logger.info(f"已添加訂閱: {symbol}")

    def remove_symbol(self, symbol: str):
        """移除交易對訂閱"""
        self._subscribed_symbols.discard(symbol)
        self._tickers.pop(symbol, None)
        logger.info(f"已移除訂閱: {symbol}")

    def add_kline_subscription(self, symbol: str, timeframe: str):
        """添加 K 線訂閱"""
        self._subscribed_klines[symbol].add(timeframe)

        if self._running:
            # 動態啟動新的 K 線監聽
            task = asyncio.create_task(
                self._watch_klines_loop(symbol, timeframe),
                name=f"ws_klines_{symbol}_{timeframe}"
            )
            self._tasks.append(task)

        logger.info(f"已添加 K線訂閱: {symbol} {timeframe}")

    # ========== 狀態查詢 ==========

    @property
    def is_running(self) -> bool:
        """是否正在運行"""
        return self._running

    @property
    def is_connected(self) -> bool:
        """是否已連接 (有數據更新)"""
        if not self._running or not self._last_update:
            return False
        # 30 秒內有更新視為已連接
        return (datetime.now() - self._last_update).total_seconds() < 30

    def get_stats(self) -> Dict[str, Any]:
        """獲取統計信息"""
        return {
            'running': self._running,
            'connected': self.is_connected,
            'subscribed_symbols': len(self._subscribed_symbols),
            'kline_subscriptions': sum(len(tfs) for tfs in self._subscribed_klines.values()),
            'ticker_updates': self._ticker_updates,
            'kline_updates': self._kline_updates,
            'cached_tickers': len(self._tickers),
            'reconnect_count': self._reconnect_count,
            'last_update': self._last_update.isoformat() if self._last_update else None
        }


# ========== 工廠函數 ==========

async def create_ws_provider(
    exchange_id: str = 'bitget',
    api_key: Optional[str] = None,
    secret: Optional[str] = None,
    password: Optional[str] = None,
    sandbox: bool = False,
    **kwargs
) -> WebSocketDataProvider:
    """
    創建 WebSocket 數據提供者

    Args:
        exchange_id: 交易所 ID (支持 CCXT Pro 的交易所)
        api_key: API Key (可選)
        secret: API Secret (可選)
        password: API Passphrase (可選，Bitget 需要)
        sandbox: 是否使用沙盒模式
        **kwargs: 傳遞給 WebSocketDataProvider 的其他參數

    Returns:
        WebSocketDataProvider 實例
    """
    try:
        # 嘗試導入 ccxt.pro
        import ccxt.pro as ccxtpro

        exchange_class = getattr(ccxtpro, exchange_id, None)
        if not exchange_class:
            raise ValueError(f"交易所 {exchange_id} 不支持 CCXT Pro")

        config = {
            'enableRateLimit': True,
        }

        if api_key:
            config['apiKey'] = api_key
        if secret:
            config['secret'] = secret
        if password:
            config['password'] = password

        exchange = exchange_class(config)

        if sandbox:
            exchange.set_sandbox_mode(True)

        return WebSocketDataProvider(exchange, **kwargs)

    except ImportError:
        logger.error("ccxt.pro 未安裝，請使用 pip install ccxt[pro]")
        raise
    except Exception as e:
        logger.error(f"創建 WebSocket 提供者失敗: {e}")
        raise


# ========== 混合數據提供者 ==========

class HybridDataProvider:
    """
    混合數據提供者

    結合 WebSocket 即時數據和 REST API 歷史數據，
    提供最佳的數據獲取體驗。

    - Ticker 數據: 優先使用 WebSocket，降級到 REST
    - K 線歷史: 使用 REST API 獲取完整歷史
    - K 線即時: 使用 WebSocket 補充最新數據
    """

    def __init__(
        self,
        exchange: Any,
        ws_provider: Optional[WebSocketDataProvider] = None
    ):
        """
        初始化混合數據提供者

        Args:
            exchange: CCXT 交易所實例 (REST API)
            ws_provider: WebSocket 數據提供者 (可選)
        """
        self.exchange = exchange
        self.ws_provider = ws_provider
        self._use_ws = ws_provider is not None and ws_provider.is_running

    async def get_ticker(self, symbol: str) -> dict:
        """
        獲取 Ticker 數據

        優先使用 WebSocket 快取，否則回退到 REST API
        """
        # 嘗試 WebSocket
        if self._use_ws and self.ws_provider:
            ticker = self.ws_provider.get_ticker(symbol)
            if ticker and (datetime.now() - ticker.timestamp).total_seconds() < 60:
                return {
                    'symbol': ticker.symbol,
                    'last': ticker.last,
                    'bid': ticker.bid,
                    'ask': ticker.ask,
                    'high': ticker.high,
                    'low': ticker.low,
                    'baseVolume': ticker.volume,
                    'quoteVolume': ticker.quote_volume,
                    'percentage': ticker.change_24h,
                    'timestamp': ticker.timestamp.timestamp() * 1000
                }

        # 回退到 REST
        return await self.exchange.fetch_ticker(symbol)

    async def get_tickers(self, symbols: List[str]) -> Dict[str, dict]:
        """批量獲取 Ticker 數據"""
        if self._use_ws and self.ws_provider:
            result = {}
            missing = []

            for symbol in symbols:
                ticker = self.ws_provider.get_ticker(symbol)
                if ticker and (datetime.now() - ticker.timestamp).total_seconds() < 60:
                    result[symbol] = {
                        'symbol': ticker.symbol,
                        'last': ticker.last,
                        'quoteVolume': ticker.quote_volume,
                        'percentage': ticker.change_24h
                    }
                else:
                    missing.append(symbol)

            # 獲取缺失的
            if missing:
                if hasattr(self.exchange, 'fetch_tickers'):
                    rest_tickers = await self.exchange.fetch_tickers(missing)
                    result.update(rest_tickers)
                else:
                    for symbol in missing:
                        result[symbol] = await self.exchange.fetch_ticker(symbol)

            return result

        # 全部使用 REST
        if hasattr(self.exchange, 'fetch_tickers'):
            return await self.exchange.fetch_tickers(symbols)

        result = {}
        for symbol in symbols:
            result[symbol] = await self.exchange.fetch_ticker(symbol)
        return result

    async def get_ohlcv(
        self,
        symbol: str,
        timeframe: str = '1h',
        limit: int = 168
    ) -> List[List]:
        """
        獲取 OHLCV K 線數據

        結合 REST 歷史和 WebSocket 即時數據
        """
        # 先獲取 REST 歷史數據
        klines = await self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)

        # 如果有 WebSocket 數據，補充最新的
        if self._use_ws and self.ws_provider:
            ws_klines = self.ws_provider.get_klines_as_ohlcv(symbol, timeframe)

            if ws_klines and klines:
                last_rest_ts = klines[-1][0]
                # 添加比 REST 更新的 K 線
                for ws_k in ws_klines:
                    if ws_k[0] > last_rest_ts:
                        klines.append(ws_k)
                    elif ws_k[0] == last_rest_ts:
                        # 更新最後一根
                        klines[-1] = ws_k

        return klines[-limit:] if len(klines) > limit else klines

    def set_ws_provider(self, ws_provider: WebSocketDataProvider):
        """設置 WebSocket 提供者"""
        self.ws_provider = ws_provider
        self._use_ws = ws_provider.is_running

    @property
    def is_ws_connected(self) -> bool:
        """WebSocket 是否已連接"""
        return self._use_ws and self.ws_provider and self.ws_provider.is_connected
