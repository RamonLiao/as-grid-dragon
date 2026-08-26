"""REST 同步組件：持倉/掛單/帳戶/funding（#3 原子區語意原樣搬移）。

鎖序不變式：_sync_lock（本 service 持有）→ symbol lock（共享 SymbolLocks），單向。

⚠️ 這是一條會下單的路徑，不是唯讀的對帳路徑（2026-08-26 週期同步 branch 的
最終 review 才被指出，spec/plan/七輪 task review 全沒提）：
`_sync_account()` → `risk_monitor.check_trailing_stop()` → `close_symbol_positions()`
會送市價平倉單。驅動源從 `_handle_ticker`（跑在 WS recv 迴圈內、與 `adjust_grid`
天生序列化）改成本檔 `run()` 的獨立 task 之後，**這條下單路徑與 `adjust_grid`
是真的並行的**。不互卡的理由是鎖序單向（`_sync_lock` → symbol lock，沒有反向
持有）+ `close_symbol_positions()` 自己會取 symbol lock。這是靠既有紀律碰巧
成立、不是被設計出來的——寫在這裡是為了讓下一個人在動 `check_trailing_stop`
的鎖或加新的下單路徑時知道自己踩在什麼上面。

⚠️ 不變式的正確敘述（2026-08-26 dual-review C1 修正；在那之前這段寫的是
「不會在同一個 symbol 上交錯改狀態」，**那句是錯的**）：
symbol lock 只保護 apply 的那一瞬間（鎖內無 await），**不保護 fetch→apply 的
窗口**——`_sync_positions` / `_sync_orders` 從 REST 讀回資料到寫進 state 之間
隔著一整趟 REST round-trip 的 await，而 WS handler（`bot._handle_account_update`
/ `bot._handle_order_update`）**根本不取 symbol lock**，可以整段落在那個窗口裡。
改動前這件事不會發生，純粹是因為 `sync_all()` 被 await 在 `_handle_ticker` 內、
而 `ws_client` 的 recv 迴圈一次只跑一個 handler；搬成獨立 task 之後那個天然的
序列化消失了。

所以真正的不變式是：**apply 之前必須確認「這份 REST 快照對應的那一版 state 還
沒被 WS 改過」**。作法是 `SymbolState.ws_seq`——WS handler 每次動持倉/掛單計數
就 +1，REST 在 fetch 之前抓一份、在 symbol lock 內比對，變了就丟棄該 symbol 的
快照（下一輪自然補上）。丟棄粒度是**單一 symbol**，不是整輪。
刻意不採「把 fetch 也放進 symbol lock」：那會讓 `adjust_grid` 的
`if lock.locked(): return` 在每次 REST round-trip 期間丟掉所有 tick。

⚠️ **帳戶層（`_sync_account`）刻意不設同型的守衛**（2026-08-26 re-review 裁定）。
它有一模一樣的 fetch→apply 競態：`bot._handle_account_update` 可以在
`fetch_balance` 的 round-trip 內把 `AccountBalance.wallet_balance` 寫成新值，
接著 REST 的舊快照蓋回去。不設防的理由，是這個競態在**所有 consumer** 上都無害：
  - `wallet_balance` / `AccountBalance.unrealized_pnl` 的讀者只有 `ui.py`、
    `reporting.py`、`notifier.py`——全部只是顯示。
  - 會下單/會做判斷的三個組件都不讀帳戶餘額：`risk_monitor` 讀
    `state.margin_usage` 與 **`SymbolState`.unrealized_pnl**（symbol 層，有守衛）、
    `order_executor` 與 `decision` 完全不碰。
  - 而且它自癒：下一輪（預設 10s）沒有 WS 撞進來就寫回真值，沒有像 symbol 層
    那樣「錯一次就一路錯下去」的分岔（symbol 層的 `long_position == 0` 會讓
    `_grid_step` 撤單重開倉、假的 `unrealized_pnl` 會觸發追蹤止盈的市價平倉）。
這段話存在的唯一目的：讓下一個讀者看到「`_sync_positions` 有守衛、`_sync_account`
沒有」時知道那是裁定，不是遺漏。**若哪天有會下單或會做風控判斷的路徑開始讀
`AccountBalance`，這個裁定即刻失效，必須補上同型守衛。**

規約：**本檔所有計時／節流一律用 `clock.guard_now()`，不用 `clock.now()`**
（2026-08-26 dual-review B4）。`now()` 是情境時鐘，backtester 每根 K 線用
`set_clock()` 把它換成歷史 epoch，而 live bot 與回測跑在同一個行程——混用會讓
節流量到大負數（每輪 early-return，統計靜默凍結）或在 `reset_clock()` 之後
永久失效（節流形同關閉，API 權重暴增）。這裡量的是本機牆鐘，不是情境時間。
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
# 單一 symbol、單輪 _sync_trade_stats 允許跑的最大分頁頁數。
# 這個上限原本的理由是「這段迴圈 inline 跑在 ws_client 的 recv 迴圈內，停滯期間
# ping/watchdog/recv 全部被卡住」——2026-08-26 移除 _handle_ticker 的同步呼叫後
# 那條呼叫鏈已不存在，理由必須重新論證，否則這個生產參數會變成沒有出處的魔數：
# 現在它擋的是「單輪同步無限期佔住 `_sync_lock`」。這段分頁迴圈跑在 `sync_all()`
# 的 `async with self._sync_lock` 內；無上限時，一個持續回滿頁的 symbol 會讓這一輪
# 永遠不結束 ⇒ 鎖永遠不釋放 ⇒ 後續每一輪 `sync_all()` 都走 early-return 回
# `SyncOutcome(skipped=True)`，而 skipped **刻意不計數**（見 `_evaluate`）⇒ 持倉/
# 帳戶/保證金告警全部停擺，且降級狀態機一次都不會被推進 = 完全靜默。這正是本
# branch 要根除的形態，換了個入口重演。（每日摘要的心跳那行是這個情境的最後一道
# 儀器：`last_sync_time` 只在 `sync_all()` 成功結束時蓋章，卡住就會超過門檻印警告。）
# 10 頁 = 1 萬筆成交，遠超單輪同步週期(TRADE_STATS_INTERVAL=60s)內合理發生的成交量，
# 超限就停、記 warning，下一輪從已推進的游標續拉（見 security-fix Medium-2）。
TRADE_STATS_MAX_PAGES_PER_SYNC = 10

# 關鍵項（持倉/帳戶）連續失敗幾輪才告警。3 輪 ≈ 30 秒（sync_interval 預設 10s）：
# 短到能在一次保證金事件的時間尺度內發出，長到不會被單次 REST 抖動觸發。
SYNC_FAILURE_THRESHOLD = 3

# 同一個 symbol 的 REST 快照被 ws_seq 守衛**連續**丟棄幾輪才記 warning。
# 為什麼需要這條儀器：「快照永遠被丟棄」是 C1 守衛引進的一種**新的靜默停擺**——
# 該 symbol 的持倉/掛單從此只由 WS 維護、REST 對帳完全失效，而 sync_all() 仍然
# 回 True、心跳照蓋、降級狀態機一次都不會被推進。狀態機與心跳都看不見它。
# 刻意**不**推降級計數（`_evaluate`）：丟棄是設計中的正常結果，WS 活躍期本來就會
# 發生，拿它去推狀態機會在最健康的時候誤報降級並送 Telegram。
# N = 6（≈ 60 秒 @ sync_interval 預設 10s）：與 SYNC_FAILURE_THRESHOLD 同量級，
# 取 2 倍是因為兩者的偽陽性成本不對稱——REST 失敗是異常，丟棄是設計中的正常結果
# （一次成交、一次資金費結算都會造成 1~2 輪丟棄），門檻必須高到讓一次 WS 事件叢集
# 不會出聲，又低到在一分鐘內就能點出「這個 symbol 的 REST 對帳已經被餓死」。
SNAPSHOT_DISCARD_WARN_THRESHOLD = 6

# 非法 sync_interval（非數／NaN／±inf／<=0）的 fallback 值。**不是下限**：
# 使用者刻意調小的合法值（例如測試用的 0.01）不受這個常數限制，
# _loop_interval() 只糾正非法值，不夾住小值。
# 值 = GlobalConfig.sync_interval 的預設值（10.0），由
# test_periodic_sync.test_fallback_equals_config_default 釘住。
# 2026-08-26 dual-review M7 從 1.0 改成 10.0（spec §10 修訂 6）：config 已經壞
# 掉的情境下把 REST 頻率拉高 10 倍是最糟的選擇——RestGateway 是單 worker、與
# place_order 共用同一條 queue，同步風暴會延遲下單、還可能吃到 Binance 權重限制。
# 舊名 MIN_SYNC_INTERVAL 一併改掉：它從來就不是「最小值」，名字與語意不符。
SYNC_INTERVAL_FALLBACK = 10.0


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
                 start_time_ms: Optional[int] = None, stop_event: Optional[asyncio.Event] = None):
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
        # 週期同步的降級狀態。這三個欄位是「同步有沒有在跑」的唯一儀器——
        # 驅動源移到常駐 task 後，沒有 tick 可以當不在場證明了。
        # 停機事件吃 bot 的共享實例（與 ws_client / userdata_watchdog / reporter 同構）。
        # 兩個選項之間選這個而不是「在 bot.stop() 補一句 sync_service.stop()」：
        # 後者只讓 bot.stop() 這一條路徑停得下來，而 bot._stop_event 是全組件的停機
        # 訊號、不只 bot.stop() 會 set 它——自造私有事件等於「set 了共享停機訊號，
        # 這條**會下單**的 loop 卻照跑」（今天沒出事只是因為 bot.stop() 剛好還會
        # task.cancel()，那是巧合不是設計）。給預設值是為了讓直接建構 SyncService
        # 的測試與工具不必被迫餵一個 Event。
        self._stop_event = stop_event if stop_event is not None else asyncio.Event()
        self._consecutive_failures = 0
        self._degraded = False
        self._degraded_total = 0    # 自啟動累計，供每日摘要用；永不重置
        # ws_seq 守衛的連續丟棄計數，key = (kind, symbol)，kind ∈ {"持倉", "掛單"}。
        # 兩種快照分開計數：它們是兩道獨立的守衛，掛單被餓死與持倉被餓死是不同的
        # 故障（前者讓網格漏掛，後者讓風控吃過期持倉），混在一起會互相稀釋。
        self._discard_streak: dict = {}

    async def sync_all(self) -> SyncOutcome:
        if self._sync_lock.locked():
            return SyncOutcome(skipped=True)
        async with self._sync_lock:
            positions_ok = await self._sync_positions()
            orders_ok = await self._sync_orders()
            account_ok = await self._sync_account()
            funding_ok = await self._sync_funding_rates()
            trade_stats_ok = await self._sync_trade_stats()
        # 心跳蓋章。放在這裡（而不是舊 maybe_sync 的節流分支裡）有兩個理由：
        # (1) bot.run() 啟動時那次 sync_all() 也會蓋章 ⇒ 心跳從開機就正確；
        # (2) skipped 的 early-return 走不到這行 ⇒ 「鎖被佔住、實際沒同步」不會
        #     被誤蓋成一次成功的心跳。這個時戳是每日摘要「同步是不是停擺了」的
        #     唯一來源（見 reporting._get_sync_status / notifier._format_sync_line）。
        # 用 guard_now()（牆鐘）而非 now()（情境時鐘）：後者會被 backtester 換成
        # 歷史 epoch，live 與回測同行程時會讓心跳年齡變成天文數字。
        self.last_sync_time = clock.guard_now()
        return SyncOutcome(
            positions_ok=positions_ok, orders_ok=orders_ok, account_ok=account_ok,
            funding_ok=funding_ok, trade_stats_ok=trade_stats_ok,
        )

    # `maybe_sync()` 已於 2026-08-26 刪除（spec §10 修訂紀錄）。移除 _handle_ticker
    # 的呼叫後它只剩 run() 一個呼叫端，而 run() 的 asyncio.sleep 本身就是節流器；
    # 兩者用的還是不同時鐘（sleep = event loop 的 monotonic，節流 = guard_now() 牆鐘），
    # NTP slew 下 10s sleep 後牆鐘可能只走 9.995s ⇒ 該輪回 None、週期靜默變兩倍，
    # 而 _evaluate(None) 刻意不計數 ⇒ 不留任何痕跡。第二把閘門不提供保護，只提供
    # 失效模式，故整個移除，run() 直接呼叫 sync_all()。

    def _evaluate(self, outcome: Optional[SyncOutcome], loop_error: bool = False):
        """依一輪結果推進降級狀態並告警。

        只看關鍵項（持倉=風控輸入、帳戶=保證金告警輸入）。skipped(lock 佔用)
        既不算成功也不算失敗——當成功會讓「鎖被佔住、其實沒同步」洗掉計數，
        當失敗則會在正常的並發 sync_all() 下誤報。
        `outcome is None` 只剩 loop_error=True 那條路徑在用（保留 None 的容忍，
        呼叫端傳 None 而忘了帶 loop_error 時維持「不動計數」的保守語意）。
        """
        if loop_error:
            failed = True
        elif outcome is None or outcome.skipped:
            return
        else:
            failed = not outcome.critical_ok

        if failed:
            self._consecutive_failures += 1
            if self._consecutive_failures >= SYNC_FAILURE_THRESHOLD and not self._degraded:
                self._degraded = True
                self._degraded_total += 1
                self._notify(
                    f"⚠️ REST 同步降級：持倉/帳戶同步連續失敗 "
                    f"{self._consecutive_failures} 次，風控輸入可能過期"
                )
            return

        self._consecutive_failures = 0
        if self._degraded:
            self._degraded = False
            self._notify("✅ REST 同步已恢復")

    def _record_discard(self, kind: str, symbol: str):
        """ws_seq 守衛丟棄了一份快照：推進連續計數，跨門檻就記一行 warning。

        只記 log、**不碰降級狀態機**（理由見 SNAPSHOT_DISCARD_WARN_THRESHOLD）。
        跨門檻後每再滿 N 輪才印一次（N、2N、3N…），而不是「只印一次」也不是
        「每輪都印」：只印一次的話，一個永久被餓死的 symbol 在整個引擎生命週期
        裡只留下一行、之後完全無跡可循；每輪都印則會在實盤把 log 洗掉。
        """
        streak = self._discard_streak.get((kind, symbol), 0) + 1
        self._discard_streak[(kind, symbol)] = streak
        if streak % SNAPSHOT_DISCARD_WARN_THRESHOLD == 0:
            logger.warning(
                f"[sync] {symbol} {kind}快照已連續 {streak} 輪被 WS 版本守衛丟棄——"
                f"該 symbol 的 REST 對帳實質停擺（狀態仍由 WS 維護），"
                f"請確認 WS 事件密度是否異常"
            )

    def _record_snapshot_applied(self, kind: str, symbol: str):
        """快照成功寫入 ⇒ 連續計數歸零（用 pop 避免字典無限長大）。"""
        self._discard_streak.pop((kind, symbol), None)

    def _notify(self, message: str):
        """告警送出。作法逐字沿用 userdata_watchdog.py 的 _notify：

        存引用防止 task 在執行前被 GC；完成後自移除避免長跑累積；無 event loop
        時只留 log（不退回 asyncio.run —— 那是純為了讓同步測試能跑而存在的
        生產程式碼路徑，專案規則 9 禁止兩個 pattern 混用）。

        **log 一律先寫，與 notifier 是否啟用無關**（dual-review M8）：原本
        `notifier.enabled` 為 False 時這個方法直接 return，降級/恢復的狀態轉換
        連一行 log 都不會留 ⇒ 沒設 Telegram 的部署等於整個降級狀態機不可觀測。
        """
        logger.warning(f"[sync] {message}")
        if not self.notifier.enabled:
            return
        try:
            asyncio.get_running_loop()
            task = asyncio.create_task(self.notifier.send(message))
            self.tasks.append(task)
            task.add_done_callback(lambda t: t in self.tasks and self.tasks.remove(t))
        except RuntimeError:
            logger.warning(f"[sync] 無 event loop，通知未送出: {message}")

    def _loop_interval(self) -> float:
        """本輪 sleep 秒數。每輪重讀 config，讓執行中改設定下一輪就生效。

        只糾正非法值（非數／NaN／±inf／<=0），不夾合法小值——使用者刻意調小
        （例如測試用的 0.01）是合法意圖，只有非法值才需要糾正到
        SYNC_INTERVAL_FALLBACK。

        `+inf` 必須擋（dual-review B3）：`asyncio.sleep(inf)` 不會醒，`_stop_event`
        也叫不醒它（sleep 不受 event 中斷），執行中把設定改回正常值同樣救不回來
        （每輪才重讀 config，而這一輪永遠不會結束）⇒ REST 同步整條停擺、降級狀態機
        一次都不會被推進 = 完全靜默。`notifier._format_sync_line` 對同一個量特地擋了
        ±inf，producer 端漏掉才是問題所在。

        **本函式必須是 total function（任何輸入都回一個合法秒數，絕不拋例外）**：
        它被 `run()` 用在 `await asyncio.sleep(self._loop_interval())` 這一整句裡，
        求值失敗會讓 sleep 根本沒被執行，例外被 loop 的 `except Exception` 接住後
        立刻進下一輪、再拋 ⇒ 100% CPU 忙迴圈 + 每輪一行 logger.error（實盤引擎會
        以幾十萬行/秒寫 log）。原本只接 `(TypeError, ValueError)` 擋不住
        `self.config` 為 None 或 `sync_interval` 屬性消失時的 `AttributeError`
        （見最終 review I3），故放寬到 `except Exception`。
        run() 那邊另有一道「本輪沒 sleep 到就補睡」的保險，兩道是刻意重疊的：
        這裡是「不要製造沒有 sleep 的一輪」，那裡是「就算製造了也不能變忙迴圈」。
        """
        try:
            interval = float(self.config.sync_interval)
        except Exception as e:
            logger.warning(f"[sync] sync_interval 讀取/轉換失敗({e})，"
                           f"本輪改用 fallback {SYNC_INTERVAL_FALLBACK}s")
            return SYNC_INTERVAL_FALLBACK
        # isfinite 一次擋掉 NaN 與 ±inf（-inf 也會被 <= 0 擋，但寫在同一個判斷裡
        # 語意更清楚：這裡要的是「一個有限的正秒數」）。
        if not math.isfinite(interval) or interval <= 0:
            logger.warning(f"[sync] sync_interval 非法({interval})，"
                           f"本輪改用 fallback {SYNC_INTERVAL_FALLBACK}s")
            return SYNC_INTERVAL_FALLBACK
        return interval

    async def run(self):
        """常駐同步驅動。移除 _handle_ticker 的呼叫後，這是唯一驅動源。

        例外一律吞掉續跑：這個 task 一死，REST 同步完全消失（比改動前更糟），
        所以它不能有「因為某次同步炸了就退出」的分支。CancelledError 例外——
        那是 bot.stop() 的收尾訊號，必須讓它穿過去。

        直接呼叫 `sync_all()`，不經節流：這個 sleep 就是節流器，再疊一把用不同
        時鐘的閘門只會製造靜默漏拍（見上方 maybe_sync 的墓誌銘與 spec §10）。

        `slept` 是 I3 的守衛：`await asyncio.sleep(...)` 求值失敗（例如
        `self.config` 為 None）時，本輪連 sleep 都沒發生，直接 continue 會變成
        100% CPU 的忙迴圈。任何走到 `except Exception` 而本輪未曾 sleep 的路徑，
        都必須先補睡一次 fallback 才允許進下一輪。
        """
        while not self._stop_event.is_set():
            slept = False
            try:
                await asyncio.sleep(self._loop_interval())
                slept = True
                # 睡醒後再確認一次停機訊號（sleep 期間 bot.stop() 可能已經 set）。
                # 這兩行是唯一擋住「共享停機訊號已 set，卻又跑一輪 _sync_account →
                # check_trailing_stop → close_symbol_positions（送市價平倉單）」的
                # 東西——while 的條件只在**進入**這一輪時檢查，擋不到 sleep 中途
                # 才被 set 的情況。由 test_stop_set_during_sleep_skips_that_round 守。
                if self._stop_event.is_set():
                    break
                await self.sync_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[sync] 週期同步失敗: {e}")
                self._evaluate(None, loop_error=True)
                if not slept:
                    try:
                        await asyncio.sleep(SYNC_INTERVAL_FALLBACK)
                    except asyncio.CancelledError:
                        break

    async def sync_once(self) -> SyncOutcome:
        """一輪同步 + 評估。**所有呼叫端只該用這個**（dual-review B5）。

        `sync_all()` 保持純粹（只做同步、回報成敗，既有並發測試直接用它），
        「一輪同步的結果必須被評估」這條不變式收斂在這裡一處——原本它散在
        `run()` 與 `bot.run()` 兩個檔案，任何一邊漏掉就是一整輪不計數而且靜默。
        """
        outcome = await self.sync_all()
        self._evaluate(outcome)
        return outcome

    def stop(self):
        """停整個 bot，不只停這條 loop。

        `_stop_event` 是 bot 傳進來的**共享**停機訊號（與 ws_client /
        userdata_watchdog / reporter 同一個實例，見 __init__ 的說明），所以呼叫
        這個方法會一併停掉那些組件。刻意如此——反過來（私有事件）等於「共享
        停機訊號已 set，這條會下單的 loop 卻照跑」。副作用寫在這裡是因為方法名
        `stop()` 讀起來像是只停自己。
        """
        self._stop_event.set()

    async def _sync_funding_rates(self) -> bool:
        """同步所有交易對的 funding rate。

        逐 symbol try/except（與 _sync_orders 同構）：這個方法排在 sync_all() 裡
        `_sync_trade_stats` **之前**，例外從這裡冒泡會讓後面的子項整批不執行。
        原本它完全沒有 try/except，而 _sync_trade_stats 的外層保險註解卻宣稱
        「兄弟方法每一個都保證不拋例外」——那句話當時是假的（見 dual-review C1）。

        失敗路徑已於 2026-08-26 改變（週期同步 branch）：例外原本走
        `_handle_ticker → ws_client outer except = 強制重連`，失敗持續發生時會變成
        每 5 秒重連一次的永久迴圈。那條呼叫鏈已不存在——現在例外冒到 `run()` 的
        `except Exception`，被吞掉、記一行 log、算一次失敗計數後續跑，**不再觸發
        WS 重連**。所以「別讓例外冒出去」的理由也換了：不是為了避免重連風暴，而是
        為了讓「一個 symbol 的 funding 讀不到」只降級成 `funding_ok=False`
        （非關鍵項、不進告警計數）、後面的 `_sync_trade_stats` 照跑，而不是讓
        整輪 `sync_all()` 從中間斷掉——那會連 `last_sync_time` 的心跳蓋章都跳過，
        把一次遙測失敗放大成「同步停擺」的誤報。
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
        # fetch 之前先抓每個 symbol 的 WS 版本號（見檔頭不變式）。下面那個
        # `await` 是一整趟 REST round-trip，期間 bot._handle_account_update 可以
        # 把成交後的新持倉寫進 state；沒有這道比對，apply 會拿 REST 的舊快照蓋
        # 回去 ⇒ _grid_step 的 `long_position == 0` 分岔走錯邊 ⇒ 撤掉剛掛好的
        # 網格並重新開倉（會動錢，且完全靜默）。
        seq_before = {s: st.ws_seq for s, st in self.state.symbols.items()}
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
                if st.ws_seq != seq_before.get(symbol):
                    # WS 在 fetch→apply 窗口內動過這個 symbol ⇒ REST 快照已過期，
                    # 丟棄**這一個 symbol**（不是整輪；其他 symbol 的快照仍然有效）。
                    # 不重試：下一輪 sync_all 自然會補上，而 WS 的值比 REST 新。
                    # 這行 log 常態出現代表 WS 事件密度已經高到 REST 對帳被持續
                    # 餓死（每 symbol 每輪至多一行，不會洗版）。
                    logger.info(f"[sync] {symbol} 持倉快照過期（WS 在 fetch 期間更新），"
                                f"本輪丟棄，下一輪重取")
                    self._record_discard("持倉", symbol)
                    continue
                st.long_position = long_pos
                st.short_position = short_pos
                st.unrealized_pnl = upnl
                self._record_snapshot_applied("持倉", symbol)
        return True

    async def _sync_orders(self) -> bool:
        ok = True
        for sym_config in self.config.symbols.values():
            if not sym_config.enabled:
                continue
            symbol = sym_config.ccxt_symbol

            try:
                # 同 _sync_positions：fetch 之前抓版本號（見檔頭不變式）。這裡的
                # 反向風險是 bot._handle_order_update 把某側掛單計數歸零、正要重掛，
                # REST 舊快照把它寫回非 0 ⇒ _should_adjust_grid 回 False ⇒ 該側網格
                # 靜默漏掛，最長一整個 sync_interval。
                pre = self.state.symbols.get(symbol)
                seq_before = pre.ws_seq if pre else None
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
                    if state.ws_seq != seq_before:
                        # 丟棄這個 symbol 的掛單快照（理由同 _sync_positions）。
                        # seq_before 為 None（fetch 期間才被加進 state.symbols）
                        # 也走這條：沒有 before 可比就不敢蓋。
                        logger.info(f"[sync] {symbol} 掛單快照過期（WS 在 fetch 期間更新），"
                                    f"本輪丟棄，下一輪重取")
                        self._record_discard("掛單", symbol)
                        continue
                    state.buy_long_orders, state.sell_long_orders, \
                        state.buy_short_orders, state.sell_short_orders = counts
                    self._record_snapshot_applied("掛單", symbol)
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

        節流時鐘用 `guard_now()` 不是 `now()`（dual-review B4，與檔頭規約一致）：
        `now()` 會被 backtester 換成歷史 epoch，邊實盤邊點回測時 `now() -
        _last_trade_stats_at` 是大負數 ⇒ 每輪 early-return、成交統計靜默凍結；
        回測結束 `reset_clock()` 後時間戳卡在歷史 epoch ⇒ 節流失效 ⇒ 每 10s 打
        一次 fetch_my_trades（正是 test_body_exception_does_not_disable_throttle
        警告過的「靜默變成 6 倍 API 權重」）。

        口徑注意：舊路徑（userData ORDER_TRADE_UPDATE）數的是「FILLED 事件」，每張單一次；
        這裡數的是 `fetch_my_trades` 回傳的成交紀錄，部分成交會拆成多筆，口徑因此略有偏高。
        非嚴格等價，但實務影響小（BNBUSDC 常見單量 0.02，很少發生部分成交拆單）。
        """
        if clock.guard_now() - self._last_trade_stats_at < TRADE_STATS_INTERVAL:
            return True

        try:
            await self._sync_trade_stats_body()
            return True
        except Exception as e:
            # 兄弟方法（_sync_positions/_sync_orders/_sync_account/_sync_funding_rates）
            # 各自都有整段/逐 symbol 的 try/except（_sync_funding_rates 的那道是
            # dual-review C1 才補上的——在那之前這句註解是假的，例外從那個兄弟方法
            # 仍然暢通）。
            # 例外冒泡的路徑已於 2026-08-26 改變（週期同步 branch）：原本是
            # _handle_ticker → ws_client.py 的 outer except（handler 例外=強制重連）
            # ⇒ 失敗持續發生時變成每 5 秒重連一次的永久迴圈（見 security-fix
            # Medium-1）。那條呼叫鏈已不存在。現在例外會冒過 sync_all() 的
            # `async with self._sync_lock`（鎖會正常釋放，這點沒變）到 run() 的
            # `except Exception`，被吞掉續跑——代價是**中斷本輪 sync_all()**：
            # `last_sync_time` 不會蓋章、SyncOutcome 不會產生，這一輪只會被記成一次
            # loop 級失敗（`_evaluate(None, loop_error=True)`）。這層保險存在的理由
            # 因此變成：不要讓一個遙測項的例外把整輪同步的心跳與逐項成敗一起吃掉。
            # 內層分頁 try/except 已經處理絕大多數失敗，這層是最後一道保險，不改變
            # 內層「整批丟棄/單筆跳過」的既有語意。
            logger.error(f"同步成交統計失敗（外層保險，不應常態觸發）: {e}")
            return False
        finally:
            # 節流時間戳必須在 finally 推進：放在 body 最後一行時，body 拋例外
            # （正是這層保險要接的那種）會讓時間戳永不前進 ⇒ 之後每一次 sync_all()
            # （每 10s）都重打 fetch_my_trades，靜默變成 6 倍 API 權重，且每輪重做
            # 同一批 pending 計算（見 dual-review B1）。
            self._last_trade_stats_at = clock.guard_now()

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
                        # 舊理由（呼叫鏈 ws_client handler → bot.py maybe_sync →
                        # sync_all → 這裡，無上限會吃光 recv/ping/watchdog 的時間片，
                        # 見 security-fix Medium-2）已隨 2026-08-26 移除 ticker driver
                        # 而失效。新理由見 TRADE_STATS_MAX_PAGES_PER_SYNC 的定義處：
                        # 擋的是「單輪同步無限期佔住 _sync_lock ⇒ 之後每一輪都
                        # skipped ⇒ 降級狀態機一次都不會被推進 = 完全靜默」。
                        # 已處理的 pending_n/pending_pnl/游標照常在迴圈
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
