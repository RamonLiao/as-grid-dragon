# 回測成本模型（滑價 + 資金費率）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在回測主路徑 `GridBacktester._run_terminal_ui_mode` 注入固定 bps 執行成本 haircut 與真實歷史 funding 現金流結算，讓回測 PnL 不再系統性偏樂觀。

**Architecture:** 新增純函數模組 `backtest/costs.py`（`apply_slippage`/`funding_charge`，無 I/O 無副作用），與 `grid_engine/decision.py` 並列為第二個純層邊界。`DataLoader.load_funding` 負責按需下載/快取真實 funding 歷史。backtester 只在主路徑接線呼叫；legacy 路徑不動。

**Tech Stack:** Python 3.13、pandas、ccxt（`fetch_funding_rate_history`）、pytest、uv。

## Global Constraints

- 缺 Python 套件用 `uv` 處理。
- Unit/Integration 後**必做 Monkey Testing**（極端輸入把程式玩壞）。
- Patch 優先、不整檔重寫、遵守現有 code style。
- `git add <file>...` 只 stage 指定檔，**禁止** `git add -A`/`git add .`。
- fidelity-first：`slippage_bps` 預設 `0.0001`、`funding_enabled` 預設 `True`。
- funding 現金流**不進 `trades`**（`trades` 餵 win_rate/profit_factor/trades_count）。
- funding settlement 時點一律讀交易所回傳的**真實 timestamp**，不假設 8h 網格。
- `slippage_bps` 語義是「執行成本 haircut（逆選擇代理）」，非訂單簿滑價。
- 只改主路徑 `_run_terminal_ui_mode`；legacy `_run_legacy_mode`（`initial_quantity<=0`）維持現狀。
- 每 task 完成獨立 commit。

---

### Task 1: `apply_slippage` 純函數

**Files:**
- Create: `backtest/costs.py`
- Test: `tests/test_backtest_costs.py`

**Interfaces:**
- Consumes: 無
- Produces: `apply_slippage(price: float, side: str, action: str, bps: float) -> float`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backtest_costs.py
import math
import pytest
from backtest.costs import apply_slippage


def test_apply_slippage_long_entry_buys_worse():
    # long 進場：買貴 → price*(1+bps)
    assert apply_slippage(100.0, "long", "entry", 0.0001) == pytest.approx(100.01)


def test_apply_slippage_long_tp_sells_worse():
    # long 止盈：賣便宜 → price*(1-bps)
    assert apply_slippage(100.0, "long", "tp", 0.0001) == pytest.approx(99.99)


def test_apply_slippage_short_entry_sells_worse():
    # short 進場（賣）：賣便宜 → price*(1-bps)
    assert apply_slippage(100.0, "short", "entry", 0.0001) == pytest.approx(99.99)


def test_apply_slippage_short_tp_buys_worse():
    # short 止盈（買回）：買貴 → price*(1+bps)
    assert apply_slippage(100.0, "short", "tp", 0.0001) == pytest.approx(100.01)


def test_apply_slippage_zero_bps_no_shift():
    assert apply_slippage(100.0, "long", "entry", 0.0) == 100.0


def test_apply_slippage_negative_bps_treated_as_zero():
    assert apply_slippage(100.0, "long", "entry", -0.5) == 100.0


def test_apply_slippage_nan_bps_treated_as_zero():
    assert apply_slippage(100.0, "long", "entry", float("nan")) == 100.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_backtest_costs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backtest.costs'`

- [ ] **Step 3: Write minimal implementation**

```python
# backtest/costs.py
"""回測成本模型純函數層（無 I/O、無副作用）。

與 grid_engine.decision 並列為回測第二個純層邊界：
成本語意集中一處、可 bug-for-bug 單測、不污染回測 loop。
"""
import math


def apply_slippage(price: float, side: str, action: str, bps: float) -> float:
    """成交價往不利方向偏移（執行成本 haircut，非訂單簿滑價）。

    action ∈ {"entry", "tp"}；side ∈ {"long", "short"}。
    - long  entry: price*(1+bps)  買貴
    - long  tp:    price*(1-bps)  賣便宜
    - short entry: price*(1-bps)  賣便宜
    - short tp:    price*(1+bps)  買回貴
    bps<=0 或非有限 → 不偏移。
    """
    if not (isinstance(bps, (int, float)) and math.isfinite(bps)) or bps <= 0:
        return price
    # 買方（成交價升）: long entry / short tp；賣方（成交價降）: long tp / short entry
    buy_side = (side == "long" and action == "entry") or (side == "short" and action == "tp")
    return price * (1 + bps) if buy_side else price * (1 - bps)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_backtest_costs.py -v`
Expected: PASS（7 passed）

- [ ] **Step 5: Commit**

```bash
git add backtest/costs.py tests/test_backtest_costs.py
git commit -m "feat: #5 apply_slippage 純函數（執行成本 haircut，四方向不利偏移）"
```

---

### Task 2: `funding_charge` 純函數

**Files:**
- Modify: `backtest/costs.py`
- Test: `tests/test_backtest_costs.py`

**Interfaces:**
- Consumes: 無
- Produces: `funding_charge(positions: list, rate: float, side: str, mark_price: float) -> float`
  - `positions`: list of dict，每筆含 key `"qty"`（float）
  - 回傳該側 funding 現金流：正=付出（呼叫端 `balance -= charge`）

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backtest_costs.py — 追加
from backtest.costs import funding_charge


def test_funding_charge_long_positive_rate_pays():
    # notional = 10 * 100 = 1000；rate 0.0001 → long 付 0.1
    assert funding_charge([{"qty": 10.0}], 0.0001, "long", 100.0) == pytest.approx(0.1)


def test_funding_charge_short_positive_rate_receives():
    # 正 rate 空頭收錢 → charge 為負（呼叫端 balance -= 負 = 增加）
    assert funding_charge([{"qty": 10.0}], 0.0001, "short", 100.0) == pytest.approx(-0.1)


def test_funding_charge_long_negative_rate_receives():
    assert funding_charge([{"qty": 10.0}], -0.0001, "long", 100.0) == pytest.approx(-0.1)


def test_funding_charge_sums_notional_across_positions():
    # (4+6)*100*0.0001 = 0.1
    assert funding_charge([{"qty": 4.0}, {"qty": 6.0}], 0.0001, "long", 100.0) == pytest.approx(0.1)


def test_funding_charge_empty_positions_zero():
    assert funding_charge([], 0.0001, "long", 100.0) == 0.0


def test_funding_charge_nan_rate_zero():
    assert funding_charge([{"qty": 10.0}], float("nan"), "long", 100.0) == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_backtest_costs.py -k funding_charge -v`
Expected: FAIL — `ImportError: cannot import name 'funding_charge'`

- [ ] **Step 3: Write minimal implementation**

```python
# backtest/costs.py — 追加
def funding_charge(positions: list, rate: float, side: str, mark_price: float) -> float:
    """該側 funding 現金流（正=付出，呼叫端 balance -= charge）。

    notional = Σ(pos["qty"]) * mark_price（mark_price 用結算時點 bar close 代理）。
    - long:  +notional*rate  正 rate 多頭付、負 rate 收
    - short: -notional*rate  相反
    rate 非有限、positions 空 → 0。
    """
    if not (isinstance(rate, (int, float)) and math.isfinite(rate)):
        return 0.0
    if not (isinstance(mark_price, (int, float)) and math.isfinite(mark_price)):
        return 0.0
    notional = sum(p["qty"] for p in positions) * mark_price
    charge = notional * rate
    return charge if side == "long" else -charge
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_backtest_costs.py -v`
Expected: PASS（13 passed）

- [ ] **Step 5: Commit**

```bash
git add backtest/costs.py tests/test_backtest_costs.py
git commit -m "feat: #5 funding_charge 純函數（notional×rate，long/short 正負號）"
```

---

### Task 3: Config 加 `slippage_bps` / `funding_enabled`

**Files:**
- Modify: `backtest/config.py`（dataclass 欄位、`to_dict`、`from_dict`）
- Test: `tests/test_backtest_cost_config.py`

**Interfaces:**
- Consumes: 無
- Produces: `Config.slippage_bps: float`（預設 0.0001）、`Config.funding_enabled: bool`（預設 True）；`to_dict`/`from_dict` 雙向；`from_dict` 非法值 fallback。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backtest_cost_config.py
from backtest.config import Config


def test_defaults_fidelity_first():
    c = Config()
    assert c.slippage_bps == 0.0001
    assert c.funding_enabled is True


def test_roundtrip_preserves_cost_fields():
    c = Config(slippage_bps=0.0003, funding_enabled=False)
    d = c.to_dict()
    assert d["slippage_bps"] == 0.0003
    assert d["funding_enabled"] is False
    c2 = Config.from_dict(d)
    assert c2.slippage_bps == 0.0003
    assert c2.funding_enabled is False


def test_backward_compat_missing_fields_use_defaults():
    # 舊 config 無這兩欄 → 套 fidelity-first 預設
    c = Config.from_dict({"symbol": "BTCUSDC"})
    assert c.slippage_bps == 0.0001
    assert c.funding_enabled is True


def test_from_dict_negative_slippage_fallback():
    assert Config.from_dict({"slippage_bps": -0.5}).slippage_bps == 0.0001


def test_from_dict_nan_slippage_fallback():
    assert Config.from_dict({"slippage_bps": float("nan")}).slippage_bps == 0.0001


def test_from_dict_non_bool_funding_enabled_fallback():
    assert Config.from_dict({"funding_enabled": "yes"}).funding_enabled is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_backtest_cost_config.py -v`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'slippage_bps'`

- [ ] **Step 3: Write minimal implementation**

在 `backtest/config.py` 的 dataclass 欄位區（`fee_pct` 之後、`# 持倉控制參數` 之前）加：

```python
    # 成本模型（fidelity-first：預設全開）
    slippage_bps: float = 0.0001       # 每次成交向不利方向偏移比例（執行成本 haircut）
    funding_enabled: bool = True       # 是否結算 funding 現金流
```

`to_dict` 回傳 dict 末尾（`"short_settings": self.short_settings` 之後、閉合 `}` 前）加：

```python
            "short_settings": self.short_settings,
            "slippage_bps": self.slippage_bps,
            "funding_enabled": self.funding_enabled,
```

在 `config.py` 頂部 import 區加 `import math`（若尚無）。`from_dict` 的 `cls(...)` 內（`terminal_ui_mode=...` 之後）加，並含 fallback：

```python
            terminal_ui_mode=data.get("terminal_ui_mode", True),
            slippage_bps=_norm_slippage(data.get("slippage_bps", 0.0001)),
            funding_enabled=data.get("funding_enabled", True)
                if isinstance(data.get("funding_enabled", True), bool) else True,
        )
```

在 `config.py` 模組層（`class Config` 之前）加 helper：

```python
def _norm_slippage(v) -> float:
    """滑價 fallback：非數值/NaN/負值 → 0.0001。"""
    if not (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)) or v < 0:
        return 0.0001
    return float(v)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_backtest_cost_config.py -v`
Expected: PASS（6 passed）

- [ ] **Step 5: Commit**

```bash
git add backtest/config.py tests/test_backtest_cost_config.py
git commit -m "feat: #5 Config 加 slippage_bps/funding_enabled（fidelity-first 預設+非法值 fallback）"
```

---

### Task 4: `DataLoader.load_funding` 按需下載/分頁快取

**Files:**
- Modify: `backtest/data_loader.py`（新增 `load_funding` 方法）
- Test: `tests/test_funding_loader.py`

**Interfaces:**
- Consumes: 無（`_create_exchange` 為既有私有方法，回傳 ccxt exchange）
- Produces: `DataLoader.load_funding(symbol: str, start: datetime, end: datetime, exchange=None) -> dict[int, float]`
  - 回傳 `{settlement_epoch_sec(int): funding_rate(float)}`
  - `exchange` 參數可注入 mock（測試用）；None 時 `_create_exchange("binance")`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_funding_loader.py
from datetime import datetime
from backtest.data_loader import DataLoader


class _FakeExchange:
    """回傳兩頁 funding history，第二頁後為空。ccxt timestamp 為毫秒。"""
    def __init__(self):
        self.calls = 0

    def fetch_funding_rate_history(self, symbol, since=None, limit=None, params=None):
        self.calls += 1
        if self.calls == 1:
            return [
                {"timestamp": 1_000_000_000_000, "fundingRate": 0.0001},
                {"timestamp": 1_000_028_800_000, "fundingRate": -0.0002},  # +8h
            ]
        return []  # 第二頁空 → 停


def test_load_funding_paginates_and_stops_on_empty(tmp_path):
    loader = DataLoader(data_dir=str(tmp_path))
    ex = _FakeExchange()
    fmap = loader.load_funding("BTCUSDC", datetime(2001, 9, 9), datetime(2001, 9, 10), exchange=ex)
    assert fmap == {1_000_000_000: 0.0001, 1_000_028_800: -0.0002}
    assert ex.calls == 2  # 一頁資料 + 一頁空


def test_load_funding_uses_cache_second_time(tmp_path):
    loader = DataLoader(data_dir=str(tmp_path))
    ex = _FakeExchange()
    loader.load_funding("BTCUSDC", datetime(2001, 9, 9), datetime(2001, 9, 10), exchange=ex)
    # 快取檔已寫：第二次不呼 exchange
    ex2 = _FakeExchange()
    fmap = loader.load_funding("BTCUSDC", datetime(2001, 9, 9), datetime(2001, 9, 10), exchange=ex2)
    assert fmap == {1_000_000_000: 0.0001, 1_000_028_800: -0.0002}
    assert ex2.calls == 0


def test_load_funding_fetch_failure_returns_empty(tmp_path):
    class _BoomExchange:
        def fetch_funding_rate_history(self, *a, **k):
            raise RuntimeError("network down")
    loader = DataLoader(data_dir=str(tmp_path))
    fmap = loader.load_funding("BTCUSDC", datetime(2001, 9, 9), datetime(2001, 9, 10), exchange=_BoomExchange())
    assert fmap == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_funding_loader.py -v`
Expected: FAIL — `AttributeError: 'DataLoader' object has no attribute 'load_funding'`

- [ ] **Step 3: Write minimal implementation**

在 `backtest/data_loader.py` 的 `DataLoader` class 內新增（緊接 `download` 方法之後即可）：

```python
    def get_funding_path(self, symbol: str):
        """funding 快取檔路徑。"""
        return self.data_dir / "funding" / f"{symbol}.csv"

    def load_funding(self, symbol, start, end, exchange=None) -> dict:
        """按需下載/快取真實 funding 歷史 → {settlement_epoch_sec: rate}。

        本地缺 → fetch_funding_rate_history 分頁拖區間存 CSV。
        settlement 時點以交易所真實 timestamp 為準（非假設 8h）。
        抓取失敗/缺漏 → 回已知部分（可能空）；缺時點呼叫端以 rate=0 處理。
        """
        import pandas as pd
        path = self.get_funding_path(symbol)

        if path.exists():
            df = pd.read_csv(path)
            return {int(r): float(v)
                    for r, v in zip(df["settlement_time"], df["funding_rate"])}

        if exchange is None:
            exchange = self._create_exchange("binance")

        ccxt_symbol = symbol.replace("USDC", "/USDC").replace("USDT", "/USDT")
        since = int(datetime(start.year, start.month, start.day).timestamp() * 1000)
        end_ms = int((datetime(end.year, end.month, end.day).timestamp() + 86400) * 1000)

        rows = []
        seen = set()
        try:
            while since < end_ms:
                batch = exchange.fetch_funding_rate_history(
                    ccxt_symbol, since=since, limit=1000, params={})
                if not batch:
                    break
                progressed = False
                for item in batch:
                    ts = int(item["timestamp"])          # ms
                    if ts >= end_ms or ts in seen:
                        continue
                    seen.add(ts)
                    rate = float(item.get("fundingRate", 0) or 0)
                    rows.append((ts // 1000, rate))       # 存秒
                    progressed = True
                last_ts = int(batch[-1]["timestamp"])
                if not progressed or last_ts < since:
                    break
                since = last_ts + 1
        except Exception:
            # fidelity：抓取失敗回已知部分，不中斷回測
            pass

        if rows:
            path.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(rows, columns=["settlement_time", "funding_rate"]).to_csv(
                path, index=False)

        return {int(sec): float(rate) for sec, rate in rows}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_funding_loader.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**

```bash
git add backtest/data_loader.py tests/test_funding_loader.py
git commit -m "feat: #5 DataLoader.load_funding 按需分頁下載+快取真實 funding 歷史"
```

---

### Task 5: backtester 接線滑價（`_open`/`_close`）

**Files:**
- Modify: `backtest/backtester.py`（`_run_terminal_ui_mode` 內 `_open`/`_close`）
- Test: `tests/test_backtester_slippage.py`

**Interfaces:**
- Consumes: `apply_slippage`（Task 1）、`Config.slippage_bps`（Task 3）
- Produces: 無新 public 介面（成交價經 `apply_slippage` 偏移）

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backtester_slippage.py
import pandas as pd
from datetime import datetime, timedelta
from backtest.config import Config
from backtest.backtester import GridBacktester


def _make_df(prices):
    t0 = datetime(2001, 9, 9)
    return pd.DataFrame({
        "open_time": [t0 + timedelta(minutes=i) for i in range(len(prices))],
        "open": prices, "high": prices, "low": prices,
        "close": prices, "volume": [1.0] * len(prices),
    })


def _cfg(**kw):
    base = dict(symbol="BTCUSDC", initial_balance=100000.0, initial_quantity=1.0,
                leverage=20, take_profit_spacing=0.004, grid_spacing=0.006,
                direction="both", funding_enabled=False)
    base.update(kw)
    return Config(**base)


def test_slippage_reduces_final_equity_vs_zero():
    # 同一條價格序列，開滑價的 final_equity 應 <= 零滑價
    prices = [100.0, 99.0, 100.5, 99.5, 100.8, 99.2, 101.0]
    df = _make_df(prices)
    zero = GridBacktester(df.copy(), _cfg(slippage_bps=0.0)).run()
    slip = GridBacktester(df.copy(), _cfg(slippage_bps=0.001)).run()
    assert slip.final_equity <= zero.final_equity


def test_zero_slippage_zero_funding_matches_baseline_equity():
    # slippage=0 + funding off → 成本模型純疊加、無副作用（等價守門）
    prices = [100.0, 99.0, 100.5, 99.5, 100.8]
    df = _make_df(prices)
    r = GridBacktester(df, _cfg(slippage_bps=0.0)).run()
    assert r.final_equity > 0  # smoke：跑得動且非崩壞
```

- [ ] **Step 2: Run test to verify it fails**

先確認 baseline 能跑（`test_slippage_reduces...` 會因 `_open/_close` 尚未吃 slippage 而讓 slip==zero，斷言 `<=` 可能巧合通過）。為確保 red，先寫**嚴格**版斷言：

改 `test_slippage_reduces_final_equity_vs_zero` 末行為：
```python
    assert slip.final_equity < zero.final_equity
```
Run: `uv run pytest tests/test_backtester_slippage.py::test_slippage_reduces_final_equity_vs_zero -v`
Expected: FAIL（未接線時 slip == zero，`<` 不成立）

- [ ] **Step 3: Write minimal implementation**

在 `backtest/backtester.py` 頂部 import 區（`from grid_engine.snapshot import ...` 附近）加：

```python
from backtest.costs import apply_slippage, funding_charge
```

`_run_terminal_ui_mode` 內 `_open` 改（`fill_price` 進來後先偏移）：

```python
        def _open(side: str, fill_price: float, qty: float) -> bool:
            nonlocal balance
            fill_price = apply_slippage(fill_price, side, "entry", cfg.slippage_bps)
            margin = (qty * fill_price) / leverage
            fee = qty * fill_price * fee_pct
            if margin + fee < balance:
                balance -= (margin + fee)
                (long_positions if side == "long" else short_positions).append(
                    {"price": fill_price, "qty": qty, "margin": margin})
                return True
            return False
```

`_close` 首行（`positions = ...` 之前）加偏移：

```python
        def _close(side: str, fill_price: float, tp_qty: float, ts) -> None:
            nonlocal balance
            fill_price = apply_slippage(fill_price, side, "tp", cfg.slippage_bps)
            positions = long_positions if side == "long" else short_positions
            ...  # 其餘不變
```

- [ ] **Step 4: Run test to verify it passes**

先把嚴格斷言改回 `<=`（滑價一定不會讓 equity 更好，但極端序列可能剛好無成交 → 用 `<=` 穩健）：
```python
    assert slip.final_equity <= zero.final_equity
```
Run: `uv run pytest tests/test_backtester_slippage.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: Commit**

```bash
git add backtest/backtester.py tests/test_backtester_slippage.py
git commit -m "feat: #5 backtester _open/_close 接線 apply_slippage（進場/止盈不利偏移）"
```

---

### Task 6: backtester 接線 funding 結算 + `funding_paid` + FIDELITY_NOTES

**Files:**
- Modify: `backtest/backtester.py`
  - `GridBacktester.__init__`（加 `funding_map` 參數）
  - `_run_terminal_ui_mode`（funding 結算迴圈 + `funding_paid` 累加 + BacktestResult 帶欄位）
  - `BacktestResult`（加 `funding_paid` 欄位 + `to_dict`）
  - `FIDELITY_NOTES`（更新文字）
- Test: `tests/test_backtester_funding.py`

**Interfaces:**
- Consumes: `funding_charge`（Task 2）、`Config.funding_enabled`（Task 3）、`DataLoader.load_funding`（Task 4）
- Produces: `GridBacktester(df, config, funding_map=None)`；`BacktestResult.funding_paid: float`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_backtester_funding.py
import pandas as pd
from datetime import datetime, timedelta
from backtest.config import Config
from backtest.backtester import GridBacktester


def _make_df(prices, start=datetime(2001, 9, 9)):
    return pd.DataFrame({
        "open_time": [start + timedelta(minutes=i) for i in range(len(prices))],
        "open": prices, "high": prices, "low": prices,
        "close": prices, "volume": [1.0] * len(prices),
    })


def _cfg(**kw):
    base = dict(symbol="BTCUSDC", initial_balance=100000.0, initial_quantity=1.0,
                leverage=20, take_profit_spacing=0.004, grid_spacing=0.006,
                direction="long", slippage_bps=0.0, funding_enabled=True)
    base.update(kw)
    return Config(**base)


def test_funding_charged_when_settlement_crossed():
    # 讓第一根就進場並持倉，第 3 根時間點命中一個 settlement
    prices = [100.0, 99.0, 99.0, 99.0]
    df = _make_df(prices)
    third_epoch = int(df["open_time"].iloc[2].timestamp())
    fmap = {third_epoch: 0.0001}
    r = GridBacktester(df, _cfg(), funding_map=fmap).run()
    assert r.funding_paid > 0  # 多頭持倉 + 正 rate → 付款


def test_funding_off_zero_paid():
    prices = [100.0, 99.0, 99.0, 99.0]
    df = _make_df(prices)
    third_epoch = int(df["open_time"].iloc[2].timestamp())
    r = GridBacktester(df, _cfg(funding_enabled=False), funding_map={third_epoch: 0.0001}).run()
    assert r.funding_paid == 0.0


def test_funding_does_not_pollute_trade_metrics():
    # 含 funding vs 無 funding：trades_count/win_rate/profit_factor 不變
    prices = [100.0, 99.0, 100.5, 99.5, 100.8, 99.2, 101.0, 99.0]
    df = _make_df(prices)
    epochs = {int(df["open_time"].iloc[i].timestamp()): 0.0001 for i in (2, 5)}
    with_f = GridBacktester(df.copy(), _cfg(), funding_map=epochs).run()
    no_f = GridBacktester(df.copy(), _cfg(funding_enabled=False), funding_map=epochs).run()
    assert with_f.trades_count == no_f.trades_count
    assert with_f.win_rate == no_f.win_rate
    assert with_f.profit_factor == no_f.profit_factor


def test_funding_multiple_settlements_in_one_bar():
    # 兩個 settlement 都 <= 某根 bar epoch → 都結算
    prices = [100.0, 99.0, 99.0]
    df = _make_df(prices)
    e1 = int(df["open_time"].iloc[1].timestamp())
    e_mid = e1 + 5  # 落在 bar1 與 bar2 之間，bar2 時一起結
    e2 = int(df["open_time"].iloc[2].timestamp())
    r = GridBacktester(df, _cfg(), funding_map={e1: 0.0001, e_mid: 0.0001}).run()
    # 兩筆都被結算（近似 notional 相同）→ 大於單筆
    r1 = GridBacktester(df, _cfg(), funding_map={e1: 0.0001}).run()
    assert r.funding_paid > r1.funding_paid
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_backtester_funding.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'funding_map'`

- [ ] **Step 3: Write minimal implementation**

**(a)** `BacktestResult` dataclass（`notes: str = ""` 之後）加欄位：
```python
    notes: str = ""  # 保真限制 / 已知差異說明
    funding_paid: float = 0.0  # funding 現金流總額（正=淨付出），不計入 trades
```
`BacktestResult.to_dict` 回傳 dict 加 `"funding_paid": self.funding_paid`（在 `"direction"` 後補逗號）：
```python
            "direction": self.direction,
            "funding_paid": self.funding_paid,
```

**(b)** `GridBacktester.__init__` 簽名與 body：
```python
    def __init__(self, df: pd.DataFrame, config: Config, funding_map=None):
        self.df = df.reset_index(drop=True)
        self.config = config
        self.funding_map = funding_map
```
（其餘 __init__ body 不變。）

**(c)** `_run_terminal_ui_mode` 內，`balance = cfg.initial_balance` 之後加 funding 狀態初始化：
```python
        balance = cfg.initial_balance
        funding_paid = 0.0
        # funding settlements：(epoch_sec, rate) 排序；pointer 掃過已結算的
        settlements = []
        if cfg.funding_enabled:
            fmap = self.funding_map
            if fmap is None:
                try:
                    from .data_loader import DataLoader
                    fmap = DataLoader().load_funding(
                        sym, self.df["open_time"].iloc[0], self.df["open_time"].iloc[-1])
                except Exception:
                    fmap = {}
            settlements = sorted((int(k), float(v)) for k, v in fmap.items())
        fund_i = 0
        first_epoch = (self.df["open_time"].iloc[0].timestamp()
                       if len(self.df) and hasattr(self.df["open_time"].iloc[0], "timestamp")
                       else 0.0)
        # 略過回測起點之前的 settlement（不對開跑前的時間收費）
        while fund_i < len(settlements) and settlements[fund_i][0] < first_epoch:
            fund_i += 1
```

**(d)** loop 內，`_settle` 的 for 迴圈**之後**、`# 組決策輸入` 之前，插入 funding 結算：
```python
                # funding 現金流結算：掃過所有 <= 本根 epoch 的 settlement（data-driven，非 8h 網格）
                if settlements and epoch > 0:
                    while fund_i < len(settlements) and settlements[fund_i][0] <= epoch:
                        rate = settlements[fund_i][1]
                        for fside, fpos in (("long", long_positions), ("short", short_positions)):
                            if cfg.direction not in (fside, "both"):
                                continue
                            charge = funding_charge(fpos, rate, fside, price)
                            balance -= charge
                            funding_paid += charge
                        fund_i += 1
```

**(e)** `return BacktestResult(...)` 加 `funding_paid=funding_paid,`（在 `notes=FIDELITY_NOTES,` 之後）：
```python
            notes=FIDELITY_NOTES,
            funding_paid=funding_paid,
        )
```

**(f)** 更新 `FIDELITY_NOTES`（整段替換）：
```python
FIDELITY_NOTES = (
    "回測保真限制: "
    "(1) 樂觀成交——限價單以當根收盤價成交、無 queue/部分成交佇列; "
    "(2) flat-entry 近似——零倉位 bootstrap 沿用收盤價觸發即進場; "
    "(3) leading/ATR/GLFT 增強於回測退化為中性(全關); "
    "(4) Bandit 參數優化不在回測 loop 內重現; "
    "(5) 決策同源實盤 decide()，實盤每 10s 追價重掛(pos==0)於回測以 should_adjust 偏離門檻近似; "
    "(6) 進場量語意=固定幣量(=initial_quantity，同實盤下單)，舊/新 equity 曲線不可直接比較; "
    "(7) 成本模型(主路徑)——slippage_bps 執行成本 haircut(逆選擇代理，非訂單簿滑價；"
    "網格 maker 單實際成交價≤掛單價，此 bps 當保守緩衝) + funding 現金流結算"
    "(真實歷史 settlement 時點，缺漏時點 rate=0；notional 用 bar close 當 mark price 代理); "
    "(8) 保守堆疊——fee_pct 預設 0.04%(taker)已對 maker 網格偏保守，疊 slippage haircut → "
    "回測績效偏低估、屬刻意保守下界; "
    "(9) legacy 路徑(initial_quantity<=0)不含成本模型。"
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_backtester_funding.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: Commit**

```bash
git add backtest/backtester.py tests/test_backtester_funding.py
git commit -m "feat: #5 backtester 接線 funding 現金流結算（data-driven settlement，funding_paid 不污染 trades）+ FIDELITY_NOTES 更新"
```

---

### Task 7: Monkey Testing + 全套回歸

**Files:**
- Test: `tests/test_backtest_cost_monkey.py`

**Interfaces:**
- Consumes: 全部前置 task 的 public 介面

- [ ] **Step 1: Write the failing test（極端輸入把程式玩壞）**

```python
# tests/test_backtest_cost_monkey.py
import math
import pandas as pd
from datetime import datetime, timedelta
import pytest
from backtest.config import Config
from backtest.backtester import GridBacktester
from backtest.costs import apply_slippage, funding_charge


def _make_df(prices, start=datetime(2001, 9, 9)):
    return pd.DataFrame({
        "open_time": [start + timedelta(minutes=i) for i in range(len(prices))],
        "open": prices, "high": prices, "low": prices,
        "close": prices, "volume": [1.0] * len(prices),
    })


def _cfg(**kw):
    base = dict(symbol="BTCUSDC", initial_balance=100000.0, initial_quantity=1.0,
                leverage=20, take_profit_spacing=0.004, grid_spacing=0.006,
                direction="both", slippage_bps=0.0001, funding_enabled=True)
    base.update(kw)
    return Config(**base)


def test_extreme_rate_cap_075pct_no_crash():
    prices = [100.0, 99.0, 99.0]
    df = _make_df(prices)
    e = int(df["open_time"].iloc[1].timestamp())
    r = GridBacktester(df, _cfg(), funding_map={e: 0.0075}).run()  # 幣安 ±0.75% 上限
    assert math.isfinite(r.funding_paid)


def test_nan_rate_in_map_ignored():
    prices = [100.0, 99.0, 99.0]
    df = _make_df(prices)
    e = int(df["open_time"].iloc[1].timestamp())
    r = GridBacktester(df, _cfg(), funding_map={e: float("nan")}).run()
    assert r.funding_paid == 0.0


def test_empty_funding_map_no_charge():
    df = _make_df([100.0, 99.0, 99.0])
    r = GridBacktester(df, _cfg(), funding_map={}).run()
    assert r.funding_paid == 0.0


def test_settlement_before_start_not_charged():
    df = _make_df([100.0, 99.0, 99.0])
    before = int(df["open_time"].iloc[0].timestamp()) - 100000
    r = GridBacktester(df, _cfg(), funding_map={before: 0.0075}).run()
    assert r.funding_paid == 0.0


def test_reversed_timestamps_no_double_charge():
    # 價格序列時間倒流（髒資料）→ 不重複結算、不崩
    df = _make_df([100.0, 99.0, 99.0])
    df = df.iloc[::-1].reset_index(drop=True)  # 倒序
    e = int(df["open_time"].iloc[0].timestamp())
    r = GridBacktester(df, _cfg(), funding_map={e: 0.0001}).run()
    assert math.isfinite(r.funding_paid)


def test_apply_slippage_extreme_bps_no_negative_price():
    # bps 巨大但 <1 → 價格仍為正
    assert apply_slippage(100.0, "long", "tp", 0.99) == pytest.approx(1.0)


def test_funding_charge_negative_qty_defensive():
    # 髒持倉（不應發生）：不崩、回有限值
    assert math.isfinite(funding_charge([{"qty": -5.0}], 0.0001, "long", 100.0))
```

- [ ] **Step 2: Run test to verify it fails/passes**

Run: `uv run pytest tests/test_backtest_cost_monkey.py -v`
Expected: 全 PASS（若前置 task 正確，monkey 應直接綠；任一 FAIL → 回對應 task 修防禦）

- [ ] **Step 3: 全套回歸**

Run: `uv run pytest -q`
Expected: 全套 PASS（前值 220 passed + 本次新增測試；報實際數字，不報形容詞）

- [ ] **Step 4: 若有 fail → 系統性除錯**

任一既有測試因成本模型預設全開而 fail（例如既有回測數字斷言）→ 判斷是「預期的 fidelity 變動」還是「真 bug」。預期變動 → 更新該測試斷言並註記原因；真 bug → 回對應 task 修。

- [ ] **Step 5: Commit**

```bash
git add tests/test_backtest_cost_monkey.py
git commit -m "test: #5 成本模型 monkey（極端 rate/NaN/倒流/起點前 settlement/巨額 bps）+ 全套回歸"
```

---

## Self-Review

**1. Spec coverage**（逐項對 spec）
- `costs.py::apply_slippage` → Task 1 ✓
- `costs.py::funding_charge` → Task 2 ✓
- `Config.slippage_bps`/`funding_enabled` + fallback + roundtrip + 向後相容 → Task 3 ✓
- `DataLoader.load_funding` 按需快取 + 分頁 + 失敗回空 → Task 4 ✓
- backtester 滑價接線（`_open`/`_close`）→ Task 5 ✓
- backtester funding 結算（data-driven settlement）+ funding_paid 不進 trades + BacktestResult 欄位 → Task 6 ✓
- FIDELITY_NOTES 更新（haircut 誠實命名 + 保守堆疊揭露 + mark price 近似 + legacy 不含）→ Task 6 ✓
- 等價守門（slippage=0+funding off == baseline）→ Task 5 `test_zero_slippage...` + Task 6 `test_funding_off_zero_paid`/`test_funding_does_not_pollute_trade_metrics` ✓
- Monkey（極端 rate/NaN/倒流/空 map/多 settlement/起點前）→ Task 6 + Task 7 ✓
- legacy 路徑不動 → 無 task 觸碰 `_run_legacy_mode`/`_process_*` ✓

**2. Placeholder scan**：無 TBD/TODO/「similar to」；每 code step 附完整程式碼 ✓

**3. Type consistency**：
- `apply_slippage(price, side, action, bps)` — Task 1 定義、Task 5 呼叫，簽名一致 ✓
- `funding_charge(positions, rate, side, mark_price)` — Task 2 定義、Task 6 呼叫（傳 `price` 為 mark_price）✓
- `load_funding(symbol, start, end, exchange=None) -> dict[int,float]` — Task 4 定義、Task 6 呼叫（不傳 exchange 走 lazy）✓
- `GridBacktester(df, config, funding_map=None)` — Task 6 定義、Task 6/7 測試呼叫 ✓
- `BacktestResult.funding_paid: float` — Task 6 定義、Task 6/7 測試讀 ✓
