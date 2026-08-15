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

    def check(self):
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
        except RuntimeError:
            # 無 running loop（同步測試環境）：沒有迴圈可掛 fire-and-forget task，
            # 直接同步跑完這個 coroutine，行為對呼叫端等價（訊息仍會送出）。
            asyncio.run(self.notifier.send(message))
            return
        task = asyncio.create_task(self.notifier.send(message))
        self.tasks.append(task)
        task.add_done_callback(lambda t: t in self.tasks and self.tasks.remove(t))
