"""
MAX 增強模組
- MaxEnhancement
- UCB Bandit 參數優化器
- DGT 動態網格邊界管理
- Funding Rate 管理
- GLFT 庫存控制
- 動態網格管理
- 領先指標系統
"""

import time
from dataclasses import dataclass
from typing import Optional, List, Dict, Tuple
from collections import deque

import numpy as np

from .utils import logger


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                              MAX 增強模組                                      ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@dataclass
class MaxEnhancement:
    """MAX 版本增強功能配置"""
    # === 主開關 ===
    all_enhancements_enabled: bool = False

    # === Funding Rate 偏向 ===
    funding_rate_enabled: bool = False
    funding_rate_threshold: float = 0.0001
    funding_rate_position_bias: float = 0.2

    # === GLFT γ 風險係數 ===
    glft_enabled: bool = False
    gamma: float = 0.1
    inventory_target: float = 0.5

    # === 動態網格範圍 (ATR - 滯後指標) ===
    dynamic_grid_enabled: bool = False
    atr_period: int = 14
    atr_multiplier: float = 1.5
    min_spacing: float = 0.002
    max_spacing: float = 0.015
    volatility_lookback: int = 100

    def to_dict(self) -> dict:
        return {
            "all_enhancements_enabled": self.all_enhancements_enabled,
            "funding_rate_enabled": self.funding_rate_enabled,
            "funding_rate_threshold": self.funding_rate_threshold,
            "funding_rate_position_bias": self.funding_rate_position_bias,
            "glft_enabled": self.glft_enabled,
            "gamma": self.gamma,
            "inventory_target": self.inventory_target,
            "dynamic_grid_enabled": self.dynamic_grid_enabled,
            "atr_period": self.atr_period,
            "atr_multiplier": self.atr_multiplier,
            "min_spacing": self.min_spacing,
            "max_spacing": self.max_spacing,
            "volatility_lookback": self.volatility_lookback
        }

    def is_feature_enabled(self, feature: str) -> bool:
        """檢查功能是否啟用 (考慮總開關)"""
        if not self.all_enhancements_enabled:
            return False
        return getattr(self, f"{feature}_enabled", False)

    @classmethod
    def from_dict(cls, data: dict) -> 'MaxEnhancement':
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                         UCB Bandit 參數優化器                                  ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@dataclass
class BanditConfig:
    """Bandit 優化器配置 (增強版)"""
    enabled: bool = True
    window_size: int = 50
    exploration_factor: float = 1.5
    min_pulls_per_arm: int = 3
    update_interval: int = 10

    # === 冷啟動配置 ===
    cold_start_enabled: bool = True
    cold_start_arm_idx: int = 4

    # === Contextual Bandit ===
    contextual_enabled: bool = True
    volatility_lookback: int = 20
    trend_lookback: int = 50
    high_volatility_threshold: float = 0.02
    trend_threshold: float = 0.01

    # === Thompson Sampling ===
    thompson_enabled: bool = True
    thompson_prior_alpha: float = 1.0
    thompson_prior_beta: float = 1.0
    param_perturbation: float = 0.1

    # === Reward 改進 ===
    mdd_penalty_weight: float = 0.5
    win_rate_bonus: float = 0.2

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "window_size": self.window_size,
            "exploration_factor": self.exploration_factor,
            "min_pulls_per_arm": self.min_pulls_per_arm,
            "update_interval": self.update_interval,
            "cold_start_enabled": self.cold_start_enabled,
            "cold_start_arm_idx": self.cold_start_arm_idx,
            "contextual_enabled": self.contextual_enabled,
            "volatility_lookback": self.volatility_lookback,
            "trend_lookback": self.trend_lookback,
            "high_volatility_threshold": self.high_volatility_threshold,
            "trend_threshold": self.trend_threshold,
            "thompson_enabled": self.thompson_enabled,
            "thompson_prior_alpha": self.thompson_prior_alpha,
            "thompson_prior_beta": self.thompson_prior_beta,
            "param_perturbation": self.param_perturbation,
            "mdd_penalty_weight": self.mdd_penalty_weight,
            "win_rate_bonus": self.win_rate_bonus
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'BanditConfig':
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class MarketContext:
    """市場狀態分類"""
    RANGING = "ranging"
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    HIGH_VOLATILITY = "high_vol"

    RECOMMENDED_ARMS = {
        RANGING: [0, 1, 2, 3],
        TRENDING_UP: [4, 5],
        TRENDING_DOWN: [4, 5],
        HIGH_VOLATILITY: [6, 7, 8, 9]
    }


@dataclass
class ParameterArm:
    """參數組合 (一個 Arm)"""
    gamma: float
    grid_spacing: float
    take_profit_spacing: float

    def __hash__(self):
        return hash((self.gamma, self.grid_spacing, self.take_profit_spacing))

    def __str__(self):
        return f"γ={self.gamma:.2f}/GS={self.grid_spacing*100:.1f}%/TP={self.take_profit_spacing*100:.1f}%"


class UCBBanditOptimizer:
    """UCB Bandit 參數優化器 (增強版)"""

    DEFAULT_ARMS = [
        ParameterArm(gamma=0.05, grid_spacing=0.003, take_profit_spacing=0.003),
        ParameterArm(gamma=0.05, grid_spacing=0.004, take_profit_spacing=0.004),
        ParameterArm(gamma=0.08, grid_spacing=0.005, take_profit_spacing=0.003),
        ParameterArm(gamma=0.08, grid_spacing=0.006, take_profit_spacing=0.004),
        ParameterArm(gamma=0.10, grid_spacing=0.006, take_profit_spacing=0.004),
        ParameterArm(gamma=0.10, grid_spacing=0.008, take_profit_spacing=0.005),
        ParameterArm(gamma=0.12, grid_spacing=0.008, take_profit_spacing=0.006),
        ParameterArm(gamma=0.12, grid_spacing=0.010, take_profit_spacing=0.006),
        ParameterArm(gamma=0.15, grid_spacing=0.010, take_profit_spacing=0.008),
        ParameterArm(gamma=0.15, grid_spacing=0.012, take_profit_spacing=0.008),
    ]

    def __init__(self, config: BanditConfig = None):
        self.config = config or BanditConfig()
        self.arms = self.DEFAULT_ARMS.copy()

        self.rewards: Dict[int, deque] = {
            i: deque(maxlen=self.config.window_size)
            for i in range(len(self.arms))
        }

        self.current_arm_idx: int = 0
        self.total_pulls: int = 0
        self.pull_counts: Dict[int, int] = {i: 0 for i in range(len(self.arms))}

        self.pending_trades: List[Dict] = []
        self.trade_count_since_update: int = 0

        self.best_arm_history: List[int] = []
        self.cumulative_reward: float = 0

        self.current_context: str = MarketContext.RANGING
        self.price_history: deque = deque(maxlen=100)

        self.context_rewards: Dict[str, Dict[int, deque]] = {
            ctx: {i: deque(maxlen=self.config.window_size) for i in range(len(self.arms))}
            for ctx in [MarketContext.RANGING, MarketContext.TRENDING_UP,
                       MarketContext.TRENDING_DOWN, MarketContext.HIGH_VOLATILITY]
        }
        self.context_pulls: Dict[str, Dict[int, int]] = {
            ctx: {i: 0 for i in range(len(self.arms))}
            for ctx in [MarketContext.RANGING, MarketContext.TRENDING_UP,
                       MarketContext.TRENDING_DOWN, MarketContext.HIGH_VOLATILITY]
        }

        self.thompson_alpha: Dict[int, float] = {
            i: self.config.thompson_prior_alpha for i in range(len(self.arms))
        }
        self.thompson_beta: Dict[int, float] = {
            i: self.config.thompson_prior_beta for i in range(len(self.arms))
        }

        self.dynamic_arm: Optional[ParameterArm] = None
        self.dynamic_arm_reward: float = 0

        if self.config.cold_start_enabled:
            self._cold_start_init()

        logger.info(f"[Bandit] 增強版初始化完成，共 {len(self.arms)} 個參數組合")
        logger.info(f"[Bandit] 功能: 冷啟動={self.config.cold_start_enabled}, "
                   f"Contextual={self.config.contextual_enabled}, "
                   f"Thompson={self.config.thompson_enabled}")

    def _cold_start_init(self):
        """冷啟動初始化"""
        self.current_arm_idx = self.config.cold_start_arm_idx

        recommended_arms = [4, 5]
        for arm_idx in recommended_arms:
            self.rewards[arm_idx].append(0.5)
            self.pull_counts[arm_idx] = 1
            self.total_pulls += 1

        logger.info(f"[Bandit] 冷啟動: 初始 arm={self.current_arm_idx}, "
                   f"預載 arms={recommended_arms}")

    def update_price(self, price: float):
        """更新價格歷史"""
        self.price_history.append(price)

    def detect_market_context(self) -> str:
        """檢測當前市場狀態"""
        if not self.config.contextual_enabled:
            return MarketContext.RANGING

        if len(self.price_history) < self.config.volatility_lookback:
            return self.current_context

        prices = list(self.price_history)

        recent_prices = prices[-self.config.volatility_lookback:]
        volatility = np.std(recent_prices) / np.mean(recent_prices)

        if volatility > self.config.high_volatility_threshold:
            self.current_context = MarketContext.HIGH_VOLATILITY
            return self.current_context

        if len(prices) >= self.config.trend_lookback:
            trend_prices = prices[-self.config.trend_lookback:]
            x = np.arange(len(trend_prices))
            slope = np.polyfit(x, trend_prices, 1)[0]
            trend_pct = slope / np.mean(trend_prices)

            if trend_pct > self.config.trend_threshold:
                self.current_context = MarketContext.TRENDING_UP
            elif trend_pct < -self.config.trend_threshold:
                self.current_context = MarketContext.TRENDING_DOWN
            else:
                self.current_context = MarketContext.RANGING
        else:
            self.current_context = MarketContext.RANGING

        return self.current_context

    def _thompson_sample(self) -> int:
        """Thompson Sampling 選擇 arm"""
        samples = []
        for i in range(len(self.arms)):
            sample = np.random.beta(self.thompson_alpha[i], self.thompson_beta[i])
            samples.append(sample)

        return int(np.argmax(samples))

    def _generate_dynamic_arm(self) -> Optional[ParameterArm]:
        """基於最佳 arm 生成動態參數組合"""
        if not self.config.thompson_enabled:
            return None

        best_idx = self._get_best_arm()
        best_arm = self.arms[best_idx]

        perturbation = self.config.param_perturbation

        gamma_delta = np.random.uniform(-perturbation, perturbation) * best_arm.gamma
        gs_delta = np.random.uniform(-perturbation, perturbation) * best_arm.grid_spacing
        tp_delta = np.random.uniform(-perturbation, perturbation) * best_arm.take_profit_spacing

        new_gamma = max(0.01, min(0.3, best_arm.gamma + gamma_delta))
        new_gs = max(0.002, min(0.02, best_arm.grid_spacing + gs_delta))
        new_tp = max(0.002, min(0.015, best_arm.take_profit_spacing + tp_delta))

        if new_tp >= new_gs:
            new_tp = new_gs * 0.7

        return ParameterArm(gamma=new_gamma, grid_spacing=new_gs, take_profit_spacing=new_tp)

    def _calculate_reward(self, pnls: List[float]) -> float:
        """計算改進的 Reward"""
        if not pnls:
            return 0

        mean_pnl = np.mean(pnls)
        std_pnl = np.std(pnls) if np.std(pnls) > 0 else 0.001
        sharpe = mean_pnl / std_pnl

        cumsum = np.cumsum(pnls)
        running_max = np.maximum.accumulate(cumsum)
        drawdowns = running_max - cumsum
        max_drawdown = np.max(drawdowns) if len(drawdowns) > 0 else 0

        total_pnl = sum(pnls)
        mdd_ratio = max_drawdown / abs(total_pnl) if total_pnl != 0 else 0
        mdd_penalty = self.config.mdd_penalty_weight * mdd_ratio

        win_rate = len([p for p in pnls if p > 0]) / len(pnls) if pnls else 0
        win_bonus = self.config.win_rate_bonus * (win_rate - 0.5)

        reward = sharpe - mdd_penalty + win_bonus

        return reward

    def _update_thompson(self, reward: float):
        """更新 Thompson Sampling 的 Beta 分布參數"""
        arm_idx = self.current_arm_idx

        prob_success = 1 / (1 + np.exp(-reward))

        self.thompson_alpha[arm_idx] += prob_success
        self.thompson_beta[arm_idx] += (1 - prob_success)

    def get_current_params(self) -> ParameterArm:
        """獲取當前選擇的參數"""
        if self.dynamic_arm and self.dynamic_arm_reward > 0:
            return self.dynamic_arm
        return self.arms[self.current_arm_idx]

    def select_arm(self) -> int:
        """選擇 arm (融合 UCB + Contextual + Thompson)"""
        for i in range(len(self.arms)):
            if self.pull_counts[i] < self.config.min_pulls_per_arm:
                return i

        if self.config.contextual_enabled:
            context = self.detect_market_context()
            recommended = MarketContext.RECOMMENDED_ARMS.get(context, list(range(len(self.arms))))
        else:
            recommended = list(range(len(self.arms)))

        if self.config.thompson_enabled and np.random.random() < 0.3:
            thompson_choice = self._thompson_sample()
            if thompson_choice in recommended:
                return thompson_choice

        ucb_values = []
        for i in range(len(self.arms)):
            if i not in recommended:
                ucb_values.append(float('-inf'))
                continue

            rewards = list(self.rewards[i])
            if not rewards:
                ucb_values.append(float('inf'))
                continue

            mean_reward = np.mean(rewards)
            confidence = self.config.exploration_factor * np.sqrt(
                2 * np.log(self.total_pulls + 1) / len(rewards)
            )
            ucb_values.append(mean_reward + confidence)

        return int(np.argmax(ucb_values))

    def record_trade(self, pnl: float, side: str):
        """記錄交易結果"""
        if not self.config.enabled:
            return

        self.pending_trades.append({
            'pnl': pnl,
            'side': side,
            'arm_idx': self.current_arm_idx,
            'context': self.current_context,
            'timestamp': time.time()
        })
        self.trade_count_since_update += 1

        if self.trade_count_since_update >= self.config.update_interval:
            self._update_and_select()

    def _update_and_select(self):
        """更新獎勵並選擇新的 arm"""
        if not self.pending_trades:
            return

        pnls = [t['pnl'] for t in self.pending_trades]

        reward = self._calculate_reward(pnls)

        arm_idx = self.current_arm_idx
        self.rewards[arm_idx].append(reward)
        self.pull_counts[arm_idx] += 1
        self.total_pulls += 1
        self.cumulative_reward += sum(pnls)

        if self.config.contextual_enabled:
            context = self.pending_trades[0].get('context', MarketContext.RANGING)
            self.context_rewards[context][arm_idx].append(reward)
            self.context_pulls[context][arm_idx] += 1

        if self.config.thompson_enabled:
            self._update_thompson(reward)

        new_arm_idx = self.select_arm()
        if new_arm_idx != self.current_arm_idx:
            old_params = self.arms[self.current_arm_idx]
            new_params = self.arms[new_arm_idx]
            logger.info(f"[Bandit] 切換參數: {old_params} → {new_params} "
                       f"(context={self.current_context})")
            self.current_arm_idx = new_arm_idx

        if self.config.thompson_enabled and np.random.random() < 0.1:
            self.dynamic_arm = self._generate_dynamic_arm()
            if self.dynamic_arm:
                logger.info(f"[Bandit] 動態探索: {self.dynamic_arm}")

        self.best_arm_history.append(self._get_best_arm())

        self.pending_trades = []
        self.trade_count_since_update = 0

    def _get_best_arm(self) -> int:
        """獲取目前表現最好的 arm"""
        best_idx = 0
        best_mean = float('-inf')

        for i in range(len(self.arms)):
            rewards = list(self.rewards[i])
            if rewards:
                mean = np.mean(rewards)
                if mean > best_mean:
                    best_mean = mean
                    best_idx = i

        return best_idx

    def get_stats(self) -> Dict:
        """獲取優化器統計"""
        best_idx = self._get_best_arm()
        arm_stats = []

        for i in range(len(self.arms)):
            rewards = list(self.rewards[i])
            arm_stats.append({
                'arm': str(self.arms[i]),
                'pulls': self.pull_counts[i],
                'mean_reward': np.mean(rewards) if rewards else 0,
                'is_current': i == self.current_arm_idx,
                'is_best': i == best_idx,
                'thompson_alpha': self.thompson_alpha[i],
                'thompson_beta': self.thompson_beta[i]
            })

        return {
            'enabled': self.config.enabled,
            'total_pulls': self.total_pulls,
            'current_arm': str(self.arms[self.current_arm_idx]),
            'best_arm': str(self.arms[best_idx]),
            'cumulative_reward': self.cumulative_reward,
            'current_context': self.current_context,
            'dynamic_arm': str(self.dynamic_arm) if self.dynamic_arm else None,
            'arm_stats': arm_stats
        }

    def to_dict(self) -> dict:
        """序列化狀態"""
        return {
            'current_arm_idx': self.current_arm_idx,
            'total_pulls': self.total_pulls,
            'pull_counts': dict(self.pull_counts),
            'rewards': {k: list(v) for k, v in self.rewards.items()},
            'cumulative_reward': self.cumulative_reward,
            'current_context': self.current_context,
            'thompson_alpha': dict(self.thompson_alpha),
            'thompson_beta': dict(self.thompson_beta),
            'context_pulls': {ctx: dict(pulls) for ctx, pulls in self.context_pulls.items()}
        }

    def load_state(self, state: dict):
        """載入狀態"""
        if not state:
            return
        self.current_arm_idx = state.get('current_arm_idx', 0)
        self.total_pulls = state.get('total_pulls', 0)
        self.pull_counts = {int(k): v for k, v in state.get('pull_counts', {}).items()}
        self.cumulative_reward = state.get('cumulative_reward', 0)
        self.current_context = state.get('current_context', MarketContext.RANGING)

        saved_rewards = state.get('rewards', {})
        for k, v in saved_rewards.items():
            idx = int(k)
            if idx in self.rewards:
                self.rewards[idx] = deque(v, maxlen=self.config.window_size)

        saved_alpha = state.get('thompson_alpha', {})
        for k, v in saved_alpha.items():
            self.thompson_alpha[int(k)] = v

        saved_beta = state.get('thompson_beta', {})
        for k, v in saved_beta.items():
            self.thompson_beta[int(k)] = v

        saved_context_pulls = state.get('context_pulls', {})
        for ctx, pulls in saved_context_pulls.items():
            if ctx in self.context_pulls:
                self.context_pulls[ctx] = {int(k): v for k, v in pulls.items()}


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                         DGT 動態網格邊界管理                                   ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@dataclass
class DGTConfig:
    """DGT (Dynamic Grid Trading) 配置"""
    enabled: bool = False
    reset_threshold: float = 0.05
    profit_reinvest_ratio: float = 0.5
    boundary_buffer: float = 0.02

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "reset_threshold": self.reset_threshold,
            "profit_reinvest_ratio": self.profit_reinvest_ratio,
            "boundary_buffer": self.boundary_buffer
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'DGTConfig':
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class DGTBoundaryManager:
    """DGT 動態邊界管理器"""

    def __init__(self, config: DGTConfig = None):
        self.config = config or DGTConfig()
        self.boundaries: Dict[str, Dict] = {}
        self.accumulated_profits: Dict[str, float] = {}
        self.reset_counts: Dict[str, int] = {}

    def initialize_boundary(self, symbol: str, center_price: float, grid_spacing: float, num_grids: int = 10):
        """初始化網格邊界"""
        half_grids = num_grids // 2

        upper = center_price * ((1 + grid_spacing) ** half_grids)
        lower = center_price * ((1 - grid_spacing) ** half_grids)

        self.boundaries[symbol] = {
            'center': center_price,
            'upper': upper,
            'lower': lower,
            'grid_spacing': grid_spacing,
            'num_grids': num_grids,
            'initialized_at': time.time(),
            'last_reset': time.time()
        }

        self.accumulated_profits[symbol] = 0
        self.reset_counts[symbol] = 0

        logger.info(f"[DGT] {symbol} 邊界初始化: {lower:.4f} ~ {upper:.4f} (中心: {center_price:.4f})")

    def check_and_reset(self, symbol: str, current_price: float, realized_pnl: float = 0) -> Tuple[bool, Optional[Dict]]:
        """檢查是否需要重置邊界"""
        if not self.config.enabled:
            return False, None

        if symbol not in self.boundaries:
            return False, None

        boundary = self.boundaries[symbol]
        upper = boundary['upper']
        lower = boundary['lower']

        breach_upper = current_price >= upper * (1 - self.config.boundary_buffer)
        breach_lower = current_price <= lower * (1 + self.config.boundary_buffer)

        if not (breach_upper or breach_lower):
            return False, None

        self.accumulated_profits[symbol] += realized_pnl

        old_center = boundary['center']
        new_center = current_price

        if breach_upper:
            reinvest = self.accumulated_profits[symbol] * self.config.profit_reinvest_ratio
            logger.info(f"[DGT] {symbol} 上破重置: {old_center:.4f} → {new_center:.4f}, 再投資: {reinvest:.2f}")
        else:
            reinvest = self.accumulated_profits[symbol]
            logger.info(f"[DGT] {symbol} 下破重置: {old_center:.4f} → {new_center:.4f}, 累積利潤: {reinvest:.2f}")

        self.initialize_boundary(
            symbol,
            new_center,
            boundary['grid_spacing'],
            boundary['num_grids']
        )

        self.reset_counts[symbol] += 1
        self.accumulated_profits[symbol] = 0

        return True, {
            'old_center': old_center,
            'new_center': new_center,
            'direction': 'upper' if breach_upper else 'lower',
            'reinvest_amount': reinvest,
            'reset_count': self.reset_counts[symbol]
        }

    def get_adjusted_spacing(self, symbol: str, base_spacing: float) -> float:
        """根據距離邊界的位置調整間距"""
        if symbol not in self.boundaries:
            return base_spacing
        return base_spacing

    def get_boundary_info(self, symbol: str) -> Optional[Dict]:
        """獲取邊界資訊"""
        if symbol not in self.boundaries:
            return None

        boundary = self.boundaries[symbol]
        return {
            'center': boundary['center'],
            'upper': boundary['upper'],
            'lower': boundary['lower'],
            'reset_count': self.reset_counts.get(symbol, 0),
            'accumulated_profit': self.accumulated_profits.get(symbol, 0)
        }

    def get_stats(self) -> Dict:
        """獲取統計"""
        return {
            'enabled': self.config.enabled,
            'symbols': list(self.boundaries.keys()),
            'total_resets': sum(self.reset_counts.values()),
            'total_accumulated_profit': sum(self.accumulated_profits.values()),
            'boundaries': {
                symbol: self.get_boundary_info(symbol)
                for symbol in self.boundaries
            }
        }


class FundingRateManager:
    """Funding Rate 管理器"""

    def __init__(self, exchange):
        self.exchange = exchange
        self.funding_rates: Dict[str, float] = {}
        self.last_update: Dict[str, float] = {}
        self.update_interval = 60

    def update_funding_rate(self, symbol: str) -> float:
        """更新並返回 funding rate"""
        now = time.time()

        if symbol in self.last_update:
            if now - self.last_update[symbol] < self.update_interval:
                return self.funding_rates.get(symbol, 0)

        try:
            funding_info = self.exchange.fetch_funding_rate(symbol)
            rate = float(funding_info.get('fundingRate', 0) or 0)

            self.funding_rates[symbol] = rate
            self.last_update[symbol] = now

            logger.info(f"[Funding] {symbol} funding rate: {rate*100:.4f}%")
            return rate

        except Exception as e:
            logger.error(f"[Funding] 獲取 {symbol} funding rate 失敗: {e}")
            return self.funding_rates.get(symbol, 0)

    def get_position_bias(self, symbol: str, config: 'MaxEnhancement') -> Tuple[float, float]:
        """根據 funding rate 計算持倉偏向 → (long_bias, short_bias)"""
        if not config.is_feature_enabled('funding_rate'):
            return 1.0, 1.0

        rate = self.funding_rates.get(symbol, 0)

        if abs(rate) < config.funding_rate_threshold:
            return 1.0, 1.0

        bias = config.funding_rate_position_bias

        if rate > 0:
            long_bias = 1.0 - bias
            short_bias = 1.0 + bias
        else:
            long_bias = 1.0 + bias
            short_bias = 1.0 - bias

        return long_bias, short_bias


class GLFTController:
    """GLFT (Guéant-Lehalle-Fernandez-Tapia) 庫存控制器"""

    def calculate_inventory_ratio(self, long_pos: float, short_pos: float) -> float:
        """計算庫存比例 (-1.0 到 1.0)"""
        total = long_pos + short_pos
        if total <= 0:
            return 0.0
        return (long_pos - short_pos) / total

    def calculate_spread_skew(
        self,
        long_pos: float,
        short_pos: float,
        base_spread: float,
        config: 'MaxEnhancement'
    ) -> Tuple[float, float]:
        """計算報價偏移 → (bid_skew, ask_skew)"""
        if not config.is_feature_enabled('glft'):
            return 0.0, 0.0

        inventory_ratio = self.calculate_inventory_ratio(long_pos, short_pos)
        skew = inventory_ratio * base_spread * config.gamma

        bid_skew = -skew
        ask_skew = skew

        return bid_skew, ask_skew

    def adjust_order_quantity(
        self,
        base_qty: float,
        side: str,
        long_pos: float,
        short_pos: float,
        config: 'MaxEnhancement'
    ) -> float:
        """根據庫存調整訂單數量"""
        if not config.is_feature_enabled('glft'):
            return base_qty

        inventory_ratio = self.calculate_inventory_ratio(long_pos, short_pos)

        if side == 'long':
            adjust = 1.0 - inventory_ratio * config.gamma
        else:
            adjust = 1.0 + inventory_ratio * config.gamma

        adjust = max(0.5, min(1.5, adjust))

        return base_qty * adjust


class DynamicGridManager:
    """動態網格管理器"""

    def __init__(self):
        self.price_history: Dict[str, deque] = {}
        self.atr_cache: Dict[str, float] = {}
        self.last_calc_time: Dict[str, float] = {}
        self.calc_interval = 60

    def update_price(self, symbol: str, price: float):
        """更新價格歷史"""
        if symbol not in self.price_history:
            self.price_history[symbol] = deque(maxlen=1000)

        self.price_history[symbol].append({
            'price': price,
            'time': time.time()
        })

    def calculate_atr(self, symbol: str, config: 'MaxEnhancement') -> float:
        """計算 ATR 百分比"""
        now = time.time()

        if symbol in self.last_calc_time:
            if now - self.last_calc_time[symbol] < self.calc_interval:
                return self.atr_cache.get(symbol, 0.005)

        history = self.price_history.get(symbol, deque())
        if len(history) < config.volatility_lookback:
            return 0.005

        recent_prices = [h['price'] for h in list(history)[-config.volatility_lookback:]]

        returns = []
        for i in range(1, len(recent_prices)):
            if recent_prices[i-1] > 0:
                ret = (recent_prices[i] - recent_prices[i-1]) / recent_prices[i-1]
                returns.append(ret)

        if not returns:
            return 0.005

        volatility = np.std(returns) * config.atr_multiplier
        volatility = max(config.min_spacing, min(config.max_spacing, volatility))

        self.atr_cache[symbol] = volatility
        self.last_calc_time[symbol] = now

        return volatility

    def get_dynamic_spacing(
        self,
        symbol: str,
        base_take_profit: float,
        base_grid_spacing: float,
        config: 'MaxEnhancement'
    ) -> Tuple[float, float]:
        """獲取動態調整後的間距 → (take_profit_spacing, grid_spacing)"""
        if not config.is_feature_enabled('dynamic_grid'):
            return base_take_profit, base_grid_spacing

        atr = self.calculate_atr(symbol, config)

        dynamic_tp = atr * 0.5
        dynamic_tp = max(config.min_spacing, min(config.max_spacing * 0.6, dynamic_tp))

        dynamic_gs = atr
        dynamic_gs = max(config.min_spacing * 1.5, min(config.max_spacing, dynamic_gs))

        if dynamic_tp >= dynamic_gs:
            dynamic_tp = dynamic_gs * 0.6

        return dynamic_tp, dynamic_gs


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                         領先指標系統 (取代滯後 ATR)                              ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@dataclass
class LeadingIndicatorConfig:
    """領先指標配置"""
    enabled: bool = True

    # === OFI (Order Flow Imbalance) ===
    ofi_enabled: bool = True
    ofi_lookback: int = 20
    ofi_threshold: float = 0.6

    # === Volume Surge ===
    volume_enabled: bool = True
    volume_lookback: int = 50
    volume_surge_threshold: float = 2.0

    # === Spread Analysis ===
    spread_enabled: bool = True
    spread_lookback: int = 30
    spread_surge_threshold: float = 1.5

    # === 綜合信號 ===
    min_signals_for_action: int = 2

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "ofi_enabled": self.ofi_enabled,
            "ofi_lookback": self.ofi_lookback,
            "ofi_threshold": self.ofi_threshold,
            "volume_enabled": self.volume_enabled,
            "volume_lookback": self.volume_lookback,
            "volume_surge_threshold": self.volume_surge_threshold,
            "spread_enabled": self.spread_enabled,
            "spread_lookback": self.spread_lookback,
            "spread_surge_threshold": self.spread_surge_threshold,
            "min_signals_for_action": self.min_signals_for_action
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'LeadingIndicatorConfig':
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class LeadingIndicatorManager:
    """領先指標管理器"""

    def __init__(self, config: LeadingIndicatorConfig = None):
        self.config = config or LeadingIndicatorConfig()

        self.trade_history: Dict[str, deque] = {}
        self.spread_history: Dict[str, deque] = {}
        self.ofi_history: Dict[str, deque] = {}

        self.current_ofi: Dict[str, float] = {}
        self.current_volume_ratio: Dict[str, float] = {}
        self.current_spread_ratio: Dict[str, float] = {}

        self.active_signals: Dict[str, List[str]] = {}

        logger.info("[LeadingIndicator] 領先指標管理器初始化完成")

    def _ensure_symbol_data(self, symbol: str):
        """確保交易對數據結構存在"""
        if symbol not in self.trade_history:
            self.trade_history[symbol] = deque(maxlen=500)
        if symbol not in self.spread_history:
            self.spread_history[symbol] = deque(maxlen=200)
        if symbol not in self.ofi_history:
            self.ofi_history[symbol] = deque(maxlen=100)

    def record_trade(self, symbol: str, price: float, quantity: float, side: str):
        """記錄成交"""
        if not self.config.enabled:
            return

        self._ensure_symbol_data(symbol)

        self.trade_history[symbol].append({
            'time': time.time(),
            'price': price,
            'quantity': quantity,
            'side': side,
            'value': price * quantity
        })

    def update_spread(self, symbol: str, bid: float, ask: float):
        """更新買賣價差"""
        if not self.config.enabled or bid <= 0 or ask <= 0:
            return

        self._ensure_symbol_data(symbol)

        mid_price = (bid + ask) / 2
        spread_bps = (ask - bid) / mid_price * 10000

        self.spread_history[symbol].append({
            'time': time.time(),
            'bid': bid,
            'ask': ask,
            'spread_bps': spread_bps
        })

    def calculate_ofi(self, symbol: str) -> float:
        """計算 Order Flow Imbalance"""
        if symbol not in self.trade_history:
            return 0.0

        trades = list(self.trade_history[symbol])
        if len(trades) < self.config.ofi_lookback:
            return 0.0

        recent = trades[-self.config.ofi_lookback:]

        buy_volume = sum(t['value'] for t in recent if t['side'] == 'buy')
        sell_volume = sum(t['value'] for t in recent if t['side'] == 'sell')

        total = buy_volume + sell_volume
        if total <= 0:
            return 0.0

        ofi = (buy_volume - sell_volume) / total

        self.current_ofi[symbol] = ofi
        self.ofi_history[symbol].append({
            'time': time.time(),
            'ofi': ofi,
            'buy_vol': buy_volume,
            'sell_vol': sell_volume
        })

        return ofi

    def calculate_volume_ratio(self, symbol: str) -> float:
        """計算成交量比率"""
        if symbol not in self.trade_history:
            return 1.0

        trades = list(self.trade_history[symbol])
        if len(trades) < self.config.volume_lookback:
            return 1.0

        now = time.time()
        recent_minute = [t['value'] for t in trades if now - t['time'] < 60]
        historical = trades[-self.config.volume_lookback:]

        current_volume = sum(recent_minute)
        avg_volume_per_trade = np.mean([t['value'] for t in historical])
        expected_volume = avg_volume_per_trade * max(1, len(recent_minute))

        if expected_volume <= 0:
            return 1.0

        ratio = current_volume / expected_volume

        self.current_volume_ratio[symbol] = ratio
        return ratio

    def calculate_spread_ratio(self, symbol: str) -> float:
        """計算價差比率"""
        if symbol not in self.spread_history:
            return 1.0

        spreads = list(self.spread_history[symbol])
        if len(spreads) < self.config.spread_lookback:
            return 1.0

        current_spread = spreads[-1]['spread_bps']
        avg_spread = np.mean([s['spread_bps'] for s in spreads[-self.config.spread_lookback:]])

        if avg_spread <= 0:
            return 1.0

        ratio = current_spread / avg_spread

        self.current_spread_ratio[symbol] = ratio
        return ratio

    def get_signals(self, symbol: str) -> Tuple[List[str], Dict[str, float]]:
        """獲取當前活躍信號"""
        if not self.config.enabled:
            return [], {}

        signals = []

        ofi = self.calculate_ofi(symbol)
        volume_ratio = self.calculate_volume_ratio(symbol)
        spread_ratio = self.calculate_spread_ratio(symbol)

        values = {
            'ofi': ofi,
            'volume_ratio': volume_ratio,
            'spread_ratio': spread_ratio
        }

        if self.config.ofi_enabled:
            if ofi > self.config.ofi_threshold:
                signals.append('OFI_BUY_PRESSURE')
            elif ofi < -self.config.ofi_threshold:
                signals.append('OFI_SELL_PRESSURE')

        if self.config.volume_enabled:
            if volume_ratio > self.config.volume_surge_threshold:
                signals.append('VOLUME_SURGE')

        if self.config.spread_enabled:
            if spread_ratio > self.config.spread_surge_threshold:
                signals.append('SPREAD_EXPANSION')

        self.active_signals[symbol] = signals
        return signals, values

    def get_spacing_adjustment(self, symbol: str, base_spacing: float) -> Tuple[float, str]:
        """根據領先指標計算間距調整 → (調整後間距, 原因說明)"""
        if not self.config.enabled:
            return base_spacing, "領先指標關閉"

        signals, values = self.get_signals(symbol)

        if not signals:
            return base_spacing, "正常"

        adjustment = 1.0
        reasons = []

        if 'VOLUME_SURGE' in signals:
            vol_ratio = values.get('volume_ratio', 1.0)
            vol_adj = min(1.5, 1.0 + (vol_ratio - 2.0) * 0.1)
            adjustment = max(adjustment, vol_adj)
            reasons.append(f"放量×{vol_ratio:.1f}")

        if 'SPREAD_EXPANSION' in signals:
            spread_ratio = values.get('spread_ratio', 1.0)
            spread_adj = min(1.4, 1.0 + (spread_ratio - 1.5) * 0.2)
            adjustment = max(adjustment, spread_adj)
            reasons.append(f"價差擴{spread_ratio:.1f}x")

        if 'OFI_BUY_PRESSURE' in signals or 'OFI_SELL_PRESSURE' in signals:
            ofi = abs(values.get('ofi', 0))
            ofi_adj = 1.0 + ofi * 0.2
            adjustment = max(adjustment, ofi_adj)
            direction = "買" if values.get('ofi', 0) > 0 else "賣"
            reasons.append(f"{direction}壓OFI={ofi:.2f}")

        adjustment = min(adjustment, 1.8)

        adjusted_spacing = base_spacing * adjustment
        reason = " + ".join(reasons) if reasons else "正常"

        return adjusted_spacing, reason

    def get_direction_bias(self, symbol: str) -> Tuple[float, float, str]:
        """根據 OFI 計算方向偏向 → (long_bias, short_bias, reason)"""
        if not self.config.enabled or not self.config.ofi_enabled:
            return 1.0, 1.0, ""

        ofi = self.current_ofi.get(symbol, 0)

        if abs(ofi) < self.config.ofi_threshold * 0.5:
            return 1.0, 1.0, "OFI平衡"

        bias_strength = abs(ofi) * 0.3

        if ofi > 0:
            long_bias = 1.0 + bias_strength
            short_bias = 1.0 - bias_strength * 0.5
            reason = f"買壓+{ofi:.2f}"
        else:
            long_bias = 1.0 - bias_strength * 0.5
            short_bias = 1.0 + bias_strength
            reason = f"賣壓{ofi:.2f}"

        return long_bias, short_bias, reason

    def should_pause_trading(self, symbol: str) -> Tuple[bool, str]:
        """判斷是否應該暫停交易"""
        if not self.config.enabled:
            return False, ""

        signals, values = self.get_signals(symbol)

        volume_ratio = values.get('volume_ratio', 1.0)
        spread_ratio = values.get('spread_ratio', 1.0)

        if volume_ratio > 4.0 and spread_ratio > 2.0:
            return True, f"極端波動 (Vol={volume_ratio:.1f}x, Spread={spread_ratio:.1f}x)"

        if volume_ratio > 6.0:
            return True, f"異常放量 (Vol={volume_ratio:.1f}x)"

        if spread_ratio > 3.0:
            return True, f"流動性枯竭 (Spread={spread_ratio:.1f}x)"

        return False, ""

    def get_stats(self, symbol: str = None) -> Dict:
        """獲取統計資訊"""
        if symbol:
            signals, values = self.get_signals(symbol)
            return {
                'symbol': symbol,
                'enabled': self.config.enabled,
                'ofi': values.get('ofi', 0),
                'volume_ratio': values.get('volume_ratio', 1.0),
                'spread_ratio': values.get('spread_ratio', 1.0),
                'active_signals': signals,
                'trade_count': len(self.trade_history.get(symbol, [])),
                'spread_count': len(self.spread_history.get(symbol, []))
            }

        return {
            'enabled': self.config.enabled,
            'symbols': list(self.trade_history.keys()),
            'config': self.config.to_dict()
        }
