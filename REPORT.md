# fix/tui-detect-dead-bot — 根因修復報告

commit `cacf957`（base `0145129`）

## 根因

`as_terminal_max.py` 的 `start_trading` 有兩段各 `for _ in range(100): time.sleep(0.1)`
的輪詢（共 20s），終止條件只有 `self.bot.state.running`。

`grid_engine/bot.py` 的 `run()` 初始化段是**硬失敗**設計：

```
run()
 ├─ _init_exchange
 ├─ _check_hedge_mode      ← 確立不了雙向持倉模式就 raise
 ├─ acquire_listen_key
 ├─ state.running = True   ← 只有走到這裡 running 才是 True
 └─ sync_once
      except Exception ─→ logger.error + notify_crash("初始化失敗: …")
                          + state.running = False + gateway.shutdown() + return
                          ⇒ thread 乾淨結束
```

⇒ 最常見的硬失敗（bot 幾秒內 raise）之後 `running` 永遠是 False、thread 已死，
TUI 卻繼續空轉滿 20 秒，然後印「初始化較慢，請稍等…」或「Bot 仍在初始化中
（網路慢？）」——**thread 早就死了，那兩句都是假話**。

## 改動

### 1. 兩段輪詢都偵測 thread 已死（`as_terminal_max.py`）

終止條件改成 `if self.bot.state.running or not self._bot_alive(): break`，
兩段迴圈都改（初始化可能在第 10~20 秒之間才 raise，只改第一段等於把判定延後
滿 10 秒）。分支語意：

```
loop1 (最多 10s)
 ├─ running          → 成功，_trading_active = True
 ├─ thread 已死      → _release_bot_if_dead() + _print_startup_failed()
 └─ 還活著、未 running → 印「初始化較慢」→ loop2 (最多 10s)
      ├─ running       → 成功
      ├─ thread 已死   → _release_bot_if_dead() + _print_startup_failed()
      └─ 還活著        → 維持現狀：「仍在初始化中」+ 保留參照（實話）
```

- 沿用既有的 `_bot_alive()` / `_release_bot_if_dead()`，沒有新增狀態機。
- 新增 `_print_startup_failed()`（唯一的新函式）：把兩條死亡路徑的訊息統一成
  「Bot 已結束，交易未啟動」+ 指向日誌與 Telegram 的「初始化失敗」通知。
  TUI 這層拿不到真正的例外，所以只指路、不猜原因。
- **TUI 的 20s 預算沒有改**（`range(100)` × 2 原封不動）。
- `_trading_active` 的所有寫入點語意不變：只在 `running` 為真時設 True，
  死亡路徑由 `_release_bot_if_dead()` 設 False。已 `grep -n` 過全部讀取點
  （入口守衛、`main_menu` 的 `valid_choices`、`view_trading_panel`、
  `_handle_shutdown`、reload 提示），沒有一處的判斷改變。

### 2. `_check_hedge_mode` 的 per-request timeout（`grid_engine/bot.py`）

新增 `HEDGE_MODE_FETCH_TIMEOUT_SEC = 3.0`，**只在守衛執行期間**套用：

```python
prev_timeout = self.exchange.timeout
self.exchange.timeout = int(HEDGE_MODE_FETCH_TIMEOUT_SEC * 1000)  # ccxt 用毫秒
try:
    self._resolve_hedge_mode(sym_config)
finally:
    self.exchange.timeout = prev_timeout
```

守衛本體原封不動地搬進 `_resolve_hedge_mode(sym_config)`（純位移，行為逐字
相同），只為了讓 `try/finally` 能罩住**所有** raise 路徑而不必把 100 行縮排。

- **沒有動 `_init_exchange` 的 `exchange_config`，沒有任何全域 timeout。**
  這條是本次最重要的禁令：`create_order` / `cancel_order` / `fetch_positions`
  / `fetch_balance` 共用同一個 exchange 實例，全域縮短逾時 =「單已送達交易所、
  只是回應逾時」變常態 ⇒ 斷路後重掛 = 重複掛單。`finally` 的還原就是這條禁令
  的執行面，測試專門盯它（見 M6）。
- 硬失敗語意不變：確立不了就 raise。
- 順手修掉 `HEDGE_MODE_VERIFY_ATTEMPTS` 註解第 4 條的過時說法（原本寫
  「最壞約 32s、不會紅」，現在有量測式測試接著，調大即紅）。

## 最壞耗時（模型 + 實測）

`TimedExchange` 把兩個計時來源建模在同一條虛擬時間軸上：

- 每顆請求的 wall：`requests` 的 timeout 是 connect / read 各一份 ⇒ 逾時一顆
  燒 `2T`；伺服器有回應時燒它自己的延遲，延遲 > T 即逾時。
- throttle：binance `rateLimit=50ms` × `positionSide/dual` GET cost=30 ⇒ 兩顆
  GET 最小間隔 1.5s；用「下一顆最早可送出的時間點」建模，因此與呼叫端自己的
  `sleep(1.0)` 自然**重疊**（取 max，不是相加）。POST cost=1，可忽略。

實際跑出來的數字（`measure_hedge_guard`）：

| 情境 | 量測耗時 |
|---|---|
| 全程逾時（3 顆 GET 全 timeout） | **20.00s** = 6T + 2×max(1.0, 1.5) |
| 帳戶單向 + 切換被 -4068 拒絕 + 複驗 3 次 | **4.55s** |
| 不可重試錯誤（權限/簽章） | **0.05s** + 網路延遲 |

（沒套 timeout 時最壞是 62.00s；T=5.0 時是 32.00s。）

## 測試

新增 14 條，全部在 `tests/test_hedge_mode_guard.py` 與
`tests/test_tui_bot_lifecycle.py`。

**契約測試改成量測式。** 上一條 branch 用推導常數寫算術恆等式，4 條 mutation
存活（常數改 0.0 全綠、迴圈刪掉全綠、預算改 99999 全綠），因為推導鏈的中間
節點沒有被任何斷言接到使用它的程式碼上。這次是**真的跑**：

- 前半：真實的 `MaxGridBot._check_hedge_mode`，配 `TimedExchange`，量它 raise
  的虛擬時刻。
- 後半：真實的 `as_terminal_max.MainMenu.start_trading`，把上面量到的時刻當成
  bot thread 的死亡時刻，量使用者看到定案訊息的時刻。

### 為什麼不會真的睡滿 20 秒

比例尺是 **1:1**（不是縮小的代理值），但時間軸是虛擬的：`VirtualClock.t` 只是
一個累加的浮點數。所有時間來源都導到它上面 —— TUI 的 `time.sleep(0.1)`、
守衛的 `time.sleep(HEDGE_MODE_VERIFY_DELAY_SEC)`、ccxt 的 throttle 等待、每顆
請求的 wall。因此量到的數字就是真實秒數的模型值，斷言比的也是字面的 20.0，
但測試 0.9 秒跑完。假時鐘唯一沒有覆蓋的是「真實網路延遲的分布」，那本來就不是
這條契約要守的東西（它守的是**上界**）。

### 為什麼 mutation 是紅不是 hang

`ScheduledThread` / `ScheduledState` 不開真的 thread：整條時間軸由 `start_trading`
自己的 `time.sleep` 推進，`is_alive()` / `running` 都是讀時鐘的純函式。迴圈的
上界是 `for _ in range(100)`，與被測條件無關 ⇒ 任何 mutation 的結果都是「量到
錯的數字」，迴圈一定會結束。本 repo 踩過 hang 在 CI 上是 timeout 不是 red。

### 期望值

全部寫死字面值（`3000`、`20.0`、`2.0`、`14.0`、`12.0`、`10_000`），沒有從被測
module import 常數推導。

## Mutation 實跑結果（8/8 紅，皆為斷言紅、無 hang）

| # | Mutation | 紅在哪 |
|---|---|---|
| M1 | 兩段迴圈都拿掉 `is_alive()` 偵測 | `test_fast_death_…`：`9.99999999999998 == 2.0 ± 0.15`；`test_death_inside_the_second_loop…`：`20.0 == 14.0 ± 0.15`；`test_common_hard_failure…`：`10.0 == 4.55 ± 0.15` |
| M2 | 只有第一段有偵測、第二段沒有 | `test_death_inside_the_second_loop…`：`20.0 == 14.0 ± 0.15` |
| M3 | 刪掉整段第二輪詢迴圈 | `test_death_inside_the_second_loop…`：`10.0 == 14.0`；`test_a_thread_still_alive_at_the_budget…`：`10.0 == 20.0`；`test_success_inside_the_second_loop` |
| M4 | TUI 預算改大（`range(100)`→`range(400)`） | `test_a_thread_still_alive_at_the_budget…`：`25.0 == 20.0 ± 0.15` |
| M5 | 拿掉 per-request timeout 的套用 | `test_timeout_is_applied…`：`[10000] == [3000]`；`test_all_requests_timing_out…`：`assert 62.0 <= 20.0` |
| M6 | 拿掉 `finally` 的還原（try/finally → if True） | `test_timeout_is_restored_on_the_raise_path`：`assert 3000 == 10000` |
| M7 | `HEDGE_MODE_FETCH_TIMEOUT_SEC` 3.0→5.0 | `test_timeout_is_applied…`：`[5000] == [3000]`；`test_all_requests_timing_out…`：`assert 32.0 <= 20.0` |
| M8 | `HEDGE_MODE_FETCH_TIMEOUT_SEC` 3.0→1.0（改小） | `test_timeout_is_applied…`：`[1000] == [3000]`；`test_a_slow_but_answering_exchange_still_passes`：2 秒才回的正常回應被判逾時 ⇒ 拒絕啟動 |

M8 是刻意加的下界守衛：per-request timeout 調得比「正常但慢的回應」還短，會把
健康帳戶擋在門外，跟調太大一樣是缺陷。

## 使用者可見的變化

最常見的硬失敗（持倉模式確立失敗，實測守衛 4.55s；不可重試錯誤更快）
**從固定 20 秒才看到一句假話，變成 ~4.6 秒看到真正的失敗訊息**：

```
Bot 已結束，交易未啟動
常見成因：帳戶持倉模式（雙向持倉）確立失敗，或網路/限流/API 權限 ——
確切原因看日誌與 Telegram 的「初始化失敗」通知
```

「初始化較慢」/「仍在初始化中」只在 thread 真的還活著時才會出現。

## 沒有做的事（範圍紀律）

- 沒有動 `run()` 的 except 區塊本體、`sym_state.ws_seq += 1`、
  `order_executor.py`、`sync_service.py`、`notifier.py`。
- 沒有新增 `_StartupDeadline` 這類總截止時限機制。
- 沒有改 TUI 的 20s 預算（有了 thread 偵測就不需要拿預算去補償）。
- 沒有對外部服務發任何網路請求。

## 測試結果

基線 948 passed / 2 skipped → **962 passed / 2 skipped**（+14，零退步）。

---

# 外部 review 修復（Ship with follow-ups → 全數處理）

commit `5bb0900`（+ 本段的訊息微調）

## I-1 我犯了自己批評上一版的毛病（必修，已修）

上一版的 REPORT 宣稱守衛最壞耗時 **20.00s**。那是錯的：`test_all_requests_timing_out…`
只量了「初查三顆全逾時就 raise」**一條分支**，而守衛還有「初查讀到明確 False →
POST 切換 → 複驗迴圈再三顆 GET」那一整段沒被量到。**而漏掉的那條正是使用者
最可能遇到的**（帳戶本來就是單向持倉）。

換句話說：上一條 branch 死於「推導常數沒被接上實際程式碼」，我改成真跑量測是
對的方向，但只跑一條路徑等於量了一個假的最大值 —— 同一個毛病換了個外衣。

### 修法：對分支全集取 max

`HEDGE_GUARD_PATHS` 列出六條分支，每條**各自**有一條參數化測試單獨量，另有一條
測試對全集取 max 再比字面上界。實測（虛擬時鐘，T=3.0）：

| 分支 | 耗時 |
|---|---|
| 初查逾時 2 次 → 單向 → 切換 POST 逾時 → 複驗全逾時 | **40.05s** ← 真正最壞 |
| 單向 → 切換 → 複驗全逾時 | 21.50s |
| 初查全逾時（上一版誤當成最大值） | 20.00s |
| 單向 → 切換被 -4068 拒絕 → 複驗全 False | 4.55s |
| 單向 → 切換 → 複驗通過 | 1.55s |
| 初查即通過 | 0.05s |

**max = 40.05s**，上界斷言 `<= 45.0`（字面值）。

模型同時補上了 reviewer 沒點名但同源的一個漏洞：**切換 POST 走的是同一個
exchange 實例、同一份 timeout，所以它也會逾時**。原本 POST 被寫死成 0.05s，
等於把最壞路徑上的一顆請求當成免費的。加上之後最壞從 35.50s 變成 40.05s。

### 「貼齊 TUI 20s 預算」這個框架已拿掉

reviewer 是對的：TUI 的 20s 從 `thread.start()` 起算，前面還有 `_init_exchange`
的 `load_markets` / `fetch_markets`，用的是交易所實例**原本**的逾時，不在
`HEDGE_MODE_FETCH_TIMEOUT_SEC` 管轄範圍內 ⇒「守衛塞得進 20s」從來不是個成立的
保證。所有引用 20.00s / 該框架的地方（常數註解、測試 docstring、上界的意義）
全部改寫。

**這不是要去改 `_init_exchange` 或加全域 timeout —— 禁令不變**，只是不再宣稱一個
站不住的保證。45s 這個上界承諾的是另一件事：使用者按下啟動後，守衛最壞多久會
給出一個明確結果（沒有 per-request timeout 時是 97.37s，兩分鐘級）。

而守衛可以合法地跑超過 20s 這件事之所以不再是問題，正是本次根因修復本身：
TUI 在 `min(thread 死亡時刻, 20s)` 定案，兩種結果都說實話。

## I-2 下界守衛沒守到（已修）

原本錨點是 2.0s，常數改成 2.5 時它不會紅（真正殺掉那個 mutation 的是
`seen_timeouts == [3000]` 那條字面斷言）。錨點改成
`SLOW_BUT_HEALTHY_RESPONSE_SEC = 2.8`。

依據：這條線釘的是「`HEDGE_MODE_FETCH_TIMEOUT_SEC` 不得調到幣安 REST 的長尾
延遲以下」，因為守衛自己判逾時的代價不只是「bot 沒跑」——`sync_service` 在
raise 之後才啟動，既有持倉會同時失去追蹤止盈與網格管理。**2.8s 是推測值**
（我沒有實測的延遲分布，測試 docstring 已標註）；它的角色是錨點而不是預測，
日後若拿到實測 p99.9 應該用實測值取代。

已驗證：常數改 2.5 時 `test_a_slow_but_answering_exchange_still_passes` **自己**
紅（見下方 M12）。

## Minor-2 / M11 mutation 存活（已修）

fixture 的 `exchange.timeout` 初值原本就是 ccxt 預設的 `10000`，於是「還原成
`prev_timeout`」與「還原成寫死的 `10000`」不可分辨 —— 正是本 repo lessons 通則
3 第 4 條（測試值 == 欄位預設值 ⇒ 套套邏輯）。改成 `PREEXISTING_TIMEOUT_MS = 7777`，
一個沒有任何一段程式碼可能猜到的值。M11 現在紅（`assert 10000 == 7777`）。

## Minor-3 共享可變狀態的前提（已修）

`exchange.timeout` 的安全性完全靠「所有同步 REST 都經 `RestGateway` 卸載到
`max_workers=1` 的單一 worker thread」這個前提（同步 ccxt 實例不是 thread-safe）。
原本註解火力全放在「不要改全域」，剛好漏掉這一面。

- `_check_hedge_mode` 的註解點名這個依賴。
- 新增 `test_guard_relies_on_the_rest_gateway_being_single_worker` 釘住
  `max_workers == 1`。`max_workers` 改成 2 ⇒ 紅。

## Minor-5 跨檔 import autouse fixture（已修）

`from tests.test_hedge_mode_guard import _no_real_sleep` 會讓那個 **autouse**
fixture 對整個 `test_tui_bot_lifecycle.py` 生效，使全檔跑在「全域 `time.sleep`
被換掉」的狀態下 —— 本檔不需要，而且會讓未來任何真的需要 sleep 的測試靜默失真。
已移除該 import（`measure_hedge_guard` 自己用 monkeypatch 接管守衛的 sleep，
不依賴那個 fixture；`grid_engine.clock` 的全域狀態在守衛路徑上沒有被碰）。

沒有新增 `conftest.py`：不需要 —— 需要的只是**不要**把 autouse 帶過來。

## Minor-6 公式與數字對不起來（已修）

`6T + 2×max(1.0, 1.5)` 代進去是 21 不是 20（那條錯公式正是 I-1 的來源之一）。
「初查全逾時」那條的正確結構是 `3 × 2T + 2 × max(sleep 1.0, throttle 1.5)`，
但 throttle 的等待與請求本身的 wall 也重疊 ⇒ 實際是 20.00s。REPORT 不再放
簡化公式，改放**實測表格**：任何公式都可能再錯一次，量測不會。

## Minor-1 文案在窄窗口說假話（已修）

`bot` 可能已經把 `running` 設成 True、甚至已經掛了單，然後才崩潰結束 thread
（輪詢在兩次取樣之間就會錯過那個 True）。那個窗口裡「交易未啟動」是假話，而且
是最貴的一種 —— 使用者會以為交易所上乾乾淨淨。改成：

```
Bot 已結束（背景 thread 不在運行）
常見成因：帳戶持倉模式（雙向持倉）確立失敗，或網路/限流/API 權限 ——
確切原因看日誌與 Telegram 的「初始化失敗」通知
若它曾短暫啟動過，交易所上可能已經有掛單/持倉，請一併確認
```

## Backlog（記錄，本次不修）

- **Minor-4：`_trading_active` 的競態零覆蓋。** TUI 測試把 `run_bot_thread`
  整個換掉（改用虛擬時鐘的 `ScheduledThread`），所以「bot thread 的 finally 把
  `_trading_active` 設 False」與主執行緒的讀取之間的競態沒有被覆蓋。要真的覆蓋
  得動測試架構（真 thread + 真時間 ⇒ 慢且不決定性，或引入可注入的排程器），
  成本高於價值。取捨記在這裡，不是遺漏。
- `SLOW_BUT_HEALTHY_RESPONSE_SEC = 2.8` 是推測值，等實測延遲分布出來要複核。

## 本輪 Mutation（5/5 紅，皆為斷言紅、無 hang）

| # | Mutation | 紅在哪 |
|---|---|---|
| M11 | `finally` 還原成寫死 `10000` | `test_timeout_is_restored_on_the_raise_path` / `…after_success`：`assert 10000 == 7777` |
| M12 | `HEDGE_MODE_FETCH_TIMEOUT_SEC` 3.0→2.5 | **`test_a_slow_but_answering_exchange_still_passes`（下界測試自己紅）**，另加字面斷言與最壞耗時測試 |
| M13 | 3.0→5.0 | `test_the_worst_branch…`：`64.05s（分支「初查逾時2次→單向→切換逾時→複驗全逾時」）` 超過 45s，訊息附全六條分支的數字；參數化測試也各自紅 |
| M14 | 分支表刪掉最壞那條路徑的量測 | `test_the_worst_branch…`：`assert 21.5 >= 36.0` |
| M15 | `RestGateway` `max_workers` 1→2 | `test_guard_relies_on_the_rest_gateway_being_single_worker` |
| M5 重跑 | 拿掉 per-request timeout 的套用（fixture 原值改 7777 之後） | 最壞 `97.37s > 45.0`，四條分支測試同時紅 |

## 測試結果

962 passed / 2 skipped → **969 passed / 2 skipped**（+7，零退步）。
