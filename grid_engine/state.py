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
    # 最近一次 bookTicker 抵達的本機時戳（clock.guard_now()，epoch 秒；
    # 刻意不是 now()，見 clock.guard_now() docstring）。
    # 0 = 從未收過報價。下單前的時效判定讀這個欄位，見 bot._grid_step。
    quote_at: float = 0
    long_position: float = 0
    short_position: float = 0
    # 浮盈**分側**存放，`unrealized_pnl` 由兩者相加導出（見下方 property）。
    # 為什麼不留一個可寫的合計欄位：合計欄位唯一的寫入者是「本次事件帶到的那些
    # 側」，而 Binance 的 ACCOUNT_UPDATE 只推有變動的持倉——避險模式下常常只帶
    # 一側。任何「用本次看到的側重算合計」的寫法，都會把沒帶到的那一側的浮盈
    # 抹成 0；抹成 0 之後 risk_monitor.check_trailing_stop() 會看到
    # drawdown = peak - 0 超過門檻，對一個健康倉位送出市價平倉單。
    # 分側存放讓「只帶一側」與「同事件帶兩側」都自動正確，合計不可能失準。
    long_upnl: float = 0
    short_upnl: float = 0
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

    # WS 寫入版本號：每次 userData handler 動到本 symbol 的持倉/浮盈/掛單計數
    # 就 +1（bot._handle_account_update / bot._handle_order_update）。
    # 用途只有一個——REST 同步（sync_service._sync_positions/_sync_orders）在
    # 「fetch 之前」抓一份，「apply 時（symbol lock 內）」比對，變了就丟棄這個
    # symbol 的 REST 快照。REST 的 fetch→apply 中間隔著一整趟 round-trip 的
    # await，而 WS handler 不取 symbol lock；沒有這個版本號，REST 會拿過期快照
    # 蓋掉成交後的新持倉/掛單計數（見 sync_service 檔頭的不變式敘述）。
    # 只增不減、不重置；Python int 無上限，不會繞回。
    ws_seq: int = 0

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

    @property
    def unrealized_pnl(self) -> float:
        """這個 symbol 的浮盈**合計**（語意與分側化之前完全相同）。

        刻意唯讀、刻意沒有 setter：合計一旦可寫，就會有人拿「本次事件看到的側」
        去覆寫它，而那正是這一族缺陷的形狀（見 long_upnl 上方的註解）。要改值就
        改 long_upnl / short_upnl，合計自然跟著對。
        """
        return self.long_upnl + self.short_upnl


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
