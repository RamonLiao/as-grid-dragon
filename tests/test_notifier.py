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
    async def test_notify_crash_formats_message(self):
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        await notifier.notify_crash("RuntimeError: boom")
        notifier.send.assert_called_once()
        msg = notifier.send.call_args[0][0]
        assert "RuntimeError: boom" in msg
        assert "崩潰" in msg

    @pytest.mark.asyncio
    async def test_notify_restart(self):
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        await notifier.notify_restart()
        notifier.send.assert_called_once()
        msg = notifier.send.call_args[0][0]
        assert "重啟" in msg

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
