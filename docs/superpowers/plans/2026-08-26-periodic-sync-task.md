# 週期性 REST 同步 task 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `SyncService.maybe_sync()` 從 `_handle_ticker` 移到常駐背景 task，成為唯一驅動源，並在持倉／帳戶同步連續失敗時主動告警。

**Architecture:** `SyncService` 取得生命週期（`run()` / `stop()` / `_stop_event`），對稱既有的 `UserDataWatchdog`。`sync_all()` 回傳 `SyncOutcome` 回報五個子項成敗（控制流不動，只在既有 except 分支多記一筆 False）。loop 每輪評估結果，關鍵項（持倉、帳戶）連續失敗 3 次發一封 Telegram，恢復發一封，降級狀態同時進每日摘要。

**Tech Stack:** Python 3.12 / asyncio / pytest + pytest-asyncio / `uv`（`uv run pytest`）

**Spec:** `docs/superpowers/specs/2026-08-26-periodic-sync-task-design.md`

## Global Constraints

- **`_time` 不得整個刪除**：`tests/test_trade_stats_sync.py:379/521` monkeypatch `grid_engine.sync_service._time`，那是 `TRADE_STATS_INTERVAL` 在用。只換 `maybe_sync()` 那一處為 `clock.guard_now()`。
- **五個子同步項的內部邏輯與吞例外控制流不得改動**，只在既有 `except` 分支多記一筆失敗。
- **`_sync_lock` 被持有時的 early-return 語意不得改變**：`tests/test_async_offload.py:205/238/257` 用三個並發 `sync_all()` 守著它。
- **`bot.py:788` 啟動時首次 `sync_all()` 的行為零變更**（忽略回傳值）。
- **告警文案只用本專案定義的常數與數字**，不把外部資料未跳脫插進 HTML 訊息（notifier 用 `parse_mode=HTML`）；REST 例外原文只進 log，不進 Telegram。
- **`except asyncio.CancelledError` 必須在 `except Exception` 之前**（`bot.stop()` 靠 cancel + await 收尾，`bot.py:853-858`）。
- **`clock.now()` 與 `clock.guard_now()` 不得混用**：本功能的所有計時一律 `guard_now()`（牆鐘）。
- **Git**：只 `git add <file>...`，禁止 `git add -A` / `git add .`。
- **測試環境**：實盤引擎在本機常駐。跑測試前先 `pgrep -f as_terminal_max`，測試不得寫 `config/` 或 `log/`。基線在本 worktree 內實跑取得，不沿用主目錄數字。

---

## File Structure

| 檔案 | 責任 | 動作 |
|---|---|---|
| `grid_engine/sync_service.py` | `SyncOutcome` 定義、成敗回報、告警狀態機、常駐 loop | Modify |
| `grid_engine/bot.py` | 接線：建立 task、移除 ticker driver、reporter late assignment | Modify（3 處） |
| `grid_engine/reporting.py` | `_get_sync_status()` 讀 SyncService 狀態供摘要 | Modify |
| `grid_engine/notifier.py` | `_format_sync_line()` + 接進 `notify_daily_pnl` | Modify |
| `tests/test_periodic_sync.py` | T1-T4：outcome、時鐘、告警狀態機、loop | Create |
| `tests/test_periodic_sync_wiring.py` | T5：接線與單一 driver | Create |
| `tests/test_periodic_sync_summary.py` | T6：每日摘要那一行 | Create |
| `tests/test_periodic_sync_monkey.py` | T7：極端輸入 | Create |

---

### Task 1: `SyncOutcome` —— `sync_all()` 回報五個子項成敗

**Files:**
- Modify: `grid_engine/sync_service.py`（檔頭 import、新 dataclass、`sync_all` 64-72、`maybe_sync` 74-78、五個子項的 except 分支）
- Test: `tests/test_periodic_sync.py`（Create）

**Interfaces:**
- Consumes: 無
- Produces:
  - `SyncOutcome`：frozen dataclass，欄位 `positions_ok: bool = True`、`orders_ok: bool = True`、`account_ok: bool = True`、`funding_ok: bool = True`、`trade_stats_ok: bool = True`、`skipped: bool = False`，另有 property `critical_ok: bool`（= `positions_ok and account_ok`）
  - `SyncService.sync_all() -> SyncOutcome`
  - `SyncService.maybe_sync() -> Optional[SyncOutcome]`（節流未過門檻回 `None`）

- [ ] **Step 1: 寫失敗測試**

建立 `tests/test_periodic_sync.py`：

```python
"""週期性 REST 同步 task：sync_all 回報成敗、告警狀態機、常駐 loop。

驅動源從 _handle_ticker 移到常駐 task 後，「同步有沒有在跑」不再有 tick 當
不在場證明——失敗必須自己會說話，否則只是把靜默停擺換了個位置重演。
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

from grid_engine import clock
from grid_engine.bot import MaxGridBot
from grid_engine.config import GlobalConfig, SymbolConfig
from grid_engine.sync_service import SyncOutcome

SYMBOL = "XRP/USDC:USDC"


def _make_bot():
    """最小 bot fixture，沿用 tests/test_price_staleness_guard.py 的模式。"""
    cfg = GlobalConfig()
    cfg.symbols = {SYMBOL: SymbolConfig(
        symbol="XRPUSDC", ccxt_symbol=SYMBOL, enabled=True,
        take_profit_spacing=0.003, grid_spacing=0.003, initial_quantity=0.02,
        limit_multiplier=5.0, threshold_multiplier=20.0,
    )}
    cfg.bandit.enabled = False
    bot = MaxGridBot(cfg)
    bot.order_executor.place_order = AsyncMock()
    bot.order_executor.cancel_orders_for_side = AsyncMock()
    return bot


@pytest.fixture
def bot():
    b = _make_bot()
    yield b
    clock.reset_clock()
    clock.reset_guard_clock()


@pytest.fixture
def sync(bot):
    """把五個子項全換成成功的 no-op，測試各自再覆寫要失敗的那一個。"""
    s = bot.sync_service
    s._sync_positions = AsyncMock()
    s._sync_orders = AsyncMock()
    s._sync_account = AsyncMock()
    s._sync_funding_rates = AsyncMock()
    s._sync_trade_stats = AsyncMock()
    return s


@pytest.mark.asyncio
async def test_sync_all_reports_all_ok(sync):
    outcome = await sync.sync_all()
    assert isinstance(outcome, SyncOutcome)
    assert outcome.positions_ok and outcome.account_ok
    assert outcome.critical_ok
    assert not outcome.skipped


@pytest.mark.asyncio
async def test_sync_all_reports_skipped_when_lock_held(sync):
    """_sync_lock 已被持有 ⇒ early-return，回 skipped=True 且不參與判定。

    這個 early-return 是既有語意（tests/test_async_offload.py 三條並發測試在守），
    回傳值的加入不得改變它。
    """
    async with sync._sync_lock:
        outcome = await sync.sync_all()
    assert outcome.skipped is True
    assert outcome.critical_ok is True      # skipped 不算失敗
    sync._sync_positions.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_sync_returns_none_when_throttled(sync):
    """節流未過門檻回 None——不算成功也不算失敗。"""
    await sync.maybe_sync()                 # 第一次必過（last_sync_time=0）
    second = await sync.maybe_sync()         # 立刻再來一次，門檻未過
    assert second is None
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_periodic_sync.py -v`
Expected: FAIL — `ImportError: cannot import name 'SyncOutcome' from 'grid_engine.sync_service'`

- [ ] **Step 3: 實作 `SyncOutcome` 與 `sync_all` 回傳**

`grid_engine/sync_service.py` 檔頭 import 加 `from dataclasses import dataclass`（`import asyncio` 之後）。

在 `class SyncService` **之前**加：

```python
@dataclass(frozen=True)
class SyncOutcome:
    """一輪 sync_all 的逐項成敗。

    為什麼要回報而不是靠例外：五個子項各自吞例外（歷史決定，見各方法 docstring），
    呼叫端那一層幾乎永遠看不到例外 ⇒ 「REST 全掛」在今天的表現是面板數字凍結、
    風控拿著過期持倉繼續跑、沒有人被通知。這個回傳值是那條靜默路徑唯一的出口。

    critical_ok 只看持倉與帳戶：前者是風控判斷的輸入，後者是保證金告警的輸入。
    掛單數只影響顯示與 requote 計數，funding 與成交統計是遙測——把它們納入告警
    會被偶發 REST 抖動洗版，而它們失敗不影響交易安全。
    """
    positions_ok: bool = True
    orders_ok: bool = True
    account_ok: bool = True
    funding_ok: bool = True
    trade_stats_ok: bool = True
    skipped: bool = False

    @property
    def critical_ok(self) -> bool:
        return self.positions_ok and self.account_ok
```

`sync_all()` 改為（原五行呼叫順序不變）：

```python
    async def sync_all(self) -> SyncOutcome:
        if self._sync_lock.locked():
            return SyncOutcome(skipped=True)
        async with self._sync_lock:
            positions_ok = await self._sync_positions()
            orders_ok = await self._sync_orders()
            account_ok = await self._sync_account()
            funding_ok = await self._sync_funding_rates()
            trade_stats_ok = await self._sync_trade_stats()
        return SyncOutcome(
            positions_ok=positions_ok, orders_ok=orders_ok, account_ok=account_ok,
            funding_ok=funding_ok, trade_stats_ok=trade_stats_ok,
        )
```

⚠️ 子項被測試換成 `AsyncMock()` 時回傳 `MagicMock`（truthy），仍會被判成功——這是刻意的，
子項各自的失敗語意由各自的測試守（`test_account_update.py` 等）。

五個子項改成回傳 bool，**控制流一行都不動**：

- `_sync_positions`：簽章加 `-> bool`；`except` 分支 `logger.error(...)` 後改 `return False`；方法最後一行後加 `return True`。
- `_sync_orders`：簽章加 `-> bool`；方法開頭加 `ok = True`；per-symbol `except` 分支 `logger.error(...)` 後加 `ok = False`（**不是 return**，其餘 symbol 照跑）；方法尾 `return ok`。
- `_sync_account`：簽章加 `-> bool`；`except` 分支 `logger.error(...)` 後加 `return False`；`try` 區塊最後（`await self.risk_monitor.check_trailing_stop()` 之後）加 `return True`。
- `_sync_funding_rates`：簽章加 `-> bool`；`if not self.ctx.funding_manager: return True`（無 funding manager 不算失敗）；開頭 `ok = True`；per-symbol `except` 後加 `ok = False`；方法尾 `return ok`。
- `_sync_trade_stats`：簽章加 `-> bool`；找出它的外層 `except` 分支，`logger` 後加 `return False`；正常路徑（含「未到 TRADE_STATS_INTERVAL 直接 return」的 early-return）一律 `return True`。

`maybe_sync()` 改為：

```python
    async def maybe_sync(self) -> Optional[SyncOutcome]:
        """節流同步。回 None 表示本輪未達門檻（不算成功也不算失敗）。"""
        if _time() - self.last_sync_time > self.config.sync_interval:
            outcome = await self.sync_all()
            self.last_sync_time = _time()
            return outcome
        return None
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_periodic_sync.py -v`
Expected: 3 passed

- [ ] **Step 5: 確認既有並發語意沒被破壞**

Run: `uv run pytest tests/test_async_offload.py tests/test_trade_stats_sync.py tests/test_account_update.py -v`
Expected: 全綠（`test_async_offload.py` 的三條並發 `sync_all()` 是重點）

- [ ] **Step 6: Commit**

```bash
git add grid_engine/sync_service.py tests/test_periodic_sync.py
git commit -m "feat(sync): sync_all 回報逐項成敗（SyncOutcome）"
```

---

### Task 2: 節流計時改用 `clock.guard_now()`

**Files:**
- Modify: `grid_engine/sync_service.py`（`maybe_sync` 兩處 `_time()`）
- Test: `tests/test_periodic_sync.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `maybe_sync() -> Optional[SyncOutcome]`
- Produces: `maybe_sync()` 的節流基準改為 `clock.guard_now()`；`_time` 仍存在，供 `TRADE_STATS_INTERVAL` 使用

- [ ] **Step 1: 寫失敗測試**

追加到 `tests/test_periodic_sync.py`：

```python
@pytest.fixture
def fake_clock():
    """可推進的假守衛時鐘。注入 set_guard_clock 而非 set_clock：

    live bot 與 backtester 同行程，clock.now() 會被 backtester 換成歷史 epoch。
    同步節流量的是「本機牆鐘」，與情境時鐘是不同的物理量，混用是分類錯誤。
    """
    t = {"now": 1_000_000.0}
    clock.set_guard_clock(lambda: t["now"])

    def advance(seconds):
        t["now"] += seconds
    yield advance
    clock.reset_guard_clock()


@pytest.mark.asyncio
async def test_maybe_sync_throttle_uses_guard_clock(sync, fake_clock):
    """節流以守衛時鐘計時：推進時間就該再同步一次。"""
    first = await sync.maybe_sync()
    assert first is not None
    assert await sync.maybe_sync() is None          # 門檻未過

    fake_clock(sync.config.sync_interval + 1)
    assert await sync.maybe_sync() is not None      # 過門檻


def test_module_time_helper_still_exists():
    """_time 不得整個刪除：test_trade_stats_sync.py 正在 monkeypatch 它，
    那是 TRADE_STATS_INTERVAL 在用的計時來源。
    """
    from grid_engine import sync_service
    assert callable(sync_service._time)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_periodic_sync.py::test_maybe_sync_throttle_uses_guard_clock -v`
Expected: FAIL — 注入假時鐘後推進時間，`maybe_sync` 仍回 `None`（因為還在讀真實 `_time()`）

- [ ] **Step 3: 實作**

`maybe_sync()` 內兩處 `_time()` 改成 `clock.guard_now()`（`clock` 已在檔頭 `from . import clock` import）：

```python
    async def maybe_sync(self) -> Optional[SyncOutcome]:
        """節流同步。回 None 表示本輪未達門檻（不算成功也不算失敗）。

        計時用 guard_now()（牆鐘）而非 now()（情境時鐘）：後者會被 backtester
        替換成歷史 epoch，live 與回測同行程時會讓節流判斷錯亂。
        與價格時效守衛（bot.py:415）用同一個時鐘，語意一致。
        """
        if clock.guard_now() - self.last_sync_time > self.config.sync_interval:
            outcome = await self.sync_all()
            self.last_sync_time = clock.guard_now()
            return outcome
        return None
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_periodic_sync.py tests/test_trade_stats_sync.py -v`
Expected: 全綠

- [ ] **Step 5: Commit**

```bash
git add grid_engine/sync_service.py tests/test_periodic_sync.py
git commit -m "fix(sync): 節流計時改用 guard_now，與價格守衛同一時鐘"
```

---

### Task 3: 降級判定與告警狀態機

**Files:**
- Modify: `grid_engine/sync_service.py`（`__init__` 加狀態、新增 `_evaluate` 與 `_notify`、模組常數）
- Test: `tests/test_periodic_sync.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `SyncOutcome`
- Produces:
  - 模組常數 `SYNC_FAILURE_THRESHOLD = 3`
  - `SyncService._consecutive_failures: int`、`._degraded: bool`、`._degraded_total: int`
  - `SyncService._evaluate(outcome: Optional[SyncOutcome], loop_error: bool = False) -> None`
  - `SyncService._notify(message: str) -> None`

- [ ] **Step 1: 寫失敗測試**

追加到 `tests/test_periodic_sync.py`：

```python
from grid_engine.sync_service import SYNC_FAILURE_THRESHOLD


@pytest.fixture
def notified(sync):
    """攔截告警文字。_notify 是同步方法（內部 create_task），直接換掉。"""
    sent = []
    sync._notify = lambda msg: sent.append(msg)
    return sent


def test_two_failures_do_not_alert(sync, notified):
    for _ in range(SYNC_FAILURE_THRESHOLD - 1):
        sync._evaluate(SyncOutcome(positions_ok=False))
    assert notified == []
    assert sync._degraded is False


def test_third_failure_alerts_once(sync, notified):
    for _ in range(SYNC_FAILURE_THRESHOLD):
        sync._evaluate(SyncOutcome(positions_ok=False))
    assert len(notified) == 1
    assert "降級" in notified[0]
    assert sync._degraded is True
    assert sync._degraded_total == 1


def test_degraded_does_not_repeat_alert(sync, notified):
    for _ in range(SYNC_FAILURE_THRESHOLD + 5):
        sync._evaluate(SyncOutcome(account_ok=False))
    assert len(notified) == 1


def test_recovery_alerts_once_and_resets(sync, notified):
    for _ in range(SYNC_FAILURE_THRESHOLD):
        sync._evaluate(SyncOutcome(positions_ok=False))
    sync._evaluate(SyncOutcome())            # 全綠
    assert len(notified) == 2
    assert "恢復" in notified[1]
    assert sync._degraded is False
    assert sync._consecutive_failures == 0
    sync._evaluate(SyncOutcome())            # 再全綠不得重發
    assert len(notified) == 2


def test_non_critical_failures_never_alert(sync, notified):
    """掛單/funding/成交統計失敗只留 log，不進計數——它們失敗不影響交易安全。"""
    for _ in range(10):
        sync._evaluate(SyncOutcome(orders_ok=False, funding_ok=False, trade_stats_ok=False))
    assert notified == []
    assert sync._consecutive_failures == 0


def test_none_and_skipped_do_not_move_counter(sync, notified):
    """節流未過門檻(None)與 lock 佔用(skipped)不算成功也不算失敗。"""
    sync._evaluate(SyncOutcome(positions_ok=False))
    assert sync._consecutive_failures == 1
    sync._evaluate(None)
    sync._evaluate(SyncOutcome(skipped=True))
    assert sync._consecutive_failures == 1   # 沒被推進，也沒被歸零
    assert notified == []


def test_loop_error_counts_as_failure(sync, notified):
    """loop 級例外也算一次失敗——否則「sync_all 整條炸掉」會完全不計數。"""
    for _ in range(SYNC_FAILURE_THRESHOLD):
        sync._evaluate(None, loop_error=True)
    assert len(notified) == 1


def test_alert_text_contains_no_external_data(sync, notified):
    """告警文案只用常數與數字：notifier 用 parse_mode=HTML，未跳脫的外部資料
    會壞掉整封訊息，且例外原文可能帶憑證片段。
    """
    for _ in range(SYNC_FAILURE_THRESHOLD):
        sync._evaluate(SyncOutcome(positions_ok=False))
    assert "<" not in notified[0] and ">" not in notified[0]
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_periodic_sync.py -v`
Expected: FAIL — `ImportError: cannot import name 'SYNC_FAILURE_THRESHOLD'`

- [ ] **Step 3: 實作**

`grid_engine/sync_service.py` 模組常數區（`TRADE_STATS_MAX_PAGES_PER_SYNC` 之後）加：

```python
# 關鍵項（持倉/帳戶）連續失敗幾輪才告警。3 輪 ≈ 30 秒（sync_interval 預設 10s）：
# 短到能在一次保證金事件的時間尺度內發出，長到不會被單次 REST 抖動觸發。
SYNC_FAILURE_THRESHOLD = 3
```

`__init__` 尾端（`self._last_trade_stats_at = 0.0` 之後）加：

```python
        # 週期同步的降級狀態。這三個欄位是「同步有沒有在跑」的唯一儀器——
        # 驅動源移到常駐 task 後，沒有 tick 可以當不在場證明了。
        self._stop_event = asyncio.Event()
        self._consecutive_failures = 0
        self._degraded = False
        self._degraded_total = 0    # 自啟動累計，供每日摘要用；永不重置
```

新增方法（放在 `maybe_sync` 之後）：

```python
    def _evaluate(self, outcome: Optional[SyncOutcome], loop_error: bool = False):
        """依一輪結果推進降級狀態並告警。

        只看關鍵項（持倉=風控輸入、帳戶=保證金告警輸入）。None(節流未過) 與
        skipped(lock 佔用) 既不算成功也不算失敗——把它們當成功會在高頻節流下
        永遠歸零計數，當失敗則會在正常運作時誤報。
        """
        if loop_error:
            failed = True
        elif outcome is None or outcome.skipped:
            return
        else:
            failed = not outcome.critical_ok

        if failed:
            self._consecutive_failures += 1
            if self._consecutive_failures >= SYNC_FAILURE_THRESHOLD and not self._degraded:
                self._degraded = True
                self._degraded_total += 1
                self._notify(
                    f"⚠️ REST 同步降級：持倉/帳戶同步連續失敗 "
                    f"{self._consecutive_failures} 次，風控輸入可能過期"
                )
            return

        self._consecutive_failures = 0
        if self._degraded:
            self._degraded = False
            self._notify("✅ REST 同步已恢復")

    def _notify(self, message: str):
        """告警送出。作法逐字沿用 userdata_watchdog.py 的 _notify：

        存引用防止 task 在執行前被 GC；完成後自移除避免長跑累積；無 event loop
        時只留 log（不退回 asyncio.run —— 那是純為了讓同步測試能跑而存在的
        生產程式碼路徑，專案規則 9 禁止兩個 pattern 混用）。
        """
        if not self.notifier.enabled:
            return
        try:
            asyncio.get_running_loop()
            task = asyncio.create_task(self.notifier.send(message))
            self.tasks.append(task)
            task.add_done_callback(lambda t: t in self.tasks and self.tasks.remove(t))
        except RuntimeError:
            logger.warning(f"[sync] 無 event loop，通知未送出: {message}")
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_periodic_sync.py -v`
Expected: 全綠（13 條）

- [ ] **Step 5: Mutation 檢查（每個新守衛必須先在真實缺陷面前紅一次）**

逐條套用、確認**至少一條測試變紅**、再還原：
1. `SYNC_FAILURE_THRESHOLD` 3 → 1 → `test_two_failures_do_not_alert` 應紅
2. `not self._degraded` 條件刪掉 → `test_degraded_does_not_repeat_alert` 應紅
3. `outcome.critical_ok` 改成 `outcome.orders_ok` → `test_non_critical_failures_never_alert` 應紅
4. `outcome is None or outcome.skipped` 的 `return` 改成 `failed = False` → `test_none_and_skipped_do_not_move_counter` 應紅
5. 恢復分支的 `self._notify(...)` 刪掉 → `test_recovery_alerts_once_and_resets` 應紅

把「哪條 mutation 殺掉哪條測試」記進 commit message。

- [ ] **Step 6: Commit**

```bash
git add grid_engine/sync_service.py tests/test_periodic_sync.py
git commit -m "feat(sync): 關鍵項連續失敗 3 次告警、恢復通知（去重不重發）"
```

---

### Task 4: 常駐 loop `run()` / `stop()` / `_loop_interval()`

**Files:**
- Modify: `grid_engine/sync_service.py`（新增三個方法）
- Test: `tests/test_periodic_sync.py`（追加）

**Interfaces:**
- Consumes: Task 1-3 的 `maybe_sync()`、`_evaluate()`、`_stop_event`
- Produces:
  - `SyncService.run() -> None`（async，常駐）
  - `SyncService.stop() -> None`
  - `SyncService._loop_interval() -> float`
  - 模組常數 `MIN_SYNC_INTERVAL = 1.0`

- [ ] **Step 1: 寫失敗測試**

追加到 `tests/test_periodic_sync.py`：

```python
@pytest.mark.asyncio
async def test_run_syncs_while_ticker_is_completely_silent(sync):
    """本改動的核心主張：_handle_ticker 一次都不被呼叫，同步照樣進行。

    這是整份計畫存在的理由——今天 maybe_sync 只掛在 ticker handler 上，
    bookTicker 一斷，持倉同步/保證金告警/訂單對帳全部靜默停擺。
    """
    sync.config.sync_interval = 0.01
    task = asyncio.create_task(sync.run())
    await asyncio.sleep(0.1)
    sync.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert sync._sync_positions.call_count >= 2


@pytest.mark.asyncio
async def test_run_exits_cleanly_on_cancel(sync):
    """CancelledError 必須穿過去：bot.stop() 靠 cancel + await 收尾，
    被 except Exception 吃掉會讓關機卡住。
    """
    sync.config.sync_interval = 0.01
    task = asyncio.create_task(sync.run())
    await asyncio.sleep(0.03)
    task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=1.0)
    assert task.done()


@pytest.mark.asyncio
async def test_run_survives_exception_and_counts_it(sync, notified):
    """loop 內例外不得殺掉 task——修一個靜默故障的改動自己不能靜默死掉。"""
    sync.config.sync_interval = 0.01
    sync.sync_all = AsyncMock(side_effect=RuntimeError("boom"))
    task = asyncio.create_task(sync.run())
    await asyncio.sleep(0.15)
    sync.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert sync._consecutive_failures >= SYNC_FAILURE_THRESHOLD
    assert len(notified) == 1


@pytest.mark.parametrize("bad", [0, -5, float("nan"), "abc", None])
def test_loop_interval_clamps_illegal_values(sync, bad):
    """sleep(0) 會變成忙迴圈打爆 REST 配額。夾到下限而非 fallback 預設值：
    使用者刻意調小是合法意圖，只有非法值才需要糾正。
    """
    sync.config.sync_interval = bad
    assert sync._loop_interval() >= 1.0


def test_loop_interval_respects_legal_small_value(sync):
    sync.config.sync_interval = 2.5
    assert sync._loop_interval() == 2.5
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_periodic_sync.py -k "run_ or loop_interval" -v`
Expected: FAIL — `AttributeError: 'SyncService' object has no attribute 'run'`

- [ ] **Step 3: 實作**

模組常數區加：

```python
# sleep(0) 會變成忙迴圈打爆 REST 配額；非法 sync_interval 一律夾到這個下限。
MIN_SYNC_INTERVAL = 1.0
```

新增方法（放在 `_notify` 之後）：

```python
    def _loop_interval(self) -> float:
        """本輪 sleep 秒數。每輪重讀 config，讓執行中改設定下一輪就生效。"""
        try:
            interval = float(self.config.sync_interval)
        except (TypeError, ValueError):
            logger.warning(f"[sync] sync_interval 非數值({self.config.sync_interval!r})，"
                           f"夾到 {MIN_SYNC_INTERVAL}s")
            return MIN_SYNC_INTERVAL
        if math.isnan(interval) or interval < MIN_SYNC_INTERVAL:
            logger.warning(f"[sync] sync_interval 非法({interval})，夾到 {MIN_SYNC_INTERVAL}s")
            return MIN_SYNC_INTERVAL
        return interval

    async def run(self):
        """常駐同步驅動。移除 _handle_ticker 的呼叫後，這是唯一驅動源。

        例外一律吞掉續跑：這個 task 一死，REST 同步完全消失（比改動前更糟），
        所以它不能有「因為某次同步炸了就退出」的分支。CancelledError 例外——
        那是 bot.stop() 的收尾訊號，必須讓它穿過去。
        """
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(self._loop_interval())
                if self._stop_event.is_set():
                    break
                outcome = await self.maybe_sync()
                self._evaluate(outcome)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[sync] 週期同步失敗: {e}")
                self._evaluate(None, loop_error=True)

    def stop(self):
        self._stop_event.set()
```

檔頭已有 `import math`（`_sync_*` 在用），確認即可，缺才補。

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_periodic_sync.py -v`
Expected: 全綠

- [ ] **Step 5: Mutation 檢查**

1. `except asyncio.CancelledError: break` 整段刪掉（讓 CancelledError 落進 `except Exception`）→ `test_run_exits_cleanly_on_cancel` 應紅或逾時
2. `except Exception` 分支改成 `raise` → `test_run_survives_exception_and_counts_it` 應紅
3. `_loop_interval` 的夾值改成直接 `return float(self.config.sync_interval)` → `test_loop_interval_clamps_illegal_values` 應紅

- [ ] **Step 6: Commit**

```bash
git add grid_engine/sync_service.py tests/test_periodic_sync.py
git commit -m "feat(sync): SyncService 取得常駐 loop 生命週期（run/stop）"
```

---

### Task 5: 接線 —— 建立 task 並移除 ticker driver

**Files:**
- Modify: `grid_engine/bot.py:625`（移除 `await self.sync_service.maybe_sync()`）
- Modify: `grid_engine/bot.py:796-800`（`tasks.extend` 加一筆）
- Test: `tests/test_periodic_sync_wiring.py`（Create）

**Interfaces:**
- Consumes: Task 4 的 `SyncService.run()`
- Produces: `_handle_ticker` 不再呼叫任何同步；`bot.run()` 建立的 task 清單多一個 `sync_service.run()`

- [ ] **Step 1: 寫失敗測試**

建立 `tests/test_periodic_sync_wiring.py`：

```python
"""單一 driver 接線：同步只由常駐 task 驅動，ticker handler 不再碰它。

留著 ticker 呼叫的版本裡，週期 task 幾乎永遠只是空跑節流檢查，沒有測試能
區分它有沒有真的在工作——這種「平常永遠不生效」的守衛最容易腐爛。
"""
import asyncio
import inspect
from unittest.mock import AsyncMock

import pytest

from grid_engine import clock
from grid_engine.bot import MaxGridBot
from grid_engine.config import GlobalConfig, SymbolConfig

SYMBOL = "XRP/USDC:USDC"


def _make_bot():
    cfg = GlobalConfig()
    cfg.symbols = {SYMBOL: SymbolConfig(
        symbol="XRPUSDC", ccxt_symbol=SYMBOL, enabled=True,
        take_profit_spacing=0.003, grid_spacing=0.003, initial_quantity=0.02,
        limit_multiplier=5.0, threshold_multiplier=20.0,
    )}
    cfg.bandit.enabled = False
    bot = MaxGridBot(cfg)
    bot.order_executor.place_order = AsyncMock()
    bot.order_executor.cancel_orders_for_side = AsyncMock()
    return bot


@pytest.fixture
def bot():
    b = _make_bot()
    yield b
    clock.reset_clock()
    clock.reset_guard_clock()


@pytest.mark.asyncio
async def test_handle_ticker_does_not_drive_sync(bot):
    """釘死單一 driver：ticker 進來只更新報價與網格，不觸發 REST 同步。"""
    bot.adjust_grid = AsyncMock()
    bot.sync_service.maybe_sync = AsyncMock()
    bot.sync_service.sync_all = AsyncMock()

    await bot._handle_ticker({"s": "XRPUSDC", "b": "100.0", "a": "100.2"})

    bot.sync_service.maybe_sync.assert_not_called()
    bot.sync_service.sync_all.assert_not_called()


def test_handle_ticker_source_has_no_sync_call(bot):
    """原始碼層級守衛：日後有人「順手」把同步加回熱路徑會直接紅。"""
    src = inspect.getsource(MaxGridBot._handle_ticker)
    assert "maybe_sync" not in src
    assert "sync_all" not in src


def test_bot_run_creates_sync_task():
    """run() 的 task 清單必須含 sync_service.run——它是唯一驅動源，
    沒被建立等於 REST 同步完全消失（比改動前更糟，見 spec R1）。
    """
    src = inspect.getsource(MaxGridBot.run)
    assert "self.sync_service.run()" in src
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_periodic_sync_wiring.py -v`
Expected: 三條全 FAIL（`maybe_sync` 仍在 `_handle_ticker`；`run()` 尚未建立 task）

- [ ] **Step 3: 實作**

`grid_engine/bot.py:625`：刪除整行 `await self.sync_service.maybe_sync()`。若該行上方有專屬註解一併刪除，並在 `_handle_ticker` 的 docstring 補一句：

```
        REST 同步刻意不在這裡驅動：綁在 WS 推送上會讓 bookTicker 一斷就全部
        靜默停擺（見 docs/superpowers/specs/2026-08-26-periodic-sync-task-design.md）。
        驅動源是 SyncService.run() 常駐 task，唯一。
```

`grid_engine/bot.py:796` 的 `tasks.extend([...])` 加一筆：

```python
        self.tasks.extend([
            asyncio.create_task(self.ws_client.run()),
            asyncio.create_task(self.ws_client.keep_alive_loop()),
            asyncio.create_task(self.userdata_watchdog.run()),
            asyncio.create_task(self.sync_service.run()),
        ])
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_periodic_sync_wiring.py tests/test_price_staleness_guard.py -v`
Expected: 全綠（`test_price_staleness_guard.py` 有兩處 mock `maybe_sync`，移除呼叫後那些 mock 變成無作用但不會紅；若有測試斷言它被呼叫，改成斷言不被呼叫並在 commit message 說明）

- [ ] **Step 5: 全套回歸**

先 `pgrep -f as_terminal_max` 確認實盤引擎狀態，再：
Run: `uv run pytest -q`
Expected: 全綠。與本 worktree 的起始基線比對，淨增數應等於本計畫新增的測試數。

- [ ] **Step 6: Commit**

```bash
git add grid_engine/bot.py tests/test_periodic_sync_wiring.py
git commit -m "feat(sync): 常駐 task 成為唯一 driver，移除 _handle_ticker 的同步呼叫"
```

---

### Task 6: 每日摘要顯示同步降級

**Files:**
- Modify: `grid_engine/reporting.py`（`__init__` 加 `sync_source`、新增 `_get_sync_status`、`pnl_data` 加一鍵）
- Modify: `grid_engine/notifier.py`（新增 `_format_sync_line`、接進 `notify_daily_pnl`）
- Modify: `grid_engine/bot.py:118` 後（late assignment）
- Test: `tests/test_periodic_sync_summary.py`（Create）

**Interfaces:**
- Consumes: Task 3 的 `_degraded`、`_consecutive_failures`、`_degraded_total`
- Produces:
  - `DailyReporter.sync_source`（預設 `None`）
  - `DailyReporter._get_sync_status() -> Optional[dict]`，鍵為 `degraded: bool`、`consecutive_failures: int`、`degraded_total: int`
  - `TelegramNotifier._format_sync_line(sync) -> str`
  - `pnl_data["sync"]`

- [ ] **Step 1: 寫失敗測試**

建立 `tests/test_periodic_sync_summary.py`：

```python
"""每日摘要的 REST 同步狀態行。

降級狀態若只靠即時告警，錯過那一封就再也看不到——摘要是它唯一的持續表面。
"""
import pytest

from grid_engine.notifier import TelegramNotifier
from grid_engine.reporting import DailyReporter


class _FakeSync:
    def __init__(self, degraded=False, failures=0, total=0):
        self._degraded = degraded
        self._consecutive_failures = failures
        self._degraded_total = total


def test_line_omitted_when_never_degraded():
    """正常且從未降級 ⇒ 整行省略，不加噪音。"""
    assert TelegramNotifier._format_sync_line(
        {"degraded": False, "consecutive_failures": 0, "degraded_total": 0}) == ""


def test_line_shows_degraded_now():
    line = TelegramNotifier._format_sync_line(
        {"degraded": True, "consecutive_failures": 7, "degraded_total": 2})
    assert "降級中" in line and "7" in line


def test_line_shows_recovered_history():
    """恢復了但今天出過事——這是摘要唯一能講、告警講不了的事。"""
    line = TelegramNotifier._format_sync_line(
        {"degraded": False, "consecutive_failures": 0, "degraded_total": 3})
    assert "正常" in line and "3" in line


@pytest.mark.parametrize("bad", [None, "x", 42, []])
def test_line_omitted_on_bad_input(bad):
    """型別錯不得讓整封摘要發不出去。"""
    assert TelegramNotifier._format_sync_line(bad) == ""


def test_reporter_reads_sync_status():
    reporter = DailyReporter(config=None, state=None, notifier=None, stop_event=None)
    reporter.sync_source = _FakeSync(degraded=True, failures=4, total=1)
    assert reporter._get_sync_status() == {
        "degraded": True, "consecutive_failures": 4, "degraded_total": 1}


def test_reporter_returns_none_without_source():
    reporter = DailyReporter(config=None, state=None, notifier=None, stop_event=None)
    assert reporter._get_sync_status() is None


def test_reporter_swallows_broken_source():
    """讀狀態失敗只能讓那一行消失，不得讓整封摘要發不出去
    （與 _get_watchdog_status / _get_stale_quote_summary 同一條硬性要求）。
    """
    class Boom:
        @property
        def _degraded(self):
            raise RuntimeError("boom")

    reporter = DailyReporter(config=None, state=None, notifier=None, stop_event=None)
    reporter.sync_source = Boom()
    assert reporter._get_sync_status() is None
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_periodic_sync_summary.py -v`
Expected: FAIL — `AttributeError: type object 'TelegramNotifier' has no attribute '_format_sync_line'`

- [ ] **Step 3: 實作**

`grid_engine/reporting.py` 的 `__init__` 簽章加 `sync_source=None`，body 加 `self.sync_source = sync_source`。新增方法（放在 `_get_stale_quote_summary` 之後）：

```python
    def _get_sync_status(self):
        """讀 SyncService 的降級狀態供每日摘要顯示。

        硬性要求同 _get_watchdog_status：任何例外都在這裡吞掉降級成「不顯示
        該行」，不得讓整封摘要發不出去。純讀，不呼叫任何會改變狀態的方法。
        """
        if self.sync_source is None:
            return None
        try:
            return {
                "degraded": bool(self.sync_source._degraded),
                "consecutive_failures": int(self.sync_source._consecutive_failures),
                "degraded_total": int(self.sync_source._degraded_total),
            }
        except Exception as e:
            logger.warning(f"[reporter] 同步狀態讀取失敗，摘要跳過該行: {e}")
            return None
```

`run()` 內的 `pnl_data` 字典加一鍵（`"stale_quotes"` 那行之後）：

```python
                    "sync": self._get_sync_status(),
```

`grid_engine/notifier.py` 新增（放在 `_format_stale_quote_line` 之後）：

```python
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
        """
        if not isinstance(sync, dict):
            return ""
        try:
            degraded = bool(sync.get("degraded"))
            failures = int(sync.get("consecutive_failures", 0))
            total = int(sync.get("degraded_total", 0))
        except Exception:
            return ""
        if degraded:
            return f"⚠️ <b>REST 同步</b>：降級中（連續失敗 {failures} 次）\n"
        if total > 0:
            return f"✅ REST 同步：正常（自啟動曾降級 {total} 次）\n"
        return ""
```

`notify_daily_pnl` 內，`stale_line` 之後加：

```python
        sync_line = self._format_sync_line(pnl_data.get("sync"))
```

並在 msg 的 f-string 裡 `f"{stale_line}"` 之後加 `f"{sync_line}"`。

`grid_engine/bot.py`：`self.sync_service = SyncService(...)` 那段（113-118）之後加，作法對齊既有的 `self.reporter.watchdog = self.userdata_watchdog`（138 行）：

```python
        # reporter 建構在 sync_service 之前（它不需要 sync_service 才能建），
        # 故與 watchdog 同樣採後置指派，不動既有建構順序。
        self.reporter.sync_source = self.sync_service
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_periodic_sync_summary.py tests/test_notifier.py tests/test_reporting_watchdog.py -v`
Expected: 全綠（`test_reporting_watchdog.py` 守既有兩行摘要的降級語意，新增第三行不得動到它們）

- [ ] **Step 5: 驗證接線真的接上**

追加到 `tests/test_periodic_sync_wiring.py`：

```python
def test_reporter_sync_source_is_wired(bot):
    """後置指派容易漏——漏了摘要那行永遠不出現，而且不會有人發現。"""
    assert bot.reporter.sync_source is bot.sync_service
```

Run: `uv run pytest tests/test_periodic_sync_wiring.py -v`
Expected: 全綠

- [ ] **Step 6: Commit**

```bash
git add grid_engine/reporting.py grid_engine/notifier.py grid_engine/bot.py \
        tests/test_periodic_sync_summary.py tests/test_periodic_sync_wiring.py
git commit -m "feat(sync): 每日摘要顯示 REST 同步降級狀態"
```

---

### Task 7: Monkey testing 與全套驗收

**Files:**
- Test: `tests/test_periodic_sync_monkey.py`（Create）

**Interfaces:**
- Consumes: Task 1-6 全部
- Produces: 無新介面

- [ ] **Step 1: 寫極端輸入測試**

建立 `tests/test_periodic_sync_monkey.py`：

```python
"""週期同步的 monkey testing：想辦法把它玩壞。

專案規則要求 unit + integration 之後做極端測試。這裡的每一條都對應一個
「這東西掛了會怎樣」的問題，不是為了覆蓋率。
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

from grid_engine import clock
from grid_engine.bot import MaxGridBot
from grid_engine.config import GlobalConfig, SymbolConfig
from grid_engine.sync_service import SYNC_FAILURE_THRESHOLD, SyncOutcome

SYMBOL = "XRP/USDC:USDC"


def _make_bot():
    cfg = GlobalConfig()
    cfg.symbols = {SYMBOL: SymbolConfig(
        symbol="XRPUSDC", ccxt_symbol=SYMBOL, enabled=True,
        take_profit_spacing=0.003, grid_spacing=0.003, initial_quantity=0.02,
        limit_multiplier=5.0, threshold_multiplier=20.0,
    )}
    cfg.bandit.enabled = False
    bot = MaxGridBot(cfg)
    bot.order_executor.place_order = AsyncMock()
    bot.order_executor.cancel_orders_for_side = AsyncMock()
    return bot


@pytest.fixture
def sync():
    bot = _make_bot()
    s = bot.sync_service
    s._sync_positions = AsyncMock()
    s._sync_orders = AsyncMock()
    s._sync_account = AsyncMock()
    s._sync_funding_rates = AsyncMock()
    s._sync_trade_stats = AsyncMock()
    yield s
    clock.reset_clock()
    clock.reset_guard_clock()


@pytest.mark.asyncio
async def test_notifier_send_raising_does_not_kill_loop(sync):
    """告警自己炸掉不得殺 loop——通知是附屬品，同步才是主體。"""
    sync.notifier.bot_token = "t"
    sync.notifier.chat_id = "c"
    sync.notifier.send = AsyncMock(side_effect=RuntimeError("telegram down"))
    sync.config.sync_interval = 0.01
    sync.sync_all = AsyncMock(return_value=SyncOutcome(positions_ok=False))

    task = asyncio.create_task(sync.run())
    await asyncio.sleep(0.15)
    sync.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert sync._degraded is True      # 狀態照樣推進


@pytest.mark.asyncio
async def test_stop_then_run_exits_immediately(sync):
    """先 stop 再 run：不得卡住，不得跑任何同步。"""
    sync.stop()
    await asyncio.wait_for(sync.run(), timeout=2.0)
    sync._sync_positions.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_run_and_manual_sync_all(sync):
    """loop 與啟動時的 sync_all(bot.py:788) 撞在一起：靠 _sync_lock early-return，
    不得死鎖、不得重入。
    """
    sync.config.sync_interval = 0.01
    task = asyncio.create_task(sync.run())
    results = await asyncio.gather(*[sync.sync_all() for _ in range(5)])
    sync.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not sync._sync_lock.locked()
    assert any(r.skipped for r in results) or all(not r.skipped for r in results)


@pytest.mark.asyncio
async def test_flapping_does_not_spam_alerts(sync):
    """失敗↔成功來回抖動：每次真正進降級才發一封，不得洗版。"""
    sent = []
    sync._notify = lambda msg: sent.append(msg)
    for _ in range(5):
        for _ in range(SYNC_FAILURE_THRESHOLD):
            sync._evaluate(SyncOutcome(positions_ok=False))
        sync._evaluate(SyncOutcome())
    assert len(sent) == 10          # 5 次降級 + 5 次恢復，不多不少
    assert sync._degraded_total == 5


@pytest.mark.asyncio
async def test_config_interval_changed_at_runtime(sync):
    """執行中改 sync_interval：下一輪就該生效，不得要重啟。"""
    sync.config.sync_interval = 0.01
    task = asyncio.create_task(sync.run())
    await asyncio.sleep(0.05)
    sync.config.sync_interval = 999
    before = sync._sync_positions.call_count
    await asyncio.sleep(0.1)
    sync.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert sync._sync_positions.call_count - before <= 1
```

- [ ] **Step 2: 跑測試**

Run: `uv run pytest tests/test_periodic_sync_monkey.py -v`
Expected: 全綠。**任何一條紅都是真缺陷，回到對應 Task 修 code，不改測試遷就**。

- [ ] **Step 3: 全套回歸**

先 `pgrep -f as_terminal_max` 確認實盤引擎狀態，再：
Run: `uv run pytest -q`
Expected: 全綠。記錄「基線 X passed → 現在 Y passed，淨增 Z」。

- [ ] **Step 4: 確認回測不會啟動這個 loop**

spec §9 紅隊第 4 條。已查證的事實：`backtest/` 下**零處** import 或建構 `MaxGridBot`
（`grep -rn "MaxGridBot" backtest/*.py` 無輸出），所以 `SyncService.run()` 在回測中
不會被啟動。本步驟只需重跑一次這個 grep 確認仍成立，並跑回測相關測試確認未被波及：

Run: `grep -rn "MaxGridBot" backtest/*.py; uv run pytest tests/test_backtester_decision.py tests/test_clock.py -q`
Expected: grep 無輸出；測試全綠。若 grep 有輸出 ⇒ 停下來回報，設計前提改變了。

- [ ] **Step 5: 驗收準則逐條對帳**

對照 spec §6 的十條，逐條寫下「哪個測試守它」：

1. 靜默時仍週期同步 → `test_run_syncs_while_ticker_is_completely_silent`
2. ticker 不驅動同步 → `test_handle_ticker_does_not_drive_sync` + `test_handle_ticker_source_has_no_sync_call`
3. 告警狀態機 → `test_two_failures_do_not_alert` / `test_third_failure_alerts_once` / `test_degraded_does_not_repeat_alert` / `test_recovery_alerts_once_and_resets`
4. 非關鍵項不告警 → `test_non_critical_failures_never_alert`
5. cancel 乾淨 → `test_run_exits_cleanly_on_cancel`
6. 例外不殺 task → `test_run_survives_exception_and_counts_it`
7. 並發語意不變 → `tests/test_async_offload.py` 三條
8. 非法 interval → `test_loop_interval_clamps_illegal_values`
9. 全套綠 → Step 3
10. mutation 零存活 → Task 3 Step 5、Task 4 Step 5

有任何一條找不到對應測試 ⇒ 補測試，不得放行。

- [ ] **Step 6: Commit**

```bash
git add tests/test_periodic_sync_monkey.py
git commit -m "test(sync): 週期同步 monkey testing（告警炸掉/抖動/並發/執行中改設定）"
```

---

## 完成後（不屬於任何 Task，由主 session 執行）

1. `security-review` skill（本改動命中 Red Team Protocol 適用範圍：核心邏輯 + 風控路徑）
2. `dual-review` skill：外部獨立輪 + 專案規則輪，findings 併入整合修復
3. fresh-context `verifier`：read-back + 實跑測試
4. verdict 與各輪 findings 計數落 `tasks/notes.md`
5. merge 後**必須重啟引擎**才生效（Python import 時載入模組）。重啟後確認方式：
   `ps -o lstart= -p $(pgrep -f as_terminal_max | head -1)` 晚於 merge 的檔案寫入時刻
   （`ls -lT grid_engine/sync_service.py`），並在 log 看到新行程的初始化區塊。
