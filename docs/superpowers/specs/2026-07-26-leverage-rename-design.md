# Design：TODO 4a — `leverage` → `assumed_leverage` 改名與舊 key 清除

- 日期：2026-07-26
- 對應：`tasks/progress.md` TODO 4，拆分後的**前半**（純 C 路線）
- 後半（TODO 4b：讀交易所實測槓桿）另立 spec，**不在本任務範圍**
- 拆分理由：前一版合併 spec（`2026-07-26-leverage-false-knob-design.md` v1/v2）連續兩輪被 quant reviewer 判 Reject，兩次 blocker 同一形態——**斷言一條接線存在而未查證**（v1 斷在交易所邊界、v2 斷在行程邊界）。依 judgment-rubrics R4「同一錯誤第二次 → 換路徑」，改為先交付零接線風險的這半。

---

## 1. 問題陳述

`SymbolConfig.leverage`（`grid_engine/config.py:39`）看起來是槓桿控制項，實際上：

- **實盤路徑完全不讀它。** 全 repo `set_leverage` / `setLeverage` 命中數 = **0**。交易所實際槓桿由使用者手動設定（2026-07-26 read-only 實測為 **5x**），config 寫 `20` 對下單、決策、風控零影響。
- **它唯一的實效是餵回測**：`grid_engine/backtest.py:146,173` 與 `web/services/backtest_service.py:43` 把它送進 `backtest.config.Config`，後者用它算保證金（`backtest/backtester.py:316,442`）與強平（`backtest/liquidation.py:24`）。
- ⇒ 名字承諾「控制槓桿」，實際語意是「回測用的假設值」。這是 lessons 通則 1（靜態結構看起來成立 ≠ 執行期成立）的典型形態，且已在 2026-07-12 造成真金事故（依 `leverage=20` 估保證金，實際 5x，第二批補空撞 `-2019`）。

**命名是這族缺陷唯一在 `grep` 當下就會自曝的防線。**

## 2. Goals

1. 欄位改名為 `assumed_leverage`，名字自述「這是假設值，不是控制項」。
2. **不留下第二個假旋鈕**：config 檔內的舊 `leverage` key 必須被實際移除，不得與新 key 並存。
3. 任何遺漏的舊名存取（讀或寫）在測試期爆炸，而非靜默降級。

## 3. Non-goals

- **不改任何行為**。`assumed_leverage` 仍等於今天的 `leverage`，回測仍收到同樣的數值。本任務是純語意修繕。
- **不修「回測用 20x 而實盤 5x」這個保真度缺陷**——那是 TODO 4b。見 §7.1 的誠實揭露。
- 不讀交易所、不呼叫 `set_leverage`、不新增任何 API 呼叫。
- 不改 `backtest/config.py:Config.leverage`（回測引擎的真旋鈕，名副其實）。
- 不改 `backtest/`、`scripts/` 的純離線路徑。
- 不改下單 / 決策 / 風控邏輯。

## 4. Security / 安全約束

- **零交易所互動。**
- **會寫 `config/`**：僅限舊 key 清除，走既有 `config_io` 的 flock + 原子寫（`grid_engine/config_io.py:105-114`）。生產引擎執行中，寫入須經 flock 序列化；不需要為此停機（理由見 §5.4）。
- 不寫 `logs/`、`log/`；不下單、不重啟引擎。
- 測試一律在 `$(mktemp -d)` 或 `tests/` 內，禁止觸碰生產 config 與 log。

---

## 5. 設計

### 5.1 接線稽核（**本節每一行都經 grep 驗證，非推測**）

`SymbolConfig.leverage` 的全部存取點：

**生產程式碼**（`grid_engine/`、`web/`、`as_terminal_max.py`）：

| 類別 | 位置 | 改名後的行為 |
|---|---|---|
| **讀（12）** | `grid_engine/config.py:74`、`grid_engine/backtest.py:146,173`、`web/pages/1:109`、`web/pages/2:64,260`、`web/pages/3:205,905`、`web/services/backtest_service.py:43`、`as_terminal_max.py:814,917,918` | 漏改 → `__getattr__` raise |
| **寫（3）** | `web/pages/3:214`、`web/pages/2:306`、`as_terminal_max.py:916` | 漏改 → `__setattr__` raise |
| **建構子 kwarg（4）** | `web/pages/2:201`、`as_terminal_max.py:876,1078`、**`grid_engine/backtest.py:173`** | 漏改 → **dataclass 自然拋 `TypeError: unexpected keyword argument`**，不需額外保護 |
| **持久化（4）** | `config/trading_config_max.json:19,30,41,52` | §5.4 清除 |

註：`grid_engine/backtest.py:173` **同時**是屬性讀取與 `SymbolConfig(...)` 建構子 kwarg（`:167` 起），故在兩列各出現一次。`web/pages/2:133,256`、`web/pages/3:201`、`as_terminal_max.py:862` 是純區域變數（`st.number_input` / `IntPrompt.ask` 的回傳），正確地不計入。

**測試程式碼**（`tests/`，§5.1 前一版整段遺漏；不改就會全紅）：

| 類別 | 位置 | 改法 |
|---|---|---|
| **寫（4）** | `tests/test_config_save.py:24`、`tests/web/test_config_store.py:61,108,142` | 改為 `assumed_leverage` |
| **建構子 kwarg（1）** | `tests/web/test_backtest_service.py:23` | 改為 `assumed_leverage=` |
| **不受影響** | `tests/test_optimizer_*.py` 各處、`tests/web/test_backtest_service.py:32`、`tests/test_config_io.py` 全部 | 這些是 `backtest.config.Config.leverage`（真旋鈕）或 raw dict 字面值，**不得改動** |

**特別點名：`tests/web/test_config_store.py:113-136` `test_roundtrip_real_config_no_field_loss` 會因本設計而必然變紅。**
它拿真實生產 config 做 round-trip，斷言 `keys_recursive(before) - keys_recursive(after) == set()`；drop 生效後 `missing` 會是四個 `symbols.*.leverage`。
該測試守的是 merge-preserve 最核心的不變式（**存檔絕不遺失欄位**），而本任務刻意破壞它一次。
**修法明文指定**：改為白名單**僅** `leverage` 這一次性刪除，其餘不變式原樣保留——
```python
assert missing == {f"symbols.{s}.leverage" for s in before["symbols"]}
```
**不得**把斷言改成 `missing <= 某個寬鬆集合` 或直接刪掉。一個真正的安全守衛不因一次性遷移而被永久削弱。

`merge_preserve_save` 的生產呼叫端**恰好兩個**：`grid_engine/config.py:255`、`web/services/config_store.py:59`（已 grep 確認無第三個 writer；`config_io.py:87` 是唯一 `json.dump` 寫 config 之處）。

全 repo **無** `getattr(cfg, "leverage", <default>)` 這型用法（會靜默拿到預設值而不觸發 `__getattr__`）。

### 5.2 改名

- `grid_engine/config.py:SymbolConfig` 欄位 `leverage` → `assumed_leverage`（型別、預設值 `20` 不變）。
- `to_dict()` 輸出新 key。
- `from_dict()` 加向後相容分支，照抄 `config.py:81-88` 既有的 `position_threshold` → `threshold_multiplier` pattern。**新舊 key 並存時新 key 勝。**
- §5.1 表列的 11 讀 + 3 寫 + 3 建構子全部改點。
- UI label 改為「回測假設槓桿（不推送交易所）」：`web/pages/2:260 附近`、`web/pages/3:201-205 附近`、`as_terminal_max.py:917`。
- `web/pages/1:109`（交易監控）顯示文字改為 `槓桿（回測假設，非交易所實際）: {n}x`。
  - **不移除**這行。TODO 4b 才有實測值可替換；在那之前移除是淨資訊損失。

### 5.3 舊名存取一律爆炸

`SymbolConfig` 加：
- `__getattr__(name)`：`name == "leverage"` → raise **`AttributeError`**，訊息含「已改名為 `assumed_leverage`；此值不推送交易所，僅供回測」。
- `__setattr__(name, value)`：`name == "leverage"` → raise **`AttributeError`**（同訊息）。
  - 沒有 `__setattr__` 的話，`cfg.leverage = 20` 會靜默建立一個實例屬性：之後讀取成功（`__getattr__` 不再觸發）、`to_dict()` 完全忽略它 ⇒ **使用者在 web 調槓桿，畫面顯示成功、存檔沒有、回測沒用到**。那正是本任務要消滅的假旋鈕的複刻。
  - `__setattr__` 必須放行所有其他名稱（dataclass `__init__` 靠逐欄位 `setattr` 賦值）。

**fallback 分支必須帶上屬性名**：`__getattr__` 對**非** `"leverage"` 的名稱，必須
`raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")`。
理由：`SymbolConfig` 有 5 個 `@property`（`config.py:43-63`：`coin_name`/`contract_type`/`ws_symbol`/`position_limit`/`position_threshold`）。**若 property 內部拋 `AttributeError`，Python 會改而呼叫 `__getattr__`**——寫死一則「leverage 已改名」的訊息會吞掉原始錯誤並誤導除錯。

**型別紀律**：兩者都**只能拋 `AttributeError`**。`copy` / `pickle` / 部分框架會對實例做 `getattr(obj, '__deepcopy__' / '__getstate__' / '__reduce_ex__', None)`，拋非 `AttributeError` 會炸掉無關路徑。（現況已 grep 確認 repo 內無 `deepcopy` / `pickle` / `copy.copy` 作用於 `SymbolConfig`，但 Streamlit session_state 未來變更可能引入，故立為紀律而非依賴現況。）

**明文禁止**：不得對 config 欄位使用帶 default 的 `getattr`（`getattr(cfg, "leverage", 20)` 會繞過攔截靜默取預設值）。`grid_engine/backtest.py:151` 已有 `getattr(config, "direction", "both")` 的同型先例，實作時不得擴散此用法。

### 5.4 舊 key 清除

`grid_engine/config_io.py:52-53` 的 symbol merge 是 `sym_merged = dict(raw_symbols.get(sym_key, {})); sym_merged.update(sym_new)` —— **只 update，永不刪 key**。若不處理，檔案內會長期同時存在 `leverage: 20` 與 `assumed_leverage: 20`，而使用者手動編輯時最可能去改那個熟悉的舊 key ⇒ 本修繕會親手製造出它要消滅的病。

修法：
1. `merge_preserve()` 新增 `drop_symbol_keys: Optional[set] = None` 參數。
2. **實作位置明確指定**：作為對 `merged["symbols"]` 的**獨立最終 pass**，位於 `symbol_extras` 套用（`config_io.py:62-64`）**之後**。
   - 不得寫在 symbol 分支（`:48-55`）內：該分支只在 `new` 含 `"symbols"` key 時執行，`new` 不含 symbols 的呼叫會讓 drop 靜默不發生。
   - 不得寫在 `symbol_extras` 之前：`symbol_extras` 的 `update` 會把剛刪掉的 key 再塞回去。
   - 語意定為 **drop 永遠勝出**。
3. `merge_preserve_save()` 透傳該參數。
4. **兩個 save 路徑皆傳** `drop_symbol_keys={"leverage"}`：`grid_engine/config.py:255`、`web/services/config_store.py:59`。漏一個就留殘骸。
5. 生產 config 的實際清除，**需要引擎重啟到新碼才會收斂**（見下）。

### 5.5 滾動發布：舊碼會把舊 key 寫回，config 在重啟前不收斂

生產引擎（pid 31471）跑的是**舊碼**，而舊碼的 `as_terminal_max.py` 有 **18 處 `self.config.save()`**（grep 實測），其 `to_dict()`（`config.py:74`）仍 emit `"leverage": self.leverage`。實際序列：

1. web（新碼）save → `leverage` 被刪
2. 使用者在終端做任一操作 → 舊碼 save → **`leverage: 20` 被寫回**，而 `assumed_leverage` 因 merge-preserve 留存
3. ⇒ **兩個 key 又並存**，並持續來回震盪直到引擎重啟到新碼

**這不是接線錯誤，是滾動發布順序問題，但後果一樣**：Goal 2（不留下第二個假旋鈕）在引擎重啟前實際上沒有達成，而 A6-A9 全部驗的是機制、不是生產檔案，會全綠。

**因此**：
- 「引擎重啟到新碼」列為**本任務完成條件的一部分**（使用者端動作）。
- 新增驗收 **A14**：滾動完成後**實檢生產檔**確認不含任何 `leverage` key。這是 Goal 2 是否真的達成的唯一直接證據。
- 過渡期（重啟前）**零實盤影響**：該值在實盤路徑不被讀取；兩個 key 並存時經舊碼路徑的回測用 `20`，正是它今天的行為。過渡期不引入新錯誤，只是遷移未完成。

**hardcode 的清除條件**：`drop_symbol_keys={"leverage"}` 是永久寫死在兩個 save 路徑上的一次性遷移碼。實作時須加註記：生產 config 確認不含舊 key 後即可移除（列入 backlog，非本任務驗收項）。

---

## 6. 可判定驗收準則

全部須為實跑證據，不接受自述。標記 **(M)** 者須附 mutation 證明（先在真實缺陷前紅一次）。

1. **A1**：`from_dict({"leverage": 5})` → `assumed_leverage == 5`；`from_dict({"assumed_leverage": 7, "leverage": 5})` → `7`；`from_dict({})` → `20`；`to_dict()` 的 key 為 `assumed_leverage` 且**不含** `leverage`。
2. **A2 (M)**：`cfg.leverage`（讀）拋 `AttributeError`，訊息含 `assumed_leverage`。Mutation：移除 `__getattr__` → 紅。
3. **A3 (M)**：`cfg.leverage = 20`（寫）拋 `AttributeError`；且事後 `to_dict()` 不含 `leverage`、`assumed_leverage` 未被污染。Mutation：移除 `__setattr__` → 紅（此測試必須先在「只有 `__getattr__`」的版本下紅過一次，證明它抓的是寫入而非讀取）。
4. **A4**：`SymbolConfig(assumed_leverage=5)` 正常建構；`SymbolConfig(leverage=5)` 拋 `TypeError`。
5. **A5**：`__getattr__` / `__setattr__` 對其他不存在的屬性名維持原生行為（拋 `AttributeError` 且訊息含該屬性名），對合法欄位賦值正常；`dataclasses.asdict(cfg)` 與 `copy.deepcopy(cfg)` 皆不拋非 `AttributeError` 例外。
   **另加一條**：某個 `@property` 內部拋 `AttributeError` 時，向外傳播的訊息**不得**被改寫成「leverage 已改名」（構造法：把 `ccxt_symbol` 設成非字串使 `coin_name` 的 `.split` 失敗）。
6. **A6 (M)**：`merge_preserve_save(..., drop_symbol_keys={"leverage"})` 後 `load_raw()` 的 symbol dict **不含** `"leverage"`、且含 `"assumed_leverage"`。Mutation：移除 drop 邏輯 → 紅。
7. **A7 (M)**：drop 在 `new` **不含** `"symbols"` key 時**仍生效**。Mutation：把 drop 寫進 symbol 分支內 → 紅。
8. **A8 (M)**：`symbol_extras` 含 `{"leverage": 20}` 時，drop **仍勝出**（結果不含該 key）。Mutation：把 drop 移到 `symbol_extras` 之前 → 紅。
9. **A9**：**兩個** save 路徑各驗一次 A6——`GlobalConfig.save()` 與 `web/services/config_store.py` 的 save，皆須使舊 key 消失。（漏傳參數是最可能的實作疏漏，須分別驗。）
   **手法明文寫死**：`GlobalConfig.save()` 那半必須 `monkeypatch.setattr("grid_engine.config.CONFIG_FILE", tmp)`（沿用 `tests/test_config_save.py:20` 既有 pattern）。`config.py:255` 硬寫 `CONFIG_FILE`（→ `config/trading_config_max.json`），而**本機實盤引擎正在跑且會寫同一個檔**，不得依賴實作者記得隔離。
10. **A10**：`merge_preserve` 在**未傳** `drop_symbol_keys` 時行為與改動前完全相同（既有 `tests/test_config_io.py` 全數維持綠，不修改其斷言）。
11. **A11（行為零變更）**：同一份 config、同一份資料，改動前後經 `grid_engine/backtest.py` 與 `web/services/backtest_service.py` 跑出的回測 result dict **完全相同**（bit-identical）。
    **必須使用舊 key 值 ≠ 預設值的 config（指定 `leverage: 7`）**。用生產值 `20` 跑會自廢武功：若 `from_dict` 相容分支完全失效（舊 key 被 `config.py:92` 的 `if k in cls.__dataclass_fields__` 靜默濾掉），值落回預設 `20`，而檔案裡正好也是 `20` ⇒ result 逐位元相同、A11 全綠，對本任務最核心的遷移邏輯完全失明。
15. **A15（`raw` 純度）**：傳入 `drop_symbol_keys` 後，呼叫端的 `raw` dict **不被 mutate**。既有 `tests/test_config_io.py:67` 只守 `symbol_extras` 路徑，drop 路徑無對應守衛（天真實作直接迭代 `raw["symbols"]` 就會踩到）。
16. **A16（round-trip 守衛不被弱化）**：`tests/web/test_config_store.py` 的 `test_roundtrip_real_config_no_field_loss` 改為白名單**恰好**四個 `symbols.*.leverage`，其餘欄位仍斷言零遺失（見 §5.1 指定的修法）。
17. **A14（Goal 2 的唯一直接證據，滾動完成後執行）**：引擎重啟到新碼後，**實檢生產檔** `config/trading_config_max.json` 不含任何 `leverage` key、且四個 symbol 皆有 `assumed_leverage`。
    判準限定**主檔**——`config/trading_config_max.json.bak-*` 等備份保留舊 key 是正確的，不得誤傷。
12. **A12**：全套測試綠，報數量不報形容詞。
13. **A13**：`grep -rn "leverage" grid_engine/ web/ as_terminal_max.py` 的每一行**逐行人工裁決**，產出白名單（合法保留者僅限：`assumed_leverage` 本身、`from_dict` 相容分支、`__getattr__`/`__setattr__` 攔截、`drop_symbol_keys={"leverage"}` 兩處）。
    - **不使用** `grep "\.leverage"` 當自動判準——它抓不到 `leverage=sym.leverage` 這型 kwarg，而那正是 v2 review 抓到的實際遺漏（`web/services/backtest_service.py:43`）。真正的守衛是 A2/A3 的 raise，grep 只是輔助盤點。

**停止條件**：dual-review 產出 `Ship as-is` 之前，本任務不得標記完成。

---

## 7. 誠實揭露

1. **本任務不修正「回測用 20x、實盤 5x」的保真度缺陷。** 改名後 `assumed_leverage` 仍是 `20`，回測仍以 20x 計保證金、低估需求 4 倍。修的是「名字騙人」，不是「數字錯」。數字由 TODO 4b 處理。
   - **可選的權宜措施**（不在本任務範圍，需使用者明確指示）：把生產 config 的 `assumed_leverage` 直接改成 `5`，回測今天就正確。代價有兩個：(i) 它會再次變成一個會過時的靜態值——正是 4b 要根治的形態；(ii) §5.5 過渡期分析裡「值就是 20、落回預設無差別」的巧合會消失——舊碼引擎 reload 時會把回測值從 `5` 拉回預設 `20`（仍**不影響實盤**，但重啟前的回測結果會依走哪條路徑而異）。
2. **既有回測結論的槓桿假設，分兩種情況**：
   - **requote 實驗：已核實乾淨。** `scripts/requote_experiment.py:214` 取自 `scripts/calibration_gate.py:38` 的 `PROD["leverage"] = 5.0`。
   - **#14 threshold 掃描：槓桿假設不可考。** 主力 script 是 session scratchpad 的 `segment_scan.py`（`tasks/progress.md:98` 明載，repo 內不存在）；同期 `scripts/cost_sensitivity.py:122` 預設 `--leverage 20`。**現行生產 config 的 `mult=40` 上線決策未經 5x 複核，而該決策的核心正是保證金與裝死邊界。** 列為明確待辦，不得再宣稱它安全。
3. **`__getattr__` / `__setattr__` 攔截是防禦縱深，不是正確性保證。** 它抓的是「漏改」，不是「改錯」。真正的正確性由 **A1（相容分支的單元驗證）+ A11（bit-identical 回歸）共同**承擔——單靠 A11 不夠，理由見 A11 本身的註記。
   另：**A4 幾乎零鑑別力**（`SymbolConfig(leverage=5)` → `TypeError` 是 dataclass 的語言保證，等於在測 Python 本身）。成本也接近零，留著無妨，但不得算作一條防線。
4. **A13 的 grep 是盤點工具，不是驗收判準。** 自動 grep 判準在 v2 review 中已被證明會漏掉最可能的遺漏形態，故本版明文降級為輔助。

## 8. 放棄條件

若實作中發現 `__setattr__` 覆寫與 dataclass / Streamlit session_state 有無法乾淨解決的互動（例如 Streamlit 內部對 config 物件做 `deepcopy` 而觸發非預期路徑）→ 停止實作，回報使用者。降級選項是只保留 `__getattr__` 並改以「三個賦值點加測試釘死」補償，但那是較弱的防線，需使用者明確接受。
