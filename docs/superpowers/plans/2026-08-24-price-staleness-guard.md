# 價格時效守衛 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓價格快照帶上抵達時戳，並在下單前判定年齡；過期則跳過本次網格調整並留下可觀測訊號。

**Architecture:** `SymbolState` 新增 `quote_at` 時戳，由 `_handle_ticker` 在寫 bid/ask 的同一個同步 block 內以 `clock.now()` 蓋章。守衛放在 `_grid_step` 頂端——那是 `adjust_grid` 兩個呼叫端（`bot.py:539` ticker、`bot.py:668` 成交後）的共同咽喉。過期則 early-return，不下單、不撤單，並累計計數 + 節流 log + 每日摘要一行。

**Tech Stack:** Python 3 / asyncio / pytest + pytest-asyncio / dataclasses；套件管理用 `uv`。

**Spec:** `docs/superpowers/specs/2026-08-24-price-staleness-guard-design.md`

## Global Constraints

- 守衛的唯一副作用是「不下單」與「寫 log / 計數」。**不得**撤單、改倉、發任何 REST 請求。
- 守衛**不得改寫** `best_bid` / `best_ask` / `latest_price`——只讀不寫。
- `max_price_age_sec = 0` 必須讓行為**完全回到改動前**（生產緊急逃生門）。
- 時鐘一律用 `grid_engine.clock.now()`，**不得**用 `time.monotonic()`（那是另一項獨立 backlog）。
- 不得影響止盈單路徑與 `sync_service` 的 REST 同步。
- 測試基線：**714 passed / 1 skipped**。每個新守衛/斷言必須先在真實缺陷面前紅一次（mutation test）。
- git 只 stage 明確指定的檔案（`git add <file>...`），**禁止** `git add -A` / `git add .`。
- 每個 task 結束前跑 `uv run pytest tests/ -q` 確認全綠再 commit。

---

### Task 1: `SymbolState.quote_at` 欄位 + `_handle_ticker` 蓋章

**Files:**
- Modify: `grid_engine/state.py:12-50`（`SymbolState` dataclass）
- Modify: `grid_engine/bot.py:14-38`（imports）、`grid_engine/bot.py:531-535`（`_handle_ticker` 寫值 block）
- Test: `tests/test_price_staleness_guard.py`（新檔）

**Interfaces:**
- Consumes: `grid_engine.clock.now()`（既有，`grid_engine/clock.py:9`）
- Produces: `SymbolState.quote_at: float`（預設 `0`，單位為 `clock.now()` 的 epoch 秒）；
  Task 2 的 gate 讀它，Task 4 的測試改法依賴它。

- [ ] **Step 1: 寫失敗測試**

建立 `tests/test_price_staleness_guard.py`：

```python
"""價格時效守衛：快照帶抵達時戳，過期不下單。

真缺口不在 _handle_ticker（那裡的價格按定義新鮮），而在 adjust_grid 的第二個
呼叫端 _handle_order_update(bot.py:668)——它用上一次 ticker 留下的殘值
best_bid/best_ask，而 _grid_step(405/419) 把這兩個值直接餵給 place_order()。
"""
from unittest.mock import AsyncMock

import pytest

from grid_engine import clock
from grid_engine.bot import MaxGridBot
from grid_engine.config import GlobalConfig, SymbolConfig

SYMBOL = "XRP/USDC:USDC"


def _make_bot():
    """沿用 tests/test_bot_requote_wiring.py 的最小 bot fixture 模式。
    bandit 關閉：預設 enabled=True 會在 _grid_step 覆寫 grid_spacing。
    """
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
    clock.reset_clock()   # 絕不殘留 sim-clock 給後續測試


@pytest.fixture
def fake_clock():
    """可推進的假時鐘。回傳 advance(seconds)。"""
    t = {"now": 1_000_000.0}
    clock.set_clock(lambda: t["now"])

    def advance(seconds):
        t["now"] += seconds
    yield advance
    clock.reset_clock()


@pytest.mark.asyncio
async def test_handle_ticker_stamps_quote_at(bot, fake_clock):
    """ticker 進來時，quote_at 必須與 bid/ask 在同一次更新中被蓋章。"""
    bot.adjust_grid = AsyncMock()          # 隔離：本測試只驗蓋章
    bot.sync_service.maybe_sync = AsyncMock()
    state = bot.state.symbols[SYMBOL]
    assert state.quote_at == 0

    await bot._handle_ticker({"s": "XRPUSDC", "b": "100.0", "a": "100.2"})

    assert state.best_bid == 100.0
    assert state.best_ask == 100.2
    assert state.quote_at == clock.now()
```

- [ ] **Step 2: 跑測試確認它紅**

Run: `uv run pytest tests/test_price_staleness_guard.py -q`
Expected: FAIL — `AttributeError: 'SymbolState' object has no attribute 'quote_at'`

- [ ] **Step 3: 加欄位**

在 `grid_engine/state.py` 的 `SymbolState` 中，緊接 `best_ask: float = 0` 之後插入：

```python
    # 最近一次 bookTicker 抵達的本機時戳（clock.now()，epoch 秒）。
    # 0 = 從未收過報價。下單前的時效判定讀這個欄位，見 bot._grid_step。
    quote_at: float = 0
```

- [ ] **Step 4: 蓋章**

在 `grid_engine/bot.py` 的 import 區（`from .config import GlobalConfig, SymbolConfig` 那一段附近）加：

```python
from . import clock
```

在 `_handle_ticker` 的寫值 block 內（`state.latest_price = (bid + ask) / 2` 之後、
`self.leading_indicator.update_spread(...)` 之前）插入：

```python
                    # 與 bid/ask 同一個同步 block 蓋章：本區塊內無 await，
                    # 時戳與價格不可能分家。
                    state.quote_at = clock.now()
```

- [ ] **Step 5: 跑測試確認轉綠**

Run: `uv run pytest tests/test_price_staleness_guard.py -q`
Expected: PASS

- [ ] **Step 6: Mutation——把蓋章那行註解掉，確認測試轉紅，再改回來**

Run: `uv run pytest tests/test_price_staleness_guard.py -q`
Expected（蓋章被拿掉時）: FAIL

- [ ] **Step 7: Commit**

```bash
git add grid_engine/state.py grid_engine/bot.py tests/test_price_staleness_guard.py
git commit -m "feat(state): SymbolState 加 quote_at，_handle_ticker 與 bid/ask 同 block 蓋章"
```

---

### Task 2: `max_price_age_sec` config 欄位與正規化

**Files:**
- Modify: `grid_engine/config.py:186-213`（`GlobalConfig` 欄位）、`grid_engine/config.py:215-235`（`to_dict`）、`grid_engine/config.py:281+`（`from_dict`）、`grid_engine/config.py:261-268` 附近（新增 `_parse_max_price_age`）
- Test: `tests/test_price_staleness_guard.py`（沿用 Task 1 的檔案）

**Interfaces:**
- Consumes: 無（純 config 層）
- Produces: `GlobalConfig.max_price_age_sec: float`（預設 `5.0`；`0` = 關閉守衛）與
  `GlobalConfig._parse_max_price_age(value) -> float`。Task 3 的 gate 讀 `self.config.max_price_age_sec`。

- [ ] **Step 1: 寫失敗測試**

追加到 `tests/test_price_staleness_guard.py`：

```python
from grid_engine.config import GlobalConfig as _GC


def test_max_price_age_default_is_five():
    assert _GC().max_price_age_sec == 5.0


@pytest.mark.parametrize("bad", ["abc", None, -1, float("nan"), float("inf"), object()])
def test_max_price_age_garbage_falls_back(bad):
    """垃圾值不得流進 runtime loop——非法一律 fallback 5.0（config from_dict 正規化）。"""
    cfg = _GC.from_dict({"max_price_age_sec": bad})
    assert cfg.max_price_age_sec == 5.0


def test_max_price_age_zero_is_legal_disable():
    """0 是合法的「關閉守衛」值，不得被 fallback 吃掉——它是生產緊急逃生門。"""
    cfg = _GC.from_dict({"max_price_age_sec": 0})
    assert cfg.max_price_age_sec == 0.0


def test_max_price_age_round_trips_through_to_dict():
    cfg = _GC()
    cfg.max_price_age_sec = 12.5
    assert _GC.from_dict(cfg.to_dict()).max_price_age_sec == 12.5
```

- [ ] **Step 2: 跑測試確認它紅**

Run: `uv run pytest tests/test_price_staleness_guard.py -q -k max_price_age`
Expected: FAIL — `AttributeError: 'GlobalConfig' object has no attribute 'max_price_age_sec'`

- [ ] **Step 3: 加欄位**

在 `grid_engine/config.py` 的 `GlobalConfig` 中，緊接
`requote_threshold_factor: float = 0.5` 之後插入：

```python
    max_price_age_sec: float = 5.0       # 價格快照最大可用年齡（秒），0 = 關閉守衛
```

- [ ] **Step 4: 加正規化 staticmethod**

在 `GlobalConfig` 內、緊接 `_parse_position_adjust_cooldown` 之後插入
（形狀刻意與它一致）：

```python
    @staticmethod
    def _parse_max_price_age(value) -> float:
        """正規化價格快照最大可用年齡（秒），非法/負值 fallback 到 5.0。

        0 為合法值，語意是「關閉守衛」——它是生產上的緊急逃生門，
        不得被 fallback 吃掉。
        """
        try:
            age = float(value)
        except (TypeError, ValueError):
            return 5.0
        return age if math.isfinite(age) and age >= 0 else 5.0
```

- [ ] **Step 5: 接進 `to_dict` / `from_dict`**

`to_dict()` 內，緊接 `"requote_threshold_factor": self.requote_threshold_factor,` 之後：

```python
            "max_price_age_sec": self.max_price_age_sec,
```

`from_dict()` 的 `cls(...)` 內，緊接 `requote_threshold_factor=...` 那一項之後：

```python
            max_price_age_sec=cls._parse_max_price_age(
                data.get("max_price_age_sec", 5.0)),
```

- [ ] **Step 6: 跑測試確認轉綠**

Run: `uv run pytest tests/test_price_staleness_guard.py -q -k max_price_age`
Expected: PASS（4 個 test function，含 parametrize 共 9 個 case）

- [ ] **Step 7: Mutation——三條逐條實跑轉紅再改回**

1. `_parse_max_price_age` 的 `age >= 0` 改成 `age > 0` → `test_max_price_age_zero_is_legal_disable` 轉紅
2. 拿掉 `math.isfinite(age)` → `nan` / `inf` 的 parametrize case 轉紅
3. `from_dict` 改成 `data.get("max_price_age_sec", 5.0)` 不過正規化 → 垃圾值 case 轉紅

- [ ] **Step 8: 跑全套 + Commit**

```bash
uv run pytest tests/ -q
git add grid_engine/config.py tests/test_price_staleness_guard.py
git commit -m "feat(config): max_price_age_sec 欄位與正規化（0 = 關閉守衛的逃生門）"
```

---

### Task 3: `_grid_step` 時效 gate + `_note_stale_quote` 節流告警

**Files:**
- Modify: `grid_engine/bot.py:340-344`（`_grid_step` 頂端）、`grid_engine/bot.py` 新增 `_note_stale_quote` 與 `__init__` 的計數欄位
- Test: `tests/test_price_staleness_guard.py`

**Interfaces:**
- Consumes: `SymbolState.quote_at`（Task 1）、`GlobalConfig.max_price_age_sec`（Task 2）、`clock.now()`
- Produces:
  - `MaxGridBot._note_stale_quote(ccxt_symbol: str, age: float) -> None`
  - `MaxGridBot.stale_quote_counts: Dict[str, int]`（per-symbol 累計，Task 5 的每日摘要讀它）
  - `MaxGridBot._last_stale_log_at: Dict[str, float]`（節流用）
  - 模組常數 `STALE_QUOTE_LOG_SECONDS = 3600.0`

- [ ] **Step 1: 寫失敗測試**

追加到 `tests/test_price_staleness_guard.py`：

```python
async def _seed_fresh_quote(bot, price=100.0):
    """走真的 _handle_ticker 蓋章，不手動塞 quote_at——這樣測到的是真接線。"""
    bot.sync_service.maybe_sync = AsyncMock()
    await bot._handle_ticker({"s": "XRPUSDC", "b": str(price), "a": str(price)})


def _prime_for_ordering(bot):
    """把狀態擺成「flat 側缺單」⇒ _should_adjust_grid 無條件 True ⇒ 會走到下單。"""
    state = bot.state.symbols[SYMBOL]
    state.long_position = 0.0
    state.short_position = 0.0
    state.buy_long_orders = 0.0
    state.sell_long_orders = 0.0
    state.buy_short_orders = 0.0
    state.sell_short_orders = 0.0
    return state


@pytest.mark.asyncio
async def test_fresh_quote_places_orders(bot, fake_clock):
    """基準線：快照新鮮 → 正常下單。沒有這條，過期測試可能只是因為別的原因不下單。"""
    _prime_for_ordering(bot)
    await _seed_fresh_quote(bot)
    bot.order_executor.place_order.reset_mock()

    await bot.adjust_grid(SYMBOL)

    assert bot.order_executor.place_order.await_count > 0


@pytest.mark.asyncio
async def test_stale_quote_places_no_orders(bot, fake_clock):
    """快照超過 max_price_age_sec → 一張單都不許下。"""
    bot.config.max_price_age_sec = 5.0
    _prime_for_ordering(bot)
    await _seed_fresh_quote(bot)
    bot.order_executor.place_order.reset_mock()
    bot.order_executor.cancel_orders_for_side.reset_mock()

    fake_clock(5.1)
    await bot.adjust_grid(SYMBOL)

    assert bot.order_executor.place_order.await_count == 0
    # non-goal：過期不撤單（撤單同樣需要準確的價格認知）
    assert bot.order_executor.cancel_orders_for_side.await_count == 0


@pytest.mark.asyncio
async def test_order_update_path_with_stale_residual_is_blocked(bot, fake_clock):
    """本次真正要修的形態：_handle_order_update → adjust_grid 用上一次 ticker
    留下的殘值 best_bid/best_ask，中間隔多久完全不受控。
    """
    bot.config.max_price_age_sec = 5.0
    state = _prime_for_ordering(bot)
    await _seed_fresh_quote(bot)
    bot.order_executor.place_order.reset_mock()

    fake_clock(600.0)          # ticker 這 10 分鐘沒再來，但成交事件來了
    await bot._handle_order_update({
        "o": {"s": "XRPUSDC", "X": "FILLED", "S": "BUY", "ps": "LONG",
              "q": "0.02", "z": "0.02", "ap": "100.0", "rp": "0"},
    })

    assert bot.order_executor.place_order.await_count == 0
    assert state.best_bid == 100.0      # 守衛只讀不寫，殘值原樣保留


@pytest.mark.asyncio
async def test_never_received_quote_is_blocked(bot, fake_clock):
    """quote_at == 0（從未收過 ticker）不得被當成「年齡 = now」而放行。"""
    state = _prime_for_ordering(bot)
    state.latest_price = 100.0          # 有價格但沒有時戳
    state.best_bid = state.best_ask = 100.0
    assert state.quote_at == 0

    await bot.adjust_grid(SYMBOL)

    assert bot.order_executor.place_order.await_count == 0


@pytest.mark.asyncio
async def test_clock_rewind_blocks_then_self_heals(bot, fake_clock):
    """牆鐘往前跳 → age 為負 → 擋（安全側）；下一筆 ticker 重新蓋章即自癒。"""
    _prime_for_ordering(bot)
    await _seed_fresh_quote(bot)
    bot.order_executor.place_order.reset_mock()

    fake_clock(-3600.0)                 # 時鐘倒退一小時
    await bot.adjust_grid(SYMBOL)
    assert bot.order_executor.place_order.await_count == 0

    await _seed_fresh_quote(bot, price=100.0)   # 下一筆 ticker 重新蓋章
    _prime_for_ordering(bot)
    bot.order_executor.place_order.reset_mock()
    await bot.adjust_grid(SYMBOL)
    assert bot.order_executor.place_order.await_count > 0


@pytest.mark.asyncio
async def test_zero_threshold_disables_guard(bot, fake_clock):
    """max_price_age_sec = 0 → 行為完全回到改動前（生產緊急逃生門）。"""
    bot.config.max_price_age_sec = 0
    _prime_for_ordering(bot)
    await _seed_fresh_quote(bot)
    bot.order_executor.place_order.reset_mock()

    fake_clock(86400.0)                 # 一整天沒報價也照下
    await bot.adjust_grid(SYMBOL)

    assert bot.order_executor.place_order.await_count > 0


@pytest.mark.asyncio
async def test_threshold_change_takes_effect_immediately(bot, fake_clock):
    """TUI 的「設定即時套用」改門檻必須立刻生效——gate 不得快取 config 值。"""
    bot.config.max_price_age_sec = 5.0
    _prime_for_ordering(bot)
    await _seed_fresh_quote(bot)
    bot.order_executor.place_order.reset_mock()

    fake_clock(30.0)
    await bot.adjust_grid(SYMBOL)
    assert bot.order_executor.place_order.await_count == 0

    bot.config.max_price_age_sec = 60.0         # 熱改門檻
    _prime_for_ordering(bot)
    await bot.adjust_grid(SYMBOL)
    assert bot.order_executor.place_order.await_count > 0


@pytest.mark.asyncio
async def test_stale_events_are_counted_and_log_is_throttled(bot, fake_clock, caplog):
    """過期必須可觀測，但不得洗版：計數每次都加，log 每 3600 秒才一筆。"""
    import logging
    bot.config.max_price_age_sec = 5.0
    _prime_for_ordering(bot)
    await _seed_fresh_quote(bot)
    fake_clock(600.0)

    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            await bot.adjust_grid(SYMBOL)

    assert bot.stale_quote_counts[SYMBOL] == 5
    hits = [r for r in caplog.records if "價格快照過期" in r.getMessage()]
    assert len(hits) == 1
```

- [ ] **Step 2: 跑測試確認它紅**

Run: `uv run pytest tests/test_price_staleness_guard.py -q`
Expected: FAIL——過期相關的 test 全紅（目前沒有 gate，過期照樣下單）

- [ ] **Step 3: `__init__` 加計數欄位與常數**

在 `grid_engine/bot.py` 模組層（既有常數 re-export 附近）加：

```python
STALE_QUOTE_LOG_SECONDS = 3600.0  # 價格過期 log 節流間隔（秒），不洗版
```

在 `MaxGridBot.__init__` 內（`self.last_order_times` 那一區附近）加：

```python
        # 價格快照過期事件：計數給每日摘要，時戳給 log 節流
        self.stale_quote_counts: Dict[str, int] = {}
        self._last_stale_log_at: Dict[str, float] = {}
```

- [ ] **Step 4: 加 `_note_stale_quote`**

在 `_grid_step` 之前插入：

```python
    def _note_stale_quote(self, ccxt_symbol: str, age: float) -> None:
        """記錄一次「快照過期而跳過調整」。

        門檻設太小會讓網格靜默停擺，而「沒有儀器」正是 userData watchdog spec
        要根除的形態，不得在這裡重演：計數每次都加（每日摘要讀它），log 走
        節流免得洗版。本函式不得有下單/撤單/REST 副作用。
        """
        self.stale_quote_counts[ccxt_symbol] = self.stale_quote_counts.get(ccxt_symbol, 0) + 1
        now = clock.now()
        last = self._last_stale_log_at.get(ccxt_symbol, 0.0)
        # 時鐘倒退時 last 會落在未來 ⇒ 重新錨定，否則節流會凍結到永遠不 log
        if last > now:
            last = 0.0
        if now - last >= STALE_QUOTE_LOG_SECONDS:
            self._last_stale_log_at[ccxt_symbol] = now
            logger.warning(
                f"[staleness] {ccxt_symbol} 價格快照過期 {age:.1f}s "
                f"(門檻 {self.config.max_price_age_sec}s)，跳過網格調整；"
                f"累計 {self.stale_quote_counts[ccxt_symbol]} 次"
            )
```

- [ ] **Step 5: 加 gate**

在 `grid_engine/bot.py` 的 `_grid_step` 中，緊接既有的 `if price <= 0: return` 之後插入：

```python
        # 價格時效守衛：adjust_grid 有兩個呼叫端（_handle_ticker / _handle_order_update），
        # 後者用的是上一次 ticker 留下的殘值 best_bid/best_ask，中間隔多久不受控，
        # 而下方 place_order 會直接吃這兩個值。這一格是兩條路徑的共同咽喉。
        # 提前 return 語意安全：下方的 DGT check_and_reset 與 bandit 套用同樣吃 price，
        # 價格不可信時本來就不該跑；跳過不遺失狀態（下一筆 ticker 會補做）。
        max_age = self.config.max_price_age_sec
        if max_age > 0:
            age = clock.now() - sym_state.quote_at
            if sym_state.quote_at <= 0 or age < 0 or age > max_age:
                self._note_stale_quote(ccxt_symbol, age)
                return
```

- [ ] **Step 6: 跑測試確認轉綠**

Run: `uv run pytest tests/test_price_staleness_guard.py -q`
Expected: PASS

- [ ] **Step 7: Mutation——四條逐條實跑轉紅再改回**

1. 拿掉 `sym_state.quote_at <= 0` 這一項 → `test_never_received_quote_is_blocked` 轉紅
2. 拿掉 `age < 0` 這一項 → `test_clock_rewind_blocks_then_self_heals` 轉紅
3. `max_age > 0` 改成 `True`（不再支援關閉） → `test_zero_threshold_disables_guard` 轉紅
4. `_note_stale_quote` 內把節流條件改成無條件 log → `test_stale_events_are_counted_and_log_is_throttled` 轉紅

- [ ] **Step 8: Commit（先不跑全套，Task 4 會處理既有測試的紅）**

```bash
git add grid_engine/bot.py tests/test_price_staleness_guard.py
git commit -m "feat(bot): _grid_step 價格時效 gate + _note_stale_quote 節流告警"
```

---

### Task 4: 修既有測試的連帶紅（**這一步不可略過**）

**背景（實作者必讀）**：既有測試普遍直接塞 `state.latest_price = 100.0` 而不走
`_handle_ticker` ⇒ `quote_at` 停在 `0` ⇒ Task 3 的 gate 會把它們全擋掉。這是**預期內的
連帶紅**，代表 gate 真的接上了；修法是讓測試也蓋章，而不是弱化 gate。

已知會受影響的檔案（含 `_grid_step` / `adjust_grid` 的出現次數）：

| 次數 | 檔案 |
|---|---|
| 15 | `tests/test_characterization_grid.py` |
| 13 | `tests/test_async_offload.py` |
| 9 | `tests/test_order_guard.py` |
| 7 | `tests/test_bot_requote_wiring.py` |
| 5 | `tests/test_bandit_overwrites_config.py` |
| 4 | `tests/test_decision_log.py` |
| 2 | `tests/test_components.py` |
| 1 | `tests/test_userdata_watchdog_wiring.py` |
| 1 | `tests/test_trade_stats_sync.py` |

**Files:**
- Modify: 上表中實際轉紅的檔案（跑一次全套才知道確切是哪幾個）

**Interfaces:**
- Consumes: `SymbolState.quote_at`（Task 1）
- Produces: 無新介面

- [ ] **Step 1: 跑全套，取得確切紅名單**

Run: `uv run pytest tests/ -q`
把失敗清單記下來（預期集中在上表那 9 個檔）。

- [ ] **Step 2: 逐檔修正——只加蓋章，不改斷言**

修法：凡是直接設 `latest_price` / `best_bid` / `best_ask` 之後會走到
`_grid_step` 或 `adjust_grid` 的地方，同時蓋章。在檔案 import 區加
`from grid_engine import clock`，並在設價的地方加一行：

```python
        state.latest_price = 100.0
        state.best_bid = state.best_ask = 100.0
        state.quote_at = clock.now()      # 價格時效守衛：模擬「剛收到 ticker」
```

**禁止的修法**（會讓 gate 變成假守衛）：
- 把 `bot.config.max_price_age_sec = 0` 塞進共用 fixture 一次關掉所有測試的守衛
- 把 `quote_at` 的預設值從 `0` 改成別的東西
- 放寬 gate 條件去迎合測試

- [ ] **Step 3: 跑全套確認全綠**

Run: `uv run pytest tests/ -q`
Expected: PASS，總數 ≥ 基線 714 + 本計畫新增數

- [ ] **Step 4: 驗證修的是測試不是守衛**

Run: `git diff HEAD~1 -- grid_engine/`
Expected: 空輸出（Task 4 不許動 `grid_engine/` 下任何檔案）

- [ ] **Step 5: Commit**

```bash
git add tests/
git commit -m "test: 既有測試補 quote_at 蓋章（gate 上線的連帶修正，未動守衛）"
```

---

### Task 5: 每日摘要帶過期那一行

**Files:**
- Modify: `grid_engine/reporting.py:11-17`（`DailyReporter.__init__`）、`grid_engine/reporting.py:92-100`（`pnl_data` 組裝）、新增 `_get_stale_quote_summary`
- Modify: `grid_engine/notifier.py:200-213`（訊息組裝）、新增 `_format_stale_quote_line`
- Modify: `grid_engine/bot.py`（建 `DailyReporter` 的地方，把 bot 自己傳進去）
- Test: `tests/test_price_staleness_guard.py`

**Interfaces:**
- Consumes: `MaxGridBot.stale_quote_counts`（Task 3）
- Produces:
  - `DailyReporter.__init__(..., stale_quote_source=None)`——傳入持有 `stale_quote_counts` 的物件（實務上是 bot）
  - `DailyReporter._get_stale_quote_summary() -> Optional[dict]`，形狀 `{"total": int, "symbols": dict}`
  - `pnl_data["stale_quotes"]`
  - `TelegramNotifier._format_stale_quote_line(stale) -> str`

- [ ] **Step 1: 寫失敗測試**

追加到 `tests/test_price_staleness_guard.py`：

```python
from grid_engine.notifier import TelegramNotifier
from grid_engine.reporting import DailyReporter


def test_stale_quote_line_omitted_when_zero():
    """計數為 0 不出這一行——正常狀態不加噪音。"""
    assert TelegramNotifier._format_stale_quote_line({"total": 0, "symbols": {}}) == ""
    assert TelegramNotifier._format_stale_quote_line(None) == ""
    assert TelegramNotifier._format_stale_quote_line("not a dict") == ""


def test_stale_quote_line_present_when_nonzero():
    line = TelegramNotifier._format_stale_quote_line(
        {"total": 42, "symbols": {"XRP/USDC:USDC": 42}})
    assert "價格快照過期" in line
    assert "42" in line
    assert line.endswith("\n")


def test_stale_quote_line_survives_garbage_counts():
    """型別錯不得讓整封摘要發不出去——降級成不帶數字，訊號本身不能掉。"""
    line = TelegramNotifier._format_stale_quote_line({"total": "abc", "symbols": None})
    assert isinstance(line, str)


def test_reporter_collects_stale_counts():
    class _Src:
        stale_quote_counts = {"XRP/USDC:USDC": 3, "BNB/USDC:USDC": 4}

    import asyncio as _a
    r = DailyReporter(GlobalConfig(), None, None, _a.Event(), stale_quote_source=_Src())
    assert r._get_stale_quote_summary() == {
        "total": 7, "symbols": {"XRP/USDC:USDC": 3, "BNB/USDC:USDC": 4}}


def test_reporter_stale_counts_failure_is_swallowed():
    """取不到就不顯示那行，絕不能讓每日摘要發不出去（沿用 watchdog 那行的硬性要求）。"""
    class _Boom:
        @property
        def stale_quote_counts(self):
            raise RuntimeError("boom")

    import asyncio as _a
    r = DailyReporter(GlobalConfig(), None, None, _a.Event(), stale_quote_source=_Boom())
    assert r._get_stale_quote_summary() is None
```

- [ ] **Step 2: 跑測試確認它紅**

Run: `uv run pytest tests/test_price_staleness_guard.py -q -k stale_quote`
Expected: FAIL — `AttributeError: type object 'TelegramNotifier' has no attribute '_format_stale_quote_line'`

- [ ] **Step 3: `reporting.py` 收資料**

`DailyReporter.__init__` 簽章末尾加 `stale_quote_source=None`，並存 `self.stale_quote_source = stale_quote_source`。

新增方法（放在 `_get_watchdog_status` 之後，形狀與它一致）：

```python
    def _get_stale_quote_summary(self):
        """讀取價格快照過期計數供每日摘要顯示。

        硬性要求同 _get_watchdog_status：任何例外都在這裡吞掉降級成「不顯示該行」
        （回傳 None），不得往外冒泡把整封摘要弄掉。只讀，不重置計數。
        """
        if self.stale_quote_source is None:
            return None
        try:
            counts = dict(self.stale_quote_source.stale_quote_counts)
            total = sum(int(v) for v in counts.values())
            return {"total": total, "symbols": counts}
        except Exception as e:
            logger.warning(f"[reporter] 價格過期計數讀取失敗，摘要跳過該行: {e}")
            return None
```

在 `pnl_data` 字典中，緊接 `"watchdog": self._get_watchdog_status(),` 之後加：

```python
                    "stale_quotes": self._get_stale_quote_summary(),
```

- [ ] **Step 4: `notifier.py` 組字**

在 `notify_daily_pnl` 內，緊接 `watchdog_line = ...` 之後加：

```python
        stale_line = self._format_stale_quote_line(pnl_data.get("stale_quotes"))
```

並在 `msg` 的 f-string 中，把 `f"{watchdog_line}"` 那一行之後改為：

```python
            f"{watchdog_line}"
            f"{stale_line}"
```

新增（放在 `_format_watchdog_line` 之後）：

```python
    @staticmethod
    def _format_stale_quote_line(stale) -> str:
        """價格快照過期計數那一行。

        安全要求同 _format_watchdog_line：文案是這裡自己定義的常數，不把外部
        資料未跳脫插進 HTML 訊息（parse_mode=HTML）。計數為 0 或格式不符時
        整行省略——正常狀態不加噪音。
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
        return f"⚠️ <b>價格快照過期</b>：今日 {total} 次跳過網格調整\n"
```

- [ ] **Step 5: `bot.py` 接線**

`grid_engine/bot.py:104-107` 目前是：

```python
        self.reporter = DailyReporter(
            config=self.config, state=self.state,
            notifier=self.notifier, stop_event=self._stop_event,
        )
```

改成：

```python
        self.reporter = DailyReporter(
            config=self.config, state=self.state,
            notifier=self.notifier, stop_event=self._stop_event,
            stale_quote_source=self,
        )
```

- [ ] **Step 6: 跑測試確認轉綠**

Run: `uv run pytest tests/test_price_staleness_guard.py -q -k stale_quote`
Expected: PASS

- [ ] **Step 7: Mutation——三條逐條實跑轉紅再改回**

1. `_format_stale_quote_line` 的 `if total <= 0: return ""` 拿掉 → `test_stale_quote_line_omitted_when_zero` 轉紅
2. `pnl_data` 拿掉 `"stale_quotes"` key → 端到端接線測試轉紅（見 Step 8）
3. `_get_stale_quote_summary` 的 `except` 拿掉 → `test_reporter_stale_counts_failure_is_swallowed` 轉紅

- [ ] **Step 8: 加端到端接線測試**

**理由**：「那行有沒有真的被組進訊息本體」用單元測試分開驗兩半是驗不到接線的
——2026-08-24 的 0a 就是踩這個坑（`pnl_data` 少帶一個 key，摘要照樣寄得出去，
但那行不見了，人工完全看不出來）。這裡照
`tests/test_reporting_watchdog.py::TestDailyReporterEndToEndWiring`（`tests/test_reporting_watchdog.py:73-133`）
的作法：走真的 `DailyReporter.run()` + 真的 `TelegramNotifier`，只 mock `send`。

追加到 `tests/test_price_staleness_guard.py`：

```python
class TestStaleQuoteReachesTelegram:
    """紅在：reporting.py 的 pnl_data 少帶 "stale_quotes" 這個 key（接線斷掉）。"""

    @staticmethod
    async def _run_once(stale_source, notifier):
        import asyncio as _a
        import types
        from unittest.mock import AsyncMock, patch

        stop_event = _a.Event()
        config = types.SimpleNamespace(telegram_daily_pnl_hour=20)
        sym_state = types.SimpleNamespace(long_position=0.5, short_position=0.0,
                                          unrealized_pnl=1.5)
        state = types.SimpleNamespace(
            symbols={"BNB/USDC:USDC": sym_state},
            start_time=None,
            total_unrealized_pnl=1.5,
            total_equity=94.49,
            margin_usage=0.193,
            total_profit=12.3,
        )
        reporter = DailyReporter(config=config, state=state, notifier=notifier,
                                 stop_event=stop_event,
                                 stale_quote_source=stale_source)

        calls = {"n": 0}

        async def fake_sleep(_seconds):
            calls["n"] += 1
            if calls["n"] >= 2:      # 第一輪送出摘要後才收工
                stop_event.set()

        with patch("grid_engine.reporting.asyncio.sleep",
                   AsyncMock(side_effect=fake_sleep)):
            await _a.wait_for(reporter.run(), timeout=5)

    def test_stale_count_reaches_the_telegram_message(self):
        import asyncio as _a
        from unittest.mock import AsyncMock

        class _Src:
            stale_quote_counts = {"BNB/USDC:USDC": 17}

        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        _a.run(self._run_once(_Src(), notifier))

        notifier.send.assert_called_once()
        msg = notifier.send.call_args[0][0]
        assert "價格快照過期" in msg
        assert "17" in msg
        assert "94.49" in msg          # 既有欄位不得因新增那行而掉

    def test_zero_stale_count_leaves_summary_clean(self):
        """0 次時整封摘要不得出現那一行——正常狀態不加噪音。"""
        import asyncio as _a
        from unittest.mock import AsyncMock

        class _Src:
            stale_quote_counts = {}

        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        _a.run(self._run_once(_Src(), notifier))

        msg = notifier.send.call_args[0][0]
        assert "每日損益摘要" in msg
        assert "價格快照過期" not in msg
```

- [ ] **Step 9: 跑全套 + Commit**

```bash
uv run pytest tests/ -q
git add grid_engine/reporting.py grid_engine/notifier.py grid_engine/bot.py tests/test_price_staleness_guard.py
git commit -m "feat(reporting): 每日摘要帶價格快照過期計數（0 次不出這行）"
```

---

### Task 6: decision log 加 `quote_age` 儀器

**Files:**
- Modify: `grid_engine/bot.py:499-518`（`_log_decision`）及其呼叫端
- Test: `tests/test_price_staleness_guard.py`

**Interfaces:**
- Consumes: `SymbolState.quote_at`（Task 1）
- Produces: decision log 每筆 JSON 多一個頂層欄位 `quote_age`（float，秒）

**注意**：這條路徑**只涵蓋「沒被擋下」的決策**（被擋就 early-return，走不到
`_log_decision`）⇒ 它量的是「正常情況下快照有多舊」，用來判斷 5 秒門檻是否過寬或
過窄；「被擋了幾次」由 Task 3 的計數負責。兩者互補，缺一都看不全。

- [ ] **Step 1: 寫失敗測試**

追加到 `tests/test_price_staleness_guard.py`：

```python
@pytest.mark.asyncio
async def test_decision_log_carries_quote_age(bot, fake_clock, tmp_path):
    """儀器：5 秒門檻是猜測值，要靠這個欄位的實測分佈日後收緊。"""
    import json as _json
    log_path = tmp_path / "decisions.jsonl"
    bot._decision_log_path = str(log_path)
    state = _prime_for_ordering(bot)
    state.long_position = 1.0            # 有倉 → 走到 decide() 與 _log_decision
    state.buy_long_orders = 0.0
    await _seed_fresh_quote(bot)

    fake_clock(2.0)
    await bot.adjust_grid(SYMBOL)

    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert lines, "決策未落檔"
    rec = _json.loads(lines[-1])
    assert rec["quote_age"] == pytest.approx(2.0)
```

- [ ] **Step 2: 跑測試確認它紅**

Run: `uv run pytest tests/test_price_staleness_guard.py -q -k quote_age`
Expected: FAIL — `KeyError: 'quote_age'`

- [ ] **Step 3: 實作**

`grid_engine/bot.py:499` 的簽章改為：

```python
    def _log_decision(self, ccxt_symbol: str, inputs, decision,
                      quote_age: float = 0.0) -> None:
```

`rec` 字典中，緊接 `"ts": time.time(),` 之後加：

```python
                "quote_age": quote_age,
```

Task 3 的 gate 要改寫成把年齡存進區域變數，讓守衛關閉時儀器仍然有數字
（`max_price_age_sec = 0` 只關閉「擋單」，不關閉「量測」）。把 Task 3 Step 5 那段
gate 改成：

```python
        max_age = self.config.max_price_age_sec
        quote_age = clock.now() - sym_state.quote_at
        if max_age > 0:
            if sym_state.quote_at <= 0 or quote_age < 0 or quote_age > max_age:
                self._note_stale_quote(ccxt_symbol, quote_age)
                return
```

`grid_engine/bot.py:427-428` 的呼叫端改為：

```python
        if decision is not None:
            self._log_decision(ccxt_symbol, inputs, decision, quote_age)
```

- [ ] **Step 4: 跑測試確認轉綠**

Run: `uv run pytest tests/test_price_staleness_guard.py -q -k quote_age`
Expected: PASS

- [ ] **Step 5: Mutation——把 `"quote_age": quote_age` 改成固定 `0.0`，確認測試轉紅，再改回**

- [ ] **Step 6: 跑全套 + Commit**

```bash
uv run pytest tests/ -q
git add grid_engine/bot.py tests/test_price_staleness_guard.py
git commit -m "feat(bot): decision log 加 quote_age，讓 5 秒門檻能用實測收緊"
```

---

### Task 7: `tick_sim.py` 註解 + 完成報告

**Files:**
- Modify: `backtest/tick_sim.py:197`（決策 gate 的段落註解）
- Modify: `tasks/progress.md`、`tasks/notes.md`

**Interfaces:**
- Consumes: 無
- Produces: 無（僅文件）

- [ ] **Step 1: 加註解**

在 `backtest/tick_sim.py` 的
`# ---- (d) 決策 gate（鏡射 live _handle_ticker→adjust_grid→_grid_step）----`
之後補一段：

```python
        # 註：live 端的 _grid_step 另有「價格時效守衛」（快照年齡 > config
        # max_price_age_sec 就跳過本次調整），本模擬是逐 tick 餵資料、年齡恆為 0
        # ⇒ 該 gate 在回測中恆通過，故此處不需鏡射。做 live/backtest fidelity
        # 比對時，這是一個已知且刻意的差異，不是 bug。
        # 設計出處：docs/superpowers/specs/2026-08-24-price-staleness-guard-design.md §6
```

- [ ] **Step 2: 跑全套並記下確切數字**

Run: `uv run pytest tests/ -q`
把 `N passed / M skipped` 原樣抄下來——**報數量不報形容詞**。

- [ ] **Step 3: 寫完成報告進 `tasks/progress.md`**

必須包含：
- 全套測試數字（`N passed / M skipped`），與基線 714/1 的差值
- 每條 mutation 的實跑結果（哪一條改動讓哪一個測試轉紅）
- **「已 commit」與「已重啟生效」分開記**——這份改動需重啟引擎才生效，
  commit 完成 ≠ 生產生效
- 觸發面校準原文照抄：log 裡沒有這兩種形態的實證，優先度排序是推測性的，
  **不得宣稱修掉了已觀測到的生產事故**

- [ ] **Step 4: Commit**

```bash
git add backtest/tick_sim.py tasks/progress.md tasks/notes.md
git commit -m "docs: tick_sim 標註 live 時效 gate 的刻意差異 + 完成報告"
```

---

### Task 8: 驗收流程（dev-rules 強制，不可略過）

**Files:** 無（流程任務）

- [ ] **Step 1: `security-review`**

改動命中 Red Team Protocol 適用範圍（會下單的核心邏輯）⇒ 外部輪**之前**先跑
`security-review` skill，findings 併入整合修復。

- [ ] **Step 2: fresh-context `verifier`**

派 `verifier` agent：read-back + 實跑測試，**不吃實作者自述**。
重點複核：gate 真的擋得住 `_handle_order_update` 路徑；Task 4 沒有靠關閉守衛來讓
既有測試變綠（`git log -p -- grid_engine/` 檢查）。

- [ ] **Step 3: `dual-review`**

Plan track ⇒ 走 `dual-review` skill（外部獨立輪 + Round 2 專案規則輪），
findings 整合修完。**未拿到 `Ship as-is` verdict 前，任務不得標記完成、
「都做完了」不得出現。**

- [ ] **Step 4: verdict 落 `tasks/notes.md`**

附各輪 findings 計數（如「ext C2/B1、int C0」；C=blocker、B=should-fix、A=clean）。

- [ ] **Step 5: 提醒使用者重啟引擎**

改動需重啟才生效。重啟由使用者執行；重啟後確認 log 出現正常掛單，
並觀察是否出現 `[staleness]` 警告（門檻 5 秒若過緊會在這裡現形）。
