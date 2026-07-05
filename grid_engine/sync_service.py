"""REST 同步組件：持倉/掛單/帳戶/funding（#3 原子區語意原樣搬移）。

鎖序不變式：_sync_lock（本 service 持有）→ symbol lock（共享 SymbolLocks），單向。
"""
import asyncio
import time

from .utils import logger


class SyncService:
    def __init__(self, gateway, ctx, config, state, locks, notifier, risk_monitor, tasks):
        self.gateway = gateway
        self.ctx = ctx
        self.config = config
        self.state = state
        self.locks = locks
        self.notifier = notifier
        self.risk_monitor = risk_monitor
        self.tasks = tasks      # bot.tasks 共享參照：風控通知 task 防 GC + stop 可 cancel
        # 並發鎖：sync 防重入（鎖序固定 _sync_lock → symbol lock）
        self._sync_lock = asyncio.Lock()
        self.last_sync_time = 0

    async def sync_all(self):
        if self._sync_lock.locked():
            return
        async with self._sync_lock:
            await self._sync_positions()
            await self._sync_orders()
            await self._sync_account()
            await self._sync_funding_rates()

    async def maybe_sync(self):
        """ticker 高頻路徑的節流同步（原 _handle_ticker 尾端 gating 收編）"""
        if time.time() - self.last_sync_time > self.config.sync_interval:
            await self.sync_all()
            self.last_sync_time = time.time()

    async def _sync_funding_rates(self):
        """同步所有交易對的 funding rate"""
        if not self.ctx.funding_manager:
            return

        for sym_config in self.config.symbols.values():
            if sym_config.enabled:
                rate = await self.gateway.call(self.ctx.funding_manager.update_funding_rate, sym_config.ccxt_symbol)
                sym_state = self.state.symbols.get(sym_config.ccxt_symbol)
                if sym_state:
                    sym_state.current_funding_rate = rate

    async def _sync_positions(self):
        try:
            positions = await self.gateway.call(self.ctx.exchange.fetch_positions, params={'type': 'future'})
        except Exception as e:
            logger.error(f"同步持倉失敗: {e}")
            return

        agg = {s: [0.0, 0.0, 0.0] for s in self.state.symbols}  # long, short, upnl
        for pos in positions:
            symbol = pos['symbol']
            if symbol in agg:
                contracts = pos.get('contracts', 0)
                side = pos.get('side')
                pnl = float(pos.get('unrealizedPnl', 0) or 0)
                if side == 'long':
                    agg[symbol][0] = contracts
                elif side == 'short':
                    agg[symbol][1] = abs(contracts)
                agg[symbol][2] += pnl

        for symbol, (long_pos, short_pos, upnl) in agg.items():
            async with self.locks.get(symbol):
                # 原子 apply：鎖內無其他 await
                st = self.state.symbols[symbol]
                st.long_position = long_pos
                st.short_position = short_pos
                st.unrealized_pnl = upnl

    async def _sync_orders(self):
        for sym_config in self.config.symbols.values():
            if not sym_config.enabled:
                continue
            symbol = sym_config.ccxt_symbol

            try:
                orders = await self.gateway.call(self.ctx.exchange.fetch_open_orders, symbol=symbol)
                state = self.state.symbols.get(symbol)
                if not state:
                    continue

                counts = [0.0, 0.0, 0.0, 0.0]  # buy_long, sell_long, buy_short, sell_short
                for order in orders:
                    qty = abs(float(order.get('info', {}).get('origQty', 0)))
                    side = order.get('side')
                    pos_side = order.get('info', {}).get('positionSide')
                    if side == 'buy' and pos_side == 'LONG':
                        counts[0] += qty
                    elif side == 'sell' and pos_side == 'LONG':
                        counts[1] += qty
                    elif side == 'buy' and pos_side == 'SHORT':
                        counts[2] += qty
                    elif side == 'sell' and pos_side == 'SHORT':
                        counts[3] += qty
                async with self.locks.get(symbol):
                    # 原子 apply：鎖內無其他 await
                    state.buy_long_orders, state.sell_long_orders, \
                        state.buy_short_orders, state.sell_short_orders = counts
            except Exception as e:
                logger.error(f"同步 {symbol} 掛單失敗: {e}")

    async def _sync_account(self):
        try:
            balance = await self.gateway.call(self.ctx.exchange.fetch_balance, {'type': 'future'})

            # ccxt 頂層 total=marginBalance(已含浮盈)、free=availableBalance、used=initialMargin，
            # 語意對不上面板欄位，且 equity 公式會重複加浮盈。改從 info.assets 取幣安原值。
            assets = balance.get('info', {}).get('assets', []) or []
            asset_map = {a.get('asset'): a for a in assets}

            for currency in ['USDC', 'USDT']:
                acc = self.state.get_account(currency)
                info = asset_map.get(currency)

                if info:
                    # walletBalance 為真實錢包餘額(不含浮盈)；equity = wallet + unrealized 才正確
                    acc.wallet_balance = float(info.get('walletBalance', 0) or 0)
                    acc.available_balance = float(info.get('availableBalance', 0) or 0)
                    acc.margin_used = float(info.get('initialMargin', 0) or 0)
                    acc.unrealized_pnl = float(info.get('unrealizedProfit', 0) or 0)
                else:
                    # fallback：info 缺該資產時退回 ccxt 頂層(total=marginBalance，不再額外加浮盈)
                    margin_balance = float(balance.get('total', {}).get(currency, 0) or 0)
                    free = float(balance.get('free', {}).get(currency, 0) or 0)
                    upnl = sum(s.unrealized_pnl for s in self.state.symbols.values()
                               if currency in s.symbol)
                    acc.wallet_balance = margin_balance - upnl  # 還原成錢包餘額
                    acc.available_balance = free
                    acc.margin_used = margin_balance - free if margin_balance > free else 0
                    acc.unrealized_pnl = upnl

            self.state.update_totals()

            # 風控通知
            if self.notifier.enabled:
                # fire-and-forget 需存引用防 GC；完成後自移除避免累積
                task = asyncio.create_task(self.risk_monitor.check_risk_and_notify())
                self.tasks.append(task)
                task.add_done_callback(lambda t: t in self.tasks and self.tasks.remove(t))

            await self.risk_monitor.check_trailing_stop()
        except Exception as e:
            logger.error(f"同步帳戶失敗: {e}")
