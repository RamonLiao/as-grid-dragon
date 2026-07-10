# Phase 0：回測引擎保真度修復 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 `backtest/backtester.py` 輸出的數字可以拿來做策略決策 —— 修正撮合規則、權益核算、強平缺失與成本方向偏差。

**Architecture:** 抽出純函數模組 `backtest/matching.py`（限價單穿越判定）與 `backtest/liquidation.py`（權益/保證金/強平判定），由 `_run_terminal_ui_mode` 呼叫。純函數可獨立測試，回測主迴圈只負責接線。不動 `grid_engine/`（決策層），不動 `_run_legacy_mode`（deprecated 路徑）。

**Tech Stack:** Python 3, pandas, pytest, `uv`

**Spec:** `docs/superpowers/specs/2026-07-10-backtest-decision-parity-design.md`（缺口 G4 / G6 / G7 / G8；守門 G-0a1 / G-0a2 / G-0b0 / G-0b1 / G-0b2 / G-0c1 / G-0c2 / G-0c3）

## Global Constraints

- **測試指令**：`uv run python -m pytest tests/ -q`（系統 `python3` 無 pytest）
- **基線**：實作開始前為 **319 passed**。每個 task 結束時全套必須綠。
- **只動 `backtest/`**，唯一例外是 Task 8（新增 live 側斷言測試，不改 `grid_engine/` 產品碼）。
- **不改 `_run_legacy_mode` / `_legacy_grid_decision`**（`backtester.py:48`, `778+`）—— deprecated 路徑，spec §4 非目標。
- **Git staging 只 stage 明確指定的檔案**（`git add <file>...`），禁止 `git add -A` / `git add .`。
- 每個 task 一個 commit。commit message 用繁體中文，格式 `fix(backtest): ...` / `feat(backtest): ...` / `test(backtest): ...`。
- **既有測試斷言不得為了讓新碼通過而放寬**。若某測試因刻意行為變更而紅，必須在該 task 內顯式改寫並在 commit message 說明「為什麼新行為才是對的」。

## 為什麼既有測試沒抓到這些 bug

`tests/test_backtester_decision.py:15-21` 與 `tests/test_backtester_slippage.py:7-13` 的 DataFrame helper 都設 `high = low = close = price`（**退化的平坦 K 線**）。在這種資料上，「用 close 判穿越」與「用 low/high 判穿越」等價，所以 G4 的錯誤一永遠不會顯現。Task 1/2 的測試必須使用 **`high`/`low` 與 `close` 相異**的 K 線。

---

## File Structure

| 檔案 | 責任 | 動作 |
|---|---|---|
| `backtest/matching.py` | 限價單穿越判定（純函數，無狀態） | **新建**（Task 1） |
| `backtest/liquidation.py` | 權益 / margin_usage / 強平判定（純函數） | **新建**（Task 4, 5） |
| `backtest/backtester.py` | 回測主迴圈；接線上述純層 | 修改（Task 2, 3, 4, 5, 6） |
| `backtest/config.py` | `fee_pct` 預設改 maker；新增 `maintenance_margin_rate` | 修改（Task 5, 6） |
| `tests/test_backtest_matching.py` | matching 純層 + 整合撮合行為 | **新建**（Task 1, 2） |
| `tests/test_backtest_equity.py` | G8 權益核算 + margin_usage | **新建**（Task 3, 4） |
| `tests/test_backtest_liquidation.py` | 強平建模 | **新建**（Task 5） |
| `tests/test_backtest_matching_realdata.py` | 真實 K 線 characterization（G-0a1） | **新建**（Task 7） |
| `tests/test_bandit_overwrites_config.py` | G-0c3 | **新建**（Task 8） |
| `scripts/cost_sensitivity.py` | G-0c1 成本敏感度網格 | **新建**（Task 9） |
| `tests/web/test_backtest_service.py:35` | `fee_pct` 預設值斷言 | 修改（Task 6） |
| `backtest/smart_optimizer.py:231,303` | 硬編 `0.0004` | 修改（Task 6） |

---

## Task 1: 純層 `matching.py` — 限價單穿越判定

**Files:**
- Create: `backtest/matching.py`
- Test: `tests/test_backtest_matching.py`

**Interfaces:**
- Consumes: 無
- Produces:
  - `entry_crossed(side: str, bar_low: float, bar_high: float, limit: float) -> bool`
  - `tp_crossed(side: str, bar_low: float, bar_high: float, limit: float) -> bool`
  - `side` ∈ `{"long", "short"}`。Task 2 會呼叫這兩個函數。

- [ ] **Step 1: 寫失敗的測試**

建立 `tests/test_backtest_matching.py`：

```python
"""限價單撮合純層：用 bar 的 high/low 判穿越（非 close）。

回測前身用 close 判穿越，在 low 刺穿但 close 未穿越的 K 線上漏掉成交。
真實 1m K 線實測漏掉 48.5% 的多頭進場成交。
見 spec G4。
"""
import pytest

from backtest.matching import entry_crossed, tp_crossed


# ── 多頭進場：買單掛在下方，low 觸及即成交 ──────────────────────────

def test_long_entry_fills_when_low_pierces_even_if_close_stays_above():
    """核心回歸：下影線刺穿掛單價就該成交，不需要收盤站上去。
    舊實作用 close 判斷，這根 K 線會被漏掉。"""
    assert entry_crossed("long", bar_low=98.0, bar_high=101.0, limit=99.0) is True


def test_long_entry_does_not_fill_when_bar_never_reaches_limit():
    assert entry_crossed("long", bar_low=99.5, bar_high=101.0, limit=99.0) is False


def test_long_entry_fills_on_exact_touch():
    """掛單價 == bar 最低價：限價單在該價位可成交（保守但符合 maker 語意）。"""
    assert entry_crossed("long", bar_low=99.0, bar_high=101.0, limit=99.0) is True


# ── 空頭進場：賣單掛在上方，high 觸及即成交 ──────────────────────────

def test_short_entry_fills_when_high_pierces_even_if_close_stays_below():
    assert entry_crossed("short", bar_low=99.0, bar_high=102.0, limit=101.0) is True


def test_short_entry_does_not_fill_when_bar_never_reaches_limit():
    assert entry_crossed("short", bar_low=99.0, bar_high=100.5, limit=101.0) is False


def test_short_entry_fills_on_exact_touch():
    assert entry_crossed("short", bar_low=99.0, bar_high=101.0, limit=101.0) is True


# ── 止盈：方向與進場相反 ─────────────────────────────────────────────

def test_long_tp_fills_when_high_reaches_it():
    """多頭止盈是賣單、掛在上方 → 看 high。"""
    assert tp_crossed("long", bar_low=99.0, bar_high=102.0, limit=101.0) is True


def test_long_tp_does_not_fill_when_high_falls_short():
    assert tp_crossed("long", bar_low=99.0, bar_high=100.5, limit=101.0) is False


def test_short_tp_fills_when_low_reaches_it():
    """空頭止盈是買單、掛在下方 → 看 low。"""
    assert tp_crossed("short", bar_low=98.0, bar_high=101.0, limit=99.0) is True


def test_short_tp_does_not_fill_when_low_falls_short():
    assert tp_crossed("short", bar_low=99.5, bar_high=101.0, limit=99.0) is False


# ── monkey：極端輸入不得崩潰 ────────────────────────────────────────

@pytest.mark.parametrize("side", ["long", "short"])
def test_zero_and_equal_bounds_do_not_raise(side):
    assert isinstance(entry_crossed(side, 0.0, 0.0, 0.0), bool)
    assert isinstance(tp_crossed(side, 100.0, 100.0, 100.0), bool)
```

- [ ] **Step 2: 跑測試確認它失敗**

Run: `uv run python -m pytest tests/test_backtest_matching.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'backtest.matching'`

- [ ] **Step 3: 寫最小實作**

建立 `backtest/matching.py`：

```python
"""限價單撮合的純判定：用 bar 的 high/low 判穿越，成交於掛單價。

回測前身（backtester.py:_settle）用該根 close 判穿越、且以 close 成交，
造成兩個方向相反的誤差：
  1. 只看 close → 盤中觸及即成交的限價單被漏掉（真實 1m K 線實測漏 48.5%）
  2. 以 close 成交 → 送出不存在的免費價格改善（實測 mean 10.38 bps）
見 docs/superpowers/specs/2026-07-10-backtest-decision-parity-design.md 缺口 G4。

本模組只回答「有沒有穿越」。成交價一律是掛單價（limit），由呼叫端負責。
"""


def entry_crossed(side: str, bar_low: float, bar_high: float, limit: float) -> bool:
    """進場限價單是否在本根 K 線成交。

    多頭進場 = 買單掛在現價下方 → bar 最低價觸及即成交。
    空頭進場 = 賣單掛在現價上方 → bar 最高價觸及即成交。
    """
    if side == "long":
        return bar_low <= limit
    return bar_high >= limit


def tp_crossed(side: str, bar_low: float, bar_high: float, limit: float) -> bool:
    """止盈限價單是否在本根 K 線成交（方向與進場相反）。

    多頭止盈 = 賣單掛在現價上方 → 看 bar 最高價。
    空頭止盈 = 買單掛在現價下方 → 看 bar 最低價。
    """
    if side == "long":
        return bar_high >= limit
    return bar_low <= limit
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run python -m pytest tests/test_backtest_matching.py -q`
Expected: `11 passed`

- [ ] **Step 5: 跑全套確認無回歸**

Run: `uv run python -m pytest tests/ -q`
Expected: `330 passed`（319 基線 + 11 新）

- [ ] **Step 6: Commit**

```bash
git add backtest/matching.py tests/test_backtest_matching.py
git commit -m "feat(backtest): 純層 matching.py — 限價單以 high/low 判穿越

回測前身用該根 close 判穿越，在 low 刺穿但 close 未穿越的 K 線上
漏掉成交。真實 1m K 線實測漏掉 48.5% 的多頭進場成交（spec G4）。

本 commit 只加純函數與測試，尚未接線進 backtester。"
```

---

## Task 2: 接線 `_settle` — 用 high/low 判穿越、成交於掛單價

**Files:**
- Modify: `backtest/backtester.py:622-637`（`_settle`）、`backtest/backtester.py:654-656`（呼叫點）
- Modify: `backtest/backtester.py:19-27`（import）
- Test: `tests/test_backtest_matching.py`（追加整合測試）

**Interfaces:**
- Consumes: Task 1 的 `entry_crossed(side, bar_low, bar_high, limit) -> bool`、`tp_crossed(...) -> bool`
- Produces: `_settle(side: str, bar_low: float, bar_high: float, ts) -> None`（內部閉包，簽名變更）

> 新簽名**不再需要 `close`** —— 穿越判定只吃 `high`/`low`，成交價一律取掛單價。這正是修法的本質：`close` 從撮合邏輯裡完全消失。

> **關鍵行為變更**：成交價由「該根 close」改為「掛單價 `limit`」。這消除幻覺價格改善（實測 mean 10.38 bps，是所建模滑價 1bp 的 10 倍）。
>
> **同根雙觸發**：維持現行順序 **entry 先、tp 後**（`_settle` 內既有順序）。這是保守假設 —— 對多頭而言 entry 是增加曝險的那一邊。`_close` 走 FIFO（`positions[0]`），所以本根新開的倉不會被本根止盈平掉。

- [ ] **Step 1: 寫失敗的測試（追加到 `tests/test_backtest_matching.py` 末尾）**

```python
# ── 整合：backtester 真的用 high/low 且成交於掛單價 ──────────────────

import pandas as pd

from backtest.backtester import GridBacktester
from backtest.config import Config


def _ohlc_df(bars):
    """bars: list of (open, high, low, close)"""
    return pd.DataFrame({
        "open_time": pd.date_range("2024-01-01", periods=len(bars), freq="1min"),
        "open": [b[0] for b in bars],
        "high": [b[1] for b in bars],
        "low": [b[2] for b in bars],
        "close": [b[3] for b in bars],
        "volume": [100.0] * len(bars),
    })


def _zero_cost_cfg(**kw):
    base = dict(symbol="BNBUSDC", initial_balance=100000.0, initial_quantity=1.0,
                leverage=10, take_profit_spacing=0.004, grid_spacing=0.006,
                direction="long", terminal_ui_mode=True,
                fee_pct=0.0, slippage_bps=0.0, funding_enabled=False)
    base.update(kw)
    return Config(**base)


def test_long_entry_fills_at_limit_price_not_at_close():
    """G-0a2：零成本下，成交價必須嚴格等於掛單價。

    第 1 根 close=100 → 掛進場限價 100*(1-0.006)=99.4。
    第 2 根 low=98（刺穿）但 close=100（未穿越）→ 必須成交，且成交價 = 99.4。
    第 3 根收在 99.4 → 以 99.4 開的倉 unrealized 恰為 0。

    三種實作的分辨：
      舊（close 判穿越、close 成交）      → 第 2 根不成交 → 沒開倉
      半修（low 判穿越、仍以 close 成交）  → 成交於 98 → unrealized = +1.4
      正確（low 判穿越、成交於 limit）     → 成交於 99.4 → unrealized = 0

    第一個斷言擋掉「沒開倉」，第二個斷言擋掉「成交於 close」。
    兩者都對，本測試才綠。
    """
    df = _ohlc_df([
        (100.0, 100.0, 100.0, 100.0),   # 掛單：entry @ 99.4
        (100.0, 100.5,  98.0, 100.0),   # low 刺穿 99.4 → 應成交於 99.4
        (99.4,   99.4,  99.4,  99.4),   # 末根收在 99.4 → unrealized == 0
    ])
    res = GridBacktester(df, _zero_cost_cfg()).run()
    assert res.final_equity != pytest.approx(100000.0, abs=1e-9), (
        "沒有開倉：low 未被用來判穿越"
    )
    assert res.unrealized_pnl == pytest.approx(0.0, abs=1e-9), (
        f"成交價不等於掛單價 99.4（unrealized={res.unrealized_pnl}；"
        f"若為 +1.4 表示成交於該根 close=98）"
    )


def test_long_entry_does_not_fill_when_low_never_reaches_limit():
    """負向對照：low 沒到掛單價就不該成交。"""
    df = _ohlc_df([
        (100.0, 100.0, 100.0, 100.0),   # entry @ 99.4
        (100.0, 100.5,  99.5, 100.0),   # low=99.5 > 99.4 → 不成交
        (100.0, 100.0, 100.0, 100.0),
    ])
    res = GridBacktester(df, _zero_cost_cfg()).run()
    assert res.unrealized_pnl == pytest.approx(0.0, abs=1e-9)
    assert res.trades_count == 0
```

- [ ] **Step 2: 跑測試確認它失敗**

Run: `uv run python -m pytest tests/test_backtest_matching.py::test_long_entry_fills_at_limit_price_not_at_close -q`
Expected: FAIL — `AssertionError: 沒有開倉：low 未被用來判穿越`

> 舊實作用 close 判穿越，第 2 根 `close=100.0 > 99.4` 不成交 → 沒有倉位。
> 若只寫 `assert unrealized_pnl == 0` 這一條斷言，**舊實作會假綠**（沒開倉時 unrealized 也是 0）。
> 這就是為什麼第一條斷言必須先擋掉「沒開倉」。

- [ ] **Step 3: 改 import（`backtest/backtester.py:19-27` 區塊）**

在 `from backtest.costs import apply_slippage, funding_charge`（`backtester.py:27`）之後新增一行：

```python
from backtest.matching import entry_crossed, tp_crossed
```

- [ ] **Step 4: 改寫 `_settle`（`backtest/backtester.py:622-637`）**

把整個 `_settle` 函數替換為：

```python
        def _settle(side: str, bar_low: float, bar_high: float, ts) -> None:
            """結算既有 pending（上一根掛的單）對本根 K 線的成交。

            穿越用 high/low 判定（限價單盤中觸及即成交）；成交價一律是掛單價。
            close 不再參與撮合——它既不決定有沒有成交，也不決定成交在哪。
            同根雙觸發時 entry 先於 tp（保守：先增加曝險）；_close 走 FIFO，
            故本根新開的倉不會被本根止盈平掉。
            """
            positions = long_positions if side == "long" else short_positions
            e = pend[side]["entry"]
            if e is not None and entry_crossed(side, bar_low, bar_high, e["price"]):
                if _open(side, e["price"], e["qty"]):
                    pend[side]["entry"] = None
            t = pend[side]["tp"]
            if t is not None and positions and tp_crossed(side, bar_low, bar_high, t["price"]):
                _close(side, t["price"], t["qty"], ts)
                pend[side]["tp"] = None
```

> `_open` / `_close` 內部仍會呼叫 `apply_slippage`，把成交價往不利方向偏移 `slippage_bps`。這是**執行成本 haircut**，不是撮合價 —— 零成本設定下 `slippage_bps=0`，成交價嚴格等於掛單價（G-0a2）。

- [ ] **Step 5: 改呼叫點（`backtest/backtester.py:653-656`）**

把：

```python
                # 先結算成交（用上一根掛出的 pending）
                for side in ("long", "short"):
                    if cfg.direction in (side, "both"):
                        _settle(side, price, timestamp)
```

替換為：

```python
                # 先結算成交（用上一根掛出的 pending）；穿越判定吃本根 high/low
                bar_high = row['high']
                bar_low = row['low']
                if not (math.isfinite(bar_high) and math.isfinite(bar_low)
                        and bar_low > 0 and bar_high >= bar_low):
                    bar_high = bar_low = price   # 髒 OHLC 退化為 close（保守）
                for side in ("long", "short"):
                    if cfg.direction in (side, "both"):
                        _settle(side, bar_low, bar_high, timestamp)
```

- [ ] **Step 6: 跑新測試確認通過**

Run: `uv run python -m pytest tests/test_backtest_matching.py -q`
Expected: `13 passed`

- [ ] **Step 7: 跑全套，記錄哪些既有測試因刻意行為變更而紅**

Run: `uv run python -m pytest tests/ -q`
Expected: 可能有既有 backtester 測試轉紅（`tests/test_backtester_decision.py`、`tests/test_backtester_funding.py`、`tests/test_backtest_cost_monkey.py`、`tests/test_backtest_manager_delegation.py`）。

**處理原則**：
- 若斷言是**不等式或 smoke**（`>= 0`、`> 0`、`slip <= zero`）→ 應仍綠。若紅，代表真的有回歸，**必須查清楚，不得放寬**。
- 若斷言鎖定**具體成交價或具體 trades_count** → 那是被舊撮合語意鎖住的數字。逐一改寫，並在測試 docstring 寫明新期望值怎麼算出來的。
- **禁止**用 `pytest.approx` 放寬容差來讓舊斷言通過。

- [ ] **Step 8: 全套綠**

Run: `uv run python -m pytest tests/ -q`
Expected: 全數 passed

- [ ] **Step 9: Commit**

```bash
git add backtest/backtester.py tests/test_backtest_matching.py
git commit -m "fix(backtest): 撮合改用 high/low 判穿越、成交於掛單價（G4）

兩個方向相反的錯誤，一併修正：
  1. 只看 close 判穿越 → 真實 1m K 線實測漏掉 48.5% 的多頭進場成交
  2. 以 close 成交而非掛單價 → 送出不存在的免費價格改善
     （mean 10.38 bps，是所建模 slippage 1bp 的 10 倍、單邊 fee 4bp 的 2.6 倍，
      佔一次網格來回毛利 30bp 的三分之二）

FIDELITY_NOTES 第 (8) 條宣稱的「保守下界」因此是錯的：兩個誤差方向相反、
量級差約 2 倍，淨偏誤不可預測。

既有測試用 high=low=close 的平坦 K 線，故從未觸發錯誤一。新測試使用
high/low 與 close 相異的 K 線。

回測數字會變，這是 intended（spec Phase 0-a）。"
```

---

## Task 3: 修正權益核算 — 加回未平倉位鎖住的 margin（G8）

**Files:**
- Modify: `backtest/backtester.py:717-722`（`equity_curve` 內的 equity）
- Modify: `backtest/backtester.py:726-732`（`final_equity`）
- Test: `tests/test_backtest_equity.py`（新建）

**Interfaces:**
- Consumes: 無
- Produces: 無新公開介面。`BacktestResult.final_equity` / `max_drawdown` 的語意修正。

> `_open()` 執行 `balance -= (margin + fee)` 並把 `margin` 存進倉位 dict；`_close()` 才 `balance += pos["margin"] + net`。因此**只要有未平倉位，`balance` 就不含那筆 margin**，而 `equity = balance + unrealized` 兩處都漏了把它加回來。

- [ ] **Step 1: 寫失敗的測試**

建立 `tests/test_backtest_equity.py`：

```python
"""G8：權益核算必須包含未平倉位鎖住的 margin。

_open() 把 margin 從 balance 扣除並存進倉位，_close() 才加回。
equity = balance + unrealized 漏了 + sum(open margin)。
⇒ 只要有未平倉位，final_equity 系統性低估、max_drawdown 系統性虛增，
   偏誤幅度與持倉規模成正比。這直接命中 spec §7 欽定的兩個主指標。
見 spec 缺口 G8。
"""
import pandas as pd
import pytest

from backtest.backtester import GridBacktester
from backtest.config import Config


def _flat_df(prices):
    return pd.DataFrame({
        "open_time": pd.date_range("2024-01-01", periods=len(prices), freq="1min"),
        "open": prices, "high": prices, "low": prices, "close": prices,
        "volume": [100.0] * len(prices),
    })


def _zero_cost_cfg(**kw):
    base = dict(symbol="BNBUSDC", initial_balance=1000.0, initial_quantity=0.5,
                leverage=10, grid_spacing=0.006, take_profit_spacing=0.004,
                direction="long", terminal_ui_mode=True,
                fee_pct=0.0, slippage_bps=0.0, funding_enabled=False)
    base.update(kw)
    return Config(**base)


def test_final_equity_includes_margin_locked_in_open_positions():
    """G-0b0：零成本下，final_equity 必須 == 本金 + 已實現 + 未實現。

    單調下跌讓多頭一路開倉（不止盈），末根收盤價回到起點。
    修正前實測：final_equity=988.2，正確值 1007.5，缺口 19.3
    （= 4 張未平倉位的 margin，每張 ≈ price*0.5/10）。
    """
    prices = [100.0] * 3 + [99.0, 98.0, 97.0, 96.0, 95.0] + [100.0]
    res = GridBacktester(_flat_df(prices), _zero_cost_cfg()).run()

    expected = 1000.0 + res.realized_pnl + res.unrealized_pnl
    assert res.final_equity == pytest.approx(expected, abs=1e-6), (
        f"final_equity={res.final_equity} != 本金+已實現+未實現={expected}；"
        f"缺口 {expected - res.final_equity}（未平倉位鎖住的 margin）"
    )


def test_equity_curve_never_dips_below_balance_plus_unrealized():
    """equity_curve 的每一點也要含 open margin —— max_drawdown 從它算出來。"""
    prices = [100.0] * 3 + [99.0, 98.0, 97.0, 96.0, 95.0]
    res = GridBacktester(_flat_df(prices), _zero_cost_cfg()).run()
    # 全程未平倉、價格單調下跌 → 權益最低點不該低於「本金 - 未實現虧損」
    worst_equity = min(e[2] for e in res.equity_curve)
    assert worst_equity > 900.0, (
        f"權益曲線最低點 {worst_equity} 過低，疑似漏算 open margin"
    )


def test_flat_price_no_position_equity_equals_initial_balance():
    """負向對照：沒開過倉時，權益恆等於本金（修正前後都該成立）。"""
    res = GridBacktester(_flat_df([100.0] * 5), _zero_cost_cfg(direction="long",
                                                               grid_spacing=0.5)).run()
    assert res.final_equity == pytest.approx(1000.0, abs=1e-9)
```

- [ ] **Step 2: 跑測試確認它失敗**

Run: `uv run python -m pytest tests/test_backtest_equity.py -q`
Expected: FAIL — `final_equity=988.2 != 本金+已實現+未實現=1007.5；缺口 19.3`

- [ ] **Step 3: 修 `equity_curve` 的 equity（`backtest/backtester.py:717-722`）**

把：

```python
                # 計算淨值
                unrealized = sum((price - p["price"]) * p["qty"] for p in long_positions)
                unrealized += sum((p["price"] - price) * p["qty"] for p in short_positions)
                equity = balance + unrealized
```

替換為：

```python
                # 計算淨值。balance 已扣除未平倉位的 margin（_open 扣、_close 才加回），
                # 故必須把 open_margin 加回來，否則權益被低估、回撤被虛增（G8）。
                unrealized = sum((price - p["price"]) * p["qty"] for p in long_positions)
                unrealized += sum((p["price"] - price) * p["qty"] for p in short_positions)
                open_margin = (sum(p["margin"] for p in long_positions)
                               + sum(p["margin"] for p in short_positions))
                equity = balance + open_margin + unrealized
```

- [ ] **Step 4: 修 `final_equity`（`backtest/backtester.py:726-732`）**

把：

```python
        realized_pnl = sum(t["pnl"] for t in trades)
        final_equity = balance + unrealized_pnl
```

替換為：

```python
        realized_pnl = sum(t["pnl"] for t in trades)
        final_open_margin = (sum(p["margin"] for p in long_positions)
                             + sum(p["margin"] for p in short_positions))
        final_equity = balance + final_open_margin + unrealized_pnl
```

- [ ] **Step 5: 跑測試確認通過**

Run: `uv run python -m pytest tests/test_backtest_equity.py -q`
Expected: `3 passed`

- [ ] **Step 6: 跑全套**

Run: `uv run python -m pytest tests/ -q`
Expected: 全數 passed（`max_drawdown` 數值會變小，但既有斷言皆為不等式/smoke）

- [ ] **Step 7: Commit**

```bash
git add backtest/backtester.py tests/test_backtest_equity.py
git commit -m "fix(backtest): 權益核算加回未平倉位鎖住的 margin（G8）

_open() 把 margin 從 balance 扣除並存進倉位，_close() 才加回；
但 equity = balance + unrealized 兩處（equity_curve 與 final_equity）
都漏了 + sum(open margin)。

實證（零成本、本金 1000、qty 0.5、leverage 10、單調下跌後收盤回到起點）：
  回測 final_equity          988.2
  正確權益(本金+已實現+未實現) 1007.5
  缺口                        19.3 = 4 張未平倉位的 margin

⇒ 只要有未平倉位，final_equity 系統性低估、max_drawdown 系統性虛增，
   偏誤與持倉規模成正比。直接命中 spec §7 的兩個主指標，且非方向中性。"
```

---

## Task 4: `liquidation.py` 純層 — 權益 / margin_usage 觀測

**Files:**
- Create: `backtest/liquidation.py`
- Modify: `backtest/backtester.py`（`BacktestResult` 加 `peak_margin_usage`；主迴圈計算）
- Test: `tests/test_backtest_equity.py`（追加）

**Interfaces:**
- Consumes: 無
- Produces:
  - `margin_usage(long_pos: float, short_pos: float, price: float, leverage: float, equity: float) -> float`
    - `equity <= 0` → 回傳 `float("inf")`
  - `BacktestResult.peak_margin_usage: float`（Task 5 的強平判斷與 spec §7 的強平距離代理都用它）

- [ ] **Step 1: 寫失敗的測試（追加到 `tests/test_backtest_equity.py` 末尾）**

```python
# ── margin_usage 純層 ────────────────────────────────────────────────

from backtest.liquidation import margin_usage


def test_margin_usage_is_notional_over_leverage_over_equity():
    # 倉位名目 = (2+0) * 100 = 200；margin = 200/10 = 20；equity = 1000
    assert margin_usage(2.0, 0.0, 100.0, 10.0, 1000.0) == pytest.approx(0.02)


def test_margin_usage_sums_both_sides():
    # hedge mode：多空兩邊都佔保證金
    assert margin_usage(2.0, 3.0, 100.0, 10.0, 1000.0) == pytest.approx(0.05)


def test_margin_usage_is_inf_when_equity_non_positive():
    """equity <= 0 → 定義為 inf，避免除零；下游一律視為已強平。"""
    assert margin_usage(1.0, 0.0, 100.0, 10.0, 0.0) == float("inf")
    assert margin_usage(1.0, 0.0, 100.0, 10.0, -5.0) == float("inf")


def test_margin_usage_zero_when_no_position():
    assert margin_usage(0.0, 0.0, 100.0, 10.0, 1000.0) == 0.0


def test_backtest_result_reports_peak_margin_usage():
    """peak_margin_usage 是強平距離的代理（spec §7），純觀測不影響決策。"""
    prices = [100.0] * 3 + [99.0, 98.0, 97.0]
    res = GridBacktester(_flat_df(prices), _zero_cost_cfg()).run()
    assert res.peak_margin_usage > 0.0
    assert res.peak_margin_usage < 1.0
```

- [ ] **Step 2: 跑測試確認它失敗**

Run: `uv run python -m pytest tests/test_backtest_equity.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'backtest.liquidation'`

- [ ] **Step 3: 建立 `backtest/liquidation.py`**

```python
"""權益 / 保證金 / 強平的純判定。

回測前身完全沒有強平建模：_open() 在保證金不足時只是 return False，
倉位永遠不會被強平，equity 可以變成負數而回測照跑到底。
⇒ 「無限加倉 + 不爆倉」在算術上是必勝策略（martingale 恆等式），
   任何優化器都會選它。見 spec 缺口 G6。
"""


def margin_usage(long_pos: float, short_pos: float, price: float,
                 leverage: float, equity: float) -> float:
    """保證金使用率 = 倉位名目 / 槓桿 / 權益。

    hedge mode 下多空兩側各自佔用保證金，故名目相加。
    equity <= 0 → inf（下游一律視為已強平），避免除零。

    註：live 的 state.margin_usage 是帳戶層（跨 symbol，state.py:115），
    此處是單 symbol。單 symbol 回測的結論不得外推至多 symbol 實盤。
    """
    if equity <= 0:
        return float("inf")
    notional = (long_pos + short_pos) * price
    return (notional / leverage) / equity
```

- [ ] **Step 4: `BacktestResult` 加欄位（`backtest/backtester.py:105` 之後）**

在 `funding_paid: float = 0.0` 之後新增：

```python
    peak_margin_usage: float = 0.0   # 全程 margin_usage 最大值（強平距離代理）
```

並在 `to_dict()`（`backtester.py:120` 附近，`"funding_paid": self.funding_paid,` 之後）新增：

```python
            "peak_margin_usage": self.peak_margin_usage,
```

- [ ] **Step 5: 主迴圈計算 peak_margin_usage**

在 `backtest/backtester.py` import 區塊（Task 2 加的 `from backtest.matching import ...` 之後）新增：

```python
from backtest.liquidation import margin_usage
```

在 `max_equity = balance`（`backtester.py:571`）之後新增：

```python
        peak_margin_usage = 0.0
```

在 Task 3 改好的 equity 計算之後（`equity = balance + open_margin + unrealized` 那行下面）新增：

```python
                long_pos_qty = sum(p["qty"] for p in long_positions)
                short_pos_qty = sum(p["qty"] for p in short_positions)
                mu = margin_usage(long_pos_qty, short_pos_qty, price, leverage, equity)
                if mu > peak_margin_usage:
                    peak_margin_usage = mu
```

在 `return BacktestResult(...)`（`backtester.py:759`）的 `funding_paid=funding_paid,` 之後新增：

```python
            peak_margin_usage=peak_margin_usage,
```

- [ ] **Step 6: 跑測試確認通過**

Run: `uv run python -m pytest tests/test_backtest_equity.py -q`
Expected: `8 passed`

- [ ] **Step 7: 跑全套**

Run: `uv run python -m pytest tests/ -q`
Expected: 全數 passed

- [ ] **Step 8: Commit**

```bash
git add backtest/liquidation.py backtest/backtester.py tests/test_backtest_equity.py
git commit -m "feat(backtest): 純層 liquidation.margin_usage + peak_margin_usage 觀測欄位

margin_usage = 倉位名目 / 槓桿 / 權益（hedge mode 多空名目相加）；
equity <= 0 → inf，避免除零。

peak_margin_usage 是 spec §7 的強平距離代理（真實 liquidationPrice 在
sync_service.py:60-73 被丟棄，回測亦無交易所維持保證金階梯表）。
純觀測，不影響決策。Task 5 的強平判斷會消費 margin_usage。"
```

---

## Task 5: 強平建模 — `liquidated` 一票否決

**Files:**
- Modify: `backtest/liquidation.py`（加 `should_liquidate`）
- Modify: `backtest/config.py`（加 `maintenance_margin_rate`）
- Modify: `backtest/backtester.py`（`BacktestResult.liquidated`；主迴圈觸發並終止）
- Test: `tests/test_backtest_liquidation.py`（新建）

**Interfaces:**
- Consumes: Task 4 的 `margin_usage(...)`
- Produces:
  - `should_liquidate(equity: float, long_pos: float, short_pos: float, price: float, maintenance_margin_rate: float) -> bool`
  - `Config.maintenance_margin_rate: float = 0.005`
  - `BacktestResult.liquidated: bool = False`

> **一票否決**：spec §7 規定任何 `liquidated=True` 的參數組直接淘汰，不進優化目標函數。強平時以當根收盤價全平多空倉（走既有 `_close`，故會進 `trades` 並反映在 `realized_pnl`），然後**終止回測**。

- [ ] **Step 1: 寫失敗的測試**

建立 `tests/test_backtest_liquidation.py`：

```python
"""G6：強平建模。

回測前身沒有強平：_open() 保證金不足時只 return False，倉位永不被平，
equity 可為負而回測照跑到底 ⇒「無限加倉 + 不爆倉」是算術上的必勝策略。
選項 (b)「關掉裝死模式」的全部風險都在這裡，沒有強平就無法評估。
見 spec 缺口 G6、守門 G-0b1 / G-0b2。
"""
import pandas as pd
import pytest

from backtest.backtester import GridBacktester
from backtest.config import Config
from backtest.liquidation import should_liquidate


def _flat_df(prices):
    return pd.DataFrame({
        "open_time": pd.date_range("2024-01-01", periods=len(prices), freq="1min"),
        "open": prices, "high": prices, "low": prices, "close": prices,
        "volume": [100.0] * len(prices),
    })


def _cfg(**kw):
    base = dict(symbol="BNBUSDC", initial_balance=100.0, initial_quantity=0.5,
                leverage=20, grid_spacing=0.002, take_profit_spacing=0.5,
                direction="long", terminal_ui_mode=True,
                fee_pct=0.0, slippage_bps=0.0, funding_enabled=False,
                threshold_multiplier=1e9)   # 裝死永不觸發 → 無限加倉
    base.update(kw)
    return Config(**base)


# ── 純層 ─────────────────────────────────────────────────────────────

def test_should_liquidate_when_equity_below_maintenance_margin():
    # 名目 = 10 * 100 = 1000；維持保證金 = 1000 * 0.005 = 5
    assert should_liquidate(equity=4.0, long_pos=10.0, short_pos=0.0,
                            price=100.0, maintenance_margin_rate=0.005) is True


def test_should_not_liquidate_when_equity_above_maintenance_margin():
    assert should_liquidate(equity=6.0, long_pos=10.0, short_pos=0.0,
                            price=100.0, maintenance_margin_rate=0.005) is False


def test_should_liquidate_when_equity_negative():
    assert should_liquidate(equity=-1.0, long_pos=1.0, short_pos=0.0,
                            price=100.0, maintenance_margin_rate=0.005) is True


def test_no_position_never_liquidates():
    """沒有倉位就沒有維持保證金需求，權益再低也不強平。"""
    assert should_liquidate(equity=0.01, long_pos=0.0, short_pos=0.0,
                            price=100.0, maintenance_margin_rate=0.005) is False


# ── 整合 ─────────────────────────────────────────────────────────────

def test_relentless_downtrend_with_no_dead_mode_triggers_liquidation():
    """G-0b1：單邊崩盤 + 裝死關閉 + 高槓桿 → 必爆，且回測提前終止。

    這正是選項 (b)「關掉裝死模式」的尾部風險。沒有強平建模時，回測會讓
    倉位無限累積、equity 變負而照跑到底，於是 optimizer 誤判 (b) 最好。
    """
    prices = [100.0] + [100.0 * (0.99 ** i) for i in range(1, 400)]
    res = GridBacktester(_flat_df(prices), _cfg()).run()
    assert res.liquidated is True
    # 提前終止：權益曲線長度應短於 K 線數
    assert len(res.equity_curve) < len(prices)


def test_normal_range_bound_market_does_not_liquidate():
    """G-0b2：正常震盪 + 充足本金 → liquidated 必須是 False。"""
    prices = [100.0, 99.6, 100.2, 99.8, 100.4, 99.9, 100.1]
    res = GridBacktester(_flat_df(prices),
                         _cfg(initial_balance=100000.0, leverage=5,
                              threshold_multiplier=20.0)).run()
    assert res.liquidated is False


def test_liquidation_flag_defaults_false():
    res = GridBacktester(_flat_df([100.0] * 5),
                         _cfg(initial_balance=100000.0, grid_spacing=0.5)).run()
    assert res.liquidated is False
```

- [ ] **Step 2: 跑測試確認它失敗**

Run: `uv run python -m pytest tests/test_backtest_liquidation.py -q`
Expected: FAIL — `ImportError: cannot import name 'should_liquidate'`

- [ ] **Step 3: `backtest/liquidation.py` 加 `should_liquidate`**

在 `margin_usage` 之後新增：

```python
def should_liquidate(equity: float, long_pos: float, short_pos: float,
                     price: float, maintenance_margin_rate: float) -> bool:
    """權益是否已跌破維持保證金 → 觸發強平。

    採 isolated margin 的簡化模型：維持保證金 = 倉位名目 × maintenance_margin_rate。
    真實幣安是分層階梯（tiered），此處用單一費率代理並在 FIDELITY_NOTES 揭露。

    無倉位 → 永不強平（沒有維持保證金需求）。
    """
    notional = (long_pos + short_pos) * price
    if notional <= 0:
        return False
    return equity <= notional * maintenance_margin_rate
```

- [ ] **Step 4: `backtest/config.py` 加欄位**

在 `funding_enabled: bool = True`（`backtest/config.py:41`）之後新增：

```python
    maintenance_margin_rate: float = 0.005  # 維持保證金率（單一費率代理幣安分層階梯）
```

在 `to_dict()` 的 `"funding_enabled": self.funding_enabled,`（`backtest/config.py:103`）之後新增：

```python
            "maintenance_margin_rate": self.maintenance_margin_rate,
```

在 `from_dict()` 內對應位置新增：

```python
            maintenance_margin_rate=data.get("maintenance_margin_rate", 0.005),
```

- [ ] **Step 5: `BacktestResult` 加 `liquidated`**

在 Task 4 加的 `peak_margin_usage: float = 0.0` 之後新增：

```python
    liquidated: bool = False         # 觸發強平 → spec §7 一票否決，不進優化目標
```

`to_dict()` 對應新增：

```python
            "liquidated": self.liquidated,
```

- [ ] **Step 6: 主迴圈接線**

import 補 `should_liquidate`：

```python
from backtest.liquidation import margin_usage, should_liquidate
```

在 `peak_margin_usage = 0.0` 之後新增：

```python
        liquidated = False
```

在 Task 4 加的 `peak_margin_usage` 更新之後、`equity_curve.append(...)` 之前插入：

```python
                if should_liquidate(equity, long_pos_qty, short_pos_qty, price,
                                    cfg.maintenance_margin_rate):
                    # 全平多空倉（走 _close → 進 trades、反映在 realized_pnl），終止回測
                    if long_positions and cfg.direction in ("long", "both"):
                        _close("long", price, sum(p["qty"] for p in long_positions), timestamp)
                    if short_positions and cfg.direction in ("short", "both"):
                        _close("short", price, sum(p["qty"] for p in short_positions), timestamp)
                    liquidated = True
                    unrealized = 0.0
                    open_margin = 0.0
                    equity = balance
                    max_equity = max(max_equity, equity)
                    equity_curve.append((timestamp, price, equity))
                    break
```

在 `return BacktestResult(...)` 的 `peak_margin_usage=peak_margin_usage,` 之後新增：

```python
            liquidated=liquidated,
```

> `break` 跳出 `for _, row in self.df.iterrows()` 迴圈；外層 `finally: clock.reset_clock()` 仍會執行。

- [ ] **Step 7: 跑測試確認通過**

Run: `uv run python -m pytest tests/test_backtest_liquidation.py -q`
Expected: `7 passed`

- [ ] **Step 8: 跑全套**

Run: `uv run python -m pytest tests/ -q`
Expected: 全數 passed

- [ ] **Step 9: Commit**

```bash
git add backtest/liquidation.py backtest/config.py backtest/backtester.py tests/test_backtest_liquidation.py
git commit -m "feat(backtest): 強平建模 — liquidated 一票否決（G6）

回測前身沒有強平：_open() 保證金不足只 return False，倉位永不被平，
equity 可為負而回測照跑到底。⇒「無限加倉 + 不爆倉」在算術上是必勝策略
（martingale 恆等式），任何優化器都會選它。

這使 spec 的選項 (b)「關掉裝死模式」根本無法評估 —— 它的全部風險就在
尾部爆倉，而回測把爆倉刪掉了。

模型：維持保證金 = 倉位名目 × maintenance_margin_rate（預設 0.005，
單一費率代理幣安分層階梯，已揭露）。觸發時以當根收盤價全平多空、
終止回測、liquidated=True。

spec §7：任何 liquidated=True 的參數組直接淘汰，不進優化目標函數。"
```

---

## Task 6: 成本方向中性 — `fee_pct` 改 maker + FIDELITY_NOTES 誠實化

**Files:**
- Modify: `backtest/config.py:37`（`fee_pct` 預設）
- Modify: `backtest/backtester.py:30-45`（`FIDELITY_NOTES`）
- Modify: `backtest/smart_optimizer.py:231,303`（硬編 `0.0004`）
- Modify: `tests/web/test_backtest_service.py:35`（預設值斷言）
- Test: `tests/test_backtest_cost_config.py`（追加）

**Interfaces:**
- Consumes: 無
- Produces: `Config.fee_pct` 預設由 `0.0004`（taker）改為 `0.0002`（maker）

> 網格全是限價 maker 單。`fee_pct=0.0004` 是 taker 費率，**回測對每筆成交多收一倍手續費**，且 `slippage_bps` 同樣按成交次數收。高換手選項（關掉裝死、調高 threshold）被多罰，低換手選項（維持現狀）被少罰。**這不是「保守」，是在三個受測選項之間製造系統性偏差。**

- [ ] **Step 1: 寫失敗的測試（追加到 `tests/test_backtest_cost_config.py` 末尾）**

```python
def test_default_fee_is_maker_not_taker():
    """網格全是限價 maker 單。taker 費率會對高換手選項系統性多罰一倍。

    Binance USDⓈ-M VIP0：maker 0.02% = 0.0002，taker 0.05%。
    見 spec 缺口 G7。
    """
    from backtest.config import Config
    assert Config(symbol="BNBUSDC").fee_pct == 0.0002


def test_default_maintenance_margin_rate():
    from backtest.config import Config
    assert Config(symbol="BNBUSDC").maintenance_margin_rate == 0.005
```

- [ ] **Step 2: 跑測試確認它失敗**

Run: `uv run python -m pytest tests/test_backtest_cost_config.py -q`
Expected: FAIL — `assert 0.0004 == 0.0002`

- [ ] **Step 3: 改 `backtest/config.py:37`**

把：

```python
    fee_pct: float = 0.0004             # 手續費 0.04%
```

替換為：

```python
    fee_pct: float = 0.0002             # maker 手續費 0.02%（網格全是限價單；taker 為 0.05%）
```

- [ ] **Step 4: 改 `backtest/smart_optimizer.py`**

`smart_optimizer.py:231` 把 `"fee_pct": 0.0004,` 改為 `"fee_pct": 0.0002,`
`smart_optimizer.py:303` 把 `fee_pct=self.fixed_params.get('fee_pct', 0.0004),` 改為 `fee_pct=self.fixed_params.get('fee_pct', 0.0002),`

- [ ] **Step 5: 改 `tests/web/test_backtest_service.py:35`**

把 `assert cfg.fee_pct == 0.0004` 改為：

```python
    assert cfg.fee_pct == 0.0002   # maker（網格全是限價單），見 spec G7
```

- [ ] **Step 6: 改寫 `FIDELITY_NOTES`（`backtest/backtester.py:30-45`）**

整段替換為：

```python
FIDELITY_NOTES = (
    "回測保真限制: "
    "(1) 限價單撮合——用該根 high/low 判穿越、成交於掛單價；"
    "無 queue position、無部分成交、無排隊落空(maker 單的真實風險); "
    "(2) flat-entry 近似——零倉位 bootstrap 沿用收盤價觸發即進場; "
    "(3) leading/ATR/GLFT 增強於回測退化為中性(全關); "
    "(4) Bandit 不在回測 loop 內重現。實盤 bandit.enabled=true 時會【無條件覆寫】"
    "grid_spacing/take_profit_spacing(bot.py:355-358)，config 值不生效——"
    "故實驗期間必須 bandit.enabled=false 並顯式設定受測間距，否則回測與實盤跑的不是同一個策略; "
    "(5) 決策同源實盤 decide()，實盤每 10s 追價重掛(pos==0)於回測以 should_adjust 偏離門檻近似; "
    "(6) 進場量語意=固定幣量(=initial_quantity，同實盤下單)，舊/新 equity 曲線不可直接比較; "
    "(7) 成本模型(主路徑)——fee_pct 預設 maker 0.02%(網格全是限價單) + "
    "slippage_bps 執行成本 haircut(逆選擇代理，非訂單簿滑價；maker 的風險是逆選擇與排隊落空，"
    "用滑價代理量級可能差一個數量級) + funding 現金流結算"
    "(真實歷史 settlement 時點，缺漏時點 rate=0；notional 用 bar close 當 mark price 代理"
    "；funding 快取按 symbol 不按區間，同 symbol 更寬回測區間需先刪 data/funding/<symbol>.csv 重抓); "
    "(8) 【不宣稱保守下界】——成本按成交次數收，會系統性偏袒低換手方案；"
    "比較換手率差異大的方案時必須做 cost sensitivity(scripts/cost_sensitivity.py)，"
    "排序若在合理成本範圍內翻轉則不得下結論; "
    "(9) 強平模型——維持保證金=倉位名目×maintenance_margin_rate(單一費率代理幣安分層階梯)；"
    "觸發即全平並終止回測，liquidated=True 的參數組應一票否決; "
    "(10) margin_usage 為單 symbol，實盤 state.margin_usage 是帳戶層(跨 symbol)，結論不得外推; "
    "(11) legacy 路徑(initial_quantity<=0)不含成本模型、不含強平、不含 high/low 撮合。"
)
```

- [ ] **Step 7: 跑測試確認通過**

Run: `uv run python -m pytest tests/test_backtest_cost_config.py tests/web/test_backtest_service.py -q`
Expected: 全數 passed

- [ ] **Step 8: 跑全套**

Run: `uv run python -m pytest tests/ -q`
Expected: 全數 passed

- [ ] **Step 9: Commit**

```bash
git add backtest/config.py backtest/backtester.py backtest/smart_optimizer.py tests/test_backtest_cost_config.py tests/web/test_backtest_service.py
git commit -m "fix(backtest): fee_pct 改 maker 費率 + FIDELITY_NOTES 誠實化（G7）

網格全是限價 maker 單，但 fee_pct 預設是 taker 0.04%（Binance USDⓈ-M
VIP0 maker 為 0.02%）。回測對每筆成交多收一倍手續費，且 slippage 同樣
按成交次數收。

⇒ 高換手選項（關掉裝死、調高 threshold）被多罰，低換手選項（維持現狀）
   被少罰。這不是保守，是在三個受測選項之間製造系統性偏差，會直接決定排序。

FIDELITY_NOTES 移除「保守堆疊 → 屬刻意保守下界」的錯誤宣稱（撮合修正後，
幻覺價格改善已消除，但成本仍非方向中性），並補上 bandit 覆寫間距、
強平模型、margin_usage 層級三項揭露。"
```

---

## Task 7: 真實 K 線 characterization — 釘死 G4 的量級（G-0a1）

**Files:**
- Create: `tests/test_backtest_matching_realdata.py`

**Interfaces:**
- Consumes: Task 1 的 `entry_crossed`
- Produces: 無

> 這個測試把 spec 裡「漏掉 48.5% 成交」的數字變成可執行的斷言。它讀 `data/futures/um/daily/klines/BNBUSDC/1m/`，若資料不存在或 bar 數改變則 skip（資料是外部產物，不能讓它讓 CI 變紅）。

- [ ] **Step 1: 寫測試**

建立 `tests/test_backtest_matching_realdata.py`：

```python
"""用真實 1m K 線釘死「close-only 撮合」漏掉的成交量級（spec G4 / 守門 G-0a1）。

資料為外部產物（data/futures/...），缺檔或 bar 數變動即 skip —— 不讓外部
資料使 CI 變紅，但只要資料還在，這個數字就必須成立。
"""
import csv
import glob
import os

import pytest

from backtest.matching import entry_crossed

KLINE_GLOB = "data/futures/um/daily/klines/BNBUSDC/1m/*.csv"
EXPECTED_BARS = 44107
GRID_SPACING = 0.003          # 實盤有效間距（bandit arm 0），見 spec G5
EXPECTED_TOUCH = 167          # low <= limit（真實限價單成交）
EXPECTED_CLOSE_CROSS = 86     # close <= limit（舊實作的成交）


def _load_bars():
    rows = []
    for fp in sorted(glob.glob(KLINE_GLOB)):
        with open(fp) as f:
            for r in csv.reader(f):
                try:
                    rows.append((float(r[2]), float(r[3]), float(r[4])))  # high, low, close
                except (ValueError, IndexError):
                    pass
    return rows


@pytest.mark.skipif(not glob.glob(KLINE_GLOB), reason="真實 K 線資料不存在")
def test_close_only_crossing_misses_about_half_of_real_long_entry_fills():
    """舊實作（close 判穿越）漏掉約 48.5% 的真實多頭進場成交。

    limit 取上一根收盤價下方一格（= 回測掛單邏輯的簡化）。
    entry_crossed 用 low 判定 → 應得 167 次；用 close 判定 → 只有 86 次。
    """
    bars = _load_bars()
    if len(bars) != EXPECTED_BARS:
        pytest.skip(f"K 線資料已變動（{len(bars)} bars，期望 {EXPECTED_BARS}）")

    touch = close_cross = 0
    for i in range(1, len(bars)):
        _, low, close = bars[i]
        prev_close = bars[i - 1][2]
        limit = prev_close * (1 - GRID_SPACING)
        if entry_crossed("long", bar_low=low, bar_high=bars[i][0], limit=limit):
            touch += 1
        if close <= limit:
            close_cross += 1

    assert touch == EXPECTED_TOUCH
    assert close_cross == EXPECTED_CLOSE_CROSS
    missed_ratio = (touch - close_cross) / touch
    assert missed_ratio == pytest.approx(0.485, abs=0.005), (
        f"close-only 撮合漏掉 {missed_ratio:.1%} 的真實成交"
    )
```

- [ ] **Step 2: 跑測試**

Run: `uv run python -m pytest tests/test_backtest_matching_realdata.py -q -rs`
Expected: `1 passed`（若資料不存在則 `1 skipped`，此時在 commit message 註明）

- [ ] **Step 3: 跑全套**

Run: `uv run python -m pytest tests/ -q`
Expected: 全數 passed

- [ ] **Step 4: Commit**

```bash
git add tests/test_backtest_matching_realdata.py
git commit -m "test(backtest): 真實 K 線釘死 close-only 撮合漏掉的成交量級（G-0a1）

44107 根真實 BNBUSDC 1m K 線、間距 0.003（實盤有效值）：
  low  <= limit（真實成交） 167 次
  close <= limit（舊實作）   86 次
  漏掉 48.5%

資料為外部產物，缺檔或 bar 數變動即 skip。"
```

---

## Task 8: 釘死「bandit 會覆寫 config 間距」（G-0c3）

**Files:**
- Create: `tests/test_bandit_overwrites_config.py`

**Interfaces:**
- Consumes: `grid_engine.bot.MaxGridBot`、`grid_engine.config.GlobalConfig` / `SymbolConfig`、`grid_engine.enhancements.MaxEnhancement`
- Produces: 無

> **為什麼需要這個測試**：spec 原本假設「回測參數釘在 0.003/0.003 即可對標實盤」，那是把一個 bug 的產物當成穩定事實。這個測試釘死「只要 `bandit.enabled=true`，`config` 裡的 `grid_spacing` 就不生效」，防止未來再有人假設 config 值即實際值。

- [ ] **Step 1: 寫失敗的測試**

建立 `tests/test_bandit_overwrites_config.py`：

```python
"""G-0c3：bandit.enabled=true 時，config 的 grid_spacing/take_profit_spacing 不生效。

bot.py:355-358 在每個 tick 無條件用 bandit arm 覆寫這兩個欄位（不需要
all_enhancements_enabled）。生產 decisions.jsonl 60001 筆實測：實盤間距恆為
0.003/0.003（arm 0），而 config 寫的是 0.006/0.004 —— 從未生效。

⇒ 任何「照 config 建 Config 跑回測」的做法，測的都不是實盤策略。
   實驗期間必須 bandit.enabled=false 並顯式設定受測間距。
見 spec G5 / G5-bis。
"""
import pytest
from unittest.mock import AsyncMock

from grid_engine.bot import MaxGridBot
from grid_engine.config import GlobalConfig, SymbolConfig
from grid_engine.enhancements import MaxEnhancement

SYMBOL = "XRP/USDC:USDC"

CONFIG_GS = 0.006
CONFIG_TP = 0.004


def _make_bot(bandit_enabled: bool):
    cfg = GlobalConfig()
    cfg.symbols = {SYMBOL: SymbolConfig(
        symbol="XRPUSDC", ccxt_symbol=SYMBOL, enabled=True,
        take_profit_spacing=CONFIG_TP, grid_spacing=CONFIG_GS, initial_quantity=3,
        limit_multiplier=5.0, threshold_multiplier=20.0,
    )}
    cfg.max_enhancement = MaxEnhancement()
    cfg.bandit.enabled = bandit_enabled
    bot = MaxGridBot(cfg)
    bot.order_executor.place_order = AsyncMock()
    bot.order_executor.cancel_orders_for_side = AsyncMock()
    # 封鎖下單路徑：本測試只關心 config 欄位有沒有被覆寫
    bot.order_executor.is_blocked = lambda _s: True
    st = bot.state.symbols[SYMBOL]
    st.latest_price = 2.5
    st.best_bid = 2.5
    st.best_ask = 2.5
    st.long_position = 0
    st.short_position = 0
    return bot


@pytest.mark.asyncio
async def test_bandit_enabled_overwrites_config_spacing():
    bot = _make_bot(bandit_enabled=True)
    sc = bot.config.symbols[SYMBOL]
    assert sc.grid_spacing == CONFIG_GS   # 前置：config 值

    await bot._grid_step(SYMBOL, sc)

    arm = bot.bandit_optimizer.get_current_params()
    assert sc.grid_spacing == arm.grid_spacing
    assert sc.take_profit_spacing == arm.take_profit_spacing
    assert sc.grid_spacing != CONFIG_GS, (
        "bandit 沒有覆寫 config 的 grid_spacing —— 若此斷言失敗，"
        "表示 bot.py:355-358 的行為變了，spec G5 的結論需重新檢視"
    )


@pytest.mark.asyncio
async def test_bandit_disabled_preserves_config_spacing():
    """實驗前置條件：關掉 bandit，config 值才真的是實盤跑的值。"""
    bot = _make_bot(bandit_enabled=False)
    sc = bot.config.symbols[SYMBOL]

    await bot._grid_step(SYMBOL, sc)

    assert sc.grid_spacing == CONFIG_GS
    assert sc.take_profit_spacing == CONFIG_TP
```

- [ ] **Step 2: 跑測試**

Run: `uv run python -m pytest tests/test_bandit_overwrites_config.py -q`
Expected: `2 passed`（這是 characterization —— 它描述現行行為，應直接綠）

> 若 `test_bandit_enabled_overwrites_config_spacing` 紅在 `sc.grid_spacing != CONFIG_GS`，代表 bandit arm 恰好等於 config 值。此時改用 `assert sc.grid_spacing == arm.grid_spacing` 為主斷言，並在 docstring 記錄 arm 值。**不要刪掉這個測試。**

- [ ] **Step 3: 跑全套**

Run: `uv run python -m pytest tests/ -q`
Expected: 全數 passed

- [ ] **Step 4: Commit**

```bash
git add tests/test_bandit_overwrites_config.py
git commit -m "test: 釘死 bandit 會無條件覆寫 config 間距（G-0c3）

bot.py:355-358 在 bandit.enabled=true 時每 tick 覆寫 grid_spacing/
take_profit_spacing，不需要 all_enhancements_enabled。生產 decisions.jsonl
60001 筆：實盤恆為 0.003/0.003（arm 0），config 的 0.006/0.004 從未生效。

⇒ 照 config 建 Config 跑回測，測的不是實盤策略。實驗期間必須關掉 bandit。

這個測試防止未來再有人假設 config 值即實際值。"
```

---

## Task 9: 成本敏感度網格（G-0c1）

**Files:**
- Create: `scripts/cost_sensitivity.py`

**Interfaces:**
- Consumes: `backtest.backtester.GridBacktester`、`backtest.config.Config`
- Produces: CLI script，印出 fee × slippage 網格的 `final_equity` / `max_drawdown` / `trades_count` / `liquidated`

> spec G7 / §8 Phase D 要求：**若三選項排序在 fee ∈ {2,4} bps × slippage ∈ {0,1,2} bps 範圍內翻轉，則不得下結論。** 這個 script 是產生該證據的工具。

- [ ] **Step 1: 寫 script**

建立 `scripts/cost_sensitivity.py`：

```python
#!/usr/bin/env python3
"""成本敏感度網格：fee × slippage 對回測結論的影響（spec G7 / 守門 G-0c1）。

成本按成交次數收，會系統性偏袒低換手方案。比較換手率差異大的方案時
（例如「關掉裝死模式」vs「維持現狀」），成本模型的誤差可能直接決定排序。

用法:
    uv run python scripts/cost_sensitivity.py <csv_or_parquet> [--symbol BNBUSDC]

輸出：每個 (fee, slippage) 組合的 final_equity / max_drawdown / trades_count /
liquidated / peak_margin_usage。若排序在網格內翻轉，終端會明示警告。
"""
import argparse
import sys

import pandas as pd

from backtest.backtester import GridBacktester
from backtest.config import Config

FEES = [0.0002, 0.0004]              # maker 2bps / taker 4bps
SLIPPAGES = [0.0, 0.0001, 0.0002]    # 0 / 1bp / 2bps


def _load(path: str) -> pd.DataFrame:
    df = pd.read_parquet(path) if path.endswith(".parquet") else pd.read_csv(path)
    if "open_time" in df.columns and not pd.api.types.is_datetime64_any_dtype(df["open_time"]):
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    return df


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("data")
    ap.add_argument("--symbol", default="BNBUSDC")
    ap.add_argument("--initial-quantity", type=float, default=0.02)
    ap.add_argument("--grid-spacing", type=float, default=0.003)
    ap.add_argument("--take-profit-spacing", type=float, default=0.003)
    args = ap.parse_args()

    df = _load(args.data)
    print(f"{'fee(bps)':>9} {'slip(bps)':>10} {'final_equity':>14} "
          f"{'max_dd':>9} {'trades':>7} {'liq':>5} {'peak_mu':>9}")
    print("-" * 70)

    for fee in FEES:
        for slip in SLIPPAGES:
            cfg = Config(
                symbol=args.symbol,
                initial_quantity=args.initial_quantity,
                grid_spacing=args.grid_spacing,
                take_profit_spacing=args.take_profit_spacing,
                direction="both",
                terminal_ui_mode=True,
                fee_pct=fee,
                slippage_bps=slip,
            )
            r = GridBacktester(df.copy(), cfg).run()
            print(f"{fee*1e4:>9.1f} {slip*1e4:>10.1f} {r.final_equity:>14.2f} "
                  f"{r.max_drawdown:>9.4f} {r.trades_count:>7d} "
                  f"{str(r.liquidated):>5} {r.peak_margin_usage:>9.4f}")

    print()
    print("判讀：若不同成本設定下的方案排序翻轉，spec §8 Phase D 規定不得下結論。")
    print("      liquidated=True 的參數組一票否決，不進優化目標函數（spec §7）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: 實跑 smoke（用真實 K 線的一天）**

Run:
```bash
uv run python scripts/cost_sensitivity.py \
  data/futures/um/daily/klines/BNBUSDC/1m/BNBUSDC-1m-2026-06-10.csv --symbol BNBUSDC
```
Expected: 印出 6 行（2 fee × 3 slippage），每行含 `final_equity` / `liquidated` / `peak_margin_usage`。

> 若真實 K 線 CSV 無 header（幣安原始格式），先確認 `_load` 讀得到 `open_time`/`high`/`low`/`close`。若欄位是位置索引，在 `_load` 內補上 `names=[...]`。這一步必須**實際跑通**，不得只靠 code inspection。

- [ ] **Step 3: 跑全套確認無回歸**

Run: `uv run python -m pytest tests/ -q`
Expected: 全數 passed

- [ ] **Step 4: Commit**

```bash
git add scripts/cost_sensitivity.py
git commit -m "feat(scripts): 成本敏感度網格（G-0c1）

fee ∈ {2,4} bps × slippage ∈ {0,1,2} bps，輸出 final_equity / max_drawdown /
trades_count / liquidated / peak_margin_usage。

成本按成交次數收，系統性偏袒低換手方案。比較換手率差異大的方案時
（關掉裝死 vs 維持現狀），成本誤差可能直接決定排序。spec §8 Phase D：
排序若在此網格內翻轉，不得下結論。"
```

---

## Phase 0 完成條件（Definition of Done）

全部滿足才算完成，逐條實測、不接受宣稱：

- [ ] `uv run python -m pytest tests/ -q` 全數 passed，且新增測試數 ≥ 31
- [ ] **G-0a1**：`tests/test_backtest_matching_realdata.py` passed（或因資料缺失 skip 且已記錄）
- [ ] **G-0a2**：`test_long_entry_fills_at_limit_price_not_at_close` passed —— 零成本下成交價嚴格等於掛單價
- [ ] **G-0b0**：`test_final_equity_includes_margin_locked_in_open_positions` passed
- [ ] **G-0b1**：`test_relentless_downtrend_with_no_dead_mode_triggers_liquidation` passed 且回測提前終止
- [ ] **G-0b2**：`test_normal_range_bound_market_does_not_liquidate` passed
- [ ] **G-0c1**：`scripts/cost_sensitivity.py` 對真實 K 線實跑通過，印出 6 行
- [ ] **G-0c2**：`FIDELITY_NOTES` 不再出現「保守下界」字樣（`grep -c "保守下界" backtest/backtester.py` == 0）
- [ ] **G-0c3**：`tests/test_bandit_overwrites_config.py` passed
- [ ] **驗收不自我背書**：派 fresh-context `verifier` subagent 重新讀檔 + 實跑測試，且以 mutate-and-restore 證明新測試守得住（把 `_settle` 改回 close 判穿越 → 相關測試必須轉紅）
  - **還原手段不得依賴 git**：先 `cp` 到 `$(mktemp -d)` 再 `cp` 回來；prompt 明文禁止一切 git 寫入指令（見 `tasks/lessons.md` 2026-07-10）
- [ ] **dual-review** 產出 `Ship as-is` verdict（`/dual-review`）

## Phase 0 之後

Phase 0 改變了回測數字。**Phase A 的 golden 基準必須是 Phase 0 完成後的 backtester**，不是原始版本（spec §8 G-A1 註）。

Phase A/B/C 的實作計畫在 Phase 0 完成、拿到實際數字之後再寫 —— 避免在錯誤基準上規劃 golden。

---

## Self-Review 記錄

**Spec 覆蓋率**：
- G4（撮合兩個錯）→ Task 1, 2, 7 ✅
- G5 / G5-bis（bandit 覆寫間距）→ Task 6（FIDELITY_NOTES）、Task 8（斷言）✅
- G6（無爆倉建模）→ Task 5 ✅
- G7（成本非方向中性）→ Task 6, 9 ✅
- G8（權益核算）→ Task 3 ✅
- 守門 G-0a1 / G-0a2 / G-0b0 / G-0b1 / G-0b2 / G-0c1 / G-0c2 / G-0c3 → 全數對應到具體測試 ✅
- spec §7 的 `dead_mode_pct_long/_short` → **不在 Phase 0**（屬 Phase A，需 `dead` 狀態追蹤）。已於「Phase 0 之後」註明。

**型別一致性**：
- `entry_crossed` / `tp_crossed` 簽名 `(side, bar_low, bar_high, limit) -> bool`，Task 1 定義、Task 2 與 Task 7 消費，一致。
- `margin_usage(long_pos, short_pos, price, leverage, equity) -> float`，Task 4 定義、Task 5 主迴圈消費，一致。
- `should_liquidate(equity, long_pos, short_pos, price, maintenance_margin_rate) -> bool`，Task 5 定義並消費，一致。
- `BacktestResult` 新欄位順序：`peak_margin_usage`（Task 4）→ `liquidated`（Task 5），皆有預設值，不破壞既有位置參數呼叫。

**已知風險**：
- Task 2 會讓部分既有 backtester 測試轉紅（成交價語意變更）。Step 7 明列處理原則，並禁止用放寬容差的方式繞過。
- Task 8 的 `test_bandit_enabled_overwrites_config_spacing` 依賴「cold-start arm 的間距 ≠ config 值」。已在 Step 2 給出 arm 恰好相等時的處置。
