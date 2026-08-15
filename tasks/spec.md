# 當前任務 Spec：userData 靜默失效偵測與復原（watchdog）

完整設計：`docs/superpowers/specs/2026-08-15-userdata-watchdog-design.md`（權威出處）

**前提**：userData stream 自 2026-07-12 靜默死亡至今。2026-08-14/15 的實驗已否決
訂閱方式、stream name 被丟棄、listenKey 過期、listenKey 卡壞狀態、socket 健康度、
Portfolio Margin、multi-assets、API 權限；IP 白名單大幅弱化。**根因仍未確定，
且可能在 Binance 端** ⇒ 本任務不假設根因可修。

## Goals
1. **偵測**：靜默失效在 10 分鐘量級內判定並告警（log + Telegram）。
2. **復原**：有限次數（3 次，退避 5/15/45 分鐘）強制重連自救，失敗後進 `given_up` 終態。
3. **補數字**：面板與 Telegram 的成交次數／累計已實現改由 REST 取得，不再依賴 userData。

## Non-goals
不修根因；不改交易決策/下單/風控邏輯；不新增自動重啟行程；不改 `rest_gateway`；
不做全期累計統計（口徑維持「本次引擎啟動以來」）。

## Security / Safety constraints
- watchdog 不得具備下單/撤單/改倉能力；唯一副作用是「請求 WS 重連」與「發通知」。
- 重連次數硬上限 3，達上限進終態——強制重連會連帶中斷 `bookTicker`（`decide()` 的觸發來源）。
- 重連採「設旗標 + 內層迴圈自行 break」，**不得**對 `run()` 拋例外借用既有的
  「例外冒泡 = 重連」不變式（`ws_client.py` 開頭 characterization 註解鎖定）。
- `total_trades` / `total_profit` 維持**單一 writer**（REST），userData handler 停寫該兩欄。

## 可判定驗收準則
1. 六條指定 mutation 各自先紅一次（判準 `and`→`or`、`K`→0、退避改固定、`given_up` 後仍重連、
   `record_event` 不重置、增量拉取不去重）。
2. 全套測試全綠，基線 590 passed / 1 skipped，新增數量明列。
3. 活體驗收：重啟後 10 分鐘 + 4 張單內出現判死 log 與 Telegram 告警；面板成交次數 60 秒內
   從 0 變成 REST 實測值；三次重連無效後進 `given_up` 且不再重連。
4. 回歸：`decide()` 觸發頻率（`[MAX]` log 間隔）不因強制重連顯著劣化。
