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

    def __init__(self, bot_token: str = "", chat_id: str = "", switch_on: bool = True):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.switch_on = switch_on

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id) and self.switch_on

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
            logger.warning(f"Telegram 發送異常: {self._redact(e)}")
            return False

    def _redact(self, e) -> str:
        """把例外/錯誤字串裡可能出現的 bot token 遮蔽掉。

        aiohttp 的 ClientResponseError/InvalidURL 等帶 request_info 的例外，字串化
        會帶出完整 request URL（含 token），一路印進 log 檔（log/as_terminal_max.log
        會被人工貼出、也在 repo 目錄下）⇒ token 外洩（見 security-fix Low-4）。
        """
        msg = str(e)
        if self.bot_token:
            msg = msg.replace(self.bot_token, "***")
        return msg

    async def notify_crash(self, error: str):
        """Bot 崩潰通知。

        error 是原始例外字串化的結果，同樣可能挾帶 bot token（aiohttp 例外會帶出
        含 token 的 request URL）。send() 的 except 分支有 redact，但這裡是把字串
        **塞進要送出去的訊息本體**，不過 redact 等於直接把 token 發到 Telegram
        頻道（見 dual-review C5）。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        safe_error = self._redact(error)
        msg = (
            f"🚨 <b>AS Grid Bot 崩潰</b>\n"
            f"時間: {now}\n"
            f"錯誤: <code>{safe_error[:500]}</code>\n"
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

    async def notify_start(self, symbols: list = None, daily_pnl_hour: int = 20):
        """交易啟動通知"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sym_list = ", ".join(symbols) if symbols else "(無)"
        msg = (
            f"🟢 <b>AS Grid Bot 交易已啟動</b>\n"
            f"時間: {now}\n"
            f"交易對: {sym_list}\n"
            f"每日摘要: {daily_pnl_hour:02d}:00 (Asia/Taipei)"
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
        margin_usage = pnl_data.get("margin_usage", 0)
        total_profit = pnl_data.get("total_profit", 0)
        positions = pnl_data.get("positions", {})
        running_hours = pnl_data.get("running_hours", 0)

        icon = "📈" if total_pnl >= 0 else "📉"
        pos_lines = []
        for sym, pos in positions.items():
            coin = sym.split("/")[0]
            if not isinstance(pos, dict):
                # 相容舊格式：純數量
                pos_lines.append(f"  {coin}: {pos}")
                continue
            sides = []
            if pos.get("long", 0) > 0:
                sides.append(f"L:{pos['long']}")
            if pos.get("short", 0) > 0:
                sides.append(f"S:{pos['short']}")
            pos_lines.append(f"  {coin}: {', '.join(sides)} | PnL: {pos.get('pnl', 0):+.2f}")
        pos_text = "\n".join(pos_lines) or "  (無持倉)"

        watchdog_line = self._format_watchdog_line(pnl_data.get("watchdog"))

        msg = (
            f"{icon} <b>每日損益摘要</b>\n"
            f"時間: {now}\n"
            f"帳戶權益: {total_equity:.2f} USDC\n"
            f"保證金使用率: {margin_usage:.1%}\n"
            f"未實現 PnL: <b>{total_pnl:+.2f}</b>\n"
            f"累計已實現: {total_profit:+.2f}\n"
            f"運行: {running_hours:.1f} 小時\n"
            f"{watchdog_line}"
            f"\n<b>持倉概況:</b>\n{pos_text}"
        )
        await self.send(msg)

    @staticmethod
    def _format_watchdog_line(watchdog) -> str:
        """userData watchdog 狀態行（見 tasks 分支說明：終態訊號要進使用者真的
        會看的每日摘要，不能只留在 log 裡）。

        安全要求：狀態字串一律是這裡自己定義的常數，不把交易所資料或例外訊息
        未跳脫插進 HTML 訊息（parse_mode=HTML）。watchdog 為 None／格式不符
        時整行省略，不得影響既有欄位。
        """
        if not isinstance(watchdog, dict):
            return ""
        state = watchdog.get("state")
        if state == "given_up":
            silence_minutes = watchdog.get("silence_seconds", 0) / 60
            attempts = watchdog.get("attempts", 0)
            return (
                f"⛔ <b>userData 監控：已放棄自動重連，需人工介入</b>"
                f"（已重連 {attempts} 次、靜默 {silence_minutes:.0f} 分鐘）\n"
            )
        if state == "degraded":
            return "⚠️ userData 監控：重連中\n"
        if state == "healthy":
            return "✅ userData 監控：正常\n"
        return ""

    async def notify_risk_alert(self, alert: str):
        """風控警報"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        msg = (
            f"⚠️ <b>風控警報</b>\n"
            f"時間: {now}\n"
            f"警報: {alert}"
        )
        await self.send(msg)
