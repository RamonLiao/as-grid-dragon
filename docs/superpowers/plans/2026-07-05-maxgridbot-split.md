# #7 MaxGridBot 組件化拆分 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `grid_engine/bot.py` 的 MaxGridBot（1153 行檔案）拆成 7 個組件 + 組合根，行為零改變，全套既有測試斷言不改。

**Architecture:** 組件化拆分——每組件持有自己的狀態、依賴建構子注入；`ExchangeContext` 共享可變容器解決兩階段初始化（exchange/precisions/funding_manager 在 `run()→_init_exchange` 才有真值）；bot 保留生命週期、網格鏈、WS handlers。Spec：`docs/superpowers/specs/2026-07-05-maxgridbot-split-design.md`（必讀，特別是「不變式」與「兩階段初始化協定」段）。

**Tech Stack:** Python 3.13 / asyncio / ccxt（同步，非 thread-safe）/ pytest 9

## Global Constraints

- 行為零改變：既有測試**斷言一律不改**，只允許改 patch 目標與屬性路徑（如 `bot._rest` → `bot.gateway.call`）。
- 全部 ccxt REST 走**同一個** RestGateway 實例（單 worker，ccxt 非 thread-safe）。
- 鎖序單向 `_sync_lock → symbol lock`；SymbolLocks 全 bot 共享**同一實例**（同 symbol 必須拿到同一把鎖物件）。
- REST apply「fetch 鎖外、寫回鎖內無 await」原子區原樣搬移。
- 斷路器語意原樣：僅開倉單成功重置、封鎖只擋開倉、reduce_only 永遠放行。
- 組件**絕不**在 `__init__` 快照 `ctx.exchange/ctx.precisions/ctx.funding_manager`——一律呼叫當下讀 `self.ctx.<attr>`（防 None 快照 → funding 同步靜默失效）。
- `_stop_event` 是 bot 建立、共享注入的同一個 `asyncio.Event`。
- WsClient **不得**用 try 包 handler callback：ticker handler 例外必須冒泡到重連迴圈（現行為）。
- 每 task 結束全套測試綠才 commit：`python3 -m pytest tests/ -q`，預期 `267 passed`（新增測試後數字遞增，紅的中間態不落 commit）。
- `git add` 只 stage 明確列出的檔案，禁止 `git add -A` / `git add .`。
- 錯誤處理原樣搬移，不新增不收斂；發現既有問題記 follow-up 不順手修。

---

### Task 1: 葉子組件 — ExchangeContext / SymbolLocks / RestGateway + bot 接線

**Files:**
- Create: `grid_engine/context.py`
- Create: `grid_engine/locks.py`
- Create: `grid_engine/rest_gateway.py`
- Create: `tests/test_components.py`
- Modify: `grid_engine/bot.py`（`__init__`、`_rest` 呼叫點、`_symbol_lock` 呼叫點、`run`/`stop` 的 executor 引用）
- Modify: `tests/test_async_offload.py`（patch 路徑遷移）

**Interfaces:**
- Produces:
  - `ExchangeContext`：dataclass，欄位 `exchange: Optional[Any] = None`、`precisions: Dict[str, dict]`（default_factory=dict）、`funding_manager: Optional[Any] = None`
  - `SymbolLocks.get(ccxt_symbol: str) -> asyncio.Lock`（同 symbol 回同一把；內部 dict 屬性名 `_locks`）
  - `RestGateway.call(fn, *args, **kwargs) -> Awaitable[Any]`（單 worker executor 執行）、`RestGateway.shutdown()`（`wait=False, cancel_futures=True`）
  - bot 新屬性：`self.ctx`、`self.locks`、`self.gateway`；bot 的 `exchange`/`precisions`/`funding_manager` 變成轉發 `ctx` 的 property（getter+setter）——**既有測試 `bot.exchange = MagicMock()` 因此不用改**

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_components.py
"""葉子組件測試：ExchangeContext 兩階段 / SymbolLocks 鎖同一性 / RestGateway 單 worker 與停機"""
import asyncio
import threading

import pytest

from grid_engine.context import ExchangeContext
from grid_engine.locks import SymbolLocks
from grid_engine.rest_gateway import RestGateway


def test_exchange_context_starts_empty_and_mutable():
    ctx = ExchangeContext()
    assert ctx.exchange is None
    assert ctx.precisions == {}
    assert ctx.funding_manager is None
    ctx.exchange = object()
    ctx.precisions["BNB/USDC:USDC"] = {"price": 4}
    assert ctx.exchange is not None


def test_symbol_locks_identity():
    locks = SymbolLocks()
    a = locks.get("BNB/USDC:USDC")
    b = locks.get("BNB/USDC:USDC")
    c = locks.get("BTC/USDC:USDC")
    assert a is b          # 同 symbol 同一把鎖（不變式 2 的根基）
    assert a is not c
    assert isinstance(a, asyncio.Lock)


def test_rest_gateway_single_worker_serializes():
    gw = RestGateway()
    seen = []

    def record(i):
        seen.append(threading.current_thread().name)
        return i

    async def main():
        results = await asyncio.gather(*[gw.call(record, i) for i in range(5)])
        return results

    results = asyncio.run(main())
    assert results == [0, 1, 2, 3, 4]
    assert len(set(seen)) == 1                      # 全部同一 worker thread
    assert seen[0].startswith("ccxt-rest")
    gw.shutdown()


def test_rest_gateway_shutdown_rejects_new_calls():
    gw = RestGateway()
    gw.shutdown()

    async def main():
        with pytest.raises(RuntimeError):
            await gw.call(lambda: 1)

    asyncio.run(main())
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_components.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'grid_engine.context'`

- [ ] **Step 3: 實作三個葉子組件**

```python
# grid_engine/context.py
"""ExchangeContext：exchange/precisions/funding_manager 共享可變容器。

兩階段初始化協定：bot __init__ 建立空 ctx 注入各組件；run()→_init_exchange 才寫入
真值。組件一律呼叫當下讀 self.ctx.<attr>，絕不在自己 __init__ 存成員快照——
否則捕獲 None → 下單全炸 / funding 同步靜默失效（spec C1）。
"""
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ExchangeContext:
    exchange: Optional[Any] = None
    precisions: Dict[str, dict] = field(default_factory=dict)
    funding_manager: Optional[Any] = None
```

```python
# grid_engine/locks.py
"""SymbolLocks：per-symbol asyncio.Lock 註冊表（全 bot 共享同一實例）。

鎖序不變式：_sync_lock → symbol lock 單向（spec 不變式 2）。
"""
import asyncio
from typing import Dict


class SymbolLocks:
    def __init__(self):
        self._locks: Dict[str, asyncio.Lock] = {}

    def get(self, ccxt_symbol: str) -> asyncio.Lock:
        return self._locks.setdefault(ccxt_symbol, asyncio.Lock())
```

```python
# grid_engine/rest_gateway.py
"""RestGateway：全部同步 ccxt REST 卸載到單 worker thread（#2 語意原樣）。

單 worker 不可改：同步 ccxt 實例非 thread-safe，多 worker 會並發打同一 Session。
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial


class RestGateway:
    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ccxt-rest")

    async def call(self, fn, *args, **kwargs):
        """在專用單 worker thread 執行同步 REST 呼叫，不阻塞 event loop"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, partial(fn, *args, **kwargs))

    def shutdown(self):
        """排隊中的 REST 直接取消；in-flight 的自然結束"""
        self._executor.shutdown(wait=False, cancel_futures=True)
```

- [ ] **Step 4: 跑新測試確認通過**

Run: `python3 -m pytest tests/test_components.py -q`
Expected: `4 passed`

- [ ] **Step 5: bot.py 接線（行為零改變的機械替換）**

在 `grid_engine/bot.py`：

5a. import 區加：

```python
from .context import ExchangeContext
from .locks import SymbolLocks
from .rest_gateway import RestGateway
```

5b. `__init__`（現行 66-116 行）改動——刪掉 `self.exchange: Optional[CustomExchange] = None`（74 行）、`self.precisions: Dict[str, dict] = {}`（85 行）、`self._rest_executor = ThreadPoolExecutor(...)`（96 行）、`self._symbol_locks: Dict[str, asyncio.Lock] = {}`（99 行）、`self.funding_manager: Optional[FundingRateManager] = None`（103 行），改為在 `self.state = GlobalState()` 之後建立：

```python
        # 共享基礎設施（兩階段初始化容器 / per-symbol 鎖註冊表 / 單 worker REST）
        self.ctx = ExchangeContext()
        self.locks = SymbolLocks()
        self.gateway = RestGateway()
```

5c. class 內加三個轉發 property（放在 `__init__` 之後、`_init_exchange` 之前）。這讓 bot 內部 `self.exchange`/`self.precisions`/`self.funding_manager` 的所有讀寫（含 `_init_exchange` 的賦值、測試的 `bot.exchange = MagicMock()`）全部透明走 ctx，**其餘方法零改動**：

```python
    @property
    def exchange(self):
        return self.ctx.exchange

    @exchange.setter
    def exchange(self, value):
        self.ctx.exchange = value

    @property
    def precisions(self):
        return self.ctx.precisions

    @precisions.setter
    def precisions(self, value):
        self.ctx.precisions = value

    @property
    def funding_manager(self):
        return self.ctx.funding_manager

    @funding_manager.setter
    def funding_manager(self, value):
        self.ctx.funding_manager = value
```

5d. 刪 `_rest`（163-166 行）與 `_symbol_lock`（168-169 行）兩個方法，機械替換全檔呼叫點：
- `self._rest(` → `self.gateway.call(`（共 10 處：191、198、231、258、396、398、434、451、985、986、1057 行附近）
- `self._symbol_lock(` → `self.locks.get(`（共 4 處：217、249、344、598 行附近）
- `run()` 的 `await loop.run_in_executor(self._rest_executor, self._init_exchange)`（1055）→ `await self.gateway.call(self._init_exchange)`；1056 行同理換 `self._check_hedge_mode`；`loop = asyncio.get_running_loop()`（1054）刪除（不再需要）
- `run()` 失敗路徑（1087）與 `stop()`（1153）的 `self._rest_executor.shutdown(wait=False, cancel_futures=True)` → `self.gateway.shutdown()`

5e. 確認替換乾淨：

Run: `grep -n "_rest_executor\|_symbol_lock\b\|self\._rest(" grid_engine/bot.py`
Expected: 無輸出

- [ ] **Step 6: 遷移 test_async_offload 的 patch 路徑**

讀 `tests/test_async_offload.py`，機械替換（斷言不改）：
- `bot._rest` → `bot.gateway.call`
- `bot._rest_executor` → `bot.gateway._executor`
- `bot._symbol_locks` → `bot.locks._locks`
- `bot._symbol_lock(` → `bot.locks.get(`

其他測試檔若 grep 到同樣引用一併換：

Run: `grep -rn "_rest_executor\|_symbol_locks\|\._rest\b" tests/ --include="*.py"`

- [ ] **Step 7: 全套回歸**

Run: `python3 -m pytest tests/ -q`
Expected: `271 passed`（267 舊 + 4 新），0 failed

- [ ] **Step 8: Commit**

```bash
git add grid_engine/context.py grid_engine/locks.py grid_engine/rest_gateway.py grid_engine/bot.py tests/test_components.py tests/test_async_offload.py
git commit -m "refactor: #7 葉子組件 ExchangeContext/SymbolLocks/RestGateway + bot 接線（兩階段 ctx property 轉發，行為零改變）"
```

---

### Task 2: OrderExecutor — 下單/撤單/斷路器

**Files:**
- Create: `grid_engine/order_executor.py`
- Modify: `grid_engine/bot.py`（刪 4 方法 + 3 狀態 + 4 常數，呼叫點改走 `self.order_executor`）
- Modify: `tests/test_order_guard.py`（import 與屬性路徑遷移）
- Test: `tests/test_components.py`（追加 is_blocked 等價測試）

**Interfaces:**
- Consumes: Task 1 的 `RestGateway.call`、`ExchangeContext`、`SymbolLocks.get`
- Produces（bot 網格鏈與 Task 3 RiskMonitor 依賴這些簽名）:
  - `OrderExecutor(gateway, ctx, state, notifier, config, locks, stop_event, tasks)`
  - `async place_order(symbol: str, side: str, price: float, quantity: float, reduce_only: bool = False, position_side: str = None, order_type: str = 'limit') -> Optional[dict]`（簽名與現行 bot.place_order 完全一致）
  - `async cancel_orders_for_side(symbol: str, position_side: str) -> None`
  - `async close_symbol_positions(ccxt_symbol: str, sym_config: SymbolConfig) -> None`（原 `_close_symbol_positions` 轉公開）
  - `is_blocked(symbol: str) -> bool`（`time.time() < self._order_block_until.get(symbol, 0)`）
  - 狀態屬性名不變：`_order_fail_counts`、`_order_block_until`、`_order_seq`（測試遷移只改前綴）
  - 常數 `ORDER_BACKOFF_BASE/ORDER_BACKOFF_CAP/ORDER_CIRCUIT_THRESHOLD/ORDER_CIRCUIT_COOLDOWN` 移到本模組；`bot.py` 以 `from .order_executor import (...)` re-export（`tests/test_order_guard.py:18` 從 bot import，避免破壞）

- [ ] **Step 1: 追加失敗測試（is_blocked 等價）**

```python
# tests/test_components.py 追加
import time
from grid_engine.order_executor import OrderExecutor


def _make_executor():
    from unittest.mock import MagicMock
    return OrderExecutor(
        gateway=MagicMock(), ctx=ExchangeContext(), state=MagicMock(),
        notifier=MagicMock(), config=MagicMock(), locks=SymbolLocks(),
        stop_event=asyncio.Event(), tasks=[],
    )


def test_is_blocked_matches_block_until():
    ex = _make_executor()
    assert ex.is_blocked("X") is False
    ex._order_block_until["X"] = time.time() + 60
    assert ex.is_blocked("X") is True
    ex._order_block_until["X"] = time.time() - 1
    assert ex.is_blocked("X") is False
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_components.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'grid_engine.order_executor'`

- [ ] **Step 3: 建 `grid_engine/order_executor.py`**

從 bot.py **逐字搬移**（body 不改邏輯，只做列出的機械替換）：常數 36-39 行、`place_order` 370-407、`_register_order_failure` 409-430、`cancel_orders_for_side` 432-453、`_close_symbol_positions` 342-368（改名 `close_symbol_positions`）。

```python
# grid_engine/order_executor.py
"""下單/撤單執行組件（#1 加固語意原樣搬移）：
clientOrderId + 指數退避 + 斷路器（僅開倉單成功重置）+ 封鎖只擋開倉。
"""
import asyncio
import time
from typing import Dict, List, Optional

from .utils import logger

# 下單失敗退避：首次封鎖秒數、指數退避上限、連續失敗斷路閾值、斷路冷卻秒數
ORDER_BACKOFF_BASE = 2.0
ORDER_BACKOFF_CAP = 60.0
ORDER_CIRCUIT_THRESHOLD = 10
ORDER_CIRCUIT_COOLDOWN = 300.0


class OrderExecutor:
    def __init__(self, gateway, ctx, state, notifier, config, locks,
                 stop_event: asyncio.Event, tasks: List[asyncio.Task]):
        self.gateway = gateway
        self.ctx = ctx          # 呼叫當下讀 ctx.exchange/ctx.precisions，絕不快照
        self.state = state
        self.notifier = notifier
        self.config = config
        self.locks = locks
        self._stop_event = stop_event
        self.tasks = tasks      # bot.tasks 共享參照：斷路通知 task 防 GC + stop 可 cancel

        # 下單失敗退避/斷路器（per symbol）
        self._order_fail_counts: Dict[str, int] = {}
        self._order_block_until: Dict[str, float] = {}
        self._order_seq = 0

    def is_blocked(self, symbol: str) -> bool:
        """封鎖期查詢（bot 網格鏈的 order_blocked 讀取點）"""
        return time.time() < self._order_block_until.get(symbol, 0)
```

方法搬移時的機械替換表（僅此四種，其他一字不動）：

| 原（bot 內） | 新（executor 內） |
|---|---|
| `self._rest(` / `self.gateway.call(`（Task 1 已換） | `self.gateway.call(` |
| `self.exchange` | `self.ctx.exchange` |
| `self.precisions` | `self.ctx.precisions` |
| `self._symbol_lock(` / `self.locks.get(`（Task 1 已換） | `self.locks.get(` |
| `self.place_order(` / `self.cancel_orders_for_side(`（close 內的自呼叫） | 不變（同 class 內） |

`place_order` 內的封鎖檢查 `if not reduce_only and time.time() < self._order_block_until.get(symbol, 0):` 改寫為 `if not reduce_only and self.is_blocked(symbol):`（語意逐字相同）。其餘含註解全部逐字保留。

- [ ] **Step 4: bot.py 移除與接線**

4a. 刪 bot 的：常數 36-39 行、`__init__` 的 `_order_fail_counts/_order_block_until/_order_seq`（91-93）、方法 `place_order`/`_register_order_failure`/`cancel_orders_for_side`/`_close_symbol_positions`。

4b. import 區加（re-export 供 test_order_guard 既有 import 路徑）：

```python
from .order_executor import (
    OrderExecutor,
    ORDER_BACKOFF_BASE, ORDER_BACKOFF_CAP,
    ORDER_CIRCUIT_THRESHOLD, ORDER_CIRCUIT_COOLDOWN,
)
```

4c. `__init__` 在 `self.notifier = ...` 之後建（依賴 notifier/tasks/_stop_event 都已存在——注意 `self.tasks`/`self._stop_event` 的建立行必須在其前）：

```python
        self.order_executor = OrderExecutor(
            gateway=self.gateway, ctx=self.ctx, state=self.state,
            notifier=self.notifier, config=self.config, locks=self.locks,
            stop_event=self._stop_event, tasks=self.tasks,
        )
```

4d. bot 內呼叫點機械替換：
- `self.place_order(` → `self.order_executor.place_order(`（`_check_and_reduce_positions` 551/555、`_grid_step` 669/683、`_execute_side_decision` 743）
- `self.cancel_orders_for_side(` → `self.order_executor.cancel_orders_for_side(`（`_check_trailing_stop` 路徑的 close 已整包搬走；剩 `_grid_step` 667/681、`_execute_side_decision` 740）
- `_check_trailing_stop` 內 `await self._close_symbol_positions(ccxt_symbol, sym_config)`（332）→ `await self.order_executor.close_symbol_positions(ccxt_symbol, sym_config)`
- `_grid_step` 的 `order_blocked = time.time() < self._order_block_until.get(ccxt_symbol, 0)`（640）→ `order_blocked = self.order_executor.is_blocked(ccxt_symbol)`

4e. **`run()` 的 tasks 重新賦值改 extend（spec I5 的隱藏殺手）**：現行 1090 行 `self.tasks = [...]` 會讓 executor 持有的舊 list 參照失效，斷路通知 task append 到孤兒 list → stop() 不會 cancel。改為：

```python
        self.tasks.extend([
            asyncio.create_task(self._websocket_loop()),
            asyncio.create_task(self._keep_alive_loop()),
        ])
```

4f. 確認乾淨：

Run: `grep -n "_order_fail_counts\|_order_block_until\|_order_seq\|_register_order_failure\|def place_order\|def cancel_orders_for_side\|_close_symbol_positions" grid_engine/bot.py`
Expected: 只剩 `order_executor.` 前綴的呼叫點與 re-export import

- [ ] **Step 5: 遷移 test_order_guard**

讀 `tests/test_order_guard.py`，機械替換（斷言不改）：
- `bot._order_block_until` → `bot.order_executor._order_block_until`
- `bot._order_fail_counts` → `bot.order_executor._order_fail_counts`
- `bot._order_seq` → `bot.order_executor._order_seq`
- `bot.place_order(` → `bot.order_executor.place_order(`（若直接呼叫）
- `bot._register_order_failure(` → `bot.order_executor._register_order_failure(`
- `bot.tasks` 相關斷言不動（executor 共享同一 list）
- 第 18 行 `from grid_engine.bot import (...)` 常數 import 不用改（4b 已 re-export）

同時 grep 其他測試檔：

Run: `grep -rn "place_order\|cancel_orders_for_side\|_order_block\|_order_fail\|_close_symbol" tests/ --include="*.py" -l`

對每個命中檔案做同樣機械替換（`test_characterization_grid.py` 若 mock `bot.place_order` 需改 mock `bot.order_executor.place_order`）。

- [ ] **Step 6: 全套回歸**

Run: `python3 -m pytest tests/ -q`
Expected: `272 passed`，0 failed

- [ ] **Step 7: Commit**

```bash
git add grid_engine/order_executor.py grid_engine/bot.py tests/test_order_guard.py tests/test_components.py
git commit -m "refactor: #7 OrderExecutor 拆出（斷路器/退避/is_blocked 原樣，tasks 改 extend 修共享參照）"
```

（若 Step 5 grep 改到其他測試檔，一併 add 明確檔名。）

---

### Task 3: RiskMonitor + DailyReporter

**Files:**
- Create: `grid_engine/risk_monitor.py`
- Create: `grid_engine/reporting.py`
- Modify: `grid_engine/bot.py`（刪 4 方法 + last_risk_alert_time，接線）
- Modify: 命中的既有測試（notifier/風控相關 patch 路徑）

**Interfaces:**
- Consumes: Task 2 `OrderExecutor.place_order` / `close_symbol_positions`
- Produces（Task 4 SyncService 依賴這些簽名）:
  - `RiskMonitor(config, state, order_executor, notifier)`
  - `async check_trailing_stop() -> None`（原 `_check_trailing_stop`）
  - `async check_and_reduce_positions(sym_config, sym_state) -> None`（原 `_check_and_reduce_positions`）
  - `async check_risk_and_notify() -> None`（原 `_check_risk_and_notify`）
  - 屬性 `last_risk_alert_time: float`
  - 常數 `RISK_ALERT_COOLDOWN = 300` 移到 `risk_monitor.py`，bot re-export
  - `DailyReporter(config, state, notifier, stop_event)`、`async run() -> None`（原 `_daily_pnl_loop`）

- [ ] **Step 1: 建兩個組件（逐字搬移）**

`grid_engine/risk_monitor.py`：搬 `RISK_ALERT_COOLDOWN`（bot.py:33）、`_check_trailing_stop`（296-340）、`_check_and_reduce_positions`（535-558）、`_check_risk_and_notify`（1037-1050），去底線改公開名。機械替換：

| 原 | 新 |
|---|---|
| `self._close_symbol_positions(` | `self.order_executor.close_symbol_positions(` |
| `self.place_order(` / `self.order_executor.place_order(`（Task 2 已換） | `self.order_executor.place_order(` |
| 其他 `self.config/self.state/self.notifier/self.last_risk_alert_time` | 不變（同名成員） |

```python
# grid_engine/risk_monitor.py
"""風控組件：保證金追蹤止盈 / 雙向減倉 / 保證金警報（含冷卻）。行為原樣搬移。"""
import asyncio
import time

from .utils import logger

# 風控警報冷卻秒數預設值（可由 config telegram_risk_alert_cooldown 覆寫）
RISK_ALERT_COOLDOWN = 300


class RiskMonitor:
    def __init__(self, config, state, order_executor, notifier):
        self.config = config
        self.state = state
        self.order_executor = order_executor
        self.notifier = notifier
        self.last_risk_alert_time = 0.0

    # async def check_trailing_stop(self): ← bot.py 296-340 逐字，僅上表替換
    # async def check_and_reduce_positions(self, sym_config, sym_state): ← 535-558 逐字
    # async def check_risk_and_notify(self): ← 1037-1050 逐字
```

`grid_engine/reporting.py`：搬 `_daily_pnl_loop`（992-1035）逐字改名 `run`，`self._stop_event` 為注入成員：

```python
# grid_engine/reporting.py
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

    # async def run(self): ← bot.py 992-1035 逐字（含 CancelledError/60s retry 分支）
```

- [ ] **Step 2: bot.py 移除與接線**

2a. 刪：`RISK_ALert_COOLDOWN` 常數（33 行，注意大小寫是 `RISK_ALERT_COOLDOWN`）、`self.last_risk_alert_time`（88）、四個方法本體。import 加 `from .risk_monitor import RiskMonitor, RISK_ALERT_COOLDOWN`、`from .reporting import DailyReporter`。

2b. `__init__` 在 `self.order_executor = ...` 之後：

```python
        self.risk_monitor = RiskMonitor(
            config=self.config, state=self.state,
            order_executor=self.order_executor, notifier=self.notifier,
        )
        self.reporter = DailyReporter(
            config=self.config, state=self.state,
            notifier=self.notifier, stop_event=self._stop_event,
        )
```

2c. 呼叫點替換：
- `_sync_account`（290/292）：`asyncio.create_task(self._check_risk_and_notify())` → `asyncio.create_task(self.risk_monitor.check_risk_and_notify())`；`await self._check_trailing_stop()` → `await self.risk_monitor.check_trailing_stop()`（fire-and-forget 不存參照，語意原樣）
- `_grid_step`（637）：`await self._check_and_reduce_positions(...)` → `await self.risk_monitor.check_and_reduce_positions(...)`
- `run()`（1095）：`asyncio.create_task(self._daily_pnl_loop())` → `asyncio.create_task(self.reporter.run())`

- [ ] **Step 3: 遷移命中測試**

Run: `grep -rn "_check_trailing_stop\|_check_and_reduce\|_check_risk_and_notify\|_daily_pnl_loop\|last_risk_alert_time\|RISK_ALERT_COOLDOWN" tests/ --include="*.py"`

機械替換（斷言不改）：`bot._check_risk_and_notify` → `bot.risk_monitor.check_risk_and_notify`、`bot.last_risk_alert_time` → `bot.risk_monitor.last_risk_alert_time`，依此類推。從 `grid_engine.bot` import `RISK_ALERT_COOLDOWN` 的不用改（re-export）。

- [ ] **Step 4: 全套回歸**

Run: `python3 -m pytest tests/ -q`
Expected: `272 passed`，0 failed

- [ ] **Step 5: Commit**

```bash
git add grid_engine/risk_monitor.py grid_engine/reporting.py grid_engine/bot.py
git commit -m "refactor: #7 RiskMonitor + DailyReporter 拆出（追蹤止盈/減倉/警報冷卻/日報原樣）"
```

（Step 3 命中的測試檔一併 add 明確檔名。）

---### Task 4: SyncService（+ maybe_sync 收編 ticker gating）+ 跨組件整合測試

**Files:**
- Create: `grid_engine/sync_service.py`
- Modify: `grid_engine/bot.py`（刪 5 方法 + `_sync_lock`/`last_sync_time`，`_handle_ticker` 改 `maybe_sync`）
- Modify: `tests/test_account_update.py`、`tests/test_async_offload.py`（路徑遷移）
- Test: `tests/test_components.py`（追加 `_sync_account` → risk 整合測試，spec M3）

**Interfaces:**
- Consumes: Task 1 gateway/ctx/locks、Task 3 `RiskMonitor.check_risk_and_notify/check_trailing_stop`
- Produces:
  - `SyncService(gateway, ctx, config, state, locks, notifier, risk_monitor)`
  - `async sync_all() -> None`（防重入語意原樣：`if self._sync_lock.locked(): return`）
  - `async maybe_sync() -> None`：

```python
    async def maybe_sync(self):
        """ticker 高頻路徑的節流同步（原 _handle_ticker 尾端 gating 收編）"""
        if time.time() - self.last_sync_time > self.config.sync_interval:
            await self.sync_all()
            self.last_sync_time = time.time()
```

  - 屬性 `_sync_lock: asyncio.Lock`（service 自建）、`last_sync_time: float = 0`

- [ ] **Step 1: 寫失敗的跨組件整合測試（M3：patch 遷移守不住的那條線）**

```python
# tests/test_components.py 追加
from unittest.mock import AsyncMock, MagicMock, patch


def test_sync_account_triggers_risk_and_trailing():
    """_sync_account 成功路徑必觸發 check_risk_and_notify(create_task) 與
    check_trailing_stop(await)——跨組件接線斷掉時全套 patch 遷移抓不到這條。"""
    from grid_engine.sync_service import SyncService
    from grid_engine.state import GlobalState

    risk = MagicMock()
    risk.check_risk_and_notify = AsyncMock()
    risk.check_trailing_stop = AsyncMock()
    notifier = MagicMock()
    notifier.enabled = True
    ctx = ExchangeContext()
    ctx.exchange = MagicMock()
    ctx.exchange.fetch_balance = MagicMock(return_value={"info": {"assets": []}, "total": {}, "free": {}})

    svc = SyncService(
        gateway=RestGateway(), ctx=ctx, config=MagicMock(), state=GlobalState(),
        locks=SymbolLocks(), notifier=notifier, risk_monitor=risk,
    )

    async def main():
        await svc._sync_account()
        await asyncio.sleep(0)   # 讓 fire-and-forget create_task 跑起來

    asyncio.run(main())
    risk.check_trailing_stop.assert_awaited_once()
    risk.check_risk_and_notify.assert_called_once()
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_components.py::test_sync_account_triggers_risk_and_trailing -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'grid_engine.sync_service'`

- [ ] **Step 3: 建 `grid_engine/sync_service.py`（逐字搬移）**

搬 `sync_all`（175-182）、`_sync_funding_rates`（184-194）、`_sync_positions`（196-222）、`_sync_orders`（224-254）、`_sync_account`（256-294）+ 新增 `maybe_sync`。機械替換：

| 原 | 新 |
|---|---|
| `self.gateway.call(`（Task 1 已換） | `self.gateway.call(` |
| `self.exchange` / `self.ctx.exchange` | `self.ctx.exchange` |
| `self.funding_manager` | `self.ctx.funding_manager` |
| `self.locks.get(`（Task 1 已換） | `self.locks.get(` |
| `self._check_risk_and_notify` / `self.risk_monitor.check_risk_and_notify`（Task 3 已換） | `self.risk_monitor.check_risk_and_notify` |
| `self._check_trailing_stop` / `self.risk_monitor.check_trailing_stop`（Task 3 已換） | `self.risk_monitor.check_trailing_stop` |

「fetch 鎖外、寫回鎖內無 await」原子區的註解與結構逐字保留（`_sync_positions` 217-222、`_sync_orders` 249-252）。

```python
# grid_engine/sync_service.py
"""REST 同步組件：持倉/掛單/帳戶/funding（#3 原子區語意原樣搬移）。

鎖序不變式：_sync_lock（本 service 持有）→ symbol lock（共享 SymbolLocks），單向。
"""
import asyncio
import time

from .utils import logger


class SyncService:
    def __init__(self, gateway, ctx, config, state, locks, notifier, risk_monitor):
        self.gateway = gateway
        self.ctx = ctx
        self.config = config
        self.state = state
        self.locks = locks
        self.notifier = notifier
        self.risk_monitor = risk_monitor
        self._sync_lock = asyncio.Lock()
        self.last_sync_time = 0

    # async def sync_all / maybe_sync / _sync_funding_rates / _sync_positions
    # / _sync_orders / _sync_account ← 逐字搬移 + 上表替換
```

- [ ] **Step 4: bot.py 移除與接線**

4a. 刪：`self.last_sync_time`（86）、`self._sync_lock`（100）、五個 sync 方法本體。import 加 `from .sync_service import SyncService`。

4b. `__init__` 在 `self.risk_monitor = ...` 之後（**建構順序硬約束：SyncService 需要 RiskMonitor 實例**）：

```python
        self.sync_service = SyncService(
            gateway=self.gateway, ctx=self.ctx, config=self.config,
            state=self.state, locks=self.locks, notifier=self.notifier,
            risk_monitor=self.risk_monitor,
        )
```

4c. 呼叫點替換：
- `_handle_ticker` 尾端（806-808）三行 gating 整段改一行：`await self.sync_service.maybe_sync()`
- `run()`（1082）：`await self.sync_all()` → `await self.sync_service.sync_all()`

4d. bot 保留 `sync_all` 委派？**不保留**——grep 呼叫者只剩上面兩處；`web/state.py`、`ui/menu.py` 若有 `bot.sync_all()` 呼叫（Task 6 全 repo grep 兜底），改 `bot.sync_service.sync_all()`。

- [ ] **Step 5: 遷移測試**

Run: `grep -rn "sync_all\|_sync_positions\|_sync_orders\|_sync_account\|_sync_funding\|_sync_lock\|last_sync_time" tests/ web/ ui/ --include="*.py" -l`

機械替換（斷言不改）：`bot._sync_account` → `bot.sync_service._sync_account`、`bot._sync_lock` → `bot.sync_service._sync_lock`、`bot.last_sync_time` → `bot.sync_service.last_sync_time`，依此類推。`tests/test_account_update.py` 是主要命中檔。

- [ ] **Step 6: 全套回歸**

Run: `python3 -m pytest tests/ -q`
Expected: `273 passed`，0 failed

- [ ] **Step 7: Commit**

```bash
git add grid_engine/sync_service.py grid_engine/bot.py tests/test_components.py tests/test_account_update.py
git commit -m "refactor: #7 SyncService 拆出（原子區/防重入原樣，maybe_sync 收編 ticker gating）+ sync→risk 跨組件整合測試"
```

---

### Task 5: WsClient（純傳輸）+ WS 例外語意 characterization

**Files:**
- Create: `grid_engine/ws_client.py`
- Create: `tests/test_ws_exception_semantics.py`
- Modify: `grid_engine/bot.py`（刪 `_websocket_loop`/`_get_listen_key`/`_keep_alive_loop`/`listen_key`，接線）

**Interfaces:**
- Consumes: Task 1 gateway/ctx
- Produces:
  - `WsClient(gateway, ctx, config, state, stop_event, handlers: Dict[str, Callable])`——handlers key 為 WS event type 字串：`'bookTicker'`、`'ACCOUNT_UPDATE'`、`'ORDER_TRADE_UPDATE'`
  - `async acquire_listen_key() -> None`（寫 `self.listen_key`）
  - `async run() -> None`（原 `_websocket_loop`）
  - `async keep_alive_loop() -> None`（原 `_keep_alive_loop`）
  - 屬性 `listen_key: Optional[str]`

- [ ] **Step 1: 先寫 characterization 測試（搬移前寫，紅在「模組不存在」）**

```python
# tests/test_ws_exception_semantics.py
"""WS 例外語意 characterization（spec I4，等價陷阱）：
- ticker handler 例外 → 冒泡到重連迴圈 → connected=False + 重連（現行為）
- account/order handler 自帶 try 吞例外 → 不觸發重連
WsClient 不得用 try 包 callback，否則 ticker 語意被改。"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from grid_engine.ws_client import WsClient
from grid_engine.context import ExchangeContext
from grid_engine.rest_gateway import RestGateway


class _FakeWs:
    """吐一則訊息後永遠 pending 的假 websocket"""
    def __init__(self, messages):
        self._messages = list(messages)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, _):
        pass

    async def recv(self):
        if self._messages:
            return self._messages.pop(0)
        await asyncio.sleep(3600)

    async def ping(self):
        pass


def _make_client(handlers, stop_event, state):
    cfg = MagicMock()
    cfg.websocket_url = "wss://x"
    cfg.symbols = {}
    return WsClient(gateway=RestGateway(), ctx=ExchangeContext(), config=cfg,
                    state=state, stop_event=stop_event, handlers=handlers)


def test_ticker_handler_exception_triggers_reconnect():
    stop = asyncio.Event()
    state = MagicMock()
    boom = AsyncMock(side_effect=RuntimeError("handler bug"))
    client = _make_client({"bookTicker": boom}, stop, state)

    msg = json.dumps({"e": "bookTicker", "s": "X", "b": "1", "a": "2"})
    connect_calls = []

    def fake_connect(*a, **kw):
        connect_calls.append(1)
        if len(connect_calls) >= 2:
            stop.set()          # 第二次連線 = 已重連，收工
        return _FakeWs([msg])

    async def main():
        with patch("grid_engine.ws_client.websockets.connect", side_effect=fake_connect), \
             patch("grid_engine.ws_client.asyncio.sleep", new=AsyncMock()):
            await asyncio.wait_for(client.run(), timeout=5)

    asyncio.run(main())
    assert boom.await_count >= 1
    assert len(connect_calls) >= 2          # 例外導致重連
    assert state.connected is False          # outer except 有把 connected 拉下來


def test_stop_event_exits_run_loop():
    stop = asyncio.Event()
    stop.set()
    client = _make_client({}, stop, MagicMock())

    async def main():
        await asyncio.wait_for(client.run(), timeout=2)   # 立即返回，不連線

    asyncio.run(main())
```

（account/order handler 吞例外屬 handler 自身 try——留在 bot 沒搬，行為由既有 `test_account_update.py` 覆蓋，不在此重複。）

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_ws_exception_semantics.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'grid_engine.ws_client'`

- [ ] **Step 3: 建 `grid_engine/ws_client.py`（逐字搬移）**

搬 `_websocket_loop`（938-978）改名 `run`、`_get_listen_key`（171-173）改名 `_fetch_listen_key`、`_keep_alive_loop`（980-990）改名 `keep_alive_loop`。**dispatch 改 handlers dict 但不包 try**：

```python
# grid_engine/ws_client.py
"""WS 純傳輸組件：連線/訂閱/重連/listenKey 續期。

例外語意（characterization 鎖定）：handler callback 例外「必須」冒泡到本迴圈
的 outer except → connected=False + sleep 5s + 重連。不得用 try 包 callback。
"""
import asyncio
import json
import ssl
from typing import Callable, Dict, Optional

import certifi
import websockets

from .utils import logger


class WsClient:
    def __init__(self, gateway, ctx, config, state, stop_event: asyncio.Event,
                 handlers: Dict[str, Callable]):
        self.gateway = gateway
        self.ctx = ctx
        self.config = config
        self.state = state
        self._stop_event = stop_event
        self.handlers = handlers
        self.listen_key: Optional[str] = None

    def _fetch_listen_key(self) -> str:
        response = self.ctx.exchange.fapiPrivatePostListenKey()
        return response.get("listenKey")

    async def acquire_listen_key(self):
        self.listen_key = await self.gateway.call(self._fetch_listen_key)

    # async def run(self): ← bot.py 938-978 逐字，僅兩處改動：
    #   1) event dispatch 的 if/elif 三分支換成：
    #        handler = self.handlers.get(event_type)
    #        if handler:
    #            await handler(data)
    #      （語意等價：未知 event type 原本也是掉出 if/elif 不處理）
    #   2) self.listen_key 讀取不變（已是本 class 成員）
    # async def keep_alive_loop(self): ← bot.py 980-990 逐字，替換：
    #   self._rest(self.exchange.fapiPrivatePutListenKey)
    #     → self.gateway.call(self.ctx.exchange.fapiPrivatePutListenKey)
    #   self.listen_key = await self._rest(self._get_listen_key)
    #     → self.listen_key = await self.gateway.call(self._fetch_listen_key)
```

- [ ] **Step 4: bot.py 移除與接線**

4a. 刪：`self.listen_key`（75）、三個方法本體、`import ssl`/`import websockets`/`import certifi`（若 bot 無其他使用——grep 確認）。import 加 `from .ws_client import WsClient`。

4b. `__init__` 最後（handlers 引用 bot 方法，bound method 沒有先後問題）：

```python
        self.ws_client = WsClient(
            gateway=self.gateway, ctx=self.ctx, config=self.config,
            state=self.state, stop_event=self._stop_event,
            handlers={
                'bookTicker': self._handle_ticker,
                'ACCOUNT_UPDATE': self._handle_account_update,
                'ORDER_TRADE_UPDATE': self._handle_order_update,
            },
        )
```

4c. `run()` 替換：
- `self.listen_key = await self._rest(self._get_listen_key)`（1057）→ `await self.ws_client.acquire_listen_key()`
- tasks（Task 2 已改 extend）：`self._websocket_loop()` → `self.ws_client.run()`、`self._keep_alive_loop()` → `self.ws_client.keep_alive_loop()`

- [ ] **Step 5: 遷移命中測試 + 全套回歸**

Run: `grep -rn "_websocket_loop\|_keep_alive\|listen_key\|_get_listen_key" tests/ --include="*.py"`

機械替換後全套：

Run: `python3 -m pytest tests/ -q`
Expected: `275 passed`，0 failed

- [ ] **Step 6: Commit**

```bash
git add grid_engine/ws_client.py grid_engine/bot.py tests/test_ws_exception_semantics.py
git commit -m "refactor: #7 WsClient 純傳輸拆出（callback 不包 try，ticker 例外→重連 characterization 鎖定）"
```

---

### Task 6: bot.py 收尾 — 組裝斷言、全 repo 呼叫者掃描、行數確認

**Files:**
- Modify: `grid_engine/bot.py`（殘留清理）
- Modify: `grid_engine/__init__.py`（若 export 清單需補新組件——先讀再決定）
- Modify: 命中的 `web/`、`ui/`、`as_terminal_max.py`（外部呼叫者路徑遷移）
- Test: `tests/test_components.py`（追加組裝斷言）

**Interfaces:**
- Consumes: Task 1-5 全部組件
- Produces: 最終 MaxGridBot 組合根（`__init__` 組裝順序：ctx/locks/gateway → notifier → order_executor → risk_monitor/reporter → sync_service → ws_client）

- [ ] **Step 1: 寫組裝斷言測試（spec 風險表的兩條防線）**

```python
# tests/test_components.py 追加
def _make_bot():
    from grid_engine.bot import MaxGridBot
    from grid_engine.config import GlobalConfig
    return MaxGridBot(GlobalConfig())


def test_bot_wiring_shares_single_instances():
    """gateway/locks/ctx/stop_event 必須全組件同一實例——
    複製成兩份 = ccxt 並發打非 thread-safe Session / 原子區失效 / 停機失效"""
    bot = _make_bot()
    assert bot.order_executor.gateway is bot.gateway
    assert bot.sync_service.gateway is bot.gateway
    assert bot.ws_client.gateway is bot.gateway
    assert bot.order_executor.locks is bot.locks
    assert bot.sync_service.locks is bot.locks
    assert bot.order_executor.ctx is bot.ctx
    assert bot.sync_service.ctx is bot.ctx
    assert bot.ws_client.ctx is bot.ctx
    assert bot.order_executor._stop_event is bot._stop_event
    assert bot.ws_client._stop_event is bot._stop_event
    assert bot.reporter._stop_event is bot._stop_event
    assert bot.order_executor.tasks is bot.tasks
    assert bot.sync_service.risk_monitor is bot.risk_monitor
    assert bot.risk_monitor.order_executor is bot.order_executor


def test_bot_two_phase_init_propagates_to_components():
    """_init_exchange 後組件讀到真 exchange/funding_manager（防 None 快照，spec C1）"""
    from unittest.mock import MagicMock
    bot = _make_bot()
    assert bot.order_executor.ctx.exchange is None
    bot.exchange = MagicMock()            # 走 property → ctx
    bot.funding_manager = MagicMock()
    assert bot.order_executor.ctx.exchange is bot.exchange
    assert bot.sync_service.ctx.funding_manager is bot.funding_manager
```

- [ ] **Step 2: 跑測試（應直接綠——前面 task 接線正確的驗證；若紅表示組裝有漏，修 bot 不修測試）**

Run: `python3 -m pytest tests/test_components.py -q`
Expected: PASS

- [ ] **Step 3: 全 repo 外部呼叫者掃描與遷移**

Run: `grep -rn "\.sync_all\|\.place_order\|\.cancel_orders_for_side\|\.last_sync_time\|\.listen_key\|_rest_executor\|_symbol_lock\|_sync_lock\|_order_block_until\|_daily_pnl_loop\|_check_trailing_stop" web/ ui/ as_terminal_max.py grid_engine/ --include="*.py" | grep -v "order_executor\.\|sync_service\.\|ws_client\.\|risk_monitor\.\|reporter\."`

命中的外部呼叫者（web/state.py、ui/menu.py 等）改走新組件路徑；`grid_engine/backtest.py`、`grid_engine/__init__.py` 若引用舊名一併處理。**改動前先讀該檔上下文，確認呼叫語意。**

- [ ] **Step 4: bot.py 殘留清理與行數確認**

- 刪除不再使用的 import（`ThreadPoolExecutor`、`partial`、`ssl`、`certifi`、`websockets` 等——grep 逐一確認 bot 內已無使用）
- `__init__` docstring/註解更新為組裝順序說明

Run: `wc -l grid_engine/bot.py grid_engine/order_executor.py grid_engine/sync_service.py grid_engine/ws_client.py grid_engine/risk_monitor.py grid_engine/reporting.py`
Expected: bot.py ≈ 450-550 行（生命週期+網格鏈+handlers+組裝）

- [ ] **Step 5: 全套回歸 + Commit**

Run: `python3 -m pytest tests/ -q`
Expected: `277 passed`，0 failed

```bash
git add grid_engine/bot.py tests/test_components.py
git commit -m "refactor: #7 bot.py 收尾 — 組裝斷言（單例/兩階段）+ 外部呼叫者遷移 + 殘留清理"
```

（Step 3 命中檔案一併 add 明確檔名。）

---

### Task 7: Monkey testing + 最終驗證

**Files:**
- Test: `tests/test_components.py`（追加共享鎖競態 monkey）

**Interfaces:**
- Consumes: 全部組件

- [ ] **Step 1: 補「組件間共享鎖競態」monkey 測試**

```python
# tests/test_components.py 追加
def test_monkey_cross_component_lock_contention():
    """SyncService 原子區與 bot 網格鏈（adjust_grid skip-if-locked）搶同一把
    symbol lock：50 並發下鎖內臨界區不得交錯（#3 語意跨組件仍成立）"""
    locks = SymbolLocks()
    sym = "BNB/USDC:USDC"
    in_critical = []
    violations = []

    async def worker(i):
        lock = locks.get(sym)
        if i % 3 == 0 and lock.locked():
            return                      # 模擬 adjust_grid 的 skip-if-locked
        async with lock:
            in_critical.append(i)
            if len(in_critical) > 1:
                violations.append(tuple(in_critical))
            await asyncio.sleep(0)      # 讓出 event loop，製造交錯機會
            in_critical.remove(i)

    async def main():
        await asyncio.gather(*[worker(i) for i in range(50)])

    asyncio.run(main())
    assert violations == []
```

- [ ] **Step 2: 既有 monkey 全套重跑（並發風暴/REST 例外風暴/停機競態已在遷移後的既有測試裡）**

Run: `python3 -m pytest tests/ -q`
Expected: `278 passed`，0 failed

- [ ] **Step 3: 決策日誌等價抽查（#4 契約未破的直接證據）**

Run: `python3 -c "from grid_engine.replay import replay_file; import sys; sys.exit(0)"`（模組可載入）
另跑既有 replay/characterization 測試明確範圍：

Run: `python3 -m pytest tests/test_characterization_grid.py tests/test_decision_log.py -q`
Expected: 全綠

- [ ] **Step 4: Commit**

```bash
git add tests/test_components.py
git commit -m "test: #7 monkey — 跨組件共享鎖競態 50 並發 + 全套回歸"
```

---

## 完成後（不在本計畫內，由主對話執行）

1. dual-review 兩輪（內部 reviewer + fresh-context 外部輪）
2. verifier fresh-context 驗收（重讀檔 + 實跑全套）
3. 更新 `tasks/progress.md`
4. 部署後 #4 Task 10 的 24h replay zero-diff 同時驗收本次拆分
