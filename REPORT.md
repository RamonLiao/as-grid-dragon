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
