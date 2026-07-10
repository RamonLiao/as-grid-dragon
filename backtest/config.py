"""
配置管理模組
"""
from dataclasses import dataclass, field
from typing import Optional, List
import json
import math


def _norm_slippage(v) -> float:
    """滑價 fallback：非數值/NaN/負值 → 0.0001。"""
    if not (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)) or v < 0:
        return 0.0001
    return float(v)


@dataclass
class Config:
    """回測配置類"""

    # 交易對設定
    symbol: str = "XRPUSDC"

    # 資金設定
    initial_balance: float = 1000.0
    order_value: float = 10.0           # 舊版: USDT 金額 (已棄用，改用 initial_quantity)
    initial_quantity: float = 0.0       # 新版: 幣的數量 (與終端 UI 一致)
    leverage: int = 20

    # 網格參數（非對稱）
    take_profit_spacing: float = 0.004  # 止盈間距 0.4%
    grid_spacing: float = 0.006         # 補倉間距 0.6%

    # 風控參數
    max_drawdown: float = 0.5           # 最大回撤 50%
    max_positions: int = 50             # 最大持倉數
    fee_pct: float = 0.0002             # maker 手續費 0.02%（網格全是限價單；taker 為 0.05%）

    # 成本模型（fidelity-first：預設全開）
    slippage_bps: float = 0.0001       # 每次成交向不利方向偏移比例（執行成本 haircut）
    funding_enabled: bool = True       # 是否結算 funding 現金流
    maintenance_margin_rate: float = 0.005  # 維持保證金率（單一費率代理幣安分層階梯）

    # 持倉控制參數 (與實盤 GridStrategy 一致)
    # 如果設為 0，會使用 limit_multiplier/threshold_multiplier 自動計算
    position_threshold: float = 500.0   # 裝死模式閾值：持倉超過此值停止補倉
    position_limit: float = 100.0       # 止盈加倍閾值：持倉超過此值止盈數量加倍
    limit_multiplier: float = 5.0       # 止盈加倍倍數 (position_limit = initial_quantity × limit_multiplier)
    threshold_multiplier: float = 20.0  # 裝死模式倍數 (position_threshold = initial_quantity × threshold_multiplier)；對齊 grid_engine/config.py:30 的生產值

    # 策略方向
    direction: str = "both"             # "long" / "short" / "both"

    # 網格刷新間隔（分鐘）
    grid_refresh_interval: int = 5

    # 裝死模式配置
    dead_mode_enabled: bool = True      # 是否啟用裝死模式
    dead_mode_fallback_long: float = 1.05   # 多頭裝死無對手倉時止盈比例
    dead_mode_fallback_short: float = 0.95  # 空頭裝死無對手倉時止盈比例

    # 是否使用終端 UI 兼容模式 (order_value = initial_quantity × price)
    terminal_ui_mode: bool = True

    # 初始持倉注入（seed position）：讓回測從既有倉位起跑，重現生產裝死狀態。
    # qty>0 時 pre-populate 一張 lot @ seed price；margin 從 balance 扣、不扣 fee
    # （既存倉位非本回測新成交）。全 0（預設）→ 與空倉起跑 bit-identical。
    # ⚠️ 單 lot ≡ 多 lot 加權均價只對 mark-to-market（unrealized）與一次性全平
    #    精確——即「凍結後強平」的重現情境。一旦後續 partial TP 逐格成交，
    #    backtester 的 per-lot FIFO 會先平這張 index-0 的 seed lot（例如多頭 690
    #    的高價 lot），認列大額 realized 虧損，而生產 Binance hedge mode 是對
    #    【混合均價】結算。故涉及 seed 部分平倉的實驗（threshold 掃描），
    #    realized_pnl 與 final_equity 會系統性偏離生產，只可看方向不可當精確預測
    #    （內部 review I2）。
    seed_long_qty: float = 0.0
    seed_long_price: float = 0.0
    seed_short_qty: float = 0.0
    seed_short_price: float = 0.0

    @property
    def long_settings(self) -> dict:
        """多頭設定：上方止盈(小間距)，下方補倉(大間距)"""
        return {
            "up_spacing": self.take_profit_spacing,
            "down_spacing": self.grid_spacing
        }

    @property
    def short_settings(self) -> dict:
        """空頭設定：上方補倉(大間距)，下方止盈(小間距)"""
        return {
            "up_spacing": self.grid_spacing,
            "down_spacing": self.take_profit_spacing
        }

    def to_dict(self) -> dict:
        """轉換為字典"""
        return {
            "symbol": self.symbol,
            "initial_balance": self.initial_balance,
            "order_value": self.order_value,
            "initial_quantity": self.initial_quantity,
            "leverage": self.leverage,
            "take_profit_spacing": self.take_profit_spacing,
            "grid_spacing": self.grid_spacing,
            "max_drawdown": self.max_drawdown,
            "max_positions": self.max_positions,
            "fee_pct": self.fee_pct,
            "direction": self.direction,
            "grid_refresh_interval": self.grid_refresh_interval,
            "position_threshold": self.position_threshold,
            "position_limit": self.position_limit,
            "limit_multiplier": self.limit_multiplier,
            "threshold_multiplier": self.threshold_multiplier,
            "terminal_ui_mode": self.terminal_ui_mode,
            "long_settings": self.long_settings,
            "short_settings": self.short_settings,
            "slippage_bps": self.slippage_bps,
            "funding_enabled": self.funding_enabled,
            "maintenance_margin_rate": self.maintenance_margin_rate,
            "dead_mode_enabled": self.dead_mode_enabled,
            "seed_long_qty": self.seed_long_qty,
            "seed_long_price": self.seed_long_price,
            "seed_short_qty": self.seed_short_qty,
            "seed_short_price": self.seed_short_price,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'Config':
        """從字典創建"""
        return cls(
            symbol=data.get("symbol", "XRPUSDC"),
            initial_balance=data.get("initial_balance", 1000.0),
            order_value=data.get("order_value", 10.0),
            initial_quantity=data.get("initial_quantity", 0.0),
            leverage=data.get("leverage", 20),
            take_profit_spacing=data.get("take_profit_spacing", 0.004),
            grid_spacing=data.get("grid_spacing", 0.006),
            max_drawdown=data.get("max_drawdown", 0.5),
            max_positions=data.get("max_positions", 50),
            fee_pct=data.get("fee_pct", 0.0002),
            direction=data.get("direction", "both"),
            grid_refresh_interval=data.get("grid_refresh_interval", 5),
            position_threshold=data.get("position_threshold", 500.0),
            position_limit=data.get("position_limit", 100.0),
            limit_multiplier=data.get("limit_multiplier", 5.0),
            threshold_multiplier=data.get("threshold_multiplier", 20.0),
            terminal_ui_mode=data.get("terminal_ui_mode", True),
            slippage_bps=_norm_slippage(data.get("slippage_bps", 0.0001)),
            funding_enabled=data.get("funding_enabled", True)
                if isinstance(data.get("funding_enabled", True), bool) else True,
            maintenance_margin_rate=data.get("maintenance_margin_rate", 0.005),
            dead_mode_enabled=data.get("dead_mode_enabled", True),
            seed_long_qty=data.get("seed_long_qty", 0.0),
            seed_long_price=data.get("seed_long_price", 0.0),
            seed_short_qty=data.get("seed_short_qty", 0.0),
            seed_short_price=data.get("seed_short_price", 0.0),
        )

    def save(self, filepath: str):
        """保存配置到文件"""
        with open(filepath, 'w') as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load(cls, filepath: str) -> 'Config':
        """從文件載入配置"""
        with open(filepath, 'r') as f:
            return cls.from_dict(json.load(f))

    def __str__(self) -> str:
        return (
            f"Config({self.symbol})\n"
            f"  資金: ${self.initial_balance} | 每單: ${self.order_value} | 槓桿: {self.leverage}x\n"
            f"  止盈: {self.take_profit_spacing*100:.2f}% | 補倉: {self.grid_spacing*100:.2f}%\n"
            f"  方向: {self.direction} | 最大持倉: {self.max_positions}"
        )


# 預設配置模板
PRESETS = {
    "conservative": Config(
        take_profit_spacing=0.003,
        grid_spacing=0.005,
        leverage=10,
        max_positions=30
    ),
    "balanced": Config(
        take_profit_spacing=0.004,
        grid_spacing=0.006,
        leverage=20,
        max_positions=50
    ),
    "aggressive": Config(
        take_profit_spacing=0.005,
        grid_spacing=0.008,
        leverage=30,
        max_positions=80
    )
}
