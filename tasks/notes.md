# Notes

## 2026-08-14 userData stream 死因調查（決定性實驗，why 留檔）

**結論先講**：`ws_client.py:64` 的「`SUBSCRIBE [listenKey]` vs 直接連 `/ws/<listenKey>`」
這個假設**已被否決**，不要改它。

**為什麼要做這個實驗**：07-30 修好 `-1125` 後，log 仍零筆 `[userData]`。原本最合理的懷疑是
Binance 對無效 stream name 靜默接受（`ws_client.py:44-47` 的註解就是這個形態），而
SUBSCRIBE 一把 listenKey 也許正是「被靜默接受但不推」。

**實驗設計**：兩條連線同時掛同一把新取的 listenKey，一條走 A（SUBSCRIBE）一條走 B（path URL），
再加對照組（同連線同時訂 bookTicker + listenKey）。純被動，不下單。

**結果**：A 零事件、B 零事件、對照組 bookTicker 2360 筆/5 分鐘而 userData 0。
REST `allOrders` 證明同時窗確實有 8 筆 CANCELED/NEW。⇒ socket 健康、key 有效、事件存在，就是不推。

**方法論教訓（值得記住）**：第一輪實驗**無效**，因為腳本沒做 listenKey keepalive，
60 分鐘後 key 就死了，跑了 6.6 小時的「零事件」證明不了任何事。
證據是重取時拿到的是新的一把 key（`hC98…` ≠ 原本的 `AaTj…`）。
**任何用 listenKey 做的長時間觀察，都必須自帶 25 分鐘 PUT keepalive，否則結論全廢。**

**已排除**：Portfolio Margin（`enablePortfolioMarginTrading: false`）、multi-assets（false）、
API 權限（`enableReading`/`enableFutures` 皆 true）、socket 健康度。
**未排除（下一步）**：`ipRestrict: true` + 家用浮動 IP。實驗期間 request ip 從
`36.225.15.37` 變成 `118.150.131.186`，log 裡累計出現過 6 個不同 IP。
若 Binance 推送時也校驗來源 IP，症狀會恰好是靜默不推——這條若成立，GCE 固定 IP 會一併解掉。

## 2026-08-14 `assumed_leverage` 守衛：為什麼掛在 `__setattr__` 而不是 `from_dict`

第一版掛在 `from_dict`，verifier 抓到 web 有三個寫入點（`2_⚙️_交易對管理.py:194,306`、
`3_🔬_回測優化.py:214`）直接建構 dataclass 或直接賦值，完全繞過守衛，只靠 Streamlit
widget 的 client 端範圍限制撐著。改掛 `SymbolConfig.__setattr__` 後，因為 dataclass
`__init__` 也走 setattr，一次涵蓋 from_dict / TUI / web 三點 / REPL，`from_dict` 那行守衛
連同 `as_terminal_max.py` 的兩處包裝都可以拿掉（少三個要記得維護的地方）。

同時記下 verifier 的另一個發現：原測試用 `5.7` 驗「非整數要拒絕」是**假守衛**——
`int(5.7)==5` 恰好等於 fallback 值，靜默截斷與正確拒絕在結果上無法區分，
所以「拿掉 `is_integer()`」這條 mutation 存活。改用 `7.3`/`20.9`（截斷值 ≠ fallback）才抓得到。

## 2026-07-03 架構審查結論（quant 視角）

### 全貌
- 專案 ~25.5k 行，實為**兩套平行交易系統 + 一個 Web 儀表板**：
  1. 舊系統：`main.py` → `ui/menu.py` → `core/bot.py`(1797行) + `exchanges/` + `indicators/`
  2. 新系統（實際跑錢）：`as_terminal_max.py` → `grid_engine/`（自包含，不依賴 core/exchanges）
  3. Web：`web/app.py`（Streamlit）混用 `core/backtest` + `backtest/` + `exchanges/`
- 重複三份實作：`MaxGridBot`、`BacktestManager`、`GridStrategy` 在 core/ 與 grid_engine/ 各一份
- 唯一純死碼：`asBack/`（僅資料）、`grid_engine/backtest.py`（325行，無人引用）

### P0 發現（風險排序）
1. **回測驗證的不是實盤策略**：`backtest/backtester.py:18` import `core.strategy.GridStrategy`（舊系統），實盤跑的是 `grid_engine/bot.py` 的獨立邏輯 → 回測優化出的參數對實盤策略不保證有效
2. **同步 ccxt 阻塞 event loop**：`grid_engine/bot.py:13` `import ccxt`（非 pro），`place_order`/`cancel_orders_for_side`/`fetch_open_orders` 都在 WS callback 的 async context 直接同步呼叫，無 to_thread/executor → 每次 REST 呼叫期間整個 WS 處理停擺
3. **無鎖並發**：4-5 個 asyncio task 共享 `sym_state`/`accounts`，無任何 Lock；WS ticker 每 tick 跑 `adjust_grid` 與 `_handle_order_update`/`_sync_*` 競爭
4. **下單無冪等/無 backoff**：`place_order`(bot.py:332) 無 clientOrderId、無 retry/backoff、例外吞掉 return None（BTC Margin insufficient 43萬次即此因）；有倉位時每 tick 撤單重掛無冷卻

### P1
- MaxGridBot god class：WS/下單/風控/同步/通知全在一個 1000 行 class
- 回測成本模型缺滑價、缺資金費率（實盤有 FundingRateManager，回測沒有）
- Bandit optimizer 有 to_dict/load_state 但 bot 從未持久化 → 重啟歸零重學
- 交易狀態零持久化，重啟全靠交易所 REST 同步

### 建議路線（未執行，待使用者決定）
1. 短期：place_order 加 backoff + clientOrderId；有倉位時 adjust_grid 加冷卻（順帶解 BNB 高頻 TODO）
2. 中期：同步 ccxt 換 ccxt.pro 或包 to_thread；關鍵共享狀態加 asyncio.Lock
3. 中期：讓回測直接吃 grid_engine 的策略邏輯（抽出純函數 strategy core）
4. 長期：淘汰 core/ 舊系統（確認 web 依賴遷移後），刪 grid_engine/backtest.py 死碼

### #2+#3 async 卸載與並發鎖（2026-07-03 完成，86acd3e..800fd98）
- 架構：單 worker ThreadPoolExecutor 序列化全部 ccxt REST（Session 非 thread-safe，故不用 to_thread）；鎖序單向 `_sync_lock → symbol lock`；adjust_grid skip-if-locked（ticker 高頻，排隊=積壓過期決策）；apply 一律「fetch 鎖外、寫回鎖內無 await」
- Spec/Plan：docs/superpowers/specs、plans 下同名 2026-07-03 檔（未入版控）
- **Review 留下的 follow-up（非 blocker，merge 後小 patch）**：
  1. `_check_trailing_stop` 例外被 `_sync_account` 外層 except 記成「同步帳戶失敗」→ 給它獨立 try/except（log 語意）
  2. bot.py `_sync_account` 的 `create_task(_check_risk_and_notify())` 未存引用（pre-existing，與 `_register_order_failure` 已修的 pattern 不一致）
  3. `place_order` 在 executor shutdown 後被呼叫會把 RuntimeError 計入退避/斷路（停機噪音）→ 可在 except 內對 `_stop_event` 早退
- **重要認知（改架構前必讀）**：現行拓撲所有交易路徑仍串行在單一 WS recv task 上 → 鎖目前實際不競爭（防未來併發化的正確投資）；「WS 新值 vs REST 舊快照」race 是靠串行性防住的，不是靠鎖。若未來把 `sync_all` 拆成獨立 periodic task（讓 sync 不再 head-of-line 阻塞 ticker），防重入鎖+原子 apply 已就緒，但需另補 staleness 防護
- `_grid_step` 直接索引 `state.symbols[s]`：若未來加「runtime 動態 enable symbol」須回補 guard，否則 KeyError 會打穿 `_websocket_loop` 變重連風暴

### #8 新舊引擎成本歸零對比（2026-07-06，Phase 2 刪 core/ 前守門）— **FAIL**

**方法**：`scripts/compare_backtest_engines.py <symbol> <start> <end>`。成本對齊：新引擎 `zero_costs=True` 後手動設 `fee_pct=0.0008`（每邊 0.0004，對齊舊引擎 core/backtest.py:230 寫死值）；舊引擎 kwargs 全關（`hard_stop_pct=1e9, slippage_pct=0.0, funding_rate=0.0`）。position_threshold/limit 映射核對一致：舊 `SymbolConfig.position_threshold=0.4/position_limit=0.1`（grid_engine/config.py 實例）與新 `initial_quantity×threshold_multiplier=0.02×20=0.4`、`×limit_multiplier=5=0.1` 相符，非映射參數 bug。

**資料選擇踩坑**：`DataLoader()` 預設 `data_dir` 只有 `data/funding/`，K 線實際在 `asBack/data/`（script 已顯式指定，見檔頭注解）。且 config 內 symbol（BNBUSDC/ETHUSDC/SOLUSDC/BTCUSDC）grid_spacing 統一 0.006（0.6%），BNBUSDC 近期（2026-06-11~15）1 分鐘 K 線單根最大波幅僅 0.56% < grid_spacing → 兩邊皆 0 交易（0/0 無資訊量），改選有較大單根波動的區間。

**跑了兩組區間，結果一致（非單次噪音）**：

| Symbol/區間 | 舊 return% | 新 return% | 舊 maxDD% | 新 maxDD% | 舊筆數 | 新筆數 |
|---|---|---|---|---|---|---|
| ETHUSDC 2026-01-25~31（7天,10080根） | +0.1163 | **-0.1083** | 0.1162 | 0.3462 | 60 | 23 |
| BNBUSDC 2025-11-17~23（7天,10080根） | +0.0949 | **-0.0504** | 0.0949 | 0.2280 | 35 | 11 |

**判讀：FAIL**——return_pct **方向相反**（舊賺、新虧，兩組區間皆然，非隨機性，兩邊已用固定種子/零滑價確定性跑），max_drawdown 新引擎恆大 2-3 倍，成交筆數新引擎恆少（約舊的 1/3）。量級雖在 0.2x~5x 內未觸發「差一個數量級」的 bug 警戒線，但 PASS 判準明定「方向一致」是必要條件，此處未過。

**根因（讀碼定位，未動 to_backtest_config，遵照指示 FAIL 就停）**：兩引擎的成交模擬架構本質不同，非參數映射問題：
- 舊引擎 `core/backtest.py` `run_backtest`：每根 K 線用**當根收盤價**即時重算網格進場/止盈價，並用**同一根**的 high/low 判斷是否觸發（`core/backtest.py:329,358,395,424`）。
- 新引擎 `backtest/backtester.py` `_run_terminal_ui_mode`（約 line 529-654）：「追價語意」——本根算出的 pending 掛單，要等**下一根**的 close 價格穿越才成交（`_settle`，line 624-637），且用 close 而非 high/low。
這代表新引擎的訂單觸發時機系統性延後一根、且判定基準（close vs high/low）不同，導致同一組行情產生的進出場時點與盈虧結構性不同 —— 屬於**引擎重寫的架構差異**，不是簡單的 kwargs/multiplier 映射 bug，不能靠改 `to_backtest_config` 修。

**Blocker for #9 Phase 2**：在此差異被判讀（是否為刻意重新設計、可接受）或修正之前，不建議直接刪 `core/backtest.py` 作為唯一對照組——建議先讓使用者決策：(a) 接受新引擎的追價語意為既定重新設計並記錄差異，或 (b) 修 `backtest/backtester.py` 觸發邏輯貼近舊引擎同根 high/low 判定後重跑本 script 驗證。

全套回歸：`uv run pytest tests/ -q` → 294 passed（unchanged，本次僅新增 script，未動任何 src）。

### #9 Task 8 裁決（2026-07-06，使用者拍板）
對比 FAIL（return 方向相反）→ **接受新引擎為基準，進 Phase 2**。根因非映射 bug（position_threshold/limit 核對一致），是 #4 刻意撮合重設計：舊引擎同根 high/low 盤中觸發（look-ahead 傾向），新引擎追價語意+settle-then-decide 鏡像實盤 decide()（replay zero-diff 守門的那套）。±0.1% 微利量級下撮合時機差異足以翻方向。舊引擎正因不忠於實盤而被刪，不是基準。FIDELITY_NOTES 已有「crossing 只看 close」揭露項。

### #9 fee 對齊修正後重跑（2026-07-06，Critical review finding 修復）

**發現**：`compare_backtest_engines.py` 原本設 `new_cfg.fee_pct = 0.0008` 的推論依據是 backtester.py:282/324 的 fee/2，但那段是 `_run_legacy_mode`（未執行路徑）。新引擎實際跑的是 `_run_terminal_ui_mode`（backtester.py:540 起），其 `_open`/`_close` 內 `fee = qty * fill_price * fee_pct`（line 587,606,616）——**無 /2**，每邊直接收整個 `fee_pct`。舊引擎每邊收 0.0004（core/backtest.py:230）。故正確對齊值是 `fee_pct = 0.0004`，原本的 0.0008 讓新引擎多付一倍手續費，在 ±0.1% 微利量級足以是翻方向的混雜變量。

**修正**：`new_cfg.fee_pct` 0.0008 → 0.0004，同步修正註解與 docstring usage 範例。

**重跑結果（同兩組區間）**：

| Symbol/區間 | 舊 return% | 新 return%（修正前 -0.1083） | 新 return%（修正後） | 舊筆數 | 新筆數 |
|---|---|---|---|---|---|
| ETHUSDC 2026-01-25~31 | +0.1163 | -0.1083 | **-0.0824** | 60 | 23 |
| BNBUSDC 2025-11-17~23 | +0.0949 | -0.0504 | **-0.0350** | 35 | 11 |

maxDD：ETHUSDC 舊 0.1162% vs 新 0.3326%；BNBUSDC 舊 0.0949% vs 新 0.2267%（皆略降但仍 2-3 倍於舊引擎，符合 fee 減半後虧損規模同步縮小的預期）。

**判讀**：fee 修正後 return **方向仍相反**（舊賺新虧，兩組區間皆然），只是虧損量級縮小（fee 減半後虧得少一點，符合預期，但方向不變）。這證實了先前 FAIL 的主因**不是** fee 對齊 bug，而是既有判讀成立的撮合語意差異（同根 high/low vs 追價 settle-then-decide）——修正後結論更穩固，#9 Phase 2 裁決（接受新引擎為基準）維持不變，無需回頭修正該裁決。

全套回歸：`uv run pytest tests/ -q` → 294 passed（unchanged）。
