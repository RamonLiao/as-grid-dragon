"""每日損益摘要排程（Asia/Taipei 整點）。行為原樣搬移。"""
import asyncio
from datetime import datetime

from . import clock
from .utils import logger

_WATCHDOG_VALID_STATES = ("healthy", "degraded", "given_up")


class DailyReporter:
    def __init__(self, config, state, notifier, stop_event: asyncio.Event, watchdog=None,
                 stale_quote_source=None, sync_source=None):
        self.config = config
        self.state = state
        self.notifier = notifier
        self._stop_event = stop_event
        self.watchdog = watchdog
        self.stale_quote_source = stale_quote_source
        self.sync_source = sync_source

    def _get_watchdog_status(self):
        """讀取 watchdog 狀態供每日摘要顯示。

        硬性要求：取狀態失敗絕不能讓每日摘要發不出去——任何例外都在這裡
        被吞掉降級成「不顯示該行」（回傳 None），不得往外冒泡。只讀屬性，
        不呼叫任何會改變 watchdog 狀態的方法。
        """
        if self.watchdog is None:
            return None
        try:
            state = self.watchdog.state
            if state not in _WATCHDOG_VALID_STATES:
                return None
            silence_seconds = max(0.0, clock.now() - self.watchdog.last_event_at)
            attempts = int(self.watchdog.attempts)
            return {
                "state": state,
                "silence_seconds": silence_seconds,
                "attempts": attempts,
            }
        except Exception as e:
            logger.warning(f"[reporter] watchdog 狀態讀取失敗，摘要跳過該行: {e}")
            return None

    def _get_stale_quote_summary(self):
        """讀取價格快照過期計數供每日摘要顯示。

        硬性要求同 _get_watchdog_status：任何例外都在這裡吞掉降級成「不顯示該行」
        （回傳 None），不得往外冒泡把整封摘要弄掉。只讀，不重置計數。

        為什麼這行必須存在：_last_stale_log_at（Task 3）只在 1 小時節流窗口內
        壓抑重複 log，且不會在 symbol 恢復正常後被清除——「過期 → 恢復 →
        再過期」的第二段在窗口內不會產生新 log，只有計數會動。每日摘要是
        那個情境唯一的可見表面。

        同時帶出 last_stale_seconds_ago：counts 只會累計、永不重置，長時間
        運行的引擎一旦發生過一次過期，這個數字就永久存在且凍結，操作者無法
        再用「這行出現」當事件訊號。加這個欄位讓摘要能顯示「最近一次 X 小時
        前」，把「今天有事」跟「上個月出過事」分開——讀 _last_stale_at
        （guard_now() 時戳，與 stale_quote_counts 同來源、同 symbol 集合），
        失敗一樣降級成不帶這個欄位，不得讓整行、更不得讓整封摘要消失。
        """
        if self.stale_quote_source is None:
            return None
        try:
            counts = dict(self.stale_quote_source.stale_quote_counts)
            total = sum(int(v) for v in counts.values())
            summary = {"total": total, "symbols": counts, "last_stale_seconds_ago": None}
        except Exception as e:
            logger.warning(f"[reporter] 價格過期計數讀取失敗，摘要跳過該行: {e}")
            return None
        try:
            last_at_map = dict(getattr(self.stale_quote_source, "_last_stale_at", {}) or {})
            if last_at_map:
                most_recent = max(last_at_map.values())
                summary["last_stale_seconds_ago"] = max(0.0, clock.guard_now() - most_recent)
        except Exception as e:
            logger.warning(f"[reporter] 價格過期最近時戳讀取失敗，摘要略過該欄位: {e}")
        return summary

    def _get_sync_status(self):
        """讀 SyncService 的降級狀態供每日摘要顯示。

        硬性要求同 _get_watchdog_status：任何例外都在這裡吞掉降級成「不顯示
        該行」，不得讓整封摘要發不出去。純讀，不呼叫任何會改變狀態的方法。
        """
        if self.sync_source is None:
            return None
        try:
            status = {
                "degraded": bool(self.sync_source._degraded),
                "consecutive_failures": int(self.sync_source._consecutive_failures),
                "degraded_total": int(self.sync_source._degraded_total),
            }
        except Exception as e:
            logger.warning(f"[reporter] 同步狀態讀取失敗，摘要跳過該行: {e}")
            return None
        # 心跳（最終 review I1）：上面三個欄位量的是「降級狀態機有沒有被推進」，
        # 而狀態機自己是被 SyncService.run() 推的——那個 task 從未被建立、被
        # BaseException 帶走、或被誰 cancel 掉時，三個欄位會永遠停在
        # False/0/0，摘要那行與「一切正常」逐字元相同 = 最致命的失效模式沒有
        # 儀器。last_sync_time 是唯一由「同步真的跑完」推進的量，補進來當心跳。
        # 內層獨立 try：心跳讀不到只讓這兩個鍵缺席（formatter 會退回舊行為），
        # 不連累已經讀到的降級狀態，也不讓整封摘要發不出去。
        try:
            last = float(self.sync_source.last_sync_time)
            # last_sync_time 初值 0（引擎剛啟動、還沒有任何一輪 sync_all 跑完）
            # 不能直接相減：那會得到 ~1.8e9 秒的假年齡。用 None 表達「無年齡可
            # 算」，由 formatter 用專屬文案處理——刻意不省略那一行：sync_all()
            # 現在在 bot.run() 啟動時就會蓋章，摘要發送時（最快也是啟動後數小時）
            # 還停在 0 本身就代表沒有任何一輪同步成功結束過。
            status["last_sync_age"] = None if last <= 0 else clock.guard_now() - last
            # 停擺門檻由 formatter 算（max(60, 6*interval)），但 interval 要在這裡
            # 取——formatter 是 staticmethod，不得去讀全域狀態。用 _loop_interval()
            # 而非裸讀 config.sync_interval：它是 total function，非法設定值也回得出
            # 一個合法秒數，且純讀不改狀態。
            status["sync_interval"] = float(self.sync_source._loop_interval())
        except Exception as e:
            logger.warning(f"[reporter] 同步心跳讀取失敗，摘要略過該欄位: {e}")
        return status

    def _collect_positions(self) -> dict:
        """組持倉快照。單一標的的狀態壞掉（屬性缺失、數值型別錯）只能讓那一個
        標的消失，不得讓整封每日摘要發不出去——run() 的外層 except 是 sleep(60)
        後重算 target，那會直接把當天的摘要跳掉一整天（靜默漏送）。
        """
        positions = {}
        try:
            symbols = self.state.symbols.items()
        except Exception as e:
            logger.warning(f"[reporter] 讀取 symbols 失敗，摘要以無持倉帶出: {e}")
            return positions
        for sym, sym_state in symbols:
            try:
                if sym_state.long_position > 0 or sym_state.short_position > 0:
                    positions[sym] = {
                        "long": sym_state.long_position,
                        "short": sym_state.short_position,
                        "pnl": sym_state.unrealized_pnl,
                    }
            except Exception as e:
                logger.warning(f"[reporter] 標的 {sym} 持倉讀取失敗，該標的跳過: {e}")
        return positions

    async def run(self):
        """每日 telegram_daily_pnl_hour (Asia/Taipei, UTC+8) 整點發送損益摘要"""
        while not self._stop_event.is_set():
            try:
                now = datetime.utcnow()
                # Asia/Taipei (UTC+8) 整點 → UTC
                utc_hour = (self.config.telegram_daily_pnl_hour - 8) % 24
                target = now.replace(hour=utc_hour, minute=0, second=0, microsecond=0)
                if now >= target:
                    from datetime import timedelta
                    target += timedelta(days=1)
                wait_seconds = (target - now).total_seconds()
                await asyncio.sleep(wait_seconds)

                if self._stop_event.is_set():
                    break

                positions = self._collect_positions()

                running_hours = 0
                try:
                    if self.state.start_time:
                        running_hours = (datetime.now() - self.state.start_time).total_seconds() / 3600
                except Exception as e:
                    logger.warning(f"[reporter] 運行時數計算失敗，以 0 帶入: {e}")

                pnl_data = {
                    "total_pnl": self.state.total_unrealized_pnl,
                    "total_equity": self.state.total_equity,
                    "margin_usage": self.state.margin_usage,
                    "total_profit": self.state.total_profit,
                    "positions": positions,
                    "running_hours": running_hours,
                    "watchdog": self._get_watchdog_status(),
                    "stale_quotes": self._get_stale_quote_summary(),
                    "sync": self._get_sync_status(),
                }
                await self.notifier.notify_daily_pnl(pnl_data)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"每日摘要發送失敗: {e}")
                await asyncio.sleep(60)
