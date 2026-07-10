# 回測決策層對齊 — 設計規格

- 日期：2026-07-10
- 狀態：設計核可，待實作計畫
- 起因：progress.md「本輪衍生的未決事項」第 1 項的前置 blocker
- 相關：#4 回測/實盤策略脫鉤（`2026-07-04-strategy-decoupling-design.md`）、#11 裝死模式死鎖

---

## 1. 背景與前提修正

progress.md 記載的 blocker 是：

> `backtest/backtester.py` 是**獨立實作**，不共用 `decision.py`，沒有裝死模式邏輯（也沒有 RiskMonitor）。

**這個前提對主路徑而言是錯的。** 本次調查（唯讀，逐條驗證）確認：

| 宣稱 | 實況 | 證據 |
|---|---|---|
| backtester 不共用 `decision.py` | **主路徑 `_run_terminal_ui_mode` 完整呼叫 `decide()`** | `backtester.py:696` |
| 沒有裝死模式邏輯 | 完整消費 `enter_dead_mode`/`exit_dead_mode`/`cancel_side`/`orders`/`new_anchor_price` | `backtester.py:702-715` |
| 沒有 RiskMonitor | 屬實 | `backtester.py` 無對應呼叫 |

blocker 描述的其實是 `_legacy_grid_decision`（`backtester.py:48`）—— 只有 `terminal_ui_mode=False` 或 `initial_quantity <= 0` 才會走的 deprecated 路徑（路由在 `backtester.py:512-515`）。它確實是獨立實作。

> **這是本 repo 第三次踩到「同檔多執行路徑」的坑**（見 `lessons.md` 2026-07-06「引用行號驗證語意前，先確認該行屬於哪條執行路徑」）。因為 `Config.initial_quantity` 預設是 `0.0`（`backtest/config.py:27`），legacy 路徑至今仍可被靜默觸發，所以它沒被刪，也就一直是誤讀來源。

真正的阻礙比原先認知的小，但**不是零**，而且分布在三個不同的地方。

---

## 2. 三個旋鈕的實際狀態

使用者要用回測定奪的三個選項，沒有一個是「改個值就能測」：

| 選項 | live 現況 | backtest 現況 | 真正的阻礙 |
|---|---|---|---|
| (a) 調高 `threshold_multiplier` | ✅ 可調，生產值 20.0 | ✅ 可調，但預設 14.0 | 只需對齊預設值 |
| (b) 關掉裝死模式 | ❌ **`grid_engine` 無此欄位** | ⚠️ 欄位存在但只有死路徑讀 | 純層 `decision.py:168` 是無條件 `if is_dead_mode(...)`，**沒有 gate** |
| (c) 開啟 `glft_enabled` | ⚠️ 被總開關擋住 | ❌ 硬編 `MaxEnhancement()` 預設值 | `backtester.py:546` 不吃 config |

證據：
- `dead_mode_enabled` 全 repo 只出現在 `backtest/config.py:57`（定義）與 `backtester.py:52`（`_legacy_grid_decision` 讀取）。`grid_engine/config.py` 無此欄位，`decision.py` 無 gate。
- `position_threshold = initial_quantity × threshold_multiplier`，兩邊同式：`grid_engine/config.py:52`、`backtester.py:541`。
- `backtester.py:546` 是 `self._max_enh = MaxEnhancement()`，完全不吃 `Config`。

### 2.1 `glft_enabled` 不是一個開關，是兩個

`is_feature_enabled()`（`grid_engine/enhancements.py:69-73`）有總開關前置：

```python
if not self.all_enhancements_enabled:
    return False
return getattr(self, f"{feature}_enabled", False)
```

生產 `config/trading_config_max.json` 的 `max_enhancement.all_enhancements_enabled` 是 `false`。**單獨把 `glft_enabled` 設成 `true` 什麼都不會發生** —— `decide()` 拿到的 `glft_enabled` 恆為 `False`。

打開總開關的連帶影響（已逐條驗證，查的是 `grid_engine/` 而非舊 `indicators/`）：

| 功能 | gate | 生產旗標 | 開總開關後 |
|---|---|---|---|
| `funding_rate` | `is_feature_enabled`（`enhancements.py:718`） | `false` | 不變 |
| `dynamic_grid` | `is_feature_enabled`（`enhancements.py:849`） | `false` | 不變 |
| `glft` | `is_feature_enabled`（`enhancements.py:756,776`） | `false` | **啟動** |
| `leading_indicator` | **不 gate 在總開關**，走 `config.leading_indicator.enabled` | `true`（已啟動） | 不變 |
| bandit 覆寫 `gamma` | `bot.py:359-360` 直接讀 `all_enhancements_enabled` | `bandit.enabled: true` | **啟動** |

結論：開總開關 ⇒ GLFT 生效，**且 `gamma` 從固定 0.1 變成 bandit 每 tick 覆寫的浮動值**。見 §6 風險 1。

### 2.2 GLFT 在數學上救不了庫存失衡

以生產 `logs/decisions.jsonl` 前 80000 筆實算（有效樣本 67103）：

```
inventory_ratio   median 0.871   min 0.657   max 1.000
glft_enabled 實際值：恆 False        gamma：恆 0.1
```

`inventory_ratio` **從未低於 0.657** —— 極度偏多，且無均值回歸跡象。

`glft_quantity()`（`decision.py:107-113`）的調整因子是 `1 - inv × gamma`，再 `clamp(0.5, 1.5)`；`compute_quantity()` 尾端還有 `max(initial_quantity × 0.5, q)` 地板（`decision.py:140`）。代入生產值：

- `gamma = 0.1, inv = 0.871` → 多頭開倉量 × **0.913**（減 8.7%，幾乎無作用）
- `gamma → ∞` → clamp 下限 0.5 擋住 → 多頭開倉量**最多砍到一半**

而且 `glft_quantity` 只作用在**開倉單**（`compute_quantity` 的 `is_take_profit=False` 分支），止盈單不受影響。

**GLFT 永遠不會停止買入，更不會賣出。** 目前唯一會讓開倉單完全停掉的機制是裝死模式（`decision.py:168-180` 的 dead 分支只掛止盈、不掛 entry）。

因此 **選項 (c) 單獨使用，風險嚴格劣於現狀**：倉位仍單調成長，只是慢一半，直到撞保證金。它只有搭配 (a) 才有意義 —— 用 GLFT 減速，換取更晚觸發裝死。回測應證實或推翻這個預測。

> 這是控制理論的 actuator saturation：`clamp(0.5, 1.5)` 把控制器的權威上限鎖死，執行器飽和後，控制器增益調得再高也沒用。裝死模式則是 bang-bang controller，有足夠權威但沒有中間狀態 —— 一進去就完全不吃網格，正是使用者抱怨的症狀。

---

## 3. 回測保真度缺口

逐條驗證後剔除兩個誤報（`funding_rate` 與 `dynamic_grid` 在生產也是關的，回測 `MaxEnhancement()` 預設全關，**兩邊一致，不是缺口**）。真正的缺口三個：

### G1. `pos == 0` 時實盤繞過 `decide()`

`bot.py:395-401`（多頭，空頭對稱於 `409-416`）：

- **live**：`position == 0` 且未被斷路器封鎖且距上次下單 > 10s → 撤該側單 → 掛在 `best_bid`（貼價、積極）
- **backtest**：走純層 `else` 分支，掛在 `price × (1 - grid_spacing)`（被動、低一格）

已記載於 `backtester.py:36` FIDELITY_NOTES 第 (5) 條。**這條偏離正落在「網格吃不吃得到波動」的正中心** —— pos==0 的積極貼價重掛就是網格的填充行為。要用回測估計「關掉裝死後多頭多久吃回倉位」，這是一級變數，不是可接受的近似。

抽不進純層的原因是它需要 `best_bid`（盤口）與 `time.time()`（節流）。**但時間與盤口都是「值」，不是副作用** —— 把它們當輸入餵進 `DecisionInputs` 即可保持純度。純度的定義是「不自己去讀時間」，不是「不知道時間」。

### G2. RiskMonitor 缺席

`risk_monitor.py:20-64` `check_trailing_stop()`：
- 帳戶層閘門 `state.margin_usage >= risk.margin_threshold`（生產 0.5）
- 逐 symbol：`unrealized_pnl >= trailing_start_profit`（5.0）→ 開始追蹤
- 追蹤中回撤 `>= max(trailing_min_drawdown, peak × trailing_drawdown_pct)`（`max(2.0, peak × 0.1)`）→ 呼叫 `close_symbol_positions()` **全平該 symbol 多空倉**

**這是除了裝死模式以外，唯一會減少多頭庫存的機制**，且它會直接重置 `inventory_ratio` 與裝死狀態。回測缺它 ⇒ 高估持倉、低估已實現獲利、「關掉裝死模式」的回測看起來會比實際危險。

`risk_monitor.py:66-89` `check_and_reduce_positions()`：觸發條件是 `long >= 0.8T **and** short >= 0.8T`。在 `inv` 中位數 0.871 的偏多分布下幾乎不可能同時成立 —— 生產資料證實了 progress.md 已記載的「單邊崩盤不會觸發減倉」。

兩者節奏不同：`check_trailing_stop` 掛在 `sync_service.py:151`（每 `sync_interval` = 10s），`check_and_reduce_positions` 在 `bot.py:368`（每 tick）。純層須拆成兩個函數。

### G3. leading indicator 缺席（本次不修）

生產 `leading_indicator.enabled: true`，會在 pause 時把 tp/gs 雙雙 ×2（`snapshot.py:56-60`）。回測 `_build_bundle`（`backtester.py:520-527`）硬編 `leading_enabled=False`。

**結構性補不了**：leading 吃 OFI / volume_ratio / spread_ratio，需要逐筆成交與盤口資料；回測只有 1m K 線。維持 FIDELITY_NOTES 揭露。

---

## 3bis. 回測引擎本身的缺陷（量化 review 追加，嚴重度高於 G1-G3）

以下四項在原始設計中被遺漏。**它們會使三個選項的相對排序由回測缺陷決定，而非由策略決定。** 必須在 Phase A 之前修復（見 §8 Phase 0）。

### G4. 撮合規則有兩個錯，方向相反、量級不等

`_settle()`（`backtester.py:622-637`）：

```python
crossed = price <= e["price"]                  # price = 該根 close
if crossed and _open(side, price, e["qty"])    # ← 以 close 成交，不是以掛單價
```

- **錯誤一**：只看 close 判斷穿越。限價單盤中被觸及即成交，不需收盤站上去。
- **錯誤二**：成交價取 close 而非 limit。掛在 100 的買單、close 跌到 98 → 回測讓你用 98 買到。這是**不存在的免費價格改善**，且 close 是掛單當下的未來資訊。

以 `data/futures/um/daily/klines/BNBUSDC/1m/` 的 **44107 根真實 1m K 線**、用實盤有效間距 `0.003`（見 G5）實算多頭補倉單：

| 量測 | 值 |
|---|---|
| 真實觸及（`low <= limit`） | 167 次（0.38% of bars） |
| 回測成交（`close <= limit`） | 86 次（0.19% of bars） |
| **回測漏掉的成交** | **81 次 = 真實成交的 48.5%** |
| **每筆免費價格改善** | mean **10.38 bps** / median 6.92 / p90 25.14 |
| 對照 `slippage_bps` | 1.00 bps |
| 對照 `fee_pct` 單邊 | 4.00 bps |

**幻覺價格改善是所建模滑價的 10 倍、單邊手續費的 2.6 倍。** 一次網格來回毛利 = `take_profit_spacing` = 30 bps，進場與止盈各拿約 10 bps 幻覺改善 ⇒ **約 20 bps 假獲利，佔毛利三分之二**。

⇒ **FIDELITY_NOTES 第 (8) 條「保守堆疊 → 偏低估、屬刻意保守下界」是錯的。** 實況是「每筆成交偏樂觀、成交筆數偏少」，兩個誤差方向相反且量級差約 2 倍，**淨偏誤不可預測**——比單向偏誤更糟，因為無法宣告下界。

> 根因：`slippage_bps` 是為 **taker** 設計的模型（成交價比預期差）。網格是 **maker**，maker 的風險是**逆選擇**與**排隊落空**，不是滑價。用滑價代理逆選擇，量級差一個數量級。

**修法**（標準限價單回測）：
- 多頭進場：`low <= limit` → 成交於 **`limit`**；多頭止盈：`high >= tp` → 成交於 **`tp`**
- 空頭對稱
- 同根 K 線內進場與止盈皆觸及 → 保守假設**不利者先成交**（進場先）

### G5. 回測跑的不是實盤策略：bandit 無條件覆寫間距

`bot.py:355-358`（`bandit.enabled: true`，**不需要 `all_enhancements_enabled`**）：

```python
sym_config.grid_spacing = bandit_params.grid_spacing
sym_config.take_profit_spacing = bandit_params.take_profit_spacing
```

生產 `logs/decisions.jsonl` 前 60001 筆實測：

| 參數 | 實盤實際值 | `trading_config_max.json` |
|---|---|---|
| `grid_spacing` | **0.003**（60001/60001 筆） | 0.006 |
| `take_profit_spacing` | **0.003**（60001/60001 筆） | 0.004 |

實盤跑的是 **0.003/0.003 對稱網格**；config 那組 0.006/0.004 **從未生效**。

FIDELITY_NOTES 第 (4) 條「Bandit 參數優化不在回測 loop 內重現」**低估了嚴重性** —— 不是「優化不重現」，是**間距參數整個對不上**。此缺口影響**全部三個選項**，非僅 GLFT。

**修法**：回測參數釘在實盤有效值 `0.003/0.003`，並明文揭露「bandit 被 pin 住」。

> **獨立可疑點（另案追查，不在本次範圍）**：60001 筆全部同值，而 `thompson_enabled: true`、`update_interval: 10` 的 bandit 理應探索。要嘛 bandit 卡住（潛在 bug），要嘛已收斂。無論何者，回測都必須用實測有效值而非 config 值。

### G6. 沒有爆倉建模 ⇒ 選項 (b) 根本不可評估

`_open()`（`backtester.py:586-593`）在 `margin + fee >= balance` 時 `return False`，掛單留到下根再試。**倉位永遠不會被強平**；`equity = balance + unrealized` 可為負，回測照跑到底。

選項 (b)「關掉裝死模式」的全部風險就在這裡：**無限加倉 + 不爆倉 = 必勝策略**。回測必然告訴你關掉裝死最好。這不是策略結論，是 martingale 在無限資金假設下的算術恆等式。

**修法**：建模強平事件 —— `equity <= maintenance_margin` → 當根價格全平、記 `liquidated=True`、終止回測。任何 `liquidated=True` 的參數組**一票否決**，不進入優化目標函數。

### G7. 成本模型不是方向中性的，它系統性偏袒「保留裝死模式」

`fee_pct = 0.0004` 是 **taker** 費率；網格全是 maker 單（Binance USDⓈ-M VIP0 maker = 0.02% = 2 bps）。**回測對每筆成交多收一倍手續費**，且 `slippage_bps` 同樣按成交次數收。

高換手選項（關掉裝死、調高 threshold）被多罰，低換手選項（維持現狀）被少罰。**既然要比較的正是換手率差異巨大的三個選項，成本模型的偏差會直接決定排序。**

**修法**：`fee_pct` 改 maker 費率；三選項一律做 **cost sensitivity 網格**（fee ∈ {2, 4} bps × slippage ∈ {0, 1, 2} bps）。**若排序在此範圍內翻轉，則不得下結論。**

---

## 4. 目標與非目標

### 目標

0. **先讓回測引擎本身可信**（G4-G7）—— 撮合、強平、參數對齊、成本中性。這是其餘一切的前提
1. 讓回測能誠實比較 (a)(b)(c) 三個選項，產出可信數字，**且結論通過樣本外驗證**
2. 消除 `decide()` 的最後一個洞（G1），讓「決策層單一真理來源」名副其實 —— 完成 #4 沒做完的一半
3. 讓 `RiskMonitor` 的決策也成為 live/backtest 共用的純層（G2）
4. 移除會誤導人的假旋鈕

### 非目標

- **不刪 `_run_legacy_mode` / `_legacy_grid_decision`**：`Config.initial_quantity` 預設 `0.0`，刪除會改變靜默降級語意，超出本次範圍。改為加註 deprecated docstring，明寫它不是實驗路徑。
- **不補 leading indicator 到回測**（G3，結構性缺資料）
- **不收斂 `glft_controller` 重複實作**：`ManagerBundle.glft_controller` 其實無人使用（`snapshot.py` 不碰它，`decision.py:107` 有自己的 `glft_quantity()`）。記為技術債。
- **不改 bandit 與 gamma 的耦合**（見 §6 風險 1）
- 不做參數決策本身 —— 本次只交付「能回答問題的工具」，數字下一輪再跑

---

## 5. 架構

### 5.1 `grid_engine/decision.py`

`DecisionInputs` 新增欄位：

```python
dead_mode_enabled: bool = True      # 向後相容預設
now: float = 0.0                    # caller 注入（live: time.time()；backtest: bar epoch）
last_order_time_long: float = 0.0
last_order_time_short: float = 0.0
best_bid: float = 0.0
best_ask: float = 0.0
```

`_decide_side()` 兩處改動：

1. **裝死 gate**：`if inputs.dead_mode_enabled and is_dead_mode(my_pos, inputs.position_threshold):`
2. **`pos == 0` 分支**（置於 `should_adjust` 檢查之前，對應 live 的 `if sym_state.long_position == 0` 優先於 `elif need_long`）：
   - 節流未過（`now - last_order_time <= 10`）→ 回傳 `should_adjust=False` 的空決策（`new_anchor_price=None`，對應 live 未進分支時不更新 anchor）
   - 節流已過 → `should_adjust=True`、`cancel_side=True`、一張 entry `OrderIntent`（價格取 `best_bid`（long）/ `best_ask`（short），數量走 `compute_quantity(inputs, side, is_take_profit=False)`）、`new_anchor_price=price`（對應 `bot.py:402` 的 `last_grid_price_long = price`）

   節流常數 `10` 提升為模組常數 `_ZERO_POS_REPLACE_INTERVAL = 10.0`（現行硬編於 `bot.py:397,411`）。

   > **`last_order_time_*` 的語意**：live 的 `pos==0` 節流讀的是 `last_order_times[f"{sym}_long"]`（`bot.py:397`），與網格路徑的 `last_order_times[f"{sym}_long_grid"]`（`bot.py:405`）是**不同的鍵**。`DecisionInputs.last_order_time_long/short` 對應前者，caller 不得混用。

   > **斷路器語意**：live 的 `pos==0` 分支額外檢查 `not order_blocked`（`bot.py:396`）。斷路器是 I/O 層狀態，**不進純層** —— caller 維持在執行前檢查 `order_executor.is_blocked()`，與現行 `_place_grid` 路徑一致。

   > **已知 inert 殘留（刻意保留）**：live 在 `pos == 0` 時 `dead_flag` 不會被清除（因為根本不執行 `_execute_side_decision`）。純層的 `pos==0` 分支**同樣不動 dead 旗標**，以維持等價。該狀態會自癒：下次有倉位且 `pos <= threshold` 時，`else` 分支發出 `exit_dead_mode=True`。此處刻意不「順手修正」，否則 G-C1 的 characterization 會失去等價證明的意義。

`SideDecision` 不變（既有欄位足以表達 `pos==0` 決策）。

**純度不變**：`now` / `best_bid` / `best_ask` 是輸入值，純層不自行讀取時間或盤口。`decide()` 仍為無 I/O、無副作用的純函數，`replay.py` 的重放契約不受影響（新欄位隨 `inputs` 一併落地 `logs/decisions.jsonl`）。

### 5.2 `grid_engine/risk.py`（新檔，純層）

對應兩種節奏，兩個純函數：

```python
@dataclass(frozen=True)
class TrailingInputs:
    enabled: bool
    margin_usage: float
    margin_threshold: float
    unrealized_pnl: float
    trailing_active: bool
    peak_pnl: float
    trailing_start_profit: float
    trailing_drawdown_pct: float
    trailing_min_drawdown: float

@dataclass(frozen=True)
class TrailingDecision:
    reset: bool                  # margin_usage < threshold → 清 trailing_active / peak_pnl
    activate: bool               # 開始追蹤
    new_peak: Optional[float]    # 創新高
    flatten: bool                # 觸發全平
    deactivate: bool             # 全平後關閉追蹤

def decide_trailing(inputs: TrailingInputs) -> TrailingDecision: ...


@dataclass(frozen=True)
class ReduceInputs:
    long_position: float
    short_position: float
    position_threshold: float
    now: float
    last_reduce_time: float

@dataclass(frozen=True)
class ReduceIntent:
    long_qty: float
    short_qty: float

def decide_reduce(inputs: ReduceInputs) -> Optional[ReduceIntent]: ...
```

`RiskMonitor` 退化成薄執行層：讀 `state` → 組 inputs → 呼叫純函數 → 依決策執行下單與寫回 state。行為零變更（Phase A 守門）。

`REDUCE_COOLDOWN = 60`、`local_threshold = threshold × 0.8`、`reduce_qty = threshold × 0.1` 三個常數搬進 `risk.py`。

### 5.3 `backtest/`

**`backtest/config.py`**
- 新增：`all_enhancements_enabled: bool = False`、`glft_enabled: bool = False`、`gamma: float = 0.1`
- 新增：`dead_mode_enabled: bool = True`（已存在，改為主路徑真正消費）
- 新增：`risk_enabled` / `margin_threshold` / `trailing_start_profit` / `trailing_drawdown_pct` / `trailing_min_drawdown`。**`risk_enabled` 在 Phase A 預設 `False`**（維持現行零風控行為），Phase B 才改為 `True` 對齊生產 —— 見 §8。其餘預設對齊生產 `risk` 區塊。
- 修改：`threshold_multiplier` 預設 `14.0` → `20.0`（對齊 `grid_engine/config.py:30`）
  > 這是 default 變更，對**直接 `Config()` 建構且未指定該欄**的呼叫端才有影響。已查證所有生產呼叫端都顯式傳值（`web/services/backtest_service.py:47`、`smart_optimizer.py:307`、`grid_engine/backtest.py:148`），僅測試受影響。
- **刪除**：`dead_mode_fallback_long` / `dead_mode_fallback_short`（純層硬編 `1.05`/`0.95`，此二欄從未生效）
- **刪除**：static `position_threshold` / `position_limit`（主路徑走 `initial_quantity × multiplier`，`backtester.py:541`；此二欄只有死路徑讀）

> 刪除理由：**假旋鈕比缺旋鈕更危險** —— 它讓人以為實驗做過了。這與 progress.md「#10-B 判死後那三欄唯讀揭露應移除」是同一條原則。

**`backtest/backtester.py`**
- `_max_enh` 改由 `Config` 建構：`MaxEnhancement(all_enhancements_enabled=cfg.all_enhancements_enabled, glft_enabled=cfg.glft_enabled, gamma=cfg.gamma)`
- `DecisionInputs` 補新欄位：`dead_mode_enabled=cfg.dead_mode_enabled`、`now=epoch`、`best_bid=best_ask=price`（1m K 線無盤口，用 close 代理，揭露）、`last_order_time_*` 由回測自行維護
  > 1m K 線的間隔（60s）恆大於節流窗（10s），故 `pos==0` 節流在回測中**永不 binding**，每根 K 線都會重掛。這是與 live 的已知節奏差異，方向中性。
- **（Phase A）** `margin_usage` 建模：`(long_pos + short_pos) × price / leverage / equity`，`equity <= 0` 時定義為 `inf`，避免除零。Phase A 僅**觀測**（餵 `peak_margin_usage`），不參與任何決策。
- **（Phase B）** 每根 K 線接 `risk.decide_trailing()` 與 `risk.decide_reduce()`，開始**消費** `margin_usage`（節奏差異見 §6 風險 3）
- `_legacy_grid_decision` / `_run_legacy_mode` 加 deprecated docstring

**`BacktestResult`** 新增三欄（驗收指標，§7）：
```python
dead_mode_pct_long: float = 0.0     # 裝死狀態的 bar 數 / 有效 bar 總數
dead_mode_pct_short: float = 0.0
peak_margin_usage: float = 0.0      # 全程 margin_usage 最大值（強平距離代理）
```
三者皆為**純觀測欄位**，不參與決策，故新增本身不構成行為變更（Phase A 即可加入）。`dead_mode_pct_*` 的分母是通過髒資料防禦（`backtester.py:645`）的有效 bar 數。

### 5.4 `grid_engine/config.py`

`SymbolConfig` 新增 `dead_mode_enabled: bool = True`，並在 `to_dict()` / `from_dict()` 接線。

生產 `trading_config_max.json` 無此鍵 → `from_dict` 取預設 `True` → **行為零變更**（向後相容）。

---

## 6. 已知風險與必須揭露的事項

1. **bandit 覆寫策略參數**（Phase 0 部分處理，其餘揭露）
   `bot.py:355-360`，`bandit.enabled: true` 時每 tick 覆寫：
   - `grid_spacing` / `take_profit_spacing`：**無條件覆寫**，實測恆為 `0.003/0.003`（見 G5）。Phase 0 以 pin 住實測值處理。
   - `gamma`：額外需 `all_enhancements_enabled`。回測沒有 bandit，`gamma` 固定。

   ⇒ **開 GLFT 的回測結果不能直接對標實盤**，除非實盤同時關掉 `bandit.enabled`。
   ⇒ 回測全程是「bandit 被凍結在某一 arm」的假設。若 bandit 在實盤重新探索，回測結論即失效。
   若日後決定採用 GLFT，須先處理此耦合（讓 bandit 不覆寫 `gamma`，或讓回測重現 bandit）。

2. **GLFT 的天花板**（預期結論，回測應證實）
   `clamp(0.5, 1.5)` + `max(initial_quantity × 0.5, q)` 地板 ⇒ 多頭開倉量最多砍半，永不停止買入。
   預期：選項 (c) 單獨無效；只有搭配 (a) 才有意義。**若回測結論與此預測相反，先懷疑回測而非結論。**

3. **trailing stop 節奏偏離**
   live 每 10s 檢查（`sync_service.py:151`），回測受限於 1m K 線只能每根檢查一次。低估觸發頻率 ⇒ 回測的已實現獲利偏低、持倉偏高 ⇒ **偏向保守，不會騙人開危險參數**。

3bis. **`margin_usage` 層級不一致**
   live 的 `state.margin_usage` 是**帳戶層**（`state.py:115`，`total_margin / total_equity`，跨 symbol），trailing stop 的閘門讀它。§5.3 的回測建模是**單 symbol**。多 symbol 實盤下 trailing 的觸發時機會完全不同。
   ⇒ 單 symbol 回測的 trailing 結論**不得外推至多 symbol 實盤**。

3ter. **`threshold_multiplier` 一參數兩用，optimizer 無法歸因**
   `decision.py:97`：`if my_position > position_limit or opposite_position >= position_threshold: return base_qty * 2`
   `position_threshold` 同時是**裝死觸發門檻**與**對手側止盈加倍的條件**。調高它 ⇒ 裝死更晚觸發（意圖中）**且**對手側止盈更難加倍（副作用）。回測差異是兩個效應的淨和。
   ⇒ 實驗期間須把止盈加倍的門檻解耦成獨立參數做 ablation，否則不知道自己在調什麼。

4. **`best_bid`/`best_ask` 用 close 代理**
   1m K 線無盤口。回測的 `pos==0` 重掛價 = 該根 close，live = 當下 `best_bid`。點差量級的偏差，方向中性。

5. **leading indicator 仍缺**（G3）。維持 FIDELITY_NOTES 揭露。

6. **`_run_legacy_mode` 仍可達**：`Config.initial_quantity` 預設 `0.0`。實驗配置必須顯式設 `initial_quantity > 0` 且 `terminal_ui_mode=True`。加一道測試守門，確保實驗走的是主路徑。

---

## 7. 驗收指標

指標**分三層**。量化 review 的核心裁定：**`trades_count` 與 `realized_pnl` 不得作為優化目標函數。**

> **為什麼**：攤平加倉策略的已實現獲利**恆為正** —— 只有賺錢的倉位會被止盈平掉，虧損全部躺在未實現裡。用它當目標函數，等於獎勵「把虧損藏進未實現」。這是 martingale 假象的標準形態。`final_equity` 與 `max_drawdown` 不會說謊，因為它們吃未實現損益。

### 第一層 — 主指標（優化目標與淘汰條件）

| 指標 | 來源 | 角色 |
|---|---|---|
| `liquidated` | **新增**（G6） | **布林，一票否決**。任何觸發強平的參數組直接淘汰，不進目標函數 |
| `final_equity` | 既有 | 優化目標 |
| `max_drawdown` | 既有 | 優化目標（equity-based） |

### 第二層 — 決策指標（回答使用者的原始問題）

| 指標 | 來源 | 意義 |
|---|---|---|
| `funding_paid` | 既有，**升級為一級** | 裝死模式的本質是長期持有單邊大倉等反彈；單邊多頭 + 正 funding 下，**funding 是該選項的主要成本**，隨持倉時間線性累積。三選項的 funding 差異可能大於其網格收益差異 |
| `dead_mode_pct_long` / `_short` | 新增 | 直接量化「網格停擺多久」，與生產症狀一一對應（`long in_dead=100%`） |
| `dead_tp_fill_rate` | **新增**（M1） | 裝死特殊止盈單的成交率。`dead_mode_price()` 在 `opposite_position=0` 時是 `price × 1.05`（生產實例 +4.8%）。**若歷史成交率趨近 0，則「裝死模式」實務上等於「無限期持有 + 付 funding」，而非設計文件宣稱的「等極端反彈」** —— 這個數字直接回答使用者的原始問題 |
| `peak_margin_usage` | 新增 | 強平距離代理（見下） |

### 第三層 — 診斷指標（**禁止**作為優化目標）

| 指標 | 為何禁用 |
|---|---|
| `trades_count` / `realized_pnl` | martingale 假象：恆正，可靠無限加倉刷高 |
| `win_rate` / `profit_factor` | 同上，攤平策略天生高勝率 |
| `sharpe_ratio` | `backtester.py:756` 用 1m 報酬 × √525600 年化。網格權益曲線自相關極高，1m 報酬標準差嚴重低估真實波動 ⇒ Sharpe 系統性膨脹。**裝死模式下權益曲線幾乎是價格的線性函數，Sharpe 退化為價格趨勢的度量。** 若要使用，改日報酬或 Newey-West 調整 |

### 關於 `peak_margin_usage` 作為強平距離代理

`fetch_positions()` 回傳的 `liquidationPrice` 目前在 `sync_service.py:60-73` 被丟棄，回測亦無交易所維持保證金階梯表；而 `margin_usage` 已因 trailing stop 必須建模，複用零成本。**誠實命名為代理，不宣稱是真實強平距離。** G6 的 `liquidated` 事件建模落地後，代理不再是唯一風險訊號。

---

## 8. 分階段實作與守門條件

沿用 #4 / #5 已驗證的模式：把「重構」與「刻意行為變更」分開。**每個階段各有一個可證偽的守門條件**；每種刻意變更分屬不同階段，不得混在同一個 diff 裡，否則任一階段的數字變化都無法歸因。

### Phase 0 — 回測引擎保真度修復（**前置，不可跳過**）

G4/G5/G6/G7 是後續所有數字的前提。**在 Phase 0 完成前，回測輸出的任何數字都不得用於策略決策。**

| 子階段 | 內容 | 守門 |
|---|---|---|
| **0-a** | 撮合改為 `high`/`low` 判穿越、成交於**掛單價**；同根雙觸發時不利者（進場）先成交 | **G-0a1**：以 `data/futures/.../BNBUSDC/1m/` 真實 K 線實測，多頭進場成交次數由 86 → 167（±rounding）<br>**G-0a2**：零成本（`slippage_bps=0, fee_pct=0`）下，每筆成交價**嚴格等於**掛單價（斷言無價格改善） |
| **0-b** | 建模強平：`equity <= maintenance_margin` → 當根全平、`liquidated=True`、終止回測 | **G-0b1**：構造必爆參數組（極高 `threshold_multiplier` + `dead_mode_enabled=False` + 單邊趨勢資料），斷言 `liquidated is True` 且回測提前終止<br>**G-0b2**：正常參數組 `liquidated is False`，且結果與未加此邏輯前一致 |
| **0-c** | `fee_pct` 改 maker 費率；回測策略參數釘在實盤有效值 `0.003/0.003`；FIDELITY_NOTES 第 (4)(8) 條改寫 | **G-0c1**：cost sensitivity 網格（fee ∈ {2,4} bps × slippage ∈ {0,1,2} bps）可跑出 6 組結果<br>**G-0c2**：FIDELITY_NOTES 不再宣稱「保守下界」 |

> **G-0a2 是本階段的核心守門**：它把「幻覺價格改善」變成一個可自動偵測的斷言。零成本下若成交價 ≠ 掛單價，測試必紅。

### Phase A — 行為零變更（純重構）

範圍：`dead_mode_enabled` gate（預設 `True`）、抽 `risk.py` 且 `RiskMonitor` 委派之、`backtester` 吃 config、`margin_usage` 純觀測建模、刪死欄位、`BacktestResult` 新增三個觀測欄。

**`backtest/config.py` 的 `risk_enabled` 在本階段預設 `False`，backtester 不呼叫 `risk.py`。**

守門（皆須實測，非宣稱）：
- **G-A1**：backtester 在「零成本（`slippage_bps=0, funding_enabled=False`）+ 全增強關 + `dead_mode_enabled=True` + `risk_enabled=False`」下，改造前後 `BacktestResult` 的 `final_equity` / `trades_count` / `realized_pnl` **bit-identical**
  > **基準是 Phase 0 完成後的 backtester**，不是原始版本。Phase 0 已刻意改變回測數字（撮合修正），Phase A 之後的每一個 golden 都以 Phase 0 的輸出為基準。基準漂移必須顯式記錄在測試 fixture 裡。
- **G-A2**：`tests/test_characterization_grid.py` 既有斷言**不改而綠**
- **G-A3**：`RiskMonitor` 委派 `risk.py` 後，`tests/test_components.py` 的跨組件接線斷言不改而綠
- **G-A4**：生產 config（無 `dead_mode_enabled` 鍵）經 `from_dict` → `to_dict` round-trip 後行為不變

### Phase B — 刻意變更 1：回測接上風控

範圍：backtester 呼叫 `risk.decide_trailing()` / `risk.decide_reduce()`；`risk_enabled` 預設改 `True` 對齊生產。

- 回測數字**會變**（intended —— trailing stop 會全平倉）。
- 守門 **G-B1**：`risk_enabled=False` 時，結果與 Phase A 的 golden **bit-identical**（證明接線沒有污染無風控路徑）
- 守門 **G-B2**：在一段已知會觸發 trailing 的資料上，`risk_enabled=True` 的 `trades_count` 嚴格大於 `risk_enabled=False`，且 `peak_margin_usage` 不高於後者（證明風控確實生效且方向正確）

### Phase C — 刻意變更 2：`pos == 0` 收進 `decide()`

範圍：G1。

- 回測數字**會變**（intended —— 這正是 FIDELITY_NOTES 第 (5) 條要消除的近似）。**Phase A/B 的 golden 不作為本階段的回歸基準。**
- 守門 **G-C1**（實盤等價證明）：**先**寫 characterization test 鎖死現行 live 的 `pos==0` 行為（撤該側單 → 掛 `best_bid` → 10s 節流 → 更新 `last_grid_price` → `dead_flag` 不動），**再**改造；改造後該測試**斷言不改而綠**。
- 守門 **G-C2**：`replay.py` 對既有 `logs/decisions.jsonl` 的重放仍 diff=0（新欄位有預設值，舊日誌可讀）。

> **G-C1 的順序不可顛倒。** characterization 必須先於改造落地，否則它記錄的是改造後的行為，證明不了等價 —— 這正是 `lessons.md`「characterization test 是最容易把 bug 制度化的測試類型」的教訓。
>
> 同一條教訓的另一半：Phase C 落地後，須逐條回問每個 characterization 斷言「這行為為什麼是對的」。`dead_flag` 在 `pos==0` 不清除就是一個答不漂亮但可證明 inert 的例子（見 §5.1），已明文記錄而非默許。

> **Phase C 的撮合語意必須明確定義（M2）**：貼 `best_bid` 的限價買單是 maker，可能永不成交。Phase 0 修好撮合後，`low <= best_bid` 在回測中幾乎恆真 ⇒ 每根都成交 ⇒ **嚴重高估 `pos==0` 的倉位重建速度**。Phase C 必須明文規定貼價單在回測中的成交條件（建議：要求 `low < best_bid` 嚴格小於，代表價格確實走過該價位而非僅觸及），並在 FIDELITY_NOTES 揭露。

### Phase D — 樣本外驗證（**產出結論的必要條件**）

`threshold_multiplier` 是**尾部風險參數**：成本只在罕見大趨勢中兌現，收益（更多網格成交）天天兌現。在單一歷史路徑上跑 Optuna（`smart_optimizer.py` 已在 optimize 它，範圍 6~80），**必然把它推到上界** —— 因為樣本內那根尾巴沒出現。使用者多頭卡住的那四天，正是尾巴出現時的樣子。

**沒有 Phase D，Phase 0~C 交付的只是一台精確的 overfit 機器。**

要求：
1. **train/test 時間切分**，且 test **必須包含一段單邊趨勢**（例如 2026-07-06~07-10 多頭卡死那段）
2. **跨 symbol**：`BNBUSDC` 與 `XRPUSDC`（兩者資料皆已在 `data/` 下）
3. **報告最差 regime 表現**，而非只報平均
4. **對 `threshold_multiplier` 輸出 sensitivity curve**，不得只報最佳點 —— 尾部參數的最佳點永遠在懸崖邊
5. **cost sensitivity**（G7）：若三選項排序在 fee ∈ {2,4} bps × slippage ∈ {0,1,2} bps 範圍內翻轉，**不得下結論**
6. **`threshold_multiplier` ablation**（風險 3ter）：把止盈加倍門檻解耦成獨立參數，分離兩個效應

守門 **G-D1**：任一選項在 test 期間 `liquidated=True` → 該選項淘汰，不論 train 期表現。
守門 **G-D2**：結論書面陳述必須附上 sensitivity curve 與最差 regime 數字，不得只給單點最佳值。

---

## 9. 測試策略

- **`risk.py` 純層單元測試**：trailing 五態（margin 未達閾值 → reset；未啟動且 pnl 未達標；未啟動且 pnl 達標 → activate；追蹤中創新高 → new_peak；追蹤中回撤達標 → flatten + deactivate）；`decide_reduce` 的 AND 條件、`0.8T` 邊界、60s cooldown。
- **`decision.py` 新行為測試**：`dead_mode_enabled=False` 且倉位遠超 threshold → 仍掛 entry 單（釘住「這是刻意的」，並在測試名稱寫明 why）。
- **撮合正確性**（Phase 0）：零成本下成交價 **嚴格等於** 掛單價（G-0a2）；`low` 觸及但 `close` 未穿越的 K 線**必須成交**（釘死 G4 錯誤一）；`close` 穿越但成交價非 `close`（釘死 G4 錯誤二）。
- **強平**（Phase 0）：必爆參數組 → `liquidated is True` 且提前終止（G-0b1）。
- **`pos==0` characterization**（Phase C 改造**之前**寫，見 G-C1）。
- **backtester golden**（G-A1），Phase B 沿用以證明無風控路徑未受污染（G-B1）。
- **主路徑守門**：斷言實驗配置走 `_run_terminal_ui_mode` 而非 legacy（風險 6）。
- **Monkey**：`now` 倒流（bar 時間非單調）、`best_bid = 0`、`margin_usage` 為 `NaN` / `inf`、`peak_pnl` 為負、`equity <= 0` 時 `margin_usage` 除零、`position_threshold = 0`。

> 測試命名遵循 `lessons.md`：禁止 `..._does_nothing` / `..._no_cancel` / `..._returns_empty` 這類描述 what 的名字，一律描述 why。

---

## 10. 後續（不在本次範圍）

1. 用本次交付的工具跑 optimizer，定奪 (a)(b)(c) —— progress.md 未決事項第 1 項的本體。**必須走 Phase D 的樣本外流程**
2. 若採用 GLFT → 先解 §6 風險 1 的 bandit/gamma 耦合
2bis. **追查 bandit 是否卡住**：`decisions.jsonl` 60001 筆 `grid_spacing`/`take_profit_spacing` 全同值，而 `thompson_enabled: true`、`update_interval: 10`。若 bandit 從未探索，`bandit_state.json` 的持久化與 `select_arm` 路徑有 bug 的可能。**這同時意味著 #6 的 bandit 持久化功能可能從未真正生效過。**
3. `glft_controller` 重複實作收斂（`enhancements.py:767` vs `decision.py:107`）
4. `_sync_positions()` 多空 `unrealizedPnl` 加總（`sync_service.py:73`）
5. anchor 語意污染（`bot.py:406`）
6. 強平距離監控（`liquidationPrice` 在 `sync_service.py:60-73` 被丟棄）
