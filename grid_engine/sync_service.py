"""REST 同步組件：持倉/掛單/帳戶/funding（#3 原子區語意原樣搬移）。

鎖序不變式：_sync_lock（本 service 持有）→ symbol lock（共享 SymbolLocks），單向。
"""
import asyncio
from dataclasses import dataclass
import math
from time import time as _time
from typing import Optional

from . import clock
from .utils import logger

TRADE_STATS_INTERVAL = 60.0     # 與 sync_interval(10s) 解耦，省 API 權重
# 分頁游標往回退的安全邊際：REST 端偶有到達延遲/時鐘誤差，純用「上次看到的最大
# timestamp」當下次 since 會漏掉「timestamp 落在邊際窗內、但比上輪 cursor 晚才可見」
# 的成交；退幾秒換一點重複讀取（靠 tid dedup 擋掉重複計數，成本可忽略），兜住這種
# timestamp 到達順序亂掉的情況。5s 是主觀但有理由的取值：遠大於 REST 正常延遲
# （通常 < 1s），又遠小於「累計成交數已破千」時重拉全部歷史的成本。
#
# 已知限制（不是這個邊際能解的）：dedup 判準是 `tid <= page_max_id`（純看 id 大小），
# 邊際只兜「timestamp 亂序」，兜不了「id 有空洞」——若某筆成交的 id 落在已處理過的
# id 區間內但遲遲不可見（例如先看到 id=1,2,3,5，id=4 之後才出現），無論邊際多大都
# 會被 dedup 永久判定成「舊資料」而漏抓。這是已知風險，未处理：Binance 同一帳戶/
# symbol 的 trade id 實務上單調遞增且依序回傳，此情境判定為極低機率，改成「有界的
# 已見 id 集合」需要額外的記憶體管理與過期策略，這輪不做。
# 反方向的限制（whole-branch review 已裁決不修，同樣記在這裡）：若某筆成交的 id
# 異常偏大（例如交易所端資料錯亂），page_max_id 會被它一次跳到很高，之後所有 id
# 比它小、但其實是尚未處理過的正常成交，會被 `tid <= page_max_id` 誤判成舊資料而
# 永久漏抓——同一個 dedup 判準，方向相反的盲點。未處理：同上，trade id 實務上
# 不會亂跳，機率極低。
TRADE_STATS_SINCE_MARGIN_MS = 5_000
# 單一 symbol、單輪 _sync_trade_stats 允許跑的最大分頁頁數。無上限時，這段迴圈跑在
# ws_client.py 的 recv 迴圈內（handler 例外冒泡=重連是這條路徑唯一的失敗語意，見
# ws_client.py 檔頭 characterization 註解），停滯期間 ping/watchdog/recv 全部被卡住。
# 10 頁 = 1 萬筆成交，遠超單輪同步週期(TRADE_STATS_INTERVAL=60s)內合理發生的成交量，
# 超限就停、記 warning，下一輪從已推進的游標續拉（見 security-fix Medium-2）。
TRADE_STATS_MAX_PAGES_PER_SYNC = 10


@dataclass(frozen=True)
class SyncOutcome:
    """一輪 sync_all 的逐項成敗。

    為什麼要回報而不是靠例外：五個子項各自吞例外（歷史決定，見各方法 docstring），
    呼叫端那一層幾乎永遠看不到例外 ⇒ 「REST 全掛」在今天的表現是面板數字凍結、
    風控拿著過期持倉繼續跑、沒有人被通知。這個回傳值是那條靜默路徑唯一的出口。

    critical_ok 只看持倉與帳戶：前者是風控判斷的輸入，後者是保證金告警的輸入。
    掛單數只影響顯示與 requote 計數，funding 與成交統計是遙測——把它們納入告警
    會被偶發 REST 抖動洗版，而它們失敗不影響交易安全。
    """
    positions_ok: bool = True
    orders_ok: bool = True
    account_ok: bool = True
    funding_ok: bool = True
    trade_stats_ok: bool = True
    skipped: bool = False

    @property
    def critical_ok(self) -> bool:
        return self.positions_ok and self.account_ok


class SyncService:
    def __init__(self, gateway, ctx, config, state, locks, notifier, risk_monitor, tasks,
                 start_time_ms: Optional[int] = None):
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
        # 成交統計：口徑為「本次引擎啟動以來」，與 userData 時代的語意一致。
        # 預設值不可是 0（epoch）：少傳這個 kwarg 會讓口徑從「本次啟動以來」靜默
        # 變成「全期累計」（見 whole-branch review Minor-3）。
        if start_time_ms is None:
            start_time_ms = int(_time() * 1000)
        self.start_time_ms = start_time_ms
        self._last_trade_id: dict = {}
        self._last_trade_since: dict = {}   # 每 symbol 的分頁游標，跨輪持續推進（見 Critical-1 修復）
        self._last_trade_stats_at = 0.0

    async def sync_all(self) -> SyncOutcome:
        if self._sync_lock.locked():
            return SyncOutcome(skipped=True)
        async with self._sync_lock:
            positions_ok = await self._sync_positions()
            orders_ok = await self._sync_orders()
            account_ok = await self._sync_account()
            funding_ok = await self._sync_funding_rates()
            trade_stats_ok = await self._sync_trade_stats()
        return SyncOutcome(
            positions_ok=positions_ok, orders_ok=orders_ok, account_ok=account_ok,
            funding_ok=funding_ok, trade_stats_ok=trade_stats_ok,
        )

    async def maybe_sync(self) -> Optional[SyncOutcome]:
        """節流同步。回 None 表示本輪未達門檻（不算成功也不算失敗）。

        計時用 guard_now()（牆鐘）而非 now()（情境時鐘）：後者會被 backtester
        替換成歷史 epoch，live 與回測同行程時會讓節流判斷錯亂。
        與價格時效守衛（bot.py:415）用同一個時鐘，語意一致。
        """
        if clock.guard_now() - self.last_sync_time > self.config.sync_interval:
            outcome = await self.sync_all()
            self.last_sync_time = clock.guard_now()
            return outcome
        return None

    async def _sync_funding_rates(self) -> bool:
        """同步所有交易對的 funding rate。

        逐 symbol try/except（與 _sync_orders 同構）：這個方法排在 sync_all() 裡
        `_sync_trade_stats` **之前**，例外冒泡走的是同一條致命路徑
        （_handle_ticker → ws_client outer except = 強制重連 ⇒ 失敗持續發生時
        變成每 5 秒重連一次的永久迴圈，decide() 停擺）。原本它完全沒有 try/except，
        而 _sync_trade_stats 的外層保險註解卻宣稱「兄弟方法每一個都保證不拋例外」
        ——那句話當時是假的（見 dual-review C1）。
        """
        if not self.ctx.funding_manager:
            return True

        ok = True
        for sym_config in self.config.symbols.values():
            if not sym_config.enabled:
                continue
            try:
                rate = await self.gateway.call(self.ctx.funding_manager.update_funding_rate, sym_config.ccxt_symbol)
                sym_state = self.state.symbols.get(sym_config.ccxt_symbol)
                if sym_state:
                    sym_state.current_funding_rate = rate
            except Exception as e:
                logger.error(f"同步 {sym_config.ccxt_symbol} funding rate 失敗: {e}")
                ok = False
        return ok

    async def _sync_positions(self) -> bool:
        try:
            positions = await self.gateway.call(self.ctx.exchange.fetch_positions, params={'type': 'future'})
        except Exception as e:
            logger.error(f"同步持倉失敗: {e}")
            return False

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
        return True

    async def _sync_orders(self) -> bool:
        ok = True
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
                ok = False
        return ok

    async def _sync_account(self) -> bool:
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
            return True
        except Exception as e:
            logger.error(f"同步帳戶失敗: {e}")
            return False

    async def _sync_trade_stats(self) -> bool:
        """成交次數/已實現盈虧的**唯一** writer。

        userData handler 曾經是唯一 writer，而該路徑 2026-07-12 起靜默死亡一個月，
        面板與 Telegram 日報的數字全是 0。改由 REST 維持後，兩處同時寫會在 userData
        復活時造成翻倍 ⇒ handler 已停寫，這裡是單一 writer。

        不取 symbol lock：total_trades/total_profit 是本方法唯一 writer（handler 已停寫），
        沒有 write-write race；讀者（面板/Telegram）只讀這兩個獨立純量欄位，不像
        long/short/upnl 三個欄位要求同一快照的原子性，單一賦值在 CPython 下不會有 torn
        read。（symbol lock 是否值得加，留給 whole-branch review 的 Minor 清單裁決。）

        累加/游標套用時機：整段分頁迴圈先把新增筆數/盈虧/max_id 緩衝在區域變數，
        只有這個 symbol 的分頁**完整成功**（沒有拋例外）才一次套用到 st 並推進
        `_last_trade_id` / `_last_trade_since`。分頁中途失敗絕不能半套用——半套用會讓
        下一輪從舊游標重新拉到這批已經算過的成交，造成重複計數（見 review Critical-2）。

        口徑注意：舊路徑（userData ORDER_TRADE_UPDATE）數的是「FILLED 事件」，每張單一次；
        這裡數的是 `fetch_my_trades` 回傳的成交紀錄，部分成交會拆成多筆，口徑因此略有偏高。
        非嚴格等價，但實務影響小（BNBUSDC 常見單量 0.02，很少發生部分成交拆單）。
        """
        if clock.now() - self._last_trade_stats_at < TRADE_STATS_INTERVAL:
            return True

        try:
            await self._sync_trade_stats_body()
            return True
        except Exception as e:
            # 兄弟方法（_sync_positions/_sync_orders/_sync_account/_sync_funding_rates）
            # 各自都有整段/逐 symbol 的 try/except（_sync_funding_rates 的那道是
            # dual-review C1 才補上的——在那之前這句註解是假的，例外從那個兄弟方法
            # 仍然暢通）。例外冒泡的路徑：_handle_ticker → ws_client.py 的 outer
            # except（檔頭 characterization 註解鎖定的語意：handler 例外=強制重連）
            # ⇒ 若失敗持續發生，變成每 5 秒重連一次的永久迴圈，decide() 停擺，
            # 手上還有實倉與掛單（見 security-fix Medium-1）。內層分頁 try/except
            # 已經處理絕大多數失敗，這層是最後一道保險，不改變內層「整批丟棄/
            # 單筆跳過」的既有語意。
            logger.error(f"同步成交統計失敗（外層保險，不應常態觸發）: {e}")
            return False
        finally:
            # 節流時間戳必須在 finally 推進：放在 body 最後一行時，body 拋例外
            # （正是這層保險要接的那種）會讓時間戳永不前進 ⇒ 之後每一次 sync_all()
            # （每 10s）都重打 fetch_my_trades，靜默變成 6 倍 API 權重，且每輪重做
            # 同一批 pending 計算（見 dual-review B1）。
            self._last_trade_stats_at = clock.now()

    async def _sync_trade_stats_body(self):
        for sym_config in self.config.symbols.values():
            if not sym_config.enabled:
                continue
            symbol = sym_config.ccxt_symbol
            st = self.state.symbols.get(symbol)
            if not st:
                continue

            base_max_id = self._last_trade_id.get(symbol, 0)
            page_max_id = base_max_id
            # 分頁游標跨輪持續推進（不是每輪重設回 start_time_ms）：否則第 2 輪起第一頁
            # 必然整頁被 tid dedup 掉，會誤觸「同毫秒卡死」的終止分支，計數永久凍結
            # （見 review Critical-1；成交數一旦超過單頁上限 1000 就會發生）。
            since = self._last_trade_since.get(symbol, self.start_time_ms)
            pending_n = 0
            pending_pnl = 0.0
            last_seen_ts = None
            id_warn_logged = False   # 節流：同一輪同一 symbol 只記一次 id 解析失敗警告
            page_count = 0
            try:
                while True:
                    page_count += 1
                    trades = await self.gateway.call(
                        self.ctx.exchange.fetch_my_trades,
                        symbol=symbol, since=since, limit=1000)
                    if not trades:
                        break
                    for t in trades:
                        try:
                            tid = int(t.get('id'))
                        except (TypeError, ValueError):
                            # 與下方 realizedPnl/timestamp 對齊：不對稱的靜默 continue
                            # 正是本分支要根除的「數字靜默停住」換個形式回來（見
                            # security-fix Low-3）。節流成每輪至多一次，避免整批畸形
                            # id 洗版 log。
                            if not id_warn_logged:
                                logger.warning(
                                    f"{symbol} 成交 id 解析失敗（tid={t.get('id')!r}），"
                                    f"跳過此筆計數（本輪同類警告僅記一次）")
                                id_warn_logged = True
                            continue
                        if tid <= page_max_id:
                            continue
                        # tid 先於欄位解析推進 page_max_id：即使這筆的 realizedPnl/info
                        # 畸形而被下面 continue 跳過，dedup 游標仍然前進，同一筆壞資料
                        # 不會下一輪又撞到、重複記警告、卡住整批（見 whole-branch review
                        # Important-3——這正是本分支要根除的「數字靜默停住」換個形式回來）。
                        # 取捨：這筆的 id 從此永久燒進游標——即使交易所後續回傳這筆的
                        # 正確資料，也不會再被重抓（`tid <= page_max_id` 會直接濾掉）。
                        # 用「這筆的統計值永久漏記」換「不會每輪重複卡在同一筆壞資料上」，
                        # 是本分支的刻意選擇（見 verifier-fix Minor-3）。
                        page_max_id = max(page_max_id, tid)
                        try:
                            # 用 .get('info', {}) 而非 `t.get('info') or {}`：缺 info
                            # 鍵維持原行為（視同 0，不算畸形）；info 鍵存在但顯式 None
                            # 才是畸形（.get() on None 拋 AttributeError，被下面接住跳過）。
                            info = t.get('info', {})
                            pnl = float(info.get('realizedPnl', 0) or 0)
                            if not math.isfinite(pnl):
                                # float('nan')/float('inf') 不拋例外，逐筆 try/except
                                # 接不到——但 total_profit 用 += 累加、沒有重置點，一旦
                                # 混入 NaN/inf 就永久污染，且會直接印上 Telegram 日報
                                # （見 verifier-fix Important-1）。比照畸形欄位處理：
                                # 跳過此筆，不進累加。
                                raise ValueError(f"realizedPnl 非有限值: {pnl}")
                        except (TypeError, ValueError, AttributeError) as e:
                            # 單筆隔離，不比照 id 的 continue 之外再毒死整個 symbol
                            # 那一輪（硬化不對稱是 review 指出的問題本身）。
                            logger.warning(
                                f"{symbol} 成交 id={tid} 欄位解析失敗，跳過此筆計數"
                                f"（不影響同批其他筆與游標推進）: {e}")
                            continue
                        pending_n += 1
                        pending_pnl += pnl
                    raw_last_ts = trades[-1].get('timestamp')
                    last_ts = None
                    if raw_last_ts:
                        try:
                            last_ts = int(raw_last_ts)
                        except (TypeError, ValueError) as e:
                            # 畸形 timestamp 視同缺 timestamp 處理（走下面既有的安全路徑），
                            # 不讓它整批丟棄已經算好的 pending_n/pending_pnl。
                            logger.warning(
                                f"{symbol} 成交分頁最後一筆 timestamp 解析失敗，"
                                f"視同缺 timestamp 處理: {e}")
                    if last_ts is not None:
                        last_seen_ts = last_ts
                    if len(trades) < 1000:
                        break
                    if last_ts is None:
                        # 理論上 Binance 不會回 falsy/畸形 timestamp（未觀測過），但這條
                        # 分支與上面 Critical-1 同構：若真的發生，_last_trade_since 不
                        # 推進，下一輪同一個 since 撈回同一頁、全被 tid dedup、又走這條
                        # break ⇒ 永久凍結。留一行 log 避免它變成靜默凍結。
                        logger.warning(
                            f"{symbol} 成交分頁最後一筆缺（或無法解析）timestamp，"
                            f"本輪停止推進（since={since} 未推進，若持續發生請檢查"
                            f"交易所回傳格式）")
                        break
                    # 分頁：Binance 單次上限 1000。用最後一筆的 timestamp（不 +1）當下一頁
                    # since —— 若最後一筆與頁尾之後還有同毫秒的成交，+1 會把它們永久跳過；
                    # 這裡改成 inclusive，重疊部分靠上面的 tid dedup 擋掉重複計數。
                    # 終止條件是「since 推不動」而非「這頁沒有新 id」：後者在游標持續推進
                    # 的情況下，「這頁全是舊資料」是分頁過程中的正常過渡態（例如追上真正
                    # 邊界前的最後一次重疊頁），不代表卡死，錯把它當卡死會漏抓後面的新資料。
                    nxt = last_ts
                    # 判定用 `<=` 而非 `==`（見 dual-review C4）：`trades[-1]` 不保證
                    # 是該頁 timestamp 最大的一筆，`nxt < since` 時原本的 `==` 擋不住，
                    # since 會往回退 ⇒ 下一頁重撈已掃過的區間。`==` 只守住「完全不動」
                    # 這一個點，不是完整的單調性守衛。
                    if nxt <= since:
                        logger.error(
                            f"{symbol} 成交分頁游標無法前進（ts={nxt} <= since={since}，"
                            f"同毫秒成交量超過單頁上限或頁尾非最大 ts），本輪停止推進")
                        break
                    since = nxt
                    if page_count >= TRADE_STATS_MAX_PAGES_PER_SYNC:
                        # 這段迴圈 inline 跑在 WS recv 迴圈內（呼叫鏈：ws_client.py
                        # handler → bot.py maybe_sync → sync_all → 這裡）；無上限會讓
                        # 單次同步吃光 recv/ping/watchdog 的時間片（見 security-fix
                        # Medium-2）。已處理的 pending_n/pending_pnl/游標照常在迴圈
                        # 外套用，下一輪從這裡推進到的 since 續拉，不漏不重。
                        logger.warning(
                            f"{symbol} 成交分頁達單輪上限 {TRADE_STATS_MAX_PAGES_PER_SYNC} "
                            f"頁，本輪停止，游標已推進至 since={since}，下一輪續拉")
                        break
            except Exception as e:
                # 失敗：緩衝區整個丟棄，不套用到 st、不推進游標，保留既有數值。
                # 把失敗當成 0 筆寫回去、或把已算的部分半套用，都會讓數字錯（倒退或翻倍）。
                logger.error(f"同步 {symbol} 成交統計失敗: {e}")
                continue

            # 分頁完整成功才套用：累加與游標一起原子推進，避免下一輪重算同一批。
            st.total_trades += pending_n
            st.total_profit += pending_pnl
            self._last_trade_id[symbol] = page_max_id
            if last_seen_ts is not None:
                # 游標不是直接等於「看到的最大 timestamp」，而是往回退一段安全邊際
                # （見上方 TRADE_STATS_SINCE_MARGIN_MS 說明），且只單調前進不倒退。
                floor = self._last_trade_since.get(symbol, self.start_time_ms)
                self._last_trade_since[symbol] = max(
                    floor, last_seen_ts - TRADE_STATS_SINCE_MARGIN_MS)

        self.state.total_trades = sum(s.total_trades for s in self.state.symbols.values())
        self.state.total_profit = sum(s.total_profit for s in self.state.symbols.values())
