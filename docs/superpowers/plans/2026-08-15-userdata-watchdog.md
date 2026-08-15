# userData Watchdog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 userData stream 的靜默失效可被偵測、有限度自救，並讓面板／Telegram 的成交統計改由 REST 取得而不再依賴 userData。

**Architecture:** 新增獨立元件 `UserDataWatchdog`（純狀態機 + 自有 60 秒迴圈），由 `order_executor` 餵「下/撤單張數」、由 `bot.py` 的兩個 userData handler 餵「事件」；判死時呼叫 `ws_client.request_reconnect()`（設旗標，由 `run()` 內層迴圈自行 break，不從外部關 socket）。成交統計改由 `sync_service._sync_trade_stats()` 從 REST `fetch_my_trades` 增量取得，userData handler 停止寫該兩個計數器以維持單一 writer。

**Tech Stack:** Python 3.13、asyncio、ccxt（`binanceusdm`）、pytest。可注入時鐘用既有的 `grid_engine/clock.py`（`clock.now()` / `set_clock()` / `reset_clock()`）。

**Spec:** `docs/superpowers/specs/2026-08-15-userdata-watchdog-design.md`

## Global Constraints

- **工作目錄是 worktree**：`/Users/ramonliao/Documents/理財/加密貨幣/量化交易/LouisLab/as-grid-dragon-wt-userdata`（branch `feat/userdata-watchdog`）。全套測試必須在該目錄跑：monorepo 根目錄會被 `as-grid-auto/test_position_mode.py` 的 collection-time `sys.exit(1)` 打斷。`data/` 是指向本體 checkout 的 symlink。
- 測試基線（**worktree 內實測**）：**589 passed / 2 skipped**。兩條 skip 是 `test_backtest_matching_realdata.py`（K 線資料已變動）與 `tests/web/test_config_store.py:117`（worktree 無 gitignored 的 `config/`，這是刻意不 symlink——真錢引擎正在讀那個檔）。每個 Task 結束時全套必須維持此基線 + 新增測試全綠，數量要報出來。
- 只 `git add <file>...` 明確列出的檔案，禁止 `git add -A` / `git add .`。
- watchdog 不得具備下單、撤單、改倉能力；唯一副作用是「請求 WS 重連」與「發通知」。
- 重連硬上限 **3 次**，退避 **300 / 900 / 2700 秒**，之後進 `given_up` 終態。
- 不得對 `ws_client.run()` 拋例外來觸發重連——`ws_client.py` 開頭的 characterization 註解鎖定了「例外冒泡 = 重連」這條不變式。
- `total_trades` / `total_profit` 維持單一 writer（REST）。
- 判準常數：`K = 4`（`DEFAULT_ORDER_THRESHOLD`）、`N = 600.0` 秒（`DEFAULT_SILENCE_SECONDS`）。
- 引擎行程正在跑真錢（`ps aux | grep as_terminal_max`）。**測試不得寫入 `config/` 或 `log/`**，需要暫存檔一律用 `$(mktemp -d)`。

## File Structure

| 檔案 | 責任 |
|---|---|
| `grid_engine/userdata_watchdog.py`（新增） | 判死狀態機、退避、告警、觸發重連。不碰交易所。 |
| `tests/test_userdata_watchdog.py`（新增） | 狀態機單元測試（注入 clock、假 ws_client、假 notifier）。 |
| `grid_engine/ws_client.py`（改） | 新增 `request_reconnect()` 與內層迴圈的旗標檢查。 |
| `tests/test_ws_reconnect_request.py`（新增） | `request_reconnect()` 的行為測試。 |
| `grid_engine/order_executor.py`（改） | 成功下單／撤單各呼叫一次 `record_order_action()`。 |
| `grid_engine/bot.py`（改） | 建構與接線 watchdog；兩個 handler 呼叫 `record_event()`；`_handle_order_update` 停寫兩個計數器。 |
| `grid_engine/sync_service.py`（改） | 新增 `_sync_trade_stats()`，REST 增量統計成交。 |
| `tests/test_trade_stats_sync.py`（新增） | `_sync_trade_stats()` 的增量／去重／失敗不歸零測試。 |

---

### Task 1: `UserDataWatchdog` 狀態機

**Files:**
- Create: `grid_engine/userdata_watchdog.py`
- Test: `tests/test_userdata_watchdog.py`

**Interfaces:**
- Consumes: `grid_engine.clock.now()`；一個具備 `request_reconnect()` 的 ws_client 物件；一個具備 `enabled` 屬性與 `async send(msg)` 的 notifier。
- Produces:
  - `UserDataWatchdog(ws_client, notifier, tasks, stop_event, order_threshold=4, silence_seconds=600.0)`
  - `.record_order_action() -> None`
  - `.record_event() -> None`
  - `.check() -> None`
  - `async .run() -> None`
  - 屬性 `.state`（`'healthy'` / `'degraded'` / `'given_up'`）、`.attempts`、`.orders_since_event`、`.last_event_at`、`.next_attempt_at`
  - 模組常數 `CHECK_INTERVAL = 60.0`、`DEFAULT_ORDER_THRESHOLD = 4`、`DEFAULT_SILENCE_SECONDS = 600.0`、`BACKOFF_SECONDS = (300.0, 900.0, 2700.0)`

- [ ] **Step 1: 寫失敗測試**

建立 `tests/test_userdata_watchdog.py`：

```python
"""UserDataWatchdog 狀態機測試。

判準是「orders_since_event >= K 且 now - last_event_at >= N」——兩者同時成立。
只看時間會在真正安靜的時段誤報（引擎裝死、價格不動不 requote，實盤成交率曾低到
~1 筆/天）；只看張數則沒給推送延遲留餘裕。下面每條測試都在守衛其中一半。
"""
import asyncio
import pytest

from grid_engine import clock
from grid_engine.userdata_watchdog import (
    UserDataWatchdog, BACKOFF_SECONDS, DEFAULT_ORDER_THRESHOLD,
    DEFAULT_SILENCE_SECONDS,
)


class FakeWs:
    def __init__(self):
        self.reconnects = 0

    def request_reconnect(self):
        self.reconnects += 1


class FakeNotifier:
    enabled = True

    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)
        return True


@pytest.fixture
def frozen_clock():
    holder = {"t": 1_000_000.0}
    clock.set_clock(lambda: holder["t"])
    yield holder
    clock.reset_clock()


def make_wd(**kw):
    ws, notifier = FakeWs(), FakeNotifier()
    wd = UserDataWatchdog(ws_client=ws, notifier=notifier, tasks=[],
                          stop_event=asyncio.Event(), **kw)
    return wd, ws, notifier


def test_starts_healthy(frozen_clock):
    wd, ws, _ = make_wd()
    assert wd.state == "healthy"
    wd.check()
    assert ws.reconnects == 0


def test_quiet_period_does_not_trigger(frozen_clock):
    """安靜時段：時間到了但沒下單 -> 不得判死。守衛判準的 `and`。"""
    wd, ws, notifier = make_wd()
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS + 1
    wd.check()
    assert wd.state == "healthy"
    assert ws.reconnects == 0
    assert notifier.sent == []


def test_orders_without_elapsed_time_does_not_trigger(frozen_clock):
    """下了單但時間還沒到 -> 不得判死（留推送延遲餘裕）。"""
    wd, ws, _ = make_wd()
    for _ in range(DEFAULT_ORDER_THRESHOLD):
        wd.record_order_action()
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS - 1
    wd.check()
    assert wd.state == "healthy"
    assert ws.reconnects == 0


def test_below_order_threshold_does_not_trigger(frozen_clock):
    """張數不足門檻 -> 不得判死。守衛 K。"""
    wd, ws, _ = make_wd()
    for _ in range(DEFAULT_ORDER_THRESHOLD - 1):
        wd.record_order_action()
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS + 1
    wd.check()
    assert wd.state == "healthy"
    assert ws.reconnects == 0


def test_detects_and_reconnects_once(frozen_clock):
    wd, ws, notifier = make_wd()
    for _ in range(DEFAULT_ORDER_THRESHOLD):
        wd.record_order_action()
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS + 1
    wd.check()
    assert wd.state == "degraded"
    assert ws.reconnects == 1
    assert wd.attempts == 1
    assert len(notifier.sent) == 1
    # 立刻再 check 不得重複重連（退避未到）
    wd.check()
    assert ws.reconnects == 1


def test_backoff_sequence_then_give_up(frozen_clock):
    """退避必須是 300/900/2700，第 4 次評估進終態。"""
    wd, ws, notifier = make_wd()
    for _ in range(DEFAULT_ORDER_THRESHOLD):
        wd.record_order_action()
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS + 1
    wd.check()                       # attempt 1
    assert ws.reconnects == 1

    for i, wait in enumerate(BACKOFF_SECONDS):
        frozen_clock["t"] += wait - 1
        wd.check()                   # 退避未滿，不得動作
        assert ws.reconnects == i + 1, f"退避 {wait}s 未到就重連了"
        frozen_clock["t"] += 1
        wd.check()
        if i < len(BACKOFF_SECONDS) - 1:
            assert ws.reconnects == i + 2
        else:
            assert wd.state == "given_up"
            assert ws.reconnects == len(BACKOFF_SECONDS)

    # 終態後不論再過多久都不得重連
    frozen_clock["t"] += 100_000
    wd.check()
    assert ws.reconnects == len(BACKOFF_SECONDS)
    assert len(notifier.sent) == 2   # 判死一封 + 放棄一封


def test_event_resets_everything(frozen_clock):
    wd, ws, notifier = make_wd()
    for _ in range(DEFAULT_ORDER_THRESHOLD):
        wd.record_order_action()
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS + 1
    wd.check()
    assert wd.attempts == 1

    wd.record_event()
    assert wd.state == "healthy"
    assert wd.attempts == 0
    assert wd.orders_since_event == 0
    assert wd.next_attempt_at == 0.0
    assert len(notifier.sent) == 2   # 判死一封 + 恢復一封

    # 重置後必須重新累積才會再判死
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS + 1
    wd.check()
    assert ws.reconnects == 1


def test_event_leaves_given_up_state(frozen_clock):
    wd, ws, _ = make_wd()
    for _ in range(DEFAULT_ORDER_THRESHOLD):
        wd.record_order_action()
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS + 1
    wd.check()
    for wait in BACKOFF_SECONDS:
        frozen_clock["t"] += wait
        wd.check()
    assert wd.state == "given_up"

    wd.record_event()
    assert wd.state == "healthy"


def test_watchdog_has_no_trading_surface():
    """安全約束：watchdog 不得具備下單/撤單能力。"""
    forbidden = {"place_order", "cancel_order", "cancel_orders_for_side",
                 "close_symbol_positions", "create_order"}
    assert forbidden.isdisjoint(dir(UserDataWatchdog))
```

- [ ] **Step 2: 跑測試確認全紅**

```bash
cd "/Users/ramonliao/Documents/理財/加密貨幣/量化交易/LouisLab/as-grid-dragon"
uv run pytest tests/test_userdata_watchdog.py -v
```

Expected: 全部 FAIL，`ModuleNotFoundError: No module named 'grid_engine.userdata_watchdog'`

- [ ] **Step 3: 寫實作**

建立 `grid_engine/userdata_watchdog.py`：

```python
"""userData 靜默失效偵測與有限度復原。

背景：userData stream 自 2026-07-12 起靜默死亡至今（2026-08-15），而系統沒有任何
偵測——唯一的外部症狀是 keepalive 每 30 分鐘的 -1125，那條在 6a264d6 被修掉之後
症狀消失、故障還在。本元件處理的是「沒有儀器」這個缺陷，不是根因。

判準綁事件計數而非純時間：實盤成交率曾低到 ~1 筆/天，純時間判準會在安靜時段誤報。
"""
import asyncio

from . import clock
from .utils import logger

CHECK_INTERVAL = 60.0
DEFAULT_ORDER_THRESHOLD = 4        # 引擎 requote 一次即 4 張
DEFAULT_SILENCE_SECONDS = 600.0
BACKOFF_SECONDS = (300.0, 900.0, 2700.0)


class UserDataWatchdog:
    def __init__(self, ws_client, notifier, tasks, stop_event,
                 order_threshold: int = DEFAULT_ORDER_THRESHOLD,
                 silence_seconds: float = DEFAULT_SILENCE_SECONDS):
        self.ws_client = ws_client
        self.notifier = notifier
        self.tasks = tasks          # bot.tasks 共享參照：通知 task 防 GC + stop 可 cancel
        self._stop_event = stop_event
        self.order_threshold = order_threshold
        self.silence_seconds = silence_seconds

        self.state = "healthy"
        self.orders_since_event = 0
        self.last_event_at = clock.now()
        self.attempts = 0
        self.next_attempt_at = 0.0
        self._alerted = False

    # ---- 輸入 ----
    def record_order_action(self):
        """order_executor 每次成功下單/撤單呼叫一次。"""
        self.orders_since_event += 1

    def record_event(self):
        """userData handler 每收到一筆事件呼叫一次。唯一的復原入口。"""
        recovered = self._alerted
        self.orders_since_event = 0
        self.last_event_at = clock.now()
        self.state = "healthy"
        self.attempts = 0
        self.next_attempt_at = 0.0
        self._alerted = False
        if recovered:
            msg = "✅ userData stream 已恢復推送，成交事件重新進來了"
            logger.info(msg)
            self._notify(msg)

    # ---- 判定 ----
    def _is_dead(self) -> bool:
        # 兩個條件必須同時成立，見模組 docstring
        return (self.orders_since_event >= self.order_threshold
                and clock.now() - self.last_event_at >= self.silence_seconds)

    def check(self):
        if self.state == "given_up":
            return
        if not self._is_dead():
            return

        now = clock.now()
        if now < self.next_attempt_at:
            return

        if self.attempts >= len(BACKOFF_SECONDS):
            self.state = "given_up"
            msg = (f"⛔ userData stream 自動復原失敗：已重連 {self.attempts} 次仍無事件推送，"
                   f"停止自動復原。成交統計改由 REST 維持，但事件驅動路徑失效中，需人工介入。")
            logger.error(msg)
            self._notify(msg)
            return

        self.attempts += 1
        self.state = "degraded"
        self.next_attempt_at = now + BACKOFF_SECONDS[self.attempts - 1]
        logger.warning(
            f"[watchdog] userData 靜默失效判定成立："
            f"{self.orders_since_event} 張單無推送、靜默 {now - self.last_event_at:.0f}s，"
            f"強制重連（第 {self.attempts}/{len(BACKOFF_SECONDS)} 次）"
        )
        self.ws_client.request_reconnect()

        if not self._alerted:
            self._alerted = True
            self._notify(
                f"⚠️ userData stream 疑似靜默失效："
                f"已下/撤 {self.orders_since_event} 張單但零事件推送。"
                f"將嘗試自動重連最多 {len(BACKOFF_SECONDS)} 次。"
            )

    # ---- 迴圈 ----
    async def run(self):
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(CHECK_INTERVAL)
                if self._stop_event.is_set():
                    break
                self.check()
            except asyncio.CancelledError:
                break
            except Exception as e:
                # watchdog 自己掛掉會讓「沒有儀器」的問題原樣重演，故吞例外續跑
                logger.error(f"[watchdog] check 失敗: {e}")

    def _notify(self, message: str):
        if not self.notifier.enabled:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return      # 無 event loop（同步測試環境）時只留 log
        task = asyncio.create_task(self.notifier.send(message))
        self.tasks.append(task)
        task.add_done_callback(lambda t: t in self.tasks and self.tasks.remove(t))
```

- [ ] **Step 4: 跑測試確認全綠**

```bash
uv run pytest tests/test_userdata_watchdog.py -v
```

Expected: 10 passed

- [ ] **Step 5: 跑 mutation，每條必須先紅一次**

逐條手動改 `grid_engine/userdata_watchdog.py`，每次只改一處，跑 `uv run pytest tests/test_userdata_watchdog.py`，確認**有測試轉紅**，然後改回來：

| # | 改動 | 預期轉紅的測試 |
|---|---|---|
| M1 | `_is_dead` 的 `and` 改成 `or` | `test_quiet_period_does_not_trigger`、`test_orders_without_elapsed_time_does_not_trigger` |
| M2 | `DEFAULT_ORDER_THRESHOLD` 改成 `0` | `test_below_order_threshold_does_not_trigger` |
| M3 | `BACKOFF_SECONDS` 改成 `(300.0, 300.0, 300.0)` | `test_backoff_sequence_then_give_up` |
| M4 | `check()` 開頭的 `if self.state == "given_up": return` 刪掉 | `test_backoff_sequence_then_give_up` |
| M5 | `record_event()` 裡 `self.attempts = 0` 刪掉 | `test_event_resets_everything` |

任何一條**沒有**轉紅 = 該測試是假守衛，必須補測試再繼續。把五條的實際結果記下來，Task 結束時回報。

- [ ] **Step 6: 跑全套 + commit**

```bash
uv run pytest -q
git add grid_engine/userdata_watchdog.py tests/test_userdata_watchdog.py
git commit -m "feat(watchdog): userData 靜默失效判定狀態機

判準綁事件計數（orders_since_event >= K 且靜默 >= N），純時間判準會在
實盤安靜時段誤報。退避 300/900/2700 後進 given_up 終態。"
```

---

### Task 2: `ws_client.request_reconnect()`

**Files:**
- Modify: `grid_engine/ws_client.py`
- Test: `tests/test_ws_reconnect_request.py`

**Interfaces:**
- Consumes: 無（獨立於 Task 1）
- Produces: `WsClient.request_reconnect() -> None`；屬性 `WsClient._reconnect_requested: bool`

- [ ] **Step 1: 寫失敗測試**

建立 `tests/test_ws_reconnect_request.py`：

```python
"""request_reconnect() 行為測試。

重點在「設旗標 + 內層迴圈自行 break」而不是從外部關 socket 或拋例外——
ws_client.py 開頭的 characterization 註解鎖定了「例外冒泡 = 重連」這條不變式，
借用它會讓「handler 出錯」與「watchdog 故意觸發」無法區分。
"""
import asyncio
import pytest

from grid_engine.ws_client import WsClient


def make_client():
    return WsClient(gateway=None, ctx=None, config=None, state=None,
                    stop_event=asyncio.Event(), handlers={})


def test_flag_defaults_false():
    assert make_client()._reconnect_requested is False


def test_request_sets_flag():
    c = make_client()
    c.request_reconnect()
    assert c._reconnect_requested is True


def test_consume_clears_flag():
    """旗標必須是一次性的，否則會變成永久重連迴圈。"""
    c = make_client()
    c.request_reconnect()
    assert c._consume_reconnect_request() is True
    assert c._reconnect_requested is False
    assert c._consume_reconnect_request() is False


def test_request_is_idempotent():
    c = make_client()
    c.request_reconnect()
    c.request_reconnect()
    assert c._consume_reconnect_request() is True
    assert c._consume_reconnect_request() is False
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
uv run pytest tests/test_ws_reconnect_request.py -v
```

Expected: FAIL，`AttributeError: 'WsClient' object has no attribute '_reconnect_requested'`

- [ ] **Step 3: 改 `ws_client.py`**

在 `__init__` 末尾（`self.listen_key: Optional[str] = None` 之後）加：

```python
        # watchdog 觸發的重連請求：只設旗標，由 run() 內層迴圈自行 break。
        # 不從外部關 socket、也不對 run() 拋例外——後者會污染本檔開頭
        # characterization 註解鎖定的「例外冒泡 = 重連」語意。
        self._reconnect_requested = False
```

在類別中加兩個方法：

```python
    def request_reconnect(self):
        """請求下一次迴圈檢查時斷開重連（最壞延遲 = recv timeout 30s）。"""
        self._reconnect_requested = True

    def _consume_reconnect_request(self) -> bool:
        """讀取並清除旗標。一次性語意：清不掉會變成永久重連迴圈。"""
        if self._reconnect_requested:
            self._reconnect_requested = False
            return True
        return False
```

在 `run()` 的內層 `while` 中，把現有的：

```python
                        except asyncio.TimeoutError:
                            await ws.ping()
```

改成：

```python
                        except asyncio.TimeoutError:
                            await ws.ping()

                        if self._consume_reconnect_request():
                            logger.warning("[WebSocket] 收到重連請求，主動斷開重連")
                            break
```

註：該檢查放在 `try/except` 之後、內層 `while` 本體末尾，因此 `recv()` 成功與 timeout 兩條路徑都會經過。

- [ ] **Step 4: 跑測試確認通過**

```bash
uv run pytest tests/test_ws_reconnect_request.py -v
```

Expected: 4 passed

- [ ] **Step 5: mutation 驗證**

| # | 改動 | 預期轉紅 |
|---|---|---|
| M6 | `_consume_reconnect_request` 不清旗標（刪 `self._reconnect_requested = False`） | `test_consume_clears_flag`、`test_request_is_idempotent` |

- [ ] **Step 6: 跑全套 + commit**

```bash
uv run pytest -q
git add grid_engine/ws_client.py tests/test_ws_reconnect_request.py
git commit -m "feat(ws): request_reconnect() 旗標式重連請求

不從外部關 socket、不對 run() 拋例外——後者會污染 characterization
註解鎖定的「例外冒泡 = 重連」語意。旗標一次性消費。"
```

---

### Task 3: REST 成交統計（單一 writer）

**Files:**
- Modify: `grid_engine/sync_service.py`
- Modify: `grid_engine/bot.py`（`_handle_order_update` 停寫兩個計數器）
- Test: `tests/test_trade_stats_sync.py`

**Interfaces:**
- Consumes: 無（獨立於 Task 1、2）
- Produces: `SyncService(..., start_time_ms: int)` 新增建構參數；`SyncService._sync_trade_stats()`；`SyncService._last_trade_id: Dict[str, int]`；模組常數 `TRADE_STATS_INTERVAL = 60.0`

- [ ] **Step 1: 寫失敗測試**

建立 `tests/test_trade_stats_sync.py`：

```python
"""REST 成交統計測試。

單一 writer 是硬約束：userData handler 與 REST 同時寫 total_trades/total_profit
的話，userData 一旦復活數字就會翻倍。
"""
import asyncio
import pytest

from grid_engine import clock
from grid_engine.state import GlobalState, SymbolState
from grid_engine.sync_service import SyncService, TRADE_STATS_INTERVAL


class FakeGateway:
    async def call(self, fn, *a, **kw):
        return fn(*a, **kw)


class FakeExchange:
    def __init__(self, pages):
        self.pages = pages          # list[list[dict]]，依序回傳
        self.calls = 0

    def fetch_my_trades(self, symbol=None, since=None, limit=None):
        page = self.pages[min(self.calls, len(self.pages) - 1)]
        self.calls += 1
        return page


class FakeCtx:
    def __init__(self, exchange):
        self.exchange = exchange
        self.funding_manager = None


class FakeSymCfg:
    def __init__(self, symbol="BNB/USDC:USDC"):
        self.enabled = True
        self.ccxt_symbol = symbol


class FakeConfig:
    def __init__(self):
        self.symbols = {"BNBUSDC": FakeSymCfg()}
        self.sync_interval = 10


def trade(tid, pnl, ts=1_700_000_000_000):
    return {"id": str(tid), "timestamp": ts, "info": {"realizedPnl": str(pnl)}}


def make_service(pages):
    ex = FakeExchange(pages)
    state = GlobalState()
    state.symbols["BNB/USDC:USDC"] = SymbolState(symbol="BNB/USDC:USDC")
    svc = SyncService(gateway=FakeGateway(), ctx=FakeCtx(ex), config=FakeConfig(),
                      state=state, locks=None, notifier=None, risk_monitor=None,
                      tasks=[], start_time_ms=1_699_000_000_000)
    return svc, state, ex


@pytest.fixture
def frozen_clock():
    holder = {"t": 1_000_000.0}
    clock.set_clock(lambda: holder["t"])
    yield holder
    clock.reset_clock()


def test_counts_and_sums(frozen_clock):
    svc, state, _ = make_service([[trade(1, "0.5"), trade(2, "-0.25")]])
    asyncio.run(svc._sync_trade_stats())
    st = state.symbols["BNB/USDC:USDC"]
    assert st.total_trades == 2
    assert st.total_profit == pytest.approx(0.25)
    assert state.total_trades == 2
    assert state.total_profit == pytest.approx(0.25)


def test_incremental_no_double_count(frozen_clock):
    """同一筆成交重複出現在後續回應中，不得被算第二次。"""
    svc, state, _ = make_service([
        [trade(1, "0.5"), trade(2, "-0.25")],
        [trade(1, "0.5"), trade(2, "-0.25"), trade(3, "1.0")],
    ])
    asyncio.run(svc._sync_trade_stats())
    frozen_clock["t"] += TRADE_STATS_INTERVAL
    asyncio.run(svc._sync_trade_stats())
    st = state.symbols["BNB/USDC:USDC"]
    assert st.total_trades == 3
    assert st.total_profit == pytest.approx(1.25)


def test_throttled(frozen_clock):
    svc, _, ex = make_service([[trade(1, "0.5")]])
    asyncio.run(svc._sync_trade_stats())
    asyncio.run(svc._sync_trade_stats())     # 節流內，不得再打 API
    assert ex.calls == 1


def test_failure_does_not_zero_counters(frozen_clock):
    """REST 失敗必須保留既有數值，不得當成 0 筆寫回去。"""
    svc, state, ex = make_service([[trade(1, "0.5")]])
    asyncio.run(svc._sync_trade_stats())
    assert state.symbols["BNB/USDC:USDC"].total_trades == 1

    def boom(**kw):
        raise RuntimeError("REST down")

    ex.fetch_my_trades = boom
    frozen_clock["t"] += TRADE_STATS_INTERVAL
    asyncio.run(svc._sync_trade_stats())
    assert state.symbols["BNB/USDC:USDC"].total_trades == 1
    assert state.symbols["BNB/USDC:USDC"].total_profit == pytest.approx(0.5)


def test_userdata_handler_no_longer_writes_counters():
    """單一 writer 守衛：handler 原始碼不得再累加這兩個計數器。"""
    src = open("grid_engine/bot.py", encoding="utf-8").read()
    start = src.index("async def _handle_order_update")
    end = src.index("async def run(self)", start)
    body = src[start:end]
    assert "total_trades += 1" not in body
    assert "total_profit += realized_pnl" not in body
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
uv run pytest tests/test_trade_stats_sync.py -v
```

Expected: FAIL，`ImportError: cannot import name 'TRADE_STATS_INTERVAL'`

- [ ] **Step 3: 改 `sync_service.py`**

檔頭 import 加 `from . import clock`，並在 `from .utils import logger` 之後加模組常數：

```python
TRADE_STATS_INTERVAL = 60.0     # 與 sync_interval(10s) 解耦，省 API 權重
```

`__init__` 簽名末尾加 `start_time_ms: int = 0`，並在本體末尾加：

```python
        # 成交統計：口徑為「本次引擎啟動以來」，與 userData 時代的語意一致
        self.start_time_ms = start_time_ms
        self._last_trade_id: dict = {}
        self._last_trade_stats_at = 0.0
```

`sync_all()` 末尾加一行 `await self._sync_trade_stats()`：

```python
    async def sync_all(self):
        if self._sync_lock.locked():
            return
        async with self._sync_lock:
            await self._sync_positions()
            await self._sync_orders()
            await self._sync_account()
            await self._sync_funding_rates()
            await self._sync_trade_stats()
```

新增方法：

```python
    async def _sync_trade_stats(self):
        """成交次數/已實現盈虧的**唯一** writer。

        userData handler 曾經是唯一 writer，而該路徑 2026-07-12 起靜默死亡一個月，
        面板與 Telegram 日報的數字全是 0。改由 REST 維持後，兩處同時寫會在 userData
        復活時造成翻倍 ⇒ handler 已停寫，這裡是單一 writer。
        """
        if clock.now() - self._last_trade_stats_at < TRADE_STATS_INTERVAL:
            return

        for sym_config in self.config.symbols.values():
            if not sym_config.enabled:
                continue
            symbol = sym_config.ccxt_symbol
            st = self.state.symbols.get(symbol)
            if not st:
                continue

            max_id = self._last_trade_id.get(symbol, 0)
            since = self.start_time_ms
            try:
                while True:
                    trades = await self.gateway.call(
                        self.ctx.exchange.fetch_my_trades,
                        symbol=symbol, since=since, limit=1000)
                    if not trades:
                        break
                    for t in trades:
                        try:
                            tid = int(t.get('id'))
                        except (TypeError, ValueError):
                            continue
                        if tid <= max_id:
                            continue
                        max_id = max(max_id, tid)
                        st.total_trades += 1
                        st.total_profit += float(
                            t.get('info', {}).get('realizedPnl', 0) or 0)
                    if len(trades) < 1000:
                        break
                    # 分頁：Binance 單次上限 1000，用最後一筆時間往後推
                    last_ts = trades[-1].get('timestamp')
                    if not last_ts:
                        break
                    since = int(last_ts) + 1
            except Exception as e:
                # 失敗保留既有數值。把失敗當成 0 筆寫回去會讓面板數字倒退。
                logger.error(f"同步 {symbol} 成交統計失敗: {e}")
                continue

            self._last_trade_id[symbol] = max_id

        self.state.total_trades = sum(s.total_trades for s in self.state.symbols.values())
        self.state.total_profit = sum(s.total_profit for s in self.state.symbols.values())
        self._last_trade_stats_at = clock.now()
```

- [ ] **Step 4: 改 `bot.py` 的 `_handle_order_update` 停寫計數器**

把 `if order_status == 'FILLED':` 底下的：

```python
                sym_state.total_trades += 1
                self.state.total_trades += 1
```

改成：

```python
                # total_trades / total_profit 的 writer 已改為 sync_service._sync_trade_stats()
                # （REST）。這裡再寫一次會在 userData 復活時造成計數翻倍。
```

並把：

```python
                if realized_pnl != 0:
                    sym_state.total_profit += realized_pnl
                    self.state.total_profit += realized_pnl
                    pnl_sign = ...
```

的兩行累加刪掉，保留 `pnl_sign` 起的 log 與後續 bandit/dgt 邏輯：

```python
                if realized_pnl != 0:
                    pnl_sign = "+" if realized_pnl > 0 else ""
                    logger.info(f"[userData] {symbol_raw} 成交! {side} {position_side}, "
                               f"盈虧: {pnl_sign}{realized_pnl:.4f}")
```

- [ ] **Step 5: 改 `bot.py` 的 SyncService 建構傳入 `start_time_ms`**

在 `SyncService(...)` 的建構參數末尾加 `start_time_ms=int(time.time() * 1000),`（`bot.py` 已 import `time`；若沒有則補 import）。

- [ ] **Step 6: 跑測試確認通過**

```bash
uv run pytest tests/test_trade_stats_sync.py -v
```

Expected: 5 passed

- [ ] **Step 7: mutation 驗證**

| # | 改動 | 預期轉紅 |
|---|---|---|
| M7 | `if tid <= max_id: continue` 刪掉 | `test_incremental_no_double_count` |
| M8 | except 分支改成把 `st.total_trades = 0` 寫回去 | `test_failure_does_not_zero_counters` |
| M9 | 節流的 `if ... < TRADE_STATS_INTERVAL: return` 刪掉 | `test_throttled` |
| M10 | 把 `bot.py` 的 `sym_state.total_trades += 1` 加回去 | `test_userdata_handler_no_longer_writes_counters` |

- [ ] **Step 8: 跑全套 + commit**

```bash
uv run pytest -q
git add grid_engine/sync_service.py grid_engine/bot.py tests/test_trade_stats_sync.py
git commit -m "feat(sync): 成交統計改由 REST 維持，userData handler 停寫計數器

userData 路徑 2026-07-12 起靜默死亡一個月，面板與 Telegram 日報的
「累計已實現」全是 0。改為單一 writer 避免 userData 復活時計數翻倍。"
```

---

### Task 4: 接線

**Files:**
- Modify: `grid_engine/bot.py`
- Modify: `grid_engine/order_executor.py`
- Test: `tests/test_userdata_watchdog_wiring.py`（新增）

**Interfaces:**
- Consumes: Task 1 的 `UserDataWatchdog(ws_client, notifier, tasks, stop_event)` 與 `.record_order_action()` / `.record_event()` / `.run()`；Task 2 的 `WsClient.request_reconnect()`
- Produces: `MaxGridBot.userdata_watchdog` 屬性；`OrderExecutor(..., watchdog=None)` 新增建構參數

- [ ] **Step 1: 寫失敗測試**

建立 `tests/test_userdata_watchdog_wiring.py`：

```python
"""接線測試：訊號要真的從 order_executor / handler 流進 watchdog。

參照 tests/test_bot_requote_wiring.py 的作法——這類測試存在的理由是
「元件本身正確但沒被接上」這個失效模式，單測抓不到。
"""
import asyncio
import pytest

from grid_engine.order_executor import OrderExecutor


class SpyWatchdog:
    def __init__(self):
        self.orders = 0
        self.events = 0

    def record_order_action(self):
        self.orders += 1

    def record_event(self):
        self.events += 1


class FakeGateway:
    async def call(self, fn, *a, **kw):
        return fn(*a, **kw)


class FakeExchange:
    def create_order(self, *a, **kw):
        return {"id": "1"}

    def fetch_open_orders(self, symbol):
        return [{"id": "9", "side": "buy",
                 "info": {"positionSide": "LONG", "origQty": "0.02"},
                 "reduceOnly": False}]

    def cancel_order(self, oid, symbol):
        return {"id": oid}


class FakeCtx:
    exchange = FakeExchange()
    precisions = {"BNB/USDC:USDC": {"price": 2, "amount": 2, "min_amount": 0.01}}


class FakeLocks:
    def get(self, symbol):
        return asyncio.Lock()


def make_executor(wd):
    return OrderExecutor(
        gateway=FakeGateway(), ctx=FakeCtx(), state=None, notifier=None,
        config=None, locks=FakeLocks(), stop_event=asyncio.Event(),
        tasks=[], watchdog=wd)


def test_place_order_records_action():
    wd = SpyWatchdog()
    ex = make_executor(wd)
    asyncio.run(ex.place_order("BNB/USDC:USDC", "buy", 600.0, 0.02,
                               position_side="long"))
    assert wd.orders == 1


def test_cancel_records_action():
    wd = SpyWatchdog()
    ex = make_executor(wd)
    asyncio.run(ex.cancel_orders_for_side("BNB/USDC:USDC", "long"))
    assert wd.orders == 1


def test_executor_works_without_watchdog():
    """watchdog=None 時不得爆炸（回測/測試路徑不接 watchdog）。"""
    ex = make_executor(None)
    assert asyncio.run(ex.place_order("BNB/USDC:USDC", "buy", 600.0, 0.02,
                                      position_side="long")) is not None


def test_handlers_call_record_event():
    """兩個 userData handler 都必須餵 watchdog，否則判準永遠不會復原。"""
    src = open("grid_engine/bot.py", encoding="utf-8").read()
    for name in ("_handle_account_update", "_handle_order_update"):
        start = src.index(f"async def {name}")
        end = src.index("\n    async def ", start + 10)
        assert "self.userdata_watchdog.record_event()" in src[start:end], name


def test_watchdog_run_is_scheduled():
    src = open("grid_engine/bot.py", encoding="utf-8").read()
    assert "self.userdata_watchdog.run()" in src
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
uv run pytest tests/test_userdata_watchdog_wiring.py -v
```

Expected: FAIL，`TypeError: OrderExecutor.__init__() got an unexpected keyword argument 'watchdog'`

- [ ] **Step 3: 改 `order_executor.py`**

`__init__` 簽名末尾加 `watchdog=None`，本體加 `self.watchdog = watchdog`。

`place_order` 在 `return result` 之前加：

```python
            # 交易所端每次下單成功必定推一筆 ORDER_TRADE_UPDATE ⇒ 這是 watchdog 的訊號源
            if self.watchdog:
                self.watchdog.record_order_action()
```

`cancel_orders_for_side` 在 `await self.gateway.call(self.ctx.exchange.cancel_order, order['id'], symbol)` 之後加：

```python
                    if self.watchdog:
                        self.watchdog.record_order_action()
```

- [ ] **Step 4: 改 `bot.py` 接線**

在 `self.ws_client = WsClient(...)` 建構**之後**加：

```python
        # userData 靜默失效偵測（必須在 ws_client 之後建構：需要它的 request_reconnect）
        self.userdata_watchdog = UserDataWatchdog(
            ws_client=self.ws_client, notifier=self.notifier,
            tasks=self.tasks, stop_event=self._stop_event,
        )
        self.order_executor.watchdog = self.userdata_watchdog
```

檔頭加 `from .userdata_watchdog import UserDataWatchdog`。

在 `_handle_account_update` 與 `_handle_order_update` 的 `try:` 第一行各加：

```python
            self.userdata_watchdog.record_event()
```

在 `run()` 裡建立其他長跑 task 的地方（與 `ws_client.keep_alive_loop()` 同一處），加：

```python
            self.tasks.append(asyncio.create_task(self.userdata_watchdog.run()))
```

- [ ] **Step 5: 跑測試確認通過**

```bash
uv run pytest tests/test_userdata_watchdog_wiring.py -v
```

Expected: 5 passed

- [ ] **Step 6: mutation 驗證**

| # | 改動 | 預期轉紅 |
|---|---|---|
| M11 | `place_order` 的 `record_order_action()` 刪掉 | `test_place_order_records_action` |
| M12 | `cancel_orders_for_side` 的 `record_order_action()` 刪掉 | `test_cancel_records_action` |
| M13 | `_handle_order_update` 的 `record_event()` 刪掉 | `test_handlers_call_record_event` |

- [ ] **Step 7: 全套 + 靜態檢查 + commit**

```bash
uv run pytest -q
uv run python -m py_compile grid_engine/userdata_watchdog.py grid_engine/ws_client.py \
    grid_engine/order_executor.py grid_engine/bot.py grid_engine/sync_service.py
git add grid_engine/order_executor.py grid_engine/bot.py tests/test_userdata_watchdog_wiring.py
git commit -m "feat(watchdog): 接線——order_executor 餵張數、handler 餵事件

order_executor 的 place_order/cancel_orders_for_side 是下/撤單的唯一咽喉點，
掛在那裡零額外 API 呼叫。watchdog=None 時整條路徑降級為 no-op。"
```

---

### Task 5: 驗收與活體檢查

**Files:** 無（純驗收）

**Interfaces:**
- Consumes: Task 1-4 的全部產出

- [ ] **Step 1: 全套測試 + 數量對帳**

```bash
uv run pytest -q
```

Expected: `590 + 新增數量` passed / 1 skipped。把新增數量逐檔列出（Task 1 十條、Task 2 四條、Task 3 五條、Task 4 五條 = 24 條，實際數字以跑出來為準）。

- [ ] **Step 2: 派 fresh-context `verifier`**

依 dev-rules「實作者不自我驗收」，派 `verifier` agent。派工三件套：

- 目標與背景：本 plan + `docs/superpowers/specs/2026-08-15-userdata-watchdog-design.md`
- 驗收條件：spec §7 的四條可判定準則；read-back 五個改動檔；獨立挑 mutation 自己驗（不吃本 plan 列的 M1-M13）
- 回報格式：ACCEPT / ACCEPT WITH FINDINGS / REJECT + 逐條證據
- 檔案系統邊界：所有寫入/執行只能發生在 `$(mktemp -d)`，repo 內一律唯讀；**禁止寫 `config/` 與 `log/`**（真錢引擎正在跑）

- [ ] **Step 3: `security-review` skill**

改動命中 Red Team Protocol 適用範圍（會刪改使用者資料路徑的相鄰程式碼 + 會影響下單迴圈的重連）⇒ 依 dev-rules，外部 review 輪**之前**先跑 `security-review`，findings 併入整合修復。

- [ ] **Step 4: `dual-review` skill**

Round 1 外部 fresh-context 輪 + Round 2 專案規則輪。verdict 未到 `Ship as-is` 前不得標記完成。verdict 與各輪 findings 計數落 `tasks/notes.md`。

- [ ] **Step 5: 活體驗收（需使用者重啟引擎）**

重啟後依 spec §7.3 逐條檢查：

```bash
# 判死與告警（生產當前就處於失效態，應在 10 分鐘 + 4 張單內出現）
grep "watchdog" log/as_terminal_max.log

# 面板成交次數應在 60 秒內從 0 變成 REST 實測值
grep "userData" log/as_terminal_max.log | tail -5

# 三次重連後進終態，之後不得再出現重連 log
grep -c "強制重連" log/as_terminal_max.log      # 上限 3
grep "停止自動復原" log/as_terminal_max.log

# 回歸：decide() 觸發頻率不得因強制重連顯著劣化
grep "\[MAX\]" log/as_terminal_max.log | tail -20
```

⚠️ 重啟是使用者的操作，不要自己動生產行程。

- [ ] **Step 6: 更新 `tasks/progress.md` 與 `tasks/notes.md`**

記錄：verdict、各輪 findings 計數（格式 `ext C?/B?、int C?`）、活體驗收數字、以及**根因仍未確定**這件事不因本任務完成而結案。
