"""
Telegram 通知模組
透過 Telegram Bot API 發送交易通知
"""

import re

import aiohttp
from datetime import datetime
from .utils import logger


class TelegramNotifier:
    """Telegram Bot 通知器"""

    TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

    #: Telegram 單則訊息硬上限。超過 API 直接回 400，整封訊息等於沒送到。
    TELEGRAM_MAX_CHARS = 4096

    def __init__(self, bot_token: str = "", chat_id: str = "", switch_on: bool = True):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.switch_on = switch_on

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id) and self.switch_on

    @classmethod
    def _truncate(cls, message: str) -> str:
        """訊息超過 Telegram 4096 字元硬上限時截斷，寧可少一段也不要整封掉。

        截斷會把 HTML 標籤切成半個（`<b` 或開了沒關的 `<b>`），Telegram 兩種
        都會回 400「can't parse entities」⇒ 截斷版一律**把標籤整個拿掉**，
        只保留純文字（`&lt;` 這類實體本來就合法，留著）。送達 > 排版。
        """
        if len(message) <= cls.TELEGRAM_MAX_CHARS:
            return message
        suffix = "\n…（訊息過長已截斷）"
        body = re.sub(r"<[^>]*>|<[^>]*$", "", message)
        return body[: cls.TELEGRAM_MAX_CHARS - len(suffix)] + suffix

    async def send(self, message: str) -> bool:
        """發送 Telegram 訊息，失敗不拋異常"""
        if not self.enabled:
            return False
        message = self._truncate(str(message))
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

    #: 持倉行上限。Telegram 單則訊息 4096 字元，超過整封摘要發不出去。
    MAX_POSITION_LINES = 20

    @staticmethod
    def _coerce_num(value, default: float = 0.0) -> float:
        """把摘要欄位強制轉成可格式化的有限浮點數。

        硬性要求是「每日摘要不得發不出去」（見 tasks/notes.md 2026-08-16）。
        任一欄位型別錯就讓整封掉光是不可接受的，因此這裡降級成
        fallback 值而不拋。`__float__` 可以拋任意例外 ⇒ `except Exception`。
        NaN / inf 不會拋但會印出 `+nan` / `inf`，一律視同無效值。
        """
        try:
            n = float(value)
        except Exception:
            return default
        if n != n or n in (float("inf"), float("-inf")):
            return default
        return n

    @staticmethod
    def _escape(text) -> str:
        """parse_mode=HTML 下任何非數值字串都要跳脫，否則 `<` 會讓 Telegram 回 400。"""
        return (
            str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    async def notify_daily_pnl(self, pnl_data: dict):
        """每日損益摘要"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not isinstance(pnl_data, dict):
            pnl_data = {}
        total_pnl = self._coerce_num(pnl_data.get("total_pnl"))
        total_equity = self._coerce_num(pnl_data.get("total_equity"))
        margin_usage = self._coerce_num(pnl_data.get("margin_usage"))
        total_profit = self._coerce_num(pnl_data.get("total_profit"))
        positions = pnl_data.get("positions", {})
        running_hours = self._coerce_num(pnl_data.get("running_hours"))

        icon = "📈" if total_pnl >= 0 else "📉"
        pos_lines = []
        omitted = 0
        if not isinstance(positions, dict):
            positions = {}
        for sym, pos in positions.items():
            if len(pos_lines) >= self.MAX_POSITION_LINES:
                omitted = len(positions) - len(pos_lines)
                break
            coin = self._escape(str(sym).split("/")[0])
            if not isinstance(pos, dict):
                # 相容舊格式：純數量
                pos_lines.append(f"  {coin}: {self._escape(pos)}")
                continue
            sides = []
            long_qty = self._coerce_num(pos.get("long"))
            short_qty = self._coerce_num(pos.get("short"))
            if long_qty > 0:
                sides.append(f"L:{long_qty:g}")
            if short_qty > 0:
                sides.append(f"S:{short_qty:g}")
            pnl = self._coerce_num(pos.get("pnl"))
            pos_lines.append(f"  {coin}: {', '.join(sides)} | PnL: {pnl:+.2f}")
        if omitted > 0:
            pos_lines.append(f"  …另有 {omitted} 個標的未列")
        pos_text = "\n".join(pos_lines) or "  (無持倉)"

        watchdog_line = self._format_watchdog_line(pnl_data.get("watchdog"))
        stale_line = self._format_stale_quote_line(pnl_data.get("stale_quotes"))
        sync_line = self._format_sync_line(pnl_data.get("sync"))

        msg = (
            f"{icon} <b>每日損益摘要</b>\n"
            f"時間: {now}\n"
            f"帳戶權益: {total_equity:.2f} USDC\n"
            f"保證金使用率: {margin_usage:.1%}\n"
            f"未實現 PnL: <b>{total_pnl:+.2f}</b>\n"
            f"累計已實現: {total_profit:+.2f}\n"
            f"運行: {running_hours:.1f} 小時\n"
            f"{watchdog_line}"
            f"{stale_line}"
            f"{sync_line}"
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
            # key 存在但值型別錯（字串、None、自訂物件）不得讓整封摘要發不出去，
            # 只降級成不帶數字的告警——「需人工介入」這句本身才是不能掉的訊號。
            try:
                silence_minutes = float(watchdog.get("silence_seconds", 0)) / 60
            except Exception:
                silence_minutes = 0.0
            try:
                attempts = int(watchdog.get("attempts", 0))
            except Exception:
                attempts = 0
            return (
                f"⛔ <b>userData 監控：已放棄自動重連，需人工介入</b>"
                f"（已重連 {attempts} 次、靜默 {silence_minutes:.0f} 分鐘）\n"
            )
        if state == "degraded":
            return "⚠️ userData 監控：重連中\n"
        if state == "healthy":
            return "✅ userData 監控：正常\n"
        return ""

    @staticmethod
    def _format_stale_quote_line(stale) -> str:
        """價格快照過期計數那一行。

        安全要求同 _format_watchdog_line：文案是這裡自己定義的常數，不把外部
        資料未跳脫插進 HTML 訊息（parse_mode=HTML）。

        兩種「省略」與一種「降級」要分清楚（行為，不是註解上的理想）：
        - 不是 dict（含 None）⇒ 整行省略；
        - 計數為 0 ⇒ 整行省略——正常狀態不加噪音；
        - total 存在但轉不成 int ⇒ **不省略**，降級成不帶數字的告警行。
          「有過期」這個訊號不能因為型別錯就掉。

        數字是「自啟動累計」不是「今日」：stale_quote_counts 從 MaxGridBot
        .__init__ 建立後全 repo 沒有任何重置點，措辭必須誠實。不做
        snapshot-diff 造假的日增量——這套引擎重啟頻繁，reporter 自造的「今日」
        會隨重啟歸零，比誠實累計更誤導。

        為什麼這行必須存在：_last_stale_log_at（Task 3）不會在 symbol 恢復
        正常後被清除，1 小時節流窗口內第二次過期不會產生新 log，只有這個
        計數會動——每日摘要是那個情境唯一的可見表面。

        「最近一次 X 前」補的是計數本身補不了的洞：累計數字只會長不會降，
        長時間運行的引擎出過一次事後這行就永久存在、數字凍結，操作者無法
        再用「這行出現」當事件訊號。時戳讓「今天有事」跟「上個月出過事」
        分得開。缺這個欄位（型別錯、None、舊呼叫端未帶）就整段省略，只降級
        不報錯——不能因為這個附加資訊讓「有過期」這個主訊號本身變得更脆弱。
        """
        if not isinstance(stale, dict):
            return ""
        try:
            total = int(stale.get("total", 0))
        except Exception:
            # 型別錯不得讓整封摘要發不出去，但「有過期」這個訊號不能掉
            return "⚠️ <b>價格快照過期</b>：計數異常，請查 log\n"
        if total <= 0:
            return ""
        last_part = ""
        try:
            last_seconds = stale.get("last_stale_seconds_ago")
            if last_seconds is not None:
                last_seconds = float(last_seconds)
                if last_seconds < 3600:
                    last_part = f"，最近一次 {last_seconds / 60:.0f} 分鐘前"
                else:
                    last_part = f"，最近一次 {last_seconds / 3600:.1f} 小時前"
        except Exception:
            last_part = ""  # 附加資訊，讀不到就不帶，不影響主訊號
        return f"⚠️ <b>價格快照過期</b>：累計 {total} 次跳過網格調整（自啟動）{last_part}\n"

    @staticmethod
    def _format_sync_line(sync) -> str:
        """REST 同步狀態那一行。

        安全要求同 _format_watchdog_line：文案是這裡自己定義的常數，不把外部
        資料未跳脫插進 HTML 訊息（parse_mode=HTML）。

        三種狀態、兩種省略：
        - 非 dict（含 None）⇒ 整行省略；
        - 正常且自啟動從未降級 ⇒ 整行省略（不加噪音）；
        - 正常但曾降級 ⇒ 顯示累計次數。這是摘要唯一能講、即時告警講不了的事：
          告警發過就過去了，「今天出過事」只有這裡看得到。
        計數口徑是「自啟動累計」不是「今日」——引擎重啟頻繁，自造的日增量
        會隨重啟歸零，比誠實累計更誤導（與 _format_stale_quote_line 同裁決）。

        **心跳優先於上面三種狀態**（最終 review I1）：degraded/degraded_total 是
        由 SyncService.run() 推進的，run() 本身死掉／從未被建立時它們永遠是
        False/0 ⇒ 「同步整條停擺」與「一切正常」的輸出逐字元相同，正是這條
        branch 要根除的形態。last_sync_age 是唯一由「同步真的跑完」推進的量，
        超過門檻就**無條件**印警告，蓋掉其他分支。門檻 max(60, 6*interval)：
        6 輪的餘裕吸收得掉單次 REST 抖動與重試，60s 的地板擋住 interval 被設得
        極小時（測試用 0.01）門檻跟著塌成毫秒級的誤報。

        壞欄位一律降級成保守文案、不整行消失（#7）：`int(None)` 讓整行回 ""
        等於「降級中的警告被一個型別錯吞掉」——fail-silent 換個位置重演。
        與 _format_stale_quote_line 的既有裁決一致：主訊號不能因為附加數字讀
        不到就掉。
        """
        if not isinstance(sync, dict):
            return ""

        # 心跳。鍵缺席（舊呼叫端／reporter 的心跳讀取降級）才跳過這一段，
        # 缺席不等於健康，所以只是退回舊行為、不假裝正常。
        if "last_sync_age" in sync:
            age = sync.get("last_sync_age")
            if age is None:
                return "⚠️ <b>REST 同步停擺</b>：自啟動以來從未完成任何一輪同步\n"
            try:
                age = float(age)
                interval = float(sync.get("sync_interval", 0) or 0)
            except Exception:
                return "⚠️ <b>REST 同步</b>：心跳讀取異常，請查 log\n"
            # NaN 的任何比較都是 False（會靜默穿過門檻判斷），±inf 會印出
            # 「距上次同步 inf 分鐘」——兩者都當成心跳異常處理，不放行。
            if not (float("-inf") < age < float("inf")):
                return "⚠️ <b>REST 同步</b>：心跳讀取異常，請查 log\n"
            if age < 0:
                # 牆鐘往回跳（NTP step / 手動改時間）。沿用 bot._note_stale_quote
                # 對同一件事的態度：不把它當成「剛同步過」而讓這行消失，重新錨定
                # 成「時距不可信」的告警——時鐘回跳本身就會讓同步靜默停擺整個跳幅。
                return "⚠️ <b>REST 同步</b>：偵測到時鐘後跳，同步時距不可信，請查 log\n"
            if not (0.0 <= interval < float("inf")):    # NaN / 負 / inf 一律不參與門檻
                interval = 0.0
            if age > max(60.0, 6.0 * interval):
                return f"⚠️ <b>REST 同步停擺</b>：距上次同步 {age / 60:.0f} 分鐘\n"

        degraded = bool(sync.get("degraded"))
        try:
            failures = int(sync.get("consecutive_failures", 0))
        except Exception:
            failures = None
        try:
            total = int(sync.get("degraded_total", 0))
        except Exception:
            total = None

        if degraded:
            if failures is None:
                return "⚠️ <b>REST 同步</b>：降級中（連續失敗次數異常，請查 log）\n"
            return f"⚠️ <b>REST 同步</b>：降級中（連續失敗 {failures} 次）\n"
        if total is None:
            return "⚠️ <b>REST 同步</b>：降級累計數異常，請查 log\n"
        if total > 0:
            return f"✅ REST 同步：正常（自啟動曾降級 {total} 次）\n"
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
