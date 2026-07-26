# Design：`leverage` 假旋鈕修繕（讀取校準 + 改名揭露）

- 日期：2026-07-26
- 對應 TODO：`tasks/progress.md` TODO 4
- 路線：**B + C**（B = 反向接線讀取實測值；C = 欄位改名揭露）。方案 A（`set_leverage` 寫入交易所）經使用者裁決**不做**。
- 版本：**v2**（v1 經 fresh-context quant reviewer 判 Reject，2 blockers + 6 must-fix；本版為修訂後）

---

## 0. v1 → v2 修訂摘要（留痕）

| 項目 | v1 宣稱 | v2 修正 | 依據 |
|---|---|---|---|
| Blocker-1 | 「`fetch_positions` 回傳本來就含 `leverage`」（標為事實） | **證偽**。ccxt 4.5.32 預設 V3 positionRisk 不回該欄位，須 `params={'useV2': True}` | 本機 read-only 實測，見 §1.4 |
| Blocker-1 連帶 | 「零額外 REST 呼叫」 | 改為**獨立低頻 V2 呼叫**，實盤持倉同步路徑完全不動（使用者裁決） | §5.1 |
| Blocker-2 | 「舊 key 在下次 `save()` 自然遷移」 | **證偽**。`config_io.merge_preserve` 只 update 不刪 key，舊 key 會永久殘留 → 需顯式刪除機制 | `grid_engine/config_io.py:52-53` |
| Must-fix | 連帶改動清單漏 `web/services/backtest_service.py:43` | 補入，且 web 回測路徑的取值規則明文化 | §5.2 / §5.4 |
| Must-fix | REST 例外路徑未定義 | 明文：例外 → 全部設 `None`，不留過期值 | §5.1 |
| Must-fix | A9 replay zero-diff 當「實盤行為零變更的證據」 | **降級為回歸守衛**（`DecisionInputs` 不含 leverage ⇒ 恆綠），改用有鑑別力的 A9' | §6 |
| Must-fix | A8 grep 判準 | 抓不到 `leverage=sym.leverage` 這型 → 改用 `__getattr__` 爆炸法 | §5.2 / §6 |
| Must-fix | §7.3 一句話擔保 #14 與 requote 兩者 | 拆開：requote 已核實 5x；#14 槓桿假設不可考，列明確待辦 | §7.3 |
| Should-fix | hedge 分歧取高值 | 改為**視同未知**（`None`）+ WARNING，語意與其餘缺值處置一致 | §5.1 |
| Should-fix | 無「長期取不到」告警 | 補 §5.1 告警 | §5.1 |
| Should-fix | 回測結果不可重現 | 實際生效 leverage 與來源寫進 result dict | §5.4 |
| Should-fix | point-in-time / cross margin / 拒開倉塌陷 未揭露 | 補 §7.5 / §7.6 / §7.7 | §7 |

---

## 1. 問題陳述

### 1.1 實盤路徑完全不讀這個欄位（已由 reviewer 獨立複核）
`grep -rn "leverage"` 全 repo：

| 位置 | 角色 |
|---|---|
| `grid_engine/config.py:39` | `SymbolConfig.leverage: int = 20` 宣告 |
| `grid_engine/config.py:74` | `to_dict()` 序列化 |
| `grid_engine/backtest.py:146,173` | 餵給 `backtest.config.Config` |
| `web/services/backtest_service.py:43` | **web 頁3 回測的唯一映射入口** |
| `web/pages/1,2,3`、`as_terminal_max.py:814,862,876,916-918,1078` | 顯示與編輯 |
| `config/trading_config_max.json:19,30,41,52` | 4 個 symbol 各一 |

全 repo `set_leverage` / `setLeverage` / `marginMode` 命中數 = **0**。實盤下單、決策、風控路徑確實不讀。

### 1.2 真正的傷害在回測
`backtest/backtester.py:316,442`（`margin_required = (qty*price)/leverage`）、`backtest/accounting.py:71,78,94,95,102,115`、`backtest/liquidation.py:24`（`margin_usage = (notional/leverage)/equity`）全用它。

⇒ 經 `grid_engine/backtest.py` 或 `web/services/backtest_service.py` 的回測皆以 **20x** 計保證金，實盤 **5x**，保證金需求系統性低估 **4 倍**。

### 1.3 這是 lessons 通則 1 的第五個現場
「接線斷在 repo 外」——唯一合格證據是查外部系統本身（通則 1 第 4 條）。

### 1.4 資料來源（**實測，非推測**）

2026-07-26 本機 read-only 實測（ccxt 4.5.32，生產帳戶，未下單）：

```
[default (V3 positionRisk)]  BNB/USDC:USDC long/short
    leverage=None            info 內無 'leverage' key
[useV2=True]                 BNB/USDC:USDC long/short
    leverage=5.0             info['leverage']='5'
    marginMode='cross'       liquidationPrice=288.936
```

結論三條：
1. **ccxt 4.5.32 的 `fetch_positions` 預設走 `fapiPrivateV3GetPositionRisk`，不回 `leverage`。** v1 的核心前提是假的。
2. `params={'useV2': True}` 可取得，且 LONG/SHORT 兩側同值（Binance 槓桿為 **per-symbol**）。
3. 帳戶 `marginMode` 實測為 **cross**（見 §7.6 的保真度限制）。

`grid_engine/sync_service.py:55` 目前的 `fetch_positions(params={'type':'future'})` 走的正是預設 V3 路徑，`:61-71` 迴圈同時丟棄了 `leverage` 與 `liquidationPrice`。

---

## 2. Goals

1. 讓**交易所實測槓桿**成為引擎內的權威值，並出現在使用者會看的監控畫面上。
2. 讓所有經 `grid_engine` / `web` 觸發的回測**不再用假設值**產出會被當作真金依據的數字。
3. 讓欄位名稱本身在 `grep` 時就自曝「這是假設值，不是控制項」，且**不留下第二個假旋鈕**。

## 3. Non-goals

- **不呼叫 `set_leverage`**：改槓桿是使用者手動裁決。
- **不改實盤持倉同步路徑**：`_sync_positions` 的 endpoint、解析、寫入 state 的行為一行不動（使用者裁決，見 §5.1）。
- **不做 marginMode 檢查、不接線 `liquidationPrice`**：同位置同毛病，屬獨立項。
- **不改任何下單 / 決策 / 風控邏輯**。
- **不改 `backtest/config.py:Config.leverage`**：那是回測引擎的真旋鈕，名副其實。
- **不改 `backtest/`、`scripts/` 的純離線路徑**：槓桿由呼叫端明確給定。
- **不追溯重跑既有回測結論**（但需揭露，見 §7.3）。

## 4. Security / 安全約束

- 交易所互動**全程 read-only**：只新增一個 `fetch_positions(params={'useV2': True})` 讀取呼叫，零 write endpoint。
- 不寫 `logs/`、`log/`；不下單、不重啟引擎。
- **會寫 `config/`**：僅限 §5.3 的一次性舊 key 清除，走既有 `config_io` flock + 原子寫，且必須在引擎停機時或經 flock 序列化下進行。此為 v1 non-goal 的**刻意修訂**（Blocker-2 迫使），留痕於此。
- 測試一律在 `$(mktemp -d)` 或 `tests/` 內，禁止觸碰生產 config 與 log。

---

## 5. 設計

### 5.1 B — 實測槓桿讀取（獨立低頻呼叫）

**為何獨立呼叫**：`useV2=True` 若加在 `_sync_positions` 既有那次呼叫上，等於把**實盤持倉同步**的 endpoint 由 V3 換成 V2，而該路徑餵的 `long_position`/`short_position` 直接進 `decide()`。真金路徑的改動風險，遠高於每小時多一次 REST 的成本。使用者裁決走獨立呼叫。

**狀態欄位**（`grid_engine/state.py:SymbolState`）：
```python
exchange_leverage: float | None = None       # 交易所實測槓桿；None = 未知
leverage_mismatch_notified: bool = False     # 分歧通知去重
leverage_unavailable_rounds: int = 0         # 連續取不到的輪數（告警用）
```
`None` 表示「未知」，**不得**用 `0` 或 `20` 代替（lessons 2026-07-10：安全檢查函數不能用 False 表達「我不知道」）。

**讀取**：`SyncService` 新增 `_sync_leverage()`，由 `maybe_sync()` 以獨立節流閘呼叫：
- 成功後間隔 **3600s**（槓桿是幾乎不變的值）。
- 目前為 `None` 時改用 **60s** 重試間隔，避免一次 REST 抽風就讓值消失一小時。
- 呼叫：`fetch_positions(params={'type':'future','useV2':True})`，走既有 `gateway.call`（沿用重試/斷路器）。

**取值規則**（每個 enabled symbol 獨立判定）：

| 情形 | 處置 |
|---|---|
| 取得單一有效值（`> 0` 的數值） | 寫入 `exchange_leverage` |
| 同 symbol 兩側（LONG/SHORT）值**不一致** | **視同未知**（`None`）+ WARNING。理由見下 |
| 該 symbol 不在回傳中（零倉位） | `None` |
| 欄位缺失 / 非數值 / `<= 0` | `None` + WARNING |
| **整批呼叫拋例外** | **所有 symbol 一律設 `None`** + WARNING。不留過期值 |

「兩側不一致視同未知」取代 v1 的「取較高者」：Binance 槓桿為 per-symbol（§1.4 實測兩側同值），該分歧在現實中幾乎不可能發生；真發生代表我們對交易所模型的理解有誤，此時**任何**數值都不該被信任。取高值 = 保證金估得較低 = 樂觀，與「保證金估算取最壞情況」的量化紀律相牴觸；而揭露職責已由 WARNING 承擔，數值不必兼任。

**告警**：
- 分歧（`exchange_leverage` 與 `assumed_leverage` 皆已知且不等）→ WARNING + Telegram **一次**，由 `leverage_mismatch_notified` 去重；分歧消失則重設旗標。
- `exchange_leverage` 連續 **6 輪**取不到（`leverage_unavailable_rounds >= 6`）→ WARNING + Telegram 一次。防止 B 方案整體靜默失效卻無人知曉。
- **不阻擋啟動、不阻擋交易**：實盤下單不讀此值，擋啟動換不到任何安全性。

### 5.2 C — 欄位改名 `leverage` → `assumed_leverage`

- `grid_engine/config.py:SymbolConfig` 欄位改名，`to_dict()` 輸出新 key。
- `from_dict()` 加向後相容分支，照抄 `config.py:81-88` 既有的 `position_threshold` → `threshold_multiplier` pattern。**新舊 key 並存時新 key 勝**。
- **舊名存取一律爆炸**：`SymbolConfig` 加 `__getattr__`，對 `"leverage"` 拋 `AttributeError` 並附明確訊息（「已改名為 assumed_leverage；此值不推送交易所」）。取代 v1 那條抓不到 `leverage=sym.leverage` 形態的 grep 驗收——讓遺漏在測試期爆炸，而非靜默。
- 連帶改動（完整清單）：
  - `grid_engine/backtest.py:146,173`
  - **`web/services/backtest_service.py:43`**（v1 遺漏；web 頁3 回測唯一映射入口）
  - `web/pages/2_⚙️_交易對管理.py:64,260,306`、`web/pages/3_🔬_回測優化.py:201-214,905`、`web/pages/1_📈_交易監控.py:109`
  - `as_terminal_max.py:814,862,876,916-918,1078`
  - 測試：`tests/web/test_config_store.py`、`tests/test_config_save.py`、`tests/test_config_io.py`、`tests/web/test_backtest_service.py`
- **`backtest/config.py:Config.leverage` 不改名**（真旋鈕，名副其實）。

### 5.3 舊 key 清除（Blocker-2 修法）

`grid_engine/config_io.py:52-53` 的 symbol merge 只 `update`、永不刪 key。若不處理，`config/trading_config_max.json` 會長期同時存在 `leverage: 20` 與 `assumed_leverage: 5`，而使用者手動編輯時最可能去改那個熟悉的舊 key —— **本修繕會親手製造出它要消滅的病**。

修法：
1. `merge_preserve()` 新增 `drop_symbol_keys: Optional[set] = None` 參數，於 symbol 分支 merge 後移除指定 key；`merge_preserve_save()` 透傳。
2. `GlobalConfig.save()` 與 `web/services/config_store.py` 兩個 save 路徑**皆**傳入 `drop_symbol_keys={"leverage"}`。（兩個 writer 都要，漏一個就留殘骸。）
3. 生產 config 的一次性清除：在引擎停機窗口，或由任一 save 路徑自然觸發。**不需要為此特地停機**——舊 key 殘留期間的行為與今日相同（見下）。

**過渡期行為（明文接受）**：若在生產引擎（舊碼）執行期間，web 側以新碼 save 而移除了 `leverage`，舊碼引擎下次 reload 讀不到該 key 會落回預設 `20`。
**零實盤影響**——該值在實盤路徑不被讀取；經此路徑的回測會用 `20`，而那正是它今天的行為。過渡期不引入新錯誤，只是延後修正。

### 5.4 回測取值規則（涵蓋兩個入口）

適用 `grid_engine/backtest.py:run_backtest()` / `optimize_params()` **與** `web/services/backtest_service.py:to_backtest_config()`：

| `exchange_leverage` | 行為 |
|---|---|
| 已知 | **用實測值**，忽略 `assumed_leverage` |
| `None`（未知） | **raise**，訊息須含：為何拒絕、如何取得實測值、以及顯式覆寫的方法 |

raise 訊息必須提供**明確的一鍵出路**（顯式 `leverage=` 覆寫參數），否則使用者會用最壞的方式繞過（直接改 code）。顯式覆寫時，報告頭與 result 皆須標示「使用者顯式指定，非交易所實測」。

**可重現性**（quant 規則：回測必須可重現）：`result` dict 新增
```python
"effective_leverage": 5.0,
"leverage_source": "exchange" | "explicit",
```
並印在報告頭。否則同一份資料、同一份 config，在使用者調整交易所槓桿前後會跑出不同數字而無跡可尋。

`backtest/` 套件與 `scripts/*` 的純離線路徑不受影響。

### 5.5 UI

- **頁 1 交易監控 / 終端面板**：顯示 `exchange_leverage`，格式 `5x（交易所實測）`；`None` 時顯示 `?（未取得）`。**不再顯示 config 值**——監控面板是最容易誤導真金決策的地方，且 §5.1 已保證 `None` 不會是過期值。
- 頁 2 / 頁 3：保留可編輯（離線回測需要），label 改「回測假設槓桿（不推送交易所）」。

---

## 6. 可判定驗收準則

全部須為實跑證據，不接受自述。標記 **(M)** 者須附 mutation 證明（先在真實缺陷前紅一次）。

1. **A1 (M)**：`_sync_leverage` 收到含 `leverage` 的回傳 → `SymbolState.exchange_leverage` 等於該值。Mutation：移除讀取那行 → 紅。
2. **A2 (M)**：symbol 先有值（`5`），下一輪回傳不含該 symbol → 變回 `None`（**不是**維持 5）。Mutation：改成沿用舊值 → 紅。
3. **A3 (M)**：整批呼叫拋例外 → 所有 symbol 的 `exchange_leverage` 變 `None`。Mutation：改成 early-return 保留舊值 → 紅。
4. **A4**：兩側 leverage 為 5 與 10 → 結果為 `None` + WARNING（**不是**取 10）。
5. **A5 (M)**：分歧持續時 Telegram 只發一次；分歧消失後再現則再發一次。Mutation：去重旗標寫死 `True` → 紅。
6. **A6**：連續 6 輪取不到 → 發一次 unavailable 告警。
7. **A7**：`from_dict({"leverage": 5})` → `assumed_leverage == 5`；`from_dict({"assumed_leverage": 7, "leverage": 5})` → `7`；`to_dict()` key 為 `assumed_leverage`。
8. **A8 (M)**：存取 `SymbolConfig.leverage` 拋 `AttributeError` 且訊息含新欄位名。Mutation：移除 `__getattr__` → 紅。
9. **A9 (M)**：`merge_preserve_save(..., drop_symbol_keys={"leverage"})` 後，`load_raw()` 的 symbol dict **不含** `"leverage"` 且含 `"assumed_leverage"`。Mutation：移除 drop 邏輯 → 紅。**兩個 save 路徑各驗一次**（`GlobalConfig.save()` 與 `config_store`）。
10. **A10 (M)**：`exchange_leverage is None` 時，`grid_engine/backtest.py` **與** `web/services/backtest_service.py` **皆** raise（不 fallback 到 `assumed_leverage`）。Mutation：改成 fallback → 兩處各紅一次。
11. **A11**：`exchange_leverage=5` → 回測實收 `Config.leverage == 5`，且 `result["effective_leverage"] == 5.0`、`result["leverage_source"] == "exchange"`。
12. **A12（保真度回歸，人工判讀）**：同一份資料、同一組參數，以 20x 與 5x 各跑一次，報告 `trades_count` 與**開倉因保證金不足被拒次數**（`backtest/backtester.py:317-319` 目前是無計數器的靜默 skip，需補計數器）。若 5x 下拒開次數暴增到回測失去代表性，須在結果標註，不得靜默交付。
13. **A13**：全套測試綠，報數量不報形容詞。
14. **A9'（取代 v1 的 A9）**：以 mock gateway 跑一輪完整 sync，斷言 `decide()` 收到的 `DecisionInputs` 逐欄位與改動前 bit-identical。
    - v1 原本的「`decisions.jsonl` replay zero-diff」**降級為回歸守衛，不得當作獨立證據**：`grid_engine/replay.py:32-34` 只重跑 `decide()`，而 `grid_engine/decision.py:24-44` 的 `DecisionInputs` 21 個欄位不含 leverage，本改動也不新增 ⇒ zero-diff 在改動前後**必然成立**，就算把 `_sync_leverage` 寫爛也一樣綠。（lessons 2026-07-15：驗證器與被驗物共用判準 = 回歸守衛，不是獨立證據。）仍照跑，但如實標示其性質。

**停止條件**：dual-review 產出 `Ship as-is` 之前，本任務不得標記完成。

---

## 7. 誠實揭露 / 已知限制

1. **B 修不了「人為口算保證金時用錯槓桿」本身**——那是人為錯誤。它能修的是讓正確的值出現在使用者會看的畫面上，以及讓回測不再用錯的槓桿。不包裝成比實際更強。
2. **本設計不改善任何策略績效**，也不宣稱改善；是保真度與可觀測性修繕。故本 spec 不提績效門檻——沒有績效宣稱要驗證。
3. **既有回測結論的槓桿假設，分兩種情況**（v1 用一句話擔保兩者，是錯的）：
   - **requote 實驗：已核實乾淨。** `scripts/requote_experiment.py:214` 取自 `scripts/calibration_gate.py:38` 的 `PROD["leverage"] = 5.0`，用的是正確值。
   - **#14 threshold 掃描：槓桿假設不可考。** 主力 script 是 session scratchpad 的 `segment_scan.py`（`tasks/progress.md:98` 明載，repo 內不存在），無法驗證；同期 `scripts/cost_sensitivity.py:122` 預設 `--leverage 20`。**mult=40 的上線決策（現行生產 config）未經 5x 複核，而該決策的核心正是保證金與裝死邊界。** 列為明確待辦，非本任務範圍——但不得再宣稱它安全。
4. **`exchange_leverage` 依賴該 symbol 有倉位**。V2 positionRisk 對零倉位 symbol 的覆蓋情況未實測（本帳戶只有 BNBUSDC 有倉位）。零倉位 symbol 可能長期為 `None`，其回測會 raise（有 §5.4 的顯式覆寫出路）。
5. **point-in-time 違規（新揭露）**：`exchange_leverage` 是「**現在**的交易所設定」，卻被用來回測**過去**期間。若使用者在樣本期間中途改過槓桿，這就是用今日狀態回填過去。`leverage_source` 欄位讓事後可追溯，但不消除偏誤。
6. **cross margin 保真度限制（新揭露，已實測）**：帳戶 `marginMode` 實測為 **cross**（單一保證金池、跨 symbol 共享、強平由帳戶層維持保證金決定），而 `backtest/liquidation.py:29-31` docstring 自承用的是 **isolated margin 簡化模型**。餵入正確的 leverage 是把「用錯的數字」換成「用對的數字餵進簡化模型」——**保真度提升真實但小於直覺**。marginMode 建模維持 non-goal。
7. **5x 會讓回測大量開倉被靜默拒絕（新揭露）**：保證金需求變 4 倍後，`backtest/backtester.py:317-319` 的 `if margin_required <= available_margin` 靜默 skip 可能大量觸發，使成交筆數塌陷——從「低估風險」變成「另一種失真」。A12 要求把它量出來並人工判讀，不得靜默交付。

---

## 8. 放棄條件

1. 若 `fetch_positions(params={'useV2': True})` 在生產環境被 Binance 標記為 deprecated 而停止服務 → 停止實作，回報使用者重新裁決（改用 `fapiPrivateV2GetPositionRisk` 直呼或其他 endpoint 是新的取捨）。
2. 若 A12 顯示 5x 下回測開倉拒絕率高到使回測結果失去代表性 → **不得**以「數字更正確了」交付。停下來回報，讓使用者決定是否需要先解決回測的保證金/入金建模。
