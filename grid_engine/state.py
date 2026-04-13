"""
交易狀態
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict
from collections import deque


@dataclass
class SymbolState:
    """單一交易對狀態"""
    symbol: str
    latest_price: float = 0
    best_bid: float = 0
    best_ask: float = 0
    long_position: float = 0
    short_position: float = 0
    unrealized_pnl: float = 0
    buy_long_orders: float = 0
    sell_long_orders: float = 0
    buy_short_orders: float = 0
    sell_short_orders: float = 0
    tracking_active: bool = False
    peak_pnl: float = 0
    current_pnl: float = 0
    recent_trades: deque = field(default_factory=lambda: deque(maxlen=5))
    total_trades: int = 0
    total_profit: float = 0

    # 裝死模式狀態
    long_dead_mode: bool = False
    short_dead_mode: bool = False

    # 網格價格追蹤
    last_grid_price_long: float = 0
    last_grid_price_short: float = 0

    # MAX 增強狀態
    current_funding_rate: float = 0
    dynamic_take_profit: float = 0
    dynamic_grid_spacing: float = 0
    inventory_ratio: float = 0

    # 領先指標狀態
    leading_ofi: float = 0
    leading_volume_ratio: float = 1.0
    leading_spread_ratio: float = 1.0
    leading_signals: List[str] = field(default_factory=list)


@dataclass
class AccountBalance:
    """單一帳戶餘額"""
    currency: str = "USDC"
    wallet_balance: float = 0
    available_balance: float = 0
    unrealized_pnl: float = 0
    margin_used: float = 0

    @property
    def equity(self) -> float:
        """權益 = 錢包餘額 + 未實現盈虧"""
        return self.wallet_balance + self.unrealized_pnl

    @property
    def margin_ratio(self) -> float:
        """保證金使用率"""
        if self.equity <= 0:
            return 0
        return self.margin_used / self.equity


@dataclass
class GlobalState:
    """全局狀態"""
    running: bool = False
    connected: bool = False
    start_time: Optional[datetime] = None

    accounts: Dict[str, AccountBalance] = field(default_factory=lambda: {
        "USDC": AccountBalance(currency="USDC"),
        "USDT": AccountBalance(currency="USDT")
    })

    total_equity: float = 0
    free_balance: float = 0
    margin_usage: float = 0
    total_unrealized_pnl: float = 0

    symbols: Dict[str, SymbolState] = field(default_factory=dict)
    total_trades: int = 0
    total_profit: float = 0

    trailing_active: Dict[str, bool] = field(default_factory=dict)
    peak_pnl: Dict[str, float] = field(default_factory=dict)
    peak_equity: float = 0

    last_reduce_time: Dict[str, float] = field(default_factory=dict)

    def get_account(self, currency: str) -> AccountBalance:
        """獲取指定幣種帳戶"""
        if currency not in self.accounts:
            self.accounts[currency] = AccountBalance(currency=currency)
        return self.accounts[currency]

    def update_totals(self):
        """更新總計數據"""
        self.total_equity = sum(acc.equity for acc in self.accounts.values())
        self.free_balance = sum(acc.available_balance for acc in self.accounts.values())
        self.total_unrealized_pnl = sum(acc.unrealized_pnl for acc in self.accounts.values())
        if self.total_equity > 0:
            total_margin = sum(acc.margin_used for acc in self.accounts.values())
            self.margin_usage = total_margin / self.total_equity
