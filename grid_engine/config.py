"""
配置類
"""

import json
from dataclasses import dataclass, field
from typing import Dict

from .utils import CONFIG_FILE, console
from .enhancements import (
    MaxEnhancement, BanditConfig, DGTConfig, LeadingIndicatorConfig
)


@dataclass
class SymbolConfig:
    """單一交易對配置"""
    symbol: str = "XRPUSDC"
    ccxt_symbol: str = "XRP/USDC:USDC"
    enabled: bool = True

    take_profit_spacing: float = 0.004
    grid_spacing: float = 0.006
    initial_quantity: float = 3
    leverage: int = 20

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
            "leverage": self.leverage,
            "limit_multiplier": self.limit_multiplier,
            "threshold_multiplier": self.threshold_multiplier,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'SymbolConfig':
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
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


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
    sync_interval: float = 30.0
    symbols: Dict[str, SymbolConfig] = field(default_factory=dict)
    risk: RiskConfig = field(default_factory=RiskConfig)
    max_enhancement: MaxEnhancement = field(default_factory=MaxEnhancement)
    bandit: BanditConfig = field(default_factory=BanditConfig)
    dgt: DGTConfig = field(default_factory=DGTConfig)
    leading_indicator: LeadingIndicatorConfig = field(default_factory=LeadingIndicatorConfig)
    legacy_api_detected: bool = field(default=False, repr=False)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

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
            "symbols": {k: v.to_dict() for k, v in self.symbols.items()},
            "risk": self.risk.to_dict(),
            "max_enhancement": self.max_enhancement.to_dict(),
            "bandit": self.bandit.to_dict(),
            "dgt": self.dgt.to_dict(),
            "leading_indicator": self.leading_indicator.to_dict(),
            "telegram_bot_token": self.telegram_bot_token,
            "telegram_chat_id": self.telegram_chat_id,
        }

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
            sync_interval=data.get("sync_interval", 30.0),
            legacy_api_detected=False,
            telegram_bot_token=data.get("telegram_bot_token", ""),
            telegram_chat_id=data.get("telegram_chat_id", ""),
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
        with open(CONFIG_FILE, 'w') as f:
            json.dump(self.to_dict(), f, indent=2)
        console.print("[green]配置已保存[/]")

    @classmethod
    def load(cls) -> 'GlobalConfig':
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, 'r') as f:
                return cls.from_dict(json.load(f))
        return cls()
