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
