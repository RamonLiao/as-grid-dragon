"""REST 同步組件：持倉/掛單/帳戶/funding（#3 原子區語意原樣搬移）。

鎖序不變式：_sync_lock（本 service 持有）→ symbol lock（共享 SymbolLocks），單向。
"""
import asyncio
import time

from . import clock
from .utils import logger

TRADE_STATS_INTERVAL = 60.0     # 與 sync_interval(10s) 解耦，省 API 權重


class SyncService:
    def __init__(self, gateway, ctx, config, state, locks, notifier, risk_monitor, tasks,
                 start_time_ms: int = 0):
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
        # 成交統計：口徑為「本次引擎啟動以來」，與 userData 時代的語意一致
        self.start_time_ms = start_time_ms
        self._last_trade_id: dict = {}
        self._last_trade_stats_at = 0.0

    async def sync_all(self):
        if self._sync_lock.locked():
            return
        async with self._sync_lock:
            await self._sync_positions()
            await self._sync_orders()
            await self._sync_account()
            await self._sync_funding_rates()
            await self._sync_trade_stats()

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

    async def _sync_trade_stats(self):
        """成交次數/已實現盈虧的**唯一** writer。

        userData handler 曾經是唯一 writer，而該路徑 2026-07-12 起靜默死亡一個月，
        面板與 Telegram 日報的數字全是 0。改由 REST 維持後，兩處同時寫會在 userData
        復活時造成翻倍 ⇒ handler 已停寫，這裡是單一 writer。

        不取 symbol lock：total_trades/total_profit 是本方法唯一 writer（handler 已停寫），
        沒有 write-write race；讀者（面板/Telegram）只讀這兩個獨立純量欄位，不像
        long/short/upnl 三個欄位要求同一快照的原子性，單一賦值在 CPython 下不會有 torn
        read。若在這個 await 密集的分頁迴圈裡取鎖，還違反本檔案「鎖內無其他 await」的既有
        不變式，需要額外緩衝重寫，換不到實質好處。
        """
        if clock.now() - self._last_trade_stats_at < TRADE_STATS_INTERVAL:
            return

        for sym_config in self.config.symbols.values():
            if not sym_config.enabled:
                continue
            symbol = sym_config.ccxt_symbol
            st = self.state.symbols.get(symbol)
            if not st:
                continue

            max_id = self._last_trade_id.get(symbol, 0)
            since = self.start_time_ms
            try:
                while True:
                    trades = await self.gateway.call(
                        self.ctx.exchange.fetch_my_trades,
                        symbol=symbol, since=since, limit=1000)
                    if not trades:
                        break
                    progressed = False
                    for t in trades:
                        try:
                            tid = int(t.get('id'))
                        except (TypeError, ValueError):
                            continue
                        if tid <= max_id:
                            continue
                        max_id = max(max_id, tid)
                        progressed = True
                        st.total_trades += 1
                        st.total_profit += float(
                            t.get('info', {}).get('realizedPnl', 0) or 0)
                    if len(trades) < 1000:
                        break
                    last_ts = trades[-1].get('timestamp')
                    if not last_ts or not progressed:
                        # 整頁滿額卻沒有任何新 id：同一毫秒的成交量撞到單頁上限，
                        # 再往前推只會拿到一樣的頁面（無限迴圈風險）。停手，下個節流
                        # 週期用同一個 since 重試——不會漏，因為 max_id 還沒推進。
                        if not progressed:
                            logger.error(
                                f"{symbol} 成交分頁在 ts={last_ts} 卡死"
                                f"（同毫秒成交量超過單頁上限），本輪停止推進")
                        break
                    # 分頁：Binance 單次上限 1000。用最後一筆的 timestamp（不 +1）當下一頁
                    # since —— 若最後一筆與頁尾之後還有同毫秒的成交，+1 會把它們永久跳過；
                    # 這裡改成 inclusive，重疊部分靠上面的 tid dedup 擋掉重複計數。
                    since = int(last_ts)
            except Exception as e:
                # 失敗保留既有數值。把失敗當成 0 筆寫回去會讓面板數字倒退。
                logger.error(f"同步 {symbol} 成交統計失敗: {e}")
                continue

            self._last_trade_id[symbol] = max_id

        self.state.total_trades = sum(s.total_trades for s in self.state.symbols.values())
        self.state.total_profit = sum(s.total_profit for s in self.state.symbols.values())
        self._last_trade_stats_at = clock.now()
