"""每日損益摘要排程（Asia/Taipei 整點）。行為原樣搬移。"""
import asyncio
from datetime import datetime

from .utils import logger


class DailyReporter:
    def __init__(self, config, state, notifier, stop_event: asyncio.Event):
        self.config = config
        self.state = state
        self.notifier = notifier
        self._stop_event = stop_event

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

                positions = {}
                for sym, sym_state in self.state.symbols.items():
                    if sym_state.long_position > 0 or sym_state.short_position > 0:
                        positions[sym] = {
                            "long": sym_state.long_position,
                            "short": sym_state.short_position,
                            "pnl": sym_state.unrealized_pnl,
                        }

                running_hours = 0
                if self.state.start_time:
                    running_hours = (datetime.now() - self.state.start_time).total_seconds() / 3600

                pnl_data = {
                    "total_pnl": self.state.total_unrealized_pnl,
                    "total_equity": self.state.total_equity,
                    "margin_usage": self.state.margin_usage,
                    "total_profit": self.state.total_profit,
                    "positions": positions,
                    "running_hours": running_hours,
                }
                await self.notifier.notify_daily_pnl(pnl_data)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"每日摘要發送失敗: {e}")
                await asyncio.sleep(60)
