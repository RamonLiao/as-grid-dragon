# #9 web/ 遷移設計 — 脫離舊系統並刪除 core/

日期：2026-07-06
狀態：已與使用者逐節確認

## 目標與範圍

**終局**：web/ 全面遷移到新系統（`grid_engine/` + `backtest/`），然後刪除舊系統（`core/`、`ui/`、`exchanges/`、`main.py`），`config/models.py` 瘦身成 indicators 專用。

**已拍板的決策**：

| 決策點 | 結論 |
|---|---|
| 終局 | web 全遷 + 刪舊系統 |
| bot 生命週期 | web 砍掉啟動/停止 bot 能力（生產在 GCE 跑 grid_engine，web 只做監控/回測/設定） |
| 回測頁深度 | 功能對等全遷（單次回測、參數優化、Monte Carlo） |
| 優化器 | 兩套都接（optimizer 網格搜尋 + smart_optimizer Optuna TPE），UI 給選項 |
| config/models.py | 瘦身：刪與 grid_engine 重複的 SymbolConfig/GlobalConfig/RiskConfig 及 State 類，留 4 個 indicator config |
| exchanges/ | 整包刪，頁4 簡化成 Binance 專用（ccxt 直連，與 grid_engine 同路） |
| 頁1 監控 | 降級成歷史檢視，改讀 `logs/decisions.jsonl` + `logs/bandit_state.json` |

**硬約束**：#4 的 24h replay zero-diff 驗收未完成，**本任務不動 grid_engine 本體**（引擎加狀態匯出等留待後續任務）。

**執行策略**：兩階段「先接新、再刪舊」。Phase 1 結束時舊碼成為孤兒（grep 可證零引用），Phase 2 刪除變成純機械操作。每階段結尾全測綠 + Playwright 實點。

## 現況依賴地圖（2026-07-05/06 scout 盤點）

- `web/state.py:21` → `config.models.GlobalConfig`；`:148` → `core.bot.MaxGridBot`
- `web/pages/2:26`、`pages/3:29` → `config.models.SymbolConfig`
- `web/pages/3:31` → `core.backtest.BacktestManager`（呼叫點：227 get_available_dates、242 download_data、246 load_data、256 run_backtest、394 optimize_params）
- `web/pages/4:37,158,223` → `exchanges`（列表 / get_adapter 測連線 / fetch_balance+positions）
- `web/components/sidebar.py:62` → `state.is_trading_active`（間接依賴）
- 頁1 全頁資料來自 in-process `bot.state`（`pages/1:146,218`），砍 bot 後失能
- Monte Carlo 段（`pages/3:929-1013`）**已用新 GridBacktester**，不需遷移
- 兩套系統共用同一份 `config/trading_config_max.json`（`grid_engine/utils.py:29`）
- `tests/` 零依賴舊系統（270 tests 全綁新系統）
- `main.py:9` → `ui.menu`；`ui/menu.py:24-26` → config.models + core.backtest；`ui/terminal.py:18` → config.models
- `coin_selection/ws_provider.py:37-39`、`symbol_scanner.py:35-36` 的 `try: from core.logging_setup...` 引用的模組已不存在，恆走 except fallback（死碼）
- `scripts/check_web_system.py:73,118,225` → core.bot + exchanges；`scripts/check_symbol_conversion.py` → 4 交易所 adapter

## Phase 1：web 接新系統

### state.py
- 刪：`start_trading`、`stop_trading`、`get_bot`、`is_trading_active`、`get_trading_stats`、`get_trading_duration`，及 `trading_active`/`bot`/`bot_thread` session 欄位。
- 留：`get_config`/`save_config`/`reload_config`/`check_config_updated`/`init_session_state`，import 改 `grid_engine.config.GlobalConfig`。
- **前置驗證**：新舊 GlobalConfig 欄位集不同（535 行 vs 247 行）。實作前 diff 兩邊 schema，確認 `grid_engine.GlobalConfig.from_dict()` 讀現有 JSON 缺欄位有 default、多欄位安全忽略。

### app.py + components/sidebar.py
- app.py 首頁：砍啟停按鈕與 bot 統計（58-104、147-211 行區塊），改配置摘要 + 引擎產出概況。
- sidebar：移除 `is_trading_active`，狀態改「引擎於 GCE 運行」說明或以 decisions.jsonl 最後事件時間顯示最後活動。

### 頁1 交易監控 → 歷史檢視
- 資料源：`logs/decisions.jsonl`（tail 解析成 DataFrame：決策時間軸、per-symbol 事件）+ `logs/bandit_state.json`（bandit 臂權重）。
- 保留頁面圖表結構，只換資料層。檔案不存在/為空時顯示引導訊息。
- GCE 遠端檔案存取不在本次範圍。**操作缺口明寫（conscious decision）**：實盤即時可觀測性（保證金/爆倉風險）不靠 web——告警管道是 grid_engine 內建的 Telegram notifier（`grid_engine/notifier.py`），web 只是事後歷史檢視，不是監控台。頁1 頂部放一行說明避免誤用。

### 頁2 交易對管理
- `SymbolConfig` 改用 `grid_engine.config.SymbolConfig`，讀寫同一份 trading_config_max.json。
- **存檔採 merge-preserve，不做整檔改寫**（review finding #1/#4/#5 的統一解）：`grid_engine.from_dict()` 會過濾未知欄位，直接 `to_dict()` 覆寫會把 JSON 裡引擎 schema 沒有的欄位靜默抹掉——已確認會死的有 per-symbol `trading_mode`（頁3 拿它選優化參數 bounds，`web/pages/3:381,424`）、risk 的 `hard_stop_enabled/max_loss_pct/max_position_loss_pct`、top-level `exchange_type/testnet`。web 存檔改成：讀原始 JSON → 只更新編輯過的已知欄位 → 未知 key 原樣保留寫回。此機制放 service/state 層，**零改動 grid_engine**（符合硬約束）；#4 驗收完成後可另開任務把 `trading_mode` 正式收編進引擎 schema。
- 一次性驗收：首次存檔前備份 trading_config_max.json；用現況 JSON 副本跑 merge-preserve 存檔 → diff，斷言零欄位遺失。
- **Conscious decision 明寫**：config 裡的硬止損（hard_stop）只有舊 core/bot 實作（`core/bot.py:1634`），生產 grid_engine 的 risk_monitor 根本不讀——即「你以為有的硬止損在生產上本來就沒生效」。本任務只保欄位不丟；生產引擎要不要補 hard_stop 另開任務決定。

### 頁3 回測優化
新增薄服務層 `web/services/backtest_service.py`（頁面導向新接口，非舊介面相容層），集中所有轉換邏輯，可脫離 Streamlit 單測：

| 舊呼叫 | 新對應 |
|---|---|
| `get_available_dates()` → List[str] | `DataLoader.get_date_range()` → (start, end)，日期選擇器改 range 模式 |
| `download_data(...)` | `DataLoader.download(symbol, ccxt_symbol, start, end, exchange=...)` |
| `load_data(...)` | `DataLoader.load(symbol, start, end)` → DataFrame |
| `run_backtest(cfg, df)` → dict | `to_backtest_config(cfg)` → `GridBacktester(config, df).run()` → view 轉換 |
| `optimize_params(cfg, df, cb)` | UI「優化模式」radio：網格搜尋 → `optimizer.py`；智能優化 → `SmartOptimizer(df, base_config, param_bounds…).optimize(progress_callback=…)` |

三個轉換函數（各配黃金測試）：
1. `to_backtest_config(SymbolConfig) -> backtest.Config` — 已知輸入→已知輸出鎖死映射。**真正的語義陷阱（review 修正）**：`take_profit_spacing` 兩邊單位其實一致（皆小數比例 0.004=0.4%），危險在 `backtest/config.py:45-48`——`position_threshold` 預設絕對值 500、`position_limit` 預設 100，**必須顯式設 0 才會走 `initial_quantity×multiplier` 自動計算**；且 `initial_quantity` 預設 0.0，忘了帶入=空回測。黃金測試必須斷言 `position_threshold==0 and position_limit==0` 且 `initial_quantity` 正確帶入。另把頁3 用的 `trading_mode` 一併傳給優化器（見頁2 merge-preserve）。
2. `backtest_result_to_view(BacktestResult) -> dict` — 對齊頁面現有渲染欄位。
3. `optimization_to_view(OptimizationResult | SmartOptimizationResult) -> DataFrame` — 兩種優化結果歸一成單一結果表 + param_importance，頁面單一渲染路徑（不在頁面 isinstance 分流）。

其他：Monte Carlo 段不動；維持 progress callback 進度條；實作前以 `uv` 確認 Optuna 已裝。

### 頁4 設定
- 砍多交易所選單，API key 段只留 Binance。
- 測試連線改 `ccxt.binance()` 直連 `load_markets()`/`fetch_balance()`。

## Phase 2：刪舊系統

每刪一項前先 `grep` 證明零引用；全部完成後全套測試 + web 實跑。

**整包刪除**：`core/`、`ui/`（menu.py + terminal.py）、`main.py`、`exchanges/`、`scripts/check_symbol_conversion.py`。

**修改**：
- `config/models.py` 瘦身：刪 SymbolConfig/RiskConfig/GlobalConfig/SymbolState/AccountBalance/GlobalState，留 SerializableMixin + MaxEnhancement/BanditConfig/DGTConfig/LeadingIndicatorConfig。
- `coin_selection/ws_provider.py`、`symbol_scanner.py`：刪死掉的 try 分支，except fallback 內容轉正（行為不變，現在就跑 fallback）。
- `scripts/check_web_system.py`：core.bot/exchanges 檢查項換成新系統對應物。
- `README.md`：移除舊入口啟動方式。

**不動**：`asBack/` — 注意它是 **load-bearing 資料目錄**而非無關備份：`backtest/data_loader.py:71,83-89`、`grid_engine/utils.py:30`、`constants.py:40` 都指向 `asBack/data` 當 K 線資料源，頁3 的 download/load 目標目錄必須對到它。根目錄 `utils.py`/`constants.py` 刪除階段 grep 確認，仍有新系統引用就留（已知：`config/models.py:15` import constants；`web/pages/3:30` 用 `utils.normalize_symbol`——Phase 1 隨頁3 重寫一併遷到 service 層或確認保留）。

## 測試與驗收

- **單元（TDD）**：service 層三個轉換函數黃金測試；config round-trip 測試（現有 JSON 副本 → load → save → load，斷言 symbols 數量與關鍵欄位不遺失）。
- **功能對等對比（僅 Phase 1 期間可做，必須在刪碼前）**：同一 symbol+日期，舊 BacktestManager vs 新路徑各跑一次單次回測。**必須先做成本歸零對齊（review 修正）**，否則對比無效——兩引擎成本模型是設計性不同：手續費舊每邊 0.04%（`core/backtest.py:230,339`）vs 新每邊減半、滑價舊隨機 `uniform(0,0.05%)` vs 新確定性 bps、資金費率舊固定值 vs 新讀真實 CSV、舊有 `hard_stop_pct=0.03` 砍倉 vs 新引擎無 hard_stop。對比時兩邊 fee/滑價/funding 全設 0、舊引擎 hard_stop 關閉，只比純網格邏輯的收益率/回撤；這樣殘餘的量級差才能歸因到參數映射 bug。
- **回歸**：每 Phase 結尾全套 pytest，報數字（基線 270 passed）。
- **Playwright 實點**（hard-reload 後）：頁1 載歷史、頁2 建/改/存後驗 JSON、頁3 完整流程（載資料→單次回測→兩種優化各一輪小 trial→Monte Carlo）、頁4 測連線含無 key 錯誤路徑。
- **Monkey testing**：空日期範圍、無資料 symbol、trials=0、decisions.jsonl 損毀行/空檔、config JSON 手刪欄位、優化中途關頁。目標：友善錯誤，不噴 traceback。
- **驗收**：fresh-context verifier（重讀檔+實跑測試）→ dual-review 兩輪。

## 風險 Top 3（scout 盤點 + 量化 review 修正，2026-07-06）

1. **config 存檔靜默丟欄位**：grid_engine schema 缺 `trading_mode`/hard_stop 三欄/`exchange_type`，整檔覆寫即抹掉且不可逆——merge-preserve 存檔 + 備份 + diff 驗收防禦。
2. **`to_backtest_config` 語義映射**：`position_threshold/limit` 不設 0 就走絕對值 500/100 而非 multiplier、`initial_quantity` 預設 0=空回測——錯了不會炸只會給錯回測結論，黃金測試鎖死。
3. **功能對等對比被成本模型差異污染**：兩引擎 fee/滑價/funding/hard_stop 設計性不同，不歸零對齊就無法區分「正常差異」與「映射 bug」。

（頁1 資料源全換、頁3 接口全適配仍是工作量大頭，但屬可控重寫，非隱性風險。）

## 量化 review 紀錄（2026-07-06，reviewer/opus，實讀 code 驗證）

- 3 must-fix 已修入上文（trading_mode 丟失→merge-preserve；成本歸零對齊；position_threshold/initial_quantity 黃金測試）。
- 2 should-fix 已修入（hard_stop 欄位流失標為 conscious decision + 生產無 hard_stop 事實揭露；首次存檔 schema 改寫加備份+diff 驗收）。
- take_profit_spacing 單位經查兩邊一致（原 spec 誤判為頭號陷阱，已更正）。
- 已驗證：tests/ 零依賴舊系統成立；刪除清單無漏網 import；asBack/ 是資料源依賴非備份。
- 遺留另開任務：生產 grid_engine 是否補 hard_stop 實作；trading_mode 正式收編引擎 schema（#4 驗收後）。
