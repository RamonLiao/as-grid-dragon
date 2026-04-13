"""
MAX 網格交易機器人
"""

import asyncio
import json
import math
import ssl
import time
from datetime import datetime
from typing import Optional, List, Dict, Tuple

import ccxt
import certifi
import websockets

from .utils import logger
from .strategy import GridStrategy
from .enhancements import (
    FundingRateManager, GLFTController, DynamicGridManager,
    UCBBanditOptimizer, DGTBoundaryManager, LeadingIndicatorManager
)
from .config import GlobalConfig, SymbolConfig
from .state import GlobalState, SymbolState


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
        self.config = config
        self.state = GlobalState()

        for symbol, sym_cfg in config.symbols.items():
            if sym_cfg.enabled:
                self.state.symbols[sym_cfg.ccxt_symbol] = SymbolState(symbol=sym_cfg.ccxt_symbol)

        self.exchange: Optional[CustomExchange] = None
        self.listen_key: Optional[str] = None
        self.tasks: List[asyncio.Task] = []
        self._stop_event = asyncio.Event()
        self.precisions: Dict[str, dict] = {}
        self.last_sync_time = 0
        self.last_order_times: Dict[str, float] = {}

        # MAX 增強模組
        self.funding_manager: Optional[FundingRateManager] = None
        self.glft_controller = GLFTController()
        self.dynamic_grid_manager = DynamicGridManager()

        # 學習模組 (Bandit + DGT)
        self.bandit_optimizer = UCBBanditOptimizer(config.bandit)
        self.dgt_manager = DGTBoundaryManager(config.dgt)

        # 領先指標系統
        self.leading_indicator = LeadingIndicatorManager(config.leading_indicator)

        logger.info(f"[MAX] 初始化完成 - Bandit: {config.bandit.enabled}, Leading: {config.leading_indicator.enabled}")

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

    def _get_listen_key(self) -> str:
        response = self.exchange.fapiPrivatePostListenKey()
        return response.get("listenKey")

    def sync_all(self):
        self._sync_positions()
        self._sync_orders()
        self._sync_account()
        self._sync_funding_rates()

    def _sync_funding_rates(self):
        """同步所有交易對的 funding rate"""
        if not self.funding_manager:
            return

        for sym_config in self.config.symbols.values():
            if sym_config.enabled:
                rate = self.funding_manager.update_funding_rate(sym_config.ccxt_symbol)
                sym_state = self.state.symbols.get(sym_config.ccxt_symbol)
                if sym_state:
                    sym_state.current_funding_rate = rate

    def _sync_positions(self):
        try:
            positions = self.exchange.fetch_positions(params={'type': 'future'})

            for sym_state in self.state.symbols.values():
                sym_state.long_position = 0
                sym_state.short_position = 0
                sym_state.unrealized_pnl = 0

            for pos in positions:
                symbol = pos['symbol']
                if symbol in self.state.symbols:
                    contracts = pos.get('contracts', 0)
                    side = pos.get('side')
                    pnl = float(pos.get('unrealizedPnl', 0) or 0)

                    if side == 'long':
                        self.state.symbols[symbol].long_position = contracts
                    elif side == 'short':
                        self.state.symbols[symbol].short_position = abs(contracts)

                    self.state.symbols[symbol].unrealized_pnl += pnl

        except Exception as e:
            logger.error(f"同步持倉失敗: {e}")

    def _sync_orders(self):
        for sym_config in self.config.symbols.values():
            if not sym_config.enabled:
                continue
            symbol = sym_config.ccxt_symbol

            try:
                orders = self.exchange.fetch_open_orders(symbol=symbol)
                state = self.state.symbols.get(symbol)
                if not state:
                    continue

                state.buy_long_orders = 0
                state.sell_long_orders = 0
                state.buy_short_orders = 0
                state.sell_short_orders = 0

                for order in orders:
                    qty = abs(float(order.get('info', {}).get('origQty', 0)))
                    side = order.get('side')
                    pos_side = order.get('info', {}).get('positionSide')

                    if side == 'buy' and pos_side == 'LONG':
                        state.buy_long_orders += qty
                    elif side == 'sell' and pos_side == 'LONG':
                        state.sell_long_orders += qty
                    elif side == 'buy' and pos_side == 'SHORT':
                        state.buy_short_orders += qty
                    elif side == 'sell' and pos_side == 'SHORT':
                        state.sell_short_orders += qty
            except Exception as e:
                logger.error(f"同步 {symbol} 掛單失敗: {e}")

    def _sync_account(self):
        try:
            balance = self.exchange.fetch_balance({'type': 'future'})

            for currency in ['USDC', 'USDT']:
                total = float(balance.get('total', {}).get(currency, 0) or 0)
                free = float(balance.get('free', {}).get(currency, 0) or 0)

                acc = self.state.get_account(currency)
                acc.wallet_balance = total
                acc.available_balance = free
                acc.margin_used = total - free if total > free else 0

                unrealized = 0
                for sym_state in self.state.symbols.values():
                    if currency in sym_state.symbol:
                        unrealized += sym_state.unrealized_pnl
                acc.unrealized_pnl = unrealized

            self.state.update_totals()

            self._check_trailing_stop()
        except Exception as e:
            logger.error(f"同步帳戶失敗: {e}")

    def _check_trailing_stop(self):
        """保證金追蹤止盈邏輯"""
        risk = self.config.risk

        if not risk.enabled:
            return

        if self.state.margin_usage < risk.margin_threshold:
            self.state.trailing_active.clear()
            self.state.peak_pnl.clear()
            return

        for sym_config in self.config.symbols.values():
            if not sym_config.enabled:
                continue

            ccxt_symbol = sym_config.ccxt_symbol
            sym_state = self.state.symbols.get(ccxt_symbol)
            if not sym_state:
                continue

            current_pnl = sym_state.unrealized_pnl

            if self.state.trailing_active.get(ccxt_symbol, False):
                peak = self.state.peak_pnl.get(ccxt_symbol, 0)
                if current_pnl > peak:
                    self.state.peak_pnl[ccxt_symbol] = current_pnl
                    logger.info(f"[追蹤止盈] {sym_config.symbol} 新高: {current_pnl:.2f}U")

                peak = self.state.peak_pnl.get(ccxt_symbol, 0)
                drawdown = peak - current_pnl

                trigger = max(risk.trailing_min_drawdown, peak * risk.trailing_drawdown_pct)

                if drawdown >= trigger and peak > 0:
                    logger.info(f"[追蹤止盈] {sym_config.symbol} 觸發! 最高:{peak:.2f}, 當前:{current_pnl:.2f}, 回撤:{drawdown:.2f}")
                    self._close_symbol_positions(ccxt_symbol, sym_config)
                    self.state.trailing_active[ccxt_symbol] = False
                    self.state.peak_pnl[ccxt_symbol] = 0

            else:
                if current_pnl >= risk.trailing_start_profit:
                    self.state.trailing_active[ccxt_symbol] = True
                    self.state.peak_pnl[ccxt_symbol] = current_pnl
                    logger.info(f"[追蹤止盈] {sym_config.symbol} 開始追蹤! 浮盈: {current_pnl:.2f}U")

    def _close_symbol_positions(self, ccxt_symbol: str, sym_config: SymbolConfig):
        """平倉指定交易對"""
        try:
            sym_state = self.state.symbols.get(ccxt_symbol)
            if not sym_state:
                return

            self.cancel_orders_for_side(ccxt_symbol, 'long')
            self.cancel_orders_for_side(ccxt_symbol, 'short')

            if sym_state.long_position > 0:
                self.place_order(
                    ccxt_symbol, 'sell', 0, sym_state.long_position,
                    reduce_only=True, position_side='long', order_type='market'
                )
                logger.info(f"[追蹤止盈] {sym_config.symbol} 市價平多 {sym_state.long_position}")

            if sym_state.short_position > 0:
                self.place_order(
                    ccxt_symbol, 'buy', 0, sym_state.short_position,
                    reduce_only=True, position_side='short', order_type='market'
                )
                logger.info(f"[追蹤止盈] {sym_config.symbol} 市價平空 {sym_state.short_position}")

        except Exception as e:
            logger.error(f"[追蹤止盈] {sym_config.symbol} 平倉失敗: {e}")

    def place_order(self, symbol: str, side: str, price: float, quantity: float,
                    reduce_only: bool = False, position_side: str = None,
                    order_type: str = 'limit'):
        try:
            prec = self.precisions.get(symbol, {"price": 4, "amount": 0, "min_amount": 1})
            price = round(price, prec["price"])
            quantity = round(quantity, prec["amount"])
            quantity = max(quantity, prec["min_amount"])

            params = {'reduce_only': reduce_only}
            if position_side:
                params['positionSide'] = position_side.upper()

            if order_type == 'market':
                return self.exchange.create_order(symbol, 'market', side, quantity, params=params)
            else:
                return self.exchange.create_order(symbol, 'limit', side, quantity, price, params)
        except Exception as e:
            logger.error(f"下單失敗 {symbol}: {e}")
            return None

    def cancel_orders_for_side(self, symbol: str, position_side: str):
        try:
            orders = self.exchange.fetch_open_orders(symbol)
            for order in orders:
                order_side = order.get('side')
                order_pos_side = order.get('info', {}).get('positionSide', 'BOTH')
                reduce_only = order.get('reduceOnly', False)

                should_cancel = False
                if position_side == 'long':
                    if (not reduce_only and order_side == 'buy' and order_pos_side == 'LONG') or \
                       (reduce_only and order_side == 'sell' and order_pos_side == 'LONG'):
                        should_cancel = True
                elif position_side == 'short':
                    if (not reduce_only and order_side == 'sell' and order_pos_side == 'SHORT') or \
                       (reduce_only and order_side == 'buy' and order_pos_side == 'SHORT'):
                        should_cancel = True

                if should_cancel:
                    self.exchange.cancel_order(order['id'], symbol)
        except Exception as e:
            logger.error(f"撤單失敗 {symbol}: {e}")

    def _get_dynamic_spacing(self, sym_config: SymbolConfig, sym_state: SymbolState) -> Tuple[float, float]:
        """獲取動態調整後的間距"""
        max_cfg = self.config.max_enhancement
        ccxt_symbol = sym_config.ccxt_symbol

        base_take_profit = sym_config.take_profit_spacing
        base_grid_spacing = sym_config.grid_spacing

        # === 1. 領先指標調整 ===
        leading_reason = ""
        leading_signals = []
        leading_values = {}

        if self.config.leading_indicator.enabled:
            leading_signals, leading_values = self.leading_indicator.get_signals(ccxt_symbol)

            sym_state.leading_ofi = leading_values.get('ofi', 0)
            sym_state.leading_volume_ratio = leading_values.get('volume_ratio', 1.0)
            sym_state.leading_spread_ratio = leading_values.get('spread_ratio', 1.0)
            sym_state.leading_signals = leading_signals

            should_pause, pause_reason = self.leading_indicator.should_pause_trading(ccxt_symbol)
            if should_pause:
                logger.warning(f"[LeadingIndicator] {sym_config.symbol} 暫停交易: {pause_reason}")
                base_take_profit *= 2.0
                base_grid_spacing *= 2.0
                leading_reason = f"暫停:{pause_reason}"
            elif leading_signals:
                adjusted_spacing, leading_reason = self.leading_indicator.get_spacing_adjustment(
                    ccxt_symbol, base_grid_spacing
                )
                if adjusted_spacing != base_grid_spacing:
                    ratio = adjusted_spacing / base_grid_spacing
                    base_grid_spacing = adjusted_spacing
                    base_take_profit *= ratio

        # === 2. 動態網格範圍 (ATR) ===
        if not leading_reason or leading_reason == "正常":
            take_profit, grid_spacing = self.dynamic_grid_manager.get_dynamic_spacing(
                ccxt_symbol,
                base_take_profit,
                base_grid_spacing,
                max_cfg
            )
        else:
            take_profit = base_take_profit
            grid_spacing = base_grid_spacing

        # === 3. GLFT 偏移 ===
        bid_skew, ask_skew = self.glft_controller.calculate_spread_skew(
            sym_state.long_position,
            sym_state.short_position,
            grid_spacing,
            max_cfg
        )

        sym_state.dynamic_take_profit = take_profit
        sym_state.dynamic_grid_spacing = grid_spacing
        sym_state.inventory_ratio = self.glft_controller.calculate_inventory_ratio(
            sym_state.long_position, sym_state.short_position
        )

        if leading_reason and leading_reason != "正常":
            logger.debug(f"[LeadingIndicator] {sym_config.symbol} 間距調整: {leading_reason}")

        return take_profit, grid_spacing

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

    def _check_and_reduce_positions(self, sym_config: SymbolConfig, sym_state: SymbolState):
        """檢查並減倉"""
        REDUCE_COOLDOWN = 60

        ccxt_symbol = sym_config.ccxt_symbol
        local_threshold = sym_config.position_threshold * 0.8
        reduce_qty = sym_config.position_threshold * 0.1

        last_reduce = self.state.last_reduce_time.get(ccxt_symbol, 0)
        if time.time() - last_reduce < REDUCE_COOLDOWN:
            return

        if sym_state.long_position >= local_threshold and sym_state.short_position >= local_threshold:
            logger.info(f"[風控] {sym_config.symbol} 多空持倉均超過 {local_threshold}，開始雙向減倉")

            if sym_state.long_position > 0:
                self.place_order(ccxt_symbol, 'sell', 0, reduce_qty, True, 'long', 'market')
                logger.info(f"[風控] {sym_config.symbol} 市價平多 {reduce_qty}")

            if sym_state.short_position > 0:
                self.place_order(ccxt_symbol, 'buy', 0, reduce_qty, True, 'short', 'market')
                logger.info(f"[風控] {sym_config.symbol} 市價平空 {reduce_qty}")

            self.state.last_reduce_time[ccxt_symbol] = time.time()

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

    async def adjust_grid(self, ccxt_symbol: str):
        sym_config = None
        for cfg in self.config.symbols.values():
            if cfg.ccxt_symbol == ccxt_symbol and cfg.enabled:
                sym_config = cfg
                break

        if not sym_config:
            return

        sym_state = self.state.symbols.get(ccxt_symbol)
        if not sym_state:
            return

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

        self._check_and_reduce_positions(sym_config, sym_state)

        # 多頭
        if sym_state.long_position == 0:
            if time.time() - self.last_order_times.get(f"{ccxt_symbol}_long", 0) > 10:
                self.cancel_orders_for_side(ccxt_symbol, 'long')
                qty = self._get_adjusted_quantity(sym_config, sym_state, 'long', False)
                self.place_order(ccxt_symbol, 'buy', sym_state.best_bid, qty, False, 'long')
                self.last_order_times[f"{ccxt_symbol}_long"] = time.time()
                sym_state.last_grid_price_long = price
        else:
            if self._should_adjust_grid(sym_config, sym_state, 'long'):
                await self._place_grid(ccxt_symbol, sym_config, 'long')
                sym_state.last_grid_price_long = price

        # 空頭
        if sym_state.short_position == 0:
            if time.time() - self.last_order_times.get(f"{ccxt_symbol}_short", 0) > 10:
                self.cancel_orders_for_side(ccxt_symbol, 'short')
                qty = self._get_adjusted_quantity(sym_config, sym_state, 'short', False)
                self.place_order(ccxt_symbol, 'sell', sym_state.best_ask, qty, False, 'short')
                self.last_order_times[f"{ccxt_symbol}_short"] = time.time()
                sym_state.last_grid_price_short = price
        else:
            if self._should_adjust_grid(sym_config, sym_state, 'short'):
                await self._place_grid(ccxt_symbol, sym_config, 'short')
                sym_state.last_grid_price_short = price

    async def _place_grid(self, ccxt_symbol: str, sym_config: SymbolConfig, side: str):
        """掛出網格訂單 (MAX 版本)"""
        sym_state = self.state.symbols[ccxt_symbol]
        price = sym_state.latest_price

        if side == 'long' and sym_state.long_position <= 0:
            logger.debug(f"[Grid] {sym_config.symbol} 多頭無倉位，跳過 _place_grid")
            return
        if side == 'short' and sym_state.short_position <= 0:
            logger.debug(f"[Grid] {sym_config.symbol} 空頭無倉位，跳過 _place_grid")
            return

        take_profit_spacing, grid_spacing = self._get_dynamic_spacing(sym_config, sym_state)

        tp_qty = self._get_adjusted_quantity(sym_config, sym_state, side, True)
        base_qty = self._get_adjusted_quantity(sym_config, sym_state, side, False)

        if side == 'long':
            my_position = sym_state.long_position
            opposite_position = sym_state.short_position
            dead_mode_flag = sym_state.long_dead_mode
            pending_tp_orders = sym_state.sell_long_orders
        else:
            my_position = sym_state.short_position
            opposite_position = sym_state.long_position
            dead_mode_flag = sym_state.short_dead_mode
            pending_tp_orders = sym_state.buy_short_orders

        is_dead = GridStrategy.is_dead_mode(my_position, sym_config.position_threshold)

        if is_dead:
            if not dead_mode_flag:
                if side == 'long':
                    sym_state.long_dead_mode = True
                else:
                    sym_state.short_dead_mode = True
                logger.info(f"[MAX] {sym_config.symbol} {side}頭進入裝死模式 (持倉:{my_position})")

            if pending_tp_orders <= 0:
                special_price = GridStrategy.calculate_dead_mode_price(
                    price, my_position, opposite_position, side
                )

                if side == 'long':
                    self.place_order(ccxt_symbol, 'sell', special_price, tp_qty, True, 'long')
                else:
                    self.place_order(ccxt_symbol, 'buy', special_price, tp_qty, True, 'short')
                logger.info(f"[MAX] {sym_config.symbol} {side}頭裝死止盈@{special_price:.4f}")
        else:
            if dead_mode_flag:
                if side == 'long':
                    sym_state.long_dead_mode = False
                else:
                    sym_state.short_dead_mode = False
                logger.info(f"[MAX] {sym_config.symbol} {side}頭離開裝死模式")

            self.cancel_orders_for_side(ccxt_symbol, side)

            tp_price, entry_price = GridStrategy.calculate_grid_prices(
                price, take_profit_spacing, grid_spacing, side
            )

            if side == 'long':
                if sym_state.long_position > 0:
                    self.place_order(ccxt_symbol, 'sell', tp_price, tp_qty, True, 'long')
                self.place_order(ccxt_symbol, 'buy', entry_price, base_qty, False, 'long')
            else:
                if sym_state.short_position > 0:
                    self.place_order(ccxt_symbol, 'buy', tp_price, tp_qty, True, 'short')
                self.place_order(ccxt_symbol, 'sell', entry_price, base_qty, False, 'short')

            logger.info(f"[MAX] {sym_config.symbol} {side}頭 止盈@{tp_price:.4f}({tp_qty:.1f}) "
                       f"補倉@{entry_price:.4f}({base_qty:.1f}) [TP:{take_profit_spacing*100:.2f}%/GS:{grid_spacing*100:.2f}%]")

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

        if time.time() - self.last_sync_time > self.config.sync_interval:
            self.sync_all()
            self.last_sync_time = time.time()

    async def _handle_account_update(self, data: dict):
        """處理 ACCOUNT_UPDATE 事件"""
        try:
            account_data = data.get('a', {})

            balances = account_data.get('B', [])
            for bal in balances:
                asset = bal.get('a', '')
                if asset in ['USDC', 'USDT']:
                    wallet_balance = float(bal.get('wb', 0) or 0)
                    cross_wallet = float(bal.get('cw', 0) or 0)

                    acc = self.state.get_account(asset)
                    acc.wallet_balance = wallet_balance
                    acc.available_balance = cross_wallet

                    logger.info(f"[userData] {asset} 餘額更新: 錢包={wallet_balance:.2f}, 可用={cross_wallet:.2f}")

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
            sym_config = None
            for cfg in self.config.symbols.values():
                if cfg.symbol == symbol_raw:
                    ccxt_symbol = cfg.ccxt_symbol
                    sym_config = cfg
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

    async def _websocket_loop(self):
        ssl_context = ssl.create_default_context(cafile=certifi.where())

        while not self._stop_event.is_set():
            try:
                async with websockets.connect(self.config.websocket_url, ssl=ssl_context) as ws:
                    self.state.connected = True

                    streams = []
                    for cfg in self.config.symbols.values():
                        if cfg.enabled:
                            streams.append(f"{cfg.ws_symbol}@bookTicker")

                    if streams:
                        await ws.send(json.dumps({"method": "SUBSCRIBE", "params": streams, "id": 1}))

                    if self.listen_key:
                        await ws.send(json.dumps({"method": "SUBSCRIBE", "params": [self.listen_key], "id": 2}))
                        logger.info("[WebSocket] 已訂閱 userData stream")

                    while not self._stop_event.is_set():
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=30)
                            data = json.loads(msg)

                            event_type = data.get('e', '')

                            if event_type == 'bookTicker':
                                await self._handle_ticker(data)
                            elif event_type == 'ACCOUNT_UPDATE':
                                await self._handle_account_update(data)
                            elif event_type == 'ORDER_TRADE_UPDATE':
                                await self._handle_order_update(data)

                        except asyncio.TimeoutError:
                            await ws.ping()
            except Exception as e:
                self.state.connected = False
                if not self._stop_event.is_set():
                    logger.error(f"WebSocket 錯誤: {e}")
                    await asyncio.sleep(5)

    async def _keep_alive_loop(self):
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(1800)
                if not self._stop_event.is_set():
                    self.exchange.fapiPrivatePutListenKey()
                    self.listen_key = self._get_listen_key()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"更新 listenKey 失敗: {e}")

    async def run(self):
        try:
            self._init_exchange()
            self._check_hedge_mode()
            self.listen_key = self._get_listen_key()

            self.state.running = True
            self.state.start_time = datetime.now()

            self.sync_all()
        except Exception as e:
            logger.error(f"[MAX] 初始化失敗: {e}")
            self.state.running = False
            return

        self.tasks = [
            asyncio.create_task(self._websocket_loop()),
            asyncio.create_task(self._keep_alive_loop())
        ]

        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(0.5)
        finally:
            await self.stop()

    async def stop(self):
        self._stop_event.set()
        self.state.running = False

        for task in self.tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
