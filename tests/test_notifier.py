"""TelegramNotifier 單元測試"""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch

from grid_engine.notifier import TelegramNotifier


class TestTelegramNotifier:
    """基本功能測試"""

    def test_init_with_valid_config(self):
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        assert notifier.bot_token == "123:ABC"
        assert notifier.chat_id == "456"
        assert notifier.enabled is True

    def test_init_disabled_when_empty(self):
        notifier = TelegramNotifier(bot_token="", chat_id="")
        assert notifier.enabled is False

    def test_init_disabled_when_partial(self):
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="")
        assert notifier.enabled is False

    @pytest.mark.asyncio
    async def test_send_when_disabled(self):
        notifier = TelegramNotifier(bot_token="", chat_id="")
        result = await notifier.send("test")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_failure_no_crash(self):
        """發送失敗不應該拋出異常"""
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        with patch("aiohttp.ClientSession.post", side_effect=Exception("network error")):
            result = await notifier.send("test message")
            assert result is False

    @pytest.mark.asyncio
    async def test_send_failure_masks_bot_token_in_log(self, caplog):
        """security-fix Low-4：aiohttp 連線類例外字串化會帶出完整 URL（含 token）；
        log 內容不得出現 token 明文（該檔會被人工貼出、也在 repo 目錄下）。"""
        import logging
        token = "123456789:AAExampleSecretTokenValue"
        notifier = TelegramNotifier(bot_token=token, chat_id="456")
        boom = Exception(
            f"Cannot connect to host api.telegram.org:443 ssl:default "
            f"[https://api.telegram.org/bot{token}/sendMessage]"
        )
        caplog.set_level(logging.WARNING, logger="as_grid_max")
        with patch("aiohttp.ClientSession.post", side_effect=boom):
            result = await notifier.send("test message")
            assert result is False
        assert token not in caplog.text, "bot token 明文洩漏進 log"

    @pytest.mark.asyncio
    async def test_send_failure_with_empty_token_does_not_crash(self, caplog):
        """bot_token 為空字串時 str.replace 不能炸出奇怪結果（notifier.enabled 為
        False 時 send() 提早 return，這裡直接測 _redact 本身涵蓋這個邊界）。"""
        notifier = TelegramNotifier(bot_token="", chat_id="456")
        assert notifier._redact(Exception("some error")) == "some error"

    @pytest.mark.asyncio
    async def test_notify_crash_formats_message(self):
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        await notifier.notify_crash("RuntimeError: boom")
        notifier.send.assert_called_once()
        msg = notifier.send.call_args[0][0]
        assert "RuntimeError: boom" in msg
        assert "崩潰" in msg

    @pytest.mark.asyncio
    async def test_notify_crash_redacts_bot_token(self):
        """dual-review C5：notify_crash(error) 把原始例外字串塞進**要送出去的訊息
        本體**，卻沒過 _redact —— redaction 原本只掛在 send() 的 except 分支。
        aiohttp 例外字串化會帶出含 token 的完整 request URL，等於直接把 token
        發到 Telegram 頻道（比印進 log 更糟：訊息可被轉發）。

        mutation：把 `safe_error = self._redact(error)` 改回直接用 `error`
        ⇒ 紅在 `assert token not in msg`。
        """
        token = "123456789:AAExampleSecretTokenValue"
        notifier = TelegramNotifier(bot_token=token, chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        await notifier.notify_crash(
            f"ClientConnectorError: [https://api.telegram.org/bot{token}/sendMessage]"
        )
        msg = notifier.send.call_args[0][0]
        assert token not in msg, "bot token 明文被發到 Telegram 訊息裡"
        assert "***" in msg, "遮蔽後的佔位符必須還在（證明真的走了 redact）"

    @pytest.mark.asyncio
    async def test_notify_restart(self):
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        await notifier.notify_restart()
        notifier.send.assert_called_once()
        msg = notifier.send.call_args[0][0]
        assert "重啟" in msg

    @pytest.mark.asyncio
    async def test_notify_start(self):
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        await notifier.notify_start()
        notifier.send.assert_called_once()
        msg = notifier.send.call_args[0][0]
        assert "啟動" in msg

    @pytest.mark.asyncio
    async def test_notify_stop(self):
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        await notifier.notify_stop()
        notifier.send.assert_called_once()
        msg = notifier.send.call_args[0][0]
        assert "停止" in msg

    @pytest.mark.asyncio
    async def test_notify_daily_pnl(self):
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        pnl_data = {
            "total_pnl": 12.5,
            "total_equity": 1000.0,
            "positions": {"XRP/USDC:USDC": 3.0},
            "running_hours": 24,
        }
        await notifier.notify_daily_pnl(pnl_data)
        notifier.send.assert_called_once()
        msg = notifier.send.call_args[0][0]
        assert "12.5" in msg

    @pytest.mark.asyncio
    async def test_notify_risk_alert(self):
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        await notifier.notify_risk_alert("保證金率過低: 85%")
        notifier.send.assert_called_once()
        msg = notifier.send.call_args[0][0]
        assert "保證金率過低" in msg


class TestConfigTelegram:
    """Config 整合 Telegram 欄位測試"""

    def test_default_telegram_fields(self):
        from grid_engine.config import GlobalConfig
        config = GlobalConfig()
        assert config.telegram_bot_token == ""
        assert config.telegram_chat_id == ""

    def test_telegram_serialization(self):
        from grid_engine.config import GlobalConfig
        config = GlobalConfig()
        config.telegram_bot_token = "123:ABC"
        config.telegram_chat_id = "456"
        d = config.to_dict()
        assert d["telegram_bot_token"] == "123:ABC"
        assert d["telegram_chat_id"] == "456"

    def test_telegram_deserialization(self):
        from grid_engine.config import GlobalConfig
        data = {"telegram_bot_token": "123:ABC", "telegram_chat_id": "456"}
        config = GlobalConfig.from_dict(data)
        assert config.telegram_bot_token == "123:ABC"
        assert config.telegram_chat_id == "456"

    def test_backward_compat_no_telegram(self):
        """舊 config 沒有 telegram 欄位不應 crash"""
        from grid_engine.config import GlobalConfig
        config = GlobalConfig.from_dict({})
        assert config.telegram_bot_token == ""
        assert config.telegram_chat_id == ""
        assert config.telegram_enabled is True
        assert config.telegram_daily_pnl_hour == 20

    @pytest.mark.parametrize("bad,expected", [
        ("8", 8),          # 字串數字 → 轉 int
        ("abc", 20),       # 垃圾字串 → fallback
        (None, 20),
        (12.7, 12),        # float → 截斷
        (-1, 20),          # 超出範圍 → fallback
        (24, 20),
        (99999, 20),
    ])
    def test_daily_pnl_hour_monkey(self, bad, expected):
        """手改 config 塞垃圾值不應炸掉 daily loop"""
        from grid_engine.config import GlobalConfig
        config = GlobalConfig.from_dict({"telegram_daily_pnl_hour": bad})
        assert config.telegram_daily_pnl_hour == expected

    def test_new_telegram_fields_roundtrip(self):
        from grid_engine.config import GlobalConfig
        config = GlobalConfig()
        config.telegram_enabled = False
        config.telegram_daily_pnl_hour = 8
        d = config.to_dict()
        config2 = GlobalConfig.from_dict(d)
        assert config2.telegram_enabled is False
        assert config2.telegram_daily_pnl_hour == 8

    def test_risk_alert_enabled_roundtrip(self):
        from grid_engine.config import GlobalConfig
        config = GlobalConfig()
        assert config.telegram_risk_alert_enabled is True
        config.telegram_risk_alert_enabled = False
        config2 = GlobalConfig.from_dict(config.to_dict())
        assert config2.telegram_risk_alert_enabled is False

    @pytest.mark.parametrize("bad,expected", [
        (None, False),     # bool(None) → False
        ("yes", True),     # 非空字串 → True
        (0, False),
        (1, True),
    ])
    def test_risk_alert_enabled_monkey(self, bad, expected):
        """手改 config 塞垃圾值不應炸掉，bool() 正規化"""
        from grid_engine.config import GlobalConfig
        config = GlobalConfig.from_dict({"telegram_risk_alert_enabled": bad})
        assert config.telegram_risk_alert_enabled is expected

    def test_risk_alert_cooldown_roundtrip(self):
        from grid_engine.config import GlobalConfig
        config = GlobalConfig()
        assert config.telegram_risk_alert_cooldown == 300
        config.telegram_risk_alert_cooldown = 1800
        config2 = GlobalConfig.from_dict(config.to_dict())
        assert config2.telegram_risk_alert_cooldown == 1800

    @pytest.mark.parametrize("bad,expected", [
        ("600", 600),      # 字串數字 → 轉 int
        ("abc", 300),      # 垃圾字串 → fallback
        (None, 300),
        (0, 300),          # 非正數 → fallback
        (-60, 300),
        (90.7, 90),        # float → 截斷
    ])
    def test_risk_alert_cooldown_monkey(self, bad, expected):
        from grid_engine.config import GlobalConfig
        config = GlobalConfig.from_dict({"telegram_risk_alert_cooldown": bad})
        assert config.telegram_risk_alert_cooldown == expected

    def test_risk_alert_backward_compat(self):
        """舊 config 沒有此欄位 → 預設開"""
        from grid_engine.config import GlobalConfig
        config = GlobalConfig.from_dict({})
        assert config.telegram_risk_alert_enabled is True


class TestNotifierSwitch:
    """telegram_enabled 總開關測試"""

    def test_switch_off_disables(self):
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456", switch_on=False)
        assert notifier.enabled is False

    def test_switch_on_with_cred(self):
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456", switch_on=True)
        assert notifier.enabled is True

    def test_switch_on_without_cred(self):
        notifier = TelegramNotifier(switch_on=True)
        assert notifier.enabled is False

    @pytest.mark.asyncio
    async def test_notify_start_with_symbols(self):
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        await notifier.notify_start(symbols=["BNB/USDC:USDC"], daily_pnl_hour=8)
        msg = notifier.send.call_args[0][0]
        assert "BNB/USDC:USDC" in msg
        assert "08:00" in msg

    @pytest.mark.asyncio
    async def test_notify_daily_pnl_new_format(self):
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        await notifier.notify_daily_pnl({
            "total_pnl": 1.5,
            "total_equity": 94.49,
            "margin_usage": 0.193,
            "total_profit": 12.3,
            "positions": {"BNB/USDC:USDC": {"long": 0.5, "short": 0, "pnl": 1.5}},
            "running_hours": 24,
        })
        msg = notifier.send.call_args[0][0]
        assert "94.49" in msg
        assert "19.3%" in msg
        assert "+12.30" in msg
        assert "BNB" in msg and "L:0.5" in msg

    @pytest.mark.asyncio
    async def test_notify_daily_pnl_no_positions(self):
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        await notifier.notify_daily_pnl({})
        msg = notifier.send.call_args[0][0]
        assert "(無持倉)" in msg

    @pytest.mark.asyncio
    async def test_daily_pnl_watchdog_given_up_demands_human_intervention(self):
        """given_up 時摘要必須明確講出需要人工介入，並帶關鍵數字（重連次數、靜默時長）。"""
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        await notifier.notify_daily_pnl({
            "total_pnl": 1.0, "total_equity": 100.0,
            "positions": {}, "running_hours": 1,
            "watchdog": {"state": "given_up", "silence_seconds": 7200, "attempts": 3},
        })
        msg = notifier.send.call_args[0][0]
        assert "需人工介入" in msg
        assert "3 次" in msg
        assert "120 分鐘" in msg

    @pytest.mark.asyncio
    async def test_daily_pnl_watchdog_healthy_is_short(self):
        """healthy 時要簡短不佔版面：不得出現「需人工介入」或「重連中」字樣。"""
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        await notifier.notify_daily_pnl({
            "total_pnl": 1.0, "total_equity": 100.0,
            "positions": {}, "running_hours": 1,
            "watchdog": {"state": "healthy", "silence_seconds": 0, "attempts": 0},
        })
        msg = notifier.send.call_args[0][0]
        assert "✅" in msg and "userData" in msg
        assert "需人工介入" not in msg
        assert "重連中" not in msg

    @pytest.mark.asyncio
    async def test_daily_pnl_watchdog_degraded_is_visible(self):
        """degraded（重連中）要看得出來，且不能被誤判成 given_up 或 healthy。"""
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        await notifier.notify_daily_pnl({
            "total_pnl": 1.0, "total_equity": 100.0,
            "positions": {}, "running_hours": 1,
            "watchdog": {"state": "degraded", "silence_seconds": 300, "attempts": 1},
        })
        msg = notifier.send.call_args[0][0]
        assert "重連中" in msg
        assert "需人工介入" not in msg

    @pytest.mark.asyncio
    async def test_daily_pnl_watchdog_none_omits_line_and_does_not_crash(self):
        """watchdog 為 None（未接線／舊呼叫點）時摘要照常發出，且不含 watchdog 行。"""
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        await notifier.notify_daily_pnl({
            "total_pnl": 1.0, "total_equity": 100.0,
            "positions": {}, "running_hours": 1,
            "watchdog": None,
        })
        msg = notifier.send.call_args[0][0]
        assert "userData" not in msg
        assert notifier.send.called


class TestNotifierMonkey:
    """極端測試 — 故意把 notifier 玩壞"""

    @pytest.mark.asyncio
    async def test_send_huge_message(self):
        """超長訊息不應 crash"""
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        huge_msg = "x" * 100000
        await notifier.notify_crash(huge_msg)
        # notify_crash 會截斷到 500 字
        msg = notifier.send.call_args[0][0]
        assert len(msg) < 1000

    @pytest.mark.asyncio
    async def test_send_with_html_injection(self):
        """HTML 特殊字元不應破壞訊息格式"""
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        await notifier.notify_crash("<script>alert('xss')</script>")
        msg = notifier.send.call_args[0][0]
        assert "script" in msg

    @pytest.mark.asyncio
    async def test_send_with_unicode(self):
        """Unicode 字元不應 crash"""
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        await notifier.notify_crash("錯誤: 🔥💀 崩潰了 émojis")
        assert notifier.send.called

    @pytest.mark.asyncio
    async def test_daily_pnl_with_empty_data(self):
        """空數據不應 crash"""
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        await notifier.notify_daily_pnl({})
        assert notifier.send.called

    @pytest.mark.asyncio
    async def test_daily_pnl_watchdog_garbage_shapes_do_not_crash(self):
        """Monkey test：watchdog 欄位塞各種不合法形狀（非 dict、未知 state、
        缺 key），一律不得讓摘要發不出去。"""
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        for garbage in ["not a dict", 123, [], {}, {"state": "???"},
                        {"state": "given_up"}, {"state": None}]:
            await notifier.notify_daily_pnl({
                "total_pnl": 1.0, "total_equity": 100.0,
                "positions": {}, "running_hours": 1,
                "watchdog": garbage,
            })
        assert notifier.send.call_count == 7

    @pytest.mark.asyncio
    async def test_daily_pnl_watchdog_given_up_wrong_typed_values_still_send(self):
        """M1：given_up 時 key 存在但值型別錯（字串／None／__float__ 會拋非
        TypeError 的物件），摘要仍必須發得出去，且「需人工介入」不得掉。

        紅在：把 _format_watchdog_line 的兩個 try 拿掉後，這裡會 TypeError／
        KeyError 直接往外炸（notifier.send 根本沒被呼叫）。
        """
        class HostileNumber:
            def __float__(self):
                raise KeyError("boom")

            def __int__(self):
                raise KeyError("boom")

        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        # (watchdog, 期望出現的次數字串, 期望出現的分鐘字串)
        # 字面值寫死：fallback 常數若被改成別的數字，這裡要紅（verifier 抓到的存活 mutation）
        cases = [
            ({"state": "given_up", "silence_seconds": "7200", "attempts": 3},
             "3 次", "120 分鐘"),          # 可轉換的字串照樣算出真數字
            ({"state": "given_up", "silence_seconds": None, "attempts": None},
             "0 次", "0 分鐘"),
            ({"state": "given_up", "silence_seconds": [], "attempts": {}},
             "0 次", "0 分鐘"),
            ({"state": "given_up", "silence_seconds": HostileNumber(),
              "attempts": HostileNumber()},
             "0 次", "0 分鐘"),
        ]
        for watchdog, attempts_text, minutes_text in cases:
            await notifier.notify_daily_pnl({
                "total_pnl": 1.0, "total_equity": 100.0,
                "positions": {}, "running_hours": 1,
                "watchdog": watchdog,
            })
            msg = notifier.send.call_args[0][0]
            assert "需人工介入" in msg
            assert attempts_text in msg
            assert minutes_text in msg
        assert notifier.send.call_count == len(cases)

    @pytest.mark.asyncio
    async def test_daily_pnl_scalar_fields_wrong_typed_still_send(self):
        """M1 同型缺口：total_pnl / total_equity / margin_usage / total_profit /
        running_hours 值型別錯時，摘要仍必須發得出去，數字降級成 0。

        紅在：把 _coerce_num 拿掉（欄位改回 pnl_data.get(...)）後，f-string
        的 :+.2f / :.1% 會 TypeError 往外炸，notifier.send 根本不會被呼叫。
        """
        class HostileNumber:
            def __float__(self):
                raise KeyError("boom")

        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        garbage_values = ["abc", None, [], {}, object(), HostileNumber(),
                          float("nan"), float("inf"), float("-inf")]
        fields = ["total_pnl", "total_equity", "margin_usage",
                  "total_profit", "running_hours"]
        calls = 0
        for field in fields:
            for bad in garbage_values:
                await notifier.notify_daily_pnl({
                    "total_pnl": 1.0, "total_equity": 100.0,
                    "margin_usage": 0.5, "total_profit": 2.0,
                    "positions": {}, "running_hours": 1,
                    field: bad,
                })
                calls += 1
                msg = notifier.send.call_args[0][0]
                assert "每日損益摘要" in msg
                assert "nan" not in msg and "inf" not in msg
        assert notifier.send.call_count == calls

    @pytest.mark.asyncio
    async def test_daily_pnl_bad_field_falls_back_to_zero_not_some_other_constant(self):
        """fallback 必須是 0（不是隨便一個常數）—— 逐欄位斷言字面值，
        _coerce_num 的 default 被改成別的數字時要紅。"""
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        await notifier.notify_daily_pnl({
            "total_pnl": "x", "total_equity": "x", "margin_usage": "x",
            "total_profit": "x", "running_hours": "x", "positions": {},
        })
        msg = notifier.send.call_args[0][0]
        assert "帳戶權益: 0.00 USDC" in msg
        assert "保證金使用率: 0.0%" in msg
        assert "未實現 PnL: <b>+0.00</b>" in msg
        assert "累計已實現: +0.00" in msg
        assert "運行: 0.0 小時" in msg

    @pytest.mark.asyncio
    async def test_daily_pnl_positions_garbage_shapes_still_send(self):
        """positions 本身不是 dict、或倉位欄位型別錯，摘要仍要發得出去。

        紅在：拿掉 positions 的 isinstance 守衛 → 'not a dict'.items() AttributeError；
        拿掉 long/short 的 _coerce_num → 'abc' > 0 TypeError。
        """
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        bad_positions = [
            "not a dict",
            123,
            [{"long": 1}],
            None,
            {"BNB/USDC:USDC": {"long": "abc", "short": None, "pnl": "x"}},
            {"BNB/USDC:USDC": {"long": float("nan"), "short": float("inf"), "pnl": float("nan")}},
            {None: {"long": 1, "short": 0, "pnl": 1.0}},
            {12345: 3.0},
        ]
        for positions in bad_positions:
            await notifier.notify_daily_pnl({
                "total_pnl": 1.0, "total_equity": 100.0,
                "positions": positions, "running_hours": 1,
            })
            msg = notifier.send.call_args[0][0]
            assert "每日損益摘要" in msg
        assert notifier.send.call_count == len(bad_positions)

    @pytest.mark.asyncio
    async def test_daily_pnl_position_symbol_html_is_escaped(self):
        """parse_mode=HTML：標的名稱裡的 < > & 必須跳脫，否則 Telegram 回 400，
        整封摘要發不出去。紅在：拿掉 _escape。"""
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        await notifier.notify_daily_pnl({
            "total_pnl": 1.0, "total_equity": 100.0, "running_hours": 1,
            "positions": {"<b>&evil": {"long": 1, "short": 0, "pnl": 1.0}},
        })
        msg = notifier.send.call_args[0][0]
        assert "&lt;b&gt;&amp;evil" in msg
        assert "<b>&evil" not in msg

    @pytest.mark.asyncio
    async def test_daily_pnl_caps_position_lines(self):
        """持倉數量爆掉時要截斷 —— Telegram 單則 4096 字元，超過整封發不出去。

        紅在：拿掉 MAX_POSITION_LINES 迴圈上限 → 200 行全印，訊息超過 4096。
        """
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        positions = {
            f"SYM{i}/USDC:USDC": {"long": 1.0, "short": 1.0, "pnl": -1.0}
            for i in range(200)
        }
        await notifier.notify_daily_pnl({
            "total_pnl": 1.0, "total_equity": 100.0,
            "positions": positions, "running_hours": 1,
        })
        msg = notifier.send.call_args[0][0]
        assert len(msg) < 4096
        assert "另有 180 個標的未列" in msg

    @pytest.mark.asyncio
    async def test_daily_pnl_non_dict_payload_still_sends(self):
        """pnl_data 整包不是 dict（上游回傳 None／list）時也不得炸。"""
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        for payload in [None, [], "oops", 42]:
            await notifier.notify_daily_pnl(payload)
        assert notifier.send.call_count == 4

    @pytest.mark.asyncio
    async def test_send_truncates_over_telegram_limit(self):
        """訊息超過 4096 字元時 send() 必須自己截斷 —— Telegram 會直接回 400，
        整封等於沒送到。紅在：拿掉 _truncate。"""
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        sent = {}

        class FakeResp:
            status = 200

            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *a):
                return False

            async def text(self_inner):
                return ""

        class FakeSession:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *a):
                return False

            def post(self_inner, url, json=None, timeout=None):
                sent["text"] = json["text"]
                return FakeResp()

        with patch("grid_engine.notifier.aiohttp.ClientSession", lambda: FakeSession()):
            ok = await notifier.send("<b>" + "x" * 10000 + "</b>")
        assert ok is True
        assert len(sent["text"]) <= TelegramNotifier.TELEGRAM_MAX_CHARS
        assert "訊息過長已截斷" in sent["text"]

    def test_truncate_drops_html_tags_so_telegram_can_parse(self):
        """截斷版一律拿掉 HTML 標籤：切一半的 `<b` 與開了沒關的 `<b>` 都會讓
        Telegram 回 400。紅在：_truncate 改成單純切片不拿掉標籤。"""
        long_msg = "<b>標題</b>\n" + "".join(f"<b>{i}</b>行內容內容內容\n" for i in range(600))
        out = TelegramNotifier._truncate(long_msg)
        assert len(out) <= TelegramNotifier.TELEGRAM_MAX_CHARS
        assert "<" not in out and ">" not in out
        assert "標題" in out

    def test_truncate_leaves_short_message_untouched(self):
        """沒超過上限就一個字都不能動。"""
        msg = "<b>每日損益摘要</b>\n帳戶權益: 94.49 USDC"
        assert TelegramNotifier._truncate(msg) == msg

    @pytest.mark.asyncio
    async def test_daily_pnl_with_negative_values(self):
        """負數值正常處理"""
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        pnl_data = {
            "total_pnl": -999.99,
            "total_equity": -100,
            "positions": {},
            "running_hours": 0,
        }
        await notifier.notify_daily_pnl(pnl_data)
        msg = notifier.send.call_args[0][0]
        assert "-999.99" in msg

    @pytest.mark.asyncio
    async def test_concurrent_sends(self):
        """並發發送不應 crash"""
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        tasks = [notifier.notify_risk_alert(f"alert {i}") for i in range(50)]
        await asyncio.gather(*tasks)
        assert notifier.send.call_count == 50

    def test_notifier_with_none_values(self):
        """None 值不應 crash"""
        notifier = TelegramNotifier(bot_token=None, chat_id=None)
        assert notifier.enabled is False
