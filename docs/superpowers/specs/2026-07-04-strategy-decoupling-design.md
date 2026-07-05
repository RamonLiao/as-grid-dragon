# #4 回測/實盤策略脫鉤 — 設計文件（Rev 2）

日期：2026-07-04
狀態：Rev 2 草案（Rev 1 經 quant 視角 review 後修訂；待使用者確認）
前置審查：tasks/notes.md 2026-07-03 架構審查 P0-1；scout 耦合盤點；quant reviewer 獨立審查（1 blocker + 7 major，全數已驗證並反映於本版）

## 問題

回測驗證的不是實盤策略：`backtest/backtester.py:18` import `core.strategy.GridStrategy`（舊系統完整版），實盤跑的是 `grid_engine/bot.py` 的獨立邏輯 + `grid_engine/strategy.py`（簡化版）。兩版 `GridStrategy` 已發散，且實盤 `_place_grid` 不走單一決策入口，散呼叫分方法並交纏 5 個增強模組的副作用。回測優化出的參數對實盤不保證有效。

Review 進一步確認，等價性由三層決定，缺一即失真：
1. **決策數學**（間距/數量/價位/裝死）——本任務主體。
2. **決策時機與錨定**（誰觸發重掛、錨在哪個價）——實盤是追價網格（`_should_adjust_grid` bot.py:584-602：現價偏離上次掛網價 ≥ grid_spacing×0.5 觸發重掛），回測現況是靜態階梯（錨在上次成交價）。此層不統一，同一決策函數仍產出不同成交序列。
3. **輸入的時間語意**——ATR 60 秒牆鐘快取（enhancements.py:802-808）、volume「近 60 秒」窗口（enhancements.py:1004）在回放時間軸下會壞掉。

## 目標

1. 實盤與回測吃**同一個純函數決策層**，涵蓋上述第 1、2 層（決策數學 + 重掛/錨定判斷）。
2. 純層涵蓋：核心網格決策（網格價位、裝死、止盈加倍）+ 動態間距與數量調整（manager 輸出作為結構化快照輸入）+ `_should_adjust_grid` 追價錨定判斷。
3. 第 3 層以 **sim-clock 注入**解決：`grid_engine/clock.py` 提供 `now()`，manager 內部 `time.time()` 改走它；實盤預設真實時間（行為不變），回測以 K 線時間驅動——ATR/volume/funding 在回測中真正有效。
4. **實盤行為零改變**——硬約束。純層是「搬移」現行 grid_engine 邏輯（含 bug-for-bug，見 GLFT 條目），不是重寫、不採 core 版語意。
5. 移除 `backtest/backtester.py` 對 `core.strategy` 的依賴（為 #9 鋪路）。
6. **強驗收：實盤決策日誌 + 離線重放**。實盤每次 `decide()` 落地一行 JSON（`DecisionInputs` + `GridDecision`），離線用同一 `decide()` 逐筆重放比對。這是唯一能驗證「快照捕捉完整性」的手段（函數級一致性測試是套套邏輯，防不了兩邊吃同一個殘缺快照）。

## 非目標與明示的保真限制

- **Bandit 閉環不重現**：實盤 bandit 每 tick 依線上回饋覆寫 `sym_config` 間距（bot.py:648-653）；回測以固定參數評估決策函數，不含 bandit 閉環。回測結論適用於「給定參數的策略」，不含參數自適應層。
- **flat-entry 開倉不進純層**：零倉位 bootstrap（bot.py:667-674/683-690，掛 best_bid/ask + 10s 冷卻 + 封鎖期判斷）依賴守衛與時間，留在 bot；回測的 bootstrap 開倉由 backtester 以對應規則模擬（掛 K 線 close，視為 taker 進場），差異寫入限制。
- **`_check_and_reduce_positions`**（bot.py:559-580）不進純層（依賴即時成交/掛單）；回測沿用現有成交模擬。future work。
- **DGT (`DGTBoundaryManager`)** 留在 bot：`check_and_reset` 是狀態機且 `get_adjusted_spacing` 現為 no-op（enhancements.py:646-647），對決策零影響。明確不搬，防 plan 階段誤搬。
- **下單守衛/退避/斷路器/cooldown**：執行層，不進純層，回測不重放。
- 回測成本模型（滑價、資金費率損益）→ #5。
- MaxGridBot 拆分 → #7。
- 成交模擬保真升級（queue position、partial fill）→ 不在本任務；現行 backtester「價格穿越即全量成交」的樂觀偏差寫入限制，#5 一併檢討。

## 方案選擇

| 方案 | 說明 | 取捨 |
|------|------|------|
| **A'. 純函數模組 + sim-clock + 錨定納入（採用，Rev 2）** | `grid_engine/decision.py` 搬移實盤邏輯；`clock.py` 注入時間；`_should_adjust_grid` 入純層 | Rev 1 的 A 經 review 證明「只統一葉子數學」時回測仍失真；本版補齊時機與時間兩層 |
| B. 統一到 core/strategy.py 完整版入口 | 回測已在用，改動最小 | 讓實盤改走舊系統語意 = 改變正在賺錢的行為，方向錯誤 |
| C. 全保真重放（守衛/退避也抽象） | 最徹底 | 守衛在回測中無意義，overengineering |
| D. 時間型 manager 回測退化中性值 | 免 sim-clock | 動態間距在回測無效，實質退回「只抽核心」，P0 動機打折。AskUserQuestion 逾時，依推薦棄此選 A' |

## 架構

### `grid_engine/clock.py`（新，~10 行）

```python
_now_fn = time.time
def now() -> float: return _now_fn()
def set_clock(fn) -> None: ...   # 回測注入 K 線時間；實盤不呼叫
```

`enhancements.py` 內所有 `time.time()`（ATR 快取、volume 窗口、funding 更新間隔、leading 視窗）改 `clock.now()`。實盤預設即 `time.time()`，零行為差異。bot.py 的守衛時間（cooldown、封鎖期）**不改**——守衛不進回測。

### 新模組 `grid_engine/decision.py`（純函數，無 I/O、不寫任何物件）

```python
@dataclass(frozen=True)
class EnhancementSnapshot:
    """manager 在本 tick 的輸出快照。實盤收集時必須逐字複刻現行呼叫序列（見等價性保障 3）。
    欄位為示意，plan 階段對照 manager 原始碼定案；原則：輸出進快照，狀態機留外面。"""
    leading_pause: bool                # should_pause_trading()
    leading_pause_reason: str
    leading_adjusted_gs: float | None  # get_spacing_adjustment() 的絕對間距值
    leading_reason: str                # 驅動「ATR 是否套用」分支（bot.py:487），必須保留
    atr_tp: float | None               # DynamicGridManager.get_dynamic_spacing() 回 (tp, gs) 對
    atr_gs: float | None
    funding_long_bias: float           # FundingRateManager.get_position_bias()
    funding_short_bias: float
    # 明確排除：
    # - GLFT bid/ask skew：實盤 bot.py:499 算完即丟（已驗證純計算、無副作用）= 死代碼。
    #   bug-for-bug 不進快照；「讓 skew 生效」另開 follow-up，屬策略變更非重構。
    # - GLFT adjust_order_quantity 是純函數，直接搬進 decision.py，不走快照。
    # - UCBBandit：改寫 config 間距，發生在快照前，屬參數選擇非單 tick 決策。
    # - DGT：no-op，留 bot。

@dataclass(frozen=True)
class DecisionInputs:
    price: float                   # 實盤 = (bid+ask)/2（bot.py:787）；回測 = K 線 close
    long_position: float
    short_position: float
    buy_long_orders: float         # _should_adjust_grid 需要四向掛單數
    sell_long_orders: float
    buy_short_orders: float
    sell_short_orders: float
    last_grid_price_long: float    # 追價錨點
    last_grid_price_short: float
    long_dead_mode: bool
    short_dead_mode: bool
    grid_spacing: float            # bandit 覆寫後的值
    take_profit_spacing: float
    initial_quantity: float
    position_threshold: float
    position_limit: float
    enh: EnhancementSnapshot
    # 不含 best_bid/best_ask：_place_grid 只用 mid（flat-entry 才用 bid/ask，不在純層）。

@dataclass(frozen=True)
class OrderIntent:
    side: str; position_side: str; price: float; quantity: float; reduce_only: bool

@dataclass(frozen=True)
class SideDecision:
    should_adjust: bool            # _should_adjust_grid 結果；False = 本 tick 不動作
    enter_dead_mode: bool          # 供 bot 寫回 dead_mode flag 與 log
    exit_dead_mode: bool
    cancel_side: bool              # 非裝死路徑的無條件撤舊
    orders: tuple[OrderIntent, ...]
    new_anchor_price: float | None # 供 bot 寫回 last_grid_price_*
    dynamic_tp: float
    dynamic_gs: float
    display: dict                  # leading_ofi/volume_ratio/spread_ratio/signals/inventory_ratio
                                   # 等面板欄位，bot 決策後寫回 sym_state（現行 bot.py:466-469,508）

@dataclass(frozen=True)
class GridDecision:
    long: SideDecision
    short: SideDecision

def decide(inputs: DecisionInputs) -> GridDecision: ...
```

### 搬移對照（來源 grid_engine/bot.py，行號為 2026-07-04 現狀）

| 現行位置 | 去處 | 備註 |
|----------|------|------|
| `_should_adjust_grid` (584-602) | `decision.py` | 已是純函數，直接搬 |
| `_get_dynamic_spacing` (450-515) | `decision.compute_spacing` | manager 呼叫改讀快照；sym_state 寫入改回傳（`display`） |
| `_get_adjusted_quantity` (517-557) | `decision.compute_quantity` | GLFT qty 調整為純函數直接搬；funding 讀快照 |
| `_place_grid` (698-770) 決策半段 | `decision.decide_side` | 下單/撤單半段留 bot |
| `grid_engine/strategy.py` 全部 | 併入 `decision.py` | 之後刪檔 |

### 實盤接線（bot.py）

`_grid_step` 內：bandit 覆寫 config（不動）→ **snapshot**（symbol lock 內、無 await；逐字複刻現行 manager 呼叫序列）→ `decide()` → **execute**（依 `SideDecision` 撤單/下單走既有 `_rest` 守衛路徑；寫回 dead_mode/anchor/dynamic/display 欄位）→ 決策日誌落地一行 JSON。鎖序、skip-if-locked、守衛全不動。

### 回測接線（backtest/backtester.py）

- 移除 `core.strategy` import，改吃 `grid_engine.decision.decide()`。
- 回測 loop 每根 K 線：`clock.set_clock` 推進至 K 線時間 → 以 K 線資料餵真 manager 實例（`update_price` 等既有介面）→ 組快照 → `decide()` → 模擬撤掛與成交。
- 重掛迴圈改用純層 `should_adjust` + `new_anchor_price`——回測從靜態階梯改為與實盤相同的追價網格語意。
- 成交判定沿用「價格穿越掛單價即成交」；其樂觀偏差（無 queue、無 partial fill）寫入報告說明。
- `grid_engine/backtest.py`（325 行死碼）刪除。

### 遷移相容性 audit（plan 階段第一步）

core 版 `get_grid_decision` 有 `dead_mode_enabled`/`fallback_long`/`fallback_short` 參數（backtester.py:233-235 在用），grid_engine 版硬編 1.05/0.95 無開關。遷移前盤點所有回測/optimizer config 是否設過這三參數；有則在純層補等價開關（預設值 = 實盤現行為），無則直接遷移。

## 等價性保障

1. **Characterization tests 先行**：重構前對 `_place_grid`/`_get_dynamic_spacing`/`_get_adjusted_quantity`/`_should_adjust_grid` 寫行為快照測試（mock managers 固定輸出，斷言撤/下單完整參數與 sym_state 寫回），覆蓋分支：pause、leading 調整、ATR 套用/跳過、裝死進/出、加倍條件、qty 下限 clamp。搬移後同組測試不改而綠。
2. **Manager 呼叫序列等價**：`get_signals` 系列在現行碼被呼叫 3 次且每次 append OFI history（有狀態 deque，enhancements.py:985）——快照收集若改變呼叫次數，manager 內部狀態演化漂移，影響未來 tick。用真 manager 實例 + call-recording 測試斷言搬移前後呼叫序列與 manager 狀態演化一致。
3. **決策日誌重放**（強驗收）：實盤 `decide()` 每次落地 `DecisionInputs`+`GridDecision` JSON 一行；離線重放逐筆比對。上線後跑 ≥24h 作為最終驗收。
4. sim-clock 預設 `time.time()`，實盤路徑位元級等價。

## 測試計畫

- `tests/test_decision.py`：純函數單測（表格驅動：裝死邊界、加倍、間距分支耦合、追價觸發、qty clamp）。
- Characterization + 呼叫序列等價（上述 1、2）。
- 重放工具 + 其自身測試（上述 3）。
- Monkey testing（專案規則）：極端 `DecisionInputs`——price=0/NaN/負、倉位遠超 limit、spacing 極端、快照欄位全 None/極值、錨價為 0——斷言不拋例外、輸出在合理域；回測端用人造極端 K 線（跳空 50%、零成交量、時間倒流）打 sim-clock 與 manager。

## 交付順序（供 writing-plans 拆解）

1. 相容性 audit（dead_mode_enabled/fallback 使用盤點）。
2. Characterization tests 鎖死現行為。
3. `clock.py` + enhancements.py 換 `clock.now()`（實盤等價，測試證明）。
4. `decision.py` 搬移四個方法 + 併 strategy.py；bot 接線 + 決策日誌；characterization 綠 + 呼叫序列等價綠。
5. 刪 `grid_engine/strategy.py`，修 import。
6. backtester 遷移：`decide()` + sim-clock + manager 歷史驅動 + 追價重掛語意；移除 core.strategy 依賴；刪 `grid_engine/backtest.py`。
7. 重放比對工具 + 純函數單測 + monkey testing。
8. 上線觀察：決策日誌重放 ≥24h 零 diff 作為最終驗收。

## 未決事項（使用者確認點）

- Rev 2 三個關鍵決定係 AskUserQuestion 逾時後採推薦選項，均可否決：(a) sim-clock 注入（vs 時間型 manager 退化中性值）；(b) `_should_adjust_grid`+錨定納入純層（vs 只統一葉子數學）；(c) 決策日誌重放驗收（vs 只靠測試）。
- `EnhancementSnapshot` 確切欄位在 plan 階段對照 manager 原始碼定案。
- 回測 K 線粒度是否足以餵 LeadingIndicator 的 OFI/spread 視窗（需 tick 級 bid/ask？）在 plan 階段驗證；粒度不足的 manager 在回測中退化為中性值並在報告中明示。
- GLFT price skew 死代碼：本任務 bug-for-bug 保留；「使其生效」是策略變更，另開 issue 由使用者決定。
