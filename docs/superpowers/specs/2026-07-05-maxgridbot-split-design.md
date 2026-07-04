# #7 MaxGridBot God Class 組件化拆分 — 設計文件

日期：2026-07-05
狀態：已確認（brainstorming 定案 + 量化工程師視角 spec review 修訂：C1 兩階段初始化、C2 _stop_event、I1-I5、M1-M3 全數折入）
前置：#1 下單加固、#2 REST 卸載、#3 鎖化、#4 決策純層均已落地（HEAD=80a77bc）

## 目標與非目標

**目標**：把 `grid_engine/bot.py` 的 MaxGridBot（1092 行、38 方法）拆成組件化結構——每個組件持有自己的狀態、依賴顯式注入、bot 退化為「組合根 + 生命週期 + 網格主流程 + WS handlers」（~450 行）。**行為零改變**。

**非目標**：
- 不切 `GlobalState`（仍是單一共享物件注入各組件；state 所有權收斂屬未來工作，見 #8/#9 後續）。
- 不抽網格決策鏈（`adjust_grid → _grid_step → decide() → _execute_side_decision`）——它是各組件匯流點，留在 bot。
- 不動 `decision.py`/`snapshot.py` 純層、不動 enhancement managers、不改任何交易邏輯。
- 不做 async 化 ccxt、不換 aiohttp。

## 拆分強度決策（使用者定案）

- **組件化拆分**：抽出真實類別、狀態各歸其主、bot 組合根。非純機械檔案切分（不解決所有權）、非深度重構含 GlobalState 切分（實盤風險過高）。
- **核心網格鏈留在 bot**：抽成 GridOrchestrator 只會製造需要注入全部其他組件的迷你 god class，且 characterization 測試改動面最大。

## 架構

```
grid_engine/
├── bot.py            MaxGridBot = 組合根 + run/stop/init + 網格主流程 + WS handlers（~450 行）
├── rest_gateway.py   RestGateway：單 worker ThreadPoolExecutor + call() + shutdown   [新]
├── order_executor.py OrderExecutor：下單/撤單/斷路器/backoff/平倉                    [新]
├── sync_service.py   SyncService：sync_all + 4 個 _sync_* + _sync_lock              [新]
├── ws_client.py      WsClient：WS 連線/重連/listenKey/keepalive（純傳輸層）          [新]
├── risk_monitor.py   RiskMonitor：追蹤止盈/減倉/保證金警報                          [新]
├── reporting.py      DailyReporter：每日損益摘要排程                                [新]
├── locks.py          SymbolLocks：per-symbol asyncio.Lock 註冊表                    [新]
└── context.py        ExchangeContext：exchange/precisions/funding_manager 共享容器  [新]
```

### 組件職責、持有狀態、依賴

| 組件 | 搬入方法（bot.py 現行行號） | 持有的狀態（從 bot self.* 遷入） | 注入依賴 |
|---|---|---|---|
| `ExchangeContext` | —（共享可變容器，無方法） | `exchange`、`precisions`、`funding_manager`（`_init_exchange` 於 run() 才寫入的兩階段真值） | — |
| `RestGateway` | `_rest`(163)、executor 建立(96)、`shutdown(cancel_futures=True)`(1153) | `_rest_executor`（max_workers=1） | — |
| `OrderExecutor` | `place_order`(370)、`_register_order_failure`(409)、`cancel_orders_for_side`(432)、`_close_symbol_positions`(342)、`is_blocked(symbol)` 查詢介面 | `_order_fail_counts`、`_order_block_until`、`_order_seq` | gateway、ctx（讀 exchange/precisions）、state、notifier、config、SymbolLocks、`_stop_event`、task registry（bot.tasks，斷路通知 task 防 GC + stop 可 cancel，見 bot.py:423） |
| `SyncService` | `sync_all`(175)、`_sync_positions`(196)、`_sync_orders`(224)、`_sync_account`(256)、`_sync_funding_rates`(184)、`maybe_sync()`（收編 _handle_ticker:806-808 的 interval gating） | `_sync_lock`、`last_sync_time` | gateway、ctx（讀 exchange/funding_manager）、state、SymbolLocks、notifier、RiskMonitor（_sync_account:289-292 反向呼叫 risk/trailing） |
| `WsClient` | `_websocket_loop`(938)、`_get_listen_key`(171)、`_keep_alive_loop`(980) | `listen_key` | gateway、ctx（讀 exchange）、config、`_stop_event`、handler callbacks（bot 註冊） |
| `RiskMonitor` | `_check_trailing_stop`(296)、`_check_and_reduce_positions`(535)、`_check_risk_and_notify`(1037) | `last_risk_alert_time` | state、OrderExecutor（平倉/減倉）、notifier、config |
| `DailyReporter` | `_daily_pnl_loop`(992) | 排程時間狀態 | state、notifier、config、`_stop_event` |
| `SymbolLocks` | `_symbol_lock`(168) 懶初始化 | `_symbol_locks` dict | — |

**狀態所有權修正（spec review 折入）**：
- `last_order_times` **留在 bot**，不遷 OrderExecutor——grep 證實它只被網格鏈讀寫（`_grid_cooldown_passed`:585、`_grid_step`:666-688），OrderExecutor 完全不碰；遷走只會製造 bot 每 tick 反向寫組件的隱藏耦合。
- `_order_block_until` 歸 OrderExecutor，但網格鏈在 `_grid_step`:640/665/679 直接讀它決定 `order_blocked` —— OrderExecutor 必須提供 `is_blocked(symbol)`（回傳封鎖中與否）供 bot 讀，這三個讀取點是明確的遷移清單。
- `_stop_event` 由 bot 持有、**共享注入** OrderExecutor（place_order:374 停機閘）/WsClient（941/958/976/981/984 loop 退出）/DailyReporter（994/1006）——漏注入會導致停機 hang 或停機期間仍送單。

### 兩階段初始化協定（Critical，必守）

`exchange`(126)、`precisions`、`funding_manager`(134) 在 `__init__` 時是 None/空，真值在 `run() → _init_exchange` 才產生。組件若在建構時快照這些值，會捕獲 None → 下單全炸、`_sync_funding_rates` 的 `if not funding_manager: return`(186) 永遠成立 → **funding 同步靜默失效**（無報錯）。

解法：`ExchangeContext` 共享可變容器。bot `__init__` 建立空 ctx 注入各組件；`_init_exchange` 寫入 `ctx.exchange/ctx.precisions/ctx.funding_manager`；組件一律**呼叫當下**讀 `self.ctx.exchange`，絕不在自己 `__init__` 存成員快照。測試現行的 `bot.exchange = MagicMock()`（test_order_guard.py:33 等）機械遷移為 `bot.ctx.exchange = MagicMock()`；bot 上可留 `exchange` property 轉發 ctx 以減少 bot 內部改動面。

### 留在 bot 的部分

- 生命週期：`__init__`（改為組件組裝）、`_init_exchange`、`_check_hedge_mode`、`run`、`stop`。
- WS handlers：`_handle_ticker`、`_handle_account_update`、`_handle_order_update` —— 事件到領域的黏合層（寫 state、觸發 adjust_grid、餵 bandit、呼叫 RiskMonitor），與網格鏈同層。WsClient 只做傳輸，收到訊息後分派給 bot 註冊的 callbacks。
- 網格鏈：`adjust_grid`、`_should_adjust_grid`、`_grid_cooldown_passed`、`_grid_step`、`_place_grid`、`_execute_side_decision`、`_build_bundle`、`_build_inputs`、`_log_decision`、`_get_adjusted_quantity`，以及網格鏈專屬狀態 `last_order_times`（見所有權修正）。
- Bandit 持久化 hooks：`_persist_bandit_state`、`_maybe_persist_bandit_state`（run 載入 / 評估後條件存 / stop 收尾）。

### 資料流（拆分後）

```
WsClient(傳輸) ──callback──▶ bot._handle_*(領域黏合)
                                │ 寫 state / 餵 bandit
                                ▼
                     bot.adjust_grid → _grid_step
                                │ build_snapshot()+decide()  [純層，不動]
                                ▼
                     bot._execute_side_decision
                                │
                                ▼
                     OrderExecutor.place_order / cancel ──▶ RestGateway ──▶ ccxt
SyncService（週期）────────────────────────────────────────▶ RestGateway ──▶ ccxt
RiskMonitor（ticker 觸發）──▶ OrderExecutor（平倉/減倉）
DailyReporter（定時）──▶ notifier
```

## 不變式（拆分絕不可破壞）

1. **單 worker executor 語意**：全部 ccxt REST 走同一個 `RestGateway` 實例序列化（ccxt Session 非 thread-safe，#2 定案）。組件各自注入的是**同一個** gateway。
2. **鎖序單向 `_sync_lock → symbol lock`**：SymbolLocks 是共享註冊表——SyncService、OrderExecutor（`_close_symbol_positions`）、bot（`adjust_grid` skip-if-locked）拿到的是**同一把鎖物件**。拆分後需有測試斷言鎖同一性。
3. **REST apply 原子區**：`_sync_positions`/`_sync_orders` 的「fetch 鎖外、寫回鎖內無 await」模式原樣搬移（#3 定案）。
4. **斷路器語意**：clientOrderId + 指數 backoff + 僅開倉單成功重置 + 封鎖期不白撤 + `position_adjust_cooldown`（#1 定案）——整組搬進 OrderExecutor，邏輯零改動。
5. **decide() 純層與決策日誌契約**：`decision.py`/`snapshot.py`/`replay.py` 零接觸；`logs/decisions.jsonl` 格式不變（#4 replay 驗收仍有效）。
6. **停機路徑**：`stop()` 順序（通知 → 存 bandit → 取消 tasks → gateway.shutdown(cancel_futures=True)）不變，含 init 失敗路徑的 shutdown。

## 錯誤處理

- 各組件錯誤處理**原樣搬移**，不新增、不收斂。搬移期間發現的既有問題記入 follow-up，不順手修（no overengineering）。
- **WsClient callback 例外傳播語意（等價陷阱，明文鎖定）**：現況三個 handler 行為不對稱——`_handle_ticker`(784) 無內部 try，例外冒泡到 `_websocket_loop` 的 outer except(974) → `connected=False` + sleep 5s + **整條 WS 重連**；`_handle_account_update`/`_handle_order_update` 自帶 try 吞例外、不觸發重連。拆分後 WsClient **不得**用自己的 try 包 callback（那會讓 ticker handler 例外不再觸發重連 = 行為改變）；callback 例外必須照現狀冒泡進重連迴圈的 try。補一條 characterization：ticker handler 拋例外 → 觸發重連。
- `_sync_account`(289-292) 對 risk/trailing 的反向呼叫（`create_task(_check_risk_and_notify)` fire-and-forget + `await _check_trailing_stop`）語意原樣保留，走注入的 RiskMonitor。

## 測試策略

- **等價守門 = 全套 267 tests 綠**：現有測試（含 #4 characterization：`_place_grid`/`_should_adjust_grid` 行為鎖定、#1 斷路器 35 測、#3 並發 17 測）斷言不改，只做機械性遷移——patch 目標與屬性路徑更新（如 `bot._rest` → `bot.gateway.call`、`bot._order_fail_counts` → `bot.order_executor._fail_counts`）。
- **測試不留 shim**：直接改測試戳新組件路徑；bot 上不保留 `_place_order` 之類的轉發別名（`_place_grid` 等留在 bot 的方法除外，它們本來就沒搬）。
- **新增組件級測試（少量）**：
  - SymbolLocks：同 symbol 回傳同一把鎖（鎖同一性，守不變式 2）。
  - RestGateway：停機後拒絕新呼叫；單 worker 序列化行為。
  - OrderExecutor：斷路器狀態機在新類上的 smoke（既有 35 測遷移後已覆蓋主體）；`is_blocked()` 與 `_grid_step` 讀取點等價。
  - ExchangeContext 兩階段：組件在 `_init_exchange` **之後**讀到真 exchange/funding_manager（防 None 快照 → funding 同步靜默失效）。
  - WS 例外語意 characterization：ticker handler 拋例外 → 觸發重連；account/order handler 拋例外 → 不觸發重連。
  - 組件間整合：`_sync_account` 觸發 `_check_risk_and_notify`（create_task）與 `_check_trailing_stop`（await）——這條跨組件序列現有測試幾乎沒蓋，patch 路徑遷移守不住它。
- **Monkey testing**（專案規則）：既有並發風暴測試（50 並發/REST 例外風暴/停機競態）遷移後重跑即為 monkey 覆蓋；再補「組件間共享鎖競態」一項。
- **最終驗收**：部署後 #4 Task 10 的 24h replay zero-diff 同時驗證本次拆分（同一份 decisions.jsonl 契約）。

## 實作切法（每組件一 task 一 commit，依依賴序）

1. `locks.py` SymbolLocks + `rest_gateway.py` RestGateway + `context.py` ExchangeContext（無依賴的葉子先行）
2. `order_executor.py` OrderExecutor（含 `is_blocked()`、task registry 注入、`_stop_event` 停機閘）
3. `risk_monitor.py` RiskMonitor + `reporting.py` DailyReporter（RiskMonitor 依賴 OrderExecutor）
4. `sync_service.py` SyncService（依賴 RiskMonitor——排在其後，避免 `_sync_account` 接線改兩次）
5. `ws_client.py` WsClient（handlers 留 bot，callback 註冊，例外語意 characterization 先行）
6. bot.py 收尾：`__init__` 組裝（含建構順序：ctx/locks/gateway → executor → risk/reporter → sync → ws）、殘留引用清理、行數確認
7. 全套回歸 + monkey + 補組件級/整合測試

組裝建構順序是硬約束：SyncService 建構需要 RiskMonitor 實例，RiskMonitor 需要 OrderExecutor 實例。每步結束全套測試必須綠才 commit（紅的中間態不落 commit）。

## 風險與緩解

| 風險 | 緩解 |
|---|---|
| 鎖物件在搬移中被複製成兩把（sync 與 order 各自建鎖）→ 原子區失效 | 鎖同一性測試 + 注入同一 SymbolLocks 實例的組裝斷言 |
| executor 被建成多個 → ccxt 並發打非 thread-safe Session | RestGateway 單例注入 + 組裝斷言 |
| 測試 patch 目標漏改 → 假綠（patch 到已不存在的路徑，mock 永不觸發） | 遷移時逐檔確認 patch 路徑實際存在（`create=False` 預設會 AttributeError 兜底）；verifier 實跑 |
| 搬移時手滑改到行為（例如 cooldown 檢查順序） | 逐方法 diff review；characterization 測試斷言不改 |
| stop() 順序被組件化打亂 → 停機丟單/bandit 沒存 | stop 路徑既有測試 + 停機競態 monkey 測試遷移後重跑 |
| 組件建構時快照 None exchange/funding_manager → 下單炸或 funding 同步靜默失效 | ExchangeContext 呼叫當下讀 + 兩階段測試（見測試策略） |
| `_stop_event` 漏注入 → 停機 hang（loop 不退）或停機期間仍送單 | 依賴表明列 + stop 路徑測試涵蓋三個 loop 退出 |
| WsClient 包 callback try → ticker 例外不再觸發重連（行為改變） | 例外語意明文鎖定 + characterization 測試 |
