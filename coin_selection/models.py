"""
選幣系統資料模型
================

定義評分、排名和輪動相關的資料結構
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any
from enum import Enum


class ActionType(Enum):
    """建議動作類型"""
    HOLD = "HOLD"       # 持有當前幣種
    WATCH = "WATCH"     # 關注，可能需要切換
    MONITOR = "MONITOR" # 監控中
    AVOID = "AVOID"     # 不建議交易
    SWITCH = "SWITCH"   # 建議切換


class TrendType(Enum):
    """趨勢類型"""
    UP = "↑"
    DOWN = "↓"
    STABLE = "→"


@dataclass
class CoinScore:
    """
    幣種評分資料結構

    Attributes:
        symbol: 交易對名稱 (e.g., "XRPUSDC")
        volatility_score: 波動率評分 (0-100)
        liquidity_score: 流動性評分 (0-100)
        mean_revert_score: 均值回歸評分 (0-100)
        momentum_score: 動量評分 (0-100)
        final_score: 加權總分 (0-100)
        timestamp: 評分時間

        # 評分細節
        atr_pct: ATR 佔價格百分比
        volume_24h: 24小時交易量 (USD)
        hurst_exponent: Hurst 指數 (0-1)
        adx: ADX 趨勢強度 (0-100)
    """
    symbol: str
    volatility_score: float = 0.0
    liquidity_score: float = 0.0
    mean_revert_score: float = 0.0
    momentum_score: float = 0.0
    final_score: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)

    # 評分細節指標
    atr_pct: float = 0.0
    volume_24h: float = 0.0
    hurst_exponent: float = 0.5
    adx: float = 25.0
    volume_cv: float = 1.0      # 交易量變異係數 (越低越穩定)
    adf_pvalue: float = 1.0     # ADF 測試 p-value (< 0.05 為平穩)

    def to_dict(self) -> Dict[str, Any]:
        """轉換為字典"""
        return {
            'symbol': self.symbol,
            'volatility_score': round(self.volatility_score, 2),
            'liquidity_score': round(self.liquidity_score, 2),
            'mean_revert_score': round(self.mean_revert_score, 2),
            'momentum_score': round(self.momentum_score, 2),
            'final_score': round(self.final_score, 2),
            'timestamp': self.timestamp.isoformat(),
            'details': {
                'atr_pct': round(self.atr_pct * 100, 3),  # 轉為百分比
                'volume_24h': self.volume_24h,
                'hurst_exponent': round(self.hurst_exponent, 3),
                'adx': round(self.adx, 2),
                'volume_cv': round(self.volume_cv, 3),
                'adf_pvalue': round(self.adf_pvalue, 4)
            }
        }

    def __str__(self) -> str:
        return (
            f"{self.symbol}: {self.final_score:.1f}分 "
            f"(波動:{self.volatility_score:.0f} 流動:{self.liquidity_score:.0f} "
            f"均回:{self.mean_revert_score:.0f} 動量:{self.momentum_score:.0f})"
        )


@dataclass
class CoinRank:
    """
    幣種排名資料結構

    Attributes:
        rank: 排名 (1 = 最佳)
        symbol: 交易對名稱
        score: 完整評分資料
        trend: 趨勢方向 (↑ ↓ →)
        action: 建議動作
        score_change_24h: 24小時評分變化
    """
    rank: int
    symbol: str
    score: CoinScore
    trend: TrendType = TrendType.STABLE
    action: ActionType = ActionType.MONITOR
    score_change_24h: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """轉換為字典"""
        return {
            'rank': self.rank,
            'symbol': self.symbol,
            'final_score': round(self.score.final_score, 2),
            'trend': self.trend.value,
            'action': self.action.value,
            'score_change_24h': round(self.score_change_24h, 2),
            'details': self.score.to_dict()
        }

    def __str__(self) -> str:
        return (
            f"#{self.rank} {self.trend.value} {self.symbol}: "
            f"{self.score.final_score:.1f}分 [{self.action.value}]"
        )


@dataclass
class RotationConfig:
    """
    輪動配置

    Attributes:
        score_threshold: 評分差異閾值 (超過此值才觸發輪動)
        min_cooldown_hours: 最小冷卻期 (小時)
        max_rotations_per_week: 每週最大輪動次數
        require_confirmation: 是否需要用戶確認
    """
    score_threshold: float = 15.0
    min_cooldown_hours: int = 24
    max_rotations_per_week: int = 2
    require_confirmation: bool = True

    def to_dict(self) -> Dict[str, Any]:
        """轉換為字典"""
        return {
            'score_threshold': self.score_threshold,
            'min_cooldown_hours': self.min_cooldown_hours,
            'max_rotations_per_week': self.max_rotations_per_week,
            'require_confirmation': self.require_confirmation
        }


@dataclass
class RotationSignal:
    """
    輪動信號

    Attributes:
        from_symbol: 原幣種
        to_symbol: 目標幣種
        score_diff: 評分差異
        reason: 輪動原因說明
        from_score: 原幣種評分
        to_score: 目標幣種評分
        estimated_slippage: 預估滑點
        timestamp: 信號產生時間
    """
    from_symbol: str
    to_symbol: str
    score_diff: float
    reason: str
    from_score: Optional[CoinScore] = None
    to_score: Optional[CoinScore] = None
    estimated_slippage: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        """轉換為字典"""
        return {
            'from_symbol': self.from_symbol,
            'to_symbol': self.to_symbol,
            'score_diff': round(self.score_diff, 2),
            'reason': self.reason,
            'from_score': self.from_score.final_score if self.from_score else None,
            'to_score': self.to_score.final_score if self.to_score else None,
            'estimated_slippage': self.estimated_slippage,
            'timestamp': self.timestamp.isoformat()
        }

    def __str__(self) -> str:
        return (
            f"輪動建議: {self.from_symbol} → {self.to_symbol} "
            f"(差異: {self.score_diff:.1f}分)\n"
            f"原因: {self.reason}"
        )


@dataclass
class RotationLog:
    """
    輪動歷史記錄

    Attributes:
        timestamp: 輪動時間
        from_symbol: 原幣種
        to_symbol: 目標幣種
        trigger_reason: 觸發原因
        score_before: 輪動前評分
        score_after: 輪動後評分
        pnl_impact: 損益影響
    """
    timestamp: datetime
    from_symbol: str
    to_symbol: str
    trigger_reason: str
    score_before: float
    score_after: float
    pnl_impact: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """轉換為字典"""
        return {
            'timestamp': self.timestamp.isoformat(),
            'from_symbol': self.from_symbol,
            'to_symbol': self.to_symbol,
            'trigger_reason': self.trigger_reason,
            'score_before': round(self.score_before, 2),
            'score_after': round(self.score_after, 2),
            'pnl_impact': round(self.pnl_impact, 4)
        }
