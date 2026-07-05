# #2 REST 卸載 + #3 並發鎖 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 MaxGridBot 所有同步 ccxt REST 呼叫卸載到單 worker thread executor（解除 event loop 阻塞），並以 per-symbol lock + sync 防重入鎖保護共享狀態。

**Architecture:** 專用 `ThreadPoolExecutor(max_workers=1)` 序列化所有 REST（ccxt 同步實例非 thread-safe）；下單/撤單/同步方法鏈全轉 async；`adjust_grid` 以 per-symbol `asyncio.Lock` skip-if-locked 保護，REST sync 遵守「fetch 在 thread、apply 為無 await 原子區塊且持 symbol lock」規則。鎖序固定 `_sync_lock → symbol lock`，反向禁止。

**Tech Stack:** Python 3.13, asyncio, concurrent.futures, pytest + pytest-asyncio（已在用）。

**Spec:** `docs/superpowers/specs/2026-07-03-async-offload-and-locks-design.md`

## Global Constraints

- REST executor 必須 `max_workers=1`（ccxt Session 非 thread-safe）。
- WS handlers（`_handle_account_update`/`_handle_order_update` 的狀態變異區）**不加鎖、不加 await**。
- 原子 apply：REST 結果寫回 `SymbolState`/`AccountBalance` 的區塊內禁止任何 `await`。
- 鎖序：`_sync_lock` → `_symbol_locks[s]`；只拿 symbol lock 的路徑不得再拿 `_sync_lock`。
- 範圍外：`exchanges/`、`core/`、回測、web、UI（#9 淘汰）。
- git 只 add 明確指定的檔案，禁止 `git add -A` / `git add .`。
- 每個 task 結束跑全套：`uv run pytest tests/ -q`，必須全綠才 commit。

## 呼叫鏈全景（實作者必讀）

轉 async 的方法與其呼叫點（行號為改動前 bot.py）：

| 方法 | 呼叫點（需補 await） |
|---|---|
| `place_order` (343) | adjust_grid 643/659、_place_grid 713/715/733/734/737/738、_close_symbol_positions 327/334、_check_and_reduce_positions 550/554 |
| `cancel_orders_for_side` (402) | adjust_grid 641/657、_place_grid 725、_close_symbol_positions 323/324 |
| `_check_and_reduce_positions` (534) | adjust_grid 632 |
| `_close_symbol_positions` (316) | _check_trailing_stop 306 |
| `_check_trailing_stop` (270) | _sync_account 266 |
| `sync_all` (153) + `_sync_positions/_sync_orders/_sync_account/_sync_funding_rates` | _handle_ticker 766、run() 1019 |

`_register_order_failure`、`_should_adjust_grid`、`_grid_cooldown_passed`、`_get_dynamic_spacing` 保持同步（無 IO）。外部（UI/web）無人直接呼叫上述方法（已 grep 確認），僅 tests/ 需同步調整。

---

### Task 1: REST executor 與 `_rest` helper

**Files:**
- Modify: `grid_engine/bot.py`（imports、`__init__`、`stop`）
- Test: `tests/test_async_offload.py`（新建）

**Interfaces:**
- Produces: `async def _rest(self, fn, *args, **kwargs)` — 在 `self._rest_executor`（單 worker）執行 `fn(*args, **kwargs)` 並回傳結果；例外原樣穿透到 await 點。後續所有 task 依賴它。

- [ ] **Step 1: 寫失敗測試**

```python
"""#2/#3 並發安全測試

核心契約 (2026-07-03 架構審查修復 #2+#3):
  - 所有 ccxt REST 走單 worker executor：不阻塞 event loop、天然序列化。
  - adjust_grid per-symbol 互斥，忙碌時 skip 不排隊。
  - sync_all 防重入；REST apply 為原子區塊。
  - 停機後不再送單，executor 收斂。
"""

import asyncio
import threading
import time as _time

import pytest
from unittest.mock import MagicMock

from grid_engine.bot import MaxGridBot
from grid_engine.config import GlobalConfig


def _make_bot():
    bot = MaxGridBot(GlobalConfig())
    bot.exchange = MagicMock()
    return bot


class TestRestHelper:
    @pytest.mark.asyncio
    async def test_rest_runs_fn_and_returns_result(self):
        bot = _make_bot()
        assert await bot._rest(lambda a, b=0: a + b, 1, b=2) == 3

    @pytest.mark.asyncio
    async def test_rest_does_not_block_event_loop(self):
        """REST 慢呼叫期間 event loop 心跳必須照跳。"""
        bot = _make_bot()
        beats = []

        async def heartbeat():
            for _ in range(4):
                beats.append(_time.monotonic())
                await asyncio.sleep(0.05)

        hb = asyncio.create_task(heartbeat())
        await bot._rest(_time.sleep, 0.3)
        await hb
        gaps = [b - a for a, b in zip(beats, beats[1:])]
        assert max(gaps) < 0.2, f"event loop 被卡住: gaps={gaps}"

    @pytest.mark.asyncio
    async def test_rest_serializes_concurrent_calls(self):
        """單 worker：同一時刻最多 1 個 REST 在跑。"""
        bot = _make_bot()
        active, peak = [0], [0]
        lk = threading.Lock()

        def work():
            with lk:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            _time.sleep(0.05)
            with lk:
                active[0] -= 1

        await asyncio.gather(*[bot._rest(work) for _ in range(5)])
        assert peak[0] == 1

    @pytest.mark.asyncio
    async def test_rest_propagates_exception(self):
        bot = _make_bot()

        def boom():
            raise ValueError("x")

        with pytest.raises(ValueError):
            await bot._rest(boom)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_async_offload.py -v`
Expected: 4 FAIL/ERROR，`AttributeError: ... no attribute '_rest'`

- [ ] **Step 3: 實作**

`grid_engine/bot.py` 檔頭 imports 補：

```python
from concurrent.futures import ThreadPoolExecutor
from functools import partial
```

`__init__`（`self._order_seq = 0` 之後）加：

```python
# REST 卸載：單 worker 序列化（同步 ccxt 實例非 thread-safe）
self._rest_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ccxt-rest")
```

`_get_listen_key` 定義之前加方法：

```python
async def _rest(self, fn, *args, **kwargs):
    """在專用單 worker thread 執行同步 REST 呼叫，不阻塞 event loop"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(self._rest_executor, partial(fn, *args, **kwargs))
```

`stop()` 末尾（task cancel 迴圈之後）加：

```python
# 排隊中的 REST 直接取消；in-flight 的自然結束，place_order 入口的停機檢查擋住後續
self._rest_executor.shutdown(wait=False, cancel_futures=True)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_async_offload.py -v` → 4 PASS；`uv run pytest tests/ -q` → 全綠（113）。

- [ ] **Step 5: Commit**

```bash
git add grid_engine/bot.py tests/test_async_offload.py
git commit -m "feat: REST 卸載基礎 — 單 worker executor + _rest helper"
```

---

### Task 2: 下單/撤單鏈轉 async

**Files:**
- Modify: `grid_engine/bot.py`（`place_order`、`cancel_orders_for_side`、`_check_and_reduce_positions`、`_close_symbol_positions`、`adjust_grid`、`_place_grid`）
- Modify: `tests/test_order_guard.py`（呼叫點補 await）
- Test: `tests/test_async_offload.py`

**Interfaces:**
- Consumes: Task 1 的 `self._rest`。
- Produces: `async def place_order(...)`（簽名參數不變）、`async def cancel_orders_for_side(symbol, position_side)`、`async def _check_and_reduce_positions(sym_config, sym_state)`、`async def _close_symbol_positions(ccxt_symbol, sym_config)`。

- [ ] **Step 1: 寫失敗測試**（加入 `tests/test_async_offload.py`）

```python
class TestAsyncOrderPath:
    @pytest.mark.asyncio
    async def test_place_order_goes_through_executor(self):
        """create_order 必須在 executor thread 執行，不在 event loop thread。"""
        bot = _make_bot()
        seen = {}

        def fake_create(*a, **kw):
            seen["thread"] = threading.current_thread().name
            return {"id": "1"}

        bot.exchange.create_order = fake_create
        bot.precisions["BNB/USDC:USDC"] = {"price": 2, "amount": 1, "min_amount": 0.1}
        result = await bot.place_order("BNB/USDC:USDC", "buy", 600.0, 1.0)
        assert result == {"id": "1"}
        assert seen["thread"].startswith("ccxt-rest")

    @pytest.mark.asyncio
    async def test_place_order_skipped_after_stop(self):
        """停機後 place_order 直接 return None，不打 exchange。"""
        bot = _make_bot()
        bot._stop_event.set()
        assert await bot.place_order("BNB/USDC:USDC", "buy", 600.0, 1.0) is None
        bot.exchange.create_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_order_failure_still_backs_off(self):
        """回歸：executor 內拋例外 → 退避計數照常累加。"""
        bot = _make_bot()
        bot.exchange.create_order = MagicMock(side_effect=RuntimeError("boom"))
        assert await bot.place_order("BNB/USDC:USDC", "buy", 600.0, 1.0) is None
        assert bot._order_fail_counts["BNB/USDC:USDC"] == 1
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_async_offload.py -v`
Expected: 新 3 測 FAIL（`await bot.place_order` 對同步方法報 TypeError 或 thread 斷言失敗）。

- [ ] **Step 3: 實作**

`place_order`：`def` → `async def`；入口加停機檢查；兩處 `self.exchange.create_order(...)` 改 `await self._rest(self.exchange.create_order, ...)`：

```python
    async def place_order(self, symbol: str, side: str, price: float, quantity: float,
                          reduce_only: bool = False, position_side: str = None,
                          order_type: str = 'limit'):
        # 停機後不再送單（executor 已排入的由 shutdown(cancel_futures) 收掉）
        if self._stop_event.is_set():
            return None
        # 退避封鎖只擋開倉單；reduce_only（止盈/平倉）永遠放行
        if not reduce_only and time.time() < self._order_block_until.get(symbol, 0):
            return None
```

（try 區塊內）

```python
            if order_type == 'market':
                result = await self._rest(self.exchange.create_order, symbol, 'market', side, quantity, params=params)
            else:
                result = await self._rest(self.exchange.create_order, symbol, 'limit', side, quantity, price, params=params)
```

`cancel_orders_for_side`：`def` → `async def`；`orders = self.exchange.fetch_open_orders(symbol)` → `orders = await self._rest(self.exchange.fetch_open_orders, symbol)`；`self.exchange.cancel_order(order['id'], symbol)` → `await self._rest(self.exchange.cancel_order, order['id'], symbol)`。

`_check_and_reduce_positions`：`def` → `async def`；550/554 兩處 `self.place_order(...)` → `await self.place_order(...)`。

`_close_symbol_positions`：`def` → `async def`；323/324 `self.cancel_orders_for_side(...)` → `await ...`；327/334 `self.place_order(...)` → `await ...`。

`adjust_grid`：632 `self._check_and_reduce_positions(...)` → `await self._check_and_reduce_positions(...)`；641/657 `self.cancel_orders_for_side(...)` → `await ...`；643/659 `self.place_order(...)` → `await ...`。

`_place_grid`：713/715/733/734/737/738 `self.place_order(...)` → `await ...`；725 `self.cancel_orders_for_side(...)` → `await ...`。

**注意（過渡狀態處理）**：`_close_symbol_positions` 轉 async 後，其呼叫者 `_check_trailing_stop:306` 的裸呼叫會變成 un-awaited coroutine。本 task 一併處理：`_check_trailing_stop` 轉 `async def`、306 行補 `await`；而 `_check_trailing_stop` 的呼叫者 `_sync_account:266`（本 task 仍是同步方法）暫改為 `asyncio.create_task(self._check_trailing_stop())`，Task 3 把 `_sync_account` 轉 async 時再轉正為 `await self._check_trailing_stop()`。這保證本 task 的 commit 點行為完整、無 un-awaited coroutine warning。

`tests/test_order_guard.py`：所有直接呼叫 `bot.place_order(...)` / `bot.cancel_orders_for_side(...)` 的測試函數改 async：加 `@pytest.mark.asyncio`、`def` → `async def`、呼叫前加 `await`。涉及行（改動前）：66-67、81、93-94、102、105、116、124、136、143、158、166、182、184、193、200、296、304、312、315。同步斷言（`_order_fail_counts` 等）不變。`bot.cancel_orders_for_side = MagicMock()`（215）改 `AsyncMock()`。

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/ -q` → 全綠（116）。

- [ ] **Step 5: Commit**

```bash
git add grid_engine/bot.py tests/test_order_guard.py tests/test_async_offload.py
git commit -m "feat: 下單/撤單/減倉/平倉鏈轉 async — REST 走 executor"
```

---

### Task 3: sync 系列與啟動/keepalive 轉 async

**Files:**
- Modify: `grid_engine/bot.py`（`sync_all`、`_sync_positions`、`_sync_orders`、`_sync_account`、`_sync_funding_rates`、`_check_trailing_stop` 呼叫轉正、`_handle_ticker`、`run`、`_keep_alive_loop`）
- Modify: `tests/test_account_update.py`（`_sync_account` 呼叫點補 await）
- Test: `tests/test_async_offload.py`

**Interfaces:**
- Consumes: Task 1 `_rest`、Task 2 async 化的 `_check_trailing_stop`。
- Produces: `async def sync_all()` 及四個 async `_sync_*`；「fetch → 原子 apply」內部結構（Task 4 在 apply 區段套 symbol lock）。

- [ ] **Step 1: 寫失敗測試**（加入 `tests/test_async_offload.py`）

```python
def _make_synced_bot():
    from grid_engine.config import SymbolConfig
    cfg = GlobalConfig()
    sym_cfg = SymbolConfig(symbol="BNBUSDC")
    sym_cfg.enabled = True
    cfg.symbols["BNBUSDC"] = sym_cfg
    bot = MaxGridBot(cfg)
    bot.exchange = MagicMock()
    bot.exchange.fetch_positions.return_value = []
    bot.exchange.fetch_open_orders.return_value = []
    bot.exchange.fetch_balance.return_value = {"info": {"assets": []}, "total": {}, "free": {}}
    bot.funding_manager = None
    return bot


class TestAsyncSync:
    @pytest.mark.asyncio
    async def test_sync_all_offloads_fetches(self):
        bot = _make_synced_bot()
        threads = set()

        def rec(*a, **kw):
            threads.add(threading.current_thread().name)
            return []

        bot.exchange.fetch_positions = rec
        bot.exchange.fetch_open_orders = rec
        bot.exchange.fetch_balance = MagicMock(
            side_effect=lambda *a, **kw: (threads.add(threading.current_thread().name),
                                          {"info": {"assets": []}, "total": {}, "free": {}})[1])
        await bot.sync_all()
        assert threads and all(t.startswith("ccxt-rest") for t in threads)

    @pytest.mark.asyncio
    async def test_sync_orders_applies_counts(self):
        """回歸：async 化後掛單計數彙總邏輯不變。"""
        bot = _make_synced_bot()
        sym = list(bot.state.symbols)[0]
        bot.exchange.fetch_open_orders.return_value = [
            {"side": "buy", "info": {"origQty": "2", "positionSide": "LONG"}},
            {"side": "sell", "info": {"origQty": "3", "positionSide": "LONG"}},
        ]
        await bot._sync_orders()
        st = bot.state.symbols[sym]
        assert st.buy_long_orders == 2 and st.sell_long_orders == 3
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_async_offload.py -v` → 新 2 測 FAIL。

- [ ] **Step 3: 實作**

`sync_all`：

```python
    async def sync_all(self):
        await self._sync_positions()
        await self._sync_orders()
        await self._sync_account()
        await self._sync_funding_rates()
```

`_sync_positions`：fetch 卸載 + 先彙總再一次寫回（為 Task 4 的原子 apply 鋪路）：

```python
    async def _sync_positions(self):
        try:
            positions = await self._rest(self.exchange.fetch_positions, params={'type': 'future'})
        except Exception as e:
            logger.error(f"同步持倉失敗: {e}")
            return

        agg = {s: [0.0, 0.0, 0.0] for s in self.state.symbols}  # long, short, upnl
        for pos in positions:
            symbol = pos['symbol']
            if symbol in agg:
                contracts = pos.get('contracts', 0)
                side = pos.get('side')
                pnl = float(pos.get('unrealizedPnl', 0) or 0)
                if side == 'long':
                    agg[symbol][0] = contracts
                elif side == 'short':
                    agg[symbol][1] = abs(contracts)
                agg[symbol][2] += pnl

        for symbol, (long_pos, short_pos, upnl) in agg.items():
            # 原子 apply：無 await（Task 4 在此加 symbol lock）
            st = self.state.symbols[symbol]
            st.long_position = long_pos
            st.short_position = short_pos
            st.unrealized_pnl = upnl
```

`_sync_orders`：外層迴圈保留，`orders = self.exchange.fetch_open_orders(symbol=symbol)` → `orders = await self._rest(self.exchange.fetch_open_orders, symbol=symbol)`，計數彙總改為先算局部變數、最後一次寫回 state（apply 區塊無 await）：

```python
                counts = [0.0, 0.0, 0.0, 0.0]  # buy_long, sell_long, buy_short, sell_short
                for order in orders:
                    qty = abs(float(order.get('info', {}).get('origQty', 0)))
                    side = order.get('side')
                    pos_side = order.get('info', {}).get('positionSide')
                    if side == 'buy' and pos_side == 'LONG':
                        counts[0] += qty
                    elif side == 'sell' and pos_side == 'LONG':
                        counts[1] += qty
                    elif side == 'buy' and pos_side == 'SHORT':
                        counts[2] += qty
                    elif side == 'sell' and pos_side == 'SHORT':
                        counts[3] += qty
                # 原子 apply（Task 4 在此加 symbol lock）
                state.buy_long_orders, state.sell_long_orders, \
                    state.buy_short_orders, state.sell_short_orders = counts
```

`_sync_account`：`async def`；`balance = self.exchange.fetch_balance({'type': 'future'})` → `balance = await self._rest(self.exchange.fetch_balance, {'type': 'future'})`；`self._check_trailing_stop()`（Task 2 暫为 create_task 者）轉正 → `await self._check_trailing_stop()`。其餘 apply 邏輯不動（本來就無 await）。

`_sync_funding_rates`：`async def`；`rate = self.funding_manager.update_funding_rate(...)` → `rate = await self._rest(self.funding_manager.update_funding_rate, sym_config.ccxt_symbol)`。

`_handle_ticker` 766：`self.sync_all()` → `await self.sync_all()`。

`run()` 1012-1019：

```python
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(self._rest_executor, self._init_exchange)
            await loop.run_in_executor(self._rest_executor, self._check_hedge_mode)
            self.listen_key = await self._rest(self._get_listen_key)

            self.state.running = True
            self.state.start_time = datetime.now()

            await self.sync_all()
```

`_keep_alive_loop` 943-944：

```python
                    await self._rest(self.exchange.fapiPrivatePutListenKey)
                    self.listen_key = await self._rest(self._get_listen_key)
```

`tests/test_account_update.py`：呼叫 `bot._sync_account()` 的測試改 async + await（`grep -n "_sync_account()" tests/test_account_update.py` 定位，改法同 Task 2 的 test_order_guard 模式）。

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/ -q` → 全綠（118）。

- [ ] **Step 5: Commit**

```bash
git add grid_engine/bot.py tests/test_account_update.py tests/test_async_offload.py
git commit -m "feat: sync/啟動/keepalive REST 全數卸載至 executor"
```

---

### Task 4: 並發鎖 — symbol lock + sync 防重入

**Files:**
- Modify: `grid_engine/bot.py`（`__init__`、`adjust_grid`、`sync_all`、`_sync_positions`、`_sync_orders`、`_close_symbol_positions`）
- Test: `tests/test_async_offload.py`

**Interfaces:**
- Consumes: Task 3 的「fetch → 原子 apply」結構。
- Produces: `self._symbol_locks: Dict[str, asyncio.Lock]`、`self._sync_lock: asyncio.Lock`、`def _symbol_lock(self, symbol) -> asyncio.Lock`。

- [ ] **Step 1: 寫失敗測試**（加入 `tests/test_async_offload.py`）

```python
class TestConcurrencyLocks:
    @pytest.mark.asyncio
    async def test_adjust_grid_skips_when_locked(self):
        """同 symbol 並發觸發：一個在跑，其餘直接 skip 不排隊。"""
        bot = _make_synced_bot()
        sym = list(bot.state.symbols)[0]
        st = bot.state.symbols[sym]
        st.latest_price = st.best_bid = st.best_ask = 600.0
        entered = []

        async def slow_step(ccxt_symbol, sym_config):
            entered.append(ccxt_symbol)
            await asyncio.sleep(0.1)

        bot._grid_step = slow_step
        await asyncio.gather(*[bot.adjust_grid(sym) for _ in range(5)])
        assert len(entered) == 1, "adjust_grid 未互斥或發生排隊"

    @pytest.mark.asyncio
    async def test_adjust_grid_lock_released_on_exception(self):
        bot = _make_synced_bot()
        sym = list(bot.state.symbols)[0]
        st = bot.state.symbols[sym]
        st.latest_price = st.best_bid = st.best_ask = 600.0

        async def boom(ccxt_symbol, sym_config):
            raise RuntimeError("x")

        bot._grid_step = boom
        with pytest.raises(RuntimeError):
            await bot.adjust_grid(sym)
        assert not bot._symbol_lock(sym).locked()

    @pytest.mark.asyncio
    async def test_sync_all_no_reentry(self):
        """sync 進行中再觸發 → 直接 return，fetch 只跑一輪。"""
        bot = _make_synced_bot()
        calls = []

        def slow_fetch(*a, **kw):
            calls.append(1)
            _time.sleep(0.1)
            return []

        bot.exchange.fetch_positions = slow_fetch
        await asyncio.gather(bot.sync_all(), bot.sync_all(), bot.sync_all())
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_sync_apply_waits_for_adjust_lock(self):
        """adjust_grid 持鎖期間，_sync_orders 的 apply 不得插入。"""
        bot = _make_synced_bot()
        sym = list(bot.state.symbols)[0]
        st = bot.state.symbols[sym]
        st.buy_long_orders = 99  # adjust 期間的「決策依據」
        bot.exchange.fetch_open_orders.return_value = []

        lock = bot._symbol_lock(sym)
        async with lock:  # 模擬 adjust_grid 持鎖中
            sync_task = asyncio.create_task(bot._sync_orders())
            await asyncio.sleep(0.05)
            assert st.buy_long_orders == 99, "sync 在 adjust 持鎖期間改寫了掛單計數"
        await sync_task
        assert st.buy_long_orders == 0  # 釋放後才 apply
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_async_offload.py -v` → 新 4 測 FAIL（無 `_symbol_lock`/`_grid_step`）。

- [ ] **Step 3: 實作**

`__init__`（executor 之後）：

```python
# 並發鎖：per-symbol 網格互斥 + sync 防重入（鎖序固定 _sync_lock → symbol lock）
self._symbol_locks: Dict[str, asyncio.Lock] = {}
self._sync_lock = asyncio.Lock()
```

helper（`_rest` 之後）：

```python
def _symbol_lock(self, ccxt_symbol: str) -> asyncio.Lock:
    return self._symbol_locks.setdefault(ccxt_symbol, asyncio.Lock())
```

`adjust_grid` 拆成 wrapper + `_grid_step`：`adjust_grid` 保留開頭 sym_config 解析（587-599 的 resolve + `sym_state`/price 檢查前半），body 改：

```python
    async def adjust_grid(self, ccxt_symbol: str):
        sym_config = None
        for cfg in self.config.symbols.values():
            if cfg.ccxt_symbol == ccxt_symbol and cfg.enabled:
                sym_config = cfg
                break
        if sym_config is None:
            return

        # 忙碌時跳過本 tick（ticker 高頻，排隊只會積壓過期決策）
        lock = self._symbol_lock(ccxt_symbol)
        if lock.locked():
            return
        async with lock:
            await self._grid_step(ccxt_symbol, sym_config)

    async def _grid_step(self, ccxt_symbol: str, sym_config: SymbolConfig):
        sym_state = self.state.symbols[ccxt_symbol]
        price = sym_state.latest_price
        if price <= 0:
            return
        # …原 adjust_grid 其餘 body 原封搬入（DGT/Bandit/減倉/多頭/空頭分支）…
```

（搬移時不改任何邏輯，只縮排搬家；原 body 中 `sym_state = self.state.symbols[ccxt_symbol]` 等取值保留。）

`sync_all` 加防重入 + 鎖：

```python
    async def sync_all(self):
        if self._sync_lock.locked():
            return
        async with self._sync_lock:
            await self._sync_positions()
            await self._sync_orders()
            await self._sync_account()
            await self._sync_funding_rates()
```

`_sync_positions` 的 apply 迴圈加鎖：

```python
        for symbol, (long_pos, short_pos, upnl) in agg.items():
            async with self._symbol_lock(symbol):
                # 原子 apply：鎖內無其他 await
                st = self.state.symbols[symbol]
                st.long_position = long_pos
                st.short_position = short_pos
                st.unrealized_pnl = upnl
```

`_sync_orders` 的 apply 區塊加鎖（fetch 在鎖外）：

```python
                async with self._symbol_lock(symbol):
                    state.buy_long_orders, state.sell_long_orders, \
                        state.buy_short_orders, state.sell_short_orders = counts
```

`_close_symbol_positions` 全 body 包鎖（呼叫方 `_check_trailing_stop` 在 sync 路徑上，鎖序 `_sync_lock → symbol` 合法）：

```python
    async def _close_symbol_positions(self, ccxt_symbol: str, sym_config: SymbolConfig):
        async with self._symbol_lock(ccxt_symbol):
            try:
                # …原 body 原封搬入（cancel×2 + 平多 + 平空）…
```

**死鎖自查**：`_close_symbol_positions` 只被 `_check_trailing_stop`（持 `_sync_lock`）呼叫 → sync→symbol ✓；`adjust_grid` 持 symbol lock 期間呼叫鏈（`_grid_step`→`_check_and_reduce_positions`/`_place_grid`）不得碰 `_sync_lock` 或再取 symbol lock — `_place_grid`/`_check_and_reduce_positions` 內不得呼叫 `_close_symbol_positions`（現況即如此，不要改出來）。

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/ -q` → 全綠（122）。

- [ ] **Step 5: Commit**

```bash
git add grid_engine/bot.py tests/test_async_offload.py
git commit -m "feat: 並發鎖 — adjust_grid per-symbol 互斥(skip)、sync 防重入、原子 apply 上鎖"
```

---

### Task 5: Monkey testing + 全回歸

**Files:**
- Test: `tests/test_async_offload.py`

**Interfaces:**
- Consumes: Task 1-4 全部。

- [ ] **Step 1: 寫 monkey 測試**（加入 `tests/test_async_offload.py`）

```python
class TestMonkey:
    @pytest.mark.asyncio
    async def test_ticker_storm_50_concurrent(self):
        """50 個並發 adjust_grid + 3 個 sync_all 同時轟：不死鎖、不炸例外。"""
        bot = _make_synced_bot()
        sym = list(bot.state.symbols)[0]
        st = bot.state.symbols[sym]
        st.latest_price = st.best_bid = st.best_ask = 600.0
        bot.exchange.create_order.return_value = {"id": "1"}
        await asyncio.wait_for(
            asyncio.gather(
                *[bot.adjust_grid(sym) for _ in range(50)],
                bot.sync_all(), bot.sync_all(), bot.sync_all(),
            ),
            timeout=10,
        )
        assert not bot._symbol_lock(sym).locked()
        assert not bot._sync_lock.locked()

    @pytest.mark.asyncio
    async def test_rest_exception_storm_releases_all_locks(self):
        """每個 REST 都炸：鎖不得洩漏，之後仍可正常進入。"""
        bot = _make_synced_bot()
        sym = list(bot.state.symbols)[0]
        st = bot.state.symbols[sym]
        st.latest_price = st.best_bid = st.best_ask = 600.0
        for m in ("fetch_positions", "fetch_open_orders", "fetch_balance",
                  "create_order", "cancel_order"):
            setattr(bot.exchange, m, MagicMock(side_effect=RuntimeError("boom")))
        await asyncio.gather(
            *[bot.adjust_grid(sym) for _ in range(10)],
            bot.sync_all(),
            return_exceptions=True,
        )
        assert not bot._symbol_lock(sym).locked()
        assert not bot._sync_lock.locked()
        # 復原後可再進入
        bot.exchange.fetch_positions = MagicMock(return_value=[])
        bot.exchange.fetch_open_orders = MagicMock(return_value=[])
        bot.exchange.fetch_balance = MagicMock(
            return_value={"info": {"assets": []}, "total": {}, "free": {}})
        await bot.sync_all()

    @pytest.mark.asyncio
    async def test_stop_mid_flight(self):
        """REST in-flight 時 stop：不掛死、事後不再送單。"""
        bot = _make_synced_bot()
        bot.exchange.create_order = MagicMock(
            side_effect=lambda *a, **kw: (_time.sleep(0.2), {"id": "1"})[1])
        bot.precisions[list(bot.state.symbols)[0]] = {"price": 2, "amount": 1, "min_amount": 0.1}
        sym = list(bot.state.symbols)[0]
        order_task = asyncio.create_task(bot.place_order(sym, "buy", 600.0, 1.0))
        await asyncio.sleep(0.05)
        await asyncio.wait_for(bot.stop(), timeout=5)
        await asyncio.wait_for(order_task, timeout=5)
        calls_after_stop = bot.exchange.create_order.call_count
        assert (await bot.place_order(sym, "buy", 600.0, 1.0)) is None
        assert bot.exchange.create_order.call_count == calls_after_stop
```

- [ ] **Step 2: 跑 monkey 測試**

Run: `uv run pytest tests/test_async_offload.py -v` → 全 PASS（若 FAIL 即抓到實作 bug，回頭修）。

- [ ] **Step 3: 全套回歸**

Run: `uv run pytest tests/ -q` → 全綠（125），回報確切數字。

- [ ] **Step 4: Commit**

```bash
git add tests/test_async_offload.py
git commit -m "test: #2/#3 monkey testing — ticker 風暴/例外風暴/停機競態"
```

---

## 完成後

1. 派 `verifier` subagent fresh-context 驗收（重讀 diff + 實跑全套測試）。
2. 跑 dual-review 兩輪（內部 reviewer + fresh-context 外部輪；codex/grok quota 耗盡期間不呼叫外部 CLI）。
3. 更新 `tasks/progress.md`（#2 #3 打勾）。
