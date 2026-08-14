"""
配置類
"""

import json
import math
from dataclasses import dataclass, field
from typing import Dict, Optional

from .utils import CONFIG_FILE, console
from .enhancements import (
    MaxEnhancement, BanditConfig, DGTConfig, LeadingIndicatorConfig
)
from .config_io import merge_preserve_save


def _norm_requote_factor(v) -> float:
    """正規化有倉位重掛閾值係數，非有限/≤0/>10 → fallback 0.5"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        f = float("nan")
    if not math.isfinite(f) or f <= 0 or f > 10:
        console.print(f"[yellow]requote_threshold_factor={v!r} 非法，回退 0.5[/]")
        return 0.5
    return f


def _norm_assumed_leverage(v) -> int:
    """正規化回測假設槓桿，非整數/超出 1~125 → fallback 5（交易所實測值）。

    此值只餵回測的保證金/強平計算，垃圾值不會報錯而是靜默算錯風險：
    實測 -5 會跑完 113 筆交易吐出 +0.155% 正報酬，0 則 divide-by-zero
    後靜默回 0 筆。UI clamp 1~125 擋不住直接編輯 JSON、腳本賦值與
    未來新增的寫入點，所以守衛掛在 `SymbolConfig.__setattr__`
    ——dataclass `__init__` 也走 setattr ⇒ 那是唯一涵蓋全部路徑的咽喉點。

    交易所槓桿是整數，非整數值一律**拒絕**而非截斷（截斷會讓 20.9 悄悄
    變 20，使用者以為填對了）。
    """
    if isinstance(v, bool):  # bool 是 int 子類，float(True)==1.0 會矇混成 1x
        f = float("nan")
    else:
        try:
            f = float(v)
        except Exception:
            # 不只 TypeError/ValueError：自訂 __float__ 可以拋任何東西，
            # 讓它炸穿等於把「配置有垃圾值」變成無關的 crash。
            f = float("nan")
    if not math.isfinite(f) or not f.is_integer() or not (1 <= f <= 125):
        console.print(f"[yellow]assumed_leverage={v!r} 非法，回退 5[/]")
        return 5
    return int(f)


@dataclass
class SymbolConfig:
    """單一交易對配置"""
    symbol: str = "XRPUSDC"
    ccxt_symbol: str = "XRP/USDC:USDC"
    enabled: bool = True

    take_profit_spacing: float = 0.004
    grid_spacing: float = 0.006
    initial_quantity: float = 3
    assumed_leverage: int = 5   # 交易所實測值（2026-07-26）。不推送交易所，
                                # 僅供回測算保證金/強平；填錯會低估爆倉風險。

    limit_multiplier: float = 5.0
    threshold_multiplier: float = 20.0

    @property
    def coin_name(self) -> str:
        return self.ccxt_symbol.split('/')[0]

    @property
    def contract_type(self) -> str:
        return self.ccxt_symbol.split('/')[1].split(':')[0]

    @property
    def ws_symbol(self) -> str:
        return f"{self.coin_name.lower()}{self.contract_type.lower()}"

    @property
    def position_limit(self) -> float:
        """動態計算持倉限制 (止盈加倍閾值)"""
        return self.initial_quantity * self.limit_multiplier

    @property
    def position_threshold(self) -> float:
        """動態計算持倉閾值 (裝死模式閾值)"""
        return self.initial_quantity * self.threshold_multiplier

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "ccxt_symbol": self.ccxt_symbol,
            "enabled": self.enabled,
            "take_profit_spacing": self.take_profit_spacing,
            "grid_spacing": self.grid_spacing,
            "initial_quantity": self.initial_quantity,
            "assumed_leverage": self.assumed_leverage,
            "limit_multiplier": self.limit_multiplier,
            "threshold_multiplier": self.threshold_multiplier,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'SymbolConfig':
        # shallow copy：本函式只改 top-level key，避免就地竄改呼叫端的 dict
        # （leverage 遷移分支生產 config 四個 symbol 全都有 ⇒ 每次載入必觸發）。
        data = dict(data)
        # 兼容舊配置
        if "position_threshold" in data and "threshold_multiplier" not in data:
            qty = data.get("initial_quantity", 3)
            if qty > 0:
                data["threshold_multiplier"] = data["position_threshold"] / qty
            del data["position_threshold"]
        if "position_limit" in data and "limit_multiplier" not in data:
            qty = data.get("initial_quantity", 3)
            if qty > 0:
                data["limit_multiplier"] = data["position_limit"] / qty
            del data["position_limit"]
        # 兼容舊 key：leverage → assumed_leverage（新 key 存在時新 key 勝）
        if "leverage" in data:
            if "assumed_leverage" not in data:
                data["assumed_leverage"] = data["leverage"]
            del data["leverage"]
        # assumed_leverage 的值域守衛不在這裡：它掛在 __setattr__，
        # dataclass __init__ 也走 setattr ⇒ 這條路徑一樣會被檢查，
        # 且舊 key 遷移過來的非法值同樣擋得住。
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    _RENAMED = {
        "leverage": "assumed_leverage 已取代 leverage。此值不推送交易所"
                    "（實盤槓桿由交易所端設定），僅供回測使用。",
    }

    def __getattr__(self, name):
        # 只在正常查找失敗後才被呼叫。注意 @property 內部拋 AttributeError
        # 也會落到這裡，且原始例外會被完全丟棄（__context__ 為 None）——
        # fallback 必須帶上真正的屬性名，否則 coin_name 出錯會被誤報成
        # 「leverage 已改名」，把除錯導向完全錯誤的方向。
        if name in SymbolConfig._RENAMED:
            raise AttributeError(SymbolConfig._RENAMED[name])
        raise AttributeError(
            f"{type(self).__name__!r} object has no attribute {name!r}"
            f"（若 {name!r} 是 @property，其內部的 AttributeError 原文已被本"
            f" __getattr__ 遮蔽；用 type(obj).{name}.fget(obj) 直接呼叫可重現）")

    def __setattr__(self, name, value):
        # __getattr__ 只攔讀不攔寫；少了這個，cfg.leverage = 20 會靜默建立
        # 實例屬性（讀得到、to_dict() 忽略）＝ 假旋鈕復刻。
        if name in SymbolConfig._RENAMED:
            raise AttributeError(SymbolConfig._RENAMED[name])
        if name == "assumed_leverage":
            # 唯一咽喉點：from_dict、TUI IntPrompt、web 三個寫入點、
            # 直接建構 dataclass、REPL 賦值全部經過這裡。
            value = _norm_assumed_leverage(value)
        object.__setattr__(self, name, value)


@dataclass
class RiskConfig:
    """風控配置"""
    enabled: bool = True
    margin_threshold: float = 0.5
    trailing_start_profit: float = 5.0
    trailing_drawdown_pct: float = 0.10
    trailing_min_drawdown: float = 2.0

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "margin_threshold": self.margin_threshold,
            "trailing_start_profit": self.trailing_start_profit,
            "trailing_drawdown_pct": self.trailing_drawdown_pct,
            "trailing_min_drawdown": self.trailing_min_drawdown
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'RiskConfig':
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class GlobalConfig:
    """全局配置"""
    api_key: str = ""
    api_secret: str = ""
    api_password: str = ""               # Bitget 等需要 passphrase
    exchange_id: str = "binance"         # ccxt exchange id
    sandbox_mode: bool = False           # ccxt set_sandbox_mode
    api_url_override: str = ""           # 手動覆蓋 REST API URL (e.g. Bybit demo)
    websocket_url: str = "wss://fstream.binance.com/ws"
    sync_interval: float = 10.0
    position_adjust_cooldown: float = 5.0  # 有倉位時網格重掛最小間隔（秒），0 = 關閉
    requote_threshold_factor: float = 0.5  # 有倉位重掛閾值係數（0 < factor <= 10）
    symbols: Dict[str, SymbolConfig] = field(default_factory=dict)
    risk: RiskConfig = field(default_factory=RiskConfig)
    max_enhancement: MaxEnhancement = field(default_factory=MaxEnhancement)
    bandit: BanditConfig = field(default_factory=BanditConfig)
    dgt: DGTConfig = field(default_factory=DGTConfig)
    leading_indicator: LeadingIndicatorConfig = field(default_factory=LeadingIndicatorConfig)
    legacy_api_detected: bool = field(default=False, repr=False)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_enabled: bool = True
    telegram_risk_alert_enabled: bool = True
    telegram_risk_alert_cooldown: int = 300  # 風控警報冷卻秒數
    telegram_daily_pnl_hour: int = 20  # Asia/Taipei (UTC+8) 整點
    # === Bandit 狀態持久化 ===
    bandit_state_path: Optional[str] = None       # None → bot 套 default logs/bandit_state.json
    bandit_state_max_age_sec: Optional[int] = None  # None = 永不過期

    def to_dict(self) -> dict:
        return {
            "api_key": self.api_key,
            "api_secret": self.api_secret,
            "api_password": self.api_password,
            "exchange_id": self.exchange_id,
            "sandbox_mode": self.sandbox_mode,
            "api_url_override": self.api_url_override,
            "websocket_url": self.websocket_url,
            "sync_interval": self.sync_interval,
            "position_adjust_cooldown": self.position_adjust_cooldown,
            "requote_threshold_factor": self.requote_threshold_factor,
            "symbols": {k: v.to_dict() for k, v in self.symbols.items()},
            "risk": self.risk.to_dict(),
            "max_enhancement": self.max_enhancement.to_dict(),
            "bandit": self.bandit.to_dict(),
            "dgt": self.dgt.to_dict(),
            "leading_indicator": self.leading_indicator.to_dict(),
            "telegram_bot_token": self.telegram_bot_token,
            "telegram_chat_id": self.telegram_chat_id,
            "telegram_enabled": self.telegram_enabled,
            "telegram_risk_alert_enabled": self.telegram_risk_alert_enabled,
            "telegram_risk_alert_cooldown": self.telegram_risk_alert_cooldown,
            "telegram_daily_pnl_hour": self.telegram_daily_pnl_hour,
            "bandit_state_path": self.bandit_state_path,
            "bandit_state_max_age_sec": self.bandit_state_max_age_sec,
        }

    @staticmethod
    def _parse_daily_pnl_hour(value) -> int:
        """正規化每日摘要時間，非法值 fallback 到 20"""
        try:
            hour = int(value)
        except (TypeError, ValueError):
            return 20
        return hour if 0 <= hour <= 23 else 20

    @staticmethod
    def _parse_risk_alert_cooldown(value) -> int:
        """正規化風控警報冷卻秒數，非法值 fallback 到 300"""
        try:
            cooldown = int(value)
        except (TypeError, ValueError):
            return 300
        return cooldown if cooldown > 0 else 300

    @staticmethod
    def _parse_position_adjust_cooldown(value) -> float:
        """正規化有倉位重掛冷卻秒數，非法/負值 fallback 到 5.0（0 為合法關閉值）"""
        try:
            cooldown = float(value)
        except (TypeError, ValueError):
            return 5.0
        return cooldown if math.isfinite(cooldown) and cooldown >= 0 else 5.0

    @staticmethod
    def _parse_bandit_state_max_age(value) -> Optional[int]:
        """正規化 bandit 狀態過期秒數；非正/非法/None → None（永不過期）。"""
        if value is None:
            return None
        try:
            secs = int(value)
        except (TypeError, ValueError):
            return None
        return secs if secs > 0 else None

    @classmethod
    def from_dict(cls, data: dict) -> 'GlobalConfig':
        config = cls(
            api_key=data.get("api_key", ""),
            api_secret=data.get("api_secret", ""),
            api_password=data.get("api_password", ""),
            exchange_id=data.get("exchange_id", "binance"),
            sandbox_mode=data.get("sandbox_mode", False),
            api_url_override=data.get("api_url_override", ""),
            websocket_url=data.get("websocket_url", "wss://fstream.binance.com/ws"),
            sync_interval=data.get("sync_interval", 10.0),
            position_adjust_cooldown=cls._parse_position_adjust_cooldown(
                data.get("position_adjust_cooldown", 5.0)),
            requote_threshold_factor=_norm_requote_factor(
                data.get("requote_threshold_factor", 0.5)),
            legacy_api_detected=False,
            telegram_bot_token=data.get("telegram_bot_token", ""),
            telegram_chat_id=data.get("telegram_chat_id", ""),
            telegram_enabled=bool(data.get("telegram_enabled", True)),
            telegram_risk_alert_enabled=bool(data.get("telegram_risk_alert_enabled", True)),
            telegram_risk_alert_cooldown=cls._parse_risk_alert_cooldown(data.get("telegram_risk_alert_cooldown")),
            telegram_daily_pnl_hour=cls._parse_daily_pnl_hour(data.get("telegram_daily_pnl_hour")),
            bandit_state_path=data.get("bandit_state_path") or None,
            bandit_state_max_age_sec=cls._parse_bandit_state_max_age(
                data.get("bandit_state_max_age_sec")),
        )
        for k, v in data.get("symbols", {}).items():
            config.symbols[k] = SymbolConfig.from_dict(v)
        if "risk" in data:
            config.risk = RiskConfig.from_dict(data["risk"])
        if "max_enhancement" in data:
            config.max_enhancement = MaxEnhancement.from_dict(data["max_enhancement"])
        if "bandit" in data:
            config.bandit = BanditConfig.from_dict(data["bandit"])
        if "dgt" in data:
            config.dgt = DGTConfig.from_dict(data["dgt"])
        if "leading_indicator" in data:
            config.leading_indicator = LeadingIndicatorConfig.from_dict(data["leading_indicator"])
        return config

    def save(self):
        # drop_symbol_keys：一次性遷移，清除舊 leverage key。
        # merge_preserve 只 update 不刪 key，不顯式 drop 的話舊 key 會與
        # assumed_leverage 永久並存 ⇒ 使用者手動編輯舊 key 會靜默無效
        # ＝ 親手製造第二個假旋鈕。
        # 清除條件：生產 config 確認不含舊 key 後即可移除本參數（backlog）。
        merge_preserve_save(CONFIG_FILE, self.to_dict(),
                            drop_symbol_keys={"leverage"})
        console.print("[green]配置已保存[/]")

    @classmethod
    def load(cls) -> 'GlobalConfig':
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, 'r') as f:
                return cls.from_dict(json.load(f))
        return cls()
