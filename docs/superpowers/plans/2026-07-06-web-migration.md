# #9 web/ 遷移實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** web/ 全面遷移到新系統（grid_engine + backtest），刪除舊系統（core/、ui/、exchanges/、main.py），config/models.py 瘦身。

**Architecture:** 兩階段——Phase 1（Task 1-8）新增三個 service 模組後逐頁改接新系統，web 砍掉 bot 生命週期控制；Phase 2（Task 9-11）grep 證零引用後機械刪除舊碼。存檔採 merge-preserve 防止 grid_engine schema 缺欄位（trading_mode 等）靜默流失。

**Tech Stack:** Python 3 / Streamlit / pandas / ccxt / Optuna / pytest。套件用 `uv` 管理。

**Spec:** `docs/superpowers/specs/2026-07-06-web-migration-design.md`（含量化 review 修訂，必讀）

## Global Constraints

- **不動 grid_engine/ 任何檔案**（#4 24h replay 驗收未完成）。backtest/、indicators/、coin_selection/ 中僅 Task 10 明列的死碼清理可動。
- git stage 只用 `git add <file>...` 明確列檔，禁止 `git add -A` / `git add .`。
- 全套測試基線 **270 passed**（`uv run pytest tests/ -q`）。每個 task 結尾跑全套，數量只能增不能減。
- `config/trading_config_max.json` 是生產引擎讀的檔，任何寫入路徑都必須走 Task 1 的 merge-preserve，禁止直接 `GlobalConfig.save()`。
- web/ 頁面檔名含 emoji（如 `web/pages/1_📈_交易監控.py`），shell 操作記得加引號。
- Streamlit 頁面本體不好單測——所有可測邏輯放 `web/services/`，頁面只做渲染。

---

## File Structure

```
web/services/__init__.py          # 新增（空檔）
web/services/config_store.py      # 新增：merge-preserve config 讀寫 + symbol extras（trading_mode）
web/services/history_reader.py    # 新增：decisions.jsonl / bandit_state.json 讀取
web/services/backtest_service.py  # 新增：SymbolConfig→backtest.Config 映射、回測/優化執行、結果 view 歸一
tests/web/__init__.py             # 新增（空檔）
tests/web/test_config_store.py    # 新增
tests/web/test_history_reader.py  # 新增
tests/web/test_backtest_service.py# 新增
web/state.py                      # 重寫：砍 bot 生命週期，config 走 config_store
web/app.py                        # 修改：砍啟停/bot 統計
web/components/sidebar.py         # 修改：移除 is_trading_active
web/pages/1_📈_交易監控.py         # 重寫：歷史檢視（讀 history_reader）
web/pages/2_⚙️_交易對管理.py       # 修改：grid_engine SymbolConfig + trading_mode via extras
web/pages/3_🔬_回測優化.py         # 修改：BacktestManager 五呼叫點改 backtest_service
web/pages/4_🛠️_設定.py            # 修改：Binance 專用 + ccxt 直連
scripts/compare_backtest_engines.py # 新增（Task 8）：新舊引擎成本歸零對比，Phase 2 前必跑
# Phase 2 刪除：core/ ui/ main.py exchanges/ scripts/check_symbol_conversion.py
# Phase 2 修改：config/models.py coin_selection/{ws_provider,symbol_scanner}.py scripts/check_web_system.py README.md
```

---

### Task 1: config_store — merge-preserve 讀寫

**Files:**
- Create: `web/services/__init__.py`（空檔）、`web/services/config_store.py`
- Create: `tests/web/__init__.py`（空檔）、`tests/web/test_config_store.py`

**Interfaces:**
- Consumes: `grid_engine.config.GlobalConfig`（`from_dict`/`to_dict`）、`grid_engine.utils.CONFIG_FILE`
- Produces（後續 task 依賴的簽名）:
  - `load_raw(path: Path | None = None) -> dict`
  - `load_config(path: Path | None = None) -> GlobalConfig`
  - `get_symbol_extra(ccxt_symbol: str, key: str, default=None, path: Path | None = None)`
  - `save_config(config: GlobalConfig, symbol_extras: dict[str, dict] | None = None, path: Path | None = None) -> None`

**背景（為什麼要 merge-preserve）**：`grid_engine.GlobalConfig.to_dict()` 不含 JSON 裡實際存在的 `trading_mode`（per-symbol）、`hard_stop_enabled`/`max_loss_pct`/`max_position_loss_pct`（risk）、`exchange_type`/`testnet`（top-level）。直接 to_dict 覆寫會把這些欄位永久抹掉，其中 `trading_mode` 被頁3 優化器使用。策略：寫檔時以 raw JSON 為底，只覆蓋 engine schema 已知欄位，未知 key 原樣保留。

- [ ] **Step 1: 寫 failing tests**

```python
# tests/web/test_config_store.py
"""config_store merge-preserve 測試。

為什麼重要：config/trading_config_max.json 是生產引擎讀的檔。
grid_engine schema 缺 trading_mode/hard_stop 等欄位，naive to_dict 覆寫
會靜默抹掉它們（trading_mode 丟了 → 頁3 優化參數範圍錯）。
"""
import json
import pytest
from pathlib import Path

from web.services import config_store
from grid_engine.config import GlobalConfig

SAMPLE = {
    "api_key": "k", "api_secret": "s",
    "exchange_type": "binance",       # 舊 schema 欄位，engine 不認識
    "testnet": False,                  # 舊 schema 欄位
    "symbols": {
        "XRP/USDC:USDC": {
            "symbol": "XRPUSDC", "ccxt_symbol": "XRP/USDC:USDC",
            "enabled": True, "take_profit_spacing": 0.004,
            "grid_spacing": 0.006, "initial_quantity": 3.0,
            "leverage": 20, "limit_multiplier": 5.0,
            "threshold_multiplier": 20.0,
            "trading_mode": "swing",   # engine schema 沒有此欄位
        }
    },
    "risk": {
        "enabled": True, "margin_threshold": 0.5,
        "hard_stop_enabled": True,          # engine RiskConfig 沒有
        "max_loss_pct": 0.1,                # engine RiskConfig 沒有
        "max_position_loss_pct": 0.05,      # engine RiskConfig 沒有
    },
}


@pytest.fixture
def cfg_file(tmp_path):
    p = tmp_path / "trading_config_max.json"
    p.write_text(json.dumps(SAMPLE, indent=2))
    return p


def test_load_config_parses_engine_fields(cfg_file):
    config = config_store.load_config(path=cfg_file)
    assert isinstance(config, GlobalConfig)
    assert "XRP/USDC:USDC" in config.symbols
    assert config.symbols["XRP/USDC:USDC"].take_profit_spacing == 0.004


def test_get_symbol_extra_reads_trading_mode(cfg_file):
    assert config_store.get_symbol_extra(
        "XRP/USDC:USDC", "trading_mode", path=cfg_file) == "swing"
    assert config_store.get_symbol_extra(
        "XRP/USDC:USDC", "nonexistent", default="d", path=cfg_file) == "d"


def test_save_preserves_unknown_fields(cfg_file):
    """核心保證：engine schema 沒有的欄位，存檔後原樣保留。"""
    config = config_store.load_config(path=cfg_file)
    config.symbols["XRP/USDC:USDC"].leverage = 25  # 模擬頁2 編輯
    config_store.save_config(config, path=cfg_file)

    raw = json.loads(cfg_file.read_text())
    assert raw["symbols"]["XRP/USDC:USDC"]["leverage"] == 25          # 編輯生效
    assert raw["symbols"]["XRP/USDC:USDC"]["trading_mode"] == "swing"  # 未知欄位保留
    assert raw["exchange_type"] == "binance"                            # top-level 保留
    assert raw["testnet"] is False
    assert raw["risk"]["hard_stop_enabled"] is True                     # risk 未知欄位保留
    assert raw["risk"]["max_loss_pct"] == 0.1
    assert raw["risk"]["max_position_loss_pct"] == 0.05


def test_save_applies_symbol_extras(cfg_file):
    """頁2 編輯 trading_mode 走 extras 通道。"""
    config = config_store.load_config(path=cfg_file)
    config_store.save_config(
        config,
        symbol_extras={"XRP/USDC:USDC": {"trading_mode": "high_freq"}},
        path=cfg_file)
    raw = json.loads(cfg_file.read_text())
    assert raw["symbols"]["XRP/USDC:USDC"]["trading_mode"] == "high_freq"


def test_save_new_symbol_and_removed_symbol(cfg_file):
    """新增 symbol 進檔；config 移除的 symbol 從檔案消失（刪除是有意操作）。"""
    from grid_engine.config import SymbolConfig
    config = config_store.load_config(path=cfg_file)
    config.symbols["BNB/USDC:USDC"] = SymbolConfig(
        symbol="BNBUSDC", ccxt_symbol="BNB/USDC:USDC")
    del config.symbols["XRP/USDC:USDC"]
    config_store.save_config(config, path=cfg_file)
    raw = json.loads(cfg_file.read_text())
    assert "BNB/USDC:USDC" in raw["symbols"]
    assert "XRP/USDC:USDC" not in raw["symbols"]


def test_save_creates_one_time_backup(cfg_file):
    """首次存檔前建 .bak-pre-web-migration 備份，之後不再覆蓋。"""
    config = config_store.load_config(path=cfg_file)
    config_store.save_config(config, path=cfg_file)
    bak = cfg_file.with_name(cfg_file.name + ".bak-pre-web-migration")
    assert bak.exists()
    original = json.loads(bak.read_text())
    assert original["symbols"]["XRP/USDC:USDC"]["leverage"] == 20

    # 二次存檔改值，備份不變
    config.symbols["XRP/USDC:USDC"].leverage = 30
    config_store.save_config(config, path=cfg_file)
    assert json.loads(bak.read_text())["symbols"]["XRP/USDC:USDC"]["leverage"] == 20


def test_roundtrip_real_config_no_field_loss():
    """用 repo 現況 JSON 實測 round-trip 零欄位遺失（遞迴比對 key 集合）。"""
    real = Path(__file__).resolve().parents[2] / "config" / "trading_config_max.json"
    if not real.exists():
        pytest.skip("no real config")
    import shutil, tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "trading_config_max.json"
        shutil.copy(real, p)
        before = json.loads(p.read_text())
        config = config_store.load_config(path=p)
        config_store.save_config(config, path=p)
        after = json.loads(p.read_text())

        def keys_recursive(d, prefix=""):
            out = set()
            for k, v in d.items():
                out.add(f"{prefix}{k}")
                if isinstance(v, dict):
                    out |= keys_recursive(v, f"{prefix}{k}.")
            return out

        missing = keys_recursive(before) - keys_recursive(after)
        assert missing == set(), f"存檔遺失欄位: {missing}"
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `uv run pytest tests/web/test_config_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'web.services'`（或 collection error）。
注意：若 `web/` 缺 `__init__.py` 導致 import 問題，確認 `web/__init__.py` 存在（現況已存在）。

- [ ] **Step 3: 實作 config_store**

```python
# web/services/config_store.py
"""Config 讀寫（merge-preserve）。

為什麼不用 grid_engine.GlobalConfig.save()：engine 的 to_dict() 只 emit
engine schema 認識的欄位，而 config/trading_config_max.json 還有
trading_mode（頁3 優化器用）、risk hard_stop 三欄、exchange_type/testnet
等欄位。直接覆寫會把它們永久抹掉。這裡以 raw JSON 為底做欄位級 merge。
"""
import json
import shutil
from pathlib import Path
from typing import Optional

from grid_engine.config import GlobalConfig
from grid_engine.utils import CONFIG_FILE

BACKUP_SUFFIX = ".bak-pre-web-migration"


def _resolve(path: Optional[Path]) -> Path:
    return Path(path) if path is not None else CONFIG_FILE


def load_raw(path: Optional[Path] = None) -> dict:
    p = _resolve(path)
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def load_config(path: Optional[Path] = None) -> GlobalConfig:
    return GlobalConfig.from_dict(load_raw(path))


def get_symbol_extra(ccxt_symbol: str, key: str, default=None,
                     path: Optional[Path] = None):
    raw = load_raw(path)
    return raw.get("symbols", {}).get(ccxt_symbol, {}).get(key, default)


def _ensure_backup(p: Path) -> None:
    if not p.exists():
        return
    bak = p.with_name(p.name + BACKUP_SUFFIX)
    if not bak.exists():
        shutil.copy(p, bak)


def save_config(config: GlobalConfig,
                symbol_extras: Optional[dict] = None,
                path: Optional[Path] = None) -> None:
    """merge-preserve 存檔。

    - top-level：raw 有、engine to_dict 沒有的 key 原樣保留。
    - symbols：以 config.symbols 為準（新增進檔、刪除移除），
      每個 symbol 內 raw 有、engine 沒有的 key（如 trading_mode）保留。
    - risk 等巢狀 dict：同樣欄位級 merge。
    - symbol_extras：{ccxt_symbol: {key: value}} 顯式覆寫（頁2 編輯 trading_mode 用）。
    """
    p = _resolve(path)
    raw = load_raw(p)
    new = config.to_dict()

    merged = dict(raw)  # raw 為底，保留未知 top-level key
    for k, v in new.items():
        if k == "symbols":
            merged_symbols = {}
            raw_symbols = raw.get("symbols", {})
            for sym_key, sym_new in v.items():
                sym_merged = dict(raw_symbols.get(sym_key, {}))
                sym_merged.update(sym_new)
                merged_symbols[sym_key] = sym_merged
            merged[k] = merged_symbols  # config 已刪的 symbol 不進 merged
        elif isinstance(v, dict) and isinstance(raw.get(k), dict):
            sub = dict(raw[k])
            sub.update(v)
            merged[k] = sub
        else:
            merged[k] = v

    for sym_key, extras in (symbol_extras or {}).items():
        if sym_key in merged.get("symbols", {}):
            merged["symbols"][sym_key].update(extras)

    _ensure_backup(p)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
```

同時建兩個空檔：`web/services/__init__.py`、`tests/web/__init__.py`。

- [ ] **Step 4: 跑測試確認 pass**

Run: `uv run pytest tests/web/test_config_store.py -v`
Expected: 7 passed

- [ ] **Step 5: 全套回歸 + commit**

Run: `uv run pytest tests/ -q`
Expected: 277 passed（270 基線 + 7 新）

```bash
git add web/services/__init__.py web/services/config_store.py tests/web/__init__.py tests/web/test_config_store.py
git commit -m "feat: #9 config_store merge-preserve 存檔（防 trading_mode/hard_stop 欄位流失）"
```

---

### Task 2: history_reader — decisions.jsonl / bandit_state 讀取

**Files:**
- Create: `web/services/history_reader.py`
- Test: `tests/web/test_history_reader.py`

**Interfaces:**
- Produces:
  - `load_decisions(path: Path | None = None, max_lines: int = 5000) -> pd.DataFrame` — 欄位至少 `ts`(datetime)、`symbol`、`price`、`long_position`、`short_position`；損毀行跳過不炸。
  - `load_bandit_state(path: Path | None = None) -> dict` — 檔案不存在/損毀回 `{}`。
  - `last_activity(df: pd.DataFrame) -> Optional[datetime]` — 空 DataFrame 回 None。

**資料格式**（`logs/decisions.jsonl` 每行一筆）：
```json
{"ts": 1783265786.63, "symbol": "BNB/USDC:USDC", "inputs": {"price": 588.405, "long_position": 0.58, "short_position": 0.06, "grid_spacing": 0.003, "take_profit_spacing": 0.003, ...}, ...}
```

- [ ] **Step 1: 寫 failing tests**

```python
# tests/web/test_history_reader.py
"""history_reader 測試。

為什麼重要：頁1 監控降級後唯一資料源是 decisions.jsonl。
引擎隨時在 append、行可能截斷，reader 必須對損毀輸入免疫。
"""
import json
import pandas as pd
from datetime import datetime

from web.services import history_reader

GOOD = {"ts": 1783265786.63, "symbol": "BNB/USDC:USDC",
        "inputs": {"price": 588.405, "long_position": 0.58,
                   "short_position": 0.06}}


def _write_jsonl(tmp_path, lines):
    p = tmp_path / "decisions.jsonl"
    p.write_text("\n".join(lines))
    return p


def test_load_decisions_parses_fields(tmp_path):
    p = _write_jsonl(tmp_path, [json.dumps(GOOD)])
    df = history_reader.load_decisions(path=p)
    assert len(df) == 1
    row = df.iloc[0]
    assert row["symbol"] == "BNB/USDC:USDC"
    assert row["price"] == 588.405
    assert row["long_position"] == 0.58
    assert isinstance(row["ts"], pd.Timestamp)


def test_load_decisions_skips_corrupt_lines(tmp_path):
    p = _write_jsonl(tmp_path, [
        json.dumps(GOOD),
        '{"ts": 178326, "symbol": "X", "inputs"',  # 截斷行（引擎寫入中）
        "",                                          # 空行
        "not json at all",
        json.dumps(GOOD),
    ])
    df = history_reader.load_decisions(path=p)
    assert len(df) == 2


def test_load_decisions_missing_file(tmp_path):
    df = history_reader.load_decisions(path=tmp_path / "nope.jsonl")
    assert df.empty


def test_load_decisions_tail_limit(tmp_path):
    lines = [json.dumps({**GOOD, "ts": GOOD["ts"] + i}) for i in range(100)]
    p = _write_jsonl(tmp_path, lines)
    df = history_reader.load_decisions(path=p, max_lines=10)
    assert len(df) == 10
    # tail 語意：拿最後 10 筆（最新的）
    assert df["ts"].max() == pd.to_datetime(GOOD["ts"] + 99, unit="s")


def test_load_bandit_state(tmp_path):
    p = tmp_path / "bandit_state.json"
    p.write_text(json.dumps({"arms": {"a": 1}}))
    assert history_reader.load_bandit_state(path=p) == {"arms": {"a": 1}}
    assert history_reader.load_bandit_state(path=tmp_path / "no.json") == {}
    p.write_text("{corrupt")
    assert history_reader.load_bandit_state(path=p) == {}


def test_last_activity(tmp_path):
    p = _write_jsonl(tmp_path, [json.dumps(GOOD)])
    df = history_reader.load_decisions(path=p)
    assert history_reader.last_activity(df) is not None
    assert history_reader.last_activity(pd.DataFrame()) is None
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `uv run pytest tests/web/test_history_reader.py -v`
Expected: FAIL — `cannot import name 'history_reader'`

- [ ] **Step 3: 實作**

```python
# web/services/history_reader.py
"""grid_engine 落地檔讀取（頁1 歷史檢視資料層）。

引擎在 GCE/本地持續 append decisions.jsonl，行可能截斷——
逐行 json.loads、壞行跳過，永不 raise。
"""
import json
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DECISIONS_PATH = _PROJECT_ROOT / "logs" / "decisions.jsonl"
BANDIT_STATE_PATH = _PROJECT_ROOT / "logs" / "bandit_state.json"


def load_decisions(path: Optional[Path] = None,
                   max_lines: int = 5000) -> pd.DataFrame:
    p = Path(path) if path is not None else DECISIONS_PATH
    if not p.exists():
        return pd.DataFrame()

    rows = []
    with open(p, encoding="utf-8", errors="replace") as f:
        tail = deque(f, maxlen=max_lines)
    for line in tail:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        inputs = rec.get("inputs", {}) or {}
        rows.append({
            "ts": rec.get("ts"),
            "symbol": rec.get("symbol", ""),
            "price": inputs.get("price"),
            "long_position": inputs.get("long_position"),
            "short_position": inputs.get("short_position"),
            "grid_spacing": inputs.get("grid_spacing"),
            "take_profit_spacing": inputs.get("take_profit_spacing"),
            "long_dead_mode": inputs.get("long_dead_mode"),
            "short_dead_mode": inputs.get("short_dead_mode"),
        })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df[pd.to_numeric(df["ts"], errors="coerce").notna()]
    df["ts"] = pd.to_datetime(df["ts"], unit="s")
    return df


def load_bandit_state(path: Optional[Path] = None) -> dict:
    p = Path(path) if path is not None else BANDIT_STATE_PATH
    if not p.exists():
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError, ValueError):
        return {}


def last_activity(df: pd.DataFrame) -> Optional[datetime]:
    if df is None or df.empty or "ts" not in df.columns:
        return None
    return df["ts"].max().to_pydatetime()
```

- [ ] **Step 4: 跑測試確認 pass**

Run: `uv run pytest tests/web/test_history_reader.py -v`
Expected: 6 passed

- [ ] **Step 5: 全套回歸 + commit**

Run: `uv run pytest tests/ -q`
Expected: 283 passed

```bash
git add web/services/history_reader.py tests/web/test_history_reader.py
git commit -m "feat: #9 history_reader — decisions.jsonl/bandit_state 容錯讀取"
```

---

### Task 3: backtest_service — config 映射與回測/優化執行

**Files:**
- Create: `web/services/backtest_service.py`
- Test: `tests/web/test_backtest_service.py`

**Interfaces:**
- Consumes: `grid_engine.config.SymbolConfig`、`backtest.config.Config`、`backtest.backtester.GridBacktester/BacktestResult`、`backtest.data_loader.DataLoader`、`backtest.optimizer.GridOptimizer/OptimizationResult`、`backtest.smart_optimizer.SmartOptimizer/SmartOptimizationResult/TradingMode/OptimizationObjective`
- Produces:
  - `to_backtest_config(sym: SymbolConfig, *, initial_balance: float = 1000.0, zero_costs: bool = False) -> Config`（`initial_quantity <= 0` raise ValueError）
  - `run_single_backtest(sym: SymbolConfig, df: pd.DataFrame) -> dict`（view dict）
  - `run_grid_optimization(sym, df, progress_callback=None) -> pd.DataFrame`
  - `run_smart_optimization(sym, df, *, n_trials, objective: str, trading_mode: str | None, progress_callback=None) -> pd.DataFrame`
  - `backtest_result_to_view(result: BacktestResult) -> dict`
  - view dict keys：`return_pct, max_drawdown, realized_pnl, unrealized_pnl, total_pnl, trades_count, win_rate, profit_factor, sharpe_ratio, final_equity, trade_history, equity_curve, notes`

**關鍵語意（實作前必讀）**：
- `GridBacktester.run()` 主路徑（`backtest/backtester.py:541-542`）**永遠**以 `initial_quantity × threshold_multiplier / limit_multiplier` 計算閾值，無視 `Config.position_threshold/position_limit` 絕對值；絕對值只在 `initial_quantity <= 0` 的 deprecated legacy 路徑生效。因此守門條件是 `initial_quantity > 0`（否則 raise）+ multiplier 必須從 SymbolConfig 帶入。
- 現有頁3 smart 路徑（`web/pages/3:404-410`）的 base_config 沒帶 multiplier（用預設 5/14 而非使用者配置）——`to_backtest_config` 統一修掉。

- [ ] **Step 1: 寫 failing tests**

```python
# tests/web/test_backtest_service.py
"""backtest_service 黃金測試。

為什麼重要：SymbolConfig→backtest.Config 映射錯了不會炸，
只會默默給錯回測結論（量化系統最貴的一類 bug）。
已知輸入→已知輸出鎖死每個欄位。
"""
import pandas as pd
import numpy as np
import pytest

from web.services import backtest_service
from grid_engine.config import SymbolConfig


SYM = SymbolConfig(
    symbol="XRPUSDC", ccxt_symbol="XRP/USDC:USDC", enabled=True,
    take_profit_spacing=0.004, grid_spacing=0.006,
    initial_quantity=3.0, leverage=20,
    limit_multiplier=5.0, threshold_multiplier=20.0,
)


def test_to_backtest_config_golden():
    cfg = backtest_service.to_backtest_config(SYM)
    assert cfg.symbol == "XRPUSDC"
    assert cfg.initial_quantity == 3.0          # 預設 0.0=空回測，必須帶入
    assert cfg.leverage == 20
    assert cfg.take_profit_spacing == 0.004     # 兩邊皆小數比例，1:1
    assert cfg.grid_spacing == 0.006
    assert cfg.limit_multiplier == 5.0          # 不帶 → backtester 用預設 5/14
    assert cfg.threshold_multiplier == 20.0
    assert cfg.initial_balance == 1000.0
    # 成本模型：單次回測用引擎預設（保真）
    assert cfg.fee_pct == 0.0004
    assert cfg.funding_enabled is True


def test_to_backtest_config_rejects_zero_quantity():
    """initial_quantity<=0 會落入 legacy 絕對值路徑（500/100 預設）→ 直接拒絕。"""
    bad = SymbolConfig(symbol="X", ccxt_symbol="X/USDC:USDC", initial_quantity=0)
    with pytest.raises(ValueError):
        backtest_service.to_backtest_config(bad)


def test_to_backtest_config_zero_costs():
    """新舊引擎對比模式：成本全歸零。"""
    cfg = backtest_service.to_backtest_config(SYM, zero_costs=True)
    assert cfg.fee_pct == 0.0
    assert cfg.slippage_bps == 0.0
    assert cfg.funding_enabled is False


def _make_df(n=300, price=1.0):
    """合成 1m K 線：正弦波動保證網格有成交。"""
    ts = pd.date_range("2026-01-01", periods=n, freq="1min")
    wave = price * (1 + 0.02 * np.sin(np.arange(n) / 20))
    return pd.DataFrame({
        "timestamp": ts, "open": wave, "high": wave * 1.001,
        "low": wave * 0.999, "close": wave, "volume": 100.0,
    })


def test_run_single_backtest_returns_view_dict():
    view = backtest_service.run_single_backtest(SYM, _make_df())
    for key in ("return_pct", "max_drawdown", "total_pnl", "trades_count",
                "win_rate", "profit_factor", "sharpe_ratio", "final_equity",
                "trade_history", "equity_curve"):
        assert key in view, f"view 缺 {key}"
    assert isinstance(view["trades_count"], int)


def test_grid_optimization_returns_dataframe():
    param_ranges = {"take_profit_spacing": [0.003, 0.004],
                    "grid_spacing": [0.005, 0.006]}
    df = backtest_service.run_grid_optimization(
        SYM, _make_df(), param_ranges=param_ranges)
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 4  # 2x2 組合
    assert "take_profit_spacing" in df.columns
    assert "return_pct" in df.columns


def test_smart_optimization_returns_dataframe():
    pytest.importorskip("optuna")
    df = backtest_service.run_smart_optimization(
        SYM, _make_df(), n_trials=3, objective="sharpe", trading_mode="swing")
    assert isinstance(df, pd.DataFrame)
    assert len(df) >= 1
    assert "objective_value" in df.columns
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `uv run pytest tests/web/test_backtest_service.py -v`
Expected: FAIL — import error

- [ ] **Step 3: 實作**

```python
# web/services/backtest_service.py
"""回測服務層：SymbolConfig→Config 映射 + 回測/優化執行 + 結果歸一。

所有轉換集中在此（可脫離 Streamlit 單測），頁3 只做渲染。
兩種優化器（GridOptimizer / SmartOptimizer）結果歸一成同一張
DataFrame，頁面單一渲染路徑。
"""
from typing import Callable, Dict, List, Optional

import pandas as pd

from grid_engine.config import SymbolConfig
from backtest.config import Config
from backtest.backtester import GridBacktester, BacktestResult
from backtest.optimizer import GridOptimizer

try:
    from backtest.smart_optimizer import (
        SmartOptimizer, TradingMode, OptimizationObjective,
    )
    SMART_AVAILABLE = True
except ImportError:
    SMART_AVAILABLE = False


def to_backtest_config(sym: SymbolConfig, *,
                       initial_balance: float = 1000.0,
                       zero_costs: bool = False) -> Config:
    """SymbolConfig → backtest.Config。

    initial_quantity<=0 會讓 GridBacktester 落入 deprecated legacy 路徑
    （使用 position_threshold/limit 絕對值預設 500/100），直接拒絕。
    multiplier 必須帶入：backtester.run() 以
    initial_quantity×multiplier 計算閾值（backtester.py:541-542）。
    """
    if sym.initial_quantity <= 0:
        raise ValueError(
            f"initial_quantity 必須 > 0（{sym.symbol} 現值 "
            f"{sym.initial_quantity}），否則回測落入 legacy 絕對值路徑")
    cfg = Config(
        symbol=sym.symbol,
        initial_balance=initial_balance,
        initial_quantity=sym.initial_quantity,
        leverage=sym.leverage,
        take_profit_spacing=sym.take_profit_spacing,
        grid_spacing=sym.grid_spacing,
        limit_multiplier=sym.limit_multiplier,
        threshold_multiplier=sym.threshold_multiplier,
        position_threshold=0.0,   # 明確歸零：主路徑本就不讀，防 legacy 誤用
        position_limit=0.0,
    )
    if zero_costs:
        cfg.fee_pct = 0.0
        cfg.slippage_bps = 0.0
        cfg.funding_enabled = False
    return cfg


def backtest_result_to_view(result: BacktestResult) -> dict:
    return {
        "return_pct": result.return_pct,
        "max_drawdown": result.max_drawdown,
        "realized_pnl": result.realized_pnl,
        "unrealized_pnl": result.unrealized_pnl,
        "total_pnl": result.total_pnl,
        "trades_count": result.trades_count,
        "win_rate": result.win_rate,
        "profit_factor": result.profit_factor,
        "sharpe_ratio": result.sharpe_ratio,
        "final_equity": result.final_equity,
        "trade_history": result.trade_history,
        "equity_curve": result.equity_curve,
        "notes": result.notes,
    }


def run_single_backtest(sym: SymbolConfig, df: pd.DataFrame) -> dict:
    cfg = to_backtest_config(sym)
    result = GridBacktester(df, cfg).run()
    return backtest_result_to_view(result)


def run_grid_optimization(
        sym: SymbolConfig, df: pd.DataFrame,
        param_ranges: Optional[Dict[str, List]] = None,
        progress_callback: Optional[Callable[[int, int], None]] = None,
) -> pd.DataFrame:
    """網格搜尋。回傳 all_results DataFrame（含各參數欄 + 指標欄）。"""
    base = to_backtest_config(sym)
    optimizer = GridOptimizer(df, base_config=base, param_ranges=param_ranges)
    result = optimizer.run(progress_callback=progress_callback)
    return result.all_results


def run_smart_optimization(
        sym: SymbolConfig, df: pd.DataFrame, *,
        n_trials: int = 100, objective: str = "sharpe",
        trading_mode: Optional[str] = None,
        progress_callback: Optional[Callable] = None,
) -> pd.DataFrame:
    """Optuna TPE。結果歸一成 DataFrame：每 trial 一列，
    參數欄 + objective_value 欄，與網格搜尋同構供頁面單一渲染。"""
    if not SMART_AVAILABLE:
        raise RuntimeError("Optuna 未安裝（uv add optuna）")
    base = to_backtest_config(sym)
    mode = TradingMode(trading_mode) if trading_mode else None
    objective_map = {
        "return": OptimizationObjective.RETURN,
        "sharpe": OptimizationObjective.SHARPE,
        "sortino": OptimizationObjective.SORTINO,
        "calmar": OptimizationObjective.CALMAR,
        "profit_factor": OptimizationObjective.PROFIT_FACTOR,
        "risk_adjusted": OptimizationObjective.RISK_ADJUSTED,
    }
    optimizer = SmartOptimizer(df, base_config=base, trading_mode=mode)
    smart = optimizer.optimize(
        n_trials=n_trials,
        objective=objective_map.get(objective, OptimizationObjective.SHARPE),
        progress_callback=progress_callback,
        show_progress=False,
    )
    rows = []
    for t in smart.all_trials:
        row = dict(t.params) if hasattr(t, "params") else dict(t.get("params", {}))
        value = t.value if hasattr(t, "value") else t.get("value")
        row["objective_value"] = value
        rows.append(row)
    out = pd.DataFrame(rows)
    out.attrs["best_params"] = smart.best_params
    out.attrs["best_metrics"] = smart.best_metrics
    out.attrs["param_importance"] = smart.param_importance
    return out
```

**注意**：`smart.all_trials` 的元素型別（Optuna trial 物件 vs dict）以 `backtest/smart_optimizer.py` 的 `SmartOptimizationResult` 實際定義為準——實作時先讀該 dataclass，測試 `test_smart_optimization_returns_dataframe` 會抓到不匹配。

- [ ] **Step 4: 跑測試確認 pass**

Run: `uv run pytest tests/web/test_backtest_service.py -v`
Expected: 7 passed（Optuna 未裝時 smart 測試 skip——先 `uv run python -c "import optuna"` 確認，沒裝就 `uv add optuna`）

- [ ] **Step 5: 全套回歸 + commit**

Run: `uv run pytest tests/ -q`
Expected: 290 passed

```bash
git add web/services/backtest_service.py tests/web/test_backtest_service.py
git commit -m "feat: #9 backtest_service — config 黃金映射 + 雙優化器歸一"
```

---

### Task 4: state.py 瘦身 + app.py / sidebar / 頁1 改接

一個 task 做完（耦合：state.py 刪 API 會立刻弄壞三個消費者，必須原子完成保持 repo 綠）。

**Files:**
- Modify: `web/state.py`（整檔重寫）
- Modify: `web/app.py`（`render_main_metrics` 58-104、`render_control_panel` 147-211、227 行 stats）
- Modify: `web/components/sidebar.py:56-98`
- Rewrite: `web/pages/1_📈_交易監控.py`

**Interfaces:**
- Produces（新 `web/state.py` 全部公開 API，消費者只能用這些）:
  - `init_session_state()`、`get_config() -> GlobalConfig`（grid_engine 版）
  - `save_config(symbol_extras: dict | None = None)`、`reload_config()`、`check_config_updated() -> bool`
- 刪除：`start_trading`/`stop_trading`/`get_bot`/`is_trading_active`/`get_trading_stats`/`get_trading_duration`

- [ ] **Step 1: 重寫 web/state.py**

```python
# web/state.py（整檔取代）
"""狀態管理模組
============
管理 Streamlit session state 的配置生命週期。
bot 生命週期已移除——生產引擎（grid_engine）在 GCE 以獨立行程運行，
web 只做監控（讀落地檔）、回測、設定。
"""
import sys
from pathlib import Path

import streamlit as st

_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from grid_engine.config import GlobalConfig  # noqa: E402
from web.services import config_store  # noqa: E402


def init_session_state():
    """初始化 session state"""
    if "initialized" not in st.session_state:
        st.session_state.initialized = True
        st.session_state.config = config_store.load_config()
        st.session_state.config_version = 0


def get_config() -> GlobalConfig:
    init_session_state()
    return st.session_state.config


def save_config(symbol_extras: dict | None = None):
    """merge-preserve 儲存（防止 engine schema 缺欄位流失，見 config_store）。"""
    config_store.save_config(st.session_state.config, symbol_extras=symbol_extras)
    st.session_state.config_version = st.session_state.get("config_version", 0) + 1


def reload_config():
    st.session_state.config = config_store.load_config()


def check_config_updated() -> bool:
    """檢查配置是否已被其他頁面更新，不同步則自動重載。"""
    init_session_state()
    try:
        file_config = config_store.load_config()
        current_symbols = set(st.session_state.config.symbols.keys())
        file_symbols = set(file_config.symbols.keys())
        if current_symbols != file_symbols:
            st.session_state.config = file_config
            return True
        for symbol in current_symbols:
            current = st.session_state.config.symbols[symbol]
            file_cfg = file_config.symbols[symbol]
            if (current.take_profit_spacing != file_cfg.take_profit_spacing or
                    current.grid_spacing != file_cfg.grid_spacing or
                    current.initial_quantity != file_cfg.initial_quantity or
                    current.leverage != file_cfg.leverage or
                    current.limit_multiplier != file_cfg.limit_multiplier or
                    current.threshold_multiplier != file_cfg.threshold_multiplier or
                    current.enabled != file_cfg.enabled):
                st.session_state.config = file_config
                return True
        return False
    except Exception as e:
        print(f"[State] 檢查配置失敗: {e}")
        return False
```

- [ ] **Step 2: 改 web/components/sidebar.py**

56-98 行的「系統狀態」段：`from state import get_config, is_trading_active` 改為 `from state import get_config`；`if is_trading_active(): ...交易運行中... else: ...待命中...` 的整個 if/else 區塊換成單一靜態徽章：

```python
            st.markdown("**系統狀態**")
            st.markdown("""
            <span style="
                display: inline-flex;
                align-items: center;
                padding: 6px 12px;
                border-radius: 20px;
                font-size: 13px;
                font-weight: 600;
                background: rgba(108, 99, 255, 0.15);
                color: #6C63FF;
                border: 1px solid rgba(108, 99, 255, 0.3);
            ">引擎於 GCE 運行</span>
            """, unsafe_allow_html=True)
```

「啟用功能」段不動（只依賴 config）。

- [ ] **Step 3: 改 web/app.py**

1. 檔頭 import：移除 `is_trading_active, get_trading_stats, get_trading_duration, start_trading, stop_trading`（保留 `get_config` 等實際還在用的——改完後 `grep -n "from state import" web/app.py` 核對）。
2. `render_main_metrics()`：刪掉 `if is_trading_active():` 整個分支，只留原本 else 分支的配置摘要（縮排提一層）。
3. `render_control_panel()`：整個函數換成引導卡片：

```python
def render_control_panel():
    """渲染引擎狀態說明（bot 生命週期由 GCE systemd 管理，web 不啟停）"""
    st.markdown("### 引擎狀態")

    from web.services import history_reader
    df = history_reader.load_decisions(max_lines=1)
    last = history_reader.last_activity(df)

    col1, col2 = st.columns([3, 1])
    with col1:
        if last is not None:
            st.info(f"🛰️ 生產引擎於 GCE 運行（本機最後決策記錄: "
                    f"{last.strftime('%Y-%m-%d %H:%M:%S')}）。"
                    f"實盤告警走 Telegram，web 僅為歷史檢視。")
        else:
            st.info("🛰️ 生產引擎於 GCE 運行。本機無決策記錄檔（logs/decisions.jsonl）。"
                    "實盤告警走 Telegram，web 僅為歷史檢視。")
    with col2:
        if st.button("📊 查看歷史", width='stretch'):
            st.switch_page("pages/1_📈_交易監控.py")
```

4. 其餘引用 `get_trading_stats`/`is_trading_active` 的殘留（227 行附近）一律改為讀 config 或直接刪除該顯示；改完 `grep -n "is_trading_active\|get_trading_stats\|get_trading_duration\|start_trading\|stop_trading\|get_bot" web/app.py` 必須零筆。

- [ ] **Step 4: 重寫頁1**

```python
# web/pages/1_📈_交易監控.py（整檔取代）
"""交易歷史檢視頁
================
資料源：grid_engine 落地檔（logs/decisions.jsonl + logs/bandit_state.json）。
非即時監控——實盤告警走 Telegram（grid_engine notifier），
本頁為事後歷史檢視。
"""
import streamlit as st
import pandas as pd

st.set_page_config(
    page_title="交易歷史 - AS 網格",
    page_icon="📈",
    layout="wide",
)

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from theme import apply_custom_theme
from components.sidebar import render_sidebar
apply_custom_theme()

from state import init_session_state, get_config, check_config_updated
from web.services import history_reader

init_session_state()


def render_header(df: pd.DataFrame):
    col1, col2 = st.columns([3, 2])
    with col1:
        st.title("📈 交易歷史檢視")
    with col2:
        last = history_reader.last_activity(df)
        if last is not None:
            st.metric("最後決策時間", last.strftime("%m-%d %H:%M:%S"))
    st.caption("⚠️ 本頁為引擎落地檔的歷史檢視，非即時監控；實盤告警走 Telegram。")


def render_position_timeline(df: pd.DataFrame):
    st.subheader("📊 持倉軌跡")
    if df.empty:
        st.info("無決策記錄（logs/decisions.jsonl 不存在或為空）。"
                "引擎在本機跑過後才會有資料。")
        return
    symbols = sorted(df["symbol"].dropna().unique())
    symbol = st.selectbox("交易對", options=symbols, key="hist_symbol")
    sdf = df[df["symbol"] == symbol].set_index("ts")
    if sdf.empty:
        st.info("此交易對無記錄")
        return
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**價格**")
        st.line_chart(sdf["price"])
    with c2:
        st.markdown("**多/空持倉**")
        st.line_chart(sdf[["long_position", "short_position"]])
    # 裝死模式事件
    dead = sdf[(sdf["long_dead_mode"] == True) | (sdf["short_dead_mode"] == True)]  # noqa: E712
    if not dead.empty:
        st.warning(f"⚠️ 期間出現裝死模式 {len(dead)} 筆決策記錄"
                   f"（最近: {dead.index.max().strftime('%m-%d %H:%M')}）")


def render_latest_snapshot(df: pd.DataFrame):
    st.subheader("🔍 各交易對最新狀態")
    if df.empty:
        st.info("無決策記錄")
        return
    latest = df.sort_values("ts").groupby("symbol").tail(1)
    rows = [{
        "交易對": r["symbol"],
        "時間": r["ts"].strftime("%m-%d %H:%M:%S"),
        "價格": f"{r['price']:.6f}" if pd.notna(r["price"]) else "-",
        "多單": f"{r['long_position']:.2f}" if pd.notna(r["long_position"]) else "-",
        "空單": f"{r['short_position']:.2f}" if pd.notna(r["short_position"]) else "-",
    } for _, r in latest.iterrows()]
    st.dataframe(pd.DataFrame(rows), width='stretch', hide_index=True)


def render_bandit_state():
    st.subheader("🎰 Bandit 狀態")
    state = history_reader.load_bandit_state()
    if not state:
        st.info("無 bandit 狀態檔（logs/bandit_state.json）")
        return
    st.json(state, expanded=False)


def render_symbol_config():
    st.subheader("⚙️ 交易對配置")
    config = get_config()
    if not config.symbols:
        st.info("未配置交易對")
        return
    symbol = st.selectbox("選擇交易對", options=list(config.symbols.keys()),
                          key="cfg_symbol")
    if not symbol:
        return
    cfg = config.symbols[symbol]
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**策略參數**")
        st.write(f"- 止盈間距: {cfg.take_profit_spacing*100:.2f}%")
        st.write(f"- 補倉間距: {cfg.grid_spacing*100:.2f}%")
        st.write(f"- 每單數量: {cfg.initial_quantity}")
        st.write(f"- 槓桿: {cfg.leverage}x")
    with col2:
        st.markdown("**倉位控制**")
        st.write(f"- 加倍止盈觸發: {cfg.position_limit:.1f}")
        st.write(f"- 裝死模式觸發: {cfg.position_threshold:.1f}")
        st.write(f"- 加倍倍數: {cfg.limit_multiplier}x")
        st.write(f"- 裝死倍數: {cfg.threshold_multiplier}x")


def main():
    render_sidebar()
    if check_config_updated():
        st.info("✅ 檢測到配置已更新，正在刷新...")
        st.rerun()

    df = history_reader.load_decisions()
    render_header(df)
    st.divider()

    left, right = st.columns([2, 1])
    with left:
        render_position_timeline(df)
    with right:
        render_latest_snapshot(df)
        st.divider()
        render_bandit_state()
        st.divider()
        render_symbol_config()

    if st.button("🔄 重新載入"):
        st.rerun()


main()
```

注意：舊頁的 `cfg.position_limit`/`cfg.position_threshold` 在 grid_engine.SymbolConfig 是 **property**（`initial_quantity × multiplier`），沿用沒問題。

- [ ] **Step 5: 驗證 + 全套回歸**

```bash
grep -rn "is_trading_active\|get_trading_stats\|get_trading_duration\|start_trading\|stop_trading\|get_bot" web/ --include="*.py" | grep -v __pycache__
```
Expected: 零筆（頁2/3/4 若有殘留引用，同步移除該 import——scout 盤點只有 sidebar 和頁1 在用，但要證明）。

Run: `uv run pytest tests/ -q` → Expected: 290 passed
Run: `uv run streamlit run web/app.py --server.headless true &` 開 `http://localhost:8501`，首頁與頁1 能載入不噴錯（旗標驗證，Playwright 完整實點在 Task 11）。驗完 kill 掉。

- [ ] **Step 6: Commit**

```bash
git add web/state.py web/app.py web/components/sidebar.py "web/pages/1_📈_交易監控.py"
git commit -m "refactor: #9 web 砍 bot 生命週期 — state 瘦身、頁1 降級歷史檢視（讀 decisions.jsonl）"
```

---

### Task 5: 頁2 交易對管理改接

**Files:**
- Modify: `web/pages/2_⚙️_交易對管理.py`

**Interfaces:**
- Consumes: `grid_engine.config.SymbolConfig`、`state.save_config(symbol_extras=...)`、`config_store.get_symbol_extra`

- [ ] **Step 1: 改 import 與新增/編輯表單**

1. `:26` `from config.models import SymbolConfig` → `from grid_engine.config import SymbolConfig`。
2. 檔頭補 `from web.services import config_store`。
3. 新增交易對段（~193-204 行）：`SymbolConfig(...)` 建構參數裡 **拿掉 `trading_mode=trading_mode`**（grid_engine 版沒有此欄位），改為存檔時走 extras：

```python
            config.symbols[ccxt_sym] = SymbolConfig(
                symbol=raw,
                ccxt_symbol=ccxt_sym,
                enabled=True,
                take_profit_spacing=take_profit / 100,
                grid_spacing=grid_spacing / 100,
                initial_quantity=quantity,
                leverage=leverage,
                limit_multiplier=limit_mult,
                threshold_multiplier=threshold_mult,
            )
            save_config(symbol_extras={ccxt_sym: {"trading_mode": trading_mode}})
```

4. 編輯保存段（~303-310 行）：`cfg.trading_mode = trading_mode` 刪掉，`save_config()` 改為：

```python
                save_config(symbol_extras={symbol: {"trading_mode": trading_mode}})
```

（`symbol` 為 editing 的 ccxt key，以該段實際變數名為準。）

5. 編輯表單的 trading_mode 預設值：原本讀 `cfg.trading_mode` 的地方改為

```python
    current_mode = config_store.get_symbol_extra(symbol, "trading_mode", default="swing")
```

6. 頁內其他 `getattr(cfg, 'trading_mode', ...)` 顯示點同樣改走 `get_symbol_extra`。

- [ ] **Step 2: 驗證**

```bash
grep -n "config.models\|cfg.trading_mode\|\.trading_mode =" "web/pages/2_⚙️_交易對管理.py"
```
Expected: 零筆。
Run: `uv run pytest tests/ -q` → 290 passed。
手動：起 streamlit，頁2 新增一個測試交易對 → 打開 `config/trading_config_max.json` 確認新 symbol 進檔、既有 symbol 的 `trading_mode` 還在 → 刪掉測試交易對還原。

- [ ] **Step 3: Commit**

```bash
git add "web/pages/2_⚙️_交易對管理.py"
git commit -m "refactor: #9 頁2 改接 grid_engine SymbolConfig，trading_mode 走 extras 通道"
```

---

### Task 6: 頁3 回測優化改接

**Files:**
- Modify: `web/pages/3_🔬_回測優化.py`

**Interfaces:**
- Consumes: `backtest_service.{run_single_backtest, run_grid_optimization, run_smart_optimization, to_backtest_config}`、`DataLoader`、`config_store.get_symbol_extra`

- [ ] **Step 1: import 與 manager 快取改掉**

1. `:29,31` 刪 `from config.models import SymbolConfig` 和 `from core.backtest import BacktestManager`；改：

```python
from grid_engine.config import SymbolConfig
from backtest.data_loader import DataLoader
from web.services import backtest_service, config_store
```

2. `get_backtest_manager()`（~56-59）改：

```python
@st.cache_resource
def get_data_loader():
    """取得資料載入器 (快取)"""
    return DataLoader()
```

頁內所有 `manager = get_backtest_manager()` 呼叫點同步改 `loader = get_data_loader()`。

- [ ] **Step 2: 資料載入段改接（單次回測與優化共用的載入邏輯）**

原 `manager.get_available_dates(symbol)` + 逐日檢查（~227-240）改為 range 檢查：

```python
    date_range = loader.get_date_range(symbol)
    need_download = True
    if date_range is not None:
        have_start, have_end = date_range
        need_download = not (str(have_start) <= start_date and str(have_end) >= end_date)

    if need_download:
        st.info("從 BINANCE 下載歷史數據中...")
        loader.download(symbol, start_date, end_date, exchange_type="binance")
```

（`get_date_range` 回傳型別以 `backtest/data_loader.py:313` 實際實作為準——回 `None` 或 `(start, end)`；日期比較前先統一成 `YYYY-MM-DD` 字串。注意 `DataLoader.download` 簽名是 `(symbol, start_date, end_date, interval, exchange_type)`，**沒有 ccxt_symbol 參數**，內部自行轉換。）

`manager.load_data(symbol, start_date, end_date)` → `loader.load(symbol, start_date, end_date)`。

- [ ] **Step 3: 單次回測改接**

`result = manager.run_backtest(sym_config, df)`（~256）改：

```python
        result = backtest_service.run_single_backtest(sym_config, df)
```

回傳 view dict 的 key 見 Task 3 Interfaces。對照頁內 render 函數實際取用的 key（`grep -n 'result\[' "web/pages/3_🔬_回測優化.py"`），舊 dict key 與新 view key 不同名者（例如舊「收益率」中文 key 或 `total_return` 類）在 render 處改名對齊——以 view dict 的 key 為準單向改頁面，不要在 service 層做別名。

- [ ] **Step 4: 優化段改接**

傳統網格分支（~394）：

```python
        results_df = backtest_service.run_grid_optimization(
            sym_config, df, progress_callback=update_progress)
```

智能優化分支：頁內既有 `run_smart_optimization`（~401-430，直接 new SmartOptimizer）整段改為呼叫 service：

```python
        trading_mode = config_store.get_symbol_extra(
            ccxt_symbol, "trading_mode", default="swing")
        results_df = backtest_service.run_smart_optimization(
            sym_config, df, n_trials=n_trials, objective=objective,
            trading_mode=trading_mode, progress_callback=update_progress_smart)
```

（smart 的 progress callback 是 `(current, total, best_value)` 三參數，頁面原有 callback 簽名照舊。）兩分支現在都回同構 DataFrame，結果渲染統一吃 `results_df`；`best_params/param_importance` 從 `results_df.attrs` 取（僅 smart 有 attrs，網格搜尋的 best 用 `results_df.iloc[0]`——GridOptimizer.run 已按 metric 排序）。

UI 已有 `use_smart` 選項（現況就有智能/傳統之分）——保留，只把兩條執行路徑換成 service。

- [ ] **Step 5: Monte Carlo 段驗證（不改碼）**

929-1013 行已用新 GridBacktester。唯一要查：它建 Config 的地方若沒帶 multiplier/initial_quantity，改用 `backtest_service.to_backtest_config(sym_config)`。看實際 code 決定，改動最小化。

- [ ] **Step 6: 驗證 + commit**

```bash
grep -n "BacktestManager\|config.models\|get_backtest_manager" "web/pages/3_🔬_回測優化.py"
```
Expected: 零筆。
Run: `uv run pytest tests/ -q` → 290 passed。
手動：起 streamlit 走頁3——選一個已配置交易對、載入短日期區間（資料已在 asBack/data 的區間，避免真下載）、跑單次回測出結果、網格優化跑 2x2 小組合、智能優化跑 3 trials。

```bash
git add "web/pages/3_🔬_回測優化.py"
git commit -m "refactor: #9 頁3 回測頁改接 DataLoader+backtest_service，雙優化器歸一渲染"
```

---

### Task 7: 頁4 設定改 Binance 專用

**Files:**
- Modify: `web/pages/4_🛠️_設定.py`

- [ ] **Step 1: 移除交易所選擇 UI**

`:37` 起的「交易所選擇」段（`list_supported_exchanges` selectbox）整段刪除，換成固定顯示：

```python
    st.markdown("**交易所**")
    st.info("🏦 Binance USDⓈ-M Futures（生產引擎唯一支援）")
```

`config.exchange_type` 引用一併移除（grid_engine.GlobalConfig 用 `exchange_id`，預設 "binance"，頁面不需要編輯它）。

- [ ] **Step 2: 改寫 API 驗證函數**

`verify_and_save_api` / `test_api_connection` 兩個函數的 adapter 路徑換 ccxt 直連（與 grid_engine 同路）。共用 helper：

```python
def _binance_client(api_key: str, api_secret: str):
    import ccxt
    return ccxt.binance({
        "apiKey": api_key,
        "secret": api_secret,
        "options": {"defaultType": "future"},
    })


def verify_and_save_api(api_key: str, api_secret: str) -> bool:
    """驗證 Binance API 連線，成功返回 True"""
    try:
        with st.spinner("🔄 驗證 Binance API 連線..."):
            exchange = _binance_client(api_key, api_secret)
            exchange.load_markets()
            balance = exchange.fetch_balance()
            try:
                exchange.fetch_positions()
                futures_ok = True
            except Exception:
                futures_ok = False

        st.success("✅ Binance API 驗證成功!")
        totals = balance.get("total", {}) or {}
        balance_info = [f"{c}: {totals[c]:.4f}"
                        for c in ("USDC", "USDT", "BTC", "ETH")
                        if totals.get(c, 0) > 0]
        if balance_info:
            st.info(f"💰 餘額: {' | '.join(balance_info[:3])}")
        if futures_ok:
            st.success("✅ 期貨交易權限正常")
        else:
            st.warning("⚠️ 無期貨交易權限，請確認 API 設定")
        return True
    except Exception as e:
        error_msg = str(e)
        st.error(f"❌ API 驗證失敗: {error_msg}")
        if "Invalid API" in error_msg or "invalid" in error_msg.lower():
            st.warning("💡 建議: 請檢查 API Key 和 Secret 是否正確")
        elif "permission" in error_msg.lower() or "403" in error_msg:
            st.warning("💡 建議: 請確認 API 有期貨交易權限")
        elif "IP" in error_msg:
            st.warning("💡 建議: 請確認當前 IP 在 API 白名單中")
        elif "timestamp" in error_msg.lower() or "time" in error_msg.lower():
            st.warning("💡 建議: 請確認系統時間是否正確")
        return False
```

呼叫端拿掉 `exchange_type`/`password`（Bitget passphrase）參數與相關輸入框。舊 adapter 回傳 `bal.wallet_balance` 物件、ccxt 回傳 dict——餘額顯示段照上面 code 用 `balance["total"]`。`test_api_connection` 同樣模式改寫（或若與 verify 重複度高就合併成一個，呼叫端統一）。

- [ ] **Step 3: 驗證 + commit**

```bash
grep -n "from exchanges\|import exchanges\|exchange_type\|get_adapter" "web/pages/4_🛠️_設定.py"
```
Expected: 零筆。
Run: `uv run pytest tests/ -q` → 290 passed。
手動：頁4 載入、不填 key 按驗證走到錯誤路徑顯示友善訊息。

```bash
git add "web/pages/4_🛠️_設定.py"
git commit -m "refactor: #9 頁4 Binance 專用 — 砍多交易所 UI，測連線改 ccxt 直連"
```

---

### Task 8: 新舊引擎成本歸零對比（Phase 2 前必跑）

**Files:**
- Create: `scripts/compare_backtest_engines.py`

**目的**：舊 `BacktestManager` 刪掉後就沒有對照組了。成本歸零對齊後比純網格邏輯，殘餘量級差才能歸因到參數映射 bug。這是一次性驗證 script，跑完留報告，Phase 2 後 script 保留（對照功能自然失效，檔頭註明）。

- [ ] **Step 1: 寫對比 script**

```python
# scripts/compare_backtest_engines.py
"""新舊回測引擎對比（#9 遷移一次性驗證）。

成本歸零對齊（fee/滑價/funding/hard_stop 全關）後，
同一 symbol+日期跑舊 core.backtest 與新 backtest.GridBacktester，
比純網格邏輯的收益率/回撤量級。差一個數量級 = 參數映射 bug。

注意：Phase 2 刪 core/ 後本 script 的舊引擎路徑失效，僅留存歷史。
用法: uv run python scripts/compare_backtest_engines.py XRPUSDC 2026-06-01 2026-06-07
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main(symbol: str, start: str, end: str):
    from web.services import config_store, backtest_service
    from backtest.data_loader import DataLoader

    config = config_store.load_config()
    sym_config = next(
        (s for s in config.symbols.values() if s.symbol == symbol), None)
    if sym_config is None:
        print(f"config 中找不到 {symbol}")
        sys.exit(1)

    loader = DataLoader()
    df = loader.load(symbol, start, end)
    if df is None or df.empty:
        print(f"無數據: {symbol} {start}~{end}（先在頁3 下載）")
        sys.exit(1)
    print(f"載入 {len(df):,} 條 K 線")

    # --- 成本對齊策略 ---
    # 舊引擎 fee 寫死每邊 0.0004（core/backtest.py:230，無法參數關閉）；
    # 新引擎每邊收 fee_pct/2（backtester.py:282）。
    # → fee 用「對齊」：新引擎 fee_pct=0.0008 ⇒ 每邊 0.0004 = 舊引擎。
    # 滑價/funding/hard_stop 兩邊皆可關 → 全關。

    # --- 新引擎 ---
    from backtest.backtester import GridBacktester
    new_cfg = backtest_service.to_backtest_config(sym_config, zero_costs=True)
    new_cfg.fee_pct = 0.0008  # 每邊 0.0004，對齊舊引擎寫死值
    new_result = GridBacktester(df, new_cfg).run()

    # --- 舊引擎（成本經由 run_backtest kwargs 關閉，core/backtest.py:194-198） ---
    from core.backtest import BacktestManager
    old_manager = BacktestManager()
    old_result = old_manager.run_backtest(
        sym_config, df,
        hard_stop_pct=1e9,    # 永不觸發
        slippage_pct=0.0,     # 關隨機滑價
        funding_rate=0.0,     # 關資金費率
    )

    print("\n=== 對比（成本歸零，純網格邏輯） ===")
    print(f"{'指標':<16}{'舊引擎':>14}{'新引擎':>14}")
    print(f"{'收益率%':<16}{old_result.get('return_pct', old_result.get('收益率', '?')):>14}"
          f"{new_result.return_pct:>14.4f}")
    print(f"{'最大回撤%':<16}{old_result.get('max_drawdown', old_result.get('最大回撤', '?')):>14}"
          f"{new_result.max_drawdown:>14.4f}")
    print(f"{'成交筆數':<16}{old_result.get('trades_count', '?'):>14}"
          f"{new_result.trades_count:>14}")
    print("\n判讀：方向一致、量級同階（比值 0.2x~5x 內）= PASS；"
          "差一個數量級以上 = 映射 bug，回頭查 to_backtest_config。")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1], sys.argv[2], sys.argv[3])
```

**實作注意**：舊 `run_backtest` 回傳 dict 的實際 key 以讀碼為準（`core/backtest.py` `run_backtest` 的 return 段），對比列印處的 `old_result.get(...)` fallback key 依實際調整。

- [ ] **Step 2: 實跑 + 記錄**

```bash
uv run python scripts/compare_backtest_engines.py XRPUSDC <start> <end>
```
（日期用 `asBack/data` 已有資料的區間，先 `ls asBack/data | head` 查。）
Expected: PASS 判讀。結果數字記入 `tasks/notes.md`（含跑的 symbol/區間/兩邊數字）。若 FAIL：停下修 `to_backtest_config`，不進 Phase 2。

- [ ] **Step 3: Commit**

```bash
git add scripts/compare_backtest_engines.py tasks/notes.md
git commit -m "test: #9 新舊引擎成本歸零對比 — 純網格邏輯量級一致性驗證"
```

---

### Task 9: Phase 2 — 刪除舊系統

**前置條件**：Task 1-8 全部完成、Task 8 對比 PASS。

- [ ] **Step 1: 逐項 grep 證零引用後刪除**

每刪一項前先跑對應 grep（排除 `__pycache__`、`asBack/`、`docs/`、`tasks/`），必須零筆才刪：

```bash
# core/
grep -rn "from core\|import core" --include="*.py" . | grep -v __pycache__ | grep -v asBack | grep -v scripts/compare_backtest_engines.py
# 預期僅剩 scripts/check_web_system.py（Task 10 處理）與 compare script（保留，已註明失效）
# → 若僅剩這兩處：先改 check_web_system（見 Task 10 Step 1 可提前做），或本步暫緩 core/ 到 Task 10 後
```

**刪除順序**（依賴淺→深）：
```bash
git rm main.py
git rm -r ui/
git rm scripts/check_symbol_conversion.py
# exchanges/ 前置：grep -rn "from exchanges\|import exchanges" --include="*.py" . | grep -v __pycache__ | grep -v asBack → 應僅剩 core/ 內部引用
git rm -r exchanges/
# core/ 最後刪（check_web_system.py 先在 Task 10 Step 1 改掉，或把該步提前到此處）
git rm -r core/
```

實務順序建議：先做 Task 10 Step 1（check_web_system 改寫）再回來刪 core/，或把兩個 task 合成一個 PR 內的連續 commit——執行者自行排序，但**每個 commit 點 repo 必須 import 得起來**（`uv run python -c "import web.state, web.services.backtest_service"` + `uv run pytest tests/ -q` 綠）。

- [ ] **Step 2: 全套回歸**

Run: `uv run pytest tests/ -q` → Expected: 290 passed（tests/ 零依賴舊系統，數字不變）

- [ ] **Step 3: Commit**

```bash
git add -u  # 僅已 git rm 的路徑；確認 git status 無其他變更混入
git commit -m "refactor: #9 Phase2 刪除舊系統 — core/ ui/ exchanges/ main.py check_symbol_conversion"
```

---

### Task 10: Phase 2 — config 瘦身與周邊清理

**Files:**
- Modify: `scripts/check_web_system.py`、`config/models.py`、`coin_selection/ws_provider.py`、`coin_selection/symbol_scanner.py`、`README.md`

- [ ] **Step 1: check_web_system.py 改寫檢查項**

`:73,225` 的 `from core.bot import MaxGridBot` 檢查項改為 `from grid_engine.config import GlobalConfig` + `from backtest.data_loader import DataLoader`；`:118` 的 exchanges 檢查項改為 `import ccxt; ccxt.binance`。維持「45 項健檢」的結構，只換被刪模組的項目，改完實跑：

```bash
uv run python scripts/check_web_system.py
```
Expected: pass 數 ≥ 前次基線 37（新系統 import 都在，理應更高）。

- [ ] **Step 2: config/models.py 瘦身**

刪 class：`SymbolConfig`、`RiskConfig`、`GlobalConfig`、`SymbolState`、`AccountBalance`、`GlobalState`。
留：`SerializableMixin`、`MaxEnhancement`、`BanditConfig`、`DGTConfig`、`LeadingIndicatorConfig`（indicators/ 4 檔在用）。
刪前證明：

```bash
grep -rn "from config.models import\|from config import models" --include="*.py" . | grep -v __pycache__ | grep -v asBack
# 預期僅剩 indicators/{dgt,funding,leading,bandit}.py 且只 import 保留的 class
```

刪後檔頭 docstring 更新為「indicators 專用 config（原全域 config 已由 grid_engine/config.py 取代）」。

- [ ] **Step 3: coin_selection 死碼清理**

`ws_provider.py:36-45` 與 `symbol_scanner.py:34-40` 附近的 `try: from core.logging_setup ...` 分支：core.logging_setup 等模組已不存在，try 分支恆 fail——刪掉 try/except 結構，把 except 分支的 fallback 內容轉正（行為不變）。改完：

```bash
uv run python -c "import coin_selection.ws_provider, coin_selection.symbol_scanner"
```
Expected: 無錯誤。

- [ ] **Step 4: README 更新**

啟動方式移除 `main.py` 舊入口，保留 `as_terminal_max.py`（新引擎）與 `streamlit run web/app.py`。頁1 描述若提到「即時監控」改為「歷史檢視」。

- [ ] **Step 5: 終驗 + commit**

```bash
grep -rn "from core\|import core\b\|from exchanges\|from ui\.\|from config.models import GlobalConfig\|from config.models import SymbolConfig" --include="*.py" . | grep -v __pycache__ | grep -v asBack | grep -v scripts/compare_backtest_engines.py
```
Expected: 零筆。
Run: `uv run pytest tests/ -q` → 290 passed。

```bash
git add scripts/check_web_system.py config/models.py coin_selection/ws_provider.py coin_selection/symbol_scanner.py README.md
git commit -m "refactor: #9 Phase2 收尾 — config/models 瘦身、coin_selection 死碼清理、健檢/README 更新"
```

---

### Task 11: 終端驗收 — Playwright 實點 + Monkey Testing

**前置**：Task 1-10 完成。本 task 不寫產品碼，只驗收與修驗收發現的 bug。

- [ ] **Step 1: 全套回歸基線**

Run: `uv run pytest tests/ -q` → 290 passed（實際數字報進 notes）

- [ ] **Step 2: Playwright 實點四頁**（hard-reload 後實點，不靠 code inspection）

起 `uv run streamlit run web/app.py`，用 Playwright MCP 逐頁：
1. 首頁：載入、引擎狀態卡顯示、無 traceback。
2. 頁1：歷史資料載入（logs/decisions.jsonl 存在時圖表有料）、選交易對切換。
3. 頁2：新增測試交易對 → 檢查 JSON（新 symbol 進檔 + 既有 trading_mode 保留）→ 編輯 → 刪除還原。
4. 頁3：載資料（用 asBack/data 已有區間）→ 單次回測出結果 → 網格優化小組合 → 智能優化 3 trials → Monte Carlo 段跑通。
5. 頁4：無 key 按驗證 → 友善錯誤不噴 traceback。

- [ ] **Step 3: Monkey testing（把它玩壞）**

- 頁3：日期範圍顛倒（end < start）、無資料的 symbol、trials=0、日期填未來。
- 頁1：`logs/decisions.jsonl` 暫時 mv 走（檔案不存在路徑）、塞一行垃圾再載入、整檔清空。測完還原。
- 頁2：手動改 JSON 刪掉某 symbol 的欄位再開頁面、把 initial_quantity 改成 0 後去頁3 跑回測（應得 ValueError 的友善顯示，不是 traceback）。
- 目標：所有情境顯示友善錯誤。發現的 crash 修完重驗（st.error + early return 模式，不吞 log）。

- [ ] **Step 4: 驗收派工（不自我驗收）**

派 fresh-context verifier subagent：重讀關鍵檔（web/services/ 三模組 + state.py）、實跑 `uv run pytest tests/ -q`、實跑 `uv run python scripts/check_web_system.py`、抽查 grep 零引用宣稱。回報 ACCEPT/REJECT。
之後走 `/dual-review` 兩輪（reviewer subagent + 外部輪 fresh-context subagent fallback）。

- [ ] **Step 5: 收尾**

修完 review findings → 全套綠 → 更新 `tasks/progress.md`（#9 完成、備份檔 `.bak-pre-web-migration` 位置、遺留任務：hard_stop 生產實作、trading_mode 收編 engine schema）→ commit。

---

## Red Team（實作前防禦檢查，Plan track 核心邏輯適用）

攻擊向量（≤5）與計畫內防禦：
1. **並發寫 config**：web 存檔瞬間引擎/另一頁也在寫 → merge-preserve 以 raw 為底降低覆蓋面；Streamlit 單使用者場景接受殘餘風險，備份檔保底。
2. **decisions.jsonl 寫入中讀到截斷行** → history_reader 逐行容錯（test_load_decisions_skips_corrupt_lines）。
3. **惡意/異常 config JSON**（手改、欄位型別錯）→ grid_engine from_dict 有 _parse_* fallback；頁面層 ValueError 顯示 st.error。
4. **initial_quantity=0 觸發 legacy 回測路徑給錯結論** → to_backtest_config 直接 raise（test_rejects_zero_quantity）。
5. **API key 在頁4 驗證時外洩到 log** → ccxt client 只在函數作用域、錯誤訊息只顯示 exception message 不 echo key。
