"""
CoinScorer - 多維度評分引擎
============================

評分因子:
- 波動率 (15%): ATR/Price 在 2%-5% 得高分
- 流動性 (20%): 24h Volume > $500M 且 Volume CV < 0.5 得滿分
- 均值回歸 (40%): Hurst < 0.4 + ADF p < 0.05 得高分 (核心指標)
- 動量 (15%): ADX < 20 得高分
- 穩定性 (10%): Volume CV 和綜合穩定度

使用方式:
    scorer = CoinScorer()
    score = await scorer.score_coin("XRPUSDC", exchange)
    scores = await scorer.score_all(["XRPUSDC", "DOGEUSDC"], exchange)

優化功能:
- 批量獲取 ticker 數據 (fetch_tickers)
- 內建快取機制 (預設 15 分鐘)
- 可配置刷新間隔
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
import numpy as np

from .models import CoinScore

logger = logging.getLogger(__name__)


class DataCache:
    """數據快取管理器"""

    def __init__(self, ttl_minutes: int = 15):
        """
        初始化快取

        Args:
            ttl_minutes: 快取有效時間 (分鐘)
        """
        self.ttl = timedelta(minutes=ttl_minutes)
        self._klines_cache: Dict[str, tuple] = {}  # {symbol: (timestamp, data)}
        self._tickers_cache: tuple = (None, {})    # (timestamp, {symbol: ticker})

    def get_klines(self, symbol: str) -> Optional[list]:
        """獲取 K 線快取"""
        if symbol in self._klines_cache:
            ts, data = self._klines_cache[symbol]
            if datetime.now() - ts < self.ttl:
                return data
        return None

    def set_klines(self, symbol: str, data: list):
        """設置 K 線快取"""
        self._klines_cache[symbol] = (datetime.now(), data)

    def get_ticker(self, symbol: str) -> Optional[dict]:
        """獲取 Ticker 快取"""
        ts, tickers = self._tickers_cache
        if ts and datetime.now() - ts < self.ttl:
            return tickers.get(symbol)
        return None

    def set_tickers(self, tickers: Dict[str, dict]):
        """批量設置 Ticker 快取"""
        self._tickers_cache = (datetime.now(), tickers)

    def clear(self):
        """清除所有快取"""
        self._klines_cache.clear()
        self._tickers_cache = (None, {})

    def set_ttl(self, minutes: int):
        """更新快取有效時間"""
        self.ttl = timedelta(minutes=minutes)


# 全局快取實例
_data_cache = DataCache(ttl_minutes=15)


class CoinScorer:
    """
    多維度幣種評分引擎

    評分權重:
    - 波動率 (volatility): 20%
    - 流動性 (liquidity): 25%
    - 均值回歸 (mean_revert): 35% (最重要)
    - 動量 (momentum): 20%

    數據獲取方式:
    - 預設: REST API (fetch_ohlcv, fetch_ticker)
    - 可選: HybridDataProvider (WebSocket + REST 混合)
    """

    DEFAULT_WEIGHTS = {
        'volatility': 0.15,
        'liquidity': 0.20,
        'mean_revert': 0.40,  # 提高均值回歸權重 (核心)
        'momentum': 0.15,
        'stability': 0.10     # 新增穩定性維度
    }

    # 評分參數
    OPTIMAL_ATR_MIN = 0.02  # 最佳 ATR 下限 2%
    OPTIMAL_ATR_MAX = 0.05  # 最佳 ATR 上限 5%
    MIN_VOLUME_24H = 50_000_000  # 最低 24h 交易量 $50M
    HIGH_VOLUME_24H = 500_000_000  # 高流動性 $500M
    GOOD_VOLUME_24H = 100_000_000  # 良好流動性 $100M

    # Volume CV 閾值 (交易量穩定性)
    GOOD_VOLUME_CV = 0.5    # 優良穩定性 (CV < 0.5)
    MAX_VOLUME_CV = 1.0     # 可接受上限

    # ADF 測試閾值
    ADF_SIGNIFICANCE = 0.05  # 顯著性水平

    def __init__(
        self,
        weights: Optional[Dict[str, float]] = None,
        data_provider: Optional[Any] = None
    ):
        """
        初始化評分引擎

        Args:
            weights: 自定義權重，格式為 {'volatility': 0.2, 'liquidity': 0.25, ...}
            data_provider: 數據提供者 (HybridDataProvider)，可加速數據獲取
        """
        self.weights = weights or self.DEFAULT_WEIGHTS.copy()
        self.data_provider = data_provider  # HybridDataProvider 實例

        # 驗證權重總和為 1
        total_weight = sum(self.weights.values())
        if abs(total_weight - 1.0) > 0.01:
            logger.warning(f"權重總和 {total_weight:.2f} 不等於 1，將自動正規化")
            for key in self.weights:
                self.weights[key] /= total_weight

    def set_data_provider(self, provider: Any):
        """設置數據提供者 (HybridDataProvider)"""
        self.data_provider = provider
        logger.info("已設置混合數據提供者")

    async def score_coin(
        self,
        symbol: str,
        exchange: Any,
        timeframe: str = '1h',
        limit: int = 168  # 7 天的小時數據
    ) -> CoinScore:
        """
        計算單一幣種評分

        Args:
            symbol: 交易對名稱 (e.g., "XRPUSDC")
            exchange: CCXT 交易所實例
            timeframe: K線週期
            limit: K線數量

        Returns:
            CoinScore 評分結果
        """
        try:
            # 獲取 K 線數據
            klines = await exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            if not klines or len(klines) < 50:
                logger.warning(f"{symbol} K線數據不足")
                return self._create_empty_score(symbol)

            # 獲取 24h 交易量
            ticker = await exchange.fetch_ticker(symbol)
            volume_24h = ticker.get('quoteVolume', 0) or 0

            # 計算各維度評分
            closes = np.array([k[4] for k in klines])
            highs = np.array([k[2] for k in klines])
            lows = np.array([k[3] for k in klines])
            volumes = np.array([k[5] for k in klines])

            # 1. 波動率評分
            atr_pct = self._calculate_atr_pct(highs, lows, closes)
            volatility_score = self._calc_volatility_score(atr_pct)

            # 2. 流動性評分
            liquidity_score = self._calc_liquidity_score(volume_24h)

            # 3. 均值回歸評分 (核心) - 結合 Hurst 和 ADF
            hurst = self._calculate_hurst_exponent(closes)
            adf_pvalue = self._calculate_adf_test(closes)
            mean_revert_score = self._calc_mean_revert_score(hurst, adf_pvalue)

            # 4. 動量評分
            adx = self._calculate_adx(highs, lows, closes)
            momentum_score = self._calc_momentum_score(adx)

            # 5. 穩定性評分 (新增)
            volume_cv = self._calculate_volume_cv(volumes)
            stability_score = self._calc_stability_score(volume_cv, adf_pvalue)

            # 計算加權總分
            final_score = (
                volatility_score * self.weights['volatility'] +
                liquidity_score * self.weights['liquidity'] +
                mean_revert_score * self.weights['mean_revert'] +
                momentum_score * self.weights['momentum'] +
                stability_score * self.weights.get('stability', 0.10)
            )

            return CoinScore(
                symbol=symbol,
                volatility_score=volatility_score,
                liquidity_score=liquidity_score,
                mean_revert_score=mean_revert_score,
                momentum_score=momentum_score,
                final_score=final_score,
                timestamp=datetime.now(),
                atr_pct=atr_pct,
                volume_24h=volume_24h,
                hurst_exponent=hurst,
                adx=adx,
                volume_cv=volume_cv,
                adf_pvalue=adf_pvalue
            )

        except Exception as e:
            logger.error(f"評分 {symbol} 時發生錯誤: {e}")
            return self._create_empty_score(symbol)

    async def score_all(
        self,
        symbols: List[str],
        exchange: Any,
        timeframe: str = '1h',
        limit: int = 168,
        use_cache: bool = True
    ) -> List[CoinScore]:
        """
        批量評分所有候選幣種 (優化版本)

        Args:
            symbols: 交易對列表
            exchange: CCXT 交易所實例
            timeframe: K線週期
            limit: K線數量
            use_cache: 是否使用快取

        Returns:
            CoinScore 列表，按總分降序排列

        優化:
            - 批量獲取 ticker 數據 (1 次 API 調用)
            - 使用快取減少重複請求
        """
        # 1. 批量預取 ticker 數據
        await self._prefetch_tickers(symbols, exchange, use_cache)

        # 2. 並行評分所有幣種
        tasks = [
            self._score_coin_cached(symbol, exchange, timeframe, limit, use_cache)
            for symbol in symbols
        ]
        scores = await asyncio.gather(*tasks, return_exceptions=True)

        # 過濾錯誤結果
        valid_scores = []
        for score in scores:
            if isinstance(score, CoinScore):
                valid_scores.append(score)
            elif isinstance(score, Exception):
                logger.error(f"評分錯誤: {score}")

        # 按總分降序排列
        valid_scores.sort(key=lambda x: x.final_score, reverse=True)
        return valid_scores

    async def _prefetch_tickers(
        self,
        symbols: List[str],
        exchange: Any,
        use_cache: bool = True
    ):
        """
        批量預取 ticker 數據

        優先順序:
        1. HybridDataProvider (WebSocket + REST 混合) - 最快
        2. fetch_tickers 批量獲取 - 快
        3. 並行 fetch_ticker - 較慢
        """
        # 檢查快取
        if use_cache:
            all_cached = all(_data_cache.get_ticker(s) is not None for s in symbols)
            if all_cached:
                logger.debug("Ticker 數據已快取，跳過獲取")
                return

        try:
            # 優先使用 HybridDataProvider
            if self.data_provider is not None:
                tickers = await self.data_provider.get_tickers(symbols)
                _data_cache.set_tickers(tickers)
                ws_status = "WS" if getattr(self.data_provider, 'is_ws_connected', False) else "REST"
                logger.info(f"[{ws_status}] 批量獲取 {len(tickers)} 個 ticker 成功")
                return

            # 嘗試批量獲取
            if hasattr(exchange, 'fetch_tickers'):
                tickers = await exchange.fetch_tickers(symbols)
                _data_cache.set_tickers(tickers)
                logger.info(f"批量獲取 {len(tickers)} 個 ticker 成功")
            else:
                # 交易所不支援批量獲取，改用並行單獨獲取
                logger.warning("交易所不支援 fetch_tickers，改用並行獲取")
                tasks = [exchange.fetch_ticker(s) for s in symbols]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                tickers = {}
                for symbol, result in zip(symbols, results):
                    if not isinstance(result, Exception):
                        tickers[symbol] = result
                _data_cache.set_tickers(tickers)

        except Exception as e:
            logger.warning(f"批量獲取 ticker 失敗: {e}，將在評分時單獨獲取")

    async def _score_coin_cached(
        self,
        symbol: str,
        exchange: Any,
        timeframe: str = '1h',
        limit: int = 168,
        use_cache: bool = True
    ) -> CoinScore:
        """
        計算單一幣種評分 (帶快取)

        數據獲取優先順序:
        1. 內部快取
        2. HybridDataProvider (WebSocket + REST)
        3. 直接 REST API
        """
        try:
            # 獲取 K 線數據 (檢查快取)
            klines = None
            if use_cache:
                klines = _data_cache.get_klines(symbol)

            if klines is None:
                # 優先使用 HybridDataProvider
                if self.data_provider is not None:
                    klines = await self.data_provider.get_ohlcv(symbol, timeframe, limit)
                else:
                    klines = await exchange.fetch_ohlcv(symbol, timeframe, limit=limit)

                if use_cache and klines:
                    _data_cache.set_klines(symbol, klines)

            if not klines or len(klines) < 50:
                logger.warning(f"{symbol} K線數據不足")
                return self._create_empty_score(symbol)

            # 獲取 24h 交易量 (優先從快取)
            ticker = _data_cache.get_ticker(symbol) if use_cache else None
            if ticker is None:
                if self.data_provider is not None:
                    ticker = await self.data_provider.get_ticker(symbol)
                else:
                    ticker = await exchange.fetch_ticker(symbol)

            volume_24h = ticker.get('quoteVolume', 0) or 0

            # 計算評分 (與原始方法相同)
            closes = np.array([k[4] for k in klines])
            highs = np.array([k[2] for k in klines])
            lows = np.array([k[3] for k in klines])
            volumes = np.array([k[5] for k in klines])

            atr_pct = self._calculate_atr_pct(highs, lows, closes)
            volatility_score = self._calc_volatility_score(atr_pct)
            liquidity_score = self._calc_liquidity_score(volume_24h)
            hurst = self._calculate_hurst_exponent(closes)
            adf_pvalue = self._calculate_adf_test(closes)
            mean_revert_score = self._calc_mean_revert_score(hurst, adf_pvalue)
            adx = self._calculate_adx(highs, lows, closes)
            momentum_score = self._calc_momentum_score(adx)
            volume_cv = self._calculate_volume_cv(volumes)
            stability_score = self._calc_stability_score(volume_cv, adf_pvalue)

            final_score = (
                volatility_score * self.weights['volatility'] +
                liquidity_score * self.weights['liquidity'] +
                mean_revert_score * self.weights['mean_revert'] +
                momentum_score * self.weights['momentum'] +
                stability_score * self.weights.get('stability', 0.10)
            )

            return CoinScore(
                symbol=symbol,
                volatility_score=volatility_score,
                liquidity_score=liquidity_score,
                mean_revert_score=mean_revert_score,
                momentum_score=momentum_score,
                final_score=final_score,
                timestamp=datetime.now(),
                atr_pct=atr_pct,
                volume_24h=volume_24h,
                hurst_exponent=hurst,
                adx=adx,
                volume_cv=volume_cv,
                adf_pvalue=adf_pvalue
            )

        except Exception as e:
            logger.error(f"評分 {symbol} 時發生錯誤: {e}")
            return self._create_empty_score(symbol)

    # ========== 評分計算方法 ==========

    def _calc_volatility_score(self, atr_pct: float) -> float:
        """
        計算波動率評分

        最佳區間: ATR/Price = 2%-5%
        - < 1%: 太平穩，網格利潤低
        - 1-2%: 略低，評分 60-80
        - 2-5%: 最佳，評分 80-100
        - 5-10%: 略高，評分 60-80
        - > 10%: 風險過高，評分 < 60
        """
        if self.OPTIMAL_ATR_MIN <= atr_pct <= self.OPTIMAL_ATR_MAX:
            # 最佳區間：80-100 分
            mid_point = (self.OPTIMAL_ATR_MIN + self.OPTIMAL_ATR_MAX) / 2
            deviation = abs(atr_pct - mid_point) / (self.OPTIMAL_ATR_MAX - self.OPTIMAL_ATR_MIN) * 2
            return 80 + (1 - deviation) * 20
        elif 0.01 <= atr_pct < self.OPTIMAL_ATR_MIN:
            # 略低：60-80 分
            return 60 + 20 * (atr_pct - 0.01) / (self.OPTIMAL_ATR_MIN - 0.01)
        elif self.OPTIMAL_ATR_MAX < atr_pct <= 0.10:
            # 略高：60-80 分
            return 80 - 20 * (atr_pct - self.OPTIMAL_ATR_MAX) / (0.10 - self.OPTIMAL_ATR_MAX)
        elif atr_pct < 0.01:
            # 太低：0-60 分
            return max(0, 60 * atr_pct / 0.01)
        else:
            # 太高：0-60 分
            return max(0, 60 - 60 * (atr_pct - 0.10) / 0.10)

    def _calc_liquidity_score(self, volume_24h: float) -> float:
        """
        計算流動性評分

        基於 24h 交易量:
        - Volume > $500M: 100分
        - Volume > $100M: 80-100分
        - Volume > $50M: 60-80分
        - Volume < $50M: 不建議
        """
        if volume_24h >= self.HIGH_VOLUME_24H:
            return 100
        elif volume_24h >= self.GOOD_VOLUME_24H:
            return 80 + 20 * (volume_24h - self.GOOD_VOLUME_24H) / (self.HIGH_VOLUME_24H - self.GOOD_VOLUME_24H)
        elif volume_24h >= self.MIN_VOLUME_24H:
            return 60 + 20 * (volume_24h - self.MIN_VOLUME_24H) / (self.GOOD_VOLUME_24H - self.MIN_VOLUME_24H)
        else:
            return max(0, 60 * volume_24h / self.MIN_VOLUME_24H)

    def _calc_mean_revert_score(self, hurst: float, adf_pvalue: float = 1.0) -> float:
        """
        計算均值回歸評分 (核心指標)

        結合 Hurst Exponent 和 ADF 測試:
        - Hurst < 0.5 表示均值回歸傾向
        - ADF p-value < 0.05 表示序列平穩

        評分邏輯:
        - H < 0.4 + ADF < 0.05: 強均值回歸，評分 95-100
        - H < 0.4: 均值回歸，評分 80-95
        - H = 0.4-0.5: 中等均值回歸，評分 60-80
        - H = 0.5: 隨機遊走，評分 50
        - H > 0.5: 趨勢性，評分 < 50 (不適合網格)
        """
        # Hurst 基礎分數
        if hurst < 0.4:
            hurst_score = 80 + 15 * (0.4 - hurst) / 0.4
        elif hurst < 0.5:
            hurst_score = 60 + 20 * (0.5 - hurst) / 0.1
        elif hurst == 0.5:
            hurst_score = 50
        else:
            hurst_score = max(0, 50 - 50 * (hurst - 0.5) / 0.5)

        # ADF 加成 (最多 +10 分)
        adf_bonus = 0
        if adf_pvalue < self.ADF_SIGNIFICANCE:
            # p < 0.05，序列平穩，給予加成
            adf_bonus = 10 * (1 - adf_pvalue / self.ADF_SIGNIFICANCE)
        elif adf_pvalue < 0.1:
            # p < 0.1，接近顯著，小幅加成
            adf_bonus = 5 * (0.1 - adf_pvalue) / 0.05

        return min(100, hurst_score + adf_bonus)

    def _calc_momentum_score(self, adx: float) -> float:
        """
        計算動量評分

        網格偏好低動量/區間震盪:
        - ADX < 20: 區間震盪，評分 80-100
        - ADX 20-25: 弱趨勢，評分 60-80
        - ADX > 25: 強趨勢，評分 < 60 (不適合)
        """
        if adx < 20:
            return 80 + 20 * (20 - adx) / 20
        elif adx <= 25:
            return 60 + 20 * (25 - adx) / 5
        else:
            return max(0, 60 - 2 * (adx - 25))

    # ========== 技術指標計算 ==========

    def _calculate_atr_pct(
        self,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        period: int = 14
    ) -> float:
        """
        計算 ATR 佔價格的百分比

        ATR (Average True Range) 衡量價格波動幅度
        使用 Wilder 平滑公式 (業界標準)
        """
        if len(closes) < period + 1:
            return 0.0

        # True Range = max(High-Low, |High-PrevClose|, |Low-PrevClose|)
        tr1 = highs[1:] - lows[1:]
        tr2 = np.abs(highs[1:] - closes[:-1])
        tr3 = np.abs(lows[1:] - closes[:-1])
        tr = np.maximum(tr1, np.maximum(tr2, tr3))

        # ATR 使用 Wilder 平滑 (RMA)
        # 公式: ATR = [(Previous ATR × (period-1)) + Current TR] / period
        if len(tr) < period:
            return 0.0

        # 初始 ATR = 前 period 個 TR 的簡單平均
        atr = np.mean(tr[:period])

        # 後續使用 Wilder 平滑
        for i in range(period, len(tr)):
            atr = (atr * (period - 1) + tr[i]) / period

        # 佔價格百分比
        current_price = closes[-1]
        if current_price > 0:
            return atr / current_price
        return 0.0

    def _calculate_hurst_exponent(
        self,
        prices: np.ndarray,
        max_lag: int = 20
    ) -> float:
        """
        計算 Hurst Exponent (R/S 分析法)

        H < 0.5: 均值回歸 (適合網格)
        H = 0.5: 隨機遊走
        H > 0.5: 趨勢性 (不適合網格)
        """
        if len(prices) < max_lag * 2:
            return 0.5  # 數據不足，返回中性值

        try:
            # 過濾非正數價格（避免 log 產生 NaN/warning）
            prices = prices[prices > 0]
            if len(prices) < max_lag * 2:
                return 0.5

            # 計算對數收益率
            log_returns = np.diff(np.log(prices))

            # R/S 分析
            lags = range(2, max_lag + 1)
            rs_values = []

            for lag in lags:
                # 分割成子序列
                n_chunks = len(log_returns) // lag
                if n_chunks < 1:
                    continue

                rs_sum = 0
                for i in range(n_chunks):
                    chunk = log_returns[i * lag:(i + 1) * lag]
                    # 計算累積離差
                    mean_chunk = np.mean(chunk)
                    cumdev = np.cumsum(chunk - mean_chunk)
                    # R = max - min
                    r = np.max(cumdev) - np.min(cumdev)
                    # S = 標準差
                    s = np.std(chunk, ddof=1)
                    if s > 0:
                        rs_sum += r / s

                if n_chunks > 0:
                    rs_values.append((lag, rs_sum / n_chunks))

            # 過濾 R/S <= 0 的值（避免 log(0) warning）
            rs_values = [(lag, rs) for lag, rs in rs_values if rs > 0]

            if len(rs_values) < 3:
                return 0.5

            # 線性回歸計算 H
            log_lags = np.log([x[0] for x in rs_values])
            log_rs = np.log([x[1] for x in rs_values])

            # H = slope of log(R/S) vs log(lag)
            slope = np.polyfit(log_lags, log_rs, 1)[0]

            # 限制在合理範圍
            return np.clip(slope, 0, 1)

        except Exception as e:
            logger.warning(f"Hurst 計算錯誤: {e}")
            return 0.5

    def _calculate_volume_cv(self, volumes: np.ndarray) -> float:
        """
        計算交易量變異係數 (Coefficient of Variation)

        CV = 標準差 / 平均值
        CV < 0.5: 穩定 (適合網格)
        CV > 1.0: 不穩定 (風險較高)
        """
        if len(volumes) < 10:
            return 1.0

        try:
            mean_vol = np.mean(volumes)
            if mean_vol <= 0:
                return 1.0
            std_vol = np.std(volumes)
            return std_vol / mean_vol
        except Exception:
            return 1.0

    def _calculate_adf_test(self, prices: np.ndarray) -> float:
        """
        計算 ADF (Augmented Dickey-Fuller) 測試 p-value

        p < 0.05: 序列平穩，拒絕單位根假設
        p > 0.05: 序列非平穩

        使用簡化版 ADF 測試 (避免 statsmodels 依賴)
        """
        if len(prices) < 30:
            return 1.0

        try:
            # 過濾非正數價格（避免 log 產生 NaN/warning）
            prices = prices[prices > 0]
            if len(prices) < 30:
                return 1.0

            # 計算對數價格差分
            log_prices = np.log(prices)
            diff = np.diff(log_prices)

            # 滯後項
            y = diff[1:]
            y_lag = diff[:-1]

            if len(y) < 20:
                return 1.0

            # 簡單 OLS 回歸: y[t] = alpha + beta * y[t-1] + error
            n = len(y)
            x_mean = np.mean(y_lag)
            y_mean = np.mean(y)

            # 計算 beta (斜率)
            num = np.sum((y_lag - x_mean) * (y - y_mean))
            den = np.sum((y_lag - x_mean) ** 2)

            if den == 0:
                return 1.0

            beta = num / den
            alpha = y_mean - beta * x_mean

            # 計算殘差和 t-統計量
            residuals = y - (alpha + beta * y_lag)
            sse = np.sum(residuals ** 2)
            mse = sse / (n - 2)
            se_beta = np.sqrt(mse / den)

            if se_beta == 0:
                return 1.0

            # t-statistic for beta
            t_stat = beta / se_beta

            # 近似 p-value (使用 ADF 臨界值表)
            # ADF 臨界值: -3.43 (1%), -2.86 (5%), -2.57 (10%)
            if t_stat < -3.43:
                return 0.01
            elif t_stat < -2.86:
                return 0.05
            elif t_stat < -2.57:
                return 0.10
            elif t_stat < -1.94:
                return 0.20
            else:
                return 0.50  # 非平穩

        except Exception as e:
            logger.warning(f"ADF 計算錯誤: {e}")
            return 1.0

    def _calc_stability_score(self, volume_cv: float, adf_pvalue: float) -> float:
        """
        計算穩定性評分

        基於:
        - Volume CV (交易量穩定性)
        - ADF p-value (價格平穩性)
        """
        # Volume CV 評分 (60% 權重)
        if volume_cv <= self.GOOD_VOLUME_CV:
            vol_score = 80 + 20 * (self.GOOD_VOLUME_CV - volume_cv) / self.GOOD_VOLUME_CV
        elif volume_cv <= self.MAX_VOLUME_CV:
            vol_score = 60 + 20 * (self.MAX_VOLUME_CV - volume_cv) / (self.MAX_VOLUME_CV - self.GOOD_VOLUME_CV)
        else:
            vol_score = max(0, 60 - 30 * (volume_cv - self.MAX_VOLUME_CV))

        # ADF 評分 (40% 權重)
        if adf_pvalue < 0.05:
            adf_score = 90 + 10 * (0.05 - adf_pvalue) / 0.05
        elif adf_pvalue < 0.10:
            adf_score = 70 + 20 * (0.10 - adf_pvalue) / 0.05
        else:
            adf_score = max(30, 70 - 40 * (adf_pvalue - 0.10) / 0.40)

        return 0.6 * vol_score + 0.4 * adf_score

    def _calculate_adx(
        self,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        period: int = 14
    ) -> float:
        """
        計算 ADX (Average Directional Index)
        使用 Wilder 平滑 (RMA) - 業界標準

        ADX < 20: 無趨勢/區間震盪
        ADX 20-25: 弱趨勢
        ADX > 25: 強趨勢
        ADX > 50: 極強趨勢
        """
        if len(closes) < period * 2:
            return 25.0  # 數據不足，返回中性值

        try:
            # +DM 和 -DM
            up_move = highs[1:] - highs[:-1]
            down_move = lows[:-1] - lows[1:]

            plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
            minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)

            # True Range
            tr1 = highs[1:] - lows[1:]
            tr2 = np.abs(highs[1:] - closes[:-1])
            tr3 = np.abs(lows[1:] - closes[:-1])
            tr = np.maximum(tr1, np.maximum(tr2, tr3))

            # Wilder 平滑 (RMA) - 正確的 ADX 計算方式
            def wilder_smooth(data, period):
                """Wilder 平滑: RMA = (Previous × (period-1) + Current) / period"""
                result = np.zeros_like(data, dtype=float)
                result[:period] = np.nan
                result[period - 1] = np.mean(data[:period])  # 初始值用 SMA
                for i in range(period, len(data)):
                    result[i] = (result[i - 1] * (period - 1) + data[i]) / period
                return result

            # 平滑 TR, +DM, -DM
            atr = wilder_smooth(tr, period)
            smooth_plus_dm = wilder_smooth(plus_dm, period)
            smooth_minus_dm = wilder_smooth(minus_dm, period)

            # +DI 和 -DI
            plus_di = np.zeros_like(atr)
            minus_di = np.zeros_like(atr)
            valid_idx = atr > 0
            plus_di[valid_idx] = 100 * smooth_plus_dm[valid_idx] / atr[valid_idx]
            minus_di[valid_idx] = 100 * smooth_minus_dm[valid_idx] / atr[valid_idx]

            # DX = |+DI - -DI| / (+DI + -DI) × 100
            di_sum = plus_di + minus_di
            di_diff = np.abs(plus_di - minus_di)
            dx = np.zeros_like(di_sum)
            valid_sum = di_sum > 0
            dx[valid_sum] = 100 * di_diff[valid_sum] / di_sum[valid_sum]

            # ADX = Wilder 平滑 of DX
            adx = wilder_smooth(dx[~np.isnan(dx)], period)

            # 返回最後一個有效值
            valid_adx = adx[~np.isnan(adx)]
            if len(valid_adx) > 0:
                return float(valid_adx[-1])
            return 25.0

        except Exception as e:
            logger.warning(f"ADX 計算錯誤: {e}")
            return 25.0

    def _create_empty_score(self, symbol: str) -> CoinScore:
        """建立空評分 (用於錯誤情況)"""
        return CoinScore(
            symbol=symbol,
            volatility_score=0,
            liquidity_score=0,
            mean_revert_score=0,
            momentum_score=0,
            final_score=0,
            timestamp=datetime.now(),
            atr_pct=0.0,
            volume_24h=0.0,
            hurst_exponent=0.5,
            adx=25.0,
            volume_cv=1.0,
            adf_pvalue=1.0
        )


# ========== 快速評分函數 ==========

async def quick_score(
    symbol: str,
    exchange: Any,
    weights: Optional[Dict[str, float]] = None
) -> CoinScore:
    """
    快速評分單一幣種

    Args:
        symbol: 交易對名稱
        exchange: CCXT 交易所實例
        weights: 自定義權重

    Returns:
        CoinScore 評分結果
    """
    scorer = CoinScorer(weights)
    return await scorer.score_coin(symbol, exchange)


async def quick_rank(
    symbols: List[str],
    exchange: Any,
    weights: Optional[Dict[str, float]] = None
) -> List[CoinScore]:
    """
    快速排名多個幣種

    Args:
        symbols: 交易對列表
        exchange: CCXT 交易所實例
        weights: 自定義權重

    Returns:
        按總分降序排列的 CoinScore 列表
    """
    scorer = CoinScorer(weights)
    return await scorer.score_all(symbols, exchange)


# ========== 快取控制函數 ==========

def set_cache_ttl(minutes: int):
    """
    設置快取有效時間

    Args:
        minutes: 快取有效時間 (分鐘)
    """
    _data_cache.set_ttl(minutes)


def clear_cache():
    """清除所有快取數據"""
    _data_cache.clear()


def get_cache_info() -> Dict[str, Any]:
    """
    獲取快取狀態資訊

    Returns:
        快取狀態字典
    """
    klines_count = len(_data_cache._klines_cache)
    ticker_ts, tickers = _data_cache._tickers_cache
    ticker_count = len(tickers) if tickers else 0

    return {
        'klines_cached': klines_count,
        'tickers_cached': ticker_count,
        'ttl_minutes': _data_cache.ttl.total_seconds() / 60,
        'ticker_cache_age': (
            (datetime.now() - ticker_ts).total_seconds()
            if ticker_ts else None
        )
    }
