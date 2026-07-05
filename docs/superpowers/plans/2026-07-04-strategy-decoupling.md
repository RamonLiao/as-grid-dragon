# 回測/實盤策略脫鉤 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 grid_engine 實盤網格決策抽成純函數層（`decision.py`），實盤與回測吃同一個 `decide()`，並以 sim-clock 讓時間型 manager 在回測中生效——實盤行為零改變。

**Architecture:** 三個新模組。`clock.py` 注入時間（實盤預設 `time.time()`，回測餵 K 線時間）。`decision.py` 純函數層：所有網格決策數學（裝死、止盈加倍、追價判斷、GLFT 數量、間距套用）+ dataclass 契約，無 I/O、不寫任何物件。`snapshot.py` 共享的「不純但單一」邊界：呼叫 manager 實例組出 `EnhancementSnapshot`，bot 和 backtester 都 import 它以保證 manager 呼叫序列一致（DRY，防兩邊發散）。bot.py `_grid_step` 改為 `build_snapshot → decide → execute → 決策日誌`；backtester 移除 `core.strategy` 依賴改吃 `decide()`。

**Tech Stack:** Python 3, dataclasses（`frozen=True` 契約）, pytest（現有測試框架）, numpy（manager 內部）, uv（套件管理）。

## Global Constraints

- **實盤行為零改變**（硬約束）：純層是「搬移」現行 grid_engine 邏輯含 bug-for-bug，不重寫、不採 core 版語意。sim-clock 預設 `time.time()`，實盤路徑位元級等價。
- 裝死無對手倉 fallback 用 grid_engine 硬編值 **1.05 / 0.95**（audit 已確認全 repo 無非預設值，不加開關）。
- 裝死判斷：`my_position > position_threshold`（嚴格大於）。止盈加倍：`my_pos > position_limit or opp_pos >= position_threshold`。數量下限 clamp：`max(initial_quantity * 0.5, qty)`。
- `decision.py` **不得** import bot / manager / ccxt / 任何有 I/O 的模組；只依賴 stdlib + dataclasses。
- 鎖序、skip-if-locked、下單守衛/退避/斷路器/cooldown、flat-entry 開倉、`_check_and_reduce_positions`、DGT、GLFT price skew（死代碼）**全不進純層**，維持現狀。
- Git：只 `git add <明確檔案>`，禁止 `git add -A`／`git add .`。
- 套件用 `uv`。測試回報數量不報形容詞。

## File Structure

- `grid_engine/clock.py`（新，~15 行）：`now()` / `set_clock(fn)` / `reset_clock()`。
- `grid_engine/decision.py`（新，~220 行）：dataclass 契約 + 純函數 `decide()` 及 helper；併入舊 `strategy.py` 的純計算。
- `grid_engine/snapshot.py`（新，~90 行）：`ManagerBundle` + `build_snapshot()`（共享，不純）。
- `grid_engine/enhancements.py`（改）：9 處 `time.time()` → `clock.now()`。
- `grid_engine/bot.py`（改）：`_grid_step` 接線純層 + 決策日誌；刪 `_get_dynamic_spacing`/`_get_adjusted_quantity`/`_should_adjust_grid`/`_place_grid` 決策半段。
- `grid_engine/strategy.py`（刪，Task 7）。
- `grid_engine/__init__.py`（改）：`GridStrategy` 相容 re-export 遷移。
- `backtest/backtester.py`（改，Task 8）：移除 `core.strategy`，改吃 `decide()` + sim-clock。
- `grid_engine/backtest.py`（刪，Task 8，325 行死碼）。
- `grid_engine/replay.py`（新，Task 9）：決策日誌重放比對工具。
- 測試：`tests/test_characterization_grid.py`、`tests/test_clock.py`、`tests/test_decision.py`、`tests/test_snapshot_sequence.py`、`tests/test_backtester_decision.py`、`tests/test_replay.py`。

---

## Task 1: Characterization tests — 鎖死現行 `_place_grid` / 決策方法行為

先對現行 bot 決策方法寫行為快照測試（mock manager 固定輸出，斷言撤/下單完整參數與 sym_state 寫回）。搬移後同組測試不改而綠 = 等價性第一道防線。此 task 不改產品碼。

**Files:**
- Test: `tests/test_characterization_grid.py`（新）

**Interfaces:**
- Consumes: `grid_engine.bot.MaxGridBot`、`grid_engine.config.SymbolConfig`、`grid_engine.state.SymbolState`。
- Produces: 一組「呼叫 `_place_grid` / `_get_dynamic_spacing` / `_get_adjusted_quantity` / `_should_adjust_grid`，斷言 place_order 呼叫序列與回傳」的測試，後續 task 不得修改其斷言。

**測試 harness 參考**（沿用 `tests/test_order_guard.py::_make_bot` 模式，讀該檔確認 config 建構方式）。

- [ ] **Step 1: 寫 characterization 測試骨架 + 正常模式多頭**

```python
# tests/test_characterization_grid.py
"""Characterization tests：鎖死重構前 bot 網格決策行為。
搬移到 decision.py 後，這些斷言必須不改而綠。"""
import pytest
from unittest.mock import MagicMock, AsyncMock, call

from grid_engine.bot import MaxGridBot
from grid_engine.config import Config, SymbolConfig
from grid_engine.enhancements import MaxEnhancement


def _make_bot(**enh_kwargs):
    cfg = Config()
    cfg.symbols = {"XRP/USDC:USDC": SymbolConfig(
        symbol="XRPUSDC", ccxt_symbol="XRP/USDC:USDC", enabled=True,
        take_profit_spacing=0.004, grid_spacing=0.006, initial_quantity=3,
        limit_multiplier=5.0, threshold_multiplier=20.0,
    )}
    cfg.max_enhancement = MaxEnhancement(**enh_kwargs)  # 預設全關 → manager 回中性值
    bot = MaxGridBot(cfg)
    bot.place_order = AsyncMock()
    bot.cancel_orders_for_side = AsyncMock()
    return bot


def _state(bot, **kw):
    st = bot.state.symbols["XRP/USDC:USDC"]
    for k, v in kw.items():
        setattr(st, k, v)
    return st


@pytest.mark.asyncio
async def test_normal_mode_long_places_tp_and_entry():
    """正常模式（持倉 < threshold）：撤舊 + 止盈 + 補倉，價格用 GridStrategy 公式。"""
    bot = _make_bot()
    sc = bot.config.symbols["XRP/USDC:USDC"]
    _state(bot, latest_price=2.5, long_position=10, short_position=0,
           sell_long_orders=0)  # threshold = 3*20 = 60，10 < 60 → 正常
    await bot._place_grid("XRP/USDC:USDC", sc, "long")

    bot.cancel_orders_for_side.assert_awaited_once_with("XRP/USDC:USDC", "long")
    # tp_price = 2.5*(1+0.004)=2.51, entry = 2.5*(1-0.006)=2.485
    calls = bot.place_order.await_args_list
    assert calls[0] == call("XRP/USDC:USDC", "sell", pytest.approx(2.51), 3.0, True, "long")
    assert calls[1] == call("XRP/USDC:USDC", "buy", pytest.approx(2.485), 3.0, False, "long")
```

- [ ] **Step 2: 跑 → 綠（現行為基準）**

Run: `uv run pytest tests/test_characterization_grid.py -v`
Expected: PASS（此為現行為基準，不是 red-green；目的是鎖死）。若 FAIL 表示對現行為理解錯，先修測試對齊現況。

- [ ] **Step 3: 補齊分支——裝死進場、裝死已有掛單、止盈加倍、追價判斷**

```python
@pytest.mark.asyncio
async def test_dead_mode_enter_places_special_tp_no_cancel():
    """裝死（持倉 > threshold=60）且無 pending tp：只掛特殊止盈，不撤單，設 dead flag。"""
    bot = _make_bot()
    sc = bot.config.symbols["XRP/USDC:USDC"]
    st = _state(bot, latest_price=2.5, long_position=70, short_position=0,
                sell_long_orders=0, long_dead_mode=False)
    await bot._place_grid("XRP/USDC:USDC", sc, "long")
    bot.cancel_orders_for_side.assert_not_awaited()
    assert st.long_dead_mode is True
    # 無對手倉 → fallback 1.05 → 2.625；tp_qty：long_pos(70) > limit(15) → 加倍 = 6
    bot.place_order.assert_awaited_once_with(
        "XRP/USDC:USDC", "sell", pytest.approx(2.625), 6.0, True, "long")


@pytest.mark.asyncio
async def test_dead_mode_with_pending_tp_does_nothing():
    """裝死且已有 pending tp（sell_long_orders>0）：不下單。"""
    bot = _make_bot()
    sc = bot.config.symbols["XRP/USDC:USDC"]
    _state(bot, latest_price=2.5, long_position=70, sell_long_orders=1, long_dead_mode=True)
    await bot._place_grid("XRP/USDC:USDC", sc, "long")
    bot.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_dead_mode_price_with_opposite_position():
    """裝死有對手倉：r = (my/opp)/100 + 1。my=70,opp=35 → r=1.02 → 2.55。"""
    bot = _make_bot()
    sc = bot.config.symbols["XRP/USDC:USDC"]
    _state(bot, latest_price=2.5, long_position=70, short_position=35, sell_long_orders=0)
    await bot._place_grid("XRP/USDC:USDC", sc, "long")
    price_arg = bot.place_order.await_args_list[0].args[2]
    assert price_arg == pytest.approx(2.55)


def test_should_adjust_grid_no_orders_returns_true():
    bot = _make_bot()
    sc = bot.config.symbols["XRP/USDC:USDC"]
    _state(bot, latest_price=2.5, buy_long_orders=0, sell_long_orders=0,
           last_grid_price_long=2.5)
    assert bot._should_adjust_grid(sc, bot.state.symbols["XRP/USDC:USDC"], "long") is True


def test_should_adjust_grid_deviation_below_threshold_false():
    """有掛單且偏離 < grid_spacing*0.5(=0.003)：不重掛。"""
    bot = _make_bot()
    sc = bot.config.symbols["XRP/USDC:USDC"]
    _state(bot, latest_price=2.5, buy_long_orders=1, sell_long_orders=1,
           last_grid_price_long=2.5)  # deviation 0 < 0.003
    assert bot._should_adjust_grid(sc, bot.state.symbols["XRP/USDC:USDC"], "long") is False


def test_should_adjust_grid_deviation_above_threshold_true():
    bot = _make_bot()
    sc = bot.config.symbols["XRP/USDC:USDC"]
    _state(bot, latest_price=2.52, buy_long_orders=1, sell_long_orders=1,
           last_grid_price_long=2.5)  # deviation 0.008 >= 0.003
    assert bot._should_adjust_grid(sc, bot.state.symbols["XRP/USDC:USDC"], "long") is True
```

- [ ] **Step 4: 補空頭鏡像測試**（`side='short'`，撤/下單方向互換：cover 用 buy@tp、開空用 sell@entry；裝死 fallback 0.95）。複製 Step1/3 的多頭案例改為空頭，斷言對應 place_order 參數。

- [ ] **Step 5: 跑全組 → 綠**

Run: `uv run pytest tests/test_characterization_grid.py -v`
Expected: PASS，記錄案例數（例：12 passed）。

- [ ] **Step 6: Commit**

```bash
git add tests/test_characterization_grid.py
git commit -m "test: characterization 鎖死現行網格決策行為（重構前基準）"
```

---

## Task 2: `clock.py` + enhancements.py 時間注入

新增 sim-clock，`enhancements.py` 全數 `time.time()` 改 `clock.now()`。實盤預設即 `time.time()`，零行為差異。

**Files:**
- Create: `grid_engine/clock.py`
- Modify: `grid_engine/enhancements.py`（9 處：行 404, 586, 587, 688, 799, 804, 940, 958, 986, 1003；`import time` 保留給 clock 預設）
- Test: `tests/test_clock.py`（新）

**Interfaces:**
- Produces:
  - `clock.now() -> float`：預設回 `time.time()`。
  - `clock.set_clock(fn: Callable[[], float]) -> None`：注入自訂時間源。
  - `clock.reset_clock() -> None`：還原成 `time.time()`（測試/回測收尾用）。

- [ ] **Step 1: 寫 clock 測試**

```python
# tests/test_clock.py
from grid_engine import clock


def test_default_now_is_walltime():
    import time
    assert abs(clock.now() - time.time()) < 1.0


def test_set_clock_overrides():
    clock.set_clock(lambda: 123.0)
    try:
        assert clock.now() == 123.0
    finally:
        clock.reset_clock()


def test_reset_restores_walltime():
    clock.set_clock(lambda: 1.0)
    clock.reset_clock()
    import time
    assert abs(clock.now() - time.time()) < 1.0
```

- [ ] **Step 2: 跑 → FAIL**

Run: `uv run pytest tests/test_clock.py -v`
Expected: FAIL（`ModuleNotFoundError: grid_engine.clock`）。

- [ ] **Step 3: 寫 clock.py**

```python
# grid_engine/clock.py
"""可注入時鐘。實盤預設真實牆鐘（零行為差異）；回測以 set_clock 餵 K 線時間，
讓 ATR 快取 / volume 窗口 / funding 更新間隔在回放時間軸下真正有效。"""
import time
from typing import Callable

_now_fn: Callable[[], float] = time.time


def now() -> float:
    return _now_fn()


def set_clock(fn: Callable[[], float]) -> None:
    global _now_fn
    _now_fn = fn


def reset_clock() -> None:
    global _now_fn
    _now_fn = time.time
```

- [ ] **Step 4: 跑 → 綠**

Run: `uv run pytest tests/test_clock.py -v`
Expected: PASS（3 passed）。

- [ ] **Step 5: enhancements.py 全數換 `clock.now()`**

在 `enhancements.py` 頂部 import 區加 `from . import clock`（保留 `import time`，clock 內部要用）。把下列各處 `time.time()` 改為 `clock.now()`：
- 404（`UCBBanditOptimizer.record_trade` timestamp）
- 586, 587（`DGTBoundaryManager.initialize_boundary`）
- 688, 799, 804（`FundingRateManager.update_funding_rate` now；`DynamicGridManager.update_price` time；`calculate_atr` now）
- 940, 958, 986, 1003（`LeadingIndicatorManager` record_trade/update_spread/ofi_history/volume 窗口）

逐處把 `time.time()` 換成 `clock.now()`。

- [ ] **Step 6: 驗證無殘留 + 全套綠**

Run: `cd "$(git rev-parse --show-toplevel)" && grep -n "time.time()" grid_engine/enhancements.py`
Expected: 無輸出（全數改完）。

Run: `uv run pytest tests/ -q`
Expected: PASS（現有全套 + clock，記錄數量，例：129 passed）。

- [ ] **Step 7: Commit**

```bash
git add grid_engine/clock.py grid_engine/enhancements.py tests/test_clock.py
git commit -m "feat: sim-clock 時間注入，enhancements 全數 time.time()→clock.now()（實盤等價）"
```

---

## Task 3: `decision.py` 純函數契約 + dataclass + 純 helper

建立 `decision.py`：dataclass 契約、併入 `strategy.py` 純計算、GLFT 數量調整（純）、`_should_adjust_grid`。此 task 只寫純層 + 單測，不接線 bot。

**Files:**
- Create: `grid_engine/decision.py`
- Test: `tests/test_decision.py`（新）

**Interfaces:**
- Produces（後續 task 依賴這些確切簽名）：
  - dataclasses：`EnhancementSnapshot`, `DecisionInputs`, `OrderIntent`, `SideDecision`, `GridDecision`（欄位見下）。
  - `decide(inputs: DecisionInputs) -> GridDecision`。
  - 純 helper（module-level function）：
    - `is_dead_mode(position, threshold) -> bool`
    - `dead_mode_price(base_price, my_position, opposite_position, side) -> float`（硬編 1.05/0.95）
    - `grid_prices(base_price, take_profit_spacing, grid_spacing, side) -> tuple[float, float]`（回 (tp_price, entry_price)）
    - `tp_quantity(base_qty, my_position, opposite_position, position_limit, position_threshold) -> float`
    - `glft_quantity(base_qty, side, long_pos, short_pos, glft_enabled, gamma) -> float`
    - `inventory_ratio(long_pos, short_pos) -> float`
    - `should_adjust(inputs, side) -> bool`
    - `compute_quantity(inputs, side, is_take_profit) -> float`

**dataclass 契約（本 task 定案，精煉自 spec 草稿）：**

```python
# grid_engine/decision.py
"""純函數網格決策層：無 I/O、不寫任何物件。實盤與回測共用。
搬移自 grid_engine/bot.py 的 _get_dynamic_spacing / _get_adjusted_quantity /
_should_adjust_grid / _place_grid 決策半段 + strategy.py 純計算（含 bug-for-bug）。"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class EnhancementSnapshot:
    """manager 在本 tick 的『已解析』輸出快照，由 snapshot.build_snapshot() 產生。
    只放需要 manager 狀態的值；純計算（GLFT 數量/裝死價/加倍）不進此處。"""
    dynamic_take_profit: float          # leading+ATR 解析後的最終止盈間距
    dynamic_grid_spacing: float         # leading+ATR 解析後的最終補倉間距
    funding_long_bias: float            # FundingRateManager.get_position_bias()[0]
    funding_short_bias: float           # [1]
    # 顯示欄位（面板用；不影響決策，供 bot 寫回 sym_state）：
    leading_ofi: float = 0.0
    leading_volume_ratio: float = 1.0
    leading_spread_ratio: float = 1.0
    leading_signals: tuple = ()


@dataclass(frozen=True)
class DecisionInputs:
    price: float
    long_position: float
    short_position: float
    buy_long_orders: float
    sell_long_orders: float
    buy_short_orders: float
    sell_short_orders: float
    last_grid_price_long: float
    last_grid_price_short: float
    long_dead_mode: bool
    short_dead_mode: bool
    grid_spacing: float                 # base（bandit 覆寫後），供 should_adjust 偏離門檻
    take_profit_spacing: float
    initial_quantity: float
    position_threshold: float
    position_limit: float
    glft_enabled: bool                  # = max_enhancement.is_feature_enabled('glft')
    gamma: float
    enh: EnhancementSnapshot


@dataclass(frozen=True)
class OrderIntent:
    side: str                # 'buy' | 'sell'
    position_side: str       # 'long' | 'short'
    price: float
    quantity: float
    reduce_only: bool


@dataclass(frozen=True)
class SideDecision:
    should_adjust: bool
    enter_dead_mode: bool
    exit_dead_mode: bool
    cancel_side: bool
    orders: tuple
    new_anchor_price: Optional[float]
    dynamic_tp: float
    dynamic_gs: float
    display: dict = field(default_factory=dict)


@dataclass(frozen=True)
class GridDecision:
    long: SideDecision
    short: SideDecision
```

- [ ] **Step 1: 寫純 helper 單測**

```python
# tests/test_decision.py
import pytest
from grid_engine import decision as d


def test_is_dead_mode_strict_gt():
    assert d.is_dead_mode(61, 60) is True
    assert d.is_dead_mode(60, 60) is False


def test_dead_mode_price_no_opposite_uses_fallback():
    assert d.dead_mode_price(2.5, 70, 0, "long") == pytest.approx(2.625)   # *1.05
    assert d.dead_mode_price(2.5, 70, 0, "short") == pytest.approx(2.375)  # *0.95


def test_dead_mode_price_with_opposite():
    # r = (70/35)/100 + 1 = 1.02
    assert d.dead_mode_price(2.5, 70, 35, "long") == pytest.approx(2.55)
    assert d.dead_mode_price(2.5, 70, 35, "short") == pytest.approx(2.5 / 1.02)


def test_grid_prices_long_short():
    assert d.grid_prices(2.5, 0.004, 0.006, "long") == pytest.approx((2.51, 2.485))
    assert d.grid_prices(2.5, 0.004, 0.006, "short") == pytest.approx((2.49, 2.515))


def test_tp_quantity_doubles_over_limit():
    assert d.tp_quantity(3, 20, 0, 15, 60) == 6      # my>limit
    assert d.tp_quantity(3, 10, 60, 15, 60) == 6     # opp>=threshold
    assert d.tp_quantity(3, 10, 0, 15, 60) == 3      # 不加倍


def test_glft_quantity_disabled_passthrough():
    assert d.glft_quantity(3, "long", 100, 0, glft_enabled=False, gamma=0.1) == 3


def test_glft_quantity_clamped():
    # inventory=1, long: adjust=1-1*0.1=0.9 → 2.7
    assert d.glft_quantity(3, "long", 100, 0, glft_enabled=True, gamma=0.1) == pytest.approx(2.7)
```

- [ ] **Step 2: 跑 → FAIL**

Run: `uv run pytest tests/test_decision.py -v`
Expected: FAIL（`AttributeError`/import error）。

- [ ] **Step 3: 寫純 helper（搬移 strategy.py + GLFT 數量，bug-for-bug）**

在 `decision.py`（接續 dataclass）加：

```python
_FALLBACK_LONG = 1.05
_FALLBACK_SHORT = 0.95
_DEAD_DIVISOR = 100


def is_dead_mode(position: float, threshold: float) -> bool:
    return position > threshold


def dead_mode_price(base_price, my_position, opposite_position, side):
    if opposite_position > 0:
        r = (my_position / opposite_position) / _DEAD_DIVISOR + 1
        return base_price * r if side == "long" else base_price / r
    return base_price * (_FALLBACK_LONG if side == "long" else _FALLBACK_SHORT)


def grid_prices(base_price, take_profit_spacing, grid_spacing, side):
    if side == "long":
        return base_price * (1 + take_profit_spacing), base_price * (1 - grid_spacing)
    return base_price * (1 - take_profit_spacing), base_price * (1 + grid_spacing)


def tp_quantity(base_qty, my_position, opposite_position, position_limit, position_threshold):
    if my_position > position_limit or opposite_position >= position_threshold:
        return base_qty * 2
    return base_qty


def inventory_ratio(long_pos, short_pos):
    total = long_pos + short_pos
    return 0.0 if total <= 0 else (long_pos - short_pos) / total


def glft_quantity(base_qty, side, long_pos, short_pos, glft_enabled, gamma):
    if not glft_enabled:
        return base_qty
    inv = inventory_ratio(long_pos, short_pos)
    adjust = 1.0 - inv * gamma if side == "long" else 1.0 + inv * gamma
    adjust = max(0.5, min(1.5, adjust))
    return base_qty * adjust
```

- [ ] **Step 4: 跑 helper 單測 → 綠**

Run: `uv run pytest tests/test_decision.py -v`
Expected: PASS。

- [ ] **Step 5: 寫 `should_adjust` / `compute_quantity` / `decide` 測試**

```python
def _inputs(**kw):
    base = dict(
        price=2.5, long_position=10, short_position=0,
        buy_long_orders=1, sell_long_orders=1, buy_short_orders=1, sell_short_orders=1,
        last_grid_price_long=2.5, last_grid_price_short=2.5,
        long_dead_mode=False, short_dead_mode=False,
        grid_spacing=0.006, take_profit_spacing=0.004,
        initial_quantity=3, position_threshold=60, position_limit=15,
        glft_enabled=False, gamma=0.1,
        enh=d.EnhancementSnapshot(dynamic_take_profit=0.004, dynamic_grid_spacing=0.006,
                                  funding_long_bias=1.0, funding_short_bias=1.0),
    )
    base.update(kw)
    return d.DecisionInputs(**base)


def test_should_adjust_no_orders():
    assert d.should_adjust(_inputs(buy_long_orders=0), "long") is True


def test_should_adjust_deviation():
    assert d.should_adjust(_inputs(price=2.5, last_grid_price_long=2.5), "long") is False
    assert d.should_adjust(_inputs(price=2.52, last_grid_price_long=2.5), "long") is True


def test_compute_quantity_tp_double_then_funding_clamp():
    inp = _inputs(long_position=20, enh=d.EnhancementSnapshot(
        0.004, 0.006, funding_long_bias=1.2, funding_short_bias=0.8))
    # tp: double(20>15) →6 ×1.2 =7.2
    assert d.compute_quantity(inp, "long", True) == pytest.approx(7.2)


def test_decide_normal_long_full():
    dec = d.decide(_inputs(long_position=10))
    s = dec.long
    assert s.should_adjust is True and s.cancel_side is True
    assert s.new_anchor_price == pytest.approx(2.5)
    intents = {(o.side, o.reduce_only): o for o in s.orders}
    assert intents[("sell", True)].price == pytest.approx(2.51)   # tp
    assert intents[("buy", False)].price == pytest.approx(2.485)  # entry


def test_decide_dead_long_enter():
    dec = d.decide(_inputs(long_position=70, sell_long_orders=0, long_dead_mode=False))
    s = dec.long
    assert s.enter_dead_mode is True and s.cancel_side is False
    assert len(s.orders) == 1 and s.orders[0].price == pytest.approx(2.625)


def test_decide_side_not_adjust_returns_empty():
    # 有掛單且零偏離 → should_adjust False → 無 orders、無 transition
    dec = d.decide(_inputs(long_position=10, price=2.5, last_grid_price_long=2.5))
    assert dec.long.should_adjust is False
    assert dec.long.orders == () and dec.long.new_anchor_price is None
```

- [ ] **Step 6: 跑 → FAIL**（`should_adjust`/`compute_quantity`/`decide` 未定義）

Run: `uv run pytest tests/test_decision.py -k "should_adjust or compute_quantity or decide" -v`
Expected: FAIL。

- [ ] **Step 7: 寫 `should_adjust` / `compute_quantity` / `decide_side` / `decide`**

```python
def should_adjust(inputs, side):
    if side == "long":
        buy_o, sell_o, anchor = inputs.buy_long_orders, inputs.sell_long_orders, inputs.last_grid_price_long
    else:
        buy_o, sell_o, anchor = inputs.buy_short_orders, inputs.sell_short_orders, inputs.last_grid_price_short
    if buy_o <= 0 or sell_o <= 0:
        return True
    if anchor > 0:
        deviation = abs(inputs.price - anchor) / anchor
        return deviation >= inputs.grid_spacing * 0.5
    return True


def compute_quantity(inputs, side, is_take_profit):
    my_pos = inputs.long_position if side == "long" else inputs.short_position
    opp_pos = inputs.short_position if side == "long" else inputs.long_position
    q = inputs.initial_quantity
    if is_take_profit:
        q = tp_quantity(q, my_pos, opp_pos, inputs.position_limit, inputs.position_threshold)
    else:
        q = glft_quantity(q, side, inputs.long_position, inputs.short_position,
                          inputs.glft_enabled, inputs.gamma)
    bias = inputs.enh.funding_long_bias if side == "long" else inputs.enh.funding_short_bias
    q *= bias
    return max(inputs.initial_quantity * 0.5, q)


def _decide_side(inputs, side):
    tp_sp, gs_sp = inputs.enh.dynamic_take_profit, inputs.enh.dynamic_grid_spacing
    display = {
        "leading_ofi": inputs.enh.leading_ofi,
        "leading_volume_ratio": inputs.enh.leading_volume_ratio,
        "leading_spread_ratio": inputs.enh.leading_spread_ratio,
        "leading_signals": list(inputs.enh.leading_signals),
        "inventory_ratio": inventory_ratio(inputs.long_position, inputs.short_position),
        "dynamic_take_profit": tp_sp,
        "dynamic_grid_spacing": gs_sp,
    }
    if not should_adjust(inputs, side):
        return SideDecision(False, False, False, False, (), None, tp_sp, gs_sp, display)

    if side == "long":
        my_pos, opp_pos = inputs.long_position, inputs.short_position
        dead_flag, pending_tp = inputs.long_dead_mode, inputs.sell_long_orders
    else:
        my_pos, opp_pos = inputs.short_position, inputs.long_position
        dead_flag, pending_tp = inputs.short_dead_mode, inputs.buy_short_orders

    price = inputs.price
    orders = []
    enter_dead = exit_dead = cancel = False

    if is_dead_mode(my_pos, inputs.position_threshold):
        if not dead_flag:
            enter_dead = True
        if pending_tp <= 0:
            special = dead_mode_price(price, my_pos, opp_pos, side)
            tp_qty = compute_quantity(inputs, side, True)
            o_side = "sell" if side == "long" else "buy"
            orders.append(OrderIntent(o_side, side, special, tp_qty, True))
    else:
        if dead_flag:
            exit_dead = True
        cancel = True
        tp_price, entry_price = grid_prices(price, tp_sp, gs_sp, side)
        tp_qty = compute_quantity(inputs, side, True)
        base_qty = compute_quantity(inputs, side, False)
        if my_pos > 0:
            o_side = "sell" if side == "long" else "buy"
            orders.append(OrderIntent(o_side, side, tp_price, tp_qty, True))
        e_side = "buy" if side == "long" else "sell"
        orders.append(OrderIntent(e_side, side, entry_price, base_qty, False))

    return SideDecision(True, enter_dead, exit_dead, cancel, tuple(orders),
                        price, tp_sp, gs_sp, display)


def decide(inputs):
    return GridDecision(long=_decide_side(inputs, "long"),
                        short=_decide_side(inputs, "short"))
```

- [ ] **Step 8: 跑全 decision 單測 → 綠**

Run: `uv run pytest tests/test_decision.py -v`
Expected: PASS（記錄數量）。

- [ ] **Step 9: Commit**

```bash
git add grid_engine/decision.py tests/test_decision.py
git commit -m "feat: decision.py 純函數決策層 + dataclass 契約（搬移 strategy.py + GLFT 數量）"
```

---

## Task 4: `snapshot.py` 共享快照收集 + 呼叫序列等價測試

把「呼叫 manager 組出 `EnhancementSnapshot`」抽成共享函數，逐字複刻現行 `_get_dynamic_spacing` 的 manager 呼叫序列與條件分支。用 call-recording 測試斷言搬移前後序列一致。

**Files:**
- Create: `grid_engine/snapshot.py`
- Test: `tests/test_snapshot_sequence.py`（新）

**Interfaces:**
- Consumes: `EnhancementSnapshot`（Task 3）。
- Produces:
  - `@dataclass ManagerBundle`：`leading_indicator`, `dynamic_grid_manager`, `glft_controller`, `funding_manager`（Optional）, `max_enhancement`, `leading_enabled: bool`。
  - `build_snapshot(bundle, ccxt_symbol, base_tp, base_gs) -> EnhancementSnapshot`。

**呼叫序列（必須逐字對照 bot.py:458-510）：**
1. `leading.enabled` → `get_signals(sym)` → 存 ofi/volume/spread/signals。
2. `should_pause_trading(sym)` → pause 則 base_tp*=2, base_gs*=2, reason="暫停:...";
3. elif signals → `get_spacing_adjustment(sym, base_gs)` → 若 adjusted!=base_gs：ratio 調整。
4. `not reason or reason=="正常"` → `dynamic_grid_manager.get_dynamic_spacing(...)`（條件呼叫，維持 calculate_atr 快取副作用時機）；else 用 base。
5. funding：`get_position_bias(sym, max_cfg)`（無 funding_manager → (1.0,1.0)）。

- [ ] **Step 1: 寫呼叫序列等價測試**

```python
# tests/test_snapshot_sequence.py
"""斷言 build_snapshot 的 manager 呼叫序列 == 現行 bot._get_dynamic_spacing。
用真 manager 實例 + call-recording，並比對搬移後回傳的 dynamic_tp/gs。"""
import pytest
from grid_engine.snapshot import ManagerBundle, build_snapshot
from grid_engine.enhancements import (
    LeadingIndicatorManager, LeadingIndicatorConfig, DynamicGridManager,
    GLFTController, MaxEnhancement,
)


def _bundle(enh=None):
    return ManagerBundle(
        leading_indicator=LeadingIndicatorManager(LeadingIndicatorConfig(enabled=True)),
        dynamic_grid_manager=DynamicGridManager(),
        glft_controller=GLFTController(),
        funding_manager=None,
        max_enhancement=enh or MaxEnhancement(),
        leading_enabled=True,
    )


def test_snapshot_neutral_when_all_disabled():
    """manager 全中性（無數據）：dynamic == base，funding bias 1.0。"""
    snap = build_snapshot(_bundle(), "XRP/USDC:USDC", 0.004, 0.006)
    assert snap.dynamic_take_profit == pytest.approx(0.004)
    assert snap.dynamic_grid_spacing == pytest.approx(0.006)
    assert snap.funding_long_bias == 1.0 and snap.funding_short_bias == 1.0


def test_get_signals_call_count_recorded():
    """記錄 leading.get_signals 呼叫次數：enabled + 無 pause + 無 signals →
    直接 get_signals 1 次 + should_pause 內 1 次 = 現行序列。"""
    b = _bundle()
    calls = []
    orig = b.leading_indicator.get_signals
    b.leading_indicator.get_signals = lambda s: (calls.append(s) or orig(s))
    build_snapshot(b, "XRP/USDC:USDC", 0.004, 0.006)
    # 現行：get_signals(464) + should_pause→get_signals(1140)；無 signals 不進 get_spacing_adjustment
    assert len(calls) == 2
```

- [ ] **Step 2: 跑 → FAIL**

Run: `uv run pytest tests/test_snapshot_sequence.py -v`
Expected: FAIL（`ModuleNotFoundError: grid_engine.snapshot`）。

- [ ] **Step 3: 寫 snapshot.py**

```python
# grid_engine/snapshot.py
"""共享快照收集：呼叫 manager 實例組出 EnhancementSnapshot。
不純（讀 manager 狀態、get_signals 有 append 副作用），但 bot 與 backtester 共用同一份，
保證 manager 呼叫序列一致——這是回測/實盤等價的關鍵，別在兩邊各寫一份。"""
from dataclasses import dataclass
from typing import Optional

from .decision import EnhancementSnapshot, inventory_ratio


@dataclass
class ManagerBundle:
    leading_indicator: object
    dynamic_grid_manager: object
    glft_controller: object
    funding_manager: Optional[object]
    max_enhancement: object
    leading_enabled: bool


def build_snapshot(bundle, ccxt_symbol, base_tp, base_gs):
    max_cfg = bundle.max_enhancement
    ofi = vol_ratio = spread_ratio = None
    signals = []
    leading_reason = ""

    if bundle.leading_enabled:
        signals, values = bundle.leading_indicator.get_signals(ccxt_symbol)  # bot.py:464
        ofi = values.get("ofi", 0)
        vol_ratio = values.get("volume_ratio", 1.0)
        spread_ratio = values.get("spread_ratio", 1.0)

        should_pause, pause_reason = bundle.leading_indicator.should_pause_trading(ccxt_symbol)  # 471
        if should_pause:
            base_tp *= 2.0
            base_gs *= 2.0
            leading_reason = f"暫停:{pause_reason}"
        elif signals:
            adjusted, leading_reason = bundle.leading_indicator.get_spacing_adjustment(  # 478
                ccxt_symbol, base_gs)
            if adjusted != base_gs:
                ratio = adjusted / base_gs
                base_gs = adjusted
                base_tp *= ratio

    if not leading_reason or leading_reason == "正常":
        tp, gs = bundle.dynamic_grid_manager.get_dynamic_spacing(  # 488（條件呼叫，維持 ATR 快取時機）
            ccxt_symbol, base_tp, base_gs, max_cfg)
    else:
        tp, gs = base_tp, base_gs

    if bundle.funding_manager is not None:
        long_bias, short_bias = bundle.funding_manager.get_position_bias(ccxt_symbol, max_cfg)
    else:
        long_bias, short_bias = 1.0, 1.0

    return EnhancementSnapshot(
        dynamic_take_profit=tp,
        dynamic_grid_spacing=gs,
        funding_long_bias=long_bias,
        funding_short_bias=short_bias,
        leading_ofi=ofi if ofi is not None else 0.0,
        leading_volume_ratio=vol_ratio if vol_ratio is not None else 1.0,
        leading_spread_ratio=spread_ratio if spread_ratio is not None else 1.0,
        leading_signals=tuple(signals),
    )
```

- [ ] **Step 4: 跑 → 綠**

Run: `uv run pytest tests/test_snapshot_sequence.py -v`
Expected: PASS。

- [ ] **Step 5: 補 pause / spacing-adjustment 分支測試**

用 monkeypatch 讓 `should_pause_trading` 回 `(True, "極端波動")`，斷言 `dynamic_take_profit`/`gs` 為 base*2（且未呼叫 `get_dynamic_spacing`）；另一案讓 signals 非空 + `get_spacing_adjustment` 回 `(base_gs*1.2, "放量")`，斷言 gs 變 base*1.2、tp 乘同 ratio、且 `get_dynamic_spacing` 未被呼叫。

- [ ] **Step 6: 跑 → 綠 + Commit**

Run: `uv run pytest tests/test_snapshot_sequence.py -v`
Expected: PASS。

```bash
git add grid_engine/snapshot.py tests/test_snapshot_sequence.py
git commit -m "feat: snapshot.py 共享快照收集，逐字複刻 manager 呼叫序列 + 序列等價測試"
```

---

## Task 5: bot.py 接線純層 + 決策日誌

`_grid_step` 有倉位分支改走 `build_snapshot → decide → execute`，寫回 dead_mode/anchor/dynamic/display，落地決策日誌。characterization（Task 1）不改而綠。

**Files:**
- Modify: `grid_engine/bot.py`（`_grid_step` 有倉位分支；刪 `_get_dynamic_spacing` 517 前、`_get_adjusted_quantity`、`_place_grid` 決策半段；改 `_place_grid` 為 `_execute_side_decision`）
- Modify: `grid_engine/__init__.py`（暫不刪 strategy re-export，留 Task 7）
- Test: `tests/test_characterization_grid.py`（不改斷言，可能需改呼叫入口名）、新增 `tests/test_decision_log.py`

**Interfaces:**
- Consumes: `decision.decide`, `snapshot.build_snapshot`, `ManagerBundle`。
- Produces:
  - `MaxGridBot._build_bundle(sym_config) -> ManagerBundle`
  - `MaxGridBot._build_inputs(sym_config, sym_state, snapshot) -> DecisionInputs`
  - `MaxGridBot._execute_side_decision(ccxt_symbol, sym_config, side, side_decision)`（撤/下單走既有 `_rest`/`place_order`/`cancel_orders_for_side` 守衛路徑；寫回 sym_state）
  - `MaxGridBot._log_decision(ccxt_symbol, inputs, decision)`（落地一行 JSON）

**⚠ characterization 相容**：Task 1 直接呼叫 `bot._place_grid(...)` 與 `bot._should_adjust_grid(...)`。接線後這些方法語意改變。處理方式：Task 1 Step 1 註記「這些測試在 Task 5 會改為呼叫 `_grid_step` 或保留 `_place_grid` 薄封裝」。**採後者**：保留 `_place_grid` 為薄封裝，內部 `build_snapshot→decide→_execute_side_decision(side)`，維持相同 place_order 序列 → characterization 斷言不動而綠。`_should_adjust_grid` 保留為 `decision.should_adjust` 的薄封裝。

- [ ] **Step 1: 寫決策日誌測試（先定行為）**

```python
# tests/test_decision_log.py
import json
from grid_engine.decision import DecisionInputs, EnhancementSnapshot, decide
# 用 Task1 的 _make_bot harness（複製或 import）


def test_decision_log_writes_one_json_line(tmp_path, monkeypatch):
    """decide() 每次落地一行 JSON，含 inputs 關鍵欄位 + 每側 should_adjust。"""
    from tests.test_characterization_grid import _make_bot, _state
    bot = _make_bot()
    logf = tmp_path / "decisions.jsonl"
    bot._decision_log_path = str(logf)
    sc = bot.config.symbols["XRP/USDC:USDC"]
    _state(bot, latest_price=2.5, long_position=10, short_position=0,
           buy_long_orders=0, sell_long_orders=0)
    import asyncio
    asyncio.run(bot._grid_step("XRP/USDC:USDC", sc))
    lines = logf.read_text().strip().splitlines()
    assert len(lines) >= 1
    rec = json.loads(lines[-1])
    assert rec["symbol"] == "XRP/USDC:USDC"
    assert "inputs" in rec and "decision" in rec
    assert rec["inputs"]["price"] == 2.5
```

- [ ] **Step 2: 跑 → FAIL**

Run: `uv run pytest tests/test_decision_log.py -v`
Expected: FAIL（無 `_decision_log_path` / 日誌未寫）。

- [ ] **Step 3: 接線 `_grid_step` + helper**

在 bot.py：
- 頂部 import：`from .decision import decide, DecisionInputs; from .snapshot import ManagerBundle, build_snapshot`。
- 新增 `_build_bundle`（組現有 self.leading_indicator / dynamic_grid_manager / glft_controller / funding_manager / config.max_enhancement，`leading_enabled=self.config.leading_indicator.enabled`）。
- 新增 `_build_inputs`（從 sym_config/sym_state 填 DecisionInputs；`glft_enabled=self.config.max_enhancement.is_feature_enabled('glft')`, `gamma=self.config.max_enhancement.gamma`, `enh=snapshot`）。
- 改 `_grid_step` 有倉位分支：對 long/short，`snapshot=build_snapshot(bundle, ccxt_symbol, sym_config.take_profit_spacing, sym_config.grid_spacing)`（注意 base = bandit 覆寫後的 sym_config 值）；`inputs=self._build_inputs(...)`；`decision=decide(inputs)`；每側 `if side_decision.should_adjust and self._grid_cooldown_passed(...)`: `await self._execute_side_decision(...)` + 寫回 anchor + `last_order_times`。零倉位 flat-entry 分支**不動**。收尾 `self._log_decision(...)`。
- `_execute_side_decision`：`if side_decision.enter_dead_mode/exit_dead_mode`: 設 sym_state dead flag + log；`if cancel_side`: `await self.cancel_orders_for_side(...)`；`for o in orders`: `await self.place_order(ccxt_symbol, o.side, o.price, o.quantity, o.reduce_only, o.position_side)`；寫回 sym_state.dynamic_take_profit/dynamic_grid_spacing + display 欄位。
- 保留 `_place_grid` 為薄封裝呼叫上述（維持 characterization）；`_should_adjust_grid` 改為 `return decision_mod.should_adjust(self._build_inputs_lite(...), side)` 或保留原純邏輯（兩者等價，characterization 綠即可）。
- `_log_decision`：`path = getattr(self, "_decision_log_path", None)`；有則 `dataclasses.asdict` inputs（enh 轉 dict）+ decision，`json.dumps(ensure_ascii=False)` append 一行。預設路徑在 `run()` 設為設定檔指定或 `logs/decisions.jsonl`（無則不寫）。

- [ ] **Step 4: 跑決策日誌 + characterization → 綠**

Run: `uv run pytest tests/test_decision_log.py tests/test_characterization_grid.py -v`
Expected: PASS（characterization 斷言未改而綠 = 行為等價）。

- [ ] **Step 5: 跑全套**

Run: `uv run pytest tests/ -q`
Expected: PASS（記錄數量）。

- [ ] **Step 6: Commit**

```bash
git add grid_engine/bot.py tests/test_decision_log.py
git commit -m "feat: bot _grid_step 接線 decide()/build_snapshot + 決策日誌落地（characterization 綠）"
```

---

## Task 6: monkey testing 純層 + snapshot（專案規則）

極端輸入打 `decide()` 與 `build_snapshot`，斷言不拋例外、輸出在合理域。

**Files:**
- Test: `tests/test_decision_monkey.py`（新）

- [ ] **Step 1: 寫極端輸入測試**

```python
# tests/test_decision_monkey.py
import math
import pytest
from grid_engine import decision as d


def _inp(**kw):
    from tests.test_decision import _inputs  # 複用
    return _inputs(**kw)


@pytest.mark.parametrize("price", [0.0, -1.0, 1e12])
def test_decide_extreme_price_no_crash(price):
    dec = d.decide(_inp(price=price, long_position=10))
    assert isinstance(dec.long.orders, tuple)


def test_decide_position_far_over_limit():
    dec = d.decide(_inp(long_position=1e9, sell_long_orders=0))
    assert dec.long.enter_dead_mode is True


def test_decide_zero_anchor_forces_adjust():
    assert d.should_adjust(_inp(last_grid_price_long=0, buy_long_orders=1, sell_long_orders=1), "long") is True


def test_glft_extreme_gamma_still_clamped():
    q = d.glft_quantity(3, "long", 100, 0, glft_enabled=True, gamma=1e6)
    assert q == pytest.approx(1.5)  # adjust clamp 上限 → 3*0.5? 下限0.5 → 1.5
```

- [ ] **Step 2: 跑 → 綠（或抓到真 crash 再修純層防禦）**

Run: `uv run pytest tests/test_decision_monkey.py -v`
Expected: PASS。若某極值拋例外 → 在 `decision.py` 對應 helper 加最小防禦（如除零已由 `inventory_ratio`/`dead_mode_price` 的 `>0` 守衛涵蓋，確認即可）。

- [ ] **Step 3: Commit**

```bash
git add tests/test_decision_monkey.py
git commit -m "test: 純層 monkey testing（極端 price/倉位/gamma/錨價）"
```

---

## Task 7: 刪 `grid_engine/strategy.py`，遷移 `GridStrategy` 引用

**Files:**
- Delete: `grid_engine/strategy.py`
- Modify: `grid_engine/__init__.py`（`from .strategy import GridStrategy` → 相容 shim）、`grid_engine/bot.py`（726/737/756 的 `GridStrategy.*` → `decision.*`）、`as_terminal_max.py:40`（確認 import 來源）

**Interfaces:**
- `grid_engine.GridStrategy` 需維持可 import（`as_terminal_max.py:40` 依賴）。做法：`__init__.py` 提供相容 shim class 或 `from .decision import ...` 包一層同名靜態方法。**選最小改動**：`__init__.py` 保留 `GridStrategy` 名稱指向一個薄相容類，靜態方法轉呼叫 `decision` 的 module function。

- [ ] **Step 1: 盤點 `GridStrategy.` 呼叫點**

Run: `cd "$(git rev-parse --show-toplevel)" && grep -rn "GridStrategy" --include="*.py" | grep -v "core/\|backtest/"`
Expected: `grid_engine/__init__.py`, `grid_engine/bot.py`(726/737/756), `as_terminal_max.py:40`。

- [ ] **Step 2: bot.py 內 `GridStrategy.*` 改 `decision.*`**

`bot.py:726` `GridStrategy.is_dead_mode(...)` → `decision.is_dead_mode(...)`；`737` `calculate_dead_mode_price` → `decision.dead_mode_price`；`756` `calculate_grid_prices` → `decision.grid_prices`。（若 Task 5 已把 `_place_grid` 改薄封裝走 `decide()`，這些行可能已不存在——確認後跳過。）import `from . import decision`。

- [ ] **Step 3: `__init__.py` 相容 shim**

```python
# grid_engine/__init__.py（替換 from .strategy import GridStrategy）
from . import decision as _decision


class GridStrategy:
    """相容 shim：舊引用（as_terminal_max.py）轉呼叫 decision 純函數。strategy.py 已刪。"""
    is_dead_mode = staticmethod(_decision.is_dead_mode)
    calculate_grid_prices = staticmethod(_decision.grid_prices)

    @staticmethod
    def calculate_dead_mode_price(base_price, my_position, opposite_position, side):
        return _decision.dead_mode_price(base_price, my_position, opposite_position, side)

    @staticmethod
    def get_grid_decision(*a, **k):
        raise NotImplementedError("改用 grid_engine.decision.decide()")
```

- [ ] **Step 4: 刪 strategy.py + 驗證 import**

```bash
git rm grid_engine/strategy.py
```

Run: `cd "$(git rev-parse --show-toplevel)" && uv run python -c "import grid_engine; import as_terminal_max; print('import ok')"`
Expected: `import ok`。

- [ ] **Step 5: 全套綠**

Run: `uv run pytest tests/ -q`
Expected: PASS（記錄數量）。

- [ ] **Step 6: Commit**

```bash
git add grid_engine/__init__.py grid_engine/bot.py as_terminal_max.py
git commit -m "refactor: 刪 grid_engine/strategy.py，GridStrategy 引用遷移至 decision.py（相容 shim）"
```

---

## Task 8: backtester 遷移——吃 `decide()` + sim-clock + 追價語意

移除 `core.strategy` 依賴，回測改吃 `decide()`；每根 K 線推進 sim-clock 餵真 manager；重掛改用 `should_adjust` + `new_anchor_price`（靜態階梯 → 追價網格）；刪 `grid_engine/backtest.py`。

**Files:**
- Modify: `backtest/backtester.py`（`_run_terminal_ui_mode` 重寫決策部分；移除 `from core.strategy import GridStrategy`；`_process_long_orders`/`_process_short_orders` 的 legacy 模式改吃 decision 或標記僅 legacy）
- Delete: `grid_engine/backtest.py`
- Test: `tests/test_backtester_decision.py`（新）

**Interfaces:**
- Consumes: `grid_engine.decision.decide/DecisionInputs/EnhancementSnapshot`, `grid_engine.snapshot.build_snapshot/ManagerBundle`, `grid_engine.clock`。
- 回測 loop 每根 K 線：`clock.set_clock(lambda t=bar_time: t)` → 餵 manager（`dynamic_grid_manager.update_price` 等）→ `build_snapshot` → 組 `DecisionInputs`（回測維護 pending 掛單數以驅動 `should_adjust`）→ `decide()` → 模擬撤掛與成交（價格穿越即成交，樂觀偏差寫入報告）。

**⚠ 語意變更（intended）**：回測從「每 refresh_interval 靜態重掛、錨在上次成交價」改為「追價：`should_adjust`（現價偏離上次掛網價 ≥ gs*0.5 或無掛單）觸發、錨在觸發時價」。回測結果會與舊版不同——這正是 P0 動機（讓回測=實盤）。舊數字不作為回歸基準。

- [ ] **Step 1: 寫回測整合測試（等價性導向）**

```python
# tests/test_backtester_decision.py
"""回測吃 decide()：驗證同一 DecisionInputs 下，回測決策 == 純層 decide()（無 core.strategy）。"""
import pandas as pd
from backtest.backtester import GridBacktester
from backtest.config import Config


def _df(prices):
    import numpy as np
    n = len(prices)
    return pd.DataFrame({
        "open_time": pd.date_range("2024-01-01", periods=n, freq="1min"),
        "open": prices, "high": prices, "low": prices, "close": prices,
        "volume": [100.0] * n,
    })


def test_backtester_runs_without_core_strategy():
    cfg = Config(symbol="XRPUSDC", initial_quantity=3, grid_spacing=0.006,
                 take_profit_spacing=0.004, direction="both", terminal_ui_mode=True)
    df = _df([2.5, 2.48, 2.46, 2.5, 2.55, 2.5])
    res = GridBacktester(df, cfg).run()
    assert res.trades_count >= 0
    assert res.final_equity > 0


def test_no_core_strategy_import():
    import backtest.backtester as b
    import inspect
    assert "core.strategy" not in inspect.getsource(b)
```

- [ ] **Step 2: 跑 → FAIL**（`test_no_core_strategy_import` red；integration 可能綠但仍用舊碼）

Run: `uv run pytest tests/test_backtester_decision.py -v`
Expected: `test_no_core_strategy_import` FAIL。

- [ ] **Step 3: 重寫 `_run_terminal_ui_mode` 吃 decide() + 追價**

移除 `from core.strategy import GridStrategy`。`_run_terminal_ui_mode`：
- 建 `ManagerBundle`（回測用真 manager 實例：`DynamicGridManager()`、`LeadingIndicatorManager(cfg.leading or disabled)`、`GLFTController()`、`funding_manager=None`、`max_enhancement`）。回測預設 leading/enhancement 關 → 中性，與現行回測（無這些）一致。
- 維護 pending 掛單狀態：`orders = {"long": {...}, "short": {...}}`（記 tp/entry 價與量、pending 布林），`last_grid_price_{long,short}`。
- 每 bar：`clock.set_clock(lambda t=bar_timestamp_epoch: t)`；`dynamic_grid_manager.update_price(sym, price)`。
- 先結算成交：price 穿越 pending entry → 開倉、穿越 tp → 平倉（沿用現行 while 迴圈成交/部分成交邏輯，含手續費）；成交後對應 pending 清除，更新 position。
- 組 `DecisionInputs`（`buy_long_orders`/`sell_long_orders` 等由 pending 狀態推出：有 pending entry→buy_long_orders=1，有 pending tp→sell_long_orders=1；`last_grid_price_*` = 上次掛網價）。`snapshot=build_snapshot(bundle, sym, cfg.take_profit_spacing, cfg.grid_spacing)`。`decide(inputs)`。
- 每側 `if side_decision.should_adjust`: 取消舊 pending（回測直接清）、依 `orders` 重掛 pending、`last_grid_price_* = side_decision.new_anchor_price`。裝死側依 SideDecision 只掛特殊 tp。
- 收尾 `clock.reset_clock()`（finally）。
- 成交模擬保真限制（無 queue、無 partial fill 佇列、樂觀成交）寫入 `BacktestResult` 或報告字串。

（flat-entry：回測零倉位 bootstrap 依 spec 以「掛 K 線 close、taker 進場」規則模擬——回測 loop 初始持倉為 0 時，第一次 entry 成交即 bootstrap，沿用現行 close 觸發即可，差異寫入限制。）

- [ ] **Step 4: legacy 模式處理**

`_process_long_orders`/`_process_short_orders`（legacy `_run_legacy_mode`）也 import GridStrategy。改吃 `decide()` 或——若 `terminal_ui_mode` 預設 True 且無人用 legacy——標記 legacy 為 deprecated 並改用 decision helper（`is_dead_mode`/`grid_prices`/`dead_mode_price`/`tp_quantity`）替換 `GridStrategy.get_grid_decision`。確認 web 回測頁（`web/pages/3_🔬_回測優化.py`）走哪個模式；預設 terminal_ui_mode=True → 主力是 Step3 路徑。

- [ ] **Step 5: 刪 grid_engine/backtest.py**

Run: `cd "$(git rev-parse --show-toplevel)" && grep -rn "grid_engine.backtest\|from .backtest import\|grid_engine import backtest" --include="*.py"`
Expected: 確認無人 import（僅 `grid_engine/__init__` 可能 re-export）。清乾淨後：

```bash
git rm grid_engine/backtest.py
```

- [ ] **Step 6: 跑 → 綠**

Run: `uv run pytest tests/test_backtester_decision.py -v`
Expected: PASS（含 `test_no_core_strategy_import`）。

- [ ] **Step 7: 回測 monkey testing（人造極端 K 線）**

在 `tests/test_backtester_decision.py` 加：跳空 50% 單根、零成交量整段、時間倒流（bar_time 遞減）、單一 bar。斷言 `run()` 不拋例外、`final_equity` 有限。

Run: `uv run pytest tests/test_backtester_decision.py -v`
Expected: PASS。

- [ ] **Step 8: 全套綠 + Commit**

Run: `uv run pytest tests/ -q`
Expected: PASS（記錄數量）。

```bash
git add backtest/backtester.py tests/test_backtester_decision.py
git commit -m "feat: backtester 遷移吃 decide()+sim-clock+追價語意，移除 core.strategy 依賴；刪 grid_engine/backtest.py"
```

---

## Task 9: 決策日誌重放比對工具（強驗收）

實盤 `decide()` 落地的 JSONL，離線用同一 `decide()` 逐筆重放比對。這是唯一能驗證「快照捕捉完整性」的手段。

**Files:**
- Create: `grid_engine/replay.py`
- Test: `tests/test_replay.py`（新）

**Interfaces:**
- Produces:
  - `replay.load_records(path) -> list[dict]`
  - `replay.replay_record(rec) -> dict`（用 rec["inputs"] 重建 DecisionInputs → decide → asdict）
  - `replay.diff_record(rec) -> Optional[dict]`（重放結果 vs rec["decision"]，回 None 表零 diff，否則回差異）
  - `replay.replay_file(path) -> tuple[int, list[dict]]`（總筆數、有 diff 的清單）

- [ ] **Step 1: 寫重放測試**

```python
# tests/test_replay.py
import json
from grid_engine import replay
from grid_engine.decision import DecisionInputs, EnhancementSnapshot, decide
import dataclasses


def _make_record():
    enh = EnhancementSnapshot(0.004, 0.006, 1.0, 1.0)
    inp = DecisionInputs(
        price=2.5, long_position=10, short_position=0,
        buy_long_orders=0, sell_long_orders=0, buy_short_orders=0, sell_short_orders=0,
        last_grid_price_long=2.5, last_grid_price_short=2.5,
        long_dead_mode=False, short_dead_mode=False,
        grid_spacing=0.006, take_profit_spacing=0.004,
        initial_quantity=3, position_threshold=60, position_limit=15,
        glft_enabled=False, gamma=0.1, enh=enh)
    dec = decide(inp)
    return {"symbol": "XRP/USDC:USDC",
            "inputs": dataclasses.asdict(inp),
            "decision": dataclasses.asdict(dec)}


def test_replay_zero_diff_on_faithful_record(tmp_path):
    rec = _make_record()
    p = tmp_path / "d.jsonl"
    p.write_text(json.dumps(rec, ensure_ascii=False) + "\n")
    total, diffs = replay.replay_file(str(p))
    assert total == 1 and diffs == []


def test_replay_detects_tampered_decision(tmp_path):
    rec = _make_record()
    rec["decision"]["long"]["should_adjust"] = not rec["decision"]["long"]["should_adjust"]
    p = tmp_path / "d.jsonl"
    p.write_text(json.dumps(rec, ensure_ascii=False) + "\n")
    total, diffs = replay.replay_file(str(p))
    assert total == 1 and len(diffs) == 1
```

- [ ] **Step 2: 跑 → FAIL**

Run: `uv run pytest tests/test_replay.py -v`
Expected: FAIL（`ModuleNotFoundError: grid_engine.replay`）。

- [ ] **Step 3: 寫 replay.py**

```python
# grid_engine/replay.py
"""決策日誌重放：用同一 decide() 逐筆重放實盤落地的 inputs，比對 decision。
零 diff = 快照捕捉完整（實盤 execute 與純層一致）。上線 ≥24h 零 diff 為 #4 最終驗收。"""
import json
import dataclasses

from .decision import DecisionInputs, EnhancementSnapshot, decide


def load_records(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _rebuild_inputs(inp: dict) -> DecisionInputs:
    enh = EnhancementSnapshot(**{**inp["enh"],
                                 "leading_signals": tuple(inp["enh"].get("leading_signals", ()))})
    fields = {k: v for k, v in inp.items() if k != "enh"}
    return DecisionInputs(enh=enh, **fields)


def replay_record(rec: dict) -> dict:
    return dataclasses.asdict(decide(_rebuild_inputs(rec["inputs"])))


def diff_record(rec: dict):
    replayed = replay_record(rec)
    return None if replayed == rec["decision"] else {
        "symbol": rec.get("symbol"), "expected": rec["decision"], "replayed": replayed}


def replay_file(path):
    recs = load_records(path)
    diffs = [d for d in (diff_record(r) for r in recs) if d is not None]
    return len(recs), diffs
```

- [ ] **Step 4: 跑 → 綠**

Run: `uv run pytest tests/test_replay.py -v`
Expected: PASS。

- [ ] **Step 5: 全套綠 + Commit**

Run: `uv run pytest tests/ -q`
Expected: PASS（記錄總數量）。

```bash
git add grid_engine/replay.py tests/test_replay.py
git commit -m "feat: 決策日誌重放比對工具（強驗收：實盤 inputs 逐筆重放零 diff）"
```

---

## Task 10: 上線觀察驗收（人工，非自動）

- [ ] **Step 1:** 部署後確認 `logs/decisions.jsonl` 持續落地（每 `decide()` 一行）。
- [ ] **Step 2:** 跑 ≥24h 後：`uv run python -c "from grid_engine import replay; t,d=replay.replay_file('logs/decisions.jsonl'); print(t, len(d))"`，期望 diff = 0。
- [ ] **Step 3:** 若有 diff → 表示 execute 與純層不一致（快照漏欄位或 bot 執行偏離 decide）；逐筆看 `expected` vs `replayed` 定位。修正後回歸相關 task 測試。
- [ ] **Step 4:** 零 diff ≥24h → #4 完成，更新 `tasks/progress.md`。

---

## 保真限制（寫入回測報告）

- Bandit 閉環不重現（回測固定參數評估 decide）。
- flat-entry bootstrap 回測以「掛 K 線 close、taker 進場」近似。
- 成交模擬樂觀偏差：價格穿越即全量成交，無 queue position / partial fill 佇列（#5 檢討）。
- GLFT price skew 為死代碼，bug-for-bug 保留不生效（使其生效屬策略變更，另開 issue）。
- 回測 K 線粒度若不足以餵 LeadingIndicator 的 OFI/spread tick 級視窗 → 該 manager 在回測退化為中性值，報告明示。
```