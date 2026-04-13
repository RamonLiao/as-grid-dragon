# Docker TUI + GCE 部署 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 將 as_terminal_max.py 打包為 Docker container，支援互動式 TUI（類似 Hummingbot），加入 Telegram 通知，部署到 GCE。

**Architecture:** 單一 GCE VM 跑一個 Docker container，TUI 透過 `docker attach` 操作。Telegram notifier 以 async task 形式嵌入 bot 生命週期，使用 aiohttp 呼叫 Telegram Bot API。SIGTERM handler 在 main thread 註冊，轉發給 bot thread 做 graceful shutdown。

**Tech Stack:** Python 3.11, Docker, docker-compose, aiohttp (已有), Rich TUI (已有), Telegram Bot API

---

## File Structure

| 檔案 | 動作 | 職責 |
|------|------|------|
| `grid_engine/notifier.py` | 新建 | TelegramNotifier — 所有通知邏輯 |
| `tests/test_notifier.py` | 新建 | notifier 單元測試 |
| `grid_engine/config.py` | 修改 | GlobalConfig 加 telegram 欄位 |
| `grid_engine/bot.py` | 修改 | 接入 notifier、每日摘要定時任務、風控警報 |
| `grid_engine/__init__.py` | 修改 | export TelegramNotifier |
| `as_terminal_max.py` | 修改 | SIGTERM handler、restart 偵測、Telegram 設定選單 |
| `Dockerfile.terminal` | 新建 | Terminal 版 Docker image |
| `docker-compose.terminal.yml` | 新建 | TUI 模式 compose |
| `.dockerignore` | 新建 | 排除敏感檔案 |
| `scripts/gce-setup.sh` | 新建 | GCE 一鍵部署腳本 |

---

### Task 1: TelegramNotifier 模組

**Files:**
- Create: `grid_engine/notifier.py`
- Create: `tests/test_notifier.py`

- [ ] **Step 1: Write failing tests for TelegramNotifier**

Create `tests/test_notifier.py`:

```python
"""TelegramNotifier 單元測試"""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

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
    async def test_send_success(self):
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        mock_response = AsyncMock()
        mock_response.status = 200

        with patch("aiohttp.ClientSession.post", return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_response),
            __aexit__=AsyncMock(return_value=False),
        )):
            result = await notifier.send("test message")
            assert result is True

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
        assert "crash" in msg.lower() or "崩潰" in msg

    @pytest.mark.asyncio
    async def test_notify_restart(self):
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        await notifier.notify_restart()
        notifier.send.assert_called_once()
        msg = notifier.send.call_args[0][0]
        assert "重啟" in msg or "restart" in msg.lower()

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
    async def test_notify_stop(self):
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        await notifier.notify_stop()
        notifier.send.assert_called_once()
        msg = notifier.send.call_args[0][0]
        assert "停止" in msg or "stop" in msg.lower()

    @pytest.mark.asyncio
    async def test_notify_risk_alert(self):
        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        await notifier.notify_risk_alert("保證金率過低: 85%")
        notifier.send.assert_called_once()
        msg = notifier.send.call_args[0][0]
        assert "保證金率過低" in msg
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/ramonliao/Documents/理財/加密貨幣/量化交易/LouisLab/as-grid-dragon && python -m pytest tests/test_notifier.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'grid_engine.notifier'`

- [ ] **Step 3: Implement TelegramNotifier**

Create `grid_engine/notifier.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/ramonliao/Documents/理財/加密貨幣/量化交易/LouisLab/as-grid-dragon && python -m pytest tests/test_notifier.py -v`
Expected: All 10 tests PASS

- [ ] **Step 5: Commit**

```bash
git add grid_engine/notifier.py tests/test_notifier.py
git commit -m "feat: 新增 TelegramNotifier 模組與測試"
```

---

### Task 2: Config 加入 Telegram 欄位

**Files:**
- Modify: `grid_engine/config.py` (GlobalConfig class, lines 104-178)

- [ ] **Step 1: Write failing test**

Append to `tests/test_notifier.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_notifier.py::TestConfigTelegram -v`
Expected: FAIL — `AttributeError: 'GlobalConfig' has no attribute 'telegram_bot_token'`

- [ ] **Step 3: Add telegram fields to GlobalConfig**

In `grid_engine/config.py`, add two fields to `GlobalConfig` dataclass (after `legacy_api_detected`):

```python
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
```

In `GlobalConfig.to_dict()`, add:

```python
            "telegram_bot_token": self.telegram_bot_token,
            "telegram_chat_id": self.telegram_chat_id,
```

In `GlobalConfig.from_dict()`, add after `legacy_api_detected=False`:

```python
            telegram_bot_token=data.get("telegram_bot_token", ""),
            telegram_chat_id=data.get("telegram_chat_id", ""),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_notifier.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add grid_engine/config.py tests/test_notifier.py
git commit -m "feat: GlobalConfig 加入 telegram_bot_token/chat_id 欄位"
```

---

### Task 3: Bot 接入 Notifier + 每日摘要 + 風控警報

**Files:**
- Modify: `grid_engine/bot.py` (MaxGridBot class)
- Modify: `grid_engine/__init__.py`

- [ ] **Step 1: Update `__init__.py` to export TelegramNotifier**

In `grid_engine/__init__.py`, add import:

```python
from .notifier import TelegramNotifier
```

Add `'TelegramNotifier'` to `__all__` list (在 `# bot` section 之後加一個 `# notifier` section)。

- [ ] **Step 2: Modify MaxGridBot.__init__ to accept notifier**

In `grid_engine/bot.py`, add import at top:

```python
from .notifier import TelegramNotifier
```

In `MaxGridBot.__init__` (after line 62 `self._stop_event = asyncio.Event()`), add:

```python
        # Telegram 通知
        self.notifier = TelegramNotifier(
            bot_token=config.telegram_bot_token,
            chat_id=config.telegram_chat_id,
        )
```

- [ ] **Step 3: Add daily PnL summary scheduled task**

In `grid_engine/bot.py`, add method to `MaxGridBot`:

```python
    async def _daily_pnl_loop(self):
        """每日 20:00 (Asia/Taipei, UTC+8) 發送損益摘要"""
        while not self._stop_event.is_set():
            try:
                now = datetime.utcnow()
                # 計算到下一個 UTC 12:00 (= Asia/Taipei 20:00) 的秒數
                target_hour = 12  # UTC 12 = Taipei 20
                target = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
                if now >= target:
                    target = target.replace(day=target.day + 1)
                wait_seconds = (target - now).total_seconds()
                await asyncio.sleep(wait_seconds)

                if self._stop_event.is_set():
                    break

                # 收集損益數據
                positions = {}
                for sym, sym_state in self.state.symbols.items():
                    net = sym_state.long_position - sym_state.short_position
                    if net != 0:
                        positions[sym] = net

                running_hours = 0
                if self.state.start_time:
                    running_hours = (datetime.now() - self.state.start_time).total_seconds() / 3600

                pnl_data = {
                    "total_pnl": self.state.total_unrealized_pnl,
                    "total_equity": self.state.total_equity,
                    "positions": positions,
                    "running_hours": running_hours,
                }
                await self.notifier.notify_daily_pnl(pnl_data)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"每日摘要發送失敗: {e}")
                await asyncio.sleep(60)
```

- [ ] **Step 4: Add risk alert method**

In `grid_engine/bot.py`, add method to `MaxGridBot`:

```python
    async def _check_risk_and_notify(self):
        """檢查風控狀態並通知"""
        if not self.notifier.enabled or not self.config.risk.enabled:
            return
        # 保證金率警報
        if self.state.margin_usage > self.config.risk.margin_threshold:
            alert = f"保證金使用率過高: {self.state.margin_usage:.1%} (閾值: {self.config.risk.margin_threshold:.1%})"
            await self.notifier.notify_risk_alert(alert)
```

- [ ] **Step 5: Wire tasks into bot.run()**

In `grid_engine/bot.py`, modify `MaxGridBot.run()`. The current `self.tasks` list (around line 867-870):

```python
        self.tasks = [
            asyncio.create_task(self._websocket_loop()),
            asyncio.create_task(self._keep_alive_loop())
        ]
```

Change to:

```python
        self.tasks = [
            asyncio.create_task(self._websocket_loop()),
            asyncio.create_task(self._keep_alive_loop()),
        ]
        if self.notifier.enabled:
            self.tasks.append(asyncio.create_task(self._daily_pnl_loop()))
```

- [ ] **Step 6: Add crash notification to bot.run()**

In `MaxGridBot.run()`, wrap the main try/except. The current structure:

```python
    async def run(self):
        try:
            self._init_exchange()
            ...
        except Exception as e:
            logger.error(f"[MAX] 初始化失敗: {e}")
            self.state.running = False
            return

        self.tasks = [...]

        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(0.5)
        finally:
            await self.stop()
```

Change the outer try to catch crash and notify:

```python
    async def run(self):
        try:
            self._init_exchange()
            self._check_hedge_mode()
            self.listen_key = self._get_listen_key()

            self.state.running = True
            self.state.start_time = datetime.now()

            self.sync_all()
        except Exception as e:
            logger.error(f"[MAX] 初始化失敗: {e}")
            await self.notifier.notify_crash(f"初始化失敗: {e}")
            self.state.running = False
            return

        self.tasks = [
            asyncio.create_task(self._websocket_loop()),
            asyncio.create_task(self._keep_alive_loop()),
        ]
        if self.notifier.enabled:
            self.tasks.append(asyncio.create_task(self._daily_pnl_loop()))

        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"[MAX] Bot 意外崩潰: {e}")
            await self.notifier.notify_crash(str(e))
        finally:
            await self.stop()
```

- [ ] **Step 7: Add stop notification to bot.stop()**

In `MaxGridBot.stop()`, add before `self._stop_event.set()`:

```python
        # 發送停止通知
        try:
            await self.notifier.notify_stop()
        except Exception:
            pass
```

- [ ] **Step 8: Integrate risk alert into sync cycle**

Find the `sync_all` method (or the periodic sync logic) in `bot.py`. After `self.state.update_totals()` is called, add:

```python
            # 風控通知 (fire and forget)
            if self.notifier.enabled:
                asyncio.create_task(self._check_risk_and_notify())
```

- [ ] **Step 9: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 10: Commit**

```bash
git add grid_engine/bot.py grid_engine/__init__.py
git commit -m "feat: Bot 接入 Telegram notifier — 崩潰通知、每日摘要、風控警報"
```

---

### Task 4: SIGTERM Handler + Restart 偵測

**Files:**
- Modify: `as_terminal_max.py` (MainMenu class)

- [ ] **Step 1: Add imports at top of as_terminal_max.py**

After the existing imports (around line 23), add:

```python
import signal
import sys
```

- [ ] **Step 2: Add SIGTERM handler to MainMenu**

In `MainMenu.__init__` (after line 67 `self._trading_active = False`), add:

```python
        # 註冊信號處理
        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT, self._handle_shutdown)
```

Add method to `MainMenu`:

```python
    def _handle_shutdown(self, signum, frame):
        """Graceful shutdown on SIGTERM/SIGINT"""
        console.print(f"\n[yellow]收到信號 {signum}，正在關閉...[/]")
        if self._trading_active and self.bot and self.bot_loop and self.bot_loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self.bot.stop(), self.bot_loop)
            try:
                future.result(timeout=10)
            except Exception:
                pass
            if self.bot_thread and self.bot_thread.is_alive():
                self.bot_thread.join(timeout=5)
        sys.exit(0)
```

- [ ] **Step 3: Add restart detection**

Container 重啟偵測：用 `/tmp/.as-grid-running` 標記檔。若啟動時標記已存在 → 是 restart。

In `MainMenu.__init__`, after signal handler registration, add:

```python
        # Restart 偵測與通知
        self._marker_file = Path("/tmp/.as-grid-running")
        self._check_restart()
```

Add method:

```python
    def _check_restart(self):
        """偵測 container restart 並發送 Telegram 通知"""
        is_restart = self._marker_file.exists()
        # 寫入標記檔
        self._marker_file.touch()

        if is_restart and self.config.telegram_bot_token and self.config.telegram_chat_id:
            from grid_engine.notifier import TelegramNotifier
            notifier = TelegramNotifier(
                self.config.telegram_bot_token,
                self.config.telegram_chat_id,
            )
            # 同步發送（此時還沒有 event loop）
            try:
                asyncio.run(notifier.notify_restart())
                console.print("[yellow]已發送重啟通知到 Telegram[/]")
            except Exception as e:
                console.print(f"[dim]重啟通知發送失敗: {e}[/]")
```

- [ ] **Step 4: Add Path import**

At the top of `as_terminal_max.py`, ensure `from pathlib import Path` is imported. Check if it's already there; if not, add it.

- [ ] **Step 5: Test manually**

Run: `python as_terminal_max.py`
- 確認能正常啟動，看到主選單
- `Ctrl+C` 應觸發 `_handle_shutdown` 而非直接 crash

- [ ] **Step 6: Commit**

```bash
git add as_terminal_max.py
git commit -m "feat: 加入 SIGTERM handler 和 container restart 偵測"
```

---

### Task 5: Telegram 設定選單

**Files:**
- Modify: `as_terminal_max.py` (MainMenu class, main_menu method)

- [ ] **Step 1: Add Telegram setup method to MainMenu**

Add method:

```python
    def setup_telegram(self):
        """設定 Telegram 通知"""
        self.show_banner()
        console.print("[bold]Telegram 通知設定[/]\n")

        if self.config.telegram_bot_token:
            console.print(f"[dim]當前 Bot Token: {self.config.telegram_bot_token[:10]}...[/]")
        if self.config.telegram_chat_id:
            console.print(f"[dim]當前 Chat ID: {self.config.telegram_chat_id}[/]")
        console.print()

        console.print("[dim]設定步驟:[/]")
        console.print("[dim]1. 在 Telegram 搜尋 @BotFather，發送 /newbot 建立機器人[/]")
        console.print("[dim]2. 複製 Bot Token (格式: 123456:ABC-DEF...)[/]")
        console.print("[dim]3. 搜尋 @userinfobot 獲取你的 Chat ID[/]")
        console.print()

        console.print("  [cyan]1[/] 設定 Bot Token")
        console.print("  [cyan]2[/] 設定 Chat ID")
        console.print("  [cyan]3[/] 發送測試訊息")
        console.print("  [cyan]4[/] 清除設定")
        console.print("  [cyan]0[/] 返回")
        console.print()

        choice = Prompt.ask("選擇", choices=["0", "1", "2", "3", "4"], default="0")

        if choice == "1":
            token = Prompt.ask("Bot Token").strip()
            if token:
                self.config.telegram_bot_token = token
                self.config.save()
        elif choice == "2":
            chat_id = Prompt.ask("Chat ID").strip()
            if chat_id:
                self.config.telegram_chat_id = chat_id
                self.config.save()
        elif choice == "3":
            if not self.config.telegram_bot_token or not self.config.telegram_chat_id:
                console.print("[red]請先設定 Bot Token 和 Chat ID[/]")
            else:
                from grid_engine.notifier import TelegramNotifier
                notifier = TelegramNotifier(
                    self.config.telegram_bot_token,
                    self.config.telegram_chat_id,
                )
                try:
                    result = asyncio.run(notifier.send("✅ AS Grid Bot 測試訊息 — 連線成功！"))
                    if result:
                        console.print("[green]✓ 測試訊息發送成功！[/]")
                    else:
                        console.print("[red]✗ 發送失敗，請檢查 Token 和 Chat ID[/]")
                except Exception as e:
                    console.print(f"[red]發送錯誤: {e}[/]")
        elif choice == "4":
            if Confirm.ask("[yellow]確定清除 Telegram 設定？[/]"):
                self.config.telegram_bot_token = ""
                self.config.telegram_chat_id = ""
                self.config.save()

        Prompt.ask("按 Enter 繼續")
```

- [ ] **Step 2: Add Telegram option to main menu**

In `main_menu()` method, after the line `console.print("  [cyan]7[/] API 設定")` (line 109), add:

```python
            console.print("  [cyan]9[/] Telegram 通知")
```

(用 9 是因為 8 已被選幣分析使用)

In `valid_choices` list, add `"9"`:

```python
            valid_choices = ["0", "1", "2", "3", "4", "5", "6", "7", "9"]
```

In the choice handling block, add before the `elif choice == "8"` line:

```python
            elif choice == "9":
                self.setup_telegram()
```

- [ ] **Step 3: Test manually**

Run: `python as_terminal_max.py`
- 確認選單顯示 `9 Telegram 通知`
- 進入 Telegram 設定選單，確認 UI 正常

- [ ] **Step 4: Commit**

```bash
git add as_terminal_max.py
git commit -m "feat: 新增 Telegram 通知設定選單 (選項 9)"
```

---

### Task 6: Docker 配置檔

**Files:**
- Create: `Dockerfile.terminal`
- Create: `docker-compose.terminal.yml`
- Create: `.dockerignore`

- [ ] **Step 1: Create .dockerignore**

```
# 敏感與不必要的檔案
config/
.claude/
.git/
.gitignore
__pycache__/
*.pyc
*.pyo
.DS_Store
log/
logs/
data/
asBack/
docs/
tests/
*.md
*.log
.env
.env.*
```

- [ ] **Step 2: Create Dockerfile.terminal**

```dockerfile
# AS 網格交易系統 - Terminal TUI 版本
FROM python:3.11-slim

# 系統依賴
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# 建立非 root 使用者
RUN useradd -m -s /bin/bash trader
WORKDIR /app

# 安裝 Python 依賴
COPY requirements-terminal.txt .
RUN pip install --no-cache-dir -r requirements-terminal.txt

# 複製程式碼
COPY . .

# 建立必要目錄並設定權限
RUN mkdir -p /app/config /app/data /app/log \
    && chown -R trader:trader /app

USER trader

ENV PYTHONUNBUFFERED=1
ENV TZ=Asia/Taipei

CMD ["python", "as_terminal_max.py"]
```

- [ ] **Step 3: Create docker-compose.terminal.yml**

```yaml
# AS 網格交易系統 - Terminal TUI 模式
# 使用方式:
#   啟動: docker compose -f docker-compose.terminal.yml up
#   斷開: Ctrl+P, Ctrl+Q
#   接回: docker attach as-grid
#   停止: docker compose -f docker-compose.terminal.yml stop

services:
  as-grid:
    build:
      context: .
      dockerfile: Dockerfile.terminal
    container_name: as-grid
    stdin_open: true
    tty: true
    volumes:
      - ./config:/app/config
      - ./data:/app/data
      - ./log:/app/log
    environment:
      - TZ=Asia/Taipei
      - PYTHONUNBUFFERED=1
    restart: unless-stopped
    deploy:
      resources:
        limits:
          memory: 1G
        reservations:
          memory: 256M
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

- [ ] **Step 4: Build and test locally**

```bash
docker compose -f docker-compose.terminal.yml build
```

Expected: Build 成功，image 不含 config/

- [ ] **Step 5: Run container locally**

```bash
docker compose -f docker-compose.terminal.yml up
```

Expected: 看到 AS Grid TUI 主選單。`Ctrl+P, Ctrl+Q` 斷開，`docker attach as-grid` 重新接回。

- [ ] **Step 6: Verify .dockerignore works**

```bash
docker run --rm as-grid-dragon-as-grid ls /app/config/
```

Expected: 目錄為空（config 不在 image 內，由 volume mount 提供）

- [ ] **Step 7: Commit**

```bash
git add Dockerfile.terminal docker-compose.terminal.yml .dockerignore
git commit -m "feat: 新增 Docker Terminal TUI 配置 (Dockerfile.terminal + compose)"
```

---

### Task 7: GCE 部署腳本

**Files:**
- Create: `scripts/gce-setup.sh`

- [ ] **Step 1: Create deployment script**

Create `scripts/gce-setup.sh`:

```bash
#!/bin/bash
# AS Grid Dragon — GCE 部署腳本
# 在全新的 Ubuntu 22.04 GCE VM 上執行
#
# 使用方式:
#   1. 建立 GCE VM (e2-small, Ubuntu 22.04, 固定外部 IP)
#   2. SSH 進入 VM
#   3. curl -sSL <raw-github-url>/scripts/gce-setup.sh | bash
#      或: git clone ... && cd as-grid-dragon && bash scripts/gce-setup.sh

set -euo pipefail

echo "=============================="
echo " AS Grid Dragon — GCE Setup"
echo "=============================="

# 1. 安裝 Docker
if ! command -v docker &> /dev/null; then
    echo "[1/4] 安裝 Docker..."
    sudo apt-get update
    sudo apt-get install -y ca-certificates curl gnupg
    sudo install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    sudo chmod a+r /etc/apt/keyrings/docker.gpg

    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
      $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
      sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

    sudo apt-get update
    sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

    # 讓當前使用者不需 sudo 就能用 docker
    sudo usermod -aG docker "$USER"
    echo "[✓] Docker 已安裝。注意：需要重新登入才能不加 sudo 使用 docker。"
else
    echo "[1/4] Docker 已安裝，跳過"
fi

# 2. Clone repo (如果不是從 repo 內執行)
REPO_DIR="$HOME/as-grid-dragon"
if [ ! -d "$REPO_DIR" ]; then
    echo "[2/4] Clone repo..."
    git clone https://github.com/RamonLiao/as-grid-dragon.git "$REPO_DIR"
else
    echo "[2/4] Repo 已存在，pull 最新版..."
    cd "$REPO_DIR" && git pull
fi

cd "$REPO_DIR"

# 3. 建立 config 目錄
echo "[3/4] 建立 config 目錄..."
mkdir -p config data log

# 4. Build Docker image
echo "[4/4] Build Docker image..."
sudo docker compose -f docker-compose.terminal.yml build

echo ""
echo "=============================="
echo " 部署完成！"
echo "=============================="
echo ""
echo "下一步："
echo "  1. 重新登入 SSH (讓 docker group 生效)"
echo "  2. cd $REPO_DIR"
echo "  3. docker compose -f docker-compose.terminal.yml up"
echo "  4. 在 TUI 中設定 API Key (選項 7) 和 Telegram (選項 9)"
echo "  5. 到交易所綁定此 VM 的 IP 白名單"
echo ""
echo "VM 外部 IP:"
curl -s http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip -H "Metadata-Flavor: Google" 2>/dev/null || echo "(無法自動取得，請到 GCP Console 查看)"
echo ""
```

- [ ] **Step 2: Make executable**

```bash
chmod +x scripts/gce-setup.sh
```

- [ ] **Step 3: Commit**

```bash
git add scripts/gce-setup.sh
git commit -m "feat: 新增 GCE 一鍵部署腳本"
```

---

### Task 8: Monkey Testing + 最終驗證

**Files:**
- Modify: `tests/test_notifier.py` (加入極端測試)

- [ ] **Step 1: Add monkey tests**

Append to `tests/test_notifier.py`:

```python
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
        # 應該包含原始文字（在 <code> 內）
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
```

- [ ] **Step 2: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_notifier.py
git commit -m "test: 新增 notifier monkey testing — 極端輸入、並發、邊界值"
```

---

### Task 9: Final Push

- [ ] **Step 1: Run full test suite one more time**

```bash
python -m pytest tests/ -v
```

- [ ] **Step 2: Push all commits**

```bash
git push origin main
```
