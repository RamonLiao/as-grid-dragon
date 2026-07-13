# 追價語意驗證（tick 級回測）+ 門檻參數化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 aggTrades tick 級路徑上對比追價門檻 0.5×spacing（現行）vs ≥1.0×spacing，產出改不改 live 語意的裁決依據；同時把門檻參數化為 `requote_threshold_factor`（預設 0.5，bit-identical）。

**Architecture:** 純層 `decision.py` 加參數（單一來源）→ live `bot.py` gate 讀同一 config → 新 `backtest/aggtrades.py`（下載/驗證/壓縮）→ 從 `backtester.py` 抽 `backtest/accounting.py::PositionBook`（禁第二份帳務）→ 新 `backtest/tick_sim.py` 事件模擬器（共用 decide()/costs/liquidation/PositionBook）→ 校準 gate script → 實驗矩陣 script。

**Tech Stack:** Python 3.12 + uv、pandas、requests（zip 下載）、pytest。測試指令一律 `uv run python -m pytest tests/ -q`（系統 python3 無 pytest）。

**Spec:** `docs/superpowers/specs/2026-07-13-requote-semantics-design.md`（權威；每 task 動工前先讀對應章節）

## Global Constraints

- 交易所全程 read-only；不下單、不改 live config、不重啟引擎（生產引擎在本機跑）。
- 寫入白名單：`backtest/`、`grid_engine/`（僅列名檔案）、`tests/`、`scripts/`、`data/`、`docs/`、`tasks/`。**不碰 `config/`、`logs/`、`log/`**。
- `requote_threshold_factor` 預設 0.5 必須 bit-identical：全套既有測試不改斷言全綠 + replay 既有 98,546 筆零 diff（9 筆已知舊 diff 不增不減）。
- **Holdout 段 2026-05-01~06-05 下載後除完整性驗證外不得開封**（不出現在任何開發/調參/對比迭代；Task 11 之前任何 task 不得讀其內容）。
- 每個新守衛/斷言先 mutation red-once（先綠再紅順序不能省——pytest logging plugin 假陰性教訓）。
- fixture 禁止把待測維度設成 0/常數/退化值（lessons 通則 3）。
- git 只准 `git add <明確檔名>`，禁止 `-A`/`--all`/`.`。
- 時間戳一律 UTC；aggTrades 檔案 UTC 日界。

---

### Task 1: 純層 `requote_threshold_factor`（decision.py）

**Files:**
- Modify: `grid_engine/decision.py`（DecisionInputs :24-43、should_adjust :116-126）
- Test: `tests/test_decision_requote_factor.py`（新檔）

**Interfaces:**
- Produces: `DecisionInputs.requote_threshold_factor: float = 0.5`（frozen dataclass 新欄位，帶預設 → 舊呼叫端不炸）；`should_adjust` 門檻改為 `inputs.grid_spacing * inputs.requote_threshold_factor`。後續 Task 3（bot 接線）、Task 7（tick sim）都吃這個欄位。

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_decision_requote_factor.py
"""requote_threshold_factor：追價門檻參數化。

門檻語意：偏離 anchor >= grid_spacing * factor 才撤單重掛。
factor=0.5 是現行 hardcode 行為；1.0 = 掛單掛到價格走完整個 spacing 才動。
"""
import dataclasses
from grid_engine.decision import DecisionInputs, EnhancementSnapshot, should_adjust, decide

def _inputs(price, anchor, factor=None, **kw):
    enh = EnhancementSnapshot(dynamic_take_profit=0.003, dynamic_grid_spacing=0.003,
                              funding_long_bias=1.0, funding_short_bias=1.0)
    base = dict(price=price, long_position=0.1, short_position=0.1,
                buy_long_orders=0.02, sell_long_orders=0.02,
                buy_short_orders=0.02, sell_short_orders=0.02,
                last_grid_price_long=anchor, last_grid_price_short=anchor,
                long_dead_mode=False, short_dead_mode=False,
                grid_spacing=0.003, take_profit_spacing=0.003,
                initial_quantity=0.02, position_threshold=0.8, position_limit=0.1,
                glft_enabled=False, gamma=0.1, enh=enh)
    base.update(kw)
    if factor is not None:
        base["requote_threshold_factor"] = factor
    return DecisionInputs(**base)

def test_default_factor_is_half():
    """不傳 factor → 預設 0.5 = 現行為（bit-identical 保證的基石）"""
    assert DecisionInputs.__dataclass_fields__["requote_threshold_factor"].default == 0.5

def test_factor_half_adjusts_at_015pct():
    # 偏離 0.16% > 0.003*0.5=0.15% → 觸發
    assert should_adjust(_inputs(price=100.16, anchor=100.0), "long") is True

def test_factor_one_holds_at_015pct():
    # 同樣偏離 0.16%，factor=1.0 門檻 0.3% → 不觸發（新語意的核心差異）
    assert should_adjust(_inputs(price=100.16, anchor=100.0, factor=1.0), "long") is False

def test_factor_one_adjusts_at_031pct():
    assert should_adjust(_inputs(price=100.31, anchor=100.0, factor=1.0), "long") is True

def test_missing_orders_adjusts_regardless_of_factor():
    # 單側掛單缺失 → 無條件重掛，factor 不擋（否則成交後永不補掛）
    assert should_adjust(_inputs(price=100.0, anchor=100.0, factor=1.0,
                                 buy_long_orders=0.0), "long") is True

def test_decide_bit_identical_with_default_factor():
    """同 inputs 加不加顯式 factor=0.5，decide() 全欄位相同"""
    a = decide(_inputs(price=100.2, anchor=100.0))
    b = decide(_inputs(price=100.2, anchor=100.0, factor=0.5))
    assert dataclasses.asdict(a) == dataclasses.asdict(b)
```

- [ ] **Step 2: 跑測試確認紅**

Run: `uv run python -m pytest tests/test_decision_requote_factor.py -q`
Expected: FAIL —— `TypeError: __init__() got an unexpected keyword argument 'requote_threshold_factor'`

- [ ] **Step 3: 最小實作**

`grid_engine/decision.py` 兩處：

```python
# DecisionInputs 尾端（enh 之後）加：
    requote_threshold_factor: float = 0.5   # 追價門檻 = grid_spacing * factor；0.5 為歷史 hardcode

# should_adjust 內（:125）：
        deviation = abs(inputs.price - anchor) / anchor
        return deviation >= inputs.grid_spacing * inputs.requote_threshold_factor
```

- [ ] **Step 4: 跑新測試綠 + 全套回歸**

Run: `uv run python -m pytest tests/test_decision_requote_factor.py -q` → 6 passed
Run: `uv run python -m pytest tests/ -q` → 全綠（基準 439 passed + 6 新 = 445；斷言一條都不許改）

- [ ] **Step 5: Mutation red-once**

把 `inputs.requote_threshold_factor` 暫改回 hardcode `0.5` → `test_factor_one_holds_at_015pct` 必須紅；改回。
把 DecisionInputs 預設暫改 `1.0` → `test_default_factor_is_half` 必須紅；改回。

- [ ] **Step 6: Commit**

```bash
git add grid_engine/decision.py tests/test_decision_requote_factor.py
git commit -m "feat(decision): requote_threshold_factor 參數化追價門檻（預設 0.5 bit-identical）"
```

---

### Task 2: GlobalConfig 欄位 + 正規化

**Files:**
- Modify: `grid_engine/config.py`（GlobalConfig dataclass :107 起、to_dict :137 起、from_dict :203 起）
- Test: `tests/test_requote_factor_config.py`（新檔）

**Interfaces:**
- Produces: `GlobalConfig.requote_threshold_factor: float = 0.5`；`from_dict` 正規化：非有限/≤0/>10 → fallback 0.5 並 `console.print` 警告（沿用該檔既有警告風格）。Task 3 的 bot gate 讀這個欄位。

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_requote_factor_config.py
from grid_engine.config import GlobalConfig

def test_default():
    assert GlobalConfig().requote_threshold_factor == 0.5

def test_roundtrip():
    cfg = GlobalConfig(requote_threshold_factor=1.0)
    assert GlobalConfig.from_dict(cfg.to_dict()).requote_threshold_factor == 1.0

def test_missing_key_falls_back():
    d = GlobalConfig().to_dict(); d.pop("requote_threshold_factor", None)
    assert GlobalConfig.from_dict(d).requote_threshold_factor == 0.5

def test_garbage_falls_back():
    for garbage in ("abc", float("nan"), float("inf"), -1.0, 0.0, 11.0, None):
        d = GlobalConfig().to_dict(); d["requote_threshold_factor"] = garbage
        assert GlobalConfig.from_dict(d).requote_threshold_factor == 0.5, garbage
```

- [ ] **Step 2: 跑測試確認紅**（AttributeError / KeyError）
- [ ] **Step 3: 實作**：dataclass 欄位（`position_adjust_cooldown` 旁）、to_dict 加 key、from_dict 加正規化 helper：

```python
def _norm_requote_factor(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        f = float("nan")
    if not math.isfinite(f) or f <= 0 or f > 10:
        console.print(f"[yellow]requote_threshold_factor={v!r} 非法，回退 0.5[/]")
        return 0.5
    return f
```

（`from_dict` 內：`requote_threshold_factor=_norm_requote_factor(data.get("requote_threshold_factor", 0.5))`。注意 config.py 已 `import math`。）

- [ ] **Step 4: 綠 + 全套回歸**（config round-trip 既有測試不得改斷言；`config_io` merge-preserve 不需動——新 key 走正常序列化）
- [ ] **Step 5: Mutation**：暫時把正規化的 `f <= 0` 改 `f < 0` → `test_garbage_falls_back` 的 `0.0` case 必須紅；改回。
- [ ] **Step 6: Commit**

```bash
git add grid_engine/config.py tests/test_requote_factor_config.py
git commit -m "feat(config): requote_threshold_factor 欄位 + from_dict 正規化"
```

---

### Task 3: bot 接線（單一來源）+ replay 向後相容

**Files:**
- Modify: `grid_engine/bot.py`（`_should_adjust_grid` :291-309 的 `deviation_threshold`、`_grid_step` 內 DecisionInputs 建構處——grep `DecisionInputs(` 定位）
- Test: `tests/test_bot_requote_wiring.py`（新檔）、`tests/test_replay_requote_compat.py`（新檔）

**Interfaces:**
- Consumes: Task 1 的 DecisionInputs 欄位、Task 2 的 config 欄位。
- Produces: live gate 與純層同源；decisions.jsonl 的 inputs 自動含新欄位（`dataclasses.asdict` 序列化）；舊記錄（無此 key）replay 走預設 0.5。

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_bot_requote_wiring.py
"""接線測試：bot 的前置 gate 與 DecisionInputs 都吃 config.requote_threshold_factor。
防假旋鈕：config 寫 1.0 而 gate 仍用 0.5 → 這裡必紅。"""
（bot 測試沿用 tests/test_components.py 的既有 bot fixture 模式構造最小 bot——
 實作時先讀該檔，複用其 mock exchange/config 建構 helper，不要自創平行 fixture。）

def test_gate_threshold_follows_config(minimal_bot):
    minimal_bot.config.requote_threshold_factor = 1.0
    sym_cfg = next(iter(minimal_bot.config.symbols.values()))   # grid_spacing=0.003
    state = minimal_bot.state.symbols[sym_cfg.ccxt_symbol]
    state.buy_long_orders = state.sell_long_orders = 0.02
    state.last_grid_price_long = 100.0
    state.latest_price = 100.2          # 偏離 0.2%：>0.15%（舊）但 <0.3%（新）
    assert minimal_bot._should_adjust_grid(sym_cfg, state, "long") is False
    minimal_bot.config.requote_threshold_factor = 0.5
    assert minimal_bot._should_adjust_grid(sym_cfg, state, "long") is True

def test_decision_inputs_carry_factor(minimal_bot):
    """_grid_step 建構的 DecisionInputs.requote_threshold_factor == config 值。
    做法：patch grid_engine.bot.decide 捕獲 inputs，跑一次 _grid_step。"""
```

```python
# tests/test_replay_requote_compat.py
import json
from grid_engine.replay import diff_record, replay_record

OLD_RECORD_JSON = r'''{...}'''  # 從 logs/decisions.jsonl 取一筆真實舊記錄（無 requote 欄位）
                                # 實作時用: head -1 logs/decisions.jsonl（read-only）

def test_old_record_without_factor_replays_clean():
    rec = json.loads(OLD_RECORD_JSON)
    assert "requote_threshold_factor" not in rec["inputs"]
    assert diff_record(rec) is None      # 預設 0.5 補上 → 決策不變

def test_new_record_with_factor_roundtrips():
    rec = json.loads(OLD_RECORD_JSON)
    rec["inputs"]["requote_threshold_factor"] = 0.5
    assert diff_record(rec) is None
```

- [ ] **Step 2: 跑測試確認紅**（gate 測試：config 欄位還沒被讀，1.0 下仍回 True）
- [ ] **Step 3: 實作**

```python
# bot.py _should_adjust_grid（:294）:
        deviation_threshold = sym_config.grid_spacing * getattr(
            self.config, "requote_threshold_factor", 0.5)
# bot.py _grid_step 的 DecisionInputs(...) 建構加：
            requote_threshold_factor=getattr(self.config, "requote_threshold_factor", 0.5),
```

replay 端不用改 code：`_rebuild_inputs` 用 `**fields` 展開，缺 key 走 dataclass 預設。確認即可。

- [ ] **Step 4: 綠 + 全量 replay 回歸**

Run: `uv run python -m pytest tests/ -q` → 全綠
Run（read-only 驗證，spec Global Constraint）:
```bash
uv run python -c "
from grid_engine.replay import replay_file
n, diffs = replay_file('logs/decisions.jsonl')
print(n, len(diffs)); assert len(diffs) == 9, '既有 9 筆舊 diff 不增不減'"
```

- [ ] **Step 5: Mutation**：暫時把 `_should_adjust_grid` 的 getattr 改回 hardcode `0.5` → `test_gate_threshold_follows_config` 必紅；改回。
- [ ] **Step 6: Commit**

```bash
git add grid_engine/bot.py tests/test_bot_requote_wiring.py tests/test_replay_requote_compat.py
git commit -m "feat(bot): requote gate 接線 config.requote_threshold_factor（與純層同源）+ replay 向後相容測試"
```

---

### Task 4: aggTrades 下載器 + 完整性驗證

**Files:**
- Create: `backtest/aggtrades.py`
- Test: `tests/test_aggtrades_loader.py`

**Interfaces:**
- Produces:
  - `AggTradesLoader(data_dir: str | None = None)`（data_dir 預設沿用 DataLoader 慣例 → `data/futures/um/daily/aggTrades/<SYMBOL>/`）
  - `.download(symbol: str, start: str, end: str) -> list[Path]`（"YYYY-MM-DD" UTC 日界含端點；逐日抓 `https://data.binance.vision/data/futures/um/daily/aggTrades/{SYMBOL}/{SYMBOL}-aggTrades-{DATE}.zip`，解壓 csv 入快取）
  - `.load_day(symbol: str, date_str: str) -> pd.DataFrame`（columns: `agg_id, price, qty, first_id, last_id, ts_ms, is_buyer_maker`）
  - `.validate_day(df, date_str) -> None`（不合法 raise ValueError）
- 後續 Task 5（壓縮）、Task 9-11（gate/實驗）吃這些。

- [ ] **Step 1: 寫失敗測試**（fixture 用手工構造的小 DataFrame + tmp_path 快取，不打網路）

```python
# tests/test_aggtrades_loader.py
import pandas as pd, pytest
from backtest.aggtrades import AggTradesLoader

def _mk_day(first_ms, last_ms, n=10, monotonic=True):
    ts = list(range(first_ms, first_ms + n - 1)) + [last_ms]
    if not monotonic:
        ts[2], ts[3] = ts[3], ts[2]
    return pd.DataFrame({"agg_id": range(n), "price": [100.0 + i * 0.01 for i in range(n)],
                         "qty": [1.0] * n, "first_id": range(n), "last_id": range(n),
                         "ts_ms": ts, "is_buyer_maker": [i % 2 == 0 for i in range(n)]})

DAY0 = 1780704000000   # 2026-06-06 00:00:00 UTC（測試錨定 UTC 日界，非本地時區）

def test_validate_full_day_passes():
    df = _mk_day(DAY0 + 60_000, DAY0 + 86_395_000)   # 首筆 <00:05、末筆 >23:55
    AggTradesLoader().validate_day(df, "2026-06-06")

def test_validate_rejects_late_start():
    df = _mk_day(DAY0 + 400_000, DAY0 + 86_395_000)  # 首筆 00:06:40 → 缺頭
    with pytest.raises(ValueError, match="首筆"):
        AggTradesLoader().validate_day(df, "2026-06-06")

def test_validate_rejects_early_end():
    df = _mk_day(DAY0 + 60_000, DAY0 + 80_000_000)   # 末筆 22:13 → 缺尾（部分日毒快取的形態）
    with pytest.raises(ValueError, match="末筆"):
        AggTradesLoader().validate_day(df, "2026-06-06")

def test_validate_rejects_non_monotonic():
    df = _mk_day(DAY0 + 60_000, DAY0 + 86_395_000, monotonic=False)
    with pytest.raises(ValueError, match="單調"):
        AggTradesLoader().validate_day(df, "2026-06-06")

def test_validate_rejects_empty():
    with pytest.raises(ValueError, match="空"):
        AggTradesLoader().validate_day(pd.DataFrame(), "2026-06-06")

def test_download_skips_today_utc(tmp_path, monkeypatch):
    """未過完的當日不入快取（07-10 kline 部分日教訓）"""
    loader = AggTradesLoader(data_dir=str(tmp_path))
    called = []
    monkeypatch.setattr(loader, "_fetch_zip", lambda s, d: called.append(d) or b"")
    import datetime
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    loader.download("BNBUSDC", today, today)
    assert called == []
```

- [ ] **Step 2: 確認紅**（ModuleNotFoundError）
- [ ] **Step 3: 實作 `backtest/aggtrades.py`**

```python
"""Binance Vision UM futures aggTrades 日檔下載/驗證/快取。

設計約束（spec §4.1）：
- UTC 日界（datetime 一律帶 tzinfo=UTC，禁止 naive→本地時區——上次 kline 偏移 8h 教訓）。
- 逐日完整性驗證後才落快取；驗證失敗的日檔不落地（skip-if-exists 毒快取教訓）。
- 未過完的當日直接跳過。
"""
import datetime as dt
import io, zipfile
from pathlib import Path
import pandas as pd
import requests

_UTC = dt.timezone.utc
_COLS = ["agg_id", "price", "qty", "first_id", "last_id", "ts_ms", "is_buyer_maker"]
_URL = "https://data.binance.vision/data/futures/um/daily/aggTrades/{sym}/{sym}-aggTrades-{d}.zip"
_HEAD_MS, _TAIL_MS = 5 * 60_000, 5 * 60_000       # 首筆 <00:05、末筆 >23:55

class AggTradesLoader:
    def __init__(self, data_dir: str | None = None):
        if data_dir is None:
            from backtest.data_loader import DataLoader
            data_dir = str(Path(DataLoader()._get_default_data_dir()) / "futures/um/daily/aggTrades")
        self.data_dir = Path(data_dir)

    def _day_path(self, symbol: str, date_str: str) -> Path:
        return self.data_dir / symbol / f"{symbol}-aggTrades-{date_str}.csv"

    def _fetch_zip(self, symbol: str, date_str: str) -> bytes:
        r = requests.get(_URL.format(sym=symbol, d=date_str), timeout=60)
        r.raise_for_status()
        return r.content

    def validate_day(self, df: pd.DataFrame, date_str: str) -> None:
        if df is None or len(df) == 0:
            raise ValueError(f"{date_str}: 空檔")
        day0 = int(dt.datetime.strptime(date_str, "%Y-%m-%d")
                   .replace(tzinfo=_UTC).timestamp() * 1000)
        day1 = day0 + 86_400_000
        ts = df["ts_ms"]
        if not ((ts >= day0) & (ts < day1)).all():
            raise ValueError(f"{date_str}: 時間戳越日界")
        if ts.iloc[0] > day0 + _HEAD_MS:
            raise ValueError(f"{date_str}: 首筆 {ts.iloc[0]} 距日始 >5min（疑缺頭）")
        if ts.iloc[-1] < day1 - _TAIL_MS:
            raise ValueError(f"{date_str}: 末筆 {ts.iloc[-1]} 距日終 >5min（疑部分日）")
        if not ts.is_monotonic_increasing:
            raise ValueError(f"{date_str}: 時間戳非單調")

    def load_day(self, symbol: str, date_str: str) -> pd.DataFrame:
        df = pd.read_csv(self._day_path(symbol, date_str), header=None, names=_COLS)
        # Binance 部分月份日檔首行帶 header：容錯丟棄非數值首行
        if not str(df.iloc[0]["ts_ms"]).isdigit():
            df = df.iloc[1:].reset_index(drop=True)
        for c in ("price", "qty"):
            df[c] = df[c].astype(float)
        df["ts_ms"] = df["ts_ms"].astype("int64")
        df["is_buyer_maker"] = df["is_buyer_maker"].astype(bool)
        return df

    def download(self, symbol: str, start: str, end: str) -> list[Path]:
        today = dt.datetime.now(_UTC).strftime("%Y-%m-%d")
        out, d = [], dt.datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=_UTC)
        end_d = dt.datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=_UTC)
        while d <= end_d:
            ds = d.strftime("%Y-%m-%d")
            d += dt.timedelta(days=1)
            if ds >= today:          # 未過完的當日不抓
                continue
            path = self._day_path(symbol, ds)
            if path.exists():
                out.append(path); continue
            raw = self._fetch_zip(symbol, ds)
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                csv_bytes = zf.read(zf.namelist()[0])
            df = pd.read_csv(io.BytesIO(csv_bytes), header=None, names=_COLS)
            if not str(df.iloc[0]["ts_ms"]).isdigit():
                df = df.iloc[1:].reset_index(drop=True)
            df["ts_ms"] = df["ts_ms"].astype("int64")
            self.validate_day(df, ds)          # 驗證過才落地
            path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(path, index=False, header=False)
            out.append(path)
        return out
```

- [ ] **Step 4: 綠 + 全套回歸**
- [ ] **Step 5: Mutation**：暫時把 `validate_day` 的末筆檢查刪掉 → `test_validate_rejects_early_end` 必紅；改回。
- [ ] **Step 6: 實抓冒煙（非測試，一次性）**：`uv run python -c "from backtest.aggtrades import AggTradesLoader; print(AggTradesLoader().download('BNBUSDC','2026-06-06','2026-06-07'))"` → 2 個檔案落地、validate 通過。**不抓 05-01~06-05（holdout 留給 Task 11）。**
- [ ] **Step 7: Commit**

```bash
git add backtest/aggtrades.py tests/test_aggtrades_loader.py
git commit -m "feat(backtest): aggTrades 日檔下載器（UTC 日界+完整性驗證+拒部分日）"
```

---

### Task 5: 事件流壓縮 + spread 重建

**Files:**
- Modify: `backtest/aggtrades.py`（追加兩個函數）
- Test: `tests/test_aggtrades_events.py`

**Interfaces:**
- Produces:
  - `compress_events(df: pd.DataFrame) -> pd.DataFrame`——連續同價 tick 合併（qty 加總、ts 取首筆），columns: `ts_ms, price, qty`。決策只依賴價格穿越門檻，同價段不觸發任何狀態改變，去重零保真損失。
  - `estimate_spread(df: pd.DataFrame) -> dict`——用 `is_buyer_maker` 側別重建 spread 估計（spec F6 索取項）：`is_buyer_maker=True` 的 trade 打在 bid、False 打在 ask；對相鄰異側 trade 對取 `ask_px - bid_px`（僅限時間差 <1s 的相鄰對，避免跨行情比較），回傳 `{"median_bps", "p90_bps", "n_pairs"}`。
- Task 7 sim 吃 compress 輸出；Task 10 報告吃 spread 估計。

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_aggtrades_events.py
import pandas as pd
from backtest.aggtrades import compress_events, estimate_spread

def test_compress_merges_same_price_runs():
    df = pd.DataFrame({"ts_ms": [1, 2, 3, 4, 5], "price": [100.0, 100.0, 100.1, 100.1, 100.0],
                       "qty": [1, 2, 3, 4, 5], "is_buyer_maker": [True] * 5})
    ev = compress_events(df)
    assert list(ev["price"]) == [100.0, 100.1, 100.0]
    assert list(ev["qty"]) == [3, 7, 5]          # 同價段 qty 加總
    assert list(ev["ts_ms"]) == [1, 3, 5]        # 段首 ts

def test_compress_preserves_single_events():
    df = pd.DataFrame({"ts_ms": [1], "price": [100.0], "qty": [1.0], "is_buyer_maker": [True]})
    assert len(compress_events(df)) == 1

def test_estimate_spread_from_side_flips():
    # bid=100.00 / ask=100.05 的交替成交 → spread 5bps
    df = pd.DataFrame({"ts_ms": [1, 100, 200, 300], "price": [100.00, 100.05, 100.00, 100.05],
                       "qty": [1] * 4, "is_buyer_maker": [True, False, True, False]})
    est = estimate_spread(df)
    assert est["n_pairs"] == 3
    assert abs(est["median_bps"] - 5.0) < 0.1

def test_estimate_spread_skips_stale_pairs():
    df = pd.DataFrame({"ts_ms": [1, 5_000], "price": [100.00, 100.05],
                       "qty": [1, 1], "is_buyer_maker": [True, False]})
    assert estimate_spread(df)["n_pairs"] == 0   # 相鄰對時差 >1s 不計
```

- [ ] **Step 2: 確認紅** → **Step 3: 實作**

```python
def compress_events(df: pd.DataFrame) -> pd.DataFrame:
    run_id = (df["price"] != df["price"].shift()).cumsum()
    g = df.groupby(run_id, sort=False)
    return pd.DataFrame({"ts_ms": g["ts_ms"].first().values,
                         "price": g["price"].first().values,
                         "qty": g["qty"].sum().values})

def estimate_spread(df: pd.DataFrame, max_gap_ms: int = 1000) -> dict:
    px, side, ts = df["price"].values, df["is_buyer_maker"].values, df["ts_ms"].values
    spreads = []
    for i in range(1, len(px)):
        if side[i] != side[i - 1] and ts[i] - ts[i - 1] < max_gap_ms:
            ask = px[i] if not side[i] else px[i - 1]
            bid = px[i] if side[i] else px[i - 1]
            if ask >= bid > 0:
                spreads.append((ask - bid) / bid * 10_000)
    if not spreads:
        return {"median_bps": float("nan"), "p90_bps": float("nan"), "n_pairs": 0}
    s = pd.Series(spreads)
    return {"median_bps": float(s.median()), "p90_bps": float(s.quantile(0.9)),
            "n_pairs": len(spreads)}
```

- [ ] **Step 4: 綠 + 全套回歸** → **Step 5: Mutation**（暫時把 `max_gap_ms` 檢查刪掉 → stale 測試必紅；改回）
- [ ] **Step 6: Commit**

```bash
git add backtest/aggtrades.py tests/test_aggtrades_events.py
git commit -m "feat(backtest): 事件流壓縮 + isBuyerMaker spread 重建（spec F6）"
```

---

### Task 6: 抽帳務 `PositionBook`（backtester 重構，行為零變）

**Files:**
- Create: `backtest/accounting.py`
- Modify: `backtest/backtester.py`（run() 內 `_open`/`_close`/`_equity_at` 閉包與 `long_positions`/`short_positions`/`balance` 追蹤改為委派 PositionBook；`_settle`/決策迴圈不動）
- Test: `tests/test_accounting.py`

**Interfaces:**
- Produces `backtest.accounting.PositionBook`：

```python
class PositionBook:
    def __init__(self, balance: float, leverage: float, fee_pct: float, slippage_bps: float): ...
    def seed(self, side: str, qty: float, price: float) -> None       # margin 扣 balance 不扣 fee（#14 語意）
    def open(self, side: str, price: float, qty: float) -> bool       # False = 保證金不足拒單（-2019 等價）
    def close(self, side: str, price: float, qty: float, ts) -> float # per-lot FIFO，回傳 realized
    def qty(self, side: str) -> float
    def equity_at(self, price: float) -> float                        # balance + open_margin + unrealized
    def netted_avg(self, side: str) -> float                          # netted 均價（Σqty*px/Σqty；空倉 0）
    def netted_equity_at(self, price: float) -> float                 # 用 netted 表示法重算（理論上 == equity_at）
    balance: float
    trades: list           # 沿用 backtester Trade 記錄所需欄位
    rejected_entries: int  # open() 回 False 的累計（Task 7/10 拒單率）
```

- 語意逐行搬自 `backtester.py:656-733` 現有閉包（open 的 slippage/fee/margin、close 的 FIFO/fee/realized），**不是重寫**——搬完 backtester 委派之，全套既有測試斷言不改全綠 = 行為零變的證據。
- Task 7 tick sim 直接 import 用。

- [ ] **Step 1: 寫失敗測試**（釘住將搬移的語意 + netted 等式）

```python
# tests/test_accounting.py
import pytest
from backtest.accounting import PositionBook

def _book(balance=1000.0, lev=5.0, fee=0.0002, slip=0.0):
    return PositionBook(balance=balance, leverage=lev, fee_pct=fee, slippage_bps=slip)

def test_open_deducts_margin_and_fee():
    b = _book()
    assert b.open("long", 100.0, 1.0) is True
    # margin = 100/5 = 20, fee = 100*0.0002 = 0.02
    assert b.balance == pytest.approx(1000 - 20 - 0.02)

def test_open_rejects_when_margin_insufficient():
    b = _book(balance=10.0)
    assert b.open("long", 100.0, 1.0) is False      # 需 20 > 10
    assert b.rejected_entries == 1
    assert b.balance == 10.0                        # 拒單不動帳

def test_close_fifo_and_realized():
    b = _book(fee=0.0)
    b.open("long", 100.0, 1.0); b.open("long", 110.0, 1.0)
    realized = b.close("long", 120.0, 1.0, ts=None)
    assert realized == pytest.approx(20.0)          # FIFO 先平 100 那口
    assert b.qty("long") == pytest.approx(1.0)

def test_equity_identity():
    b = _book(fee=0.0)
    b.open("long", 100.0, 2.0)
    # equity = balance + margin + uPnL = (1000-40) + 40 + (110-100)*2
    assert b.equity_at(110.0) == pytest.approx(1020.0)

def test_netted_equals_perlot_equity_after_partial_close():
    """FIDELITY_NOTES 12 / review F7：equity 對 lot 結構不變（數學恆等式的回歸釘）。
    若未來改動讓兩者分歧，這裡炸。"""
    b = _book(fee=0.0)
    b.open("long", 100.0, 1.0); b.open("long", 120.0, 1.0)
    b.close("long", 130.0, 0.5, ts=None)            # 部分平倉後 lot 結構 != netted
    for p in (90.0, 110.0, 140.0):
        assert b.equity_at(p) == pytest.approx(b.netted_equity_at(p))

def test_seed_no_fee():
    b = _book()
    b.seed("long", 1.0, 100.0)
    assert b.balance == pytest.approx(1000 - 20)    # 只扣 margin 不扣 fee
```

- [ ] **Step 2: 確認紅** → **Step 3: 實作 accounting.py**：從 backtester run() 閉包**逐行搬**（含 `apply_slippage` 呼叫點與 fee 計算式），加 netted 追蹤（open/seed 時更新 `Σqty`、`Σqty*px`；close 時等比例縮 netted 名目——Binance netted 語意）。
- [ ] **Step 4: backtester 委派**：run() 內 `long_positions`/`short_positions` 等改為 `book = PositionBook(...)`，閉包變薄轉發。**斷言零改動**跑全套：`uv run python -m pytest tests/ -q` 全綠（尤其 `test_backtest_seed_position.py`、`test_backtest_equity.py`、`test_backtest_liquidation.py`、`test_backtest_costs.py`）。
- [ ] **Step 5: 結構性證據**：`grep -n "margin_required\|fee_cost" backtest/backtester.py` 主路徑 run() 內不再有內聯帳務算式（legacy `_execute_*` :290-511 是死路徑豁免，維持原樣不動）。
- [ ] **Step 6: Mutation**：accounting.py 的 close 暫時改 LIFO（pop 尾）→ `test_close_fifo_and_realized` 必紅；改回。
- [ ] **Step 7: Commit**

```bash
git add backtest/accounting.py backtest/backtester.py tests/test_accounting.py
git commit -m "refactor(backtest): 抽 PositionBook 帳務層（行為零變，netted 等式回歸釘）"
```

---

### Task 7: tick 事件模擬器核心

**Files:**
- Create: `backtest/tick_sim.py`
- Test: `tests/test_tick_sim.py`

**Interfaces:**
- Consumes: Task 1 factor 欄位、Task 5 事件流（`ts_ms, price, qty`）、Task 6 PositionBook、`grid_engine.decision.decide`、`backtest.liquidation.should_liquidate`、`backtest.costs.funding_charge`。
- Produces:

```python
@dataclass
class TickSimConfig:
    grid_spacing: float = 0.003
    take_profit_spacing: float = 0.003
    initial_quantity: float = 0.02
    leverage: float = 5.0
    initial_balance: float = 184.6
    fee_pct: float = 0.0002
    slippage_bps: float = 0.0001
    threshold_multiplier: float = 40.0
    limit_multiplier: float = 5.0
    requote_threshold_factor: float = 0.5
    cooldown_sec: float = 5.0
    decision_delay_ms: int = 500
    maintenance_margin_rate: float = 0.005    # 對齊 backtest/config.py 現值
    seed_long_qty: float = 0.0
    seed_long_price: float = 0.0
    seed_short_qty: float = 0.0
    seed_short_price: float = 0.0
    funding_events: list = field(default_factory=list)   # [(epoch_sec, rate)]

@dataclass
class TickSimResult:
    final_equity: float
    max_drawdown: float
    liquidated: bool
    fills: list              # [{ts_ms, side, kind: 'entry'|'tp', price, qty}]
    round_trips: int         # 完成的 entry→TP 往返數（獨立事件數，spec §5）
    rejected_entries: int
    requote_count: int
    realized_pnl: float

def run_tick_sim(events: pd.DataFrame, cfg: TickSimConfig) -> TickSimResult
```

**模擬語意（spec §4.2 落地，實作照此不自由發揮）：**
1. 掛單物件：`{price, qty, kind, side, effective_ms, expire_ms}`。requote 時：舊單 `expire_ms = ev + delay`（cancel 延遲落地前仍可成交），新單 `effective_ms = ev + delay`。
2. 每事件順序：(a) 成交判定 → (b) 強平檢查 → (c) funding → (d) 決策 gate。
3. 成交（V1/V2 防禦）：對每張 `effective_ms <= ts < expire_ms` 的掛單，**嚴格穿越**才成交：buy 掛單要 `ev.price < limit`；sell 掛單要 `ev.price > limit`。成交價 = 掛單價，all-or-nothing，經 PositionBook（slippage haircut 在 book 內沿用）。entry 先於 tp 檢查（與 1m `_settle` 同序）。TP 可平量 clamp 事件前持倉。
4. 強平：`should_liquidate(book.equity_at(ev.price), ...)` 觸發即終止，`liquidated=True`。
5. 決策 gate（鏡射 live `_handle_ticker`→`adjust_grid`→`_grid_step`）：per-side 檢查（i）該側掛單缺失（無 active/pending entry 或 tp）→ 觸發；（ii）`|price-anchor|/anchor >= grid_spacing*factor` → 觸發；再過 per-side cooldown（距上次該側 requote ≥ cooldown_sec）。觸發 → 組 `DecisionInputs`（enh 中性快照：dynamic=base、bias=1.0；orders 欄位 = 含 pending 的張數）→ `decide()` → 依 SideDecision 施行（cancel_side → 舊單標 expire；orders → 新單標 effective；anchor 更新；dead mode 旗標維護）。`requote_count += 1`。
6. round_trips：TP 成交一次 +1。
7. equity 曲線：逐事件 `equity_at(price)` 追蹤 max_drawdown（tick 級最不利價，天然含 wick）。

- [ ] **Step 1: 寫失敗測試**（手工事件 fixtures；每條測一個機制，fixture 的待測維度不退化）

```python
# tests/test_tick_sim.py
import pandas as pd, pytest
from backtest.tick_sim import TickSimConfig, run_tick_sim

def _ev(*rows):   # rows: (ts_ms, price)
    return pd.DataFrame({"ts_ms": [r[0] for r in rows], "price": [r[1] for r in rows],
                         "qty": [1.0] * len(rows)})

BASE = dict(grid_spacing=0.003, take_profit_spacing=0.003, initial_quantity=0.02,
            leverage=5.0, initial_balance=1000.0, fee_pct=0.0, slippage_bps=0.0,
            cooldown_sec=0.0, decision_delay_ms=0)

def test_strict_crossing_fills_entry():
    # anchor 100 → buy entry @ 99.7；價格打到 99.69（嚴格低於）→ 成交
    cfg = TickSimConfig(**BASE, requote_threshold_factor=1.0)
    r = run_tick_sim(_ev((0, 100.0), (1000, 99.75), (2000, 99.69)), cfg)
    assert any(f["kind"] == "entry" and f["side"] == "long" for f in r.fills)

def test_touch_does_not_fill():
    # 恰好 99.7（== limit）→ 不成交（V2 保守界）
    cfg = TickSimConfig(**BASE, requote_threshold_factor=1.0)
    r = run_tick_sim(_ev((0, 100.0), (1000, 99.70)), cfg)
    assert r.fills == []

def test_chasing_requotes_before_fill_factor_half():
    """舊語意病理重現：0.15% 一到就重掛 → 緩跌路徑永不成交"""
    cfg = TickSimConfig(**BASE, requote_threshold_factor=0.5)
    # 每步跌 0.16%（觸發 requote）連續 10 步 → 掛單一路被搬走
    rows, p = [], 100.0
    for i in range(10):
        p *= (1 - 0.0016); rows.append((i * 1000, round(p, 6)))
    r = run_tick_sim(_ev((0, 100.0), *rows), cfg)
    assert [f for f in r.fills if f["kind"] == "entry"] == []
    assert r.requote_count >= 10

def test_resting_order_fills_same_path_factor_one():
    """同一路徑，factor=1.0 → 掛單活到被穿越（新語意的核心主張）"""
    cfg = TickSimConfig(**BASE, requote_threshold_factor=1.0)
    rows, p = [], 100.0
    for i in range(10):
        p *= (1 - 0.0016); rows.append((i * 1000, round(p, 6)))
    r = run_tick_sim(_ev((0, 100.0), *rows), cfg)
    assert any(f["kind"] == "entry" for f in r.fills)

def test_cooldown_caps_requote_rate():
    cfg = TickSimConfig(**{**BASE, "cooldown_sec": 5.0}, requote_threshold_factor=0.5)
    # 1 秒內三次 0.2% 跳動：cooldown 5s → 只允許第一次 requote
    r = run_tick_sim(_ev((0, 100.0), (200, 100.2), (400, 100.4), (600, 100.6)), cfg)
    assert r.requote_count <= 2      # 初始佈網 1 次 + 至多 1 次

def test_decision_delay_keeps_old_order_alive():
    """延遲窗口內舊單仍可成交（cancel 未落地）——lookahead 防禦的行為面"""
    cfg = TickSimConfig(**{**BASE, "decision_delay_ms": 500}, requote_threshold_factor=0.5)
    # t=1000 觸發 requote（0.16% 跌）；t=1200（延遲窗內）價格穿越舊 buy 單 99.7
    r = run_tick_sim(_ev((0, 100.0), (1000, 99.84), (1200, 99.69)), cfg)
    assert any(f["kind"] == "entry" and f["price"] == pytest.approx(99.7) for f in r.fills)

def test_margin_rejection_counted():
    cfg = TickSimConfig(**{**BASE, "initial_balance": 0.5}, requote_threshold_factor=1.0)
    r = run_tick_sim(_ev((0, 100.0), (1000, 99.69)), cfg)
    assert r.rejected_entries >= 1 and r.fills == []

def test_liquidation_terminates():
    cfg = TickSimConfig(**BASE, requote_threshold_factor=1.0,
                        seed_long_qty=1.0, seed_long_price=100.0,
                        initial_balance=21.0)     # margin 20，權益薄
    # 價格崩 30% → 權益穿透維持保證金
    r = run_tick_sim(_ev((0, 100.0), (1000, 70.0)), cfg)
    assert r.liquidated is True

def test_round_trip_counting():
    cfg = TickSimConfig(**BASE, requote_threshold_factor=1.0)
    # 完整往返：進場 99.69 成交 → TP sell @ entry*1.003 → 價格上穿 → TP 成交
    r = run_tick_sim(_ev((0, 100.0), (1000, 99.69), (2000, 100.05)), cfg)
    assert r.round_trips == 1
```

- [ ] **Step 2: 確認紅** → **Step 3: 實作 `backtest/tick_sim.py`**（依上方語意 1-7；事件迴圈純 Python，PositionBook/decide/should_liquidate/funding_charge 全 import 不重寫；`DecisionInputs` 組裝參照 `backtester.py:777-797` 欄位對映，orders 欄位以「張數 → 數量近似」沿用 1m 的 1.0/0.0 慣例改為 pending qty 加總）
- [ ] **Step 4: 綠 + 全套回歸**
- [ ] **Step 5: Mutation 三發**：(a) 嚴格穿越暫改 `<=` → `test_touch_does_not_fill` 紅；(b) cooldown 檢查暫時刪除 → `test_cooldown_caps_requote_rate` 紅；(c) 延遲暫改為「立即撤舊單」→ `test_decision_delay_keeps_old_order_alive` 紅。各自改回。
- [ ] **Step 6: Commit**

```bash
git add backtest/tick_sim.py tests/test_tick_sim.py
git commit -m "feat(backtest): tick 事件模擬器（追價/cooldown/延遲/嚴格穿越/拒單/強平）"
```

---

### Task 8: seed 場景 + 退化路徑等價守門

**Files:**
- Modify: `backtest/tick_sim.py`（seed 已在 config；此 task 補等價測試所需的鉤子，若 Task 7 已完整則只補測試）
- Test: `tests/test_tick_sim_equivalence.py`

**Interfaces:**
- Consumes: Task 7 全部；`backtest/backtester.py::GridBacktester` + `backtest/config.py::Config`。

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_tick_sim_equivalence.py
"""退化路徑等價守門（spec §9）：tick sim 在「1 tick = 1 bar close、零延遲、
零 cooldown、factor=0.5」下，與 GridBacktester 餵 h=l=c=price 的退化 bar 應
產生相同 fills 序列與 final_equity（容差 1e-9）。
差異白名單：無（有 diff 即 FAIL——兩邊帳務同一份 PositionBook，決策同一份
decide()，唯一自由度是撮合序，而退化 bar 下 touch/crossing 邊界不會出現：
fixture 價格全部嚴格穿越，不踩 == limit 的邊界）。"""
import pandas as pd, pytest

def _price_path():
    # 構造會產生 entry+TP 至少各一次的路徑（嚴格穿越，不踩 == 邊界）
    return [100.0, 99.65, 99.90, 100.31, 99.95, 99.60, 100.40]

def test_degenerate_equivalence():
    from backtest.tick_sim import TickSimConfig, run_tick_sim
    from backtest.backtester import GridBacktester
    from backtest.config import Config
    path = _price_path()
    events = pd.DataFrame({"ts_ms": [i * 60_000 for i in range(len(path))],
                           "price": path, "qty": [1.0] * len(path)})
    tick_cfg = TickSimConfig(grid_spacing=0.003, take_profit_spacing=0.003,
                             initial_quantity=0.02, leverage=5.0, initial_balance=1000.0,
                             fee_pct=0.0002, slippage_bps=0.0, threshold_multiplier=40.0,
                             requote_threshold_factor=0.5, cooldown_sec=0.0,
                             decision_delay_ms=0)
    r_tick = run_tick_sim(events, tick_cfg)
    bt_cfg = Config(initial_balance=1000.0, initial_quantity=0.02, leverage=5,
                    grid_spacing=0.003, take_profit_spacing=0.003, fee_pct=0.0002,
                    slippage_bps=0.0, funding_enabled=False, threshold_multiplier=40.0,
                    direction="both")
    df = pd.DataFrame({"open": path, "high": path, "low": path, "close": path,
                       "open_time": pd.to_datetime([i * 60_000 for i in range(len(path))],
                                                   unit="ms", utc=True)})
    r_bar = GridBacktester(bt_cfg).run(df)   # 實作時對齊實際 run() 簽名（讀 backtester.py:504 起）
    assert r_tick.final_equity == pytest.approx(r_bar.final_equity, abs=1e-9)
    assert len(r_tick.fills) == r_bar.trades_count
```

**已知語意差異必須先消掉再比（實作時逐一處理，不許用容差掩蓋）**：1m `_settle` 用 touch（`low<=limit`）而 tick sim 用嚴格穿越 → fixture 路徑全部嚴格穿越可繞開；1m 每 bar 至多一張 entry 成交 → fixture 每步至多觸發一張。若對齊後仍 diff → 停下來查，不放寬容差。

- [ ] **Step 2: 確認紅（或直接綠——若綠，先 mutation 證明測試有鑑別力：暫時把 tick sim fee 改 0 → 必紅）**
- [ ] **Step 3: 修到綠**（處理發現的語意 gap，改 tick_sim 不改 backtester）
- [ ] **Step 4: 全套回歸** → **Step 5: Commit**

```bash
git add backtest/tick_sim.py tests/test_tick_sim_equivalence.py
git commit -m "test(backtest): tick sim 與 GridBacktester 退化路徑等價守門"
```

---

### Task 9: 校準 gate script（三 gate，FAIL 即停）

**Files:**
- Create: `scripts/calibration_gate.py`
- Test: `tests/test_calibration_gate.py`（判定函數的單元測試；script 本體薄殼）

**Interfaces:**
- Consumes: Task 4/5/7。
- Produces: `uv run python scripts/calibration_gate.py --end <YYYY-MM-DD>` → stdout 報告 + exit code（0=全 PASS）。純函數 `judge_low_gate(sim_fills_per_day: float) -> bool`（≤2.0）、`judge_high_gate(tick_fills: int, bar_fills: int) -> bool`（`0.2*bar <= tick <= 1.0*bar`；bar==0 → False 並要求換窗口）、`judge_june_alignment(sim_daily: dict[str,int], live_daily: dict[str,int]) -> bool`（live>0 的日子 sim 也 >0 的比例 ≥ 0.5，且 sim 月總量 ≤ 10× live 月總量）。

**Gate 資料definition（spec §4.3）**：
- 低端：factor=0.5、seed 現倉（多 0.58@690.29/空 0.34@571.75）、balance 184.6、lev 5、cooldown 5s、delay 500ms，事件流 = 07-12（14:51 Taipei 起）~ `--end`。live ground truth = 0 筆（寫死在 script 註解，附本 session `fetch_my_trades` 證據日期）。
- 高端：factor=1.0 同參數，窗口 06-16~06-24（選型段中段、非 W 邊界日），對照 GridBacktester 同窗口 1m bars 同參數的 fills 數。
- 6 月對齊：factor=0.5 掃 06-06~06-30 逐日 fills，對照 live income 實測（COMMISSION 按日聚合，數字寫死於 script：06-17:3、06-19:1、06-22:1、06-23:1、06-25:3、06-28:1，其餘 0——出處 2026-07-13 健檢）。

- [ ] **Step 1: 寫失敗測試**（三個 judge 函數的邊界：2.0 過/2.1 不過；0.2×/1.0× 邊界；bar=0 強制 False；對齊比例 0.5 邊界）
- [ ] **Step 2: 確認紅** → **Step 3: 實作**（judge 純函數 + main：下載缺日 → 壓縮 → 跑三 gate → 報告逐 gate PASS/FAIL + 數字）
- [ ] **Step 4: 綠 + Mutation**（judge_high_gate 的 `bar==0 → False` 暫時改 True → 對應測試紅；改回）
- [ ] **Step 5: 實跑三 gate**：`uv run python scripts/calibration_gate.py --end 2026-07-13`。**任一 FAIL → 本 plan 暫停，回報使用者，不進 Task 10。**
- [ ] **Step 6: Commit**

```bash
git add scripts/calibration_gate.py tests/test_calibration_gate.py
git commit -m "feat(scripts): 校準 gate（低端 live≈0 / 高端 vs 1m [0.2,1.0]x / 6 月對齊）"
```

---

### Task 10: 實驗矩陣 runner + 報告

**Files:**
- Create: `scripts/requote_experiment.py`
- Test: `tests/test_requote_experiment.py`（cell 建構/事件數守門/報告聚合的純函數測試）

**Interfaces:**
- Consumes: Task 4/5/7 + `backtest/aggtrades.py` spread 估計。
- Produces: `uv run python scripts/requote_experiment.py --end <date> --out tasks/requote-experiment-results.md`。

**矩陣（spec §5 落地）**：
- factor {0.5, 1.0, 1.5} × 資本 {A: 184.6+seed 現倉, B: 209.6+seed 0.58/0.58（空頭 seed 價用 571.75）} × 窗口 {W1/W2/W3 不相交切段 + 全程 06-06~end}。
  - **W 切點定義（實作時執行）**：讀 1m klines 06-06~07-10 收盤序列，W1=06-06~最高點日、W2=最高點日+1~最低點日、W3=最低點日+1~07-10；若切出來 <5 天的段與相鄰段合併並如實標注 regime。切點寫進報告。
- cost sens：fee {2,4}bps × slip {0,1,2}bps（全 cell）。
- 決策延遲 {0, 500ms, 1s}：僅基準 fee/slip、全程窗口。
- 優勝者加掃：factor ±20%（3 點）+ cooldown {2.5, 5, 10}s，基準 fee/slip。
- spread 抖動敏感度：基準 cell 觸發價 ±half-spread（取 `estimate_spread` 的 median）重跑，requote/成交數變化 >20% → 報告標註。
- 每 cell 輸出：final_equity、Δeq vs factor=0.5 同窗同本、max_dd、liquidated、fills、round_trips（<30 標「樣本不足」）、rejected_entries 率、requote_count。
- 報告尾：總組合數 N、事件數守門後的有效 cell 清單、§6 判準逐條 PASS/FAIL/inconclusive 預判（holdout 除外）。

- [ ] **Step 1: 寫失敗測試**（`build_matrix()` 組合數正確且不含 holdout 日期；`gate_cells(cells, min_events=30)` 過濾；`verdict_preview(results)` 對 §6.1-6.6 逐條判定的純函數——各給手工 results fixture 測 PASS/FAIL/inconclusive 三態）
- [ ] **Step 2: 確認紅** → **Step 3: 實作** → **Step 4: 綠 + Mutation**（verdict_preview 的「達標段 <2 → inconclusive」暫時改 <1 → 對應測試紅；改回）
- [ ] **Step 5: 實跑**：`uv run python scripts/requote_experiment.py --end 2026-07-13 --out tasks/requote-experiment-results.md`（跑完把結果檔關鍵表貼進 progress.md 摘要）
- [ ] **Step 6: Commit**

```bash
git add scripts/requote_experiment.py tests/test_requote_experiment.py tasks/requote-experiment-results.md
git commit -m "feat(scripts): 追價語意實驗矩陣 + §6 判準預判報告"
```

---

### Task 11: Holdout OOS（最終驗證，一次性）

**Files:**
- Modify: `scripts/requote_experiment.py`（加 `--holdout` 模式）
- Create: 無新檔

**前置（缺一不跑）**：Task 10 結果經使用者過目、§6.1-6.6 全 PASS、優勝 factor 已定。**Holdout 只能跑一次**；跑之前先在 `tasks/progress.md` 記「holdout 開封於 <日期>，優勝者 <factor>」。

- [ ] **Step 1**: `--holdout` 模式實作：下載 05-01~06-05 aggTrades（首次開封）→ 只跑 {優勝 factor, 0.5} × {A, B} × 全 holdout 段 × 基準 fee/slip → 判「新 ≥ 舊、零強平」→ PASS 維持建議 / FAIL 記 inconclusive。**模式內建鎖**：若 `tasks/requote-experiment-results.md` 無「§6.1-6.6 全 PASS」標記則 refuse 執行。
- [ ] **Step 2**: 實跑一次，結果 append 進 results 檔與 progress.md。
- [ ] **Step 3: Commit**

```bash
git add scripts/requote_experiment.py tasks/requote-experiment-results.md
git commit -m "feat(scripts): holdout OOS 一次性驗證（05-01~06-05）"
```

---

### Task 12: 收尾——文件 + dual-review + verifier

- [ ] **Step 1**: `backtester.py` FIDELITY_NOTES 追加第 (13) 條：1m 撮合 vs live 追價的成交率高估（實測 17/day vs 1/day）已由 tick sim 補洞；`tasks/progress.md` 更新 TODO 1a 狀態與結果摘要。
- [ ] **Step 2**: 跑 `security-review` skill 適用性判斷：本 branch 不碰下單路徑與 auth（read-only 研究 + config 參數），不命中 Red Team Protocol 適用範圍 → 記錄跳過理由。
- [ ] **Step 3**: 依 `dual-review` skill 跑完整兩輪 review（內部 + 外部 fresh-context，外部輪不給 spec/自述），整合修復到 verdict = Ship as-is。
- [ ] **Step 4**: 派 `verifier` fresh-context 驗收（read-back + 實跑全套測試 + 獨立 mutation 抽查）。
- [ ] **Step 5**: 最終回報使用者：實驗結論 + 是否建議翻 `requote_threshold_factor=1.0` + 若翻的上線 checklist（改 config JSON、重啟、首週觀察指標：日 fills、拒單、Δequity）。

---

## Self-Review 紀錄

- Spec 覆蓋：§4.1→Task 4/5、§4.2→Task 7、§4.3→Task 9、§4.4→Task 1-3、§5→Task 10、§6.1-6.6→Task 10、§6.7→Task 11、§9 等價守門→Task 8、§10 交付→Task 12。無缺。
- 型別一致：`requote_threshold_factor` 名稱三處統一（decision/config/tick_sim）；PositionBook 簽名 Task 6 定義、Task 7/8 引用一致。
- Placeholder：Task 3 的 OLD_RECORD_JSON 與 Task 9 的 W 切點是「實作時從真實資料取」的刻意留白，取法已寫明指令，非 TBD。
