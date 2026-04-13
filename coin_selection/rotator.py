"""
CoinRotator - 智能輪動引擎
===========================

功能:
- 監控當前交易對評分變化
- 偵測是否需要輪動到更優幣種
- 管理冷卻期和輪動頻率限制
- 產生輪動信號供用戶確認

使用方式:
    scorer = CoinScorer()
    ranker = CoinRanker(scorer)
    rotator = CoinRotator(ranker)

    signal = await rotator.check_rotation("DOGEUSDC", exchange, candidate_symbols)
    if signal:
        print(f"建議輪動: {signal}")
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Callable

from .models import (
    CoinScore, CoinRank, RotationConfig, RotationSignal,
    ActionType, TrendType
)
from .ranker import CoinRanker

logger = logging.getLogger(__name__)


class CoinRotator:
    """
    智能輪動引擎

    負責偵測何時應該從當前交易對切換到更優質的幣種，
    並管理輪動頻率限制。
    """

    def __init__(
        self,
        ranker: CoinRanker,
        config: Optional[RotationConfig] = None,
        on_rotation_signal: Optional[Callable[[RotationSignal], None]] = None
    ):
        """
        初始化輪動引擎

        Args:
            ranker: CoinRanker 排名系統實例
            config: 輪動配置 (可選，使用預設值)
            on_rotation_signal: 輪動信號回調函數
        """
        self.ranker = ranker
        self.config = config or RotationConfig()
        self.on_rotation_signal = on_rotation_signal

        # 輪動狀態
        self.last_rotation_time: Optional[datetime] = None
        self.rotations_this_week: int = 0
        self.week_start: Optional[datetime] = None

        # 自動輪動
        self.auto_rotation_enabled: bool = False
        self._auto_check_task: Optional[asyncio.Task] = None
        self._check_interval_minutes: int = 15

        # 輪動拒絕記錄 (用於避免重複建議)
        self.rejected_signals: Dict[str, datetime] = {}
        self.rejection_cooldown_hours: int = 12

    # ========== 核心輪動檢查 ==========

    async def check_rotation(
        self,
        current_symbol: str,
        exchange: Any,
        candidate_symbols: List[str],
        force_check: bool = False
    ) -> Optional[RotationSignal]:
        """
        檢查是否需要輪動

        Args:
            current_symbol: 當前交易對
            exchange: CCXT 交易所實例
            candidate_symbols: 候選幣種列表
            force_check: 忽略冷卻期檢查

        Returns:
            RotationSignal 如果需要輪動，否則 None
        """
        logger.debug(f"檢查輪動: 當前 {current_symbol}")

        # 1. 冷卻期檢查
        if not force_check and not self._cooldown_passed():
            cooldown_remaining = self._get_cooldown_remaining()
            logger.debug(f"冷卻期內，剩餘 {cooldown_remaining:.1f} 小時")
            return None

        # 2. 週輪動次數檢查
        self._update_week_counter()
        if self.rotations_this_week >= self.config.max_rotations_per_week:
            logger.debug(f"本週已輪動 {self.rotations_this_week} 次，達到上限")
            return None

        # 3. 確保當前幣種在候選列表中
        if current_symbol not in candidate_symbols:
            candidate_symbols = [current_symbol] + candidate_symbols

        # 4. 獲取排名
        try:
            rankings = await self.ranker.get_rankings(
                candidate_symbols, exchange, force_refresh=True
            )
        except Exception as e:
            logger.error(f"獲取排名失敗: {e}")
            return None

        if not rankings:
            return None

        # 5. 找到當前幣種和最佳幣種
        current_rank = self._find_rank(rankings, current_symbol)
        top_rank = rankings[0]

        if not current_rank:
            logger.warning(f"找不到當前幣種 {current_symbol} 的排名")
            return None

        # 6. 自己就是最佳，無需輪動
        if top_rank.symbol == current_symbol:
            logger.debug(f"{current_symbol} 已是最佳幣種")
            return None

        # 7. 評分差異檢查
        score_diff = top_rank.score.final_score - current_rank.score.final_score
        if score_diff < self.config.score_threshold:
            logger.debug(
                f"評分差異 {score_diff:.1f} 未達閾值 {self.config.score_threshold}"
            )
            return None

        # 8. 檢查是否最近被拒絕過
        rejection_key = f"{current_symbol}→{top_rank.symbol}"
        if self._was_recently_rejected(rejection_key):
            logger.debug(f"輪動建議 {rejection_key} 最近被拒絕，跳過")
            return None

        # 9. 產生輪動信號
        signal = RotationSignal(
            from_symbol=current_symbol,
            to_symbol=top_rank.symbol,
            score_diff=score_diff,
            reason=self._generate_reason(current_rank, top_rank),
            from_score=current_rank.score,
            to_score=top_rank.score,
            estimated_slippage=self._estimate_slippage(
                current_rank.score, top_rank.score
            )
        )

        logger.info(f"產生輪動信號: {signal}")

        # 觸發回調
        if self.on_rotation_signal:
            self.on_rotation_signal(signal)

        return signal

    def record_rotation(self, signal: RotationSignal):
        """
        記錄輪動執行

        在用戶確認並執行輪動後調用
        """
        self.last_rotation_time = datetime.now()
        self.rotations_this_week += 1
        logger.info(
            f"輪動已執行: {signal.from_symbol} → {signal.to_symbol}, "
            f"本週第 {self.rotations_this_week} 次"
        )

    def record_rejection(self, signal: RotationSignal):
        """
        記錄輪動被拒絕

        避免短時間內重複建議相同的輪動
        """
        rejection_key = f"{signal.from_symbol}→{signal.to_symbol}"
        self.rejected_signals[rejection_key] = datetime.now()
        logger.info(f"輪動被拒絕: {rejection_key}")

    # ========== 自動輪動 ==========

    def enable_auto_rotation(
        self,
        current_symbol: str,
        exchange: Any,
        candidate_symbols: List[str],
        check_interval_minutes: int = 15
    ):
        """
        啟用自動輪動檢查

        Args:
            current_symbol: 當前交易對
            exchange: CCXT 交易所實例
            candidate_symbols: 候選幣種列表
            check_interval_minutes: 檢查間隔 (分鐘)
        """
        if self.auto_rotation_enabled:
            logger.warning("自動輪動已啟用")
            return

        self.auto_rotation_enabled = True
        self._check_interval_minutes = check_interval_minutes

        self._auto_check_task = asyncio.create_task(
            self._auto_check_loop(current_symbol, exchange, candidate_symbols)
        )
        logger.info(f"自動輪動已啟用，檢查間隔: {check_interval_minutes} 分鐘")

    def disable_auto_rotation(self):
        """停用自動輪動檢查"""
        self.auto_rotation_enabled = False
        if self._auto_check_task:
            self._auto_check_task.cancel()
            self._auto_check_task = None
        logger.info("自動輪動已停用")

    async def _auto_check_loop(
        self,
        current_symbol: str,
        exchange: Any,
        candidate_symbols: List[str]
    ):
        """自動輪動檢查循環"""
        while self.auto_rotation_enabled:
            try:
                signal = await self.check_rotation(
                    current_symbol, exchange, candidate_symbols
                )
                if signal:
                    logger.info(f"自動輪動偵測到信號: {signal}")
                    # 信號已通過 on_rotation_signal 回調傳遞

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"自動輪動檢查錯誤: {e}")

            await asyncio.sleep(self._check_interval_minutes * 60)

    # ========== 狀態查詢 ==========

    def get_status(self) -> Dict[str, Any]:
        """
        獲取輪動狀態

        Returns:
            狀態字典
        """
        self._update_week_counter()

        return {
            'auto_rotation_enabled': self.auto_rotation_enabled,
            'last_rotation_time': (
                self.last_rotation_time.isoformat()
                if self.last_rotation_time else None
            ),
            'rotations_this_week': self.rotations_this_week,
            'max_rotations_per_week': self.config.max_rotations_per_week,
            'cooldown_passed': self._cooldown_passed(),
            'cooldown_remaining_hours': self._get_cooldown_remaining(),
            'config': self.config.to_dict()
        }

    def can_rotate(self) -> bool:
        """
        檢查是否可以執行輪動

        Returns:
            True 如果冷卻期已過且未達週上限
        """
        self._update_week_counter()
        return (
            self._cooldown_passed() and
            self.rotations_this_week < self.config.max_rotations_per_week
        )

    # ========== 配置 ==========

    def update_config(self, **kwargs):
        """
        更新輪動配置

        Args:
            score_threshold: 評分差異閾值
            min_cooldown_hours: 最小冷卻期
            max_rotations_per_week: 每週最大輪動次數
            require_confirmation: 是否需要確認
        """
        if 'score_threshold' in kwargs:
            self.config.score_threshold = kwargs['score_threshold']
        if 'min_cooldown_hours' in kwargs:
            self.config.min_cooldown_hours = kwargs['min_cooldown_hours']
        if 'max_rotations_per_week' in kwargs:
            self.config.max_rotations_per_week = kwargs['max_rotations_per_week']
        if 'require_confirmation' in kwargs:
            self.config.require_confirmation = kwargs['require_confirmation']

        logger.info(f"輪動配置已更新: {self.config.to_dict()}")

    # ========== 內部方法 ==========

    def _cooldown_passed(self) -> bool:
        """檢查冷卻期是否已過"""
        if not self.last_rotation_time:
            return True

        elapsed = datetime.now() - self.last_rotation_time
        cooldown = timedelta(hours=self.config.min_cooldown_hours)
        return elapsed >= cooldown

    def _get_cooldown_remaining(self) -> float:
        """獲取剩餘冷卻時間 (小時)"""
        if not self.last_rotation_time:
            return 0.0

        elapsed = datetime.now() - self.last_rotation_time
        cooldown = timedelta(hours=self.config.min_cooldown_hours)
        remaining = cooldown - elapsed

        if remaining.total_seconds() <= 0:
            return 0.0
        return remaining.total_seconds() / 3600

    def _update_week_counter(self):
        """更新週計數器"""
        now = datetime.now()

        # 計算本週一
        days_since_monday = now.weekday()
        week_start = now - timedelta(days=days_since_monday)
        week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)

        # 如果是新的一週，重置計數器
        if self.week_start is None or week_start > self.week_start:
            self.week_start = week_start
            self.rotations_this_week = 0
            logger.debug(f"新的一週開始，重置輪動計數器")

    def _was_recently_rejected(self, rejection_key: str) -> bool:
        """檢查輪動建議是否最近被拒絕過"""
        if rejection_key not in self.rejected_signals:
            return False

        rejected_at = self.rejected_signals[rejection_key]
        elapsed = datetime.now() - rejected_at
        cooldown = timedelta(hours=self.rejection_cooldown_hours)
        return elapsed < cooldown

    def _find_rank(
        self,
        rankings: List[CoinRank],
        symbol: str
    ) -> Optional[CoinRank]:
        """在排名列表中找到指定幣種"""
        for rank in rankings:
            if rank.symbol == symbol:
                return rank
        return None

    def _generate_reason(
        self,
        current: CoinRank,
        target: CoinRank
    ) -> str:
        """
        產生輪動原因說明

        分析兩個幣種的評分差異，產生人類可讀的說明
        """
        reasons = []
        current_score = current.score
        target_score = target.score

        # 比較各維度
        if target_score.mean_revert_score - current_score.mean_revert_score > 10:
            reasons.append(
                f"{target.symbol} 均值回歸性更強 "
                f"(H={target_score.hurst_exponent:.2f} vs {current_score.hurst_exponent:.2f})"
            )

        if target_score.volatility_score - current_score.volatility_score > 10:
            reasons.append(
                f"{target.symbol} 波動率更適合網格 "
                f"(ATR={target_score.atr_pct*100:.1f}% vs {current_score.atr_pct*100:.1f}%)"
            )

        if target_score.liquidity_score - current_score.liquidity_score > 10:
            reasons.append(f"{target.symbol} 流動性更好")

        if target_score.momentum_score - current_score.momentum_score > 10:
            reasons.append(f"{target.symbol} 區間震盪特性更明顯")

        # 趨勢分析
        if current.trend == TrendType.DOWN:
            reasons.append(f"{current.symbol} 評分持續下降")

        if target.trend == TrendType.UP:
            reasons.append(f"{target.symbol} 評分持續上升")

        if not reasons:
            reasons.append(
                f"{target.symbol} 綜合評分 ({target_score.final_score:.1f}) "
                f"優於 {current.symbol} ({current_score.final_score:.1f})"
            )

        return "；".join(reasons)

    def _estimate_slippage(
        self,
        from_score: CoinScore,
        to_score: CoinScore
    ) -> float:
        """
        估算輪動滑點

        基於兩個幣種的流動性和波動率估算
        """
        # 基礎滑點 0.05%
        base_slippage = 0.0005

        # 流動性調整 (流動性差 = 滑點高)
        avg_liquidity = (from_score.liquidity_score + to_score.liquidity_score) / 2
        if avg_liquidity < 70:
            base_slippage *= 1.5
        elif avg_liquidity < 50:
            base_slippage *= 2.0

        # 波動率調整 (高波動 = 滑點可能更大)
        avg_volatility = (from_score.atr_pct + to_score.atr_pct) / 2
        if avg_volatility > 0.05:
            base_slippage *= 1.2

        return round(base_slippage, 4)

    def reset(self):
        """重置輪動狀態"""
        self.last_rotation_time = None
        self.rotations_this_week = 0
        self.week_start = None
        self.rejected_signals.clear()
        self.disable_auto_rotation()
        logger.info("輪動狀態已重置")


# ========== 快速函數 ==========

async def quick_rotation_check(
    current_symbol: str,
    candidate_symbols: List[str],
    exchange: Any,
    config: Optional[RotationConfig] = None
) -> Optional[RotationSignal]:
    """
    快速輪動檢查

    Args:
        current_symbol: 當前交易對
        candidate_symbols: 候選幣種列表
        exchange: CCXT 交易所實例
        config: 輪動配置

    Returns:
        RotationSignal 如果需要輪動，否則 None
    """
    from .scorer import CoinScorer

    scorer = CoinScorer()
    ranker = CoinRanker(scorer)
    rotator = CoinRotator(ranker, config)

    return await rotator.check_rotation(
        current_symbol, exchange, candidate_symbols, force_check=True
    )
