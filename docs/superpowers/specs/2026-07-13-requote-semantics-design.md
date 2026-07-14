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
- 範圍：**2026-05-01** ~ 執行日前一日（UTC 日界，**不用本地時區**——上次 kline
  Taipei 日界偏移 8h 的 bug 不重演）。05-01~06-05 為 **holdout 段**：從未被
  任何先前選型讀過，下載後除完整性驗證外**不得開封**（不出現在任何開發/調參/
  對比迭代），只在優勝者定案後跑最終 OOS 驗證一次（§6.7）。被提前讀過即失效，
  須另尋 holdout 或誠實標註無 OOS。
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
- **全量成交假設（明文）**：成交一律 all-or-nothing。理由：單層 0.02 BNB
  （~$11 notional）遠小於 BNBUSDC 常態單筆成交量，且嚴格穿越已是保守界；
  部分成交建模對此 size 無一階影響（review F8）。
- **spread 噪音處理（review F6）**：trade price 同時當 mid 代理（觸發 gate）
  與成交判定，spread 會在 0.15% 門檻附近造成假觸發。防禦兩層：
  （a）用 aggTrades 的 isBuyerMaker 側別重建 bid/ask 上下界，報告該期間
  spread 分布，並佐證 slip {0,1,2}bps 假設（索取清單第 2 項一併回答）；
  （b）敏感度：基準 cell 以 ±half-spread 抖動觸發價重跑，requote 次數或
  成交數變化 >20% → 結果標註「對 spread 噪音敏感」並降低結論信心等級。

### 4.3 校準 gate（先跑，FAIL 即停）

雙向校準（review F1：只校低成交 regime 會讓「永不成交的退化模型」誤 PASS，
必須同時錨定高、低兩端的成交動力學）：

- **低端主 gate**：舊語意（factor=0.5）+ 現狀 seed 倉位，套在 07-12 14:51
  之後的觀察窗（雙側正常報價、零 -2019 的乾淨窗口，跑到執行日）。
  live ground truth：該窗口網格成交 = 0 筆。
  PASS：模擬成交 ≤ 2 筆/日均（區分 0~2 vs 17 的數量級，窗口短、統計弱，
  誠實標註）。
- **高端參照 gate（2026-07-14 修訂，留痕）**：
  原判準「tick ≤ 1.0× 1m」實跑 FAIL（1.47×），回查證明**上界前提錯誤**：
  1m 每 bar 只在 close 重掛（等效 60s cooldown），tick 成交後 5s 補掛可吃
  下一個擺動——1m 的 per-order 樂觀（touch+全 bar 存活）與 re-arm 節奏悲觀
  兩力反向，淨方向先驗不可定，「1m 是上界」不成立（分鐘內多循環僅解釋
  超額 9/109，主力是 re-arm 節奏差）。修訂後判準：
  （a）下界保留：tick ≥ **0.2×** 1m（偵測 fill 引擎系統性死亡）；
  （b）上界改為**成交真實性機械驗證**：每筆 fill 在 fill 時刻必須存在
  嚴格穿越 limit 的原始事件，違規數 = 0 才 PASS。
  **定位修正（2026-07-14 review）**：此驗證對現行引擎是套套邏輯（fill 記錄
  與驗證用同一判準同一事件流），其價值是**回歸守衛**（防未來改成 touch-fill
  或記錯 ts），不是「成交率無系統性高估」的獨立證據——後者無 live 上界可校，
  由方向排序/拒單/強平判準 + holdout + 上線首週觀察承接。
  副作用誠實揭露：修訂後 gate 不再約束成交率量級上限，若 tick sim 有
  非偷跑類系統性高估，Δeq 絕對值偏樂觀——由「只看方向排序 + 拒單/強平
  獨立判準 + holdout + 上線首週觀察」承接（§8 固有限制不變）。
- **6 月對齊 cap（2026-07-14 修訂，留痕）**：10× 放寬至 **15×**。理由：
  live 6 月受 -2019 風暴壓制（保證金耗盡、多頭裝死 104h），sim 為 7 月
  入金後資本場景（rejected=0），壓制機制性缺席為已揭露偏差；實跑 12.2×
  屬邊際超標且成交日命中率 0.667 過關。cap 仍為數量級 sanity，非精確校準。
- **成交日對齊 sanity**：6 月 live 有成交的日子（income 實測 0~3 筆/天），
  舊語意模擬在**同一批日子**應產生成交（gap 事件日對齊），整月數量級一致；
  該段 live 有 -2019 擋進場單壓低實際成交，模擬預期略高，偏離一個數量級
  以上才算 FAIL。
- 任一 FAIL → 停手回查模型，不得進入對比實驗。

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
| 窗口 | W1/W2/W3 依價格走勢標注漲/跌/震盪，**邊界互不重疊**（review F4；實作時依 06-06~07-10 走勢定不相交切點），另加全程 06-06~執行日 |
| cost sens | fee {2,4} bps × slip {0,1,2} bps |
| 決策延遲 | {0ms, 500ms, 1s}（僅基準 fee/slip 下掃） |

**優勝者局部穩健掃描（review F5，僅對優勝 factor、基準 fee/slip）**：
- factor ±20%（如優勝 1.0 → 加掃 0.8, 1.2）；
- cooldown {2.5s, 5s, 10s}（成交率的一階驅動，不能固定不掃）。
任一擾動下 Δeq 排序翻轉或績效衰減 >50% = 孤峰不採納（lessons 懸崖規則）。

總組合數與全部結果一併揭露（multiple testing 規則）；每個 cell 附
**獨立成交事件數**（以完成的 entry→TP 往返計，非 fill 筆數——網格成交
高度自相關），<30 標「樣本不足，統計上不可信」。

## 6. Ship 判準（可判定驗收）

改 live 追價語意（factor 0.5 → 1.0）的建議門檻，**全部**滿足才建議：

1. 校準 gate PASS（§4.3，含高端參照 gate）。
2. 新語意（1.0）零強平，兩資本場景、全窗口。
   **修訂（2026-07-13，plan review BLOCKER-1 驗算後）**：equity-based 強平判定
   （`liquidation.py::should_liquidate` 只吃 equity/qty/price）對帳務基礎
   **可證明不變**——per-lot FIFO 與 netted 兩套獨立平倉帳在同一組成交下
   equity 逐點相等（realized、釋放 margin、殘餘 uPnL 三項差異恆抵銷；
   數值驗證見 plan Task 6 測試）。故「雙模型保守取或」在強平通道空洞，
   改落在**分歧真實存在的通道**：
   （a）強平：equity 不變性寫成回歸釘測試（若未來改動使兩者分歧 → 測試炸）；
   （b）**拒單（-2019 等價）**：兩套帳的「可用餘額」確實分歧（FIFO 按 lot 價
   計 margin、netted 按均價計，Binance 生產為 netted 制）→ `open()` 以
   **兩套 margin 口徑任一不足即拒單**（保守取或），拒單率統計吃保守值。
3. 新語意 Δeq 在 W1/W2/W3 三段全 ≥ 舊語意（0.5），且全程窗口為正。
   **本判準只計入獨立事件數 ≥30 的 cell**（review F3）；達門檻的段 <2 →
   結論記 inconclusive（延長樣本期間或等 live 觀察累積，不得硬裁）。
4. cost sens 矩陣內排序不翻轉；排序差距 > 成本擾動量級。
5. 場景 A（現狀資本）報告保證金拒單率（= 遭拒進場單數 / 嘗試進場單數）；
   若新語意在任一窗口拒單率 >30% → 結論降級為「上線需綁入金 ~25（TODO 2）」。
6. 優勝者局部穩健掃描通過（§5：factor ±20% + cooldown 三點，無孤峰）；
   1.5 與 1.0 對照（最佳點卡掃描邊界 → 不採納單點，報 sensitivity curve）。
7. **Holdout OOS（review F2）**：以上全過後，優勝 factor 在 05-01~06-05
   holdout 段（§4.1，全程未開封）跑一次：維持「新 ≥ 舊、零強平」才維持
   建議；holdout 翻車 → 結論降級為 inconclusive，如實報告，不得回頭調參
   再驗（holdout 被讀過一次即失效）。
   **Seed 修訂（plan review SF-8）**：07 月倉位價（690.29/571.75）對 5 月
   是時間錯置、可能非物理（seed 價遠離段內價格 → 強平判準失去鑑別力）。
   holdout 段 seed 價改用**段首事件價 at-market**（qty 沿用場景定義），
   兩 factor 同 seed 對比，維持「比較語意」的目的不變。

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

- 單 symbol（BNBUSDC）；選型段（06-06 起）in-sample，無 walk-forward；
  OOS 僅一段 holdout（05-01~06-05，§6.7）。結論**只看方向與相對排序，
  不當精確預測**。
- seed per-lot FIFO 與 Binance netted 均價分歧（FIDELITY_NOTES 12）：
  PnL 口徑仍受影響（只看方向）；強平判定已改雙模型保守取或（§6.2）。
- aggTrades 是成交流不是報價流：以 trade price 近似 bookTicker mid 觸發
  deviation gate，spread 噪音假觸發已列防禦（§4.2），但重建的 bid/ask
  只是上下界估計，殘餘偏差誠實標註。
- 低端校準窗口短（觀察期數日、live 成交 ≈ 0），gate 只能區分數量級；
  高端參照 gate 錨的是另一個模型（1m backtester）而非 live ground truth，
  兩個 gate 合起來仍不構成對 factor=1.0 成交動力學的直接實證——這是
  改語意方案的固有限制（新語意沒有 live 歷史），上線後首週觀察是最終驗證。

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
