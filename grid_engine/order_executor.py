"""下單/撤單執行組件（#1 加固語意原樣搬移）：
clientOrderId + 指數退避 + 斷路器（僅開倉單成功重置）+ 封鎖只擋開倉。
"""
import asyncio
import time
from typing import Dict, List, Optional

from .utils import logger
from .config import SymbolConfig

# 下單失敗退避：首次封鎖秒數、指數退避上限、連續失敗斷路閾值、斷路冷卻秒數
ORDER_BACKOFF_BASE = 2.0
ORDER_BACKOFF_CAP = 60.0
ORDER_CIRCUIT_THRESHOLD = 10
ORDER_CIRCUIT_COOLDOWN = 300.0


class OrderExecutor:
    def __init__(self, gateway, ctx, state, notifier, config, locks,
                 stop_event: asyncio.Event, tasks: List[asyncio.Task]):
        self.gateway = gateway
        self.ctx = ctx          # 呼叫當下讀 ctx.exchange/ctx.precisions，絕不快照
        self.state = state
        self.notifier = notifier
        self.config = config
        self.locks = locks
        self._stop_event = stop_event
        self.tasks = tasks      # bot.tasks 共享參照：斷路通知 task 防 GC + stop 可 cancel

        # 下單失敗退避/斷路器（per symbol）
        self._order_fail_counts: Dict[str, int] = {}
        self._order_block_until: Dict[str, float] = {}
        self._order_seq = 0

    def is_blocked(self, symbol: str) -> bool:
        """封鎖期查詢（bot 網格鏈的 order_blocked 讀取點）"""
        return time.time() < self._order_block_until.get(symbol, 0)

    async def close_symbol_positions(self, ccxt_symbol: str, sym_config: SymbolConfig):
        """平倉指定交易對"""
        async with self.locks.get(ccxt_symbol):
            try:
                sym_state = self.state.symbols.get(ccxt_symbol)
                if not sym_state:
                    return

                await self.cancel_orders_for_side(ccxt_symbol, 'long')
                await self.cancel_orders_for_side(ccxt_symbol, 'short')

                if sym_state.long_position > 0:
                    await self.place_order(
                        ccxt_symbol, 'sell', 0, sym_state.long_position,
                        reduce_only=True, position_side='long', order_type='market'
                    )
                    logger.info(f"[追蹤止盈] {sym_config.symbol} 市價平多 {sym_state.long_position}")

                if sym_state.short_position > 0:
                    await self.place_order(
                        ccxt_symbol, 'buy', 0, sym_state.short_position,
                        reduce_only=True, position_side='short', order_type='market'
                    )
                    logger.info(f"[追蹤止盈] {sym_config.symbol} 市價平空 {sym_state.short_position}")

            except Exception as e:
                logger.error(f"[追蹤止盈] {sym_config.symbol} 平倉失敗: {e}")

    async def place_order(self, symbol: str, side: str, price: float, quantity: float,
                    reduce_only: bool = False, position_side: str = None,
                    order_type: str = 'limit') -> Optional[dict]:
        # 停機後不再送單（executor 已排入的由 shutdown(cancel_futures) 收掉）
        if self._stop_event.is_set():
            return None
        # 退避封鎖只擋開倉單；reduce_only（止盈/平倉）永遠放行
        if not reduce_only and self.is_blocked(symbol):
            return None

        try:
            prec = self.ctx.precisions.get(symbol, {"price": 4, "amount": 0, "min_amount": 1})
            price = round(price, prec["price"])
            quantity = round(quantity, prec["amount"])
            quantity = max(quantity, prec["min_amount"])

            self._order_seq += 1
            params = {
                'reduce_only': reduce_only,
                # ccxt unified key：binance→newClientOrderId、bybit→orderLinkId 自動映射
                'clientOrderId': f"asgd_{int(time.time() * 1000)}_{self._order_seq}",
            }
            if position_side:
                params['positionSide'] = position_side.upper()

            if order_type == 'market':
                result = await self.gateway.call(self.ctx.exchange.create_order, symbol, 'market', side, quantity, params=params)
            else:
                result = await self.gateway.call(self.ctx.exchange.create_order, symbol, 'limit', side, quantity, price, params=params)
            # 只有開倉單成功才重置退避 — 有倉位時 TP(reduce_only) 成功與補倉失敗
            # 每輪交錯，若 TP 成功也重置，連續失敗永遠數不到斷路閾值
            if not reduce_only:
                self._order_fail_counts[symbol] = 0
                self._order_block_until.pop(symbol, None)
            return result
        except Exception as e:
            self._register_order_failure(symbol, e)
            return None

    def _register_order_failure(self, symbol: str, error: Exception):
        """連續失敗指數退避；達斷路閾值改長冷卻並通知一次"""
        n = self._order_fail_counts.get(symbol, 0) + 1
        self._order_fail_counts[symbol] = n

        if n >= ORDER_CIRCUIT_THRESHOLD:
            block = ORDER_CIRCUIT_COOLDOWN
            if n == ORDER_CIRCUIT_THRESHOLD:
                msg = (f"⛔ 下單斷路: {symbol} 連續失敗 {n} 次，"
                       f"暫停開倉 {int(block)}s\n最後錯誤: {error}")
                logger.warning(msg)
                try:
                    asyncio.get_running_loop()
                    # 存引用防止 task 在執行前被 GC（斷路通知只發這一次，丟不得）
                    task = asyncio.create_task(self.notifier.send(msg))
                    self.tasks.append(task)
                    # 完成後自移除，避免長跑累積（stop 可能已在 cancel 流程，故先查在不在）
                    task.add_done_callback(lambda t: t in self.tasks and self.tasks.remove(t))
                except RuntimeError:
                    pass  # 無 event loop（同步測試環境）時只留 log
        else:
            block = min(ORDER_BACKOFF_BASE * (2 ** (n - 1)), ORDER_BACKOFF_CAP)

        self._order_block_until[symbol] = time.time() + block
        logger.error(f"下單失敗 {symbol} (連續{n}次, 暫停開倉{block:.0f}s): {error}")

    async def cancel_orders_for_side(self, symbol: str, position_side: str):
        try:
            orders = await self.gateway.call(self.ctx.exchange.fetch_open_orders, symbol)
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
                    await self.gateway.call(self.ctx.exchange.cancel_order, order['id'], symbol)
        except Exception as e:
            logger.error(f"撤單失敗 {symbol}: {e}")
