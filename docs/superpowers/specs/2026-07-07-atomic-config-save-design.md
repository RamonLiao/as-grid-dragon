# #10-A：`GlobalConfig.save()` 原子寫 + merge-preserve + 跨進程鎖

**日期**：2026-07-07
**Scope**：只做 A（config 寫入 I/O 安全）。B（grid_engine hard_stop 實作）另開獨立 cycle。
**Review 修訂（量化工程師 review）**：加入 F1 tmp 唯一化、F2 fcntl.flock 跨進程序列化；措辭區分「撕裂讀」與「lost-update」。

## 問題

`grid_engine/config.py:237` 的 `GlobalConfig.save()`：

```python
def save(self):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(self.to_dict(), f, indent=2)
    console.print("[green]配置已保存[/]")
```

系統是**雙進程並發寫同一個 `config/trading_config_max.json`**：`as_terminal_max.py`（live bot + 互動選單）與 web streamlit 進程。三個缺陷：

1. **非原子（撕裂讀）**：`open(CONFIG_FILE, 'w')` 先截斷再寫。寫入途中 crash / disk-full → 截斷檔，其他進程讀到壞檔。
2. **抹掉 extras**：`to_dict()` 只 emit engine schema 欄位。web 寫入的 `symbols[].trading_mode`（頁3 優化器用）等未知欄位被終端 `save()` 覆寫抹掉。
3. **lost-update（併發 RMW）**：merge-preserve 是 read-modify-write。兩進程各自「讀快照 → 改 → 寫」跨進程非原子 → 後寫者用舊快照覆蓋先寫者的改動。

已查證事實：
- 全 repo **無任何跨進程檔鎖**（`grep flock/fcntl/FileLock` 零）。`grid_engine/locks.py`、`sync_service.py` 只有進程內 `asyncio.Lock()`，對跨進程無效。commit 1b2dd59 的「同步防護」是 mtime 樂觀檢查，只縮小不消除 lost-update 窗口。
- `save()` 只在互動選單被呼叫（`setup_*`/`add/edit/delete/toggle_symbol`/`coin_selection_menu`），grid_engine 的 live async loop **零** `save()` 呼叫 → 損毀檔 raise 不會殺 live trading。
- `as_terminal_max.py` 20 個 `self.config.save()` + `scripts/check_web_system.py:108` 全走這條缺陷路徑。
- `web/services/config_store.py:save_config` 已有 merge-preserve + os.replace 原子寫，但（a）只守 web 側，（b）tmp 檔名固定 `...json.tmp` → 併發時兩進程撞同一 tmp，原子性在多寫者場景**失效**（潛伏 bug），（c）無鎖，同樣有 lost-update 窗口。

## 目標

中央修 `GlobalConfig.save()` 一處 → 全部呼叫點 + live engine 受惠。merge-preserve + 原子寫 + 鎖三者**合一單一真相**，web 與 engine 共用（避免兩份 drift；本 repo 有 core/ vs grid_engine 重複 class 造成 bug 前例）。

**明確區分**：
- `os.replace` 給「可見性原子」→ 防**撕裂讀**。
- `fcntl.flock(LOCK_EX)` 包住整個 RMW → 防**lost-update**（跨進程互斥）。
- 兩者缺一不可，各解一個問題。

## 架構

helper 落在下層 `grid_engine`（web/services 匯入 grid_engine，反向依賴不允許）。於 `grid_engine/config.py`（或新 `grid_engine/config_io.py`）加：

### 1. `load_raw(path) -> dict`
讀 JSON，缺檔回 `{}`。既有檔為 invalid JSON → 讓 `json.load` **raise**（fail loud，不 silent fallback 到 `{}` 以免丟失未知 key）。

### 2. `merge_preserve(raw, new, symbol_extras=None) -> dict`
欄位級 merge，即現行 `config_store.save_config` 那份原樣下沉：
- top-level：`raw` 有、`new` 沒有的 key 原樣保留。
- `symbols`：以 `new["symbols"]`（= `config.symbols`）為準——新增進檔、config 已刪的 symbol 移除；每 symbol 內 `raw` 有、`new` 沒有的 key（如 `trading_mode`）保留。
- 其他巢狀 dict（如 `risk`）：欄位級 merge。
- `symbol_extras`（`{ccxt_symbol: {key: value}}`）：顯式覆寫，套在 merge 之後。

### 3. `_config_lock(path)`（contextmanager）— 跨進程鎖
sidecar `.lock` 檔（**不** flock config 檔本身：`os.replace` 換 inode 會使鎖失效）：
```python
@contextmanager
def _config_lock(path):
    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)   # 阻塞式，寫檔期間持鎖
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
```
advisory lock，兩個 writer 都取 → 互斥。`.lock` sidecar 常駐 0-byte（不刪，刪會 race）→ 加 `.gitignore`。

### 4. `_atomic_write_json(path, data)`
```python
tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")   # F1: pid 唯一化
try:
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
finally:
    if tmp.exists():
        tmp.unlink(missing_ok=True)
```
`ensure_ascii=False` 與 config_store 統一（prod config 現純 ASCII，零 diff）。

### 5. `merge_preserve_save(path, new, symbol_extras=None, ensure_backup=False)` — 對外主入口
把 RMW 全段包進鎖：
```python
def merge_preserve_save(path, new, symbol_extras=None, ensure_backup=False):
    with _config_lock(path):
        merged = merge_preserve(load_raw(path), new, symbol_extras)
        if ensure_backup:
            _ensure_backup(path)
        _atomic_write_json(path, merged)
```

## 改動點

### `grid_engine/config.py`
```python
def save(self):
    merge_preserve_save(CONFIG_FILE, self.to_dict())
    console.print("[green]配置已保存[/]")
```
（損毀檔 raise 的友善包裝為 minor，見錯誤處理表；可在此 catch。）

### `web/services/config_store.py`
改為 delegate：
- `load_raw` → grid_engine 的。
- `save_config(config, symbol_extras, path)` → `merge_preserve_save(_resolve(path), config.to_dict(), symbol_extras, ensure_backup=True)`。
- **保留** `path`/`get_mtime`/`get_symbol_extra` wrapper。`_ensure_backup` 邏輯下沉或由 `ensure_backup=True` 觸發。
- 兩份 merge + 原子寫 + tmp 合一。

## 資料流與正確性

- **撕裂讀**：`os.replace` 同檔系統原子 → 任何進程讀 CONFIG_FILE 永不見截斷檔。
- **lost-update**：兩 writer 都經 `merge_preserve_save` → `flock(LOCK_EX)` 序列化「讀 raw → merge → 寫」整段 → 後者讀到的是前者已寫入的最新檔 → web 的 `trading_mode` 改動不再被終端舊快照覆寫。
- **tmp 碰撞**：flock 序列化後單 writer 進臨界區，固定 tmp 已安全；pid 唯一化為 defense-in-depth（crash 殘留 tmp 不互撞、不被誤 replace）。
- readers（`load`）不需鎖：os.replace 已保證讀到一致快照。flock 只約束 writer。
- 與 1b2dd59 的 mtime 檢查正交：flock 管寫入互斥，mtime 管「外部更新→UI reload」訊號，不衝突。

## 錯誤處理

| 情境 | 行為 |
|------|------|
| 既有檔缺失 | `load_raw` 回 `{}`，merge 退化成純 `to_dict()`，正常寫入 |
| 既有檔 invalid JSON | `load_raw` raise，save 中止，**原檔不動**；終端可 catch 後印友善錯誤（minor） |
| tmp 寫入 / fsync 失敗 | 例外在 `os.replace` 前，原檔完好；`finally` 清 tmp；鎖 `finally` 釋放 |
| disk full | 同上，原子性保原檔 |
| 持鎖進程 crash | OS 自動釋放 flock（進程結束即解），無死鎖殘留 |

## 測試

新增（`tests/test_config_save.py`，全走 tmp path / monkeypatch，**不碰真實 config**）：
- 保留未知 top-level key。
- 保留 symbol 內未知 key（`trading_mode`）。
- config 刪除的 symbol → 存檔後消失。
- 已知 engine 欄位被 `to_dict()` 覆寫。
- save 後無 `.tmp.<pid>` 殘留，檔為 valid JSON。
- monkeypatch `json.dump` raise → 原檔不變、`os.replace` 未執行、無 tmp 殘留。
- 損毀既有檔 → save raise，原檔不截斷（monkey）。
- **併發（F1+F2 核心）**：`multiprocessing` 起 N 進程，各對不同 top-level key / 不同 symbol 做 `merge_preserve_save`，斷言：(a) 所有 key 全存活（無 lost-update）；(b) 每次讀檔皆 valid JSON（無撕裂/tmp 碰撞）。

回歸：`tests/web/test_config_store.py` 全綠（行為不變）。

## 其他

- `.gitignore` 加 `config/*.lock`、`config/*.tmp.*`（sidecar 與殘留 tmp）。
- 目錄 fsync（rename 的 crash durability）：config 非 DB，YAGNI，不做。
- 平台：fcntl.flock 於 macOS（本機）+ Linux（GCE docker）皆支援；本地/掛載卷非 NFS，advisory lock 有效。

## Out of scope（明確）

- **B（hard_stop 實作）**：`RiskConfig` 無 hard_stop 概念、`RiskMonitor` 無硬止損強制——另開完整 brainstorm + plan + backtest cycle。
- 20 個終端呼叫點程式碼不動（中央修覆蓋）。
- `scripts/compare_backtest_engines.py` 死 core import（plan 明定保留歷史）。

## 安全前提

live engine（as_terminal_max.py）本機常駐、讀寫同一 config 檔。實作與測試期間：測試一律走 tmp path / monkeypatch，**絕不寫真實 `config/trading_config_max.json`**。
