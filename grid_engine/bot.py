"""
MAX 網格交易機器人
"""

import asyncio
import dataclasses
import json
import math
import os
import time
from datetime import datetime
from typing import List, Dict

import ccxt

from .utils import logger
from .decision import decide, DecisionInputs
from .snapshot import ManagerBundle, build_snapshot
from .enhancements import (
    FundingRateManager, GLFTController, DynamicGridManager,
    UCBBanditOptimizer, DGTBoundaryManager, LeadingIndicatorManager
)
from .config import GlobalConfig, SymbolConfig
from .state import GlobalState, SymbolState
from .notifier import TelegramNotifier
from .context import ExchangeContext
from .locks import SymbolLocks
from .rest_gateway import RestGateway
# ORDER_* 常數為向後相容 re-export（tests/test_order_guard.py 從 bot 匯入）
from .order_executor import (
    OrderExecutor,
    ORDER_BACKOFF_BASE, ORDER_BACKOFF_CAP,
    ORDER_CIRCUIT_THRESHOLD, ORDER_CIRCUIT_COOLDOWN,
)
from .risk_monitor import RiskMonitor
from .reporting import DailyReporter
from .sync_service import SyncService
from .ws_client import WsClient


def _create_exchange(exchange_id: str, config: dict):
    """動態建立 ccxt exchange 實例"""
    exchange_cls = getattr(ccxt, exchange_id, ccxt.binance)

    # 動態建立帶 custom fetch 的子類
    class CustomExchange(exchange_cls):
        def fetch(self, url, method='GET', headers=None, body=None):
            if headers is None:
                headers = {}
            return super().fetch(url, method, headers, body)

    return CustomExchange(config)


# 向後相容：原本程式碼可能直接引用 CustomExchange
CustomExchange = type('CustomExchange', (ccxt.binance,), {
    'fetch': lambda self, url, method='GET', headers=None, body=None:
        super(type(self), self).fetch(url, method, headers or {}, body)
})


class MaxGridBot:
    """MAX 版本網格機器人 - 整合學術模型增強功能"""

    def __init__(self, config: GlobalConfig):
        """組合根。組裝順序（硬約束）：
        ctx/locks/gateway → notifier → order_executor → risk_monitor/reporter
        → sync_service（需 risk_monitor）→ ws_client（handlers 綁 bot bound method）。
        gateway/locks/ctx/_stop_event/tasks 全組件共享單一實例（見 test_components 組裝斷言）。
        """
        self.config = config
        self.state = GlobalState()

        # 共享基礎設施（兩階段初始化容器 / per-symbol 鎖註冊表 / 單 worker REST）
        self.ctx = ExchangeContext()
        self.locks = SymbolLocks()
        self.gateway = RestGateway()

        for symbol, sym_cfg in config.symbols.items():
            if sym_cfg.enabled:
                self.state.symbols[sym_cfg.ccxt_symbol] = SymbolState(symbol=sym_cfg.ccxt_symbol)

        self.tasks: List[asyncio.Task] = []
        self._stop_event = asyncio.Event()

        # Telegram 通知
        self.notifier = TelegramNotifier(
            bot_token=config.telegram_bot_token,
            chat_id=config.telegram_chat_id,
            switch_on=getattr(config, "telegram_enabled", True),
        )
        # 下單/撤單執行組件（斷路器/退避狀態在 executor 內；tasks 傳共享參照）
        self.order_executor = OrderExecutor(
            gateway=self.gateway, ctx=self.ctx, state=self.state,
            notifier=self.notifier, config=self.config, locks=self.locks,
            stop_event=self._stop_event, tasks=self.tasks,
        )
        # 風控組件（追蹤止盈/減倉/警報冷卻）與每日損益排程
        self.risk_monitor = RiskMonitor(
            config=self.config, state=self.state,
            order_executor=self.order_executor, notifier=self.notifier,
        )
        self.reporter = DailyReporter(
            config=self.config, state=self.state,
            notifier=self.notifier, stop_event=self._stop_event,
        )
        # REST 同步組件（建構順序硬約束：需要 RiskMonitor 實例）
        self.sync_service = SyncService(
            gateway=self.gateway, ctx=self.ctx, config=self.config,
            state=self.state, locks=self.locks, notifier=self.notifier,
            risk_monitor=self.risk_monitor, tasks=self.tasks,
        )
        # WS 純傳輸組件（handlers 引用 bot bound method，callback 不包 try——
        # ticker 例外必須冒泡到 WsClient 重連迴圈）
        self.ws_client = WsClient(
            gateway=self.gateway, ctx=self.ctx, config=self.config,
            state=self.state, stop_event=self._stop_event,
            handlers={
                'bookTicker': self._handle_ticker,
                'ACCOUNT_UPDATE': self._handle_account_update,
                'ORDER_TRADE_UPDATE': self._handle_order_update,
            },
        )

        self.last_order_times: Dict[str, float] = {}

        # MAX 增強模組
        self.glft_controller = GLFTController()
        self.dynamic_grid_manager = DynamicGridManager()

        # 學習模組 (Bandit + DGT)
        self.bandit_optimizer = UCBBanditOptimizer(config.bandit)
        self._bandit_state_path = None
        self._bandit_last_saved_pulls = 0
        self.dgt_manager = DGTBoundaryManager(config.dgt)

        # 領先指標系統
        self.leading_indicator = LeadingIndicatorManager(config.leading_indicator)

        logger.info(f"[MAX] 初始化完成 - Bandit: {config.bandit.enabled}, Leading: {config.leading_indicator.enabled}")

    @property
    def exchange(self):
        return self.ctx.exchange

    @exchange.setter
    def exchange(self, value):
        self.ctx.exchange = value

    @property
    def precisions(self):
        return self.ctx.precisions

    @precisions.setter
    def precisions(self, value):
        self.ctx.precisions = value

    @property
    def funding_manager(self):
        return self.ctx.funding_manager

    @funding_manager.setter
    def funding_manager(self, value):
        self.ctx.funding_manager = value

    def _init_exchange(self):
        exchange_config = {
            "apiKey": self.config.api_key,
            "secret": self.config.api_secret,
            "options": {"defaultType": "future"},
        }
        if self.config.api_password:
            exchange_config["password"] = self.config.api_password
        self.exchange = _create_exchange(self.config.exchange_id, exchange_config)
        if self.config.sandbox_mode:
            self.exchange.set_sandbox_mode(True)
        if self.config.api_url_override:
            for key in self.exchange.urls.get("api", {}):
                self.exchange.urls["api"][key] = self.config.api_url_override
        self.exchange.load_markets(reload=False)

        self.funding_manager = FundingRateManager(self.exchange)

        markets = self.exchange.fetch_markets()
        for sym_config in self.config.symbols.values():
            if not sym_config.enabled:
                continue

            try:
                symbol_info = next(m for m in markets if m["symbol"] == sym_config.ccxt_symbol)
                price_prec = symbol_info["precision"]["price"]
                self.precisions[sym_config.ccxt_symbol] = {
                    "price": int(abs(math.log10(price_prec))) if isinstance(price_prec, float) else price_prec,
                    "amount": int(abs(math.log10(symbol_info["precision"]["amount"]))) if isinstance(symbol_info["precision"]["amount"], float) else symbol_info["precision"]["amount"],
                    "min_amount": symbol_info["limits"]["amount"]["min"]
                }
            except Exception as e:
                logger.error(f"獲取 {sym_config.ccxt_symbol} 精度失敗: {e}")

    def _check_hedge_mode(self):
        for sym_config in self.config.symbols.values():
            if sym_config.enabled:
                try:
                    mode = self.exchange.fetch_position_mode(symbol=sym_config.ccxt_symbol)
                    if not mode['hedged']:
                        self.exchange.fapiPrivatePostPositionSideDual({'dualSidePosition': 'true'})
                        break
                except Exception:
                    pass

    def _build_bundle(self, sym_config: SymbolConfig) -> ManagerBundle:
        """組現有 manager 實例成 ManagerBundle，供 build_snapshot 共用（回測/實盤同源）。"""
        return ManagerBundle(
            leading_indicator=self.leading_indicator,
            dynamic_grid_manager=self.dynamic_grid_manager,
            glft_controller=self.glft_controller,
            funding_manager=self.funding_manager,
            max_enhancement=self.config.max_enhancement,
            leading_enabled=self.config.leading_indicator.enabled,
        )

    def _build_inputs(self, sym_config: SymbolConfig, sym_state: SymbolState,
                      snapshot) -> DecisionInputs:
        """從 sym_config / sym_state / snapshot 組出純層決策輸入（無副作用）。
        grid_spacing/take_profit_spacing 取 bandit 覆寫後的 sym_config 值。"""
        max_cfg = self.config.max_enhancement
        return DecisionInputs(
            price=sym_state.latest_price,
            long_position=sym_state.long_position,
            short_position=sym_state.short_position,
            buy_long_orders=sym_state.buy_long_orders,
            sell_long_orders=sym_state.sell_long_orders,
            buy_short_orders=sym_state.buy_short_orders,
            sell_short_orders=sym_state.sell_short_orders,
            last_grid_price_long=sym_state.last_grid_price_long,
            last_grid_price_short=sym_state.last_grid_price_short,
            long_dead_mode=sym_state.long_dead_mode,
            short_dead_mode=sym_state.short_dead_mode,
            grid_spacing=sym_config.grid_spacing,
            take_profit_spacing=sym_config.take_profit_spacing,
            initial_quantity=sym_config.initial_quantity,
            position_threshold=sym_config.position_threshold,
            position_limit=sym_config.position_limit,
            glft_enabled=max_cfg.is_feature_enabled('glft'),
            gamma=max_cfg.gamma,
            enh=snapshot,
        )

    def _get_adjusted_quantity(
        self,
        sym_config: SymbolConfig,
        sym_state: SymbolState,
        side: str,
        is_take_profit: bool
    ) -> float:
        """獲取調整後的數量"""
        max_cfg = self.config.max_enhancement
        base_qty = sym_config.initial_quantity

        if is_take_profit:
            if side == 'long':
                if sym_state.long_position > sym_config.position_limit:
                    base_qty *= 2
                elif sym_state.short_position >= sym_config.position_threshold:
                    base_qty *= 2
            else:
                if sym_state.short_position > sym_config.position_limit:
                    base_qty *= 2
                elif sym_state.long_position >= sym_config.position_threshold:
                    base_qty *= 2

        if not is_take_profit:
            base_qty = self.glft_controller.adjust_order_quantity(
                base_qty, side,
                sym_state.long_position, sym_state.short_position,
                max_cfg
            )

        if self.funding_manager:
            long_bias, short_bias = self.funding_manager.get_position_bias(
                sym_config.ccxt_symbol, max_cfg
            )

            if side == 'long':
                base_qty *= long_bias
            else:
                base_qty *= short_bias

        return max(sym_config.initial_quantity * 0.5, base_qty)

    def _should_adjust_grid(self, sym_config: SymbolConfig, sym_state: SymbolState, side: str) -> bool:
        """檢查是否需要調整網格"""
        price = sym_state.latest_price
        deviation_threshold = sym_config.grid_spacing * 0.5

        if side == 'long':
            if sym_state.buy_long_orders <= 0 or sym_state.sell_long_orders <= 0:
                return True
            if sym_state.last_grid_price_long > 0:
                deviation = abs(price - sym_state.last_grid_price_long) / sym_state.last_grid_price_long
                return deviation >= deviation_threshold
            return True
        else:
            if sym_state.buy_short_orders <= 0 or sym_state.sell_short_orders <= 0:
                return True
            if sym_state.last_grid_price_short > 0:
                deviation = abs(price - sym_state.last_grid_price_short) / sym_state.last_grid_price_short
                return deviation >= deviation_threshold
            return True

    def _grid_cooldown_passed(self, ccxt_symbol: str, side: str) -> bool:
        """有倉位時網格重掛的頻率下限；position_adjust_cooldown=0 表示關閉"""
        cooldown = getattr(self.config, "position_adjust_cooldown", 5.0)
        if cooldown <= 0:
            return True
        last = self.last_order_times.get(f"{ccxt_symbol}_{side}_grid", 0)
        return time.time() - last >= cooldown

    async def adjust_grid(self, ccxt_symbol: str):
        sym_config = None
        for cfg in self.config.symbols.values():
            if cfg.ccxt_symbol == ccxt_symbol and cfg.enabled:
                sym_config = cfg
                break
        if sym_config is None:
            return

        # 忙碌時跳過本 tick（ticker 高頻，排隊只會積壓過期決策）
        lock = self.locks.get(ccxt_symbol)
        if lock.locked():
            return
        async with lock:
            await self._grid_step(ccxt_symbol, sym_config)

    async def _grid_step(self, ccxt_symbol: str, sym_config: SymbolConfig):
        sym_state = self.state.symbols[ccxt_symbol]
        price = sym_state.latest_price
        if price <= 0:
            return

        # === DGT 動態邊界管理 ===
        if self.config.dgt.enabled:
            if ccxt_symbol not in self.dgt_manager.boundaries:
                self.dgt_manager.initialize_boundary(
                    ccxt_symbol, price, sym_config.grid_spacing, num_grids=10
                )

            accumulated = self.dgt_manager.accumulated_profits.get(ccxt_symbol, 0)
            reset, reset_info = self.dgt_manager.check_and_reset(ccxt_symbol, price, accumulated)
            if reset and reset_info:
                logger.info(f"[DGT] {sym_config.symbol} 邊界重置 #{reset_info['reset_count']}: "
                           f"{reset_info['direction']}破, 中心價 {reset_info['old_center']:.4f} → {reset_info['new_center']:.4f}")

        # === Bandit 參數應用 ===
        if self.config.bandit.enabled:
            bandit_params = self.bandit_optimizer.get_current_params()
            sym_config.grid_spacing = bandit_params.grid_spacing
            sym_config.take_profit_spacing = bandit_params.take_profit_spacing
            if self.config.max_enhancement.all_enhancements_enabled:
                self.config.max_enhancement.gamma = bandit_params.gamma

        self.dynamic_grid_manager.update_price(ccxt_symbol, price)

        # 即時更新面板顯示用的動態間距（無倉位時也要刷新）
        sym_state.dynamic_take_profit = sym_config.take_profit_spacing
        sym_state.dynamic_grid_spacing = sym_config.grid_spacing

        await self.risk_monitor.check_and_reduce_positions(sym_config, sym_state)

        # 封鎖期內開倉單必被跳過，無倉位分支撤了單也補不回來 — 直接不動作
        order_blocked = self.order_executor.is_blocked(ccxt_symbol)

        # 有倉位側是否要重掛（純判斷，無 manager 副作用）——決定是否建快照跑純層。
        # 只在至少一側真的要執行時才 build_snapshot，維持 get_signals/ATR 的呼叫時機
        # 與原 _place_grid 一致（should_adjust=False 或 cooldown 未過 → 不觸發 manager）。
        need_long = (sym_state.long_position != 0
                     and self._should_adjust_grid(sym_config, sym_state, 'long')
                     and self._grid_cooldown_passed(ccxt_symbol, 'long'))
        need_short = (sym_state.short_position != 0
                      and self._should_adjust_grid(sym_config, sym_state, 'short')
                      and self._grid_cooldown_passed(ccxt_symbol, 'short'))

        decision = None
        inputs = None
        if need_long or need_short:
            bundle = self._build_bundle(sym_config)
            snapshot = build_snapshot(
                bundle, ccxt_symbol,
                sym_config.take_profit_spacing, sym_config.grid_spacing,
            )
            inputs = self._build_inputs(sym_config, sym_state, snapshot)
            decision = decide(inputs)

        # 多頭
        if sym_state.long_position == 0:
            if not order_blocked and \
                    time.time() - self.last_order_times.get(f"{ccxt_symbol}_long", 0) > 10:
                await self.order_executor.cancel_orders_for_side(ccxt_symbol, 'long')
                qty = self._get_adjusted_quantity(sym_config, sym_state, 'long', False)
                await self.order_executor.place_order(ccxt_symbol, 'buy', sym_state.best_bid, qty, False, 'long')
                self.last_order_times[f"{ccxt_symbol}_long"] = time.time()
                sym_state.last_grid_price_long = price
        elif need_long:
            await self._place_grid(ccxt_symbol, sym_config, 'long', decision.long)
            self.last_order_times[f"{ccxt_symbol}_long_grid"] = time.time()
            sym_state.last_grid_price_long = price

        # 空頭
        if sym_state.short_position == 0:
            if not order_blocked and \
                    time.time() - self.last_order_times.get(f"{ccxt_symbol}_short", 0) > 10:
                await self.order_executor.cancel_orders_for_side(ccxt_symbol, 'short')
                qty = self._get_adjusted_quantity(sym_config, sym_state, 'short', False)
                await self.order_executor.place_order(ccxt_symbol, 'sell', sym_state.best_ask, qty, False, 'short')
                self.last_order_times[f"{ccxt_symbol}_short"] = time.time()
                sym_state.last_grid_price_short = price
        elif need_short:
            await self._place_grid(ccxt_symbol, sym_config, 'short', decision.short)
            self.last_order_times[f"{ccxt_symbol}_short_grid"] = time.time()
            sym_state.last_grid_price_short = price

        if decision is not None:
            self._log_decision(ccxt_symbol, inputs, decision)

    async def _place_grid(self, ccxt_symbol: str, sym_config: SymbolConfig, side: str,
                          side_decision=None):
        """掛出網格訂單 (MAX 版本)：純層薄封裝——build_snapshot → decide → execute。
        維持與原本相同的 place_order/cancel 呼叫序列（characterization 鎖定）。
        side_decision 可由 _grid_step 預先算好傳入，避免同 tick 重複 build_snapshot。"""
        sym_state = self.state.symbols[ccxt_symbol]

        if side == 'long' and sym_state.long_position <= 0:
            logger.debug(f"[Grid] {sym_config.symbol} 多頭無倉位，跳過 _place_grid")
            return
        if side == 'short' and sym_state.short_position <= 0:
            logger.debug(f"[Grid] {sym_config.symbol} 空頭無倉位，跳過 _place_grid")
            return

        if side_decision is None:
            bundle = self._build_bundle(sym_config)
            snapshot = build_snapshot(
                bundle, ccxt_symbol,
                sym_config.take_profit_spacing, sym_config.grid_spacing,
            )
            inputs = self._build_inputs(sym_config, sym_state, snapshot)
            decision = decide(inputs)
            side_decision = decision.long if side == 'long' else decision.short
        await self._execute_side_decision(ccxt_symbol, sym_config, side, side_decision)

    async def _execute_side_decision(self, ccxt_symbol: str, sym_config: SymbolConfig,
                                     side: str, side_decision) -> None:
        """執行純層單側決策：裝死 flag 轉換 → 撤單 → 下單 → 寫回顯示欄位。
        撤/下單走既有 cancel_orders_for_side / place_order 守衛路徑。"""
        sym_state = self.state.symbols[ccxt_symbol]
        my_position = sym_state.long_position if side == 'long' else sym_state.short_position

        if side_decision.enter_dead_mode:
            if side == 'long':
                sym_state.long_dead_mode = True
            else:
                sym_state.short_dead_mode = True
            logger.info(f"[MAX] {sym_config.symbol} {side}頭進入裝死模式 (持倉:{my_position})")
        if side_decision.exit_dead_mode:
            if side == 'long':
                sym_state.long_dead_mode = False
            else:
                sym_state.short_dead_mode = False
            logger.info(f"[MAX] {sym_config.symbol} {side}頭離開裝死模式")

        if side_decision.cancel_side:
            await self.order_executor.cancel_orders_for_side(ccxt_symbol, side)

        for o in side_decision.orders:
            await self.order_executor.place_order(ccxt_symbol, o.side, o.price, o.quantity,
                                   o.reduce_only, o.position_side)

        if side_decision.orders:
            logger.info(f"[MAX] {sym_config.symbol} {side}頭掛單 x{len(side_decision.orders)} "
                        f"[TP:{side_decision.dynamic_tp*100:.2f}%/GS:{side_decision.dynamic_gs*100:.2f}%]")

        # 寫回顯示欄位（不影響交易決策）。dynamic/inventory 恆寫；leading_* 僅在
        # leading 啟用時寫回，維持原行為（關閉時保留上 tick 舊值）。
        sym_state.dynamic_take_profit = side_decision.dynamic_tp
        sym_state.dynamic_grid_spacing = side_decision.dynamic_gs
        disp = side_decision.display
        if 'inventory_ratio' in disp:
            sym_state.inventory_ratio = disp['inventory_ratio']
        if self.config.leading_indicator.enabled:
            sym_state.leading_ofi = disp.get('leading_ofi', sym_state.leading_ofi)
            sym_state.leading_volume_ratio = disp.get('leading_volume_ratio', sym_state.leading_volume_ratio)
            sym_state.leading_spread_ratio = disp.get('leading_spread_ratio', sym_state.leading_spread_ratio)
            sym_state.leading_signals = disp.get('leading_signals', sym_state.leading_signals)

    def _log_decision(self, ccxt_symbol: str, inputs, decision) -> None:
        """落地一行 JSON（inputs + decision）。I/O 失敗只記 log 不拋，絕不中斷交易。"""
        path = getattr(self, "_decision_log_path", None)
        if not path:
            return
        try:
            rec = {
                "ts": time.time(),
                "symbol": ccxt_symbol,
                "inputs": dataclasses.asdict(inputs),
                "decision": dataclasses.asdict(decision),
            }
            line = json.dumps(rec, ensure_ascii=False)
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            logger.error(f"決策日誌寫入失敗 {ccxt_symbol}: {e}")

    async def _handle_ticker(self, data: dict):
        symbol_raw = data.get('s', '')
        bid = float(data.get('b', 0))
        ask = float(data.get('a', 0))

        if not bid or not ask:
            return

        for sym_config in self.config.symbols.values():
            if sym_config.enabled and sym_config.ws_symbol.upper() == symbol_raw:
                ccxt_symbol = sym_config.ccxt_symbol
                state = self.state.symbols.get(ccxt_symbol)
                if state:
                    state.best_bid = bid
                    state.best_ask = ask
                    state.latest_price = (bid + ask) / 2

                    self.leading_indicator.update_spread(ccxt_symbol, bid, ask)

                    await self.adjust_grid(ccxt_symbol)
                break

        await self.sync_service.maybe_sync()

    async def _handle_account_update(self, data: dict):
        """處理 ACCOUNT_UPDATE 事件"""
        try:
            account_data = data.get('a', {})

            balances = account_data.get('B', [])
            for bal in balances:
                asset = bal.get('a', '')
                if asset in ['USDC', 'USDT']:
                    wallet_balance = float(bal.get('wb', 0) or 0)

                    acc = self.state.get_account(asset)
                    acc.wallet_balance = wallet_balance
                    # 注意: ACCOUNT_UPDATE 只帶 wb(錢包)/cw(全倉錢包)，不含
                    # 可用餘額與已用保證金。available_balance / margin_used
                    # 由週期性 REST _sync_account 維護為交易所真值，此處不可覆寫。

                    logger.info(f"[userData] {asset} 錢包餘額更新: {wallet_balance:.2f}")

            for sym_state in self.state.symbols.values():
                sym_state.unrealized_pnl = 0

            positions = account_data.get('P', [])
            for pos in positions:
                symbol_raw = pos.get('s', '')
                position_amt = float(pos.get('pa', 0) or 0)
                unrealized_pnl = float(pos.get('up', 0) or 0)
                position_side = pos.get('ps', '')

                ccxt_symbol = None
                for cfg in self.config.symbols.values():
                    if cfg.symbol == symbol_raw:
                        ccxt_symbol = cfg.ccxt_symbol
                        break

                if ccxt_symbol and ccxt_symbol in self.state.symbols:
                    sym_state = self.state.symbols[ccxt_symbol]

                    if position_side == 'LONG':
                        sym_state.long_position = abs(position_amt)
                    elif position_side == 'SHORT':
                        sym_state.short_position = abs(position_amt)

                    sym_state.unrealized_pnl += unrealized_pnl

                    logger.info(f"[userData] {symbol_raw} {position_side}: "
                               f"持倉={position_amt:.2f}, 浮盈={unrealized_pnl:.2f}")

            for currency in ['USDC', 'USDT']:
                acc = self.state.get_account(currency)
                acc.unrealized_pnl = sum(
                    s.unrealized_pnl for s in self.state.symbols.values()
                    if currency in s.symbol
                )

            self.state.update_totals()

        except Exception as e:
            logger.error(f"[userData] ACCOUNT_UPDATE 處理失敗: {e}")

    async def _handle_order_update(self, data: dict):
        """處理 ORDER_TRADE_UPDATE 事件"""
        try:
            order_data = data.get('o', {})
            symbol_raw = order_data.get('s', '')
            order_status = order_data.get('X', '')
            side = order_data.get('S', '')
            position_side = order_data.get('ps', '')
            realized_pnl = float(order_data.get('rp', 0) or 0)

            ccxt_symbol = None
            for cfg in self.config.symbols.values():
                if cfg.symbol == symbol_raw:
                    ccxt_symbol = cfg.ccxt_symbol
                    break

            if not ccxt_symbol or ccxt_symbol not in self.state.symbols:
                return

            sym_state = self.state.symbols[ccxt_symbol]

            if order_status == 'FILLED':
                sym_state.total_trades += 1
                self.state.total_trades += 1

                exec_price = float(order_data.get('p', 0) or order_data.get('ap', 0) or 0)
                exec_qty = float(order_data.get('q', 0) or 0)
                trade_side_for_ofi = 'buy' if side == 'BUY' else 'sell'
                if exec_price > 0 and exec_qty > 0:
                    self.leading_indicator.record_trade(ccxt_symbol, exec_price, exec_qty, trade_side_for_ofi)

                if realized_pnl != 0:
                    sym_state.total_profit += realized_pnl
                    self.state.total_profit += realized_pnl
                    pnl_sign = "+" if realized_pnl > 0 else ""
                    logger.info(f"[userData] {symbol_raw} 成交! {side} {position_side}, "
                               f"盈虧: {pnl_sign}{realized_pnl:.4f}")

                    trade_side = 'long' if position_side == 'LONG' else 'short'
                    self.bandit_optimizer.record_trade(realized_pnl, trade_side)
                    self._maybe_persist_bandit_state()

                    self.dgt_manager.accumulated_profits[ccxt_symbol] = \
                        self.dgt_manager.accumulated_profits.get(ccxt_symbol, 0) + realized_pnl
                else:
                    logger.info(f"[userData] {symbol_raw} 開倉成交: {side} {position_side}")

                if position_side == 'LONG':
                    if side == 'BUY':
                        sym_state.buy_long_orders = 0
                    else:
                        sym_state.sell_long_orders = 0
                elif position_side == 'SHORT':
                    if side == 'SELL':
                        sym_state.sell_short_orders = 0
                    else:
                        sym_state.buy_short_orders = 0

                await self.adjust_grid(ccxt_symbol)

            elif order_status == 'CANCELED':
                logger.info(f"[userData] {symbol_raw} 訂單取消: {side} {position_side}")

        except Exception as e:
            logger.error(f"[userData] ORDER_TRADE_UPDATE 處理失敗: {e}")

    async def run(self):
        try:
            await self.gateway.call(self._init_exchange)
            await self.gateway.call(self._check_hedge_mode)
            await self.ws_client.acquire_listen_key()

            self.state.running = True
            self.state.start_time = datetime.now()

            # 決策日誌預設路徑：設定檔指定 > logs/decisions.jsonl（已顯式設過則不覆寫）
            if getattr(self, "_decision_log_path", None) is None:
                self._decision_log_path = (
                    getattr(self.config, "decision_log_path", None) or "logs/decisions.jsonl"
                )

            # Bandit 狀態預設路徑 + 啟動載入（已學到的統計跨重啟延續）
            if getattr(self, "_bandit_state_path", None) is None:
                self._bandit_state_path = (
                    getattr(self.config, "bandit_state_path", None) or "logs/bandit_state.json"
                )
            if self.config.bandit.enabled:
                from grid_engine.bandit_persistence import load_bandit_state
                if load_bandit_state(
                    self.bandit_optimizer,
                    self._bandit_state_path,
                    getattr(self.config, "bandit_state_max_age_sec", None),
                ):
                    self._bandit_last_saved_pulls = self.bandit_optimizer.total_pulls

            await self.sync_service.sync_all()
        except Exception as e:
            logger.error(f"[MAX] 初始化失敗: {e}")
            await self.notifier.notify_crash(f"初始化失敗: {e}")
            self.state.running = False
            self.gateway.shutdown()
            return

        self.tasks.extend([
            asyncio.create_task(self.ws_client.run()),
            asyncio.create_task(self.ws_client.keep_alive_loop()),
        ])
        if self.notifier.enabled:
            self.tasks.append(asyncio.create_task(self.reporter.run()))
            self.tasks.append(asyncio.create_task(self.notifier.notify_start(
                symbols=list(self.state.symbols.keys()),
                daily_pnl_hour=self.config.telegram_daily_pnl_hour,
            )))
        else:
            logger.warning(
                "[MAX] Telegram 未設定（telegram_bot_token/telegram_chat_id 為空），"
                "通知與每日報告已停用。可在主選單「連線設定 → Telegram 通知」設定"
            )

        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"[MAX] Bot 意外崩潰: {e}")
            await self.notifier.notify_crash(str(e))
        finally:
            await self.stop()

    def _persist_bandit_state(self):
        """best-effort 存 bandit 狀態；僅 enabled 時存，失敗只 log 不炸主流程。"""
        if not self.config.bandit.enabled:
            return
        path = getattr(self, "_bandit_state_path", None) or "logs/bandit_state.json"
        try:
            from grid_engine.bandit_persistence import save_bandit_state
            save_bandit_state(self.bandit_optimizer, path)
        except Exception as e:
            logger.warning(f"[Bandit] 狀態存檔失敗: {e}")

    def _maybe_persist_bandit_state(self):
        """bandit 評估過（total_pulls 變化）才落地，寫檔次數 = 評估次數。"""
        if self.bandit_optimizer.total_pulls != self._bandit_last_saved_pulls:
            self._persist_bandit_state()
            self._bandit_last_saved_pulls = self.bandit_optimizer.total_pulls

    async def stop(self):
        # 發送停止通知
        try:
            await self.notifier.notify_stop()
        except Exception:
            pass

        # best-effort 收尾：落地最新 bandit 狀態
        self._persist_bandit_state()

        self._stop_event.set()
        self.state.running = False

        # 迭代快照：done-callback 會在 await 期間從 self.tasks 自移除，直接迭代會跳元素
        for task in list(self.tasks):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # 排隊中的 REST 直接取消；in-flight 的自然結束，place_order 入口的停機檢查擋住後續
        self.gateway.shutdown()
