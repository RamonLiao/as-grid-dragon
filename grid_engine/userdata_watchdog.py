"""userData 靜默失效偵測與有限度復原。

背景：userData stream 自 2026-07-12 起靜默死亡至今（2026-08-15），而系統沒有任何
偵測——唯一的外部症狀是 keepalive 每 30 分鐘的 -1125，那條在 6a264d6 被修掉之後
症狀消失、故障還在。本元件處理的是「沒有儀器」這個缺陷，不是根因。

判準綁事件計數而非純時間：實盤成交率曾低到 ~1 筆/天，純時間判準會在安靜時段誤報。
"""
import asyncio

from . import clock
from .utils import logger

CHECK_INTERVAL = 60.0
DEFAULT_ORDER_THRESHOLD = 4        # 引擎 requote 一次即 4 張
DEFAULT_SILENCE_SECONDS = 600.0
BACKOFF_SECONDS = (300.0, 900.0, 2700.0)
GIVEN_UP_REMINDER_SECONDS = 3600.0  # given_up 終態下多久重複提醒一次（節流，不洗版）


class UserDataWatchdog:
    def __init__(self, ws_client, notifier, tasks, stop_event,
                 order_threshold: int = DEFAULT_ORDER_THRESHOLD,
                 silence_seconds: float = DEFAULT_SILENCE_SECONDS):
        self.ws_client = ws_client
        self.notifier = notifier
        self.tasks = tasks          # bot.tasks 共享參照：通知 task 防 GC + stop 可 cancel
        self._stop_event = stop_event
        self.order_threshold = order_threshold
        self.silence_seconds = silence_seconds

        self.state = "healthy"
        self.orders_since_event = 0
        self.last_event_at = clock.now()
        self.attempts = 0
        self.next_attempt_at = 0.0
        self._alerted = False
        self._last_given_up_log_at = 0.0

    # ---- 輸入 ----
    def record_order_action(self):
        """order_executor 每次成功下單/撤單呼叫一次。"""
        self.orders_since_event += 1

    def record_event(self):
        """userData handler 每收到一筆事件呼叫一次。唯一的復原入口。"""
        recovered = self._alerted
        self.orders_since_event = 0
        self.last_event_at = clock.now()
        self.state = "healthy"
        self.attempts = 0
        self.next_attempt_at = 0.0
        self._alerted = False
        if recovered:
            msg = "✅ userData stream 已恢復推送，成交事件重新進來了"
            logger.info(msg)
            self._notify(msg)

    # ---- 判定 ----
    def _is_dead(self) -> bool:
        # 兩個條件必須同時成立，見模組 docstring
        return (self.orders_since_event >= self.order_threshold
                and clock.now() - self.last_event_at >= self.silence_seconds)

    def _reanchor_if_clock_rewound(self, now: float):
        """時鐘倒退防呆（spec §8.1 已認列的風險；不是 monotonic 時鐘的完整解）。

        clock.now() 是 wall clock：NTP 校正/容器時間同步/人工改時間都可能讓它倒退。
        倒退之後 last_event_at 與 next_attempt_at 會停在「永遠到不了的未來」，狀態機
        從此不再判死也不再重連——「userData 靜默失效而沒有儀器」這件事原地在
        watchdog 自己身上重演。偵測到倒退就把時間基準重新錨到 now，最壞停滯上限
        收斂成一個退避週期，而不是永久卡死。
        """
        max_backoff = max(BACKOFF_SECONDS)
        if (now >= self.last_event_at
                and now >= self.next_attempt_at - max_backoff
                and now >= self._last_given_up_log_at):
            return
        logger.warning(
            f"[watchdog] 偵測到時鐘倒退（now={now:.0f} < 既有時間基準），重新錨定，"
            f"避免退避時點卡在永遠到不了的未來")
        self.last_event_at = min(self.last_event_at, now)
        self.next_attempt_at = min(self.next_attempt_at, now + max_backoff)
        self._last_given_up_log_at = min(self._last_given_up_log_at, now)

    def check(self):
        self._reanchor_if_clock_rewound(clock.now())
        if self.state == "given_up":
            # spec §5.2：終態後只 log 不動作。但終態正是最需要持續提醒的狀態
            # （目前只有進終態當下那一封 Telegram），故每隔一段時間節流提醒一次，
            # 不是每 60s 都打（會洗版）。
            now = clock.now()
            if now - self._last_given_up_log_at >= GIVEN_UP_REMINDER_SECONDS:
                self._last_given_up_log_at = now
                logger.warning(
                    "[watchdog] userData stream 仍處於 given_up：自動復原已放棄，"
                    "事件驅動路徑持續失效中，需人工介入（成交統計仍由 REST 維持）。"
                )
            return
        if not self._is_dead():
            return

        now = clock.now()
        if now < self.next_attempt_at:
            return

        if self.attempts >= len(BACKOFF_SECONDS):
            self.state = "given_up"
            self._last_given_up_log_at = now
            msg = (f"⛔ userData stream 自動復原失敗：已重連 {self.attempts} 次仍無事件推送，"
                   f"停止自動復原。成交統計改由 REST 維持，但事件驅動路徑失效中，需人工介入。")
            logger.error(msg)
            self._notify(msg)
            return

        self.attempts += 1
        self.state = "degraded"
        self.next_attempt_at = now + BACKOFF_SECONDS[self.attempts - 1]
        logger.warning(
            f"[watchdog] userData 靜默失效判定成立："
            f"{self.orders_since_event} 張單無推送、靜默 {now - self.last_event_at:.0f}s，"
            f"強制重連（第 {self.attempts}/{len(BACKOFF_SECONDS)} 次）"
        )

        # 告警排在 request_reconnect() 之前（見 whole-branch review Minor-2）：
        # attempts/state/next_attempt_at 已在上面更新完畢，request_reconnect() 若拋例外，
        # run() 的 broad except 會吞掉、狀態機不受影響——但若告警排在重連之後，
        # 例外會讓第一封「疑似靜默失效」永遠發不出去，違反 spec §6「Telegram 失敗
        # 只 log、不影響狀態機」反方向的保證（使用者仍要被通知）。故不包 try/except
        # 壓制 request_reconnect() 的例外（沒有必要，且會掩蓋真正的重連失敗），
        # 而是單純把告警移到它前面，確保告警一定送出。
        if not self._alerted:
            self._alerted = True
            self._notify(
                f"⚠️ userData stream 疑似靜默失效："
                f"已下/撤 {self.orders_since_event} 張單但零事件推送。"
                f"將嘗試自動重連最多 {len(BACKOFF_SECONDS)} 次。"
            )

        # 證據重取（見 dual-review B2）：重連之後必須用**新的**證據重新判定。
        # 不重置的話，orders_since_event / last_event_at 只有 record_event() 會清，
        # 於是「重連成功修好了 stream，但市場安靜沒有新單」（實盤成交率曾低到
        # ~1 筆/天，加上裝死模式不 requote）時，同一批陳舊證據會在 300/900/2700 秒
        # 後再判死兩次，65 分鐘內燒完三次強制重連並發出「需人工介入」的 ⛔ 告警
        # ——而 stream 其實是好的。每次假重連都會把 state.connected 切 False、
        # 中斷 bookTicker，等於在有實倉時製造 decide() 盲窗。
        # 代價：真的壞掉時，下一次判死要等新的 K 張單 + N 秒靜默重新累積；
        # 這正是「用新證據判定」的定義，也是本元件判準綁事件計數的初衷。
        self.orders_since_event = 0
        self.last_event_at = now

        self.ws_client.request_reconnect()

    # ---- 迴圈 ----
    async def run(self):
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(CHECK_INTERVAL)
                if self._stop_event.is_set():
                    break
                self.check()
            except asyncio.CancelledError:
                break
            except Exception as e:
                # watchdog 自己掛掉會讓「沒有儀器」的問題原樣重演，故吞例外續跑
                logger.error(f"[watchdog] check 失敗: {e}")

    def _notify(self, message: str):
        if not self.notifier.enabled:
            return
        try:
            asyncio.get_running_loop()
            # 存引用防止 task 在執行前被 GC；完成後自移除避免長跑累積
            task = asyncio.create_task(self.notifier.send(message))
            self.tasks.append(task)
            task.add_done_callback(lambda t: t in self.tasks and self.tasks.remove(t))
        except RuntimeError:
            # 無 event loop 時只留 log——對齊 order_executor.py 的既有 pattern
            # （dual-review D）。原本這裡退回 asyncio.run(...)，那是純粹為了讓
            # 同步測試能跑而存在的生產程式碼路徑：生產上 watchdog 永遠在 loop 裡跑，
            # 這條分支只會在測試中被走到，卻是兩個 pattern 混用（專案規則 9）。
            logger.warning(f"[watchdog] 無 event loop，通知未送出: {message}")
