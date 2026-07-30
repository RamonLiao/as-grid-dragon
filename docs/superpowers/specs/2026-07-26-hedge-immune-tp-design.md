# 止盈加倍只給淨曝險側 — 設計 spec

日期：2026-07-26
狀態：**v3 — 可開工**（v1 Reject／3 blockers → v2 Approve with changes／4 項 → v3 全部併入）
review 軌跡：quant spec reviewer（opus、fresh context、唯讀）兩輪。
Round 1 = Reject（BL1 漏 caller、BL2 測試變永綠空殼、BL3 risk_monitor 理由算術錯）；
Round 2 = Approve with changes（NF1 overshoot、NF2 面板 ×2 標籤說謊、NF3 回退門檻不可測、NF4 粒度）。
Round 2 另撤回其 Round 1 的 SF5 數字（reviewer 重算後採用本 spec 的 delta Δ +0.16 / +0.04）。
上游證據：`tasks/health-check-2026-07-26.md`（07-15~07-26 實盤逐筆對帳）

> **命名更正（v1 SF5）**：v1 標題用「對沖免疫」，名不符實——新規則下對沖側在下穿時仍以 1×
> 被縮減，只是速度減半。本 spec 不提供「免疫」，提供的是「加倍只給淨曝險側」。
> 檔名保留 `hedge-immune-tp` 以維持 commit 與 TODO 的連續性，但**文中一律不使用「免疫」**。

---

## 1. 問題陳述（事實，非推測）

`grid_engine/decision.py:97`：

```python
def tp_quantity(base_qty, my_position, opposite_position, position_limit, position_threshold):
    if my_position > position_limit or opposite_position >= position_threshold:
        return base_qty * 2
    return base_qty
```

生產（BNBUSDC，唯一 `enabled=true` 的 symbol）：
`position_limit = initial_quantity × limit_multiplier = 0.02 × 5 = 0.1`、
`position_threshold = 0.02 × 40 = 0.8`（推導在 `grid_engine/config.py:58-65`）。
多 0.60 與空 0.20 **都** > 0.1 ⇒ **兩側止盈量都是 0.04，進場量都是 0.02**。

一次完整往返（一下穿 + 一上穿）的淨效果：多頭 +0.02 − 0.04 = **−0.02**、空頭 −0.04 + 0.02 = **−0.02**。
等量縮減對**基數小的那側**是成比例更大的傷害。

**實測（07-15 → 07-26，19 筆成交、6 次下穿 / 3 次上穿，逐筆對帳零殘差）**：

| | 起始 | +進場 | −止盈 | 推算 | 實測 |
|---|---|---|---|---|---|
| 多 | 0.60 | +6×0.02 | −3×0.04 | 0.60 | 0.60 ✅ |
| 空 | 0.36 | +4×0.02 | −6×0.04 | 0.20 | 0.20 ✅ |

⇒ 使用者 2026-07-12 手動建立的對沖 11 天內流失 **44%**（0.36 → 0.20），
delta（定義：`long_position − short_position`）+0.24 → **+0.40**，
強平價 90.8 → **288.98**（距現價由 -84% 縮到 **-49%**）。確定性機制，非隨機。

### 為什麼這是 bug 而不是設計

加倍規則的原意（推測，code 無註解）是「持倉過大 ⇒ 加速出清」，對**單側累積型網格**合理。
缺陷在於它**逐側獨立判斷**，不區分該側是 (a) 失控累積的庫存，還是 (b) 刻意建立的對沖。

### `opposite_position >= position_threshold` 子條件的真實可達性（v1 SF1 修正）

**v1 寫「新閘門下數學不可達」是錯的**：`m > o ≥ T > L` 完全可滿足（例 L=0.1、T=0.8、o=0.9、m=1.0），
該組合下它只是**冗餘**（新閘門已加倍）。它真正可達且有效的情形是 `o ≥ T ∧ ¬NEW`，
即 §5.1 的**類 2**——reviewer 在全量 `logs/decisions.jsonl`（99,270 筆）實測 **98,399 筆**屬此類，
集中在 mult=20 時代（`position_threshold = 0.4`、空頭 0.02~0.08 ≤ limit 0.1、多頭 0.58~0.60 ≥ 0.4）。

⇒ **刪除該子條件是刻意的行為變更，不是清理冗餘。**
在**現行**參數下它不可達（`o ≥ 0.8` 而多頭被保證金封在 0.74，見健檢 §8）⇒ 今天行為中性；
但這是資本規模的偶然，不是邏輯保證。

## 2. 目標與非目標

**目標**：讓 delta 主動收斂到 0，不需入金。

**非目標**：
- **不追求「對沖不被消耗」。** 下穿時對沖側止盈仍以 1× 成交。依逐筆對帳的 11 天路徑
  （6 下穿 / 3 上穿）：舊規則 delta Δ = **+0.16**、新規則 **+0.04**。
  （reviewer 報 +0.18 / +0.06，差異來自穿越次數認定；方向與量級一致，不影響結論。）
  收斂靠上漲段，下跌段仍會外擴，只是速度減半。
- 不引入對沖倉獨立帳本（brainstorming 已評估否決：需狀態持久化 + 與交易所 netted 倉位對帳）。
- 不做參數搜尋。1-vs-1 行為對照，新規則**零自由參數**，無 multiple-testing 代價，
  **不開封 holdout 05-01~06-05**。
- 不加 config flag（避免再生假旋鈕）；A/B 對照靠 scratchpad monkeypatch。
- 不改 `risk_monitor` 的**觸發條件**（現行看持倉量而非 margin usage，屬另一條 backlog，見 §8）。

## 3. 設計

### 3.1 `grid_engine/decision.py:97` — `tp_quantity`

```python
def tp_quantity(base_qty, my_position, opposite_position, position_limit):
    """止盈量加倍只給「淨曝險方向」那側。

    對沖側（較小側）維持 1× ⇒ 進出對稱、消耗速度減半（不是免疫，見 spec §2）。
    原 `or opposite_position >= position_threshold` 已**刻意刪除**：它唯一可達且有效的
    情形是「我不是淨曝險側時仍加倍我」= 最大化拆對沖（2026-07-26 全量 log 實測
    98,399 筆屬此類）。行為變更的完整 diff 分類見 spec §5.1。
    """
    if my_position > position_limit and my_position > opposite_position:
        return base_qty * 2
    return base_qty
```

- **嚴格大於** ⇒ 兩側相等時都不加倍（對稱、穩定）。
- **不加死區**：兩側接近時的翻面只是輪流把加速出清給到較大側，自穩；重掛本來每次重算，無額外 churn。
- 簽名移除 `position_threshold`（不留死參數）。

### 3.2 `grid_engine/risk_monitor.py:78-89` — 減倉量改為不對稱（v1 BL3 重寫）

**v1 的理由是錯的**：現行「兩側各市價減 `position_threshold × 0.1`（= 0.08）」
是**等量**減倉 ⇒ `delta = long − short` **完全不變**。它降的是 gross（保證金風險），
**不是**中性度。v1 寫「同步拆掉對沖、與 §3.1 原則直接相反」在算術上就不成立。

**v1 提案（只減較大側）的缺陷**：在 `long == short` 時輸出零訂單——而 §3.1 的目標正是把系統
推向 `long ≈ short`，且步長固定 0.02、兩側量化到同一格，精確相等**不是零測度事件**
⇒ 會在 gross 最高、最需要煞車的狀態下什麼都不做。

**v2 提案（大側固定 2×、小側 1×）也錯了**（v2 NF1）。實測反例：`long=0.66, short=0.64`
（都 ≥0.64 ⇒ 觸發）→ long 0.50 / short 0.56 ⇒ delta 由 **+0.02 翻成 −0.06**，
`|delta|` 從 0.02 **惡化到 0.06**。而且——比 reviewer 指出的更糟——**任何一次觸發後雙側都掉到
門檻以下，觸發條件不再成立 ⇒ 根本沒有「下一輪」**，v2 §4 向量 4 說的「最壞是輪流減、
方向仍正確」兩處都錯。overshoot 的精確條件是 `gap < reduce_qty / 2`。

這是本 spec 內**第三次**同型錯誤（把「帶號值下降」當成「收斂」；前兩次是 v1 BL3 與 v2 表格）。

**v3 設計**：把大側的額外減量**夾到 gap**，讓「不 overshoot」成為可證明的不變式，
而不是靠案例分析。觸發條件與 60s cooldown 不變，`reduce_qty = position_threshold × 0.1`。

```python
gap = abs(long_position - short_position)
extra = min(reduce_qty, gap)          # 夾到 gap ⇒ 永不越過中性點
large_qty = reduce_qty + extra
small_qty = reduce_qty
# gap == 0 時 extra == 0 ⇒ 自動退回雙側等量，不需要浮點 `==` 分支
```

**不變式（A5 要斷言的就是這三條，不是逐案例列表）**：
1. `new_delta = sign(delta) × max(0, gap − reduce_qty)` ⇒ **`|delta|` 永不增加、永不變號**；
2. gross 嚴格下降 `2 × reduce_qty + extra`（煞車在所有狀態下都保留）；
3. `gap == 0` 時退化為現行行為（雙側各 `reduce_qty`）。

驗算：`gap=0.02` → extra 0.02 → delta 0.02→**0**；`gap=0.16` → extra 0.08 → delta 0.16→**0.08**；
`gap=0` → extra 0 → delta 不變、gross ↓0.16（= 現行）。

**這個寫法順帶消掉浮點相等比較的問題**（v2 NF1 後半）——交易所回來的 `0.6400000000000001`
不會落進錯誤分支，因為根本沒有 `==` 分支。

下單仍走 `reduce_only=True` 市價，量超過實際持倉時由交易所 clamp（既有行為，不新增邏輯）。
reviewer 另已驗證：`large_qty ≤ 2 × reduce_qty = 0.2 × threshold < 0.8 × threshold`（觸發門檻）
⇒ **「某側持倉 < 下單量」在觸發條件下算術上不可能**。

### 3.3 `grid_engine/bot.py:250-270` — 清掉加倍邏輯的死拷貝

`_get_adjusted_quantity` 內含一份同義加倍邏輯，但兩個呼叫點（`bot.py:401`、`:415`）都傳
`is_take_profit=False` 且只在 `position == 0` 的開倉引導路徑 ⇒ 加倍分支是死碼。
（reviewer 已獨立 grep 全 repo 確認無第三個呼叫點。）刪除該分支。

### 3.4 影響範圍（v1 BL1：v1 的「唯一 caller」為假，本表已重新 grep）

| 位置 | 處置 |
|---|---|
| `decision.py:135` `compute_quantity` | ✅ 簽名更新為 4 引數 |
| **`backtest/backtester.py:84`** `_legacy_grid_decision` | ✅ **v1 漏掉的第二個 caller**（5 個位置參數）。呼叫點 `:304`、`:430`（`_run_legacy_mode`，`initial_quantity <= 0` 觸發，deprecated）。**必須同步改成 4 引數**——它委派同一個 `tp_quantity`，因此自動繼承新語意（單一真相來源，不做語意分岔）。⚠️ 該路徑唯一的測試（`tests/test_backtest_seed_position.py:210`）只斷言「走 legacy 會 raise」⇒ **A7 全綠抓不到這個 TypeError**，只能靠 A2 的 grep |
| `tests/test_decision.py:27-29`、`:119` | ✅ 5 引數呼叫需更新；**`:28` 斷言的正是被刪的子條件**，必須改成「該情形不再加倍」的顯式斷言，不是改簽名了事 |
| `tests/test_backtest_matching.py:147-190` | ✅ 見 §3.5（v1 BL2） |
| `decision.py:179`（裝死）/ `:187`（正常） | 兩處都經 `compute_quantity`，見 §4 向量 3 |
| `bot.py:242-243` 餵 threshold/limit 進 `DecisionInputs` | 不變（`DecisionInputs` 欄位保留，裝死判定 `is_dead_mode` 仍用 threshold） |
| `backtest/tick_sim.py:104-105`、`backtest/backtester.py:604-605` | 不變（自行從 multiplier 推導後餵 `decide()`）。⚠️ v1 誤引 `:565-566`，那是 `_validate_seed` |
| **`grid_engine/ui.py:126-133`** | ✅ **同步修改**（v2 NF2）。它不只上色，還印 `多×2` / `空×2` 標籤，判定只看 `position > position_limit`、**不看對手側** ⇒ 改動後小側實際是 1× 但面板仍顯示「×2」。這會直接**誤觸 §7 回退表第 4 條**（「小側止盈 qty 出現 0.04」），讓操作者以為新規則沒生效。修法：兩個 `elif` 各加 `and my_position > opposite_position` |
| `grid_engine/replay.py` | 不變；`decisions.jsonl` 的 inputs 已含 `position_limit`（實查確認） |

### 3.5 `tests/test_backtest_matching.py` 的 reduce_only clamp 測試必須改造（v1 BL2）

`_both_side_doubling_cfg`（`:147-155`）**刻意**設 `limit_multiplier=100.0`（讓 `m > limit` 不可能）
+ `threshold_multiplier=1.0`，好讓加倍**只**由本 spec 要刪的子條件觸發。
`test_tp_fill_cannot_close_more_than_the_position_that_existed_before_this_bars_entry`
靠該加倍（tp qty 2.0 > prior_qty 1.0）來檢驗 `min(qty, prior_qty)` clamp。

**新規則下**：兩側各 1.0（`m == o`）且 `m ≤ L=100` ⇒ tp qty = 1.0 ⇒ `min(1.0, 1.0)` 是 no-op，
而三條斷言值（`trades_count 2` / `realized 2.0` / `unrealized 1.2`）**恰好完全不變**
⇒ **測試繼續綠但已無鑑別力**（通則 3「資料退化只會一直綠」）。

**改造方案**（讓加倍在新規則下仍發生 ⇒ clamp 仍被行使）：
改為單側不對稱場景，利用 `_zero_cost_cfg` 的預設 `direction="long"`（短側恆 0 ⇒ `m > o` 必成立）：

```python
_zero_cost_cfg(limit_multiplier=0.5, threshold_multiplier=100.0)
# position_limit = 1.0 × 0.5 = 0.5  → bar2 後 m=1.0 > 0.5 ✓ 且 m=1.0 > o=0.0 ✓ → tp qty 2.0
# position_threshold = 100          → is_dead_mode(1.0, 100) = False，裝死不介入
```
K 線與 v1 相同三根。預期值改為單側版本：
`trades_count == 1`、`realized_pnl == (100.4 − 99.4) × 1.0 == 1.0`、
`unrealized_pnl == (100 − 99.4) × 1.0 == 0.6`。
**驗收要求**：拿掉 `min(qty, prior_qty)` clamp 後該測試必須**紅**（見 A8）。

## 4. Red Team（實作前列出，dev-rules 要求）

| # | 攻擊向量 | 防禦 |
|---|---|---|
| 1 | 兩側極接近時 `m > o` 每次成交都翻面 | 判定無害（自穩、無額外 churn）；測試釘死 `m == o` → 兩側皆不加倍 |
| 2 | 零倉 / 單側零倉 | `m = 0` 不過 `> position_limit`；`_decide_side:189` 另有 `if my_pos > 0` 才掛止盈；零倉引導走 `bot.py:401` 不經此函數。測試覆蓋 0/0、0/x、x/0 |
| 3 | **裝死分支也吃這個量**（`decision.py:179`） | 裝死側必然 `m > threshold ≥ limit`，通常也是大側 ⇒ 行為不變。但「裝死側反而是小側」（對手更大）時出清變慢——**刻意的行為變更**，需專門測試並記錄 |
| 4 | risk_monitor 減完後 delta 反向或 `\|delta\|` 擴大 | **v2 的答案「最壞是輪流減、方向仍正確」是錯的**（v2 NF1）：任何一次觸發後雙側都掉到門檻以下 ⇒ **沒有下一輪**。v3 改用 `extra = min(reduce_qty, gap)` 讓「`\|delta\|` 永不增加、永不變號」成為可證明的不變式（§3.2），並由 A5 直接斷言該不變式，不靠案例列舉 |
| 5 | `position_limit` 極小的 symbol（BTCUSDC 0.005）幾乎恆過門檻 ⇒ 規則退化成純淨曝險判定 | 預期行為非缺陷。目前僅 BNBUSDC enabled；其餘啟用前各自複核（§8） |
| 6 | **保留對沖 ⇒ gross 更大 ⇒ initialMargin 更高 ⇒ entry 被 `-2019` 拒的機率上升**（v1 SF3） | A6 新增 `rejected_entries` 與 `max_drawdown` 逐窗守門；§7 上線後監控 margin usage |

## 5. 可判定驗收準則

`delta` 一律定義為 `long_position − short_position`（帶號）。

| # | 準則 | 判定方式 |
|---|---|---|
| A1 | `tp_quantity` 真值表單測：`m` {≤ limit, > limit} × `m` vs `o` {<, ==, >} 全組合 | 每條斷言必須先在「改回舊實作」的 mutation 下**紅一次** |
| A2 | 簽名改 4 引數，**所有** caller 更新 | `grep -rn "tp_quantity" grid_engine backtest scripts tests` 零 5 引數殘留。**必須包含 `backtest/backtester.py:84`**（§3.4） |
| A3 | `bot.py` 加倍死分支刪除 | grep 證明兩個呼叫點都傳 `False` |
| A4 | replay 全量 `logs/decisions.jsonl` | 見 §5.1。**A4 是實作 guard，不是策略證據**（§6） |
| A5 | risk_monitor **斷言 §3.2 的三條不變式**（不是列舉案例）：(1) `abs(new_delta) <= abs(old_delta)` 且不變號；(2) gross 嚴格下降；(3) `gap == 0` 時退回雙側等量。測試狀態至少含 `gap < reduce_qty/2`（**v2 的 overshoot 反例 `0.66/0.64`，必測**）、`gap > reduce_qty`（`0.80/0.64`）、`gap == 0`、`short > long`、以及浮點雜訊 `0.6400000000000001/0.64` | mutation 兩條各須紅一次：(a) 改回一律等量 → 不變式 1 在 `0.80/0.64` 紅；(b) 改回 v2 的固定 2×（`extra = reduce_qty`）→ 不變式 1 在 **`0.66/0.64`** 紅 |
| A6 | tick_sim A/B（§5.2 shim）× 場景 A/B × W1/W2/W3/full、fee=0/slip=0 | 逐窗回報 `max abs(delta)`、`final abs(delta)`、`final_equity`、`max_drawdown`、`liquidated`、`round_trips`、`rejected_entries`。**Gate**：(i) `max abs(delta)` 與 `final abs(delta)` 每窗 ≤ 舊規則；(ii) 零強平；(iii) `final_equity` 劣化 ≤ **1.0 USDC**；(iv) `max_drawdown` 劣化 ≤ **2 個百分點**；(v) `rejected_entries` 增幅 ≤ **50%**；**基準為 0 時改判「新規則 > 5 筆即超標」**（避免 0 基準下任何拒單都算超標）。任一項超標 → **停下報告，不自行放行**。⚠️ **(i) 與 (iii) 已於 2026-07-30 事後修訂，見 §5.3——判定以 §5.3 為準** |
| A7 | 全套測試綠 | 基線 546 passed / 1 skipped（須在 `as-grid-dragon` 子目錄跑） |
| A8 | §3.5 改造後的 clamp 測試，在拿掉 `min(qty, prior_qty)` 後**必須紅** | 這條是 BL2 的直接補償：證明改造後仍有鑑別力 |
| A9 | 保留對沖後的保證金試算（照健檢 §8 的算法）：報「新規則終態的 initialMargin 與剩餘可用層數」 | 若可用層數 < 3 → 在上線報告中明確標示「氧氣不足」，不得沉默 |

delta 軌跡由 `TickSimResult.fills`（含 side/kind/qty，tp 有 `closable=min(qty, prior_qty)` clamp）
+ seed 量重建，**不需改 repo**。

### 5.1 A4 的 diff 模式（逐筆結構化斷言，不是「零 diff」）

新規則是舊規則的**真子集**：`NEW ⇒ m > L ⇒ OLD`。
reviewer 在全量 99,270 筆實測 `NEW ∧ ¬OLD` = **0 筆**，代數與實證一致。
因此所有 diff 必然**單向**：止盈 qty 由 2× 降為 1×，永不反向。

diff 恰有兩類，**兩類都可達，且非互斥**（判定必須用「或」，寫成 XOR 會誤判）：

- **類 1**：`m > position_limit and m <= o`（我夠大但不是淨曝險側）
- **類 2**：`o >= position_threshold and not (m > position_limit and m > o)`（被刪的子條件）

實測分佈（99,270 筆）：類2-only **81,957**、類1∩類2 **16,442**、類1-only **870**，
全部發生在 short 側，long 側零差異。threshold 分佈：`(0.1, 0.4)` 98,399 筆、`(0.1, 0.8)` 871 筆。

> **⚠️ 2026-07-30 Task 6 實跑更正**：上列 reviewer 數字**兩欄對不上**。實測（99,552 筆、
> 排除 `60917cc` 前的舊 code 窗口後 43,164 筆）為類1-only **870**（與上列**完全吻合**）、
> 類2-only **285**、類1∩類2 **5,022**，有 diff 者共 6,177 筆。
> 差異已查明成因，非缺陷：(a) 07-12 前 long 長期 dead mode、**根本沒有止盈單** ⇒ 該段（占
> 檔案絕大多數）不產生 diff；(b) 類 2 需 `o >= position_threshold`，07-12 改 `mult=40`
> 後 threshold = 0.8 而實際持倉最大 0.66 ⇒ **07-12 之後類 2 永不成立**，只有 threshold = 0.4
> 的早期紀錄命中，而那批又正好被 dead mode 擋掉止盈單。
> reviewer 那兩欄合計 ≈ 99,269 ≈ 當時總筆數，形態上像是把「總紀錄數」當成了「分類命中數」；
> `類1-only 870` 分毫不差則顯示他確實跑過。**Task 6 的判定以實跑數字為準。**

斷言（三項全成立才 PASS）：
1. diff 只出現在 `SideDecision.orders` 中 `reduce_only=True` 那張的 `quantity`；
2. 該側滿足類 1 **或**類 2；
3. `replayed_qty × 2 == expected_qty`。

**斷言 3 的前提（v1 N2）**：依賴 `funding_long/short_bias == 1.0` 且 `compute_quantity` 的
floor `max(initial_quantity × 0.5, q)` 不咬。reviewer 實測全 99,270 筆 bias 皆為 1.0
（生產 `funding_rate_enabled=false`、`glft_enabled=false`）⇒ 對這份 log 成立。
若日後啟用 funding bias 且 `bias < 0.5`，floor 會咬掉 1× 那格、兩倍關係破裂 ⇒ 該前提須重新確認。

### 5.2 A6 的 monkeypatch shim（v1 SF4：必須寫死，否則比較對象是稻草人）

改簽名後 `compute_quantity` 以 **4 引數**呼叫 `tp_quantity`。舊規則的 shim 因此**不能**直接
貼 HEAD 版舊函式（5 引數 → `TypeError`），必須自行注入 threshold：

```python
# scratchpad 專用，不進 repo
import grid_engine.decision as d
_THRESHOLD = 0.02 * 40.0   # initial_quantity × threshold_multiplier，須與 A/B 的 cfg 一致
def _tp_quantity_legacy(base_qty, my_position, opposite_position, position_limit):
    if my_position > position_limit or opposite_position >= _THRESHOLD:
        return base_qty * 2
    return base_qty
```
**腳本內必須 `assert` cfg 的 `threshold_multiplier`/`limit_multiplier` 等於預期值**——
`tick_sim.TickSimConfig` 的預設恰為 `40.0`/`5.0`，忘記設也看不出來（v1 N1）。
（`fee=0`/`slip=0` 反而**不是**預設值 `0.0002`/`0.0001`，這兩項忘設會露出來。）

### 5.3 A6 gate (i)/(iii) 的事後修訂（2026-07-30）

> ⚠️ **這是看過實測結果之後才改的判準——回測裡最經典的自欺形式。**
> 保留原文於 §5 表格供對照，本節說明改了什麼、為什麼、以及修訂前後的判定差異。
> 使用者已在 2026-07-30 明示核可本次修訂。

**觸發**：Task 7 首次跑出 A6 五項超標——`(iii) final_equity` 在 A/W1、B/W1 各劣化
−7.221 / −2.906；`(i) final abs(delta)` 在 A/W2（0.080→0.160）、B/W2（0.060→0.220）、
B/W3（0.060→0.100）劣化。依 spec 停下並診斷（`<scratchpad>/diag_delta_result.txt`）。

**診斷結論（三項，皆有逐筆軌跡佐證）**：

1. **A/W2 的 FAIL 是判準缺陷，不是行為退化。** 帶號 delta 逐日軌跡 NEW **每一天**都低於
   OLD（06-17 +0.46 vs +0.60 … 06-28 +0.04 vs +0.16），末端穿越 0 後繼續走到 −0.16，
   `abs()` 因此反而變大。**單點標量比不了軌跡**：v1 用帶號值被 reviewer 判為「把帶號下降
   當收斂」（BL3），改用 `abs` 之後，在穿越 0 的路徑上又反向誤判。兩個方向都會錯。
2. **B/W2、B/W3 的 `abs` 劣化為真，但 OLD 的「低 delta」是把兩側都拆光換來的。**
   窗口末雙邊持倉——B/W2：OLD `L0.08 / S0.14`（min **0.08**）vs NEW `L0.28 / S0.50`
   （min **0.28**）；B/W3：OLD min **0.40** vs NEW min **0.46**；A/W2：OLD min **0.08**
   vs NEW min **0.14**。**三項超標全部伴隨對沖存量提高** ⇒ 測到的是設計意圖本身。
   「淨曝險低是因為幾乎沒有倉位」不是本 spec 要的安全。
3. **`full` 窗口 `finΔ = 0.000`（四個 cell 全零）有真實機制**，非 artifact：末筆 fill
   `07-13 17:36`（long entry 0.02 + short tp）恰好讓兩側相等（A `0.06/0.06`、B `0.14/0.14`），
   末段兩側均被網格壓向同一平衡點。

**修訂後的 gate (i)**（取代單點 `final abs(delta)` 比較，整體**更嚴**——新增逐日軌跡要求）：
- (i-a) `max abs(delta)` 每窗 NEW ≤ OLD —— 實測 **8/8 PASS**
- (i-b) **帶號** delta 的逐日軌跡，NEW 不得在任一天高於 OLD —— 實測 **8/8 PASS**
- (i-c) `final abs(delta)` 若劣化，必須伴隨窗口末 `min(long, short)`（對沖存量）改善
  —— 三項劣化全部滿足

**修訂後的 gate (iii)**：取消單窗 `≤1.0 USDC` 容差，改判 **`full` 窗口 `final_equity` 不劣化**。
理由：W1 上漲段少賺（−7.2 / −2.9）與 W2 下跌段多賺（+7.0 / +11.2）是同一枚硬幣——
保住空頭對沖必然在漲段付方向性代價，這在 §2 目標與 §6「equity 只看方向」已載明接受。
以單窗容差卡它，等於要求「既保對沖又不付對沖成本」，與本 spec 的目的自相矛盾。
實測 `full`：A **+0.826**、B **+2.919** ⇒ PASS。

**修訂後全項 PASS。** 附帶：`max_drawdown` 幾乎全面改善（B/W2 **−10.59pp**、B/full −2.80pp、
B/W1 −2.39pp），零強平、零拒單（兩規則 × 8 窗）。完整對照表 `<scratchpad>/a6_ab_result.txt`。

**未修訂、仍然成立的限制**：gate (ii)/(iv)/(v) 原文不動；§6 的所有已知限制（in-sample 窗口、
FIFO vs netted 分歧、fee=0 非永久）一併適用於本次結果。

**本次順帶發現、未處理**：OLD 規則在 A/full 的 06-14、06-17 出現 `short = -0.00`。
可能只是浮點殘差的顯示，也可能 `PositionBook` 允許持倉穿透到負值。**未查證**，列入 §8 backlog。

## 6. 已知限制（誠實記錄）

- **A4 是實作 guard，不是策略證據**（v1 SF2）：`NEW ⊆ OLD` 可代數證明，A4 的三條斷言在
  「實作照 spec 寫」的前提下不可能失敗；replay 逐筆餵歷史 inputs，只顯示一階 qty 差、
  不含路徑分歧（成交序列會變、replay 看不到）。
- **A5 是 regression guard，不是 live fix 的證據**：risk_monitor 觸發條件在現行資本下
  **不可達**（雙側同時 ≥0.64 ⇒ gross 1.28 ≈ 730 USDC 名目，所需保證金遠超可用 17.955）。
  它防的是未來入金後的漂移。
- **A6 的 equity 數字只看方向**：seed 場景受 FIDELITY_NOTES (12) 的 per-lot FIFO vs 生產 netted
  均價分歧限制。**delta 指標由 qty 累加而來，不受該分歧影響**，可作主判準。
- **A6 的窗口是被挖過的 in-sample 段**：W1/W2/W3 已被 requote 實驗的 166 cell + 健檢的 30 cell
  跑過；`≤1.0 USDC` 等門檻是在該資料上的工程判斷，不是獨立 OOS 證據。holdout 未開封 ✓。
- **fee=0 是實查值但非永久**（BNBUSDC maker 0 / taker 4bps，Binance USDC 促銷）。
  方向上新規則**交易量更小**（小側 1× 而非 2×）⇒ 有費率時只會更有利，故單跑 fee=0 可接受；
  但若費率回到 ≥2bps，A6 的 equity 門檻需重跑。
- **滑價與容量兩項無證據**：缺 BNBUSDC depth/ADV 實測。單筆 0.02 BNB ≈ 11 USDC 名目，
  直觀無 market impact，但**屬未證**，列入索取清單。
- **拒單 / 部分成交的語意差異未消除**：live 靠交易所 `reduce_only` 擋超量平倉，回測靠
  `min(qty, prior_qty)`。兩者在超量時結果相近但不保證逐筆一致（既有落差，本 spec 不處理）。
- 本 spec 不解決「多頭均價 666.7 遠高於市價 ⇒ 多頭止盈實質是 -4 認賠單」；那是 TODO 1b
  的使用者裁決，不是 code 缺陷。

## 7. 上線與回退

**上線前**：生效需**重啟引擎**（純邏輯改動，無 config 遷移）。
⚠️ 無 startup cancel-all（v1 N3）：交易所上那張 `sell SHORT 0.04 RO` 會活到第一次 requote
（`should_adjust` 因 `anchor=0` 回 True → `cancel_side`），數秒；若不巧先成交，對沖再被吃 0.04。
**建議重啟前手動撤掉那 4 張掛單。**

**上線後驗收（活體，一行 grep）**：`logs/decisions.jsonl` 新紀錄中，
小側止盈 qty 應為 **0.02**、大側 **0.04**（現行 0.60/0.20 ⇒ 多頭 0.04、空頭 0.02）。

**回退條件（v1 SF7；任一成立即回退到前一個 commit 並重啟）**：
| 指標 | 門檻 |
|---|---|
| `abs(delta)` | 上線後 14 天未較上線日下降，或任何時點 > 0.50 |
| margin usage（`initialMargin / marginBalance`） | > 95%。⚠️ **觀測粒度為每日一點**（v2 NF4）：`state.margin_usage` 只經 `reporting.py:48` 進每日 Telegram 摘要，且生產 `telegram_risk_alert_enabled=false` ⇒ 即時告警路徑是關的。這是**人工/每日**判讀，不是自動守門 |
| 下單失敗 | **log 出現「⛔ 下單斷路」警告**（`order_executor.py:113-116`，連續失敗 10 次觸發，同時發 Telegram）。⚠️ v2 原寫「`-2019` 單日 > 20 筆」**不可測**（v2 NF3）——`_register_order_failure` 逐筆**不 log**，只在 `n == ORDER_CIRCUIT_THRESHOLD` 才 warning 一次 ⇒ 那是操作層的假旋鈕。本條改用唯一實際可觀測的信號，代價是粒度粗（持續每日 5 筆的慢性拒單抓不到）；補 per-failure log 見 §8 |
| 小側止盈 qty | 出現 0.04（= 新規則未生效或被覆寫） |

## 8. Backlog（本 spec 明確不做）

- **`risk_monitor` 的觸發條件看持倉量而非 margin usage**：它的目的是降保證金風險，
  但門檻寫成「雙側 ≥ `position_threshold × 0.8`」是間接代理。改成看 margin usage 更直接，
  但與本次對沖主題無關，且需自己的驗收設計。
- 其餘三個 symbol（ETHUSDC / SOLUSDC / BTCUSDC）目前 `enabled=false`。啟用任一個之前，
  須各自複核 `position_limit` 與預期倉位規模的關係（§4 向量 5）。
- **`order_executor` 缺 per-failure log**（v2 NF3）：`_register_order_failure`（`:106-116`）逐筆失敗
  不留痕，只在連續第 10 次才 warning 一次 ⇒ 慢性拒單（每日數筆但不連續 10 次）完全不可觀測。
  補一行含 ccxt error code 的 log 即可，但屬獨立改動，不混進本 spec。
- 索取清單：BNBUSDC depth/ADV 實測（解 §6 的滑價與容量兩項）。
- **`PositionBook` 是否允許持倉穿透到負值**（2026-07-30 Task 7 診斷順帶發現）：舊規則在
  A/full 的 06-14、06-17 出現 `short = -0.00`。可能只是浮點殘差的顯示（`-1e-17`），
  也可能帳本缺 `max(0, ...)` 守衛。**未查證**，兩規則皆受影響 ⇒ 不影響本次 A/B 對照的
  相對結論，但若為真是獨立缺陷。
