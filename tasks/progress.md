# Progress

## Current Task
無進行中任務。**#7 剛完成（2026-07-05，HEAD=51def8c，未 push——本地新增 12 commits：#7 code 8 + spec/plan/docs 4）**。下一個建議 **#8 清理**（P2 小任務，#7 review 累積 follow-up 都歸它）。#4 剩人工 Task 10（部署後 ≥24h replay zero-diff——部署新版可同時驗收 #4+#7）。

### #7 MaxGridBot god class 拆分 — 完成（2026-07-05）
- 8 commits cf3e10d..51def8c，全套 **268 passed**（基數修正：本機 uv 環境 clean HEAD 實測 257 非 ledger 舊記 267；+11 新測試=268）。SDD 7 task 全 Approved + final whole-branch review(opus, Ready to merge) + dual-review（R1 外部 fresh LGTM、R2 專案規則 conform，無衝突免 tie-breaker）+ verifier ACCEPT 6/6。
- 架構：bot.py 1153→767 行（組合根+生命週期+網格鏈+WS handlers），拆出 7 組件：`context.py`(ExchangeContext 兩階段容器)/`locks.py`(SymbolLocks)/`rest_gateway.py`(單 worker REST)/`order_executor.py`(下單/斷路器/is_blocked)/`sync_service.py`(同步/原子區/maybe_sync)/`ws_client.py`(純傳輸，callback 不包 try)/`risk_monitor.py`/`reporting.py`。bot 的 exchange/precisions/funding_manager 是轉發 ctx 的 property（兩階段初始化：組件呼叫當下讀 ctx，絕不 __init__ 快照）。
- 等價驗證：既有測試斷言全數未改（只遷 patch 路徑）、characterization 74 passed 斷言逐行核對、WS 例外語意 characterization（ticker 例外→重連）、組裝斷言（gateway/locks/ctx/stop_event/tasks 全組件單例）、monkey 跨組件鎖競態（canary 經 no-op 驗證會紅）。
- 關鍵修法：`run()` 的 `self.tasks = [...]` 改 `extend`——OrderExecutor 持共享 list 參照，重新賦值會讓斷路通知 task 逃出 stop() 的 cancel。
- **測試指令注意：`uv run python -m pytest tests/ -q`（系統 python3 無 pytest）。**
- Follow-up（非阻擋，歸 #8）：web/state.py:54 呼叫不存在的 `bot.reload_config`（有 try/except 包）、test_web_system.py required_methods 含 reload_config、`_handle_order_update` sym_config dead assignment、sync 的 fire-and-forget risk task 未納管（原版既有）、tasks list 永不移除累積（原版既有）。
- spec `docs/superpowers/specs/2026-07-05-maxgridbot-split-design.md`、plan `docs/superpowers/plans/2026-07-05-maxgridbot-split.md`、SDD ledger `.superpowers/sdd/progress.md` #7 段。

### 前次任務存檔：#4 回測/實盤策略脫鉤 — 程式碼完成 (Task 1-9 + 8b)，雙輪 review 收斂 (Ship as-is)。Subagent-Driven 執行，10 commits 800fd98..7186203，全套 187 passed。
- 純層 `grid_engine/decision.py`(decide()) + `clock.py`(sim-clock) + `snapshot.py`(共享快照) + `replay.py`(決策日誌重放驗收)；`bot.py` `_grid_step` 接線純層 + 決策日誌落地 `logs/decisions.jsonl`；刪 `strategy.py`→shim；`backtest/backtester.py` 吃 decide()+追價語意；`grid_engine/backtest.py::BacktestManager` 委派 GridBacktester(Task 8b，修 plan 誤判「死碼可刪」引入的 NotImplementedError regression)。
- 每 task 獨立 SDD review(Approved) + final whole-branch(opus, Ready to merge) + dual-review(R1 外部 1 Important→tie-breaker 判 INERT→文件揭露 7186203；R2 專案規則全 conform)。實盤等價逐行驗過 bug-for-bug，log↔replay 契約實跑 0 diff。
- **剩 Task 10（人工）**：部署後 ≥24h 跑 `replay.replay_file('logs/decisions.jsonl')` 期望 diff=0 才算 #4 真正完成。
- **Follow-up(非阻擋)**：決策日誌 rotation/停用開關、makedirs hoist、backtester price==decide() 等價鎖測試、FIDELITY_NOTES 補 crossing 只看 close、observability log 補回。
- SDD ledger 詳情：`.superpowers/sdd/progress.md`。#1-#3 先前完成(86acd3e..800fd98)。全部已 push（2026-07-04, HEAD=80a77bc）。

## TODO — 架構審查修復清單（2026-07-03，詳見 tasks/notes.md，依序修）
- [x] **#1 (P0) 下單路徑加固** — commit 86acd3e：clientOrderId + 指數 backoff + 斷路器（僅開倉單成功重置，防 TP 交錯失效）+ 封鎖期不白撤 + `position_adjust_cooldown`（預設 5s）；35 新測試，全套 109 passed；dual-review 兩輪收斂 + verifier PASS（已 push）
- [x] **#2 (P0) 同步 ccxt 阻塞 event loop** — commits b197fd9..800fd98：所有 ccxt REST（下單/撤單/sync/啟動/keepalive/funding）卸載至單 worker `ThreadPoolExecutor`（`_rest` helper；不用 to_thread — 預設 pool 多 worker 會並發打非 thread-safe 的 ccxt Session）；停機檢查 + `shutdown(cancel_futures=True)`（含 init 失敗路徑）
- [x] **#3 (P0) 無鎖並發** — 同批 commits：`adjust_grid` per-symbol lock（skip-if-locked 不排隊）+ `sync_all` 防重入 + REST apply「fetch 鎖外、寫回鎖內無 await」原子區塊 + `_close_symbol_positions` 全程持鎖；鎖序單向 `_sync_lock → symbol lock`。17 新測試（含 monkey：50 並發風暴/全 REST 例外風暴/停機競態），全套 126 passed；SDD 逐 task review + final whole-branch review + dual-review R1 LGTM + verifier ACCEPT
- [~] **#4 (P0) 回測/實盤策略脫鉤**：程式碼完成 (Task 1-9+8b, 800fd98..7186203, 187 passed)，雙輪 review Ship as-is，已 push。**剩 Task 10 人工 24h replay zero-diff 上線驗收**。詳見上方 Current Task。
- [x] **#5 (P1) 回測成本模型** — 完成，10 commits a00d313..80a77bc，全套 **267 passed**，SDD 7 task + final whole-branch review(opus) + dual-review + verifier ACCEPT(6/6 實測) 全收斂 Ship as-is，已 push。純層 `backtest/costs.py`(apply_slippage 四方向不利偏移/funding_charge 帶號現金流) + `Config.slippage_bps`(0.0001 fraction, fidelity-first 預設開)/`funding_enabled`(預設開) + `DataLoader.load_funding`(真實 funding 歷史按需分頁下載快取 `data/funding/<symbol>.csv`) + backtester 主路徑接線(滑價只在 _open/_close、crossing 不動；settlement data-driven 掃真實時點、funding 走獨立 `funding_paid` 不進 trades 防污染 win_rate/PF/count) + FIDELITY_NOTES 9 項重寫(haircut 誠實命名/保守堆疊/mark=close 代理/快取非 range-aware 揭露/legacy 無成本)。**review 抓修 2 個真 bug**：partial-fetch 例外仍寫快取→永久毒化(task review)、抓取窗口本地時區 vs UTC 偏移 8h→尾端 settlement 系統性漏扣(final review I1，Taipei 下必中)。等價守門實測：零成本 bit-identical、funding 不動交易指標。spec `docs/superpowers/specs/2026-07-04-backtest-cost-model-design.md`、plan `docs/superpowers/plans/2026-07-04-backtest-cost-model.md`。Follow-up(非阻擋)見 `.superpowers/sdd/progress.md` #5 段(range-aware 快取/空 fetch 標記/ISO 讀取/optimizer perf)。
- [x] **#6 (P1) Bandit 狀態持久化** — 完成，11 commits 65c0c71..e6c9849，全套 **220 passed**，SDD 六 task + final whole-branch review(opus) + dual-review 全收斂 Ship as-is，已 push。純層 `grid_engine/bandit_persistence.py`(save 原子寫+fsync／load 永不 raise 冷啟動兜底) + `enhancements.py`(live class)/`indicators/bandit.py` 加 `arm_signature` + bot 接線(run 載入／每評估後 total_pulls 變才存／stop 收尾) + `grid_engine/config.py` 加 `bandit_state_path`/`bandit_state_max_age_sec`。**review 抓修 3 個 async-loop crash 洞**：pull_counts 整表取代→select_arm KeyError(final-review)、thompson 有限≤0→np.random.beta ValueError(dual R1 reproduced)、load_state 竄改例外穿透違反永不 raise(task4 review)。**重大：計畫全程誤指 `indicators/bandit.py`(舊 core)，live bot 用 `grid_engine/enhancements.py` 重複 class — 同 GlobalConfig 兩份陷阱，實作者抓到修正**。Follow-up(非阻擋)見 `.superpowers/sdd/progress.md` #6 段(save fsync 阻塞 event loop 宜 offload／load 尾端未 select_arm／context_rewards 未持久化／重複 class 收斂屬#8/#9)。原始問題(bot 從未呼叫 to_dict/load_state→重啟歸零)已解。spec `docs/superpowers/specs/2026-07-04-bandit-state-persistence-design.md`、plan `docs/superpowers/plans/2026-07-04-bandit-state-persistence.md`（6 tasks TDD）。設計：純層 `grid_engine/bandit_persistence.py`(save/load 原子寫+fsync) + bandit.py 加 `arm_signature` + bot 接線 3 處（run 載入/每 10 筆評估後條件存/stop 收尾）；`grid_engine/config.py` 加 `bandit_state_path`/`bandit_state_max_age_sec`。量化 review 折入：arm_signature 不簽 sizing（reward 已驗 scale-invariant，砍 reward_signature）、只復原學到統計不復原瞬時選擇、非有限值 sanitize、max_age 過期冷啟動、replay-invariant 守門。**注意 live config 是 `grid_engine/config.py` 非 `config/models.py`（舊 core）**
- [x] **#7 (P1) MaxGridBot god class 拆分** — 完成，8 commits cf3e10d..51def8c，全套 **268 passed**，SDD 7 task + final review + dual-review + verifier ACCEPT 全收斂 Ship as-is（未 push）。詳見上方 Current Task。
- [ ] **#8 (P2) 清理**：刪 grid_engine/backtest.py（無人引用）；頂層散落 test_web_system.py / test_symbol_conversion.py / web_test_report.json 收進 tests/ 或刪
- [ ] **#9 (長期) 淘汰舊系統**：web 依賴遷移後刪 core/ + exchanges/（~6000 行，需先確認 web 回測頁遷移方案）

## TODO — 部署（先前遺留）
- [ ] 建立 GCE VM (e2-small, Ubuntu 22.04, 固定外部 IP)
- [ ] 在 GCE 上執行 `scripts/gce-setup.sh` 部署
- [ ] 交易所 API 綁定 GCE VM 的固定 IP 白名單
- [x] 考慮 BNB 間距加大到 1%+ 或加下單 cooldown 以降低交易頻率 → 併入 #1 的 position_adjust_cooldown

## Recently Completed (2026-07-04b)
- [x] **#5 回測成本模型全流程完成並 push**：brainstorming（4 決策：真實 funding 歷史/固定 bps/按需快取/fidelity-first 預設開）→ 量化工程師視角 spec review（funding 不進 trades、data-driven settlement、分頁、haircut 誠實命名、保守堆疊揭露）→ writing-plans 7 tasks → SDD 執行（每 task fresh implementer + reviewer）→ final whole-branch review 抓修 I1 時區 bug → dual-review Ship as-is → verifier ACCEPT 6/6
- [x] push origin/main：e6c9849..80a77bc（13 commits = #5 code 10 + spec/plan docs 3）

## Recently Completed (2026-07-04)
- [x] #4 相容性 audit（scout）：`dead_mode_enabled`/`fallback_long/short` 全 repo 無非預設值，backtester 用 getattr 讀且 fallback 值 = core 常數 → 純層直接遷移 grid_engine 硬編 1.05/0.95，不加開關
- [x] #4 實作計畫（writing-plans）：`docs/superpowers/plans/2026-07-04-strategy-decoupling.md`，10 tasks 全 TDD（先 red 再 green）+ 每 task commit
- [x] Self-review 對 spec：覆蓋率、placeholder、type consistency 三項 pass

## Recently Completed (2026-06-10d)
- [x] 風控警報頻率可設定 `telegram_risk_alert_cooldown`（秒，預設 300）：from_dict 正規化（非法/非正值 fallback 300）、bot 冷卻改讀 config、選單「7 風控警報頻率」分鐘輸入（清除設定移至 8）
- [x] 補 roundtrip/monkey + bot 冷卻測試，74 passed；dual-review LGTM；commit 28ff2ef 已 push

## Recently Completed (2026-06-10c)
- [x] 風控警報獨立開關 `telegram_risk_alert_enabled`（預設開）：config 三處 + `_check_risk_and_notify` 入口 gate（關閉時不消耗冷卻計時，重開後立即可發）+ Telegram 選單新增「6 開關風控警報」（清除設定改為 7）
- [x] 補測試：config roundtrip/monkey/向後相容 + bot gating 三測，全套 66 passed
- [x] Dual-review 通過（codex LGTM）；commit cdafcbd，連同 9531e97 已 push

## Recently Completed (2026-06-10b)
- [x] Telegram 功能整合 as-grid-auto：通知總開關 `telegram_enabled`、每日摘要時間 `telegram_daily_pnl_hour` 可設定（Asia/Taipei 整點，預設 20:00，非法值 fallback 20）、啟動通知列交易對、每日摘要升級（權益/保證金使用率/未實現/累計已實現/逐幣 L/S+PnL）、選單對齊（狀態顯示+開關+時間設定）
- [x] Dual-review 完成：codex 抓到 hour 無驗證 + .DS_Store 入 diff，已修（`_parse_daily_pnl_hour` + monkey tests；`git rm --cached .DS_Store`）
- [x] 57 passed；commit 9531e97（含移除誤入版控的 .DS_Store；尚未 push）
- [x] 使用者已完成 Telegram token/chat_id 設定並測試成功（Chat ID 曾誤填 bot 自身 ID，已更正為使用者 ID）

## Recently Completed (2026-06-10)
- [x] 查明 Telegram 沒通知/沒日報的根因：`config/trading_config_max.json` 缺 `telegram_bot_token`/`telegram_chat_id`，notifier 靜默停用（非程式 bug；Docker 與 `as_terminal_max.py` 走同一條 MaxGridBot+config 路徑，本來就不依賴 Docker）
- [x] bot 啟動：Telegram 未設定 → log warning；已設定 → 發 `notify_start` 啟動通知（grid_engine/bot.py）
- [x] notifier 新增 `notify_start()` + 測試，43 passed
- [ ] 待使用者在主選單「連線設定 → Telegram 通知」填入 token/chat_id 並發測試訊息

## Recently Completed (2026-06-03b)
- [x] 修復權益/保證金仍失真（浮盈雙算）— ccxt 合約 `total`=marginBalance(已含浮盈)，舊碼 `wallet_balance=total` 後 `equity=wallet+upnl` 又加一次 → 94.49 顯示成 64。`_sync_account` 改從 `balance['info']['assets']` 取 `walletBalance`/`unrealizedProfit`/`availableBalance`/`initialMargin`，equity 自動正確（`grid_engine/bot.py:218`）
- [x] 補 regression + monkey：tests/test_account_update.py +8（重現截圖 94.49、不雙算、fallback、極端值），21 passed；全套 52 passed
- [x] 對照原版 `as_terminal_max.py`：掛單頻率(每 bookTicker tick 跑 adjust_grid + 成交後 stale order-count 致 <1s 洗單)與原版完全一致 → 使用者決定不動
- [x] 面板「保證金%」定義 = 倉位保證金/權益(使用率)，非幣安 2.09%(維持保證金/保證金餘額,爆倉指標) → 使用者選擇維持現狀

## Recently Completed (2026-06-03)
- [x] 修復面板餘額/保證金顯示失真 — WS `_handle_account_update` 誤把 `cw`(全倉錢包) 當可用餘額、且從不更新 margin_used；移除錯誤賦值，available/margin 改由 REST `_sync_account` 獨佔維護（`grid_engine/bot.py:696`）
- [x] `sync_interval` 30s → 10s（grid_engine/config.py + trading_config_max.json），補償 B 方案延遲
- [x] Dual-review 通過：codex 卡死 fallback general-purpose subagent，判定 clean fix（無 must-fix）
- [x] 確認 API key 未洩漏（trading_config_max.json 從未被 git 追蹤，.gitignore 已正確排除）
- [x] 補 monkey test：tests/test_account_update.py 13 測試（核心 regression: WS 不覆寫 REST 真值 + 極端輸入），全測試 34 passed
- [x] 提交 commit 72de3e1（bot.py + config.py + 新測試；尚未 push）

## Recently Completed (2026-06-02)
- [x] 風控警報加 5 分鐘冷卻（`RISK_ALERT_COOLDOWN=300`），避免高頻 ticker 轟炸 Telegram — commit b247c78, pushed
- [x] notifier 測試 21/21 通過

## Recently Completed (2026-04-14)
- [x] 重建 Docker image（--no-cache）
- [x] 本地 `docker compose run --rm as-grid` 驗證 TUI 互動正常
- [x] 診斷交易面板 TP/GS 顯示舊值問題（`sym_state.dynamic_take_profit` 無倉位時不刷新）
- [x] 修復 bot.py：ticker handler 中每次更新 `dynamic_take_profit/grid_spacing` 為 base 值
- [x] 確認 config 保存邏輯正確（回測優化後 0.50%/0.60% 已寫入 config 檔）
- [x] 分析交易 log：BTC Margin insufficient 431K 次、BNB 成交 775 次

### 先前已完成
- [x] 修正交易面板 Ctrl+C 會觸發 Docker 退出：暫存/恢復 SIGINT handler
- [x] 主選單重構：選項 7 改為「連線設定」子選單（交易所 API + Telegram 通知）
- [x] TelegramNotifier 模組 + 21 個測試全部通過
- [x] GlobalConfig 加 telegram 欄位（向後相容舊 config）
- [x] Bot 接入 notifier：崩潰/停止/每日摘要/風控警報
- [x] Dockerfile.terminal + docker-compose.terminal.yml + .dockerignore
- [x] GCE 一鍵部署腳本 scripts/gce-setup.sh
- [x] Monkey testing（極端輸入、並發、邊界值）
- [x] 修正 Docker 互動模式：`run --rm` 取代 `up`
- [x] 全部 push 到 github.com/RamonLiao/as-grid-dragon

## Blockers
無

## Notes

### #4 計畫關鍵設計決定（2026-07-04，spec「plan 階段定案」授權內）
- **多開 `snapshot.py`（共享，不純）**：spec 說 bot/backtester「各自逐字複刻 manager 呼叫序列」→ 改成共享單一 `build_snapshot()`。理由：兩邊各寫一份 = 把 #4 要解的發散重新引入。`decision.py` 維持純函數；snapshot.py 是唯一「不純但共享」邊界（呼叫 manager、`get_signals` 有 append 副作用）。
- **`EnhancementSnapshot` 欄位收斂**：spec 草稿把 leading_reason/atr_tp/atr_gs 分開讓 decide() 重跑分支；改成 snapshot 直接存「已解析的 dynamic_tp/gs」。理由：`get_dynamic_spacing`（含 ATR 60s 快取副作用）必須**條件呼叫**，只能在 snapshot 層做，decide() 重跑分支會破壞 manager 呼叫序列。Task 4 序列等價測試守住。
- **`ofi_history` 是唯寫遙測**（推測，Task 4 測試驗證）：`get_signals` 每 tick append ofi_history(deque maxlen 100)，spec 擔心呼叫次數變動漂移狀態；但追碼發現 ofi_history 只寫不讀（決策讀 current_ofi）。若序列等價測試確認只有它不同 → 呼叫次數變動安全。
- **實盤零改變手段**：Task 1 characterization 先鎖死現行 `_place_grid`/`_should_adjust_grid` 行為 → Task 5 把 `_place_grid` 改**薄封裝**走 `decide()`，同一 place_order 序列 → characterization 斷言不改而綠 = 等價證明。
- **Task 8 是最高風險**：backtester 從靜態階梯（錨在成交價）改追價（should_adjust + 錨在觸發價），回測數字會變——這是 intended（P0 動機本身），舊數字不作回歸基準。
- **決策日誌重放（Task 9）= 強驗收**：實盤每次 decide() 落地 inputs+decision JSON 一行，離線用同一 decide() 重放比對；上線 ≥24h 零 diff 為最終驗收（唯一能驗「快照捕捉完整性」的手段，函數級一致性測試是套套邏輯防不了兩邊吃同一殘缺快照）。

### 風控警報通知設計（2026-06-10c/d）
- 獨立開關 `telegram_risk_alert_enabled` + 頻率 `telegram_risk_alert_cooldown`（秒，預設 300，UI 以分鐘輸入）
- gate 放在 `_check_risk_and_notify` 入口、冷卻檢查之前 → 關閉期間不消耗冷卻計時，重開後若仍超標立即發
- 選單編號變動：6=開關風控警報、7=風控警報頻率、8=清除設定
- 改 config 後需重啟 bot 才生效

### 風控警報無節流 bug（已修，2026-06-02）
- **問題**: `_check_risk_and_notify` 掛在 `_handle_ticker`，ticker 是 ws 高頻推送；保證金超標時每個 tick 都 `create_task` 發 Telegram，`notify_risk_alert` 內無 throttle，會被洗版到 Telegram API 限流
- **修法**: 加模組常數 `RISK_ALERT_COOLDOWN=300`，bot 加 `self.last_risk_alert_time`，超標時先檢查冷卻，未過 300s 直接 return；回到閾值以下後再超標會立即重發
- **改動檔案**: `grid_engine/bot.py`（3 處）

### 交易面板 TP/GS 顯示 bug（已修）
- **問題**: `sym_state.dynamic_take_profit` 只在 `_place_grid` 裡更新，無倉位時不會刷新，導致面板顯示舊值
- **修法**: 在 `_handle_ticker` 的 Bandit 之後、掛單分支之前，用 base 值更新 `dynamic_take_profit/grid_spacing`
- **改動檔案**: `grid_engine/bot.py`

### BTC 保證金佔比問題
- 64 USDC 帳戶跑 BTC 合約，一筆就佔滿保證金
- BTC 已停用，目前只跑 BNB，保證金佔比回到 19.3%

### 開關單頻繁分析
- BNB 0.50%/0.60% 間距對日內波動來說仍然很窄，交易頻率高是正常行為
- 要顯著降頻需加大到 1%+ 或加 cooldown 機制
- BTC 下單失敗 43 萬次（Margin insufficient），無 backoff 機制，浪費 API quota

### Docker TUI 互動方式（重要！）
- **啟動**: `docker compose -f docker-compose.terminal.yml run --rm as-grid`
- **斷開**: `Ctrl+P, Ctrl+Q`（container 繼續跑）
- **接回**: `docker attach as-grid`
- **停止**: `docker compose -f docker-compose.terminal.yml down`
