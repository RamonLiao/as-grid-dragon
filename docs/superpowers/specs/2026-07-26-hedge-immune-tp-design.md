# 對沖免疫的止盈量加倍 — 設計 spec

日期：2026-07-26
狀態：待 review
上游證據：`tasks/health-check-2026-07-26.md`（07-15~07-26 實盤逐筆對帳）

---

## 1. 問題陳述（事實，非推測）

`grid_engine/decision.py:97`：

```python
def tp_quantity(base_qty, my_position, opposite_position, position_limit, position_threshold):
    if my_position > position_limit or opposite_position >= position_threshold:
        return base_qty * 2
    return base_qty
```

生產（BNBUSDC，唯一 enabled 的 symbol）：
`position_limit = initial_quantity × limit_multiplier = 0.02 × 5 = 0.1`、
`position_threshold = 0.02 × 40 = 0.8`。
多 0.60 與空 0.20 **都** > 0.1 ⇒ **兩側止盈量都是 0.04，進場量都是 0.02**。

一次完整往返（一下穿 + 一上穿）的淨效果：多頭 +0.02 − 0.04 = **−0.02**、空頭 −0.04 + 0.02 = **−0.02**。
等量縮減對**基數小的那側**是成比例更大的傷害。

**實測（07-15 → 07-26，19 筆成交、6 次下穿 / 3 次上穿，逐筆對帳零殘差）**：

| | 起始 | +進場 | −止盈 | 推算 | 實測 |
|---|---|---|---|---|---|
| 多 | 0.60 | +6×0.02 | −3×0.04 | 0.60 | 0.60 ✅ |
| 空 | 0.36 | +4×0.02 | −6×0.04 | 0.20 | 0.20 ✅ |

⇒ 使用者 2026-07-12 手動建立的對沖 11 天內流失 **44%**（0.36 → 0.20），
delta +0.24 → **+0.40**，強平價 90.8 → **288.98**（距現價由 -84% 縮到 **-49%**）。
這不是隨機結果，是確定性機制。

### 為什麼這是 bug 而不是設計

加倍規則的原意（推測，code 無註解）是「持倉過大 ⇒ 加速出清」，對**單側累積型網格**合理。
缺陷在於它**逐側獨立判斷**，不區分該側是 (a) 失控累積的庫存，還是 (b) 刻意建立的對沖。
對 (b) 施加同一規則，就是系統性拆掉對沖。

### 附帶事實：`opposite_position >= position_threshold` 子條件

該子條件對**大側**沒有邊際效果（大側已由 `my_position > position_limit` 觸發）。
它唯一可達的效果是：**「我很小、對手巨大」時加倍我** —— 恰好是最大化拆對沖的情形。

## 2. 目標與非目標

**目標**：讓 delta 主動收斂到 0，不需入金。

**非目標**：
- 不追求「下跌段 delta 也不外擴」。下穿同時「加多」與「平空」，delta 在下跌段仍會外擴
  （使用者已知並接受）；收斂靠上漲段。
- 不引入對沖倉獨立帳本（brainstorming 已評估並否決：需狀態持久化 + 與交易所 netted 倉位對帳，
  爆炸半徑遠大於本次收益）。
- 不做參數搜尋。本次是 1-vs-1 行為對照，無 multiple-testing 代價，**不開封 holdout 05-01~06-05**。
- 不加 config flag。避免再生一個假旋鈕；A/B 對照靠 scratchpad monkeypatch 完成。

## 3. 設計

### 3.1 `grid_engine/decision.py` — `tp_quantity`

```python
def tp_quantity(base_qty, my_position, opposite_position, position_limit):
    # 止盈量加倍只給「淨曝險方向」那側。對沖側（較小側）維持 1× ⇒ 進出對稱、不被拆。
    # 原 `or opposite_position >= position_threshold` 已刪：在新閘門下數學不可達
    # （my_pos > opp_pos >= threshold > position_limit 自相矛盾），且其唯一可達效果
    # 正是「我是小側時加倍我」= 最大化拆對沖（2026-07-26 實證）。
    if my_position > position_limit and my_position > opposite_position:
        return base_qty * 2
    return base_qty
```

- **嚴格大於** ⇒ 兩側相等時都不加倍（對稱、穩定）。
- **不加死區（deadband）**：兩側接近時的翻面只是輪流把加速出清給到較大側，自穩；
  且重掛本來每次重算，不產生額外訂單 churn。YAGNI。
- 簽名移除 `position_threshold`（不留死參數）。呼叫端：`decision.py:135` `compute_quantity`。

### 3.2 `grid_engine/risk_monitor.py:79` — 雙向減倉改為只減淨曝險側

現行：雙側都 ≥ `position_threshold × 0.8`（= 0.64）時，每 60s **市價各平** `threshold × 0.1`（= 0.08）。
這會在「收斂成功且雙側長大」的狀態同步拆掉對沖，與 §3.1 的原則直接相反。

改為：同一觸發條件下，只市價減**較大側** 0.08；**兩側相等則都不減**（不任意選邊）；
保留既有 60s cooldown 與 log。

### 3.3 `grid_engine/bot.py:250-270` — 清掉加倍邏輯的死拷貝

`_get_adjusted_quantity` 內含一份與 `tp_quantity` 同義的加倍邏輯，但兩個呼叫點
（`bot.py:401`、`bot.py:415`）都傳 `is_take_profit=False`，且只在 `position == 0` 的開倉引導路徑
⇒ 加倍分支是死碼。留著它 = 留一份與新規則矛盾的誤導拷貝。刪除該分支。

### 3.4 影響範圍（已盤點）

| 位置 | 是否受影響 |
|---|---|
| `decision.py:135` `compute_quantity` | ✅ 唯一 caller，簽名更新 |
| `decision.py:179`（裝死分支）/ `:187`（正常分支） | ✅ 兩處都經 `compute_quantity`，見 §4 向量 3 |
| `bot.py:242-243` 餵 `position_threshold`/`position_limit` 進 `DecisionInputs` | 不變（`DecisionInputs` 欄位保留，裝死判定仍用 threshold） |
| `backtest/tick_sim.py:104-105`、`backtest/backtester.py:565-566` | 不變（自行從 multiplier 推導後餵 `decide()`） |
| `grid_engine/ui.py:126-132` | 不變（只用於面板顏色門檻） |
| `grid_engine/replay.py` | 不變；`decisions.jsonl` 的 inputs 已含 `position_limit`（實查確認） |

## 4. Red Team（實作前列出，dev-rules 要求）

| # | 攻擊向量 | 防禦 |
|---|---|---|
| 1 | 兩側極接近時 `my_pos > opp_pos` 每次成交都翻面 | 判定無害（自穩、無額外 churn）；測試釘死 `my == opp` → 兩側皆不加倍 |
| 2 | 零倉 / 單側零倉 | `my_pos = 0` 不過 `> position_limit`；`_decide_side:189` 另有 `if my_pos > 0` 才掛止盈；零倉引導走 `bot.py:401` 不經此函數。測試覆蓋 0/0、0/x、x/0 |
| 3 | **裝死分支也吃這個量**（`decision.py:179`） | 裝死側必然 `my_pos > threshold ≥ limit`，通常也是大側 ⇒ 行為不變。但「裝死側反而是小側」（對手更大）時出清變慢——**刻意的行為變更**，需專門測試並記錄 |
| 4 | risk_monitor 只減大側後 delta 反向超過 0.08 → 下次減另一側，來回 | 嚴格大於才減、相等不減、保留 60s cooldown ⇒ 最壞是輪流減，方向仍正確 |
| 5 | `position_limit` 極小的 symbol（BTCUSDC 0.005）幾乎恆過門檻 ⇒ 規則退化成純淨曝險判定 | 預期行為非缺陷。目前僅 BNBUSDC enabled；其餘 symbol 啟用前各自複核（寫入 §7） |

## 5. 可判定驗收準則

| # | 準則 | 判定方式 |
|---|---|---|
| A1 | `tp_quantity` 真值表單測：`my_pos` {< limit, > limit} × `my_pos` vs `opp_pos` {<, ==, >} 全組合 | 每條斷言必須先在「改回舊實作」的 mutation 下**紅一次** |
| A2 | 簽名移除 `position_threshold`，所有 caller 更新 | `grep -rn "tp_quantity" grid_engine backtest tests` 零舊簽名殘留 |
| A3 | `bot.py` 加倍死分支刪除 | grep 證明兩個呼叫點都傳 `False`；全套測試綠 |
| A4 | **replay 全量** `logs/decisions.jsonl`（~163MB） | 見下方 §5.1，逐筆結構化斷言。**任何不符該模式的 diff = 失敗** |
| A5 | risk_monitor 人造 state（雙側 0.7、threshold 0.8）→ 只對大側下 reduce order；相等 → 零 order | mutation = 改回雙側減倉，測試須紅 |
| A6 | tick_sim A/B（scratchpad monkeypatch 舊 `tp_quantity`）× 場景 A/B × W1/W2/W3/full、fee=0/slip=0 | 新規則的 `max abs(delta)` 與 `final abs(delta)` **每個窗口都 ≤ 舊規則**；零強平；`final_equity` 劣化 ≤ **1.0 USDC**。劣化超過 1.0 → **停下報告，不自行放行** |
| A7 | 全套測試綠 | 基線 546 passed / 1 skipped（須在 `as-grid-dragon` 子目錄跑） |

`delta` 一律定義為 `long_position − short_position`（帶號）。

delta 軌跡由 `TickSimResult.fills`（含 side/kind/qty）+ seed 量重建，**不需改 repo**。

### 5.1 A4 的 diff 模式（必須逐筆比對，不是「零 diff」）

新規則是舊規則的**真子集**：新加倍 ⇒ `my_pos > limit` ⇒ 舊也加倍。
因此所有 diff 必然是**單向的「舊加倍、新不加倍」，止盈 qty 由 2× 降為 1×，永不反向**。

diff 恰有兩類，缺一不可（兩類在歷史 log 中**都可達**）：

- **類 1**：`my_pos > position_limit and my_pos <= opp_pos`
  （我夠大但不是淨曝險側 ⇒ 原第一子條件觸發、新閘門否決）
- **類 2**：`opp_pos >= position_threshold and not (my_pos > position_limit and my_pos > opp_pos)`
  （原第二子條件觸發、新規則已刪除該子條件）
  ⚠️ 類 2 在 mult=20 時代（`position_threshold = 0.4`）大量存在：空頭 0.02~0.08 ≤ limit 0.1、
  多頭 0.58~0.60 ≥ 0.4 ⇒ 舊規則加倍空頭止盈，新規則不加倍。
  **若驗收腳本只斷言類 1，會把類 2 誤判成失敗。**

斷言內容（三項全部成立才算 PASS）：
1. diff 只出現在 `SideDecision.orders` 中 `reduce_only=True` 的那張的 `quantity` 欄位；
2. 該側滿足類 1 或類 2；
3. `replayed_qty × 2 == expected_qty`（單向、恰為兩倍關係）。

## 6. 已知限制（誠實記錄）

- **A5 是 regression guard，不是 live fix 的證據**：risk_monitor 那條路徑在現行資本下**不可達**
  （雙側同時 ≥0.64 所需保證金遠超可用 17.96）。它防的是未來入金後的漂移。
- **下跌段 delta 仍會外擴**（§2 非目標）。使用者已知並接受。
- **A6 的 equity 數字只看方向**：seed 場景受 FIDELITY_NOTES (12) 的 per-lot FIFO vs 生產 netted
  均價分歧限制。但 **delta 指標由 qty 累加而來，不受該分歧影響**，可作為主判準。
- **fee=0 是實查值但非永久**：BNBUSDC maker 費率 0 是 Binance USDC 促銷（taker 4bps）。
  若回到 ≥2bps，A6 的 equity 判準需重跑。
- 本 spec 不解決「多頭均價 666.7 遠高於市價 ⇒ 多頭止盈實質是 -4 認賠單」這件事本身；
  那是 TODO 1b（出場路線）的使用者裁決，不是 code 缺陷。

## 7. 上線前後的操作性事項

- 生效需**重啟引擎**（純邏輯改動，無 config 遷移）。重啟前確認在含本改動的 branch。
- 重啟後驗收：`logs/decisions.jsonl` 新紀錄中，**小側的止盈 qty 應為 0.02、大側為 0.04**
  （現行 0.60/0.20 ⇒ 多頭 0.04、空頭 0.02）。這是一行 grep 就能確認的活體證據。
- 其餘三個 symbol（ETHUSDC / SOLUSDC / BTCUSDC）目前 `enabled=false`。啟用任一個之前，
  須各自複核 `position_limit` 與預期倉位規模的關係（§4 向量 5）。
