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
- GCE 遠端檔案存取不在本次範圍。

### 頁2 交易對管理
- `SymbolConfig` 改用 `grid_engine.config.SymbolConfig`，讀寫同一份 trading_config_max.json。

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
1. `to_backtest_config(SymbolConfig) -> backtest.Config` — 單位陷阱集中地（舊 `take_profit_spacing` 存百分比小數 0.004=0.4%，新系統單位需驗證），已知輸入→已知輸出鎖死映射。
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

**不動**：`asBack/`；根目錄 `utils.py`/`constants.py` 刪除階段 grep 確認，仍有新系統引用就留。

## 測試與驗收

- **單元（TDD）**：service 層三個轉換函數黃金測試；config round-trip 測試（現有 JSON 副本 → load → save → load，斷言 symbols 數量與關鍵欄位不遺失）。
- **功能對等對比（僅 Phase 1 期間可做，必須在刪碼前）**：同一 symbol+日期，舊 BacktestManager vs 新路徑各跑一次單次回測，對比收益率/回撤方向與量級（不要求 zero-diff，差數量級=單位映射炸）。
- **回歸**：每 Phase 結尾全套 pytest，報數字（基線 270 passed）。
- **Playwright 實點**（hard-reload 後）：頁1 載歷史、頁2 建/改/存後驗 JSON、頁3 完整流程（載資料→單次回測→兩種優化各一輪小 trial→Monte Carlo）、頁4 測連線含無 key 錯誤路徑。
- **Monkey testing**：空日期範圍、無資料 symbol、trials=0、decisions.jsonl 損毀行/空檔、config JSON 手刪欄位、優化中途關頁。目標：友善錯誤，不噴 traceback。
- **驗收**：fresh-context verifier（重讀檔+實跑測試）→ dual-review 兩輪。

## 風險 Top 3（scout 盤點）

1. 頁1 資料源全換（bot.state → decisions.jsonl），頁面結構性重寫量最大。
2. 頁3 新舊引擎不相容：參數轉換、結果欄位、優化接口全要適配。
3. SymbolConfig↔backtest.Config 單位/欄位不一致（take_profit_spacing 等），錯了不會炸只會給錯回測結論——黃金測試 + 新舊對比防禦。
