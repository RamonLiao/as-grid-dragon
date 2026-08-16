# userData 靜默失效偵測與復原（watchdog）— 設計

- 日期：2026-08-15
- 狀態：待使用者複審
- Track：Plan（新增跨組件機制、會主動觸發生產引擎斷線重連）

## 1. 背景與問題

`bot.py` 的 userData handler（`_handle_account_update` / `_handle_order_update`）是
`SymbolState.total_trades` / `total_profit` 與 `GlobalState.total_trades` / `total_profit`
的**唯一**寫入點。這條路徑自 2026-07-12 起靜默死亡至今（2026-08-15），造成：

- 終端面板的「成交次數」與 Telegram 每日摘要的「累計已實現」**恆為 0**，而同期實際
  15 天 `REALIZED_PNL = −24.01` / 36 筆。使用者看到的是假數字。
- `bandit` / `leading_indicator` / `dgt` 的 `record_trade` 從未被呼叫（三者現皆關閉，本次無實害）。

### 1.1 已排除的根因（實測，2026-08-14 / 08-15）

| 假設 | 結論 | 證據 |
|---|---|---|
| 訂閱方式（`SUBSCRIBE` vs path URL） | 否決 | A/B 雙路皆零事件 |
| stream name 被伺服器靜默丟棄 | 否決 | `LIST_SUBSCRIPTIONS` 回傳中確實含該 listenKey |
| listenKey 過期／keepalive 沒跑 | 否決 | 新取得 <1 分鐘即測 |
| socket 不健康 | 否決 | 同一條連線 bookTicker 2,853 筆／10 分鐘 |
| listenKey 卡在伺服器端壞狀態 | 否決 | `DELETE`+`POST` 換出全新 key 三次，仍零推送 |
| Portfolio Margin / multi-assets / API 權限 | 否決 | `enablePortfolioMarginTrading=false`、`multiAssetsMargin=false`、`enableReading`/`enableFutures=true` |
| IP 白名單 | 大幅弱化 | REST 自同一 IP 全通，listenKey 亦由同一 IP POST |

決定性窗口（2026-08-15 17:27–17:37）：全新 listenKey `bcl1Txm…`、`LIST_SUBSCRIPTIONS` 確認登記、
窗口內交易所端 4 筆真實訂單事件（`allOrders` 交叉驗證，17:35:31–32）、
同連線 bookTicker 2,853 筆 ⇒ **userData 0 筆**。

**根因仍未確定，且可能在 Binance 端。** 本設計因此不假設根因可修。

### 1.2 這次事故真正的工程缺陷

比「userData 為什麼不推」更根本的是：**這條路徑死了一個月，系統沒有任何偵測**。
唯一的線索是 keepalive 每 30 分鐘的 `-1125`，而那條在 `6a264d6` 修掉之後就消失了——
症狀被修掉，故障還在。本設計處理的是這個缺陷。

## 2. Goals

1. **偵測**：userData 靜默失效時，在 10 分鐘量級內判定並告警（log + Telegram）。
2. **復原**：有限次數地嘗試強制重連自救，失敗後乾淨放棄，不損害交易主路徑。
3. **補數字**：成交次數／累計已實現改由 REST 取得，不再依賴 userData 是否活著。

   ⚠️ **2026-08-15 whole-branch review 更正**：本節原本寫「面板與 Telegram」，
   前提有一半是錯的。實查 `grid_engine/ui.py` **對 `trades` 零引用——終端面板沒有
   成交次數欄位**，`state.total_trades` 在整個生產路徑沒有任何讀者。真正受影響的
   只有 Telegram 每日摘要的「累計已實現」（`reporting.py:49` → `notifier.py:124`
   讀 `state.total_profit`）。
   `total_trades` 仍然改由 REST 維護（成本幾乎為零、且是 watchdog 之外的第二個
   對帳來源），但**它目前沒有出口**。要不要在面板補一個成交次數/已實現欄位，
   是獨立的 UI 決策，不在本 spec 範圍。

## 3. Non-Goals

- **不修 userData 不推送的根因**（未知且可能在交易所端）。根因調查另案進行。
- 不改變交易決策邏輯、下單邏輯、風控邏輯。
- 不新增自動重啟行程、不改 `rest_gateway` 的重試/斷路設計。
- 不做全期累計統計（口徑維持「本次引擎啟動以來」，見 §5.3）。

## 4. Security / Safety Constraints

- watchdog **不得**具備下單、撤單、改倉能力；它唯一的副作用是「請求 WS 重連」與「發通知」。
- 強制重連會連帶中斷 `bookTicker`（`decide()` 的觸發來源）。因此重連次數**必須**有硬上限，
  且達上限後進入終態不再重連。

  ⚠️ **2026-08-15 verifier 更正——這條保證比字面弱**：3 次上限是 **per-episode** 的。
  任何一筆 `ORDER_TRADE_UPDATE` 都會經 `record_event()` 把 `attempts` 歸零，
  因此一條**斷續**推送的 stream（時好時壞）可以無限次重複「3 次強制重連」的週期。
  這與 §5.2 的狀態機設計一致（`record_event()` 是唯一復原入口），但「硬上限」這個詞
  容易被讀成「這個行程最多重連 3 次」，實際是「每一次失效事件最多重連 3 次」。
  完全死掉的 stream（現行生產狀態）確實只會重連 3 次就永久停止。
- 重連採「設旗標 + 內層迴圈自行 break」，**不得**由其他 task 直接關閉 socket 或
  對 `run()` 拋例外——`ws_client.py` 開頭的 characterization 註解鎖定了
  「例外冒泡 = 重連」這條不變式，借用它會讓「handler 出錯」與「watchdog 故意觸發」
  無法區分。
- `total_trades` / `total_profit` 必須維持**單一 writer**。雙寫在 userData 復活時會造成計數翻倍。

## 5. 設計

### 5.1 元件：`grid_engine/userdata_watchdog.py`

```
class UserDataWatchdog:
    def __init__(self, config, notifier, ws_client, tasks, stop_event, clock=time.time)

    # 輸入
    record_order_action()   # order_executor 每次成功下/撤單
    record_event()          # bot.py 兩個 userData handler

    # 迴圈
    async def run()         # 每 60s 呼叫 check()，納入 bot.tasks
    def check()             # 純函式式判定 + 動作決策（可單獨測試）
```

狀態：

| 欄位 | 語意 |
|---|---|
| `orders_since_event` | 自最後一筆 userData 事件以來，成功下/撤單的張數 |
| `last_event_at` | 最後一筆 userData 事件時間；啟動時 = 引擎啟動時間 |
| `state` | `healthy` / `degraded` / `given_up` |
| `attempts` | 已用掉的重連次數 |
| `next_attempt_at` | 下次可重連的時間 |

### 5.2 判準與狀態轉移

判死條件（**兩者同時**成立）：

```
orders_since_event >= K        且        now - last_event_at >= N
```

預設 `K = 4`（引擎 requote 一次即 4 張，實測 17:35:31–32 那批）、`N = 600s`。

同時成立才判死的理由：只看時間會在真正安靜的時段誤報（引擎裝死、價格不動不 requote，
歷史上實盤成交率曾低到 ~1 筆/天）；只看張數則沒有給推送延遲留餘裕。

轉移：

```
healthy --判死--> degraded
    attempt 1：立刻 request_reconnect() + log + Telegram 告警（本次事故只發這一封）
    退避 300s  → 仍判死 → attempt 2：request_reconnect()
    退避 900s  → 仍判死 → attempt 3：request_reconnect()
    退避 2700s → 仍判死 → given_up：Telegram 第二封「放棄自動復原」，之後只 log 不動作

任何 record_event() → 無論當前狀態，全部重置回 healthy；
                      若先前曾告警，補發一封恢復通知
```

`given_up` 是終態，只有 `record_event()` 能離開。

### 5.3 REST 補數字：`sync_service._sync_trade_stats()`

- 節流 60 秒一次（與 `sync_interval` 的 10 秒解耦，避免無謂的 API 權重）。
- `fetch_my_trades(symbol, since=引擎啟動時間)`；以 `_last_trade_id`（per symbol，
  嚴格遞增）做增量拉取與去重。
- 寫入 `sym_state.total_trades`（筆數）、`sym_state.total_profit`（`realizedPnl` 加總），
  再彙總到 `state.total_trades` / `state.total_profit`。
- **userData handler 停止寫這兩個欄位**，保留 `recent_trades` deque、log 與通知。

口徑：**本次引擎啟動以來**，與現行語意一致，面板與 Telegram 文案不需改動。
代價：計數更新由「即時」變為「最慢 60 秒」。

### 5.4 `ws_client` 的改動

新增 `request_reconnect()`：設 `self._reconnect_requested = True`。
`run()` 內層迴圈在每次 `recv()` 成功或 `TimeoutError` 之後檢查該旗標，
為真則清旗標並 `break` 出內層迴圈 → 離開 `async with` → 外層 `while` 重連。

最壞延遲 = 現行 `recv` timeout（30 秒）。

## 6. 錯誤處理

- `_sync_trade_stats` 失敗：`logger.error` 後 return，維持既有數值（與其他 `_sync_*` 同構）。
  **不得**把失敗當成 0 筆寫回去。
- Telegram 發送失敗：只 log，不影響 watchdog 狀態機。
- `request_reconnect()` 在 WS 尚未連上時呼叫：旗標保留，下次連上後的第一次檢查即生效。

## 7. 可判定驗收準則

1. **單元測試（注入 clock）**，且下列 mutation 每條必須先紅一次：
   - 判準的 `and` 改成 `or` → 安靜時段誤報測試轉紅
   - `K` 改成 `0` → 誤報測試轉紅
   - 退避序列改成固定值 → 退避測試轉紅
   - `given_up` 後仍呼叫 `request_reconnect()` → 終態測試轉紅
   - `record_event()` 不重置 `attempts` → 復原測試轉紅
   - 增量拉取不去重（`_last_trade_id` 不更新）→ 計數翻倍測試轉紅
2. **全套測試**：基線 **589 passed / 2 skipped**，新增測試後全綠，數量須明列。

   ⚠️ **2026-08-15 verifier 更正**：原寫「590 passed / 1 skipped」。verifier 把分支起點
   `caec67e` clone 到隔離目錄實跑，實測是 589/2。多出來的那條 skip 是
   `tests/web/test_config_store.py:117`（需要真實 `config/`，隔離環境沒有）。
3. **活體驗收**（重啟後）：
   - 生產當前就處於失效態 ⇒ 啟動後 **10 分鐘 + 4 張單**內必須出現判死 log 與 Telegram 告警。
   - **引擎啟動後 75 分鐘**（= attempt 1 於 t0+600，再經 300/900/2700 退避）進入 `given_up`，
     發出第二封 Telegram，且**不再**出現重連 log。整個過程 Telegram 恰好 **2 封**、
     強制重連恰好 **3 次**。
   - `WebSocket 錯誤` 的頻率不得因本改動而上升——watchdog 觸發的重連走 `logger.warning`
     的「收到重連請求」路徑，**不**經過外層 `except`，兩者在 log 上必須可區分。
   - 成交統計：**重啟後產生第一筆真實成交，60 秒內 `state.total_profit` 出現非 0**。

   ⚠️ **2026-08-15 whole-branch review 更正**：本節原本寫「面板『成交次數』在 60 秒內
   從 0 變成 15 天 36 筆 / −24.01」，有兩個錯——(a) 面板沒有成交次數欄位（見 §2 的更正）；
   (b) 口徑是「本次啟動以來」，重啟後**必定**從 0 開始累積，不可能跳到 15 天的數字。
   照原判準驗收會得到「沒生效」的錯誤結論。
   原本的「65 分鐘」也更正為 **75 分鐘**：65 分鐘是從 attempt 1 起算的 3900s，
   從引擎啟動起算還要加上第一次判死前的 600s。
4. **回歸**：`decide()` 的觸發頻率（`[MAX]` log 間隔）不得因強制重連而顯著劣化。

## 8. 已知限制

- 若根因在 Binance 端，本設計不會讓 userData 復活；它保證的是
  「不會再有人不知道它死了」與「數字是真的」。
- `given_up` 之後需要人工介入（換 API key、開客服單、或修好根因後重啟）。
- REST 補數字的口徑是「本次啟動以來」，重啟仍歸零——與現行行為一致，非本次改動引入。

### 8.2 review 過程中發現、本次認列不修的三項

1. **裝死模式下狀態機會永遠停在 `degraded`，走不到 `given_up`。**
   §5.2 的「3 次後放棄」直覺上讀起來像「75 分鐘後一定會進終態」，實際不是。
   2026-08-15 的修正讓每次重連嘗試都要**重新取證**（否則三次嘗試等於同一批陳舊證據
   重播三遍，stream 修好但市場安靜時會誤判到終態並發「需人工介入」）。副作用是：
   引擎若進入裝死模式而零新單，第 2 次重連的證據永遠湊不滿 ⇒ 停在 `degraded`，
   ⛔ 那封 Telegram 不會發。
   **可見性由每日摘要接住**（天天顯示「⚠️ userData 監控：重連中」）。
   這是「沒有新證據就沒有判死依據」的設計必然，判定為正確取捨。

2. **`bandit` / `dgt` 的學習訊號仍只由死掉的 userData 路徑餵。**
   `bot.py` 的 `bandit_optimizer.record_trade()` 與 `dgt_manager.accumulated_profits`
   仍在 `_handle_order_update` 內，而這兩者會回頭影響 `adjust_grid()` 的決策。
   本 spec 只修了**顯示端**（`total_trades` / `total_profit`），沒修**進 `decide()` 的訊號**。
   目前無實害——三個增強模組在生產 config 皆 `enabled: false`（2026-07-12 C 路線裁決）。
   ⚠️ **若日後要開回任何一個，它們會拿到全零的歷史且不會有任何警告。**

3. **五條存活的 mutation（全部落在防呆第二層、生產路徑不可達）**，詳見
   `.superpowers/sdd/2026-08-15-userdata-watchdog/progress.md` 的最終裁決段。
   其中 **M1 列為下次開工第一項**：`_format_watchdog_line` 對「key 存在但值型別錯」無防禦，
   是「每日摘要不得發不出去」這條硬性要求的唯一剩餘缺口（修法一行）。

### 8.1 security review 認列但本次不修的兩項（2026-08-15，Low）

兩項都需要獨立的重構，範圍超出本 spec，**明確留給後續**：

1. **watchdog 用牆鐘（`clock.now()` → `time.time()`）量測靜默時長與退避。**
   系統時鐘向前跳 ≥600s（NTP step、VM suspend/resume、手動改時間）且已累積 ≥4 張單時，
   下一次 check 會立刻誤判死 → 非必要的強制重連 + 誤發 Telegram（有 3 次上限兜住，不是 DoS）。
   向後跳則 `next_attempt_at` 被推到遠未來 ⇒ **watchdog 靜默凍結**，真的失效時告警延遲數小時
   ——正是本 spec 要根除的「沒有儀器」重演。
   正解是間隔量測改 `time.monotonic()`，但回測需要可注入時鐘，必須另開一個 monotonic 注入點，
   不能與現有的牆鐘 `clock` 共用。

2. **`start_time_ms` 用本機時鐘當交易所端的 `since` 游標。**
   本機時鐘落後交易所 N 分鐘 ⇒ 第一輪 `fetch_my_trades` 會把**引擎啟動前 N 分鐘**的成交
   算進「本次啟動以來」的 `total_profit`，而這個數字直接印在 Telegram 日報上；
   領先則靜默漏掉啟動後最初 N 分鐘的成交。目前無校正、無界限檢查、無 log。
   正解是改用交易所時間（`exchange.fetch_time()` 或量測 offset），至少在啟動時 log 一次時間差。

另有一個**既有**缺口被本次改動放大、但屬獨立議題：`_handle_ticker` 完全不看 bookTicker 的
`E`/`T` 時戳，也沒有任何價格時效守衛 ⇒ 任何原因造成的 recv 迴圈停滯，都會讓積壓的過期價
直接進 `adjust_grid` 下單。本次已用頁數上限縮小觸發面，但守衛本身該獨立處理。
