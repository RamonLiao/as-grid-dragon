"""每日損益摘要排程（Asia/Taipei 整點）。行為原樣搬移。"""
import asyncio
from datetime import datetime

from . import clock
from .utils import logger

_WATCHDOG_VALID_STATES = ("healthy", "degraded", "given_up")


class DailyReporter:
    def __init__(self, config, state, notifier, stop_event: asyncio.Event, watchdog=None):
        self.config = config
        self.state = state
        self.notifier = notifier
        self._stop_event = stop_event
        self.watchdog = watchdog

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
                }
                await self.notifier.notify_daily_pnl(pnl_data)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"每日摘要發送失敗: {e}")
                await asyncio.sleep(60)
