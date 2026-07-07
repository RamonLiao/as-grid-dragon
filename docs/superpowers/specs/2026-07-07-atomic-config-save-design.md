# #10-A：`GlobalConfig.save()` 原子寫 + merge-preserve

**日期**：2026-07-07
**Scope**：只做 A（I/O 安全）。B（grid_engine hard_stop 實作）另開獨立 cycle。

## 問題

`grid_engine/config.py:237` 的 `GlobalConfig.save()`：

```python
def save(self):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(self.to_dict(), f, indent=2)
    console.print("[green]配置已保存[/]")
```

兩個缺陷，直接影響正在跑的 live engine（讀同一個 `config/trading_config_max.json`）：

1. **非原子**：`open(CONFIG_FILE, 'w')` 先截斷再寫。寫入途中 crash / disk-full → 檔案截斷，live engine 下次讀取拿到壞檔。
2. **抹掉 extras**：`to_dict()` 只 emit engine schema 認識的欄位。web 側寫入 `symbols[].trading_mode`（頁3 優化器用）等未知欄位，被終端 `save()` 直接覆寫抹掉。

`as_terminal_max.py` 有 20 個 `self.config.save()` 呼叫點 + `scripts/check_web_system.py:108`，全部走這條有缺陷的路徑。

web 側 `web/services/config_store.py:save_config` 已有正確的 merge-preserve + 原子寫實作，但只守 web 這一側。

## 目標

中央修 `GlobalConfig.save()` 一處 → 全部呼叫點 + live engine 受惠。merge-preserve 邏輯與 web 側**合一單一真相**（避免兩份 drift；本 repo 有 core/ vs grid_engine 重複 class 造成 bug 的前例）。

## 架構

helper 落在下層 `grid_engine`（web/services 匯入 grid_engine，反向依賴不允許）。於 `grid_engine/config.py` 加三個 module-level function：

### 1. `load_raw(path) -> dict`
讀 JSON，缺檔回 `{}`。既有檔為 invalid JSON → 讓 `json.load` **raise**（fail loud，不 silent fallback 到 `{}` 以免丟失未知 key）。與 config_store 現行行為一致。

### 2. `merge_preserve(raw, new, symbol_extras=None) -> dict`
欄位級 merge，邏輯即現行 `config_store.save_config` 那份原樣下沉：
- top-level：`raw` 有、`new` 沒有的 key 原樣保留。
- `symbols`：以 `new["symbols"]`（= `config.symbols`）為準——新增進檔、config 已刪的 symbol 移除；每個 symbol 內 `raw` 有、`new` 沒有的 key（如 `trading_mode`）保留。
- 其他巢狀 dict（如 `risk`）：欄位級 merge。
- `symbol_extras`（`{ccxt_symbol: {key: value}}`）：顯式覆寫，套在 merge 之後。

### 3. `atomic_write_json(path, data)`
```python
tmp = path.with_suffix(path.suffix + ".tmp")
try:
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
finally:
    if tmp.exists():
        tmp.unlink(missing_ok=True)  # 寫入/replace 失敗時清 tmp 殘留
```
`ensure_ascii=False` 與 config_store 統一（prod config 現為純 ASCII，零 diff）。

## 改動點

### `grid_engine/config.py`
`GlobalConfig.save()` 重寫：
```python
def save(self):
    merged = merge_preserve(load_raw(CONFIG_FILE), self.to_dict())
    atomic_write_json(CONFIG_FILE, merged)
    console.print("[green]配置已保存[/]")
```

### `web/services/config_store.py`
改為 delegate 到 grid_engine helper：
- `load_raw` → 呼叫 grid_engine 的（或 re-export）。
- `save_config` 核心 → `merge_preserve(raw, config.to_dict(), symbol_extras)` + `atomic_write_json`。
- **保留** 自己的 `_ensure_backup`（`.bak-pre-web-migration`）、`path` 參數、`get_mtime`、`get_symbol_extra` wrapper。
- 兩份 merge 邏輯合一。

## 資料流

終端選單 20 呼叫點 + `check_web_system.py` → `config.save()` → 讀當前檔 merge → 原子 replace。`os.replace` 同檔系統原子 → live engine 讀 CONFIG_FILE 永不見截斷檔。web 寫入的 `trading_mode` 等 extras 不再被終端 save 二次抹掉。

## 錯誤處理

| 情境 | 行為 |
|------|------|
| 既有檔缺失 | `load_raw` 回 `{}`，merge 退化成純 `to_dict()`，正常寫入 |
| 既有檔 invalid JSON | `load_raw` raise，save 中止，**原檔不動**（不 silent 覆寫丟 key） |
| tmp 寫入 / fsync 失敗 | 例外發生在 `os.replace` 前，原檔完好；`finally` 清 tmp |
| disk full | 同上，原子性保原檔 |

## 測試

新增 engine save 測試（`tests/test_config_save.py`，全走 tmp path / monkeypatch，**不碰真實 config**）：
- 保留未知 top-level key。
- 保留 symbol 內未知 key（`trading_mode`）。
- config 刪除的 symbol → 存檔後消失。
- 已知 engine 欄位被 `to_dict()` 覆寫。
- save 後無 `.tmp` 殘留，檔為 valid JSON。
- monkeypatch `json.dump` raise → 原檔內容不變、`os.replace` 未執行、無 tmp 殘留。
- 既有檔損毀 → save raise，原檔不截斷（monkey）。

回歸：`tests/web/test_config_store.py` 全綠（行為不變）。

## Out of scope（明確）

- **B（hard_stop 實作）**：`RiskConfig` 無 hard_stop 概念、`RiskMonitor` 無硬止損強制——另開完整 brainstorm + plan + backtest cycle。
- 20 個終端呼叫點程式碼不動（中央修覆蓋）。
- `scripts/compare_backtest_engines.py` 的死 core import（plan 明定保留歷史）。

## 安全前提

live engine（as_terminal_max.py）本機常駐、讀同一 config 檔。實作與測試期間：測試一律走 tmp path / monkeypatch，**絕不寫真實 `config/trading_config_max.json`**。
