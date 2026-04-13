"""
Telegram 通知模組
透過 Telegram Bot API 發送交易通知
"""

import aiohttp
from datetime import datetime
from .utils import logger


class TelegramNotifier:
    """Telegram Bot 通知器"""

    TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, bot_token: str = "", chat_id: str = ""):
        self.bot_token = bot_token
        self.chat_id = chat_id

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    async def send(self, message: str) -> bool:
        """發送 Telegram 訊息，失敗不拋異常"""
        if not self.enabled:
            return False
        try:
            url = self.TELEGRAM_API.format(token=self.bot_token)
            payload = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": "HTML",
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        return True
                    else:
                        body = await resp.text()
                        logger.warning(f"Telegram 發送失敗 [{resp.status}]: {body}")
                        return False
        except Exception as e:
            logger.warning(f"Telegram 發送異常: {e}")
            return False

    async def notify_crash(self, error: str):
        """Bot 崩潰通知"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = (
            f"🚨 <b>AS Grid Bot 崩潰</b>\n"
            f"時間: {now}\n"
            f"錯誤: <code>{error[:500]}</code>\n"
            f"\n請 docker attach 檢查並重新啟動交易"
        )
        await self.send(msg)

    async def notify_restart(self):
        """Container 重啟通知"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = (
            f"🔄 <b>AS Grid Bot 已重啟</b>\n"
            f"時間: {now}\n"
            f"狀態: 等待手動操作\n"
            f"\n請 docker attach 進入操作"
        )
        await self.send(msg)

    async def notify_stop(self):
        """Bot 正常停止通知"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = (
            f"🛑 <b>AS Grid Bot 已停止</b>\n"
            f"時間: {now}\n"
            f"狀態: 正常關閉"
        )
        await self.send(msg)

    async def notify_daily_pnl(self, pnl_data: dict):
        """每日損益摘要"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        total_pnl = pnl_data.get("total_pnl", 0)
        total_equity = pnl_data.get("total_equity", 0)
        positions = pnl_data.get("positions", {})
        running_hours = pnl_data.get("running_hours", 0)

        icon = "📈" if total_pnl >= 0 else "📉"
        pos_lines = "\n".join(
            f"  {sym}: {qty}" for sym, qty in positions.items()
        ) or "  無持倉"

        msg = (
            f"{icon} <b>每日損益摘要</b>\n"
            f"時間: {now}\n"
            f"損益: <b>{total_pnl:+.2f} USDC</b>\n"
            f"權益: {total_equity:.2f} USDC\n"
            f"運行: {running_hours:.1f} 小時\n"
            f"\n<b>持倉:</b>\n{pos_lines}"
        )
        await self.send(msg)

    async def notify_risk_alert(self, alert: str):
        """風控警報"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = (
            f"⚠️ <b>風控警報</b>\n"
            f"時間: {now}\n"
            f"警報: {alert}"
        )
        await self.send(msg)
