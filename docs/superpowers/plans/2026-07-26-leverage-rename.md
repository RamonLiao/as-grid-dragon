# `leverage` → `assumed_leverage` 改名與舊 key 清除 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `SymbolConfig.leverage` 改名為 `assumed_leverage`，並確保 config 檔內的舊 key 被實際移除，不留下第二個假旋鈕。

**Architecture:** 三段。先在 `config_io.merge_preserve` 加一個獨立的 `drop_symbol_keys` 最終 pass（純函式、離線可測、與改名無關）。再一次性完成改名：欄位、`to_dict`/`from_dict` 相容分支、`__getattr__`/`__setattr__` 舊名攔截、以及全部 19 個生產存取點與 5 個測試存取點——**攔截與改名必須同一個 commit**，因為攔截正是用來讓漏改在測試期爆炸的機制。最後把 `drop_symbol_keys={"leverage"}` 接進兩個 save 路徑並修正一條會被刻意打紅的既有守衛測試。

**Tech Stack:** Python 3、dataclasses、pytest、`uv`（缺套件用 `uv`）。無新依賴。

## Global Constraints

以下逐條抄自 spec（`docs/superpowers/specs/2026-07-26-leverage-rename-design.md`），**每個 task 的要求都隱含包含本節**：

- **零交易所互動。** 不呼叫任何 exchange API、不下單、不重啟引擎。
- **不改任何行為。** `assumed_leverage` 的值與型別等同今天的 `leverage`（預設 `20`，`int`）。回測收到的數值不變。
- **不改 `backtest/config.py:Config.leverage`** —— 那是回測引擎的真旋鈕，名副其實。`backtest/`、`scripts/` 全部不動。
- **不改下單 / 決策 / 風控邏輯。**
- **生產 config 保護**：`config/trading_config_max.json` 是**執行中實盤引擎**（pid 31471）正在讀寫的檔案。任何測試觸碰 config 存檔路徑者，**必須** `monkeypatch.setattr("grid_engine.config.CONFIG_FILE", tmp)` 或傳 `path=tmp`。禁止在測試中寫生產檔。
- **不寫 `logs/`、`log/`。** 暫存檔一律放 `$(mktemp -d)` 或 pytest 的 `tmp_path`。
- **git 只 stage 明確指定的檔案**（`git add <file>...`），禁止 `git add -A` / `git add .`。
- **`__getattr__` / `__setattr__` 只能拋 `AttributeError`**，不得拋其他型別（`copy`/`pickle` 會對實例做 `getattr(obj, '__deepcopy__', None)` 之類的探測，拋非 `AttributeError` 會炸掉無關路徑）。
- **禁止對 config 欄位使用帶 default 的 `getattr`**（`getattr(cfg, "leverage", 20)` 會繞過攔截靜默取預設值）。`grid_engine/backtest.py:151` 已有 `getattr(config, "direction", "both")` 的同型先例，**不得擴散**。

---

## File Structure

| 檔案 | 責任 | Task |
|---|---|---|
| `grid_engine/config_io.py` | 加 `drop_symbol_keys` 參數與最終 pass | 1 |
| `tests/test_config_io.py` | `drop_symbol_keys` 的行為、位置、純度驗收 | 1 |
| `grid_engine/config.py` | `SymbolConfig` 欄位改名、`to_dict`/`from_dict`、舊名攔截 | 2 |
| `tests/test_symbol_config_rename.py` | **新檔**：改名與攔截的全部驗收 | 2 |
| `grid_engine/backtest.py`、`web/services/backtest_service.py`、`web/pages/1,2,3`、`as_terminal_max.py` | 19 個存取點改名 + UI label | 2 |
| `tests/test_config_save.py`、`tests/web/test_config_store.py`、`tests/web/test_backtest_service.py` | 5 個測試存取點改名 | 2 |
| `grid_engine/config.py:255`、`web/services/config_store.py:59` | 接上 `drop_symbol_keys={"leverage"}` | 3 |
| `tests/web/test_config_store.py` | round-trip 守衛白名單化 | 3 |

---

### Task 1: `config_io.merge_preserve` 支援 `drop_symbol_keys`

**Files:**
- Modify: `grid_engine/config_io.py:42-65`（`merge_preserve`）、`:105-114`（`merge_preserve_save`）
- Test: `tests/test_config_io.py`（在檔案末尾追加）

**Interfaces:**
- Consumes: 無（本 task 為起點）
- Produces:
  - `merge_preserve(raw: dict, new: dict, symbol_extras: Optional[dict] = None, drop_symbol_keys: Optional[set] = None) -> dict`
  - `merge_preserve_save(path, new: dict, symbol_extras: Optional[dict] = None, ensure_backup: bool = False, drop_symbol_keys: Optional[set] = None) -> None`
  - 語意：`drop_symbol_keys` 內的 key 自每個 symbol dict 中移除，**drop 永遠勝出**（在 `symbol_extras` 之後執行）。

**背景（實作者必讀）**：`merge_preserve` 目前的結構是——`merged = dict(raw)`（`:44`）→ shallow-copy 每個 symbol dict（`:45-47`）→ 走 `new` 的迴圈（`:48-61`）→ 套用 `symbol_extras`（`:62-64`）→ `return merged`（`:65`）。

`drop` 必須實作為**對 `merged["symbols"]` 的獨立最終 pass，插在 `:64` 之後、`:65` 之前**。兩個錯誤位置各有一條靜默失效路徑：
- 寫進 symbol 分支（`:48-55`）內 → 該分支只在 `new` 含 `"symbols"` key 時執行，`new` 不含 symbols 的呼叫會讓 drop 靜默不發生。
- 寫在 `symbol_extras`（`:62-64`）之前 → `symbol_extras` 的 `update` 會把剛刪掉的 key 再塞回去。

- [ ] **Step 1: 寫四條失敗測試**

追加到 `tests/test_config_io.py` 末尾：

```python
def test_drop_symbol_keys_removes_key():
    raw = {"symbols": {"X/USDC:USDC": {"leverage": 20, "assumed_leverage": 20}}}
    new = {"symbols": {"X/USDC:USDC": {"assumed_leverage": 20}}}
    merged = config_io.merge_preserve(raw, new, drop_symbol_keys={"leverage"})
    sym = merged["symbols"]["X/USDC:USDC"]
    assert "leverage" not in sym
    assert sym["assumed_leverage"] == 20


def test_drop_symbol_keys_applies_when_new_has_no_symbols_key():
    """drop 必須是獨立最終 pass：new 不含 symbols 時仍要生效。
    若把 drop 寫進 symbol 分支內，本測試會紅。"""
    raw = {"symbols": {"X/USDC:USDC": {"leverage": 20}}, "api_key": "old"}
    new = {"api_key": "new"}
    merged = config_io.merge_preserve(raw, new, drop_symbol_keys={"leverage"})
    assert "leverage" not in merged["symbols"]["X/USDC:USDC"]
    assert merged["api_key"] == "new"


def test_drop_symbol_keys_wins_over_symbol_extras():
    """drop 必須在 symbol_extras 之後：否則 extras 會把 key 塞回來。"""
    raw = {"symbols": {"X/USDC:USDC": {"leverage": 20}}}
    new = {"symbols": {"X/USDC:USDC": {"assumed_leverage": 20}}}
    merged = config_io.merge_preserve(
        raw, new,
        symbol_extras={"X/USDC:USDC": {"leverage": 99, "trading_mode": "swing"}},
        drop_symbol_keys={"leverage"})
    sym = merged["symbols"]["X/USDC:USDC"]
    assert "leverage" not in sym
    assert sym["trading_mode"] == "swing"   # 其他 extras 不受影響


def test_drop_symbol_keys_does_not_mutate_raw():
    """純度：呼叫端的 raw 不得被改動（天真實作直接迭代 raw['symbols'] 會踩到）。"""
    raw = {"symbols": {"X/USDC:USDC": {"leverage": 20}}}
    new = {"symbols": {"X/USDC:USDC": {"assumed_leverage": 20}}}
    config_io.merge_preserve(raw, new, drop_symbol_keys={"leverage"})
    assert raw["symbols"]["X/USDC:USDC"]["leverage"] == 20
```

- [ ] **Step 2: 跑測試確認四條全紅**

Run: `uv run pytest tests/test_config_io.py -k drop_symbol_keys -v`
Expected: 4 FAILED，錯誤為 `TypeError: merge_preserve() got an unexpected keyword argument 'drop_symbol_keys'`

- [ ] **Step 3: 實作**

`grid_engine/config_io.py`，改簽名並在 `return merged` 之前插入最終 pass：

```python
def merge_preserve(raw: dict, new: dict,
                   symbol_extras: Optional[dict] = None,
                   drop_symbol_keys: Optional[set] = None) -> dict:
```

在既有的 `symbol_extras` 迴圈（`for sym_key, extras in (symbol_extras or {}).items():` ...）**之後**、`return merged` **之前**加入：

```python
    # 一次性遷移：舊 key 清除。獨立最終 pass —— 必須在 symbol_extras 之後
    # （extras 會把刪掉的 key 塞回來），且不得寫進上面的 symbol 分支內
    # （該分支只在 new 含 "symbols" 時執行）。drop 永遠勝出。
    if drop_symbol_keys:
        for sym_key, sym in merged.get("symbols", {}).items():
            for k in drop_symbol_keys:
                sym.pop(k, None)
```

`merge_preserve_save` 同步透傳：

```python
def merge_preserve_save(path, new: dict,
                        symbol_extras: Optional[dict] = None,
                        ensure_backup: bool = False,
                        drop_symbol_keys: Optional[set] = None) -> None:
    """鎖內 RMW 主入口：flock → 讀 raw → merge → (backup) → 原子寫。"""
    p = Path(path)
    with _config_lock(p):
        merged = merge_preserve(load_raw(p), new, symbol_extras, drop_symbol_keys)
        if ensure_backup:
            _ensure_backup(p)
        _atomic_write_json(p, merged)
```

**注意**：`merged["symbols"]` 的每個 value 在 `:46` 與 `:53` 都已是新 dict，所以 `sym.pop` 不會 mutate 呼叫端的 `raw`——這正是純度測試要守的性質，不要「順手優化」成直接操作 `raw`。

- [ ] **Step 4: 跑測試確認四條全綠**

Run: `uv run pytest tests/test_config_io.py -k drop_symbol_keys -v`
Expected: 4 PASSED

- [ ] **Step 5: Mutation 驗證（三條，各自證明它抓的是什麼）**

依序做四個破壞、各跑一次、確認**指定的那條至少必紅**，然後還原。
（註明溢出項——並非每條 mutation 都只紅一條，歸因看指定那條 + 紅燈集合的差集）：

1. 註解掉整段 `if drop_symbol_keys:` → `test_drop_symbol_keys_removes_key` 必紅
   （溢出：`applies_when_new_has_no_symbols` 與 `wins_over_symbol_extras` 也會紅；純度那條仍綠）
2. 把該段移到 `symbol_extras` 迴圈**之前** → `test_drop_symbol_keys_wins_over_symbol_extras` 必紅
   （**只紅這一條**，鑑別力最乾淨）
3. 把該段搬進 `if k == "symbols":` 分支內（改為對 `merged_symbols` 操作）→
   `test_drop_symbol_keys_applies_when_new_has_no_symbols_key` 必紅
   （溢出：`wins_over_symbol_extras` 也會紅，因為分支內執行等於落在 extras 之前。
   與 mut 2 的差別在紅燈數量：mut2 紅 1 條、mut3 紅 2 條）
4. **純度守衛**：把迴圈改成 `for sym in raw.get("symbols", {}).values():` →
   `test_drop_symbol_keys_does_not_mutate_raw` 必紅
   （沒有這條，純度測試從未在真實缺陷前紅過——`config_io.py:46` 的無條件
   shallow-copy 讓天真實作也會通過，那等於一條會執行的註解）
   （溢出：`removes_key` 也會紅——`:46`/`:52` 的 copy 早於最終 pass，
   pop 到 `raw` 就等於沒 pop 到 `merged`）

Run（每次破壞後）：`uv run pytest tests/test_config_io.py -k drop_symbol_keys -v`
還原後再跑一次確認 4 PASSED。

- [ ] **Step 6: 回歸——既有測試零改動仍全綠**

Run: `uv run pytest tests/test_config_io.py tests/test_config_io_concurrency.py -v`
Expected: 全部 PASSED，且**未修改任何既有測試的斷言**（`drop_symbol_keys` 預設 `None` ⇒ 行為與改動前完全相同）。

- [ ] **Step 7: Commit**

```bash
git add grid_engine/config_io.py tests/test_config_io.py
git commit -m "feat(config_io): merge_preserve 支援 drop_symbol_keys 一次性遷移

獨立最終 pass（在 symbol_extras 之後），drop 永遠勝出。
三條 mutation 分別釘死：刪除生效、位置在 extras 之後、不依賴 new 含 symbols。
未傳參數時行為 bit-identical，既有測試零改動。"
```

---

### Task 2: `SymbolConfig` 改名 + 舊名攔截 + 全部存取點

**Files:**
- Modify: `grid_engine/config.py:39`（欄位）、`:74`（`to_dict`）、`:79-92`（`from_dict`）、新增 `__getattr__`/`__setattr__`
- Modify（生產存取點）：`grid_engine/backtest.py:146,173`、`web/services/backtest_service.py:43`、`web/pages/1_📈_交易監控.py:109`、`web/pages/2_⚙️_交易對管理.py:64,201,260,306`、`web/pages/3_🔬_回測優化.py:205,214,905`、`as_terminal_max.py:814,876,916,917,918,1078`
- Modify（測試存取點）：`tests/test_config_save.py:24`、`tests/web/test_config_store.py:61,108,142`、`tests/web/test_backtest_service.py:23`
- Create: `tests/test_symbol_config_rename.py`

**Interfaces:**
- Consumes: Task 1 的 `drop_symbol_keys`（本 task 不使用，Task 3 才接）
- Produces:
  - `SymbolConfig.assumed_leverage: int = 20`（取代 `leverage`）
  - `SymbolConfig.to_dict()` 輸出 key `"assumed_leverage"`，**不含** `"leverage"`
  - `SymbolConfig.from_dict(data)` 吃舊 key `"leverage"`；新舊並存時新 key 勝
  - 讀或寫 `cfg.leverage` 一律拋 `AttributeError`

**為何改名與攔截必須同一個 commit**：攔截正是用來讓「漏改的存取點」在測試期爆炸的機制。先改名後補攔截，中間那個狀態會讓漏改靜默降級。

**存取點完整清單（spec §5.1，已 grep 驗證）**：生產讀 12、寫 3、建構子 kwarg 4；測試寫 4、建構子 kwarg 1。
**不得改動**：`tests/test_optimizer_*.py` 各處、`tests/web/test_backtest_service.py:32`、`tests/test_config_io.py` 全部——那些是 `backtest.config.Config.leverage`（真旋鈕）或 raw dict 字面值。

- [ ] **Step 1: 先抓 A11 行為基準（改名前必做，之後無法重來）**

建立基準腳本並執行，把結果存到 scratchpad（**不是** repo）：

```python
# /tmp/<mktemp>/capture_baseline.py
import json, sys
# 絕對路徑，不依賴 cwd（Run 那步也已指定 cd，兩重保險）
sys.path.insert(0, "/Users/ramonliao/Documents/理財/加密貨幣/量化交易/LouisLab/as-grid-dragon")
from grid_engine.backtest import BacktestManager
from grid_engine.config import SymbolConfig
from web.services.backtest_service import to_backtest_config

# 關鍵：leverage 值刻意選 7（≠ 欄位預設 20）。
# 用 20 跑會自廢武功：相容分支若完全失效、值靜默落回預設 20，
# 結果會逐位元相同而測不出來。
RAW = {"symbol": "BNBUSDC", "ccxt_symbol": "BNB/USDC:USDC",
       "take_profit_spacing": 0.003, "grid_spacing": 0.003,
       "initial_quantity": 0.02, "leverage": 7,
       "limit_multiplier": 5.0, "threshold_multiplier": 40.0}

cfg = SymbolConfig.from_dict(dict(RAW))
mgr = BacktestManager()
df = mgr.load_data("BNBUSDC", "2026-06-06", "2026-06-08")
assert df is not None and len(df) > 0, "資料缺失，先確認 data/ 內有 06-06~06-08"

out = {
    "backtest_result": mgr.run_backtest(cfg, df),
    "web_config": to_backtest_config(SymbolConfig.from_dict(dict(RAW))).to_dict(),
}
print(json.dumps(out, sort_keys=True, indent=1))
```

Run:
```bash
cd "/Users/ramonliao/Documents/理財/加密貨幣/量化交易/LouisLab/as-grid-dragon"
BASE=$(mktemp -d)
uv run python /path/to/capture_baseline.py > "$BASE/baseline.json"
echo "$BASE"   # 記下這個路徑，Step 9 要用
```
Expected: 輸出合法 JSON，`backtest_result` 非空。記下 `$BASE`。

註：`backtest_result` 的 `profit_factor` / `sharpe_ratio` 可能是 `Infinity` 或 `NaN`，
`json.dumps` 會原樣輸出（非嚴格 JSON 但**決定性**），對 Step 9 的 `diff` 沒有影響。
看到這兩個值不要以為出錯。

- [ ] **Step 2: 寫失敗測試（新檔）**

`tests/test_symbol_config_rename.py`：

```python
"""assumed_leverage 改名與舊名攔截的驗收。

背景：leverage 是假旋鈕——實盤路徑不讀、從未推送交易所（實測 5x，config 寫 20），
唯一實效是餵回測。改名讓它在 grep 當下就自曝語意。
"""
import copy
import dataclasses

import pytest

from grid_engine.config import SymbolConfig


def test_from_dict_accepts_old_key():
    cfg = SymbolConfig.from_dict({"leverage": 5})
    assert cfg.assumed_leverage == 5


def test_from_dict_new_key_wins_over_old():
    cfg = SymbolConfig.from_dict({"assumed_leverage": 7, "leverage": 5})
    assert cfg.assumed_leverage == 7


def test_from_dict_default_when_absent():
    assert SymbolConfig.from_dict({}).assumed_leverage == 20


def test_to_dict_emits_new_key_only():
    d = SymbolConfig(assumed_leverage=5).to_dict()
    assert d["assumed_leverage"] == 5
    assert "leverage" not in d


def test_reading_old_name_raises():
    cfg = SymbolConfig()
    with pytest.raises(AttributeError, match="assumed_leverage"):
        _ = cfg.leverage


def test_writing_old_name_raises_and_does_not_pollute():
    """__getattr__ 只攔讀不攔寫。少了 __setattr__ 時 cfg.leverage = 20 會靜默
    建立實例屬性：之後讀取成功、to_dict() 忽略它 ⇒ UI 顯示成功、存檔沒有、
    回測沒用到 —— 正是本任務要消滅的假旋鈕的複刻。"""
    cfg = SymbolConfig(assumed_leverage=5)
    with pytest.raises(AttributeError, match="assumed_leverage"):
        cfg.leverage = 20
    assert "leverage" not in cfg.to_dict()
    assert cfg.assumed_leverage == 5


def test_constructor_rejects_old_kwarg():
    SymbolConfig(assumed_leverage=5)          # 不拋
    with pytest.raises(TypeError):
        SymbolConfig(leverage=5)


def test_other_missing_attributes_keep_native_behaviour():
    cfg = SymbolConfig()
    with pytest.raises(AttributeError, match="nonexistent_field"):
        _ = cfg.nonexistent_field


def test_legal_field_assignment_still_works():
    cfg = SymbolConfig()
    cfg.assumed_leverage = 9
    cfg.grid_spacing = 0.005
    assert cfg.assumed_leverage == 9 and cfg.grid_spacing == 0.005


def test_asdict_and_deepcopy_do_not_raise_non_attribute_errors():
    cfg = SymbolConfig(assumed_leverage=5)
    assert dataclasses.asdict(cfg)["assumed_leverage"] == 5
    assert copy.deepcopy(cfg).assumed_leverage == 5


def test_property_internal_error_is_not_rewritten_as_rename_message():
    """SymbolConfig 有 5 個 @property。若 property 內部拋 AttributeError，
    Python 會改而呼叫 __getattr__ —— fallback 若寫死改名訊息，會把
    「coin_name 出錯」誤報成「leverage 已改名」，指向完全錯誤的方向。

    已實測的 Python 語意（不要期待更多）：原始例外**完全丟棄**——
    外層只剩 fallback 訊息，`__context__` 與 `__cause__` 皆為 None
    （__getattr__ 由 slot 機制在 except 區塊之外呼叫，無隱式串接）。
    所以本測試只能斷言「沒有誤報成改名」+「有指出正確的屬性名」，
    **無法**斷言原始的 .split 錯誤還在。見 plan §誠實揭露第 4 點。
    """
    cfg = SymbolConfig()
    cfg.ccxt_symbol = 12345          # 非字串 → coin_name 的 .split 會炸
    with pytest.raises(AttributeError) as ei:
        _ = cfg.coin_name
    assert "assumed_leverage" not in str(ei.value)
    assert "coin_name" in str(ei.value)
```

- [ ] **Step 3: 跑測試確認全紅**

Run: `uv run pytest tests/test_symbol_config_rename.py -v`
Expected: 大部分 FAILED（`assumed_leverage` 尚不存在）。

- [ ] **Step 4: 改 `grid_engine/config.py`**

欄位（`:39`）：`leverage: int = 20` → `assumed_leverage: int = 20`

`to_dict()`（`:74`）：`"leverage": self.leverage,` → `"assumed_leverage": self.assumed_leverage,`

`from_dict()` 相容分支，加在既有 `position_limit` 分支之後、`return cls(...)` 之前（照抄既有 pattern）：

```python
        # 兼容舊 key：leverage → assumed_leverage（新 key 存在時新 key 勝）
        if "leverage" in data:
            if "assumed_leverage" not in data:
                data["assumed_leverage"] = data["leverage"]
            del data["leverage"]
```

在 `from_dict` 之後、`@property` 之前加入攔截：

```python
    _RENAMED = {
        "leverage": "assumed_leverage 已取代 leverage。此值不推送交易所"
                    "（實盤槓桿由交易所端設定），僅供回測使用。",
    }

    def __getattr__(self, name):
        # 只在正常查找失敗後才被呼叫。注意 @property 內部拋 AttributeError
        # 也會落到這裡，且原始例外會被完全丟棄（__context__ 為 None）——
        # fallback 必須帶上真正的屬性名，否則 coin_name 出錯會被誤報成
        # 「leverage 已改名」，把除錯導向完全錯誤的方向。
        if name in SymbolConfig._RENAMED:
            raise AttributeError(SymbolConfig._RENAMED[name])
        raise AttributeError(
            f"{type(self).__name__!r} object has no attribute {name!r}"
            f"（若 {name!r} 是 @property，其內部的 AttributeError 原文已被本"
            f" __getattr__ 遮蔽；用 type(obj).{name}.fget(obj) 直接呼叫可重現）")

    def __setattr__(self, name, value):
        # __getattr__ 只攔讀不攔寫；少了這個，cfg.leverage = 20 會靜默建立
        # 實例屬性（讀得到、to_dict() 忽略）＝ 假旋鈕復刻。
        if name in SymbolConfig._RENAMED:
            raise AttributeError(SymbolConfig._RENAMED[name])
        object.__setattr__(self, name, value)
```

- [ ] **Step 5: 跑新測試確認全綠**

Run: `uv run pytest tests/test_symbol_config_rename.py -v`
Expected: 11 PASSED

- [ ] **Step 6: 修全部存取點——但不要相信綠燈**

Run: `uv run pytest -x -q 2>&1 | tail -40`

> ### ⚠️ 測試全綠 **不等於** 改完
> `tests/` 內**沒有任何測試 import `as_terminal_max` 或 `web/pages/*`**（已 grep 實測）。
> 19 個生產存取點中，**只有 3 個會被測試抓到**：`grid_engine/backtest.py:146,173`、
> `web/services/backtest_service.py:43`。
> 其餘 **14 個（web/pages 8 + as_terminal_max 6）不在任何測試路徑上**——
> 漏改要到你在實盤終端或 web UI 操作時才會炸。
> 那 13 點只能靠**下面的清單逐點 read-back** + Step 8 的 grep 盤點來保證，
> **不得**以「pytest 全綠」為由跳過 Step 6b。

**Step 6a**：先修測試會爆的 3 點，跑到綠。

**Step 6b（不可跳過）**：對下列**每一個**行號開檔 read-back，確認已改名。逐點打勾：

生產：`grid_engine/backtest.py:146,173`、`web/services/backtest_service.py:43`、`web/pages/1:109`、`web/pages/2:64,201,260,306`、`web/pages/3:205,214,905`、`as_terminal_max.py:814,876,916,917,918,1078`
測試：`tests/test_config_save.py:24`、`tests/web/test_config_store.py:61,108,142`、`tests/web/test_backtest_service.py:23`

**測試裡的 raw dict 斷言也要跟著改**：例如 `tests/test_config_save.py:26` 的
`assert raw["symbols"][...]["leverage"] == 25` —— 存檔後 key 已是 `assumed_leverage`，
斷言不改會紅。`tests/web/test_config_store.py` 內同型斷言同理。
但**同檔案內作為「engine 不認識的舊欄位」種子資料的 `"leverage": 20` 字面值**
（如 `tests/test_config_save.py:12` 的 `_seed`）語意上仍是有效的舊 config 輸入，
改不改都可；改的話請一併確認該測試守的不變式（未知欄位保留）沒被破壞。

**反向提醒（不得改）**：
- `tests/test_optimizer_*.py`、`tests/web/test_backtest_service.py:32`、`backtest/` 全部——那些是 `backtest.config.Config.leverage`（真旋鈕）。
- `tests/web/test_config_store.py` 的 `test_save_creates_one_time_backup`（約 `:105,110`）對 **`.bak` 檔**的 `leverage == 20` 斷言**必須保持不變**——備份是存檔前的複本，保留舊 key 正是它該有的行為。
- `tests/test_config_io.py` 全部（那些 `"leverage"` 是泛用的「未知/engine 欄位」字面值，與本改名無關）。

**同時把區域變數也改名**（讓 Step 8 的 grep 白名單乾淨）：
`web/pages/2:133,256`、`web/pages/3:201`、`as_terminal_max.py:862` 的
`leverage = st.number_input(...)` / `IntPrompt.ask(...)` 及其下游 `leverage=leverage`
→ 一律改為 `assumed_leverage`。

- [ ] **Step 7: 改 UI 文案（揭露語意）**

- `web/pages/2_⚙️_交易對管理.py` **兩處** `st.number_input` label（`:133` 新增表單、`:256` 編輯表單）→ `"回測假設槓桿（不推送交易所）"`
- `web/pages/3_🔬_回測優化.py:201` 同上
- `as_terminal_max.py:917` 的 prompt → `f"回測假設槓桿（不推送交易所）[當前: {cfg.assumed_leverage}]"`
- `web/pages/1_📈_交易監控.py:109` → `st.write(f"- 槓桿（回測假設，非交易所實際）: {cfg.assumed_leverage}x")`
- `web/pages/2_⚙️_交易對管理.py:64`（唯讀顯示）→ `st.write(f"**槓桿（回測假設）:** {cfg.assumed_leverage}x")`
  （spec §5.2 未點名此處，但它與 p1:109 同為顯示假設值的地方，只改一邊會不一致）
  - **不移除這行**。TODO 4b 才有實測值可替換；在那之前移除是淨資訊損失。

- [ ] **Step 8: Mutation 驗證（三條）**

1. 註解掉 `__setattr__` 整個方法 → `test_writing_old_name_raises_and_does_not_pollute` 必紅，
   **且 `test_reading_old_name_raises` 必須仍綠**。
   **這條是本 task 最重要的 mutation**：綠/紅的對比證明測試抓的是**寫入**而非讀取。
2. 註解掉 `__getattr__` 整個方法 → `test_reading_old_name_raises` 必紅
   （spec A2 標 (M)，這條先前缺席）。
   溢出（已實測，非推測）：`test_property_internal_error_...` **也會紅**
   （原生訊息變成 `'int' object has no attribute 'split'`，不含 `coin_name`）；
   `test_other_missing_attributes_keep_native_behaviour` **保持綠**
   （原生訊息仍含 `nonexistent_field`）。歸因看 `test_reading_old_name_raises`。
3. 把 `__getattr__` 的 fallback 分支改成無條件拋改名訊息 →
   `test_other_missing_attributes_keep_native_behaviour` 與
   `test_property_internal_error_is_not_rewritten_as_rename_message` 必紅。

每次破壞後 Run: `uv run pytest tests/test_symbol_config_rename.py -v`；還原後確認 11 PASSED。

- [ ] **Step 9: A11 行為零變更驗收（bit-identical）**

用 Step 1 的同一支腳本重跑並比對——**但腳本內的 `RAW` 維持舊 key `"leverage": 7`**（正是要驗相容分支）：

```bash
uv run python /path/to/capture_baseline.py > "$BASE/after.json"
diff "$BASE/baseline.json" "$BASE/after.json" && echo "BIT-IDENTICAL ✅"
```
Expected: `diff` 無輸出、印出 `BIT-IDENTICAL ✅`。

若有差異 → **停下來查**，不要調整基準去遷就。最可能的原因是相容分支沒生效（值落回預設 20）。

- [ ] **Step 10: 全套測試**

Run: `uv run pytest -q 2>&1 | tail -5`
Expected: 全部 PASSED，**報實際數字**（例如 `536 passed`），不報形容詞。
注意：`tests/web/test_config_store.py::test_roundtrip_real_config_no_field_loss` 此時**仍應是綠的**（Task 3 才接上 drop）。若它現在就紅，代表有非預期的 key 遺失，停下來查。

- [ ] **Step 11: Commit**

```bash
git add grid_engine/config.py grid_engine/backtest.py \
        web/services/backtest_service.py \
        "web/pages/1_📈_交易監控.py" "web/pages/2_⚙️_交易對管理.py" "web/pages/3_🔬_回測優化.py" \
        as_terminal_max.py \
        tests/test_symbol_config_rename.py tests/test_config_save.py \
        tests/web/test_config_store.py tests/web/test_backtest_service.py
git commit -m "refactor(config): leverage → assumed_leverage，舊名讀寫一律爆炸

leverage 是假旋鈕：實盤路徑不讀、從未推送交易所（實測 5x vs config 20），
唯一實效是餵回測。改名讓語意在 grep 當下自曝。

__getattr__ + __setattr__ 雙向攔截 —— 只有 __getattr__ 的話
cfg.leverage = 20 會靜默建實例屬性（讀得到、to_dict 忽略）＝ 假旋鈕復刻。
from_dict 相容舊 key，新舊並存時新 key 勝。
A11 bit-identical 回歸以 leverage:7 驗證（用預設值 20 跑會自廢武功）。"
```

---

### Task 3: 接上兩個 save 路徑 + round-trip 守衛白名單化

**Files:**
- Modify: `grid_engine/config.py:255`（`GlobalConfig.save`）
- Modify: `web/services/config_store.py:59`（`save_config`）
- Modify: `tests/web/test_config_store.py:113-136`（`test_roundtrip_real_config_no_field_loss`）
- Test: `tests/test_config_save.py`、`tests/web/test_config_store.py`

**Interfaces:**
- Consumes: Task 1 的 `merge_preserve_save(..., drop_symbol_keys=...)`；Task 2 的 `assumed_leverage`
- Produces: 兩個 save 路徑存檔後，symbol dict 不再含 `"leverage"`

**漏傳參數是本 task 最可能的實作疏漏，兩個路徑各驗一次。**

- [ ] **Step 1: 寫兩條失敗測試**

追加到 `tests/test_config_save.py`：

```python
def test_engine_save_drops_legacy_leverage_key(tmp_path, monkeypatch):
    """GlobalConfig.save() 路徑：舊 key 必須被實際移除。

    config.py:255 硬寫 CONFIG_FILE（→ config/trading_config_max.json），
    而實盤引擎正在讀寫該檔 —— 必須 monkeypatch 隔離，禁止寫生產檔。
    """
    import json
    from grid_engine.config import GlobalConfig
    from grid_engine.config_io import load_raw

    p = tmp_path / "trading_config_max.json"
    p.write_text(json.dumps({
        "symbols": {"X/USDC:USDC": {"leverage": 20, "initial_quantity": 1}}}))
    # GlobalConfig.load() 無 path 參數，直接讀模組層 CONFIG_FILE
    # （沿用 tests/test_config_save.py:20 既有 pattern）
    monkeypatch.setattr("grid_engine.config.CONFIG_FILE", p)

    config = GlobalConfig.load()
    config.save()

    sym = load_raw(p)["symbols"]["X/USDC:USDC"]
    assert "leverage" not in sym
    assert "assumed_leverage" in sym
```

追加到 `tests/web/test_config_store.py`：

```python
def test_web_save_drops_legacy_leverage_key(tmp_path):
    """config_store.save_config 路徑：同樣必須移除舊 key（兩個 writer 都要傳
    drop_symbol_keys，漏一個就留殘骸）。"""
    p = tmp_path / "trading_config_max.json"
    p.write_text(json.dumps({
        "symbols": {"X/USDC:USDC": {"leverage": 20, "initial_quantity": 1}}}))

    config = config_store.load_config(path=p)
    config_store.save_config(config, path=p)

    sym = json.loads(p.read_text())["symbols"]["X/USDC:USDC"]
    assert "leverage" not in sym
    assert "assumed_leverage" in sym
```

- [ ] **Step 2: 跑測試確認兩條全紅**

Run: `uv run pytest tests/test_config_save.py::test_engine_save_drops_legacy_leverage_key tests/web/test_config_store.py::test_web_save_drops_legacy_leverage_key -v`
Expected: 2 FAILED，`assert "leverage" not in sym` 失敗（舊 key 仍在）

- [ ] **Step 3: 兩個 save 路徑各接上參數**

`grid_engine/config.py:255`：

```python
    def save(self):
        # drop_symbol_keys：一次性遷移，清除舊 leverage key。
        # merge_preserve 只 update 不刪 key，不顯式 drop 的話舊 key 會與
        # assumed_leverage 永久並存 ⇒ 使用者手動編輯舊 key 會靜默無效
        # ＝ 親手製造第二個假旋鈕。
        # 清除條件：生產 config 確認不含舊 key 後即可移除本參數（backlog）。
        merge_preserve_save(CONFIG_FILE, self.to_dict(),
                            drop_symbol_keys={"leverage"})
        console.print("[green]配置已保存[/]")
```

`web/services/config_store.py:59`（同樣加註記）：

```python
    config_io.merge_preserve_save(
        _resolve(path), config.to_dict(),
        symbol_extras=symbol_extras, ensure_backup=True,
        drop_symbol_keys={"leverage"})   # 一次性遷移，見 GlobalConfig.save 註記
```

- [ ] **Step 4: 跑測試確認兩條全綠**

Run: `uv run pytest tests/test_config_save.py::test_engine_save_drops_legacy_leverage_key tests/web/test_config_store.py::test_web_save_drops_legacy_leverage_key -v`
Expected: 2 PASSED

- [ ] **Step 5: Mutation 驗證（兩條，各驗一個 writer）**

1. 拿掉 `grid_engine/config.py` 的 `drop_symbol_keys=...` → `test_engine_save_drops_legacy_leverage_key` 必紅、web 那條仍綠
2. 還原後拿掉 `config_store.py` 的 → `test_web_save_drops_legacy_leverage_key` 必紅、engine 那條仍綠

這對「漏傳一個 writer」的疏漏有鑑別力——單一測試做不到。

- [ ] **Step 6: 修正被刻意打紅的 round-trip 守衛**

`tests/web/test_config_store.py:113-136` 的 `test_roundtrip_real_config_no_field_loss` 拿**真實生產 config** 斷言存檔零欄位遺失。drop 生效後 `missing` 會是四個 `symbols.*.leverage`。

先 Run: `uv run pytest "tests/web/test_config_store.py::test_roundtrip_real_config_no_field_loss" -v`
Expected: **FAILED**，`存檔遺失欄位: {'symbols.BNBUSDC.leverage', ...}`（確認它確實紅了再改）

改法——**白名單恰好這一次性刪除，其餘不變式原樣保留**：

```python
        missing = keys_recursive(before) - keys_recursive(after)
        # 一次性遷移：leverage → assumed_leverage 是刻意刪除的唯一例外。
        # 其餘欄位仍須零遺失 —— 這條守衛是 merge-preserve 的核心不變式，
        # 不因一次遷移而永久弱化。
        expected_dropped = {f"symbols.{s}.leverage"
                            for s, v in before.get("symbols", {}).items()
                            if "leverage" in v}
        assert missing == expected_dropped, f"非預期的存檔遺失欄位: {missing - expected_dropped}"
```

**`if "leverage" in v` 這個條件不可省。** 無條件對所有 symbol 生成 `expected_dropped` 是時間炸彈：Task 4 的 A14 收斂後生產檔不再有 `leverage`，`missing` 變空集而 `expected_dropped` 仍是四條 ⇒ **本測試永久變紅**，逼未來的人把斷言改鬆——那才是真正的後門。加了條件，遷移收斂後它自然退化成空集，守衛回到原始強度。

**禁止**改成 `missing <= 某個寬鬆集合`、`assert True`、或整條刪除。

- [ ] **Step 7: 確認守衛仍有鑑別力**

Run: `uv run pytest "tests/web/test_config_store.py::test_roundtrip_real_config_no_field_loss" -v`
Expected: PASSED

Mutation：把 `GlobalConfig.save()` 與 `config_store.save_config` 的 `drop_symbol_keys` 暫時改成 `{"leverage", "grid_spacing"}` → 本測試必紅（訊息含 `symbols.*.grid_spacing`），證明它仍在守零遺失而不只是被放行。還原。

**不要用「在 `to_dict()` 裡拿掉 `grid_spacing` 一行」當 mutation——那不會紅。** merge-preserve 的本質是 `sym_merged = dict(raw[...]); sym_merged.update(sym_new)`（`config_io.py:52-53`）：`to_dict()` 少 emit 一個 key 只代表「不更新」，raw 裡的 `grid_spacing` 會原樣保留 ⇒ `missing` 不變 ⇒ 綠。

- [ ] **Step 8: A13 —— `grep` 逐行人工裁決 + 白名單**

Run: `grep -rn "leverage" grid_engine/ web/ as_terminal_max.py`

**逐行**裁決每個命中，結果須全部落入白名單：
- `assumed_leverage` 本身（欄位、`to_dict`、存取點、UI label、已改名的區域變數）
- `from_dict` 的相容分支（`if "leverage" in data:` 三行）
- `_RENAMED` dict 與 `__getattr__` / `__setattr__` 攔截
- `drop_symbol_keys={"leverage"}` 兩處（Task 3）
- **所有 `BacktestConfig(...)` / `backtest.config.Config(...)` 的 `leverage=` kwarg，共三處**：
  `grid_engine/backtest.py:146`、`web/services/backtest_service.py:43`、
  **`web/pages/3_🔬_回測優化.py:905`**。
  改名後這三行都寫成 `leverage=<...>.assumed_leverage`——**左邊是 backtest 引擎的
  真參數名，右邊才是我們改名的欄位**。這是**合法命中，不是漏改**。
  ⚠️ 把左邊也改成 `assumed_leverage=` 會 `TypeError: unexpected keyword argument`，
  而頁3 **不在任何測試路徑上**，只會在使用者跑優化時炸。

任何落在白名單外的命中 = 漏改，回到 Step 6 修。

**重要**：grep 是**盤點工具，不是驗收判準**。它抓不到 `leverage=sym.leverage`
這型 kwarg——那正是先前 review 抓到的真實遺漏形態（`web/services/backtest_service.py:43`）。
真正的守衛是 Task 2 的 `__getattr__`/`__setattr__` raise + 全套測試。
**不得**把「grep 乾淨」當成可以跳過全套測試的理由。

- [ ] **Step 9: 全套測試**

Run: `uv run pytest -q 2>&1 | tail -5`
Expected: 全部 PASSED，報實際數字。

- [ ] **Step 10: Commit**

```bash
git add grid_engine/config.py web/services/config_store.py \
        tests/test_config_save.py tests/web/test_config_store.py
git commit -m "feat(config): 兩個 save 路徑清除舊 leverage key

merge_preserve 只 update 不刪 key ⇒ 不顯式 drop 的話舊 key 會與
assumed_leverage 永久並存，使用者編輯舊 key 靜默無效＝第二個假旋鈕。
兩個 writer 各驗一次（mutation 分別證明漏傳任一個都會紅）。
round-trip 零欄位遺失守衛改為白名單這次的一次性刪除，其餘不變式保留。"
```

---

### Task 4: 滾動發布與生產檔驗收（含使用者端動作）

**Files:** 無程式碼改動。

**Interfaces:**
- Consumes: Task 3 的兩個 save 路徑

**背景**：生產引擎（pid 31471）跑的是**舊碼**，`as_terminal_max.py` 有 18 處 `self.config.save()`，舊碼 `to_dict()` 仍 emit `"leverage"`。實際序列：web（新碼）save 刪掉 → 使用者在終端操作 → 舊碼 save **寫回** ⇒ 兩 key 又並存，來回震盪直到引擎重啟到新碼。

**過渡期零實盤影響**：該值在實盤路徑不被讀取；兩 key 並存時經舊碼路徑的回測用 `20`，正是它今天的行為。

- [ ] **Step 1: 記錄滾動前的生產檔狀態（read-only）**

Run:
```bash
python3 -c "
import json; d=json.load(open('config/trading_config_max.json'))
for k,v in d['symbols'].items():
    print(k, 'leverage=', v.get('leverage'), 'assumed_leverage=', v.get('assumed_leverage'))"
```
Expected: 四個 symbol 皆 `leverage=20`、`assumed_leverage=None`

- [ ] **Step 2: 請使用者重啟引擎到新碼**

這是**使用者端動作**，不可代勞。告知使用者：
- 重啟前後實盤行為零變更（該欄位不在下單/決策/風控路徑上）
- 重啟後面板的槓桿顯示會多一句「回測假設，非交易所實際」
- 重啟是**本任務完成的必要條件**——不重啟，舊碼會把舊 key 寫回，遷移不會收斂

- [ ] **Step 2b: 觸發一次 save 讓遷移落地（路徑指定，不可任選）**

drop 只在 `save()` 發生時作用。重啟後若沒有任何 save，舊 key 會留著、A14 會 fail
（方向是安全的，但會卡住且看起來像實作出錯）。

**指定路徑**：走終端「編輯交易對」流程，在槓桿提示**直接按 Enter 取預設**
（`as_terminal_max.py:918` 的 `default=cfg.assumed_leverage`，值不變），完成存檔。

> ### ⚠️ 不要用 web 頁2 / 頁3 觸發存檔
> `web/pages/2:260` 與 `web/pages/3:205` 的 `value=min(cfg.assumed_leverage, 15)`
> 有個 UI clamp。生產值是 **20**，經該表單存檔會被**靜默夾成 15** 並寫回
> （`:306` / `:214`）——直接違反本任務「不改任何行為」。
> 這個 clamp 是既有行為、不在本任務範圍，但觸發遷移時必須繞開它。

**Step 2b 完成後立刻 diff 一次生產檔**，確認變動**只有** `leverage` → `assumed_leverage`：

```bash
git diff -- config/trading_config_max.json
```
Expected: 每個 symbol 恰好一行 `-"leverage": 20` + 一行 `+"assumed_leverage": 20`，無其他變動。

理由：同一個終端編輯流程還會把 `take_profit_spacing` / `grid_spacing` 走一遍
`FloatPrompt.ask(default=x*100) / 100` 的浮點來回。四個 symbol 的**生產實值**
（0.003 / 0.004 / 0.006）經 `x*100/100` 實測皆 bit-identical，所以今天走這條路安全——
但那是這幾個值的巧合，**不是浮點通則**，所以要 diff 確認而不是假設。

- [ ] **Step 3: A14 —— 實檢生產檔（Goal 2 的唯一直接證據）**

引擎重啟到新碼、且經 Step 2b 觸發過一次 save 之後：

```bash
python3 -c "
import json; d=json.load(open('config/trading_config_max.json'))
bad=[k for k,v in d['symbols'].items() if 'leverage' in v]
missing=[k for k,v in d['symbols'].items() if 'assumed_leverage' not in v]
print('殘留舊 key:', bad or '無 ✅')
print('缺新 key:', missing or '無 ✅')
assert not bad and not missing"
```
Expected: 兩行皆 `無 ✅`，assert 通過。

判準**限定主檔**——`config/trading_config_max.json.bak-*` 等備份保留舊 key 是正確的，不得誤傷。

- [ ] **Step 4: 更新 progress**

把 TODO 4a 標記完成，記錄 A14 實檢結果與重啟時間，並保留 TODO 4b 的開放狀態與 §7.1 的誠實揭露（4a **不**修正「回測 20x vs 實盤 5x」）。

---

## 完成條件

- Task 1-3 全部 commit、全套測試綠（報數量）
- **dual-review 產出 `Ship as-is`**（dev-rules 強制；未達成前不得標記完成）
- verifier（fresh-context）ACCEPT
- Task 4 的 A14 在生產檔上實檢通過

## 放棄條件（spec §8）

若實作中發現 `__setattr__` 覆寫與 dataclass 或 Streamlit `session_state` 有無法乾淨解決的互動（例如 Streamlit 內部對 config 物件做 `deepcopy` 而觸發非預期路徑）→ **停止實作，回報使用者**。

降級選項是只保留 `__getattr__`、改以「三個賦值點（`web/pages/3:214`、`web/pages/2:306`、`as_terminal_max.py:916`）加測試釘死」補償——但那是**較弱的防線**（守不住未來新增的賦值點），需使用者明確接受，實作者不得自行降級。

## 誠實揭露（實作者與 reviewer 都須知悉）

- **本任務不修正「回測用 20x、實盤 5x」的保真度缺陷。** 改名後 `assumed_leverage` 仍是 `20`。修的是「名字騙人」，不是「數字錯」。數字歸 TODO 4b。
- **`__getattr__`/`__setattr__` 是防禦縱深，不是正確性保證。** 它抓「漏改」，不抓「改錯」。正確性由 A1（相容分支單元驗證）+ A11（bit-identical 回歸）共同承擔。
- **`test_constructor_rejects_old_kwarg` 幾乎零鑑別力**（測的是 dataclass 的語言保證）。留著無妨，不得算作一條防線。
- **本設計會遮蔽 `@property` 內部錯誤的原文（新引入的除錯性退化，非既有）。** `SymbolConfig` 有 5 個 `@property`；加上 `__getattr__` 後，property 內部拋的 `AttributeError` 原始訊息**完全不可回復**（`__context__` 與 `__cause__` 皆為 `None`，已用最小重現實測——`__getattr__` 由 slot 機制在 except 區塊之外呼叫，無隱式串接）。fallback 訊息已附重現方法（`type(obj).<name>.fget(obj)`）作為補償，但這是補償不是修復。
- **`web/pages/2:260`、`web/pages/3:205` 的 `value=min(cfg.assumed_leverage, 15)` UI clamp 是既有行為**，不在本任務範圍。但它會把生產值 20 靜默夾成 15 並寫回，所以 Task 4 觸發遷移時必須繞開這兩個表單（見 Task 4 Step 2b）。**這本身是另一個值得單獨處理的缺陷**，記入 backlog。
