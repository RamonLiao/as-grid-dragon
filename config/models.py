# Author: louis
# Threads: https://www.threads.com/@mr.__.l
"""
配置模型
========
indicators 專用 config（原全域 config 已由 grid_engine/config.py 取代）
"""

from dataclasses import dataclass, fields, asdict


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                              序列化 Mixin                                      ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class SerializableMixin:
    """
    Dataclass 序列化 Mixin

    提供標準化的 to_dict() 和 from_dict() 方法，
    減少每個 dataclass 中重複的序列化代碼。

    使用方式:
        @dataclass
        class MyConfig(SerializableMixin):
            field1: str = ""
            field2: int = 0
    """

    def to_dict(self) -> dict:
        """將 dataclass 轉換為字典"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict):
        """從字典建立 dataclass 實例，自動過濾無效欄位"""
        valid_fields = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in valid_fields})


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                              MAX 增強配置                                      ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@dataclass
class MaxEnhancement(SerializableMixin):
    """
    MAX 版本增強功能配置

    1. Funding Rate 偏向
    2. GLFT γ 風險係數
    3. 動態網格範圍 (已被領先指標取代)

    建議配置:
    - all_enhancements_enabled: False (保持無腦執行)
    - 使用 Bandit + 領先指標 即可
    """
    # === 主開關 ===
    all_enhancements_enabled: bool = False   # 總開關：False = 純淨模式 (保持無腦執行)

    # === Funding Rate 偏向 ===
    funding_rate_enabled: bool = False          # 預設關閉 (長期持倉時可開啟)
    funding_rate_threshold: float = 0.0001      # 0.01% 以上才調整
    funding_rate_position_bias: float = 0.2     # 偏向調整比例 (20%)

    # === GLFT γ 風險係數 ===
    glft_enabled: bool = False                  # 預設關閉 (多空不平衡時可開啟)
    gamma: float = 0.1                          # 風險厭惡係數 (0.01-1.0)
    inventory_target: float = 0.5               # 目標庫存比例 (0.5 = 多空平衡)

    # === 動態網格範圍 (ATR - 滯後指標) ===
    dynamic_grid_enabled: bool = False          # 預設關閉 (已被領先指標取代)
    atr_period: int = 14                        # ATR 週期
    atr_multiplier: float = 1.5                 # ATR 乘數
    min_spacing: float = 0.002                  # 最小間距 0.2%
    max_spacing: float = 0.015                  # 最大間距 1.5%
    volatility_lookback: int = 100              # 波動率回看期

    def is_feature_enabled(self, feature: str) -> bool:
        """檢查功能是否啟用 (考慮總開關)"""
        if not self.all_enhancements_enabled:
            return False
        return getattr(self, f"{feature}_enabled", False)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                              Bandit 配置                                       ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@dataclass
class BanditConfig(SerializableMixin):
    """
    Bandit 優化器配置 (增強版)

    新增功能:
    1. 冷啟動預載 - 首次運行使用歷史最佳參數
    2. Contextual - 根據市場狀態選擇不同策略
    3. Thompson Sampling - 連續參數空間探索
    4. MDD 懲罰 - 改進 reward 計算
    
    純淨模式: enabled=False (預設)
    - 不會覆蓋用戶設定的止盈/補倉間距
    - 用戶設什麼參數就用什麼
    """
    enabled: bool = False  # 預設關閉 - 純淨模式
    window_size: int = 50              # 滑動窗口大小 (只看最近 N 筆交易)
    exploration_factor: float = 1.5    # UCB 探索係數 (越大越愛探索)
    min_pulls_per_arm: int = 3         # 每個 arm 至少要試幾次
    update_interval: int = 10          # 每 N 筆交易評估一次

    # === 冷啟動配置 ===
    cold_start_enabled: bool = True    # 啟用冷啟動預載
    cold_start_arm_idx: int = 4        # 預設使用的 arm 索引 (平衡型)

    # === Contextual Bandit ===
    contextual_enabled: bool = True    # 啟用市場狀態感知
    volatility_lookback: int = 20      # 波動率計算回看期
    trend_lookback: int = 50           # 趨勢計算回看期
    high_volatility_threshold: float = 0.02  # 高波動閾值 (2%)
    trend_threshold: float = 0.01      # 趨勢閾值 (1%)

    # === Thompson Sampling ===
    thompson_enabled: bool = True      # 啟用 Thompson Sampling
    thompson_prior_alpha: float = 1.0  # Beta 分布先驗 α
    thompson_prior_beta: float = 1.0   # Beta 分布先驗 β
    param_perturbation: float = 0.1    # 參數擾動範圍 (10%)

    # === Reward 改進 ===
    mdd_penalty_weight: float = 0.5    # Max Drawdown 懲罰權重
    win_rate_bonus: float = 0.2        # 勝率獎勵權重


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                              DGT 配置                                          ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@dataclass
class DGTConfig(SerializableMixin):
    """
    DGT (Dynamic Grid Trading) 配置

    注意: 此功能對 AS 高頻網格 (買一賣一) 效果有限
    AS 網格是跟隨價格的，沒有固定邊界概念
    保留此配置是為了未來可能的多層網格支援
    """
    enabled: bool = False              # 預設關閉 (AS 網格不需要)
    reset_threshold: float = 0.05      # 價格偏離多少觸發重置 (5%)
    profit_reinvest_ratio: float = 0.5 # 利潤再投資比例
    boundary_buffer: float = 0.02      # 邊界緩衝 (2%)


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                              領先指標配置                                       ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

@dataclass
class LeadingIndicatorConfig(SerializableMixin):
    """
    領先指標配置

    核心理念:
    - ATR/波動率是「滯後指標」: 價格已經動了才知道
    - 領先指標: 在價格大幅波動「之前」就能察覺

    使用的領先因子:
    1. Order Flow Imbalance (OFI) - 訂單流失衡，反映買賣壓力
    2. Volume Surge - 成交量突增，預示即將突破
    3. Spread Expansion - 買賣價差擴大，預示流動性變差/波動即將放大
    
    純淨模式: enabled=False (預設)
    - 不會根據領先指標調整間距
    """
    enabled: bool = False  # 預設關閉 - 純淨模式

    # === OFI (Order Flow Imbalance) ===
    ofi_enabled: bool = True
    ofi_lookback: int = 20                  # OFI 計算回看期
    ofi_threshold: float = 0.6              # OFI > 此值 = 強烈買壓 or 賣壓

    # === Volume Surge ===
    volume_enabled: bool = True
    volume_lookback: int = 50               # 成交量回看期
    volume_surge_threshold: float = 2.0     # 成交量 > 平均 × 此值 = 異常放量

    # === Spread Analysis ===
    spread_enabled: bool = True
    spread_lookback: int = 30               # 價差回看期
    spread_surge_threshold: float = 1.5     # 價差 > 平均 × 此值 = 流動性下降

    # === 綜合信號 ===
    min_signals_for_action: int = 2         # 至少 N 個信號同時觸發才調整
