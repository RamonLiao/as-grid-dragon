"""
CoinRanker - 即時排名系統
==========================

功能:
- 即時排名所有候選幣種
- 追蹤評分歷史和趨勢
- 產生建議動作 (HOLD/WATCH/MONITOR/AVOID)

使用方式:
    scorer = CoinScorer()
    ranker = CoinRanker(scorer)

    rankings = await ranker.get_rankings(["XRPUSDC", "DOGEUSDC"], exchange)
    for rank in rankings:
        print(rank)
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from collections import defaultdict

from .models import CoinScore, CoinRank, ActionType, TrendType
from .scorer import CoinScorer

logger = logging.getLogger(__name__)


class CoinRanker:
    """
    即時排名系統

    追蹤幣種評分歷史，計算趨勢，產生建議動作
    """

    # 趨勢判定閾值
    TREND_UP_THRESHOLD = 2.0    # 評分上升超過此值視為上升趨勢
    TREND_DOWN_THRESHOLD = -2.0  # 評分下降超過此值視為下降趨勢

    # 動作判定閾值
    HOLD_MIN_SCORE = 80         # 持有的最低評分
    WATCH_MIN_SCORE = 70        # 關注的最低評分
    MONITOR_MIN_SCORE = 50      # 監控的最低評分

    # 歷史保留時間
    HISTORY_RETENTION_HOURS = 24 * 7  # 保留 7 天歷史

    def __init__(
        self,
        scorer: CoinScorer,
        update_interval_minutes: int = 15
    ):
        """
        初始化排名系統

        Args:
            scorer: CoinScorer 評分引擎實例
            update_interval_minutes: 更新間隔 (分鐘)
        """
        self.scorer = scorer
        self.update_interval = update_interval_minutes

        # 評分歷史: {symbol: [(timestamp, score), ...]}
        self.history: Dict[str, List[tuple]] = defaultdict(list)

        # 最近一次排名結果
        self.last_rankings: List[CoinRank] = []
        self.last_update: Optional[datetime] = None

    async def get_rankings(
        self,
        symbols: List[str],
        exchange: Any,
        force_refresh: bool = False
    ) -> List[CoinRank]:
        """
        獲取所有幣種排名

        Args:
            symbols: 候選幣種列表
            exchange: CCXT 交易所實例
            force_refresh: 是否強制刷新 (忽略快取)

        Returns:
            按總分降序排列的 CoinRank 列表
        """
        # 檢查是否需要更新
        if not force_refresh and self._is_cache_valid():
            return self.last_rankings

        # 獲取所有幣種評分
        scores = await self.scorer.score_all(symbols, exchange)

        # 更新歷史
        for score in scores:
            self._record_history(score)

        # 建立排名
        rankings = []
        for i, score in enumerate(scores):
            trend = self._calculate_trend(score.symbol)
            action = self._determine_action(score, trend, i)
            score_change = self._get_score_change_24h(score.symbol)

            rank = CoinRank(
                rank=i + 1,
                symbol=score.symbol,
                score=score,
                trend=trend,
                action=action,
                score_change_24h=score_change
            )
            rankings.append(rank)

        # 更新快取
        self.last_rankings = rankings
        self.last_update = datetime.now()

        return rankings

    async def get_top_n(
        self,
        symbols: List[str],
        exchange: Any,
        n: int = 3
    ) -> List[CoinRank]:
        """
        獲取前 N 名幣種

        Args:
            symbols: 候選幣種列表
            exchange: CCXT 交易所實例
            n: 返回數量

        Returns:
            前 N 名的 CoinRank 列表
        """
        rankings = await self.get_rankings(symbols, exchange)
        return rankings[:n]

    async def get_best_coin(
        self,
        symbols: List[str],
        exchange: Any
    ) -> Optional[CoinRank]:
        """
        獲取最佳幣種

        Args:
            symbols: 候選幣種列表
            exchange: CCXT 交易所實例

        Returns:
            最佳幣種的 CoinRank，如果沒有合適的返回 None
        """
        rankings = await self.get_rankings(symbols, exchange)
        if rankings and rankings[0].action != ActionType.AVOID:
            return rankings[0]
        return None

    def get_rank_by_symbol(self, symbol: str) -> Optional[CoinRank]:
        """
        根據幣種名稱獲取排名

        Args:
            symbol: 交易對名稱

        Returns:
            對應的 CoinRank，如果不存在返回 None
        """
        for rank in self.last_rankings:
            if rank.symbol == symbol:
                return rank
        return None

    def get_history(
        self,
        symbol: str,
        hours: int = 24
    ) -> List[tuple]:
        """
        獲取幣種評分歷史

        Args:
            symbol: 交易對名稱
            hours: 獲取最近多少小時的歷史

        Returns:
            [(timestamp, CoinScore), ...] 列表
        """
        if symbol not in self.history:
            return []

        cutoff = datetime.now() - timedelta(hours=hours)
        return [
            (ts, score) for ts, score in self.history[symbol]
            if ts > cutoff
        ]

    # ========== 內部方法 ==========

    def _is_cache_valid(self) -> bool:
        """檢查快取是否有效"""
        if not self.last_update or not self.last_rankings:
            return False

        elapsed = (datetime.now() - self.last_update).total_seconds() / 60
        return elapsed < self.update_interval

    def _record_history(self, score: CoinScore):
        """記錄評分歷史"""
        symbol = score.symbol
        now = datetime.now()

        # 添加新記錄
        self.history[symbol].append((now, score))

        # 清理過期記錄
        cutoff = now - timedelta(hours=self.HISTORY_RETENTION_HOURS)
        self.history[symbol] = [
            (ts, s) for ts, s in self.history[symbol]
            if ts > cutoff
        ]

    def _calculate_trend(self, symbol: str) -> TrendType:
        """
        計算評分趨勢

        比較最近兩次評分的變化
        """
        history = self.history.get(symbol, [])
        if len(history) < 2:
            return TrendType.STABLE

        # 取最近兩筆
        recent_score = history[-1][1].final_score
        previous_score = history[-2][1].final_score
        diff = recent_score - previous_score

        if diff > self.TREND_UP_THRESHOLD:
            return TrendType.UP
        elif diff < self.TREND_DOWN_THRESHOLD:
            return TrendType.DOWN
        else:
            return TrendType.STABLE

    def _determine_action(
        self,
        score: CoinScore,
        trend: TrendType,
        rank: int
    ) -> ActionType:
        """
        決定建議動作

        考慮因素:
        - 絕對評分
        - 排名位置
        - 趨勢方向
        """
        final_score = score.final_score

        # 高分且排名靠前
        if final_score >= self.HOLD_MIN_SCORE and rank < 3:
            if trend == TrendType.DOWN:
                return ActionType.WATCH  # 高分但下降中，需關注
            return ActionType.HOLD

        # 中高分
        if final_score >= self.WATCH_MIN_SCORE:
            if trend == TrendType.UP and rank < 5:
                return ActionType.WATCH  # 上升中，值得關注
            return ActionType.MONITOR

        # 中等分數
        if final_score >= self.MONITOR_MIN_SCORE:
            return ActionType.MONITOR

        # 低分
        return ActionType.AVOID

    def _get_score_change_24h(self, symbol: str) -> float:
        """
        計算 24 小時評分變化

        Returns:
            評分變化值 (正數表示上升)
        """
        history = self.get_history(symbol, hours=24)
        if len(history) < 2:
            return 0.0

        oldest_score = history[0][1].final_score
        newest_score = history[-1][1].final_score
        return newest_score - oldest_score

    def clear_history(self, symbol: Optional[str] = None):
        """
        清除歷史記錄

        Args:
            symbol: 指定幣種，如果為 None 則清除全部
        """
        if symbol:
            self.history.pop(symbol, None)
        else:
            self.history.clear()
        self.last_rankings = []
        self.last_update = None


# ========== 輔助類 ==========

class RankingDisplay:
    """
    排名顯示輔助類

    提供格式化輸出功能
    """

    @staticmethod
    def format_table(rankings: List[CoinRank]) -> str:
        """
        格式化為表格字串

        Args:
            rankings: CoinRank 列表

        Returns:
            格式化的表格字串
        """
        if not rankings:
            return "暫無排名數據"

        lines = [
            "┌──────┬────────────┬────────┬────────┬────────┬────────┬────────┐",
            "│ 排名 │   幣種     │  總分  │ 波動率 │ 流動性 │ 均值回歸│  動作  │",
            "├──────┼────────────┼────────┼────────┼────────┼────────┼────────┤"
        ]

        for rank in rankings:
            s = rank.score
            line = (
                f"│ {rank.rank:2d} {rank.trend.value} │ {rank.symbol:10s} │"
                f" {s.final_score:5.1f}  │ {s.volatility_score:5.1f}  │"
                f" {s.liquidity_score:5.1f}  │ {s.mean_revert_score:5.1f}   │"
                f" {rank.action.value:6s} │"
            )
            lines.append(line)

        lines.append("└──────┴────────────┴────────┴────────┴────────┴────────┴────────┘")
        return "\n".join(lines)

    @staticmethod
    def format_summary(rankings: List[CoinRank]) -> str:
        """
        格式化摘要

        Args:
            rankings: CoinRank 列表

        Returns:
            摘要字串
        """
        if not rankings:
            return "暫無排名數據"

        lines = ["幣種評分排名:"]
        for rank in rankings[:5]:  # 只顯示前 5 名
            lines.append(f"  {rank}")

        return "\n".join(lines)


# ========== 快速函數 ==========

async def quick_rankings(
    symbols: List[str],
    exchange: Any,
    scorer: Optional[CoinScorer] = None
) -> List[CoinRank]:
    """
    快速獲取排名

    Args:
        symbols: 交易對列表
        exchange: CCXT 交易所實例
        scorer: CoinScorer 實例 (可選)

    Returns:
        CoinRank 列表
    """
    if scorer is None:
        scorer = CoinScorer()
    ranker = CoinRanker(scorer)
    return await ranker.get_rankings(symbols, exchange)
