# 追價語意驗證（tick 級回測）+ 門檻參數化 — Design Spec

日期：2026-07-13
狀態：使用者已核准設計方向（aggTrades tick 級模擬、兩資本場景都跑）

## 0. 背景（為什麼做）

2026-07-13 觀察期複檢發現：新 config 上線 26.5h 網格零成交、零 REALIZED_PNL；
`fetch_my_trades` 與 income 按日聚合證實過去一個月成交僅 0~3 筆/天、月已實現 ≈ -0.14。
機制：live 每個 bookTicker 事件經 `bot.py:294 _should_adjust_grid`（偏離 anchor ≥
`grid_spacing * 0.5` = 0.15%）+ 5s per-side cooldown 就撤單重掛，而掛單距離是
±0.3% → 掛單被追價永遠搬走，只有引擎反應間隙內暴走 >0.3% 才成交。
1m backtester（`backtester.py:712 _settle`）讓掛單活滿整根 bar → 回測成交率
~17 筆/天 vs 實盤 ~1 筆/天，高估一個數量級以上；#14 的 Δeq 絕對值全部高估，
「網格慢慢磨回 uPnL -68」在實盤成交率下不成立。

## 1. Goals

1. 在同一 tick 級價格路徑上，量化追價門檻 0.5×spacing（現行）vs ≥1.0×spacing
   （掛到成交才重掛）的 Δeq / max_drawdown / 強平 / 成交率 / 保證金拒單率差異，
   產出「要不要改 live 追價語意」的裁決依據。
2. 把追價門檻從 hardcode 變成接了線的 config 參數 `requote_threshold_factor`
   （預設 0.5 = 現行為 bit-identical），live 與純層單一來源。
3. 校準 gate：tick 模擬器在舊語意下必須重現 live 實測成交率，否則模型不可信、
   對比作廢。

## 2. Non-goals

- 不改 grid_spacing / take_profit_spacing。
- 不重開 bandit / leading / dgt / 任何增強。
- 本任務不直接翻 live 行為：`requote_threshold_factor` 上線後仍設 0.5，
  改值是使用者看完驗證數字後的獨立決定。
- 不做多 symbol、不做 post-only 改造（列為未來選項）。
- 不重構 GridBacktester 既有 1m 路徑（只加等價守門，不動其撮合語意）。

## 3. Security / Safety constraints

- 全程 read-only 對交易所：只下載公開 aggTrades 資料 + 既有 read-only API 查詢；
  不下單、不改 live config、不重啟引擎。
- API key 照舊走 config 檔，不進 code、不進 git。
- 生產引擎在本機跑（as_terminal_max）：寫檔僅限 `data/`（新快取）與
  `docs/`、`tasks/`、tests；不碰 `config/`、`logs/`、`log/`。

## 4. 架構

### 4.1 aggTrades loader（`backtest/` 新模組）

- 來源：Binance Vision UM futures 日檔
  `https://data.binance.vision/data/futures/um/daily/aggTrades/BNBUSDC/`。
- 範圍：2026-06-06 ~ 執行日前一日（UTC 日界，**不用本地時區**——上次 kline
  Taipei 日界偏移 8h 的 bug 不重演）。
- 完整性驗證：逐日斷言（a）檔案存在且非空（b）時間戳覆蓋整日（首筆 <00:05、
  末筆 >23:55 UTC）（c）時間戳單調不減。未過完的當日**不入快取**
  （上次 `07-10` kline 部分日 + skip-if-exists 毒快取教訓）。
- 壓縮：轉「價格變動事件流」（連續同價 tick 去重，保留每事件的 ts/price/qty
  聚合）。決策只依賴價格穿越 0.15%/0.3% 門檻，去重不損失保真。
- 快取：`data/futures/um/daily/aggTrades/BNBUSDC/` 原始檔 + 壓縮事件流
  parquet/csv 快取；快取檔名帶日界與壓縮版本，避免舊快取被新邏輯誤讀。

### 4.2 tick 事件模擬器（`backtest/` 新模組）

事件迴圈重放事件流，模擬 live 迴路，**共用**：

- `grid_engine/decision.py` 的 `decide()`（signal 邏輯唯一來源，量化規則）；
- `backtest/costs.py`（fee/slippage/funding）；
- #14 的 seed 注入語意（per-lot FIFO、margin 扣 balance、seed 驗證 raise）；
- GridBacktester 的保證金/強平規則（`_open` 保證金不足拒單 = -2019 等價、
  盤中最不利價強平判定、equity = balance + open_margin + unrealized）。
  **共用方式：從 backtester.py 抽出共用 helper（或 import 呼叫），禁止複製
  貼上第二份帳務邏輯**（lessons 重複 class 族）；抽出時 GridBacktester
  既有測試必須全綠不改斷言。

模擬 live 特有機制（1m backtester 沒有的）：

- deviation gate：`requote_threshold_factor` 參數化門檻；
- **5s per-side cooldown**（`position_adjust_cooldown` 語意）；
- 決策延遲：撤單/掛新單於觸發事件後 delay 生效（預設 500ms，
  敏感度掃 {0ms, 500ms, 1s}）。

成交判定（保守界，Red Team V1/V2 防禦）：

- 只認「掛單放置生效**之後**的 trade **嚴格穿越**掛單價」
  （買單：trade price < limit；賣單：trade price > limit）；
  at-price touch 不算成交（排隊位置未知）。
- 成交價 = 掛單價（maker），執行成本 haircut 由 costs.py 疊加。

### 4.3 校準 gate（先跑，FAIL 即停）

- 主 gate：舊語意（factor=0.5）+ 現狀 seed 倉位，套在 07-12 14:51 之後的
  觀察窗（雙側正常報價、零 -2019 的乾淨窗口，跑到執行日）。
  live ground truth：該窗口網格成交 = 0 筆。
  **PASS 準則：模擬成交 ≤ 2 筆/日均**（區分 0~2 vs 17 的數量級即可，
  窗口短、統計弱，誠實標註）。
- 次 gate（sanity）：6 月整月，模擬 vs income 實測 0~3 筆/天，只比數量級——
  該段 live 有 -2019 擋進場單壓低實際成交，模擬預期略高，偏離一個數量級
  以上才算 FAIL。
- FAIL → 停手回查模型，不得進入對比實驗。

### 4.4 門檻參數化（live code 變更）

- `grid_engine/config.py` 新欄位 `requote_threshold_factor: float = 0.5`，
  `from_dict` 正規化（型別/範圍 (0, 10]，垃圾值 fallback 0.5 並記 log）。
- `DecisionInputs` 新欄位；`decision.py:125 should_adjust` 改讀
  `inputs.grid_spacing * inputs.requote_threshold_factor`。
- `bot.py:294 _should_adjust_grid` 前置 gate 讀同一 config 欄位
  （兩處同源，接線測試釘死不再添假旋鈕）。
- decisions.jsonl inputs 新欄位；`replay.py` 向後相容：舊記錄缺欄位 →
  預設 0.5（既有 98,546 筆 replay 結果不變，回歸測試釘死）。
- 預設 0.5 全套 bit-identical 回歸（含 replay 零 diff、backtester 結果不變）。

## 5. 實驗矩陣

| 維度 | 取值 |
|---|---|
| 語意 factor | 0.5（現行）, 1.0, 1.5 |
| 資本場景 | A: 現狀（錢包 184.6、seed 多 0.58@690.29/空 0.34@571.75、5x） B: 入金 25 + 補中性（錢包 209.6、seed 0.58/0.58） |
| 窗口 | W1 漲 06-06~06-16、W2 跌 06-15~07-02、W3 震盪 06-25~07-10、全程 06-06~執行日 |
| cost sens | fee {2,4} bps × slip {0,1,2} bps |
| 決策延遲 | {0ms, 500ms, 1s}（僅基準 fee/slip 下掃） |

總組合數與全部結果一併揭露（multiple testing 規則）；交易次數 <30 的
cell 標「樣本不足，統計上不可信」。

## 6. Ship 判準（可判定驗收）

改 live 追價語意（factor 0.5 → 1.0）的建議門檻，**全部**滿足才建議：

1. 校準 gate PASS（§4.3）。
2. 新語意（1.0）零強平，兩資本場景、全窗口。
3. 新語意 Δeq 在 W1/W2/W3 三段全 ≥ 舊語意（0.5），且全程窗口為正。
4. cost sens 矩陣內排序不翻轉；排序差距 > 成本擾動量級。
5. 場景 A（現狀資本）報告保證金拒單率（= 遭拒進場單數 / 嘗試進場單數）；
   若新語意在任一窗口拒單率 >30% → 結論降級為「上線需綁入金 ~25（TODO 2）」。
6. 1.5 與 1.0 結果對照（懸崖偵測：最佳點若卡在掃描邊界，依 lessons
   不採納單點，報 sensitivity curve）。

任一不滿足 → 產出誠實結論（含「不改、接受倉位凍結、另議出場」路線），
交使用者裁決。

## 7. Red Team 攻擊向量與防禦（實作前定案）

| # | 向量 | 防禦 |
|---|---|---|
| V1 | Lookahead：同一 trade 既觸發撤單又判成交 | 成交只認掛單生效後的嚴格穿越；決策延遲建模（§4.2） |
| V2 | 成交樂觀：at-price touch 當成交 | 嚴格穿越才成交（保守界） |
| V3 | 容量：忽略保證金限制讓氧氣場景失真 | 沿用 `_open` 拒單語意 + 報告拒單率（§6.5） |
| V4 | 資料品質：aggTrades 缺日/部分日/毒快取 | 逐日完整性斷言、未過完日不入快取、UTC 日界（§4.1） |
| V5 | 成本：暴走段成交逆選擇重、單點 fee 誤導排序 | tick 路徑天然含逆選擇；fee/slip 全矩陣 cost sens（§5） |

## 8. 已知局限（誠實揭露，隨結果一併交付）

- 單 symbol（BNBUSDC）、單歷史段（2026-06-06 起）in-sample；無 walk-forward。
  結論**只看方向與相對排序，不當精確預測**。
- seed per-lot FIFO 與 Binance netted 均價分歧（FIDELITY_NOTES 12）繼續適用。
- aggTrades 是成交流不是報價流：以 trade price 近似 bookTicker mid 觸發
  deviation gate，低流動時段觸發時點有偏差（方向不定，靠決策延遲敏感度
  部分覆蓋）。
- 校準窗口短（觀察期數日、live 成交 ≈ 0），gate 只能區分數量級。

## 9. 測試策略

- 模擬器 unit tests：手工 tick fixtures，每個守衛 mutation red-once；
  fixture 禁止把待測維度設成 0/常數/退化值（lessons 通則 3）。
  必測：嚴格穿越 vs touch、cooldown 擋 requote、決策延遲窗口內成交歸屬、
  保證金拒單、強平觸發。
- 等價守門：退化路徑（1 tick = 1 bar、factor=0.5、零延遲零 cooldown）
  tick sim vs GridBacktester 結果一致。
- 參數化回歸：factor=0.5 全套 bit-identical（既有測試 + replay 零 diff）。
- 校準 gate 寫成可重跑 script（吃「執行日」參數，觀察期延長可重驗）。

## 10. 交付物

1. aggTrades loader + tick 模擬器 + 測試（merge 進 main）。
2. `requote_threshold_factor` 參數化（預設 0.5，行為不變）上線。
3. 校準 gate script + 實驗矩陣結果報告（含全部組合、局限、建議）
   → 使用者裁決是否翻 1.0 / 綁入金 / 不改。
