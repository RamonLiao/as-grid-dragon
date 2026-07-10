"""
回測引擎核心模組
網格交易策略回測器

整合 GridStrategy 邏輯，確保回測與實盤一致：
- 裝死模式 (Dead Mode)
- 持倉閾值控制
- 止盈加倍機制
"""
import math
import pandas as pd
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field
from .config import Config

# 決策改吃純層 grid_engine.decision.decide()（實盤與回測同源，移除舊策略模組依賴）。
from grid_engine import clock
from grid_engine.decision import (
    decide, DecisionInputs,
    is_dead_mode, dead_mode_price, grid_prices, tp_quantity,
)
from grid_engine.snapshot import ManagerBundle, build_snapshot
from grid_engine.enhancements import (
    DynamicGridManager, LeadingIndicatorManager, GLFTController, MaxEnhancement,
)
from backtest.costs import apply_slippage, funding_charge
from backtest.matching import entry_crossed, tp_crossed

# 回測保真限制（樂觀成交模型，與實盤的已知差異）。寫入 BacktestResult.notes。
FIDELITY_NOTES = (
    "回測保真限制: "
    "(1) 樂觀成交——限價單以當根收盤價成交、無 queue/部分成交佇列; "
    "(2) flat-entry 近似——零倉位 bootstrap 沿用收盤價觸發即進場; "
    "(3) leading/ATR/GLFT 增強於回測退化為中性(全關); "
    "(4) Bandit 參數優化不在回測 loop 內重現; "
    "(5) 決策同源實盤 decide()，實盤每 10s 追價重掛(pos==0)於回測以 should_adjust 偏離門檻近似; "
    "(6) 進場量語意=固定幣量(=initial_quantity，同實盤下單)，舊/新 equity 曲線不可直接比較; "
    "(7) 成本模型(主路徑)——slippage_bps 執行成本 haircut(逆選擇代理，非訂單簿滑價；"
    "網格 maker 單實際成交價≤掛單價，此 bps 當保守緩衝) + funding 現金流結算"
    "(真實歷史 settlement 時點，缺漏時點 rate=0；notional 用 bar close 當 mark price 代理"
    "；funding 快取按 symbol 不按區間，同 symbol 更寬回測區間需先刪 data/funding/<symbol>.csv 重抓，否則尾段缺漏 rate=0); "
    "(8) 保守堆疊——fee_pct 預設 0.04%(taker)已對 maker 網格偏保守，疊 slippage haircut → "
    "回測績效偏低估、屬刻意保守下界; "
    "(9) legacy 路徑(initial_quantity<=0)不含成本模型。"
)


def _legacy_grid_decision(price, my_position, opposite_position, cfg, side, base_qty):
    """legacy 模式決策：改吃純層 decision helpers（取代已刪的 core strategy get_grid_decision）。
    回傳與舊 get_grid_decision 相容的 dict。此路徑僅 initial_quantity<=0 時觸發（deprecated）。
    注意：dead_mode 自訂 fallback 比例不在此重現（config 預設 1.05/0.95 與純層常數一致）。"""
    dead = getattr(cfg, 'dead_mode_enabled', True) and is_dead_mode(my_position, cfg.position_threshold)
    tp_qty = tp_quantity(base_qty, my_position, opposite_position,
                         cfg.position_limit, cfg.position_threshold)
    if dead:
        tp_price = dead_mode_price(price, my_position, opposite_position, side)
        return {"dead_mode": True, "tp_price": tp_price, "entry_price": None, "tp_qty": tp_qty}
    tp_price, entry_price = grid_prices(price, cfg.take_profit_spacing, cfg.grid_spacing, side)
    return {"dead_mode": False, "tp_price": tp_price, "entry_price": entry_price, "tp_qty": tp_qty}


@dataclass
class Position:
    """持倉資訊"""
    entry_price: float
    quantity: float
    margin: float
    side: str  # "long" or "short"
    entry_time: datetime = None


@dataclass
class Trade:
    """交易記錄"""
    timestamp: datetime
    action: str  # BUY, SELL, SELL_SHORT, COVER_SHORT
    price: float
    quantity: float
    side: str  # LONG, SHORT
    pnl: float
    fee: float
    gross_pnl: float
    unrealized_pnl: float
    equity: float


@dataclass
class BacktestResult:
    """回測結果"""
    final_equity: float
    return_pct: float
    max_drawdown: float
    realized_pnl: float
    unrealized_pnl: float
    total_pnl: float
    trades_count: int
    win_rate: float
    profit_factor: float
    sharpe_ratio: float
    direction: str
    config: Config
    trade_history: List[Trade] = field(default_factory=list)
    equity_curve: List[Tuple] = field(default_factory=list)
    notes: str = ""  # 保真限制 / 已知差異說明
    funding_paid: float = 0.0  # funding 現金流總額（正=淨付出），不計入 trades

    def to_dict(self) -> dict:
        """轉換為字典"""
        return {
            "final_equity": self.final_equity,
            "return_pct": self.return_pct,
            "max_drawdown": self.max_drawdown,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "total_pnl": self.total_pnl,
            "trades_count": self.trades_count,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "sharpe_ratio": self.sharpe_ratio,
            "direction": self.direction,
            "funding_paid": self.funding_paid,
        }

    def __str__(self) -> str:
        return (
            f"回測結果 ({self.config.symbol})\n"
            f"{'='*40}\n"
            f"最終淨值: ${self.final_equity:.2f}\n"
            f"收益率: {self.return_pct*100:.2f}%\n"
            f"最大回撤: {self.max_drawdown*100:.2f}%\n"
            f"交易次數: {self.trades_count}\n"
            f"勝率: {self.win_rate*100:.1f}%\n"
            f"盈虧比: {self.profit_factor:.2f}\n"
            f"已實現盈虧: ${self.realized_pnl:.2f}\n"
            f"未實現盈虧: ${self.unrealized_pnl:.2f}"
        )


class GridBacktester:
    """
    網格交易回測器

    整合 GridStrategy 邏輯，確保與實盤一致：
    - 裝死模式判斷
    - 持倉閾值控制
    - 止盈加倍機制
    """

    def __init__(self, df: pd.DataFrame, config: Config, funding_map=None):
        """
        初始化回測器

        Args:
            df: K線數據 DataFrame (需含 open_time, open, high, low, close, volume)
            config: 回測配置
            funding_map: {epoch_sec: rate} funding settlement 對照表，None 則嘗試從 DataLoader 讀取
        """
        self.df = df.reset_index(drop=True)
        self.config = config
        self.funding_map = funding_map

        # 網格設定
        self.long_settings = config.long_settings
        self.short_settings = config.short_settings

        # 帳戶狀態
        self.balance = config.initial_balance
        self.max_equity = config.initial_balance

        # 持倉
        self.long_positions: List[Position] = []
        self.short_positions: List[Position] = []

        # 掛單
        self.orders = {"long": [], "short": []}

        # 記錄
        self.trade_history: List[Trade] = []
        self.equity_curve: List[Tuple] = []

        # 時間追蹤
        self.last_refresh_time = None
        self.last_long_price = None
        self.last_short_price = None

        # 裝死模式追蹤
        self.long_dead_mode = False
        self.short_dead_mode = False

        # 初始化網格
        initial_price = self.df['close'].iloc[0]
        self._init_orders(initial_price)

    def _init_orders(self, price: float):
        """初始化網格訂單"""
        if self.config.direction in ["long", "both"]:
            self._place_long_orders(price)
        if self.config.direction in ["short", "both"]:
            self._place_short_orders(price)

    def _place_long_orders(self, current_price: float):
        """
        多頭網格：上方止盈(小間距)，下方補倉(大間距)
        """
        self.orders["long"] = [
            (current_price * (1 - self.long_settings["down_spacing"]), "BUY"),
            (current_price * (1 + self.long_settings["up_spacing"]), "SELL")
        ]
        self.last_long_price = current_price

    def _place_short_orders(self, current_price: float):
        """
        空頭網格：上方補倉(大間距)，下方止盈(小間距)
        """
        self.orders["short"] = [
            (current_price * (1 + self.short_settings["up_spacing"]), "SELL_SHORT"),
            (current_price * (1 - self.short_settings["down_spacing"]), "COVER_SHORT")
        ]
        self.last_short_price = current_price

    def _refresh_orders_if_needed(self, price: float, current_time: datetime):
        """定期刷新網格"""
        if self.last_refresh_time is None:
            self.last_refresh_time = current_time
            return

        interval = timedelta(minutes=self.config.grid_refresh_interval)
        if (current_time - self.last_refresh_time) >= interval:
            if self.config.direction in ["long", "both"]:
                self._place_long_orders(price)
            if self.config.direction in ["short", "both"]:
                self._place_short_orders(price)
            self.last_refresh_time = current_time

    def _calculate_unrealized_pnl(self, price: float) -> float:
        """計算未實現盈虧"""
        long_pnl = sum(
            (price - pos.entry_price) * pos.quantity
            for pos in self.long_positions
        )
        short_pnl = sum(
            (pos.entry_price - price) * pos.quantity
            for pos in self.short_positions
        )
        return long_pnl + short_pnl

    def _get_available_margin(self) -> float:
        """計算可用保證金"""
        used_margin = sum(pos.margin for pos in self.long_positions + self.short_positions)
        return self.balance - used_margin

    def _process_long_orders(self, price: float, timestamp: datetime, available_margin: float) -> float:
        """
        處理多頭訂單 - 使用 GridStrategy 統一邏輯

        整合裝死模式：
        - 持倉超過 position_threshold 時停止補倉
        - 使用特殊止盈價格
        - 止盈數量可能加倍
        """
        effective_value = self.config.order_value * self.config.leverage
        base_qty = effective_value / price

        # 計算當前持倉量
        long_position = sum(pos.quantity for pos in self.long_positions)
        short_position = sum(pos.quantity for pos in self.short_positions)

        # 使用純層 decision helpers 獲取決策（取代已刪的舊策略模組）
        decision = _legacy_grid_decision(
            self.last_long_price or price, long_position, short_position,
            self.config, 'long', base_qty)

        self.long_dead_mode = decision['dead_mode']
        tp_price = decision['tp_price']
        entry_price = decision['entry_price']
        tp_qty = decision['tp_qty']

        # 補倉邏輯 (非裝死模式)
        if not decision['dead_mode'] and entry_price and price <= entry_price:
            qty = base_qty
            margin_required = (qty * price) / self.config.leverage
            fee_cost = qty * price * (self.config.fee_pct / 2)

            if (margin_required + fee_cost) <= available_margin:
                self.balance -= (margin_required + fee_cost)
                self.long_positions.append(Position(
                    entry_price=price,
                    quantity=qty,
                    margin=margin_required,
                    side="long",
                    entry_time=timestamp
                ))

                unrealized = self._calculate_unrealized_pnl(price)
                equity = self.balance + unrealized

                self.trade_history.append(Trade(
                    timestamp=timestamp,
                    action="BUY",
                    price=price,
                    quantity=qty,
                    side="LONG",
                    pnl=0.0,
                    fee=fee_cost,
                    gross_pnl=0.0,
                    unrealized_pnl=unrealized,
                    equity=equity
                ))

                self.last_long_price = price
                return available_margin - margin_required - fee_cost

        # 止盈邏輯 (兩種模式都執行)
        if self.long_positions and price >= tp_price:
            # 根據止盈數量決定平倉多少
            remaining_tp = tp_qty
            total_pnl = 0

            while self.long_positions and remaining_tp > 0:
                pos = self.long_positions[0]
                if pos.quantity <= remaining_tp:
                    # 全部平倉
                    self.long_positions.pop(0)
                    fee_cost = pos.quantity * price * (self.config.fee_pct / 2)
                    gross_pnl = (price - pos.entry_price) * pos.quantity
                    net_pnl = gross_pnl - fee_cost
                    self.balance += pos.margin + net_pnl
                    total_pnl += net_pnl

                    self.trade_history.append(Trade(
                        timestamp=timestamp,
                        action="SELL",
                        price=price,
                        quantity=pos.quantity,
                        side="LONG",
                        pnl=net_pnl,
                        fee=fee_cost,
                        gross_pnl=gross_pnl,
                        unrealized_pnl=self._calculate_unrealized_pnl(price),
                        equity=self.balance + self._calculate_unrealized_pnl(price)
                    ))

                    remaining_tp -= pos.quantity
                    available_margin += pos.margin + net_pnl
                else:
                    # 部分平倉
                    close_ratio = remaining_tp / pos.quantity
                    close_qty = remaining_tp
                    close_margin = pos.margin * close_ratio
                    fee_cost = close_qty * price * (self.config.fee_pct / 2)
                    gross_pnl = (price - pos.entry_price) * close_qty
                    net_pnl = gross_pnl - fee_cost
                    self.balance += close_margin + net_pnl
                    total_pnl += net_pnl

                    self.trade_history.append(Trade(
                        timestamp=timestamp,
                        action="SELL",
                        price=price,
                        quantity=close_qty,
                        side="LONG",
                        pnl=net_pnl,
                        fee=fee_cost,
                        gross_pnl=gross_pnl,
                        unrealized_pnl=self._calculate_unrealized_pnl(price),
                        equity=self.balance + self._calculate_unrealized_pnl(price)
                    ))

                    pos.quantity -= close_qty
                    pos.margin -= close_margin
                    available_margin += close_margin + net_pnl
                    remaining_tp = 0

            self.last_long_price = price

        return available_margin

    def _process_short_orders(self, price: float, timestamp: datetime, available_margin: float) -> float:
        """
        處理空頭訂單 - 使用 GridStrategy 統一邏輯

        整合裝死模式：
        - 持倉超過 position_threshold 時停止補倉
        - 使用特殊止盈價格
        - 止盈數量可能加倍
        """
        effective_value = self.config.order_value * self.config.leverage
        base_qty = effective_value / price

        # 計算當前持倉量
        long_position = sum(pos.quantity for pos in self.long_positions)
        short_position = sum(pos.quantity for pos in self.short_positions)

        # 使用純層 decision helpers 獲取決策（取代已刪的舊策略模組）
        decision = _legacy_grid_decision(
            self.last_short_price or price, short_position, long_position,
            self.config, 'short', base_qty)

        self.short_dead_mode = decision['dead_mode']
        tp_price = decision['tp_price']
        entry_price = decision['entry_price']
        tp_qty = decision['tp_qty']

        # 補倉邏輯 (非裝死模式)
        if not decision['dead_mode'] and entry_price and price >= entry_price:
            qty = base_qty
            margin_required = (qty * price) / self.config.leverage
            fee_cost = qty * price * (self.config.fee_pct / 2)

            if (margin_required + fee_cost) <= available_margin:
                self.balance -= (margin_required + fee_cost)
                self.short_positions.append(Position(
                    entry_price=price,
                    quantity=qty,
                    margin=margin_required,
                    side="short",
                    entry_time=timestamp
                ))

                unrealized = self._calculate_unrealized_pnl(price)
                equity = self.balance + unrealized

                self.trade_history.append(Trade(
                    timestamp=timestamp,
                    action="SELL_SHORT",
                    price=price,
                    quantity=qty,
                    side="SHORT",
                    pnl=0.0,
                    fee=fee_cost,
                    gross_pnl=0.0,
                    unrealized_pnl=unrealized,
                    equity=equity
                ))

                self.last_short_price = price
                return available_margin - margin_required - fee_cost

        # 止盈邏輯 (兩種模式都執行)
        if self.short_positions and price <= tp_price:
            # 根據止盈數量決定平倉多少
            remaining_tp = tp_qty
            total_pnl = 0

            while self.short_positions and remaining_tp > 0:
                pos = self.short_positions[0]
                if pos.quantity <= remaining_tp:
                    # 全部平倉
                    self.short_positions.pop(0)
                    fee_cost = pos.quantity * price * (self.config.fee_pct / 2)
                    gross_pnl = (pos.entry_price - price) * pos.quantity
                    net_pnl = gross_pnl - fee_cost
                    self.balance += pos.margin + net_pnl
                    total_pnl += net_pnl

                    self.trade_history.append(Trade(
                        timestamp=timestamp,
                        action="COVER_SHORT",
                        price=price,
                        quantity=pos.quantity,
                        side="SHORT",
                        pnl=net_pnl,
                        fee=fee_cost,
                        gross_pnl=gross_pnl,
                        unrealized_pnl=self._calculate_unrealized_pnl(price),
                        equity=self.balance + self._calculate_unrealized_pnl(price)
                    ))

                    remaining_tp -= pos.quantity
                    available_margin += pos.margin + net_pnl
                else:
                    # 部分平倉
                    close_ratio = remaining_tp / pos.quantity
                    close_qty = remaining_tp
                    close_margin = pos.margin * close_ratio
                    fee_cost = close_qty * price * (self.config.fee_pct / 2)
                    gross_pnl = (pos.entry_price - price) * close_qty
                    net_pnl = gross_pnl - fee_cost
                    self.balance += close_margin + net_pnl
                    total_pnl += net_pnl

                    self.trade_history.append(Trade(
                        timestamp=timestamp,
                        action="COVER_SHORT",
                        price=price,
                        quantity=close_qty,
                        side="SHORT",
                        pnl=net_pnl,
                        fee=fee_cost,
                        gross_pnl=gross_pnl,
                        unrealized_pnl=self._calculate_unrealized_pnl(price),
                        equity=self.balance + self._calculate_unrealized_pnl(price)
                    ))

                    pos.quantity -= close_qty
                    pos.margin -= close_margin
                    available_margin += close_margin + net_pnl
                    remaining_tp = 0

            self.last_short_price = price

        return available_margin

    def run(self) -> BacktestResult:
        """
        執行回測 - 與終端 UI (as_terminal_max.py) 完全一致的邏輯

        Returns:
            BacktestResult: 回測結果
        """
        # 使用終端 UI 兼容模式
        if self.config.terminal_ui_mode and self.config.initial_quantity > 0:
            return self._run_terminal_ui_mode()
        else:
            return self._run_legacy_mode()

    def _build_bundle(self) -> ManagerBundle:
        """回測用真 manager 實例，全增強關閉 → build_snapshot 回傳中性間距/bias。
        與 bot._build_bundle 同結構，保證回測與實盤決策同源。"""
        return ManagerBundle(
            leading_indicator=LeadingIndicatorManager(),
            dynamic_grid_manager=self._dgm,
            glft_controller=GLFTController(),
            funding_manager=None,          # → funding bias = 1.0
            max_enhancement=self._max_enh,  # 全關 → is_feature_enabled 恆 False
            leading_enabled=False,          # → 跳過 leading 區塊
        )

    def _run_terminal_ui_mode(self) -> BacktestResult:
        """終端 UI 兼容模式：決策吃純層 decide()，追價語意 + sim-clock 餵真 manager。

        每根 K 線：set_clock → update_price → 先結算既有掛單成交 → build_snapshot →
        decide() → 依 should_adjust/cancel_side/orders 重掛 pending（錨在觸發時價）。
        clock 於 finally reset，不污染其他測試的實盤時鐘。
        """
        cfg = self.config
        sym = cfg.symbol
        initial_quantity = cfg.initial_quantity
        leverage = cfg.leverage
        fee_pct = cfg.fee_pct
        position_threshold = initial_quantity * cfg.threshold_multiplier
        position_limit = initial_quantity * cfg.limit_multiplier

        # 回測用 manager（sim-clock 驅動；此處增強全關故僅承載 update_price 歷史）
        self._dgm = DynamicGridManager()
        self._max_enh = MaxEnhancement()
        bundle = self._build_bundle()

        balance = cfg.initial_balance
        funding_paid = 0.0
        # funding settlements：(epoch_sec, rate) 排序；pointer 掃過已結算的
        settlements = []
        if cfg.funding_enabled:
            fmap = self.funding_map
            if fmap is None:
                try:
                    from .data_loader import DataLoader
                    fmap = DataLoader().load_funding(
                        sym, self.df["open_time"].iloc[0], self.df["open_time"].iloc[-1])
                except Exception:
                    fmap = {}
            settlements = sorted((int(k), float(v)) for k, v in fmap.items())
        fund_i = 0
        first_epoch = (self.df["open_time"].iloc[0].timestamp()
                       if len(self.df) and hasattr(self.df["open_time"].iloc[0], "timestamp")
                       else 0.0)
        # 略過回測起點之前的 settlement（不對開跑前的時間收費）
        while fund_i < len(settlements) and settlements[fund_i][0] < first_epoch:
            fund_i += 1

        max_equity = balance
        long_positions: list = []
        short_positions: list = []
        trades: list = []
        equity_curve: list = []

        # pending 掛單狀態：每側 entry/tp 各為 {"price","qty"} 或 None，驅動 should_adjust
        pend = {"long": {"entry": None, "tp": None},
                "short": {"entry": None, "tp": None}}
        anchor = {"long": 0.0, "short": 0.0}   # last_grid_price_*（上次掛網價）
        dead = {"long": False, "short": False}

        def _open(side: str, fill_price: float, qty: float) -> bool:
            nonlocal balance
            fill_price = apply_slippage(fill_price, side, "entry", cfg.slippage_bps)
            margin = (qty * fill_price) / leverage
            fee = qty * fill_price * fee_pct
            if margin + fee < balance:
                balance -= (margin + fee)
                (long_positions if side == "long" else short_positions).append(
                    {"price": fill_price, "qty": qty, "margin": margin})
                return True
            return False

        def _close(side: str, fill_price: float, tp_qty: float, ts) -> None:
            nonlocal balance
            fill_price = apply_slippage(fill_price, side, "tp", cfg.slippage_bps)
            positions = long_positions if side == "long" else short_positions
            remaining = tp_qty
            while positions and remaining > 0:
                pos = positions[0]
                if pos["qty"] <= remaining:
                    positions.pop(0)
                    gross = ((fill_price - pos["price"]) if side == "long"
                             else (pos["price"] - fill_price)) * pos["qty"]
                    fee = pos["qty"] * fill_price * fee_pct
                    net = gross - fee
                    balance += pos["margin"] + net
                    trades.append({"pnl": net, "type": side, "timestamp": ts})
                    remaining -= pos["qty"]
                else:
                    ratio = remaining / pos["qty"]
                    close_margin = pos["margin"] * ratio
                    gross = ((fill_price - pos["price"]) if side == "long"
                             else (pos["price"] - fill_price)) * remaining
                    fee = remaining * fill_price * fee_pct
                    net = gross - fee
                    balance += close_margin + net
                    trades.append({"pnl": net, "type": side, "timestamp": ts})
                    pos["qty"] -= remaining
                    pos["margin"] -= close_margin
                    remaining = 0

        def _settle(side: str, bar_low: float, bar_high: float, ts) -> None:
            """結算既有 pending（上一根掛的單）對本根 K 線的成交。

            穿越用 high/low 判定（限價單盤中觸及即成交）；成交價一律是掛單價。
            close 不再參與撮合——它既不決定有沒有成交，也不決定成交在哪。
            同根雙觸發時 entry 先於 tp（保守：先增加曝險）；_close 走 FIFO，
            故本根新開的倉不會被本根止盈平掉。
            """
            positions = long_positions if side == "long" else short_positions
            e = pend[side]["entry"]
            if e is not None and entry_crossed(side, bar_low, bar_high, e["price"]):
                if _open(side, e["price"], e["qty"]):
                    pend[side]["entry"] = None
            t = pend[side]["tp"]
            if t is not None and positions and tp_crossed(side, bar_low, bar_high, t["price"]):
                _close(side, t["price"], t["qty"], ts)
                pend[side]["tp"] = None

        try:
            for _, row in self.df.iterrows():
                price = row['close']
                timestamp = row.get('open_time', None)

                # 髒資料防禦：價格非正/NaN → 跳過本根（避免除零、污染 pnl）
                if not (isinstance(price, (int, float)) and math.isfinite(price) and price > 0):
                    continue

                # sim-clock 推進：epoch 秒；bar_time 倒流時 manager 用 clock.now() 需容忍
                epoch = timestamp.timestamp() if hasattr(timestamp, "timestamp") else 0.0
                clock.set_clock(lambda t=epoch: t)
                self._dgm.update_price(sym, price)

                # 先結算成交（用上一根掛出的 pending）；穿越判定吃本根 high/low
                bar_high = row['high']
                bar_low = row['low']
                if not (math.isfinite(bar_high) and math.isfinite(bar_low)
                        and bar_low > 0 and bar_high >= bar_low):
                    bar_high = bar_low = price   # 髒 OHLC 退化為 close（保守）
                for side in ("long", "short"):
                    if cfg.direction in (side, "both"):
                        _settle(side, bar_low, bar_high, timestamp)

                # funding 現金流結算：掃過所有 <= 本根 epoch 的 settlement（data-driven，非 8h 網格）
                if settlements and epoch > 0:
                    while fund_i < len(settlements) and settlements[fund_i][0] <= epoch:
                        rate = settlements[fund_i][1]
                        for fside, fpos in (("long", long_positions), ("short", short_positions)):
                            if cfg.direction not in (fside, "both"):
                                continue
                            charge = funding_charge(fpos, rate, fside, price)
                            balance -= charge
                            funding_paid += charge
                        fund_i += 1

                # 組決策輸入（pending 狀態驅動 buy/sell_orders）→ decide()
                long_pos = sum(p["qty"] for p in long_positions)
                short_pos = sum(p["qty"] for p in short_positions)
                snapshot = build_snapshot(
                    bundle, sym, cfg.take_profit_spacing, cfg.grid_spacing)
                inputs = DecisionInputs(
                    price=price,
                    long_position=long_pos,
                    short_position=short_pos,
                    buy_long_orders=1.0 if pend["long"]["entry"] else 0.0,
                    sell_long_orders=1.0 if pend["long"]["tp"] else 0.0,
                    buy_short_orders=1.0 if pend["short"]["tp"] else 0.0,   # 空頭 TP = 買回
                    sell_short_orders=1.0 if pend["short"]["entry"] else 0.0,  # 空頭進場 = 賣
                    last_grid_price_long=anchor["long"],
                    last_grid_price_short=anchor["short"],
                    long_dead_mode=dead["long"],
                    short_dead_mode=dead["short"],
                    grid_spacing=cfg.grid_spacing,
                    take_profit_spacing=cfg.take_profit_spacing,
                    initial_quantity=initial_quantity,
                    position_threshold=position_threshold,
                    position_limit=position_limit,
                    glft_enabled=self._max_enh.is_feature_enabled('glft'),
                    gamma=self._max_enh.gamma,
                    enh=snapshot,
                )
                decision = decide(inputs)

                # 依純層決策重掛 pending（追價：cancel_side 清舊、依 orders 掛新、錨在觸發價）
                for side, sd in (("long", decision.long), ("short", decision.short)):
                    if cfg.direction not in (side, "both"):
                        continue
                    if not sd.should_adjust:
                        continue
                    if sd.enter_dead_mode:
                        dead[side] = True
                    if sd.exit_dead_mode:
                        dead[side] = False
                    if sd.cancel_side:
                        pend[side]["entry"] = None
                        pend[side]["tp"] = None
                    for o in sd.orders:
                        slot = "tp" if o.reduce_only else "entry"
                        pend[side][slot] = {"price": o.price, "qty": o.quantity}
                    if sd.new_anchor_price is not None:
                        anchor[side] = sd.new_anchor_price

                # 計算淨值
                unrealized = sum((price - p["price"]) * p["qty"] for p in long_positions)
                unrealized += sum((p["price"] - price) * p["qty"] for p in short_positions)
                equity = balance + unrealized
                max_equity = max(max_equity, equity)
                equity_curve.append((timestamp, price, equity))
        finally:
            clock.reset_clock()  # 絕不殘留 sim-clock 給後續（實盤/其他測試）

        # 計算結果（final_price 取最後一根有效收盤價，避免 NaN 尾根污染 final_equity）
        final_price = equity_curve[-1][1] if equity_curve else self.config.initial_balance
        unrealized_pnl = sum((final_price - p["price"]) * p["qty"] for p in long_positions)
        unrealized_pnl += sum((p["price"] - final_price) * p["qty"] for p in short_positions)

        realized_pnl = sum(t["pnl"] for t in trades)
        final_equity = balance + unrealized_pnl

        winning = [t for t in trades if t["pnl"] > 0]
        losing = [t for t in trades if t["pnl"] < 0]

        # 計算 Sharpe Ratio (基於權益曲線收益率，年化)
        # 正確公式: Sharpe = (平均收益率 / 收益率標準差) × √(年化因子)
        sharpe_ratio = 0.0
        if len(equity_curve) > 1:
            import numpy as np
            # 計算逐期收益率
            returns = []
            for i in range(1, len(equity_curve)):
                prev_equity = equity_curve[i - 1][2]  # equity_curve 是 (timestamp, price, equity)
                curr_equity = equity_curve[i][2]
                if prev_equity > 0:
                    returns.append((curr_equity - prev_equity) / prev_equity)

            if len(returns) > 1:
                mean_return = np.mean(returns)
                std_return = np.std(returns, ddof=1)

                if std_return > 0:
                    # 1 分鐘 K 線，一年約 525600 分鐘
                    periods_per_year = 525600
                    sharpe_ratio = (mean_return / std_return) * np.sqrt(periods_per_year)

        return BacktestResult(
            final_equity=final_equity,
            return_pct=(final_equity - self.config.initial_balance) / self.config.initial_balance,
            max_drawdown=1 - (min(e[2] for e in equity_curve) / max_equity) if equity_curve else 0,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            total_pnl=realized_pnl + unrealized_pnl,
            trades_count=len(trades),
            win_rate=len(winning) / len(trades) if trades else 0,
            profit_factor=sum(t["pnl"] for t in winning) / abs(sum(t["pnl"] for t in losing)) if losing else float('inf'),
            sharpe_ratio=sharpe_ratio,
            direction=self.config.direction,
            config=self.config,
            trade_history=[],  # 簡化版不記錄詳細交易歷史
            equity_curve=equity_curve,
            notes=FIDELITY_NOTES,
            funding_paid=funding_paid,
        )

    def _run_legacy_mode(self) -> BacktestResult:
        """
        舊版模式 - 保留原有邏輯以向後兼容
        """
        final_price = self.df['close'].iloc[-1]

        for _, row in self.df.iterrows():
            # 檢查持倉上限
            total_positions = len(self.long_positions) + len(self.short_positions)
            if total_positions >= self.config.max_positions:
                break

            price = row['close']
            timestamp = row['open_time']

            # 定期刷新網格
            self._refresh_orders_if_needed(price, timestamp)

            # 取得可用保證金
            available_margin = self._get_available_margin()

            # 處理多頭訂單
            if self.config.direction in ["long", "both"]:
                available_margin = self._process_long_orders(price, timestamp, available_margin)

            # 處理空頭訂單
            if self.config.direction in ["short", "both"]:
                available_margin = self._process_short_orders(price, timestamp, available_margin)

            # 計算當前淨值
            unrealized_pnl = self._calculate_unrealized_pnl(price)
            equity = self.balance + unrealized_pnl
            self.max_equity = max(self.max_equity, equity)

            # 記錄淨值曲線
            realized_pnl = sum(t.pnl for t in self.trade_history if t.pnl != 0)
            self.equity_curve.append((timestamp, price, equity, realized_pnl, unrealized_pnl))

            # 檢查最大回撤
            drawdown = 1 - (equity / self.max_equity) if self.max_equity > 0 else 0
            if drawdown >= self.config.max_drawdown:
                break

            final_price = price

        return self._generate_result(final_price)

    def _generate_result(self, final_price: float) -> BacktestResult:
        """生成回測結果"""
        # 計算盈虧
        unrealized_pnl = self._calculate_unrealized_pnl(final_price)
        realized_pnl = sum(t.pnl for t in self.trade_history if t.pnl != 0)
        final_equity = self.balance + unrealized_pnl

        # 計算勝率
        winning_trades = [t for t in self.trade_history if t.pnl > 0]
        losing_trades = [t for t in self.trade_history if t.pnl < 0]
        total_closed = len(winning_trades) + len(losing_trades)
        win_rate = len(winning_trades) / total_closed if total_closed > 0 else 0

        # 計算盈虧比
        total_profit = sum(t.pnl for t in winning_trades) if winning_trades else 0
        total_loss = abs(sum(t.pnl for t in losing_trades)) if losing_trades else 0
        profit_factor = total_profit / total_loss if total_loss > 0 else float('inf')

        # 計算 Sharpe Ratio (修正版 - 根據實際數據時間跨度年化)
        if len(self.equity_curve) > 1:
            returns = []
            for i in range(1, len(self.equity_curve)):
                prev_equity = self.equity_curve[i-1][2]
                curr_equity = self.equity_curve[i][2]
                if prev_equity > 0:
                    returns.append((curr_equity - prev_equity) / prev_equity)

            if returns and len(returns) > 1:
                import statistics
                avg_return = statistics.mean(returns)
                std_return = statistics.stdev(returns)

                if std_return > 0:
                    # 計算實際時間跨度 (分鐘)
                    start_time = self.equity_curve[0][0]
                    end_time = self.equity_curve[-1][0]
                    if hasattr(start_time, 'timestamp'):
                        # datetime 對象
                        duration_minutes = (end_time - start_time).total_seconds() / 60
                    else:
                        # 假設是 timestamp (毫秒)
                        duration_minutes = (end_time - start_time) / 60000

                    # 計算每個週期的長度 (分鐘)
                    periods = len(returns)
                    minutes_per_period = duration_minutes / periods if periods > 0 else 1

                    # 年化因子: 一年有多少個這樣的週期
                    # 1年 = 365天 * 24小時 * 60分鐘 = 525,600 分鐘
                    periods_per_year = 525600 / minutes_per_period if minutes_per_period > 0 else 525600

                    # Sharpe Ratio = (平均收益 / 標準差) * sqrt(年化因子)
                    sharpe_ratio = (avg_return / std_return) * (periods_per_year ** 0.5)
                else:
                    sharpe_ratio = 0
            else:
                sharpe_ratio = 0
        else:
            sharpe_ratio = 0

        return BacktestResult(
            final_equity=final_equity,
            return_pct=(final_equity - self.config.initial_balance) / self.config.initial_balance,
            max_drawdown=1 - final_equity / self.max_equity if self.max_equity > 0 else 0,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            total_pnl=realized_pnl + unrealized_pnl,
            trades_count=len(self.trade_history),
            win_rate=win_rate,
            profit_factor=profit_factor,
            sharpe_ratio=sharpe_ratio,
            direction=self.config.direction,
            config=self.config,
            trade_history=self.trade_history,
            equity_curve=self.equity_curve
        )

    def get_trade_df(self) -> pd.DataFrame:
        """取得交易記錄 DataFrame"""
        if not self.trade_history:
            return pd.DataFrame()

        return pd.DataFrame([
            {
                "timestamp": t.timestamp,
                "action": t.action,
                "price": t.price,
                "quantity": t.quantity,
                "side": t.side,
                "pnl": t.pnl,
                "fee": t.fee,
                "equity": t.equity
            }
            for t in self.trade_history
        ])

    def get_equity_df(self) -> pd.DataFrame:
        """取得淨值曲線 DataFrame"""
        if not self.equity_curve:
            return pd.DataFrame()

        return pd.DataFrame(
            self.equity_curve,
            columns=["timestamp", "price", "equity", "realized_pnl", "unrealized_pnl"]
        )
