# #10-A 原子寫 + merge-preserve + 跨進程鎖 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 config 寫入的 merge-preserve、原子寫、跨進程鎖合一到 grid_engine 下層 helper，修好 `GlobalConfig.save()` 的撕裂讀/抹 extras/lost-update 三缺陷，web config_store delegate 共用。

**Architecture:** 新增 `grid_engine/config_io.py`（純 stdlib，零 grid_engine 內部依賴）提供 `merge_preserve_save()`；`GlobalConfig.save()` 與 `web/services/config_store.save_config()` 皆 delegate。鎖用 sidecar `.lock` 檔 + `fcntl.flock(LOCK_EX)` 包住整個 read-modify-write；原子寫用 pid 唯一化 tmp + `os.fsync` + `os.replace`。

**Tech Stack:** Python 3.13、pytest、fcntl、multiprocessing、uv。

## Global Constraints

- **絕不寫真實 `config/trading_config_max.json`**：live engine（as_terminal_max.py）常駐讀寫此檔。所有測試走 `tmp_path`。
- 缺套件用 `uv`。跑測試用 `uv run pytest`。
- Git staging 只 stage 明確指定檔案，禁止 `git add -A`/`git add .`。
- `ensure_ascii=False`、`indent=2` 為 JSON 寫入格式（與現行 config_store 一致）。
- 保留現行 `web/services/config_store.py` 對外介面：`load_raw / load_config / get_mtime / get_symbol_extra / save_config / BACKUP_SUFFIX`。
- 平台：fcntl.flock 於 macOS + Linux 皆用；本地/掛載卷非 NFS。

---

### Task 1: `grid_engine/config_io.py` — 共用底層 helper

**Files:**
- Create: `grid_engine/config_io.py`
- Create: `tests/test_config_io.py`
- Modify: `config/.gitignore`（不存在則 Create）

**Interfaces:**
- Produces:
  - `load_raw(path) -> dict`：缺檔回 `{}`；invalid JSON 讓 `json.load` raise。
  - `merge_preserve(raw: dict, new: dict, symbol_extras: dict | None = None) -> dict`
  - `_config_lock(path)`：contextmanager，`fcntl.flock(LOCK_EX)` on sidecar `<name>.lock`。
  - `_atomic_write_json(path, data: dict) -> None`：pid 唯一化 tmp + fsync + os.replace。
  - `_ensure_backup(path) -> None`：首次備份 `<name>.bak-pre-web-migration`。
  - `merge_preserve_save(path, new: dict, symbol_extras=None, ensure_backup=False) -> None`：鎖內 RMW 主入口。
  - 常數 `BACKUP_SUFFIX = ".bak-pre-web-migration"`、`LOCK_SUFFIX = ".lock"`。

- [ ] **Step 1: 寫失敗測試**

`tests/test_config_io.py`：

```python
"""config_io 共用底層測試（merge-preserve + 原子寫）。全走 tmp_path，不碰真實 config。"""
import json
import os
from pathlib import Path

import pytest

from grid_engine import config_io


def _write(p, data):
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def test_load_raw_missing_returns_empty(tmp_path):
    assert config_io.load_raw(tmp_path / "nope.json") == {}


def test_load_raw_corrupt_raises(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        config_io.load_raw(p)


def test_merge_preserves_unknown_top_level():
    raw = {"exchange_type": "binance", "api_key": "old"}
    new = {"api_key": "new"}
    merged = config_io.merge_preserve(raw, new)
    assert merged["exchange_type"] == "binance"  # 未知 top-level 保留
    assert merged["api_key"] == "new"            # 已知欄位覆寫


def test_merge_preserves_symbol_unknown_key():
    raw = {"symbols": {"X/USDC:USDC": {"leverage": 20, "trading_mode": "swing"}}}
    new = {"symbols": {"X/USDC:USDC": {"leverage": 25}}}
    merged = config_io.merge_preserve(raw, new)
    sym = merged["symbols"]["X/USDC:USDC"]
    assert sym["leverage"] == 25             # engine 欄位覆寫
    assert sym["trading_mode"] == "swing"    # 未知欄位保留


def test_merge_drops_removed_symbol():
    raw = {"symbols": {"A/USDC:USDC": {"leverage": 1}, "B/USDC:USDC": {"leverage": 2}}}
    new = {"symbols": {"A/USDC:USDC": {"leverage": 1}}}
    merged = config_io.merge_preserve(raw, new)
    assert "B/USDC:USDC" not in merged["symbols"]  # config 已刪的消失


def test_merge_nested_dict_field_level():
    raw = {"risk": {"enabled": True, "hard_stop_enabled": True, "max_loss_pct": 0.1}}
    new = {"risk": {"enabled": False}}
    merged = config_io.merge_preserve(raw, new)
    assert merged["risk"]["enabled"] is False        # 覆寫
    assert merged["risk"]["hard_stop_enabled"] is True  # 未知保留
    assert merged["risk"]["max_loss_pct"] == 0.1


def test_symbol_extras_overlay():
    raw = {"symbols": {"X/USDC:USDC": {"leverage": 20}}}
    new = {"symbols": {"X/USDC:USDC": {"leverage": 20}}}
    merged = config_io.merge_preserve(
        raw, new, symbol_extras={"X/USDC:USDC": {"trading_mode": "high_freq"}})
    assert merged["symbols"]["X/USDC:USDC"]["trading_mode"] == "high_freq"


def test_atomic_write_no_tmp_residue(tmp_path):
    p = tmp_path / "trading_config_max.json"
    config_io._atomic_write_json(p, {"a": 1})
    assert json.loads(p.read_text()) == {"a": 1}
    residue = list(tmp_path.glob("trading_config_max.json.tmp*"))
    assert residue == [], f"tmp 殘留: {residue}"


def test_atomic_write_failure_keeps_original(tmp_path, monkeypatch):
    p = tmp_path / "trading_config_max.json"
    _write(p, {"orig": True})

    def boom(*a, **k):
        raise IOError("disk full")
    monkeypatch.setattr(config_io.json, "dump", boom)
    with pytest.raises(IOError):
        config_io._atomic_write_json(p, {"new": True})

    assert json.loads(p.read_text()) == {"orig": True}          # 原檔完好
    assert list(tmp_path.glob("*.tmp*")) == []                   # tmp 清乾淨


def test_merge_preserve_save_first_time_creates(tmp_path):
    p = tmp_path / "trading_config_max.json"
    config_io.merge_preserve_save(p, {"api_key": "k", "symbols": {}})
    assert json.loads(p.read_text())["api_key"] == "k"


def test_merge_preserve_save_preserves_and_backs_up(tmp_path):
    p = tmp_path / "trading_config_max.json"
    _write(p, {"exchange_type": "binance",
               "symbols": {"X/USDC:USDC": {"leverage": 20, "trading_mode": "swing"}}})
    config_io.merge_preserve_save(
        p, {"symbols": {"X/USDC:USDC": {"leverage": 25}}}, ensure_backup=True)
    raw = json.loads(p.read_text())
    assert raw["symbols"]["X/USDC:USDC"]["leverage"] == 25
    assert raw["symbols"]["X/USDC:USDC"]["trading_mode"] == "swing"
    assert raw["exchange_type"] == "binance"
    bak = p.with_name(p.name + config_io.BACKUP_SUFFIX)
    assert json.loads(bak.read_text())["symbols"]["X/USDC:USDC"]["leverage"] == 20


def test_ensure_backup_once(tmp_path):
    p = tmp_path / "trading_config_max.json"
    _write(p, {"v": 1})
    config_io.merge_preserve_save(p, {"v": 2}, ensure_backup=True)
    config_io.merge_preserve_save(p, {"v": 3}, ensure_backup=True)
    bak = p.with_name(p.name + config_io.BACKUP_SUFFIX)
    assert json.loads(bak.read_text())["v"] == 1  # 備份只建一次，維持首版
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_config_io.py -v`
Expected: FAIL（`ModuleNotFoundError: grid_engine.config_io`）

- [ ] **Step 3: 實作 `grid_engine/config_io.py`**

```python
"""Config 讀寫共用底層：merge-preserve + 原子寫 + 跨進程鎖。

單一真相：grid_engine.GlobalConfig.save() 與 web.services.config_store 皆 delegate。
為什麼下沉到 grid_engine：web/services 匯入 grid_engine（下層），反向依賴不允許。

原子性與併發：
- os.replace 給「可見性原子」→ 防撕裂讀。
- fcntl.flock(LOCK_EX) 包住整個 read-modify-write → 防跨進程 lost-update。
- tmp 檔 pid 唯一化 → crash 殘留不互撞、不被誤 replace（flock 之外的 defense-in-depth）。
"""
import fcntl
import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

BACKUP_SUFFIX = ".bak-pre-web-migration"
LOCK_SUFFIX = ".lock"


def load_raw(path) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)  # invalid JSON → 故意 raise（fail loud，不丟未知 key）


def merge_preserve(raw: dict, new: dict,
                   symbol_extras: Optional[dict] = None) -> dict:
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
    return merged


@contextmanager
def _config_lock(path):
    """sidecar .lock 檔上的跨進程獨佔鎖。不鎖 config 本體：os.replace 換 inode 會使鎖失效。"""
    p = Path(path)
    lock_path = p.with_name(p.name + LOCK_SUFFIX)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # 阻塞式，寫檔期間持鎖
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _atomic_write_json(path, data: dict) -> None:
    p = Path(path)
    tmp = p.with_name(f"{p.name}.tmp.{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _ensure_backup(path) -> None:
    p = Path(path)
    if not p.exists():
        return
    bak = p.with_name(p.name + BACKUP_SUFFIX)
    if not bak.exists():
        shutil.copy(p, bak)


def merge_preserve_save(path, new: dict,
                        symbol_extras: Optional[dict] = None,
                        ensure_backup: bool = False) -> None:
    """鎖內 RMW 主入口：flock → 讀 raw → merge → (backup) → 原子寫。"""
    p = Path(path)
    with _config_lock(p):
        merged = merge_preserve(load_raw(p), new, symbol_extras)
        if ensure_backup:
            _ensure_backup(p)
        _atomic_write_json(p, merged)
```

- [ ] **Step 4: 建 `config/.gitignore`（擋 sidecar 與殘留 tmp）**

`config/.gitignore` 追加（不存在則建）：

```
*.lock
*.tmp.*
```

- [ ] **Step 5: 跑測試確認通過**

Run: `uv run pytest tests/test_config_io.py -v`
Expected: PASS（12 passed）

- [ ] **Step 6: Commit**

```bash
git add grid_engine/config_io.py tests/test_config_io.py config/.gitignore
git commit -m "feat: #10-A config_io 共用底層 — merge-preserve + 原子寫(pid tmp) + flock 跨進程鎖"
```

---

### Task 2: 併發正確性測試（F1 tmp 碰撞 + F2 lost-update 的核心回歸）

**Files:**
- Create: `tests/test_config_io_concurrency.py`

**Interfaces:**
- Consumes: `config_io.merge_preserve_save`（Task 1）。

- [ ] **Step 1: 寫併發測試**

`tests/test_config_io_concurrency.py`：

```python
"""併發寫入正確性：多進程同時 merge_preserve_save，斷言無 lost-update、無撕裂讀。

為什麼重要：這是 F1(tmp 碰撞)/F2(lost-update) 唯一測得到的層級。移除 flock 或
改回固定 tmp 名，此測試即 fail；單進程 unit test 測不出併發缺陷。
"""
import json
import os
from pathlib import Path

import pytest

from grid_engine import config_io

N_PROCS = 5
ITERS = 30


def _worker(path_str, key):
    path = Path(path_str)
    for _ in range(ITERS):
        config_io.merge_preserve_save(path, {key: os.getpid()})
        # 每次寫後立即讀，撕裂讀/tmp 碰撞會讓 json.load raise → 進程非 0 退出
        with open(path, encoding="utf-8") as f:
            json.load(f)


def test_concurrent_writers_no_lost_update_no_torn(tmp_path):
    import multiprocessing as mp
    path = tmp_path / "trading_config_max.json"
    path.write_text(json.dumps({"base": 1}), encoding="utf-8")

    keys = [f"k{i}" for i in range(N_PROCS)]
    ctx = mp.get_context("spawn")  # 跨平台一致（macOS 預設即 spawn）
    procs = [ctx.Process(target=_worker, args=(str(path), k)) for k in keys]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)

    for p in procs:
        assert p.exitcode == 0, "worker 撞到撕裂讀/tmp 碰撞（json.load 失敗）"

    final = json.loads(path.read_text())
    assert final["base"] == 1                      # 原 key 不丟
    for k in keys:                                  # 每個 worker 的 key 都在 → 無 lost-update
        assert k in final, f"lost update: {k} 遺失"
```

- [ ] **Step 2: 跑測試確認通過**

Run: `uv run pytest tests/test_config_io_concurrency.py -v`
Expected: PASS（1 passed；約數秒）

- [ ] **Step 3: 反向驗證 flock 有效（手動，非留檔）**

暫時把 `config_io.py` 的 `fcntl.flock(fd, fcntl.LOCK_EX)` 註解掉，重跑上面測試，Expected: FAIL（`lost update: kN 遺失` 或 exitcode 非 0）。確認後**還原** flock。此步驗證測試真的抓得到缺陷，不 commit 任何改動。

- [ ] **Step 4: Commit**

```bash
git add tests/test_config_io_concurrency.py
git commit -m "test: #10-A 併發寫入正確性 — 多進程無 lost-update/無撕裂讀（flock+pid-tmp 回歸守門）"
```

---

### Task 3: `GlobalConfig.save()` delegate 到 config_io

**Files:**
- Modify: `grid_engine/config.py:237-240`（`save()`）、import 區
- Create: `tests/test_config_save.py`

**Interfaces:**
- Consumes: `config_io.merge_preserve_save`（Task 1）。
- `save()` 簽名不變（無參數，寫 `CONFIG_FILE`）。

- [ ] **Step 1: 寫失敗測試**

`tests/test_config_save.py`：

```python
"""GlobalConfig.save() delegate 正確性。monkeypatch CONFIG_FILE，不碰真實 config。"""
import json

from grid_engine.config import GlobalConfig, SymbolConfig


def _seed(path):
    path.write_text(json.dumps({
        "api_key": "k",
        "exchange_type": "binance",              # engine 不認識的舊欄位
        "symbols": {"XRP/USDC:USDC": {
            "symbol": "XRPUSDC", "ccxt_symbol": "XRP/USDC:USDC",
            "leverage": 20, "trading_mode": "swing",  # engine 不認識
        }},
    }, indent=2), encoding="utf-8")


def test_save_preserves_unknown_and_atomic(tmp_path, monkeypatch):
    cfg = tmp_path / "trading_config_max.json"
    _seed(cfg)
    monkeypatch.setattr("grid_engine.config.CONFIG_FILE", cfg)

    config = GlobalConfig.load()               # 讀 CONFIG_FILE(=tmp)
    config.symbols["XRP/USDC:USDC"].leverage = 25
    config.save()

    raw = json.loads(cfg.read_text())
    assert raw["symbols"]["XRP/USDC:USDC"]["leverage"] == 25           # 編輯生效
    assert raw["symbols"]["XRP/USDC:USDC"]["trading_mode"] == "swing"   # 未知欄位保留
    assert raw["exchange_type"] == "binance"                            # top-level 保留
    assert list(tmp_path.glob("trading_config_max.json.tmp*")) == []    # 無 tmp 殘留


def test_save_drops_removed_symbol(tmp_path, monkeypatch):
    cfg = tmp_path / "trading_config_max.json"
    _seed(cfg)
    monkeypatch.setattr("grid_engine.config.CONFIG_FILE", cfg)
    config = GlobalConfig.load()
    config.symbols["BNB/USDC:USDC"] = SymbolConfig(
        symbol="BNBUSDC", ccxt_symbol="BNB/USDC:USDC")
    del config.symbols["XRP/USDC:USDC"]
    config.save()
    raw = json.loads(cfg.read_text())
    assert "BNB/USDC:USDC" in raw["symbols"]
    assert "XRP/USDC:USDC" not in raw["symbols"]
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_config_save.py -v`
Expected: FAIL（`test_save_preserves_unknown_and_atomic`：現行 `save()` 抹掉 `exchange_type`/`trading_mode` → assert 掛）

- [ ] **Step 3: 改 `grid_engine/config.py`**

import 區（`from .utils import CONFIG_FILE, console` 之後）加：

```python
from .config_io import merge_preserve_save
```

`save()`（原 237-240）改為：

```python
    def save(self):
        merge_preserve_save(CONFIG_FILE, self.to_dict())
        console.print("[green]配置已保存[/]")
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_config_save.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: Commit**

```bash
git add grid_engine/config.py tests/test_config_save.py
git commit -m "fix: #10-A GlobalConfig.save() delegate config_io — 原子+merge-preserve+鎖（除撕裂讀/抹extras/lost-update）"
```

---

### Task 4: `web/services/config_store.py` delegate（兩份合一）

**Files:**
- Modify: `web/services/config_store.py`
- Modify: `tests/web/test_config_store.py`（`test_save_atomic_write_no_tmp_residue` 改 glob）

**Interfaces:**
- Consumes: `config_io.{load_raw, merge_preserve_save, BACKUP_SUFFIX}`（Task 1）。
- 對外介面不變：`load_raw / load_config / get_mtime / get_symbol_extra / save_config / BACKUP_SUFFIX`。

- [ ] **Step 1: 先改回歸測試（tmp 殘留檢查改 glob）**

`tests/web/test_config_store.py` 的 `test_save_atomic_write_no_tmp_residue`，把：

```python
    # 驗證無 .tmp 殘留
    tmp_file = cfg_file.with_suffix(cfg_file.suffix + ".tmp")
    assert not tmp_file.exists(), f"tmp 檔殘留：{tmp_file}"
```

改為：

```python
    # 驗證無 .tmp 殘留（pid 唯一化後用 glob）
    residue = list(cfg_file.parent.glob(cfg_file.name + ".tmp*"))
    assert residue == [], f"tmp 檔殘留：{residue}"
```

- [ ] **Step 2: 改 `web/services/config_store.py` delegate**

保留檔頭 docstring。改為（`_resolve` / `load_config` / `get_mtime` / `get_symbol_extra` 保留原樣，僅換掉 `load_raw` / `save_config`、刪本地 `_ensure_backup` / merge / 原子寫）：

```python
from pathlib import Path
from typing import Optional

from grid_engine.config import GlobalConfig
from grid_engine.utils import CONFIG_FILE
from grid_engine import config_io
from grid_engine.config_io import BACKUP_SUFFIX  # 對外相容 re-export


def _resolve(path: Optional[Path]) -> Path:
    return Path(path) if path is not None else CONFIG_FILE


def load_raw(path: Optional[Path] = None) -> dict:
    return config_io.load_raw(_resolve(path))


def load_config(path: Optional[Path] = None) -> GlobalConfig:
    return GlobalConfig.from_dict(load_raw(path))


def get_mtime(path: Optional[Path] = None) -> int:
    p = _resolve(path)
    if not p.exists():
        return 0
    return p.stat().st_mtime_ns


def get_symbol_extra(ccxt_symbol: str, key: str, default=None,
                     path: Optional[Path] = None):
    raw = load_raw(path)
    return raw.get("symbols", {}).get(ccxt_symbol, {}).get(key, default)


def save_config(config: GlobalConfig,
                symbol_extras: Optional[dict] = None,
                path: Optional[Path] = None) -> None:
    """merge-preserve + 原子寫 + 跨進程鎖 存檔（delegate config_io，單一真相）。

    首次存檔前建一次性 .bak-pre-web-migration 備份。symbol_extras
    {ccxt_symbol: {key: value}} 顯式覆寫（頁2 編輯 trading_mode 用）。
    """
    config_io.merge_preserve_save(
        _resolve(path), config.to_dict(),
        symbol_extras=symbol_extras, ensure_backup=True)
```

（`get_mtime` 保留原 docstring；此處省略只為精簡，實作時保留原註解。）

- [ ] **Step 3: 跑 config_store 回歸 + config_io 全套**

Run: `uv run pytest tests/web/test_config_store.py tests/test_config_io.py -v`
Expected: PASS（config_store 9 passed + config_io 12 passed）

- [ ] **Step 4: Commit**

```bash
git add web/services/config_store.py tests/web/test_config_store.py
git commit -m "refactor: #10-A config_store delegate config_io — 兩份 merge/原子寫/tmp 合一，順修固定tmp潛伏bug"
```

---

### Task 5: 全套回歸 + Monkey Testing + 最終驗收

**Files:**
- （無新增，驗證與收尾）

- [ ] **Step 1: ps-check live engine 仍在跑（確認測試沒誤傷）**

Run: `ps aux | grep as_terminal_max | grep -v grep`
Expected: as_terminal_max.py 進程仍在（PID 可能不同）；確認全程未寫真實 config。

- [ ] **Step 2: 全套測試**

Run: `uv run pytest -q`
Expected: 全綠（原 294 + 新增 config_io 12 + concurrency 1 + config_save 2 = 309 passed；數字以實跑為準）

- [ ] **Step 3: Monkey — 真實 config round-trip 零欄位遺失（唯讀，複製到 tmp）**

Run:
```bash
uv run python -c "
import json, shutil, tempfile
from pathlib import Path
from grid_engine.config import GlobalConfig
import grid_engine.config as C
real = Path('config/trading_config_max.json')
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / 'trading_config_max.json'
    shutil.copy(real, p)
    before = json.loads(p.read_text())
    C.CONFIG_FILE = p
    GlobalConfig.load().save()
    after = json.loads(p.read_text())
    def keys(d, pre=''):
        s = set()
        for k, v in d.items():
            s.add(pre+k)
            if isinstance(v, dict): s |= keys(v, pre+k+'.')
        return s
    missing = keys(before) - keys(after)
    print('MISSING:', missing)
    assert missing == set(), missing
    print('OK: round-trip 零欄位遺失')
"
```
Expected: `MISSING: set()` + `OK: round-trip 零欄位遺失`。**注意**：此腳本複製 real 到 tmp、只改記憶體 `C.CONFIG_FILE`，不寫回 `config/`。

- [ ] **Step 4: Monkey — 損毀既有檔 save raise 不截斷**

Run:
```bash
uv run python -c "
import json, tempfile
from pathlib import Path
from grid_engine import config_io
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / 'trading_config_max.json'
    p.write_text('{corrupt', encoding='utf-8')
    try:
        config_io.merge_preserve_save(p, {'a': 1})
        print('FAIL: 應 raise'); raise SystemExit(1)
    except json.JSONDecodeError:
        assert p.read_text() == '{corrupt'  # 原檔不動
        print('OK: 損毀檔 raise 且原檔不截斷')
"
```
Expected: `OK: 損毀檔 raise 且原檔不截斷`

- [ ] **Step 5: 更新 progress + Commit**

更新 `tasks/progress.md`（#10-A 完成摘要、剩 B follow-up），然後：

```bash
git add tasks/progress.md
git commit -m "docs: #10-A 完成 — config 寫入原子/merge/鎖三缺陷修復，全套 green"
```

- [ ] **Step 6: 交付驗收（dual-review 前）**

派 fresh-context verifier subagent：重讀 `grid_engine/config_io.py`、`grid_engine/config.py:save`、`web/services/config_store.py`，實跑 `uv run pytest -q` + Step 3/4 monkey，回報 ACCEPT/REJECT + 檔案:行號，不吃實作者自述。

---

## 交付後（不在本 plan 的 task 內，但務必執行）

- **dual-review**（兩輪制）：R1 外部獨立 review（codex quota 耗盡 → fresh-context subagent）、R2 project skills/rules review。整合 → 修完才算完成。
- **B（hard_stop 實作）**：另開獨立 brainstorm + plan + backtest cycle。
