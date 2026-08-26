# 週期性 REST 同步 task 設計（B1-A）

日期：2026-08-26
狀態：待實作（brainstorming 已與使用者逐段確認）
範圍代號：TODO B1 的 (A) 半 —— **不含** bookTicker liveness watchdog 與 WS 重連（那是 B 半，另案）

---

## 1. 問題

`SyncService.maybe_sync()` 全 repo 只有一個週期性呼叫端：`grid_engine/bot.py:625`，
位於 `_handle_ticker` 內。也就是說 **REST 同步被綁在 WebSocket 的 bookTicker 推送上**。

bookTicker 一停推送（WS 靜默失效、交易所端斷流、網路分區），下列全部靜默停擺：

- `_sync_positions()` —— 持倉與浮盈（風控判斷的輸入）
- `_sync_orders()` —— 四側掛單數
- `_sync_account()` —— 錢包／可用／保證金／浮盈，以及**保證金風控告警**的觸發點
- `_sync_funding_rates()`
- `_sync_trade_stats()` —— 成交統計與游標推進

停擺沒有任何徵兆：面板數字凍結在舊值，風控拿著過期持倉繼續判斷，沒有人被通知。

2026-08-25 上線的價格時效守衛（`bot.py:415-419`）在這個 failure mode 下不是瓶頸也不是解方 ——
它只擋掛單，擋不住「風控輸入過期」。守衛的 review 自評已把本項列為 backlog 最高優先。

## 2. Goals

- G1：bookTicker 完全靜默時，REST 同步仍以 `sync_interval` 的節奏持續運作。
- G2：REST 同步的驅動源**唯一**，不再由 WS handler 驅動（**2026-08-26 修訂**：
  原文寫的是「`maybe_sync()` 的驅動源」，該方法已刪除，目標本身不變）。
- G3：REST 同步的關鍵項連續失敗時**主動告警**，不再靜默停擺。
- G4：本改動自己不得引入新的靜默故障——驅動 task 不會因例外而死，且它的降級狀態可見。

## 3. Non-goals

- **不做** bookTicker liveness watchdog、不做 WS 強制重連（B1 的 (B) 半，另案）。
- **不做** `given_up` 終態或退避（backoff）。理由見 §7.3。
- **不改** 五個子同步項的內部邏輯與吞例外的控制流。
- ~~**不改** `bot.py:788` 啟動時首次 `sync_all()` 的行為。~~
  **2026-08-26 修訂（見 §10）**：改為把回傳值餵進 `_evaluate()`。啟動當下 REST
  就壞掉（key 被撤、IP 被擋）是最該立刻知道的情境，丟掉回傳值等於那一輪不計數。
- **不動** `clock.py` 的 `now()` / `guard_now()` 分離設計。

## 4. Security / 安全約束

- 告警文案只用本檔定義的常數與數字，**不把外部資料未跳脫插進 HTML 訊息**
  （notifier 用 `parse_mode=HTML`）——與 `_format_watchdog_line` /
  `_format_stale_quote_line` 同一條要求。
- 告警內容不得包含 API key、listenKey、chat_id 或任何憑證片段；REST 例外訊息
  只進 log，不進 Telegram（notifier 已有 `_redact`，但本設計採更嚴格作法：
  Telegram 只送「哪一項失敗、連續幾次」，不送例外原文）。
- 本改動不新增任何檔案寫入、不觸碰 config 存檔路徑。
- **資金安全（2026-08-26 dual-review C1 新增，見 §10 的修訂 10）**：REST 同步的
  `fetch → apply` 窗口內，state 可能已被 WS handler 改過（handler 不取 symbol
  lock），把 REST 舊快照無條件蓋回去會讓 `_grid_step` 走錯分岔（撤掉剛掛好的網格
  並重新開倉）或讓網格靜默漏掛。約束：`_sync_positions` / `_sync_orders` 必須在
  fetch 前抓 `SymbolState.ws_seq`、apply 時在 symbol lock 內比對，不一致就丟棄該
  **symbol** 的快照。鎖序不變式 `_sync_lock → symbol lock` 維持單向不變。

## 5. 設計

### 5.1 `SyncService` 取得生命週期（對稱 `UserDataWatchdog`）

新增：

- `self._stop_event` —— **2026-08-26 修訂**：吃 bot 傳入的共享 `stop_event`
  （與 ws_client / userdata_watchdog / reporter 同構），未傳時才自造。
- `async def run()` —— 常駐 loop
- `def stop()` —— set event
- `def _notify(message: str)` —— **逐字沿用** `userdata_watchdog.py:168-182` 的作法：
  `notifier.enabled` 檢查 → `create_task(notifier.send(...))` → 存進共享 `self.tasks`
  防 GC → `add_done_callback` 自移除 → 無 running loop 時只 log warning（不 `asyncio.run`）。

`run()` 的迴圈骨架（順序與 `userdata_watchdog.py:155-166` 對齊）：

```
while not self._stop_event.is_set():
    slept = False
    try:
        await asyncio.sleep(self._loop_interval())   # 每輪重讀 config
        slept = True
        if self._stop_event.is_set():
            break
        await self.sync_once()                       # 修訂 7（B5）：= sync_all() + _evaluate()
    except asyncio.CancelledError:
        break
    except Exception as e:
        logger.error(f"[sync] 週期同步失敗: {e}")
        self._evaluate(None, loop_error=True)
        if not slept:                                # 修訂：I3 的忙迴圈防禦
            try:
                await asyncio.sleep(SYNC_INTERVAL_FALLBACK)
            except asyncio.CancelledError:
                break
```

**2026-08-26 修訂（見 §10）**：loop 直接呼叫 `sync_all()`，`maybe_sync()` 已刪除；
且任何走到 `except Exception` 而本輪未曾 sleep 的路徑都必須先補睡一次
（`_loop_interval()` 求值失敗會讓 sleep 整句沒被執行 ⇒ 100% CPU 忙迴圈）。

- `except asyncio.CancelledError: break` **必須在** `except Exception` 之前。
  `bot.stop()` 靠 cancel + await 收尾（`bot.py:853-858`），吃掉 CancelledError 會讓關機卡住。
- `_loop_interval()` 讀 `config.sync_interval`，並對非法值（`<= 0`、非數、NaN）
  夾到合理下限（見 §5.5）。

### 5.2 接線

- `bot.run()` 的 `tasks.extend([...])`（`bot.py:796`）加入
  `asyncio.create_task(self.sync_service.run())`。
- `bot.stop()` 不必改：既有的 `for task in list(self.tasks): task.cancel()` 已涵蓋。
- **移除** `bot.py:625` 的 `await self.sync_service.maybe_sync()`。

移除的後果，刻意接受並記錄在此：sync 的例外從此不再冒泡進 `_handle_ticker` →
`ws_client` outer except，也就**不再觸發「sync 全掛 ⇒ 每 5 秒重連一次 WS」**的既有放大器。
那條路徑的失敗語意改由 §5.4 的告警承接。

### 5.3 `sync_all()` 回報成敗

新增 frozen dataclass（放在 `sync_service.py`）：

```
@dataclass(frozen=True)
class SyncOutcome:
    positions_ok: bool = True
    orders_ok: bool = True
    account_ok: bool = True
    funding_ok: bool = True
    trade_stats_ok: bool = True
    skipped: bool = False
```

- 五個子項的既有 `try/except` **控制流完全不動**，只在 except 分支多記一筆 False。
- `_sync_lock` 已被持有時的 early-return → 回 `SyncOutcome(skipped=True)`
  （其餘欄位維持 True，不參與判定）。
  ⚠️ `tests/test_async_offload.py:205/238/257` 用三個並發 `sync_all()` 守著這個
  early-return 語意，回傳值的加入不得改變它。
- ~~`maybe_sync()` 回傳 `Optional[SyncOutcome]`：節流門檻未過 → `None`。~~
  **2026-08-26 修訂（見 §10）**：`maybe_sync()` 整個刪除。`run()` 的 `sleep`
  已經是節流器，第二把用不同時鐘的閘門不提供保護、只提供失效模式。
- ~~`bot.py:788` 的啟動同步忽略回傳值（行為零變更）。~~
  **2026-08-26 修訂**：回傳值餵進 `_evaluate()`。
- **2026-08-26 新增**：`sync_all()` 成功結束時蓋 `last_sync_time`（`skipped`
  的 early-return 路徑不蓋）。這是每日摘要心跳那一行的唯一資料來源。

### 5.4 降級判定與告警狀態機

**關鍵項** = `positions_ok`（持倉，風控的輸入）與 `account_ok`（權益／保證金，告警的輸入）。
`orders_ok` / `funding_ok` / `trade_stats_ok` 失敗只留 log，**不進計數**——
掛單數只影響顯示與 requote 計數，funding 與成交統計是遙測，
把它們納入會被偶發 REST 抖動洗版，而它們失敗不影響交易安全。

狀態：`_consecutive_failures: int`、`_degraded: bool`。

| 事件 | 動作 |
|---|---|
| 關鍵項任一 False，或 loop 級例外 | `_consecutive_failures += 1` |
| 兩個關鍵項都 True | `_consecutive_failures = 0`；若 `_degraded` → 發「已恢復」、`_degraded = False` |
| `outcome is None`（節流未過）或 `skipped` | 不算成功也不算失敗，計數不動 |
| `_consecutive_failures` 達 **3** 且 `not _degraded` | 發一封告警、`_degraded = True` |
| `_degraded` 期間持續失敗 | **不重發** |

門檻 3 次 ≈ 30 秒（`sync_interval` 預設 10s）。取值理由：短到能在一次保證金事件的
時間尺度內發出，長到不會被單次 REST 抖動觸發。

告警文案（常數，不插外部資料）：
- 降級：`⚠️ REST 同步降級：持倉/帳戶同步連續失敗 N 次，風控輸入可能過期`
- 恢復：`✅ REST 同步已恢復`

### 5.5 時鐘與非法 interval

- ~~`maybe_sync()` 的節流計時 `_time()` → `clock.guard_now()`。~~
  **2026-08-26 修訂（見 §10）**：節流本身已隨 `maybe_sync()` 一起刪除。
  `guard_now()` 的要求改用在 `sync_all()` 的 `last_sync_time` 蓋章上——同一條
  理由（可注入、與價格時效守衛同一個守衛時鐘），只是量的東西從「上次節流放行」
  變成「上次同步真的跑完」。
- ⚠️ **`_time` 不得整個刪除**：`test_trade_stats_sync.py:379/521` 正在 monkeypatch
  `grid_engine.sync_service._time`，那是給 `TRADE_STATS_INTERVAL` 用的。只換
  `maybe_sync` 這一處。**2026-08-26 修訂**：`maybe_sync` 已刪除，這條約束仍然
  成立且更重要——`_time` 現在只剩 `TRADE_STATS_INTERVAL`（見 Ruling 6 的 backlog）
  與 `start_time_ms` 預設值在用。
- `_loop_interval()`：`config.sync_interval` 非數／NaN／**`±inf`**／`<= 0` → 用
  `SYNC_INTERVAL_FALLBACK` = **10.0 秒**（= `GlobalConfig.sync_interval` 的預設值）
  fallback 並記一次 warning（**2026-08-26 修訂**：它必須是 total function，
  `except Exception`——`self.config` 為 None 時的 `AttributeError` 會讓
  `await asyncio.sleep(self._loop_interval())` 整句沒被執行，見 §10 的 I3；
  fallback 值與常數名見 §10 的修訂 6，`inf` 見修訂 8）。
  不糾正的話 `sleep(0)` 會變成忙迴圈打爆 REST 配額、`sleep(inf)` 會讓同步整條
  停擺；只糾正非法值、不夾合法小值，是因為使用者刻意調小是合法意圖。

### 5.6 每日摘要多一行

`notifier.py` 加 `_format_sync_line(sync)`，與既有 `_format_watchdog_line` /
`_format_stale_quote_line` 同一 pattern，接進 `notify_daily_pnl`：

**2026-08-26 修訂**：加一段**心跳**，優先於下列所有分支：
`last_sync_age`（= `guard_now() - last_sync_time`，由 `_get_sync_status` 一併帶出，
formatter 是 staticmethod、不得讀全域狀態，故門檻用的 `sync_interval` 也一起帶）
超過 `min(max(60, 6 * sync_interval), 3600)` ⇒ 無條件印停擺警告（天花板見 §10
的修訂 8：`sync_interval` 被設成巨大但有限的值時，沒有天花板的門檻大到永不告警
= 停擺偵測被一個設定值靜默關掉）；`None`（從未同步過）與
負值（牆鐘回跳，沿用 `bot._note_stale_quote` 的既有態度）各有專屬告警文案，
一律不省略。另：欄位型別壞掉（如 `consecutive_failures=None`）改成印保守文案，
不再讓整行消失（`int(None)` 走 `except → return ""` 等於降級告警被靜默吞掉）。

- 非 dict／None ⇒ 整行省略
- `degraded` 為真 ⇒ `⚠️ REST 同步：降級中（連續失敗 N 次）`
- 正常且**自啟動以來曾降級過** ⇒ `✅ REST 同步：正常（曾降級 M 次）`
- 正常且從未降級 ⇒ 整行省略（不加噪音）

理由：降級狀態若只靠即時告警，錯過那一封就再也看不到。計數口徑一律「自啟動累計」，
不做 snapshot-diff 造假的日增量（與 `_format_stale_quote_line` 的既有裁決一致）。

## 6. 驗收準則（可判定）

1. bookTicker 完全靜默（`_handle_ticker` 一次都不被呼叫）時，`sync_all()` 仍隨
   `sync_interval` 被週期呼叫 —— 有測試，且該測試在移除 `bot.py:625` **之前**先紅。
2. `_handle_ticker` 跑完一輪後 `sync_all()` **不**被呼叫（單一 driver 被釘死）。
3. 告警狀態機：2 次失敗不發 → 第 3 次發一封 → 第 4、5 次不重發 → 恢復發一封。
4. 非關鍵項（orders / funding / trade_stats）連續失敗 10 次不發任何告警。
5. `run()` task 被 cancel 後乾淨結束，`bot.stop()` 不卡（有測試）。
6. loop 內任何 `Exception` 不殺 task，下一輪照跑。
7. `tests/test_async_offload.py` 的並發 `sync_all()` 三條照樣綠。
8. `config.sync_interval` = 0 / -5 / NaN / 字串 時 loop 不進忙迴圈、不崩。
   **2026-08-26 新增**：`self.config` 整個壞掉（屬性存取拋 `AttributeError`）時
   同樣不得進忙迴圈——有限時間窗內斷言迭代次數上限。
11. **2026-08-26 新增**：`run()` 的 task 確實出現在 `bot.tasks` 且未結束
   （runtime 斷言，不是對 `inspect.getsource` 的字串比對）。
12. **2026-08-26 新增**：loop 死掉/從未被建立時，每日摘要那一行**不得**與一切
   正常時逐字元相同——`last_sync_age` 超過 `max(60, 6 * sync_interval)` 無條件
   印停擺警告；「從未同步」與「age 為負（牆鐘回跳）」各有專屬文案，都不省略。
9. 全套測試綠。基線**在實作用的 worktree 內實跑取得**，不沿用主目錄數字——
   worktree 沒有 gitignore 的 `config/`，`tests/web/test_config_store.py` 會多一個 skip
   （2026-08-25 的價格時效守衛就踩過這個對不上）。實盤引擎在本機常駐，
   跑測試前先 `pgrep -f as_terminal_max` 確認，測試不得寫 `config/` 或 `log/`。
10. 每個新守衛先在真實缺陷面前紅過一次（mutation），零存活。

## 7. 已考慮並否決的選項

### 7.1 保留 `bot.py:625` 的 ticker 呼叫（加法、零回歸）
否決。本改動的本質就是「REST 同步不該綁在 WS 推送上」，留一半等於承認結構錯誤還留著它；
且留著的話週期 task 幾乎永遠只是空跑節流檢查，沒有測試能區分它有沒有真的在工作——
這種「平常永遠不生效」的守衛最容易腐爛。順帶關掉 §5.2 那個 5s 重連放大器的邊際成本近乎零。

### 7.2 只包 loop 級 `try/except`，不改 `sync_all()` 回傳（判準 (a)）
否決。`sync_all()` 的五個子項各自吞例外，loop 那一層**幾乎永遠看不到例外**，
該告警會是接近死碼的東西。花力氣修一個靜默故障，卻讓修法本身可以靜默故障，說不過去。

### 7.3 完整狀態機 + 退避 + `given_up`（對稱 userData watchdog）
否決。userData 需要退避是因為重連要打 listenKey API 且有頻率限制；這裡只是重試同一組
REST 呼叫，`rest_gateway` 本來就是單 worker 排隊，多等沒有好處。
且 `given_up` 語意在這裡**有害**：「放棄同步」＝「放棄風控」，不該存在這個終態。

### 7.4 全項失敗都納入告警（判準 (c)）
否決。會被 funding / trade_stats 的偶發 REST 抖動洗版，而那兩項失敗不影響交易安全。

### 7.5 loop 放新檔 `SyncScheduler` 或 `bot.py` 私有方法
否決。失敗語意（哪一項失敗、連續幾次、恢復了沒）天生屬於 `SyncService`；
獨立檔會變成兩個物件來回傳訊。`UserDataWatchdog` 之所以獨立，是因為它監視的是**別人**
（WS 推送），而本 loop 監視的是自己。`bot.py` 已 861 行，不再往裡塞可獨立測試的邏輯。

## 8. 風險

- **R1（最高）**：移除 ticker driver 後，若 `run()` task 因任何理由沒被建立或提早退出，
  REST 同步**完全消失**——比今天更糟。緩解：§5.1 的吞例外續跑、驗收準則 5/6、
  以及 §5.6 的每日摘要可見性。
  **2026-08-26 修訂**：原緩解不成立——`_format_sync_line` 在 `degraded=False and
  degraded_total==0` 時回 `""`，而 loop 死掉時這兩個值必然就是 False/0 ⇒ 摘要那行
  與健康時逐字元相同，最致命的失效模式沒有儀器。真正的緩解是 §5.6 的**心跳**
  （`last_sync_age`），它由 `sync_all()` 蓋章推進，不依賴 loop 還活著才會動。
- **R2**：sync 時序從「tick 驅動」變成「固定週期」。節流門檻本來就是 10s，
  實務差異應極小（推測，實作時以 log 時距抽查）。
- **R3**：`SyncOutcome` 改動 `sync_all()` 簽章，既有測試需逐一確認仍守得住原本的東西
  （`test_async_offload.py`、`test_trade_stats_sync.py`、`test_account_update.py`）。

## 9. Red Team（實作前，dev-rules 要求）

1. **`sync_interval` 被設成 0 或負值** → `sleep(0)` 忙迴圈打爆 REST 配額。
   防禦：§5.5 夾 1.0 秒下限 + warning。
2. **REST 永久失敗（key 被撤、IP 被擋）** → 告警發一封後 `_degraded` 永久為真，
   之後完全安靜。防禦：接受（不做週期提醒），但 §5.6 讓每日摘要持續顯示降級中。
3. **`notifier` 未設定或 `send()` 自己拋例外** → 不得殺掉 loop。
   防禦：`_notify` 沿用 watchdog 的 create_task + enabled 檢查，例外落在 task 內。
4. **同行程回測**：backtester 不建 `MaxGridBot`，`run()` 不會被啟動（推測，實作時驗證）；
   節流改用 `guard_now()` 後即使被啟動也不受歷史時鐘污染。
5. **`bot.stop()` 期間最後一輪 sync 正在跑** → cancel 落在 `await sync_all()` 內
   （**2026-08-26 修訂**：原文寫的是 `await maybe_sync()`），
   `_sync_lock` 由 `async with` 釋放，`gateway.shutdown()` 取消排隊呼叫。
   防禦：驗收準則 5 的測試涵蓋。

---

## 10. Spec 修訂紀錄

### 2026-08-26 — 最終 whole-branch review 的 fix wave

以下修訂由 branch `feat/periodic-sync-task` 的最終 review（B4 / M5）觸發，
依 dev-rules「偏離 spec 的 tradeoff 必須先修訂 spec（留痕）」處理。原設計不是
「本來就這樣寫」，被撤回的內容逐條列在下面。

**修訂 1（I2 / Ruling 4）—— 刪除 `maybe_sync()`，`run()` 直接呼叫 `sync_all()`。**

- 影響段落：§5.1（loop 骨架）、§5.3（回傳值）、§5.5（時鐘）。
- 被撤回的原設計：`run()` 每輪呼叫 `maybe_sync()`，由 `clock.guard_now() -
  last_sync_time > config.sync_interval` 決定要不要真的同步；未達門檻回 `None`，
  `_evaluate(None)` 不計數。
- 為什麼撤回：那是**兩把不同時鐘的閘門疊在一起**。`asyncio.sleep()` 用 event
  loop 的 monotonic 時鐘，`maybe_sync()` 的節流用 `guard_now()`（牆鐘）。NTP slew
  最大 500 ppm，10 秒 sleep 之後牆鐘可能只走 9.995s ⇒ `9.995 > 10` 為假 ⇒ 該輪
  回 `None`、實際週期靜默變成 20s；而 `_evaluate(None)` 刻意不計數 ⇒ **不留任何
  痕跡**。牆鐘往回跳則會讓同步停擺整個跳幅，同樣靜默。loop 的 `sleep` 本身已經
  是節流器，第二把閘門不提供保護、只提供失效模式。移除 ticker driver 後
  `maybe_sync()` 也已經沒有其他呼叫端，留著就是一條沒有 driver 的 dead path。
- 連帶：`last_sync_time` 的蓋章從 `maybe_sync()` 移到 `sync_all()` 成功結束時
  （`skipped` 的 early-return 不蓋），順帶讓 `bot.run()` 啟動時那次同步也蓋章，
  心跳從開機就正確。刪除 `tests/test_periodic_sync.py` 專測節流的兩條，並清掉
  `tests/test_price_staleness_guard.py:63,110` 的惰性 mock。
- 代價（若判斷錯）：失去「剛同步過就跳過」的節流語意。生產已無其他呼叫端需要它。

**修訂 2（M1 / Ruling 5）—— §3 non-goal「不改 `bot.py:788` 啟動時首次 `sync_all()`
的行為」撤回。**

- 被撤回的原設計：啟動同步忽略回傳值，行為零變更。
- 為什麼撤回：啟動當下 REST 就壞掉（key 被撤、IP 被擋）是最該立刻知道的情境，
  忽略回傳值等於那一輪完全不計數，還要再等 3 × `sync_interval` 才有第一次計數。
  改動是一行。
- 代價（若判斷錯）：開機瞬時失敗會佔掉一次計數；三次門檻仍需連續，影響可忽略。

**修訂 3（I1）—— §8 R1 的緩解重寫、§5.6 加心跳。**

- 被撤回的內容：R1 原本宣稱「§5.6 的每日摘要可見性」是 loop 死掉的緩解。
- 為什麼撤回：`_format_sync_line` 在 `degraded=False and degraded_total==0` 時
  回 `""`，而 loop 從未被建立／被 `BaseException` 帶走／被 cancel 時，那兩個值
  **必然**就是 False/0 ⇒ 摘要那行整個消失，與一切正常時的輸出逐字元相同。整條
  branch 的價值主張是「不准靜默停擺」，而它自己最致命的失效模式沒有儀器。
- 新緩解：心跳 `last_sync_age`。它由 `sync_all()` 蓋章推進，不依賴 loop 還活著。

**修訂 4（I3）—— §5.5 `_loop_interval()` 必須是 total function。**

- 被撤回的內容：只接 `(TypeError, ValueError)`。
- 為什麼撤回：`self.config` 為 None 或 `sync_interval` 屬性消失時拋
  `AttributeError`，它逃出 `_loop_interval()` ⇒ `await asyncio.sleep(...)` 整句
  沒被執行 ⇒ 被 loop 的 `except Exception` 接住 ⇒ 立刻下一輪 ⇒ 再拋 ⇒ 100% CPU
  忙迴圈，每輪一行 `logger.error`（實盤引擎會以幾十萬行/秒寫 log）。§9 紅隊第 1
  條想擋的正是這個，但防禦擋住了 `sleep(0)`、沒擋住「sleep 根本沒被執行」。
- 新防禦是兩道：`_loop_interval()` 改 `except Exception`（不製造沒有 sleep 的
  一輪），以及 loop 的 `slept` 旗標（就算製造了也要補睡一次才准進下一輪）。

**修訂 5（M2）—— §5.1 `_stop_event` 改吃 bot 的共享實例。**

- 被撤回的內容：`self._stop_event = asyncio.Event()`（自造）。
- 為什麼撤回：`ws_client` / `userdata_watchdog` / `reporter` 都收
  `stop_event=self._stop_event`。自造私有事件意味著 `bot._stop_event.set()`
  **停不了這條會下單的 loop**（`_sync_account → check_trailing_stop →
  close_symbol_positions` 會送市價平倉單）。今天沒事只是因為 `bot.stop()` 剛好
  還會 `task.cancel()`——那是巧合不是設計。

~~**未修訂、明確留到 backlog（Ruling 6）**：`_sync_trade_stats()` 的節流仍用
`clock.now()`（情境時鐘）而非 `guard_now()`。既有問題、非本 branch 引入，修它
要動 `tests/test_trade_stats_sync.py` 的 frozen_clock fixture。~~
**已於下方修訂 9 撤銷這個 backlog 決定，本 branch 一併修掉。**

### 2026-08-26（第二輪）— dual-review 的 fix wave

以下修訂由 branch `feat/periodic-sync-task` 的 dual-review（C1 / B2–B5 / M6–M12）
觸發，處理方式同上：偏離逐條留痕，不偷改。

**修訂 6（M7）—— §5.5 非法 config 的 fallback 從 1.0s 改成 10.0s，常數改名。**

- 影響段落：§5.1（loop 骨架的補睡）、§5.5。
- 被撤回的原設計：`MIN_SYNC_INTERVAL = 1.0`，非法 `sync_interval` 一律退到 1 秒。
- 為什麼撤回：那是「config 已經壞掉」的情境下**把 REST 頻率拉高 10 倍**。
  `RestGateway` 是單 worker、與 `place_order` 共用同一條 queue ⇒ 同步風暴會延遲
  下單，還可能吃到 Binance 的權重限制。壞掉的設定應該退回**預設行為**，不是退回
  最激進的行為。新值 = `GlobalConfig.sync_interval` 的預設值（10.0），由
  `test_fallback_equals_config_default` 釘住兩者相等。
- 常數同時改名 `MIN_SYNC_INTERVAL` → `SYNC_INTERVAL_FALLBACK`：它從來就不是
  「最小值」（合法的小值不會被夾），舊名字與語意不符。

**修訂 7（B5）—— §5.1/§5.2 新增 `sync_once()`，`run()` 與 `bot.run()` 都只呼叫它。**

- 被撤回的原設計：`run()` 寫 `self._evaluate(await self.sync_all())`，而
  `bot.run()` 的啟動同步（修訂 2 加的那行）從模組外呼叫私有 `_evaluate()`。
- 為什麼撤回：「一輪同步的結果必須被評估」這條不變式散落在兩個檔案，任何一邊
  漏掉就是一整輪不計數而且靜默；跨模組呼叫私有方法也讓 `_evaluate` 的簽章
  事實上變成公開 API。`sync_all()` 維持純粹（只同步、只回報，既有並發測試直接
  用它），評估收在 `sync_once()` 一處。

**修訂 8（B3）—— §5.5 `_loop_interval()` 必須擋 `±inf`；§5.6 停擺門檻加天花板。**

- 被撤回的內容：`math.isnan(interval) or interval <= 0`（漏掉 `+inf`）；
  心跳門檻 `max(60, 6 * sync_interval)`（沒有上限）。
- 為什麼撤回：`sync_interval = inf` ⇒ `asyncio.sleep(inf)` 永遠不醒，`_stop_event`
  也叫不醒它（`sleep` 不受 event 中斷），執行中改回正常值同樣救不回來（每輪才重讀
  config，而這一輪不會結束）⇒ REST 同步整條停擺、降級狀態機一次都不會被推進 =
  完全靜默，正是本 branch 要根除的形態。`notifier._format_sync_line` 對同一個量
  特地擋了 `±inf`，同一份 diff 在 producer 端漏掉。
  第二個洞在同一處：`sync_interval` 被設成巨大但**有限**的值（例如 86400）時，
  `6 * interval` 讓停擺門檻大到永不告警——停擺偵測被一個設定值靜默關掉。
- 新防禦：`not math.isfinite(interval) or interval <= 0`；門檻夾 `3600` 天花板
  （`SYNC_STALE_CEILING_SEC`）。1 小時沒有任何一輪同步跑完，不管 interval 設多少
  都是異常。

**修訂 9（B4）—— 撤銷「Ruling 6 留 backlog」，`_sync_trade_stats` 的節流改
`guard_now()`；本檔所有計時一律 `guard_now()`。**

- 被撤回的內容：上一輪明文把它留在 backlog（理由是「既有問題、非本 branch 引入，
  且要動測試 fixture」）。
- 為什麼撤回：本 branch 花大篇幅論證 `last_sync_time` 必須用 `guard_now()`，理由是
  「live bot 與 backtester 同行程，`set_clock()` 會把 `now()` 換成歷史 epoch」——
  同一個理由對 `_last_trade_stats_at` 一字不差成立，把它留在 backlog 等於在同一個
  檔案裡同時採用兩個互斥的 pattern（違反 dev-rules「衝突處理」）。實際後果：邊實盤
  邊點回測時，回測期間差值為大負數 ⇒ 每輪 early-return ⇒ 成交統計/已實現盈虧靜默
  凍結；回測結束 `reset_clock()` 後時間戳卡在歷史 epoch ⇒ 節流永久失效 ⇒ 每 10s
  打一次 `fetch_my_trades`（`tests/test_trade_stats_sync.py` 明文警告過的「靜默變成
  6 倍 API 權重」）。
- 連帶：`tests/test_trade_stats_sync.py` 的 `frozen_clock` fixture 改注入
  `set_guard_clock`；該檔既有斷言的語意不變（推進的仍然是「節流看到的那個時間」）。
- 規約寫進 `sync_service.py` 檔頭：本檔所有計時／節流一律 `clock.guard_now()`。

**修訂 10（C1）—— §4 安全約束新增：REST 的 fetch→apply 窗口必須有版本號守衛。**

- 被撤回的內容：`sync_service.py` 檔頭原本宣稱兩條路徑「不會在同一個 symbol 上
  交錯改狀態」。**那句話在移除 ticker driver 之後是錯的。**
- 為什麼撤回：symbol lock 只保護 apply 的那一瞬間（鎖內無 await），不保護
  `fetch → apply` 之間那一整趟 REST round-trip；而 WS handler
  （`_handle_account_update` / `_handle_order_update`）根本不取 symbol lock。
  改動前這件事不會發生純粹是因為 `sync_all()` 被 await 在 `_handle_ticker` 內、
  ws_client 的 recv 迴圈一次只跑一個 handler ⇒ 那個 await 期間沒有任何 handler
  能執行。搬成獨立 task 之後這個天然序列化消失了。
  後果會動錢且靜默：成交後 WS 把 `long_position` 寫成 0.02，REST 舊快照蓋回 0 ⇒
  `_grid_step` 走「無倉位」分支 ⇒ 撤掉剛掛好的網格 + 重新開一次倉；反方向則是
  掛單計數被寫回非 0 ⇒ `_should_adjust_grid` 回 False ⇒ 該側網格靜默漏掛。
- 新不變式：`SymbolState.ws_seq`（per-symbol 版本號）。WS handler 每次動持倉/掛單
  計數就 `+1`（寫入之前遞增，部分寫入也算髒）；`_sync_positions` / `_sync_orders`
  在 `gateway.call` **之前**抓一份，apply 時**在 symbol lock 內**比對，變了就丟棄
  **該 symbol** 的快照（不是整輪）並記一行 log，下一輪自然補上。
- 已考慮並否決：把 fetch 也放進 symbol lock —— 那會讓 `adjust_grid` 的
  `if lock.locked(): return` 在每次 REST round-trip 期間丟掉所有 tick。

**修訂 11（M6）—— `DailyReporter` 移除 `sync_source` ctor kwarg。**

- 被撤回的內容：`DailyReporter.__init__(..., sync_source=None)`。
- 為什麼撤回：生產端（`bot.py`）與全部測試都走後置指派（`reporter.sync_source =
  ...`，與 `reporter.watchdog` 同一個 pattern），那個 kwarg 沒有任何呼叫端 =
  兩個並存的注入方式，違反 dev-rules「兩個 pattern 互斥時選一個，不混用」。
  選刪 kwarg 而不是把 `DailyReporter` 的建構搬到 `SyncService` 之後：後者為了
  形式一致而新增一條硬性建構順序約束，收益為零。

以下修訂由 branch `feat/periodic-sync-task` 的 scoped re-review（修訂 10 的守衛
本身引入的 Critical）觸發。

**修訂 12（re-review Critical）—— `ACCOUNT_UPDATE` 的 `P` 是增量，不是全量快照。**

- 被撤回的內容：`bot._handle_account_update` 原本「先把**所有** symbol 的
  `unrealized_pnl` 歸零，再只還原 `P` 陣列裡有的那些」，以及 `sync_service.py`
  檔頭/`tests/test_periodic_sync.py` 據此寫下的「這次 ACCOUNT_UPDATE 動到哪些
  symbol 的答案是全部」。
- 為什麼撤回：Binance 的 `ACCOUNT_UPDATE` 只帶「本次事件有變動的」持倉；
  `FUNDING_FEE` 事件甚至完全沒有 `P`。把 `P` 以外的 symbol 歸零 = 憑空宣稱那些
  倉位的浮盈是 0。這是修訂 10 之前就存在的缺陷，當時被下一輪 REST 快照治好；
  **修訂 10 的 `ws_seq` 守衛把那次治療也丟棄了**（而且 handler 對每個 symbol
  都遞增 `ws_seq` ⇒ REST 的持倉對帳實質上是整帳戶被丟棄），於是缺陷從潛伏變成
  活的，後果鏈是：漏掉的 symbol 停在假的 `unrealized_pnl = 0` ⇒ 同一輪稍後
  `_sync_account()` → `risk_monitor.check_trailing_stop()` 看到
  `drawdown = peak - 0 >= max(2.0, peak * 0.10)` ⇒ `close_symbol_positions()`
  = **對健康倉位送出市價平倉單**。預設值（`risk.enabled=True`、
  `margin_threshold=0.5`、`trailing_start_profit=5.0 > trailing_min_drawdown=2.0`）
  讓它是活的。
- 新語意：以「本次事件出現過的 symbol」為集合。只有這個集合裡的 symbol 才遞增
  `ws_seq`（仍在任何寫入之前遞增，部分寫入也算髒）；`unrealized_pnl` 在同一事件
  內第一次碰到該 symbol 時**覆寫**、之後的同 symbol 條目才累加——同一 symbol 的
  LONG/SHORT 兩筆要相加，但跨事件無腦 `+=` 會讓浮盈無限膨脹。
- 已考慮並否決：在 `check_trailing_stop()` 端加「upnl 突然變 0 就 skip」的防禦。
  那是在症狀端補救，且需要一個沒有出處的啟發式門檻；根因修法更小也更正確。
- 連帶（依 dev-rules「找出所有依賴舊語意的地方」）：
  `tests/test_periodic_sync.py::test_position_snapshot_discard_is_per_symbol`
  原本斷言「沒出現在 `P` 裡的 SOL 也被丟棄」，那正是在釘死舊的錯誤語意——與它
  自己的 docstring（「丟棄粒度是單一 symbol」）矛盾。改為斷言 SOL 的 REST 快照
  照常寫入，並在測試內留下修訂理由。其餘 consumer 逐一確認過：`risk_monitor`
  讀 `SymbolState.unrealized_pnl`（正是受害者）、`ui.py` / `reporting.py` 只顯示、
  `sync_service._sync_account` 的 fallback 分支把它加總成帳戶浮盈——三者都只會
  因這次修正而變得更正確。

**修訂 13（re-review Ruling 11）—— per-symbol 連續丟棄計數。**

- 新增：`SNAPSHOT_DISCARD_WARN_THRESHOLD = 6`，`SyncService._discard_streak`
  以 `(kind, symbol)` 為 key（`kind ∈ {持倉, 掛單}`，兩道守衛分開計數）。
  同一 symbol 的快照被 `ws_seq` 守衛連續丟棄滿 N 輪就記一行 `logger.warning`，
  之後每再滿 N 輪印一次；成功套用一次即歸零。
- 為什麼需要：「快照永遠被丟棄」是修訂 10 引進的**新的靜默停擺**——該 symbol 的
  REST 對帳實質失效、狀態只剩 WS 維護，而 `sync_all()` 仍回 `True`、心跳照蓋、
  降級狀態機一次都不會被推進。狀態機與心跳都看不見它。
- N = 6（≈60s @ `sync_interval` 預設 10s）：與 `SYNC_FAILURE_THRESHOLD`(3) 同量級，
  取 2 倍是因為偽陽性成本不對稱——REST 失敗是異常，丟棄是設計中的正常結果
  （一次成交、一次資金費結算都會造成 1~2 輪丟棄）。
- 刻意**不**推降級計數（`_evaluate`）：那會在 WS 最活躍（= 最健康）時誤報降級
  並送 Telegram。

**修訂 14（re-review 文件義務）—— 帳戶層刻意不設防，理由入檔。**

- `_sync_account` 有與 `_sync_positions` 同型的 fetch→apply 競態
  （`AccountBalance.wallet_balance` / `unrealized_pnl`），**刻意不設守衛**。
  理由（re-review 追過所有 consumer）：這兩個欄位的讀者只有 `ui.py`、
  `reporting.py`、`notifier.py`，全部只是顯示；會下單/做風控判斷的
  `risk_monitor`（讀 `state.margin_usage` 與 **`SymbolState`**.unrealized_pnl）、
  `order_executor`、`decision` 都不讀帳戶餘額；且下一輪自癒，沒有 symbol 層那種
  「錯一次就一路錯下去」的分岔。
- 寫進 `sync_service.py` 檔頭的不變式段落，附**失效條件**：哪天有會下單或會做
  風控判斷的路徑開始讀 `AccountBalance`，這個裁定即刻失效，必須補上同型守衛。

**修訂 15（re-review Critical，同族第三個變形）—— 浮盈改成分側存放，
`SymbolState.unrealized_pnl` 由兩側導出。**

- 被撤回的內容：修訂 12 的「`unrealized_pnl` 在同一事件內第一次碰到該 symbol
  時**覆寫**、之後的同 symbol 條目才累加」。
- 為什麼撤回：那條語意的前提是「一次 `ACCOUNT_UPDATE` 會帶齊這個 symbol 的兩
  側」。Binance 在 symbol 層明說「only symbols of changed positions will be
  pushed」，但**沒有**保證兩側都會帶；唯一確證的單筆情境是 isolated 倉的資金費
  結算只推發生資金費的那一筆倉位。本 repo 從不設定 margin type（`grep
  marginType/set_margin_mode` 無命中），跟隨帳戶設定 ⇒ 帳戶若是 isolated 這條路
  就是活的。而 `unrealized_pnl` 是 **symbol 層的合計**、`long_position` /
  `short_position` 是**分側**的——用「本次事件出現的側」重算合計，等於宣稱沒帶
  到的那一側浮盈是 0。本 repo 強制避險模式（`bot._check_hedge_mode`
  `dualSidePosition=true`），兩側同時有倉是常態，所以這條路很寬。
  後果鏈與修訂 12 逐字相同：LONG +7.0 / SHORT -0.1（合計 6.9、trailing 中、
  peak 6.9），下一個事件只帶 SHORT（up=-0.2）且落在 `fetch_positions` 窗口內
  ⇒ 合計掉到 -0.2、REST 快照被修訂 10 的 `ws_seq` 守衛丟棄治不回來 ⇒
  `drawdown = 6.9 - (-0.2) = 7.1 >= max(2.0, 0.69)` ⇒ `close_symbol_positions()`
  = **對健康倉位送出市價平倉單**（re-review 已在 sandbox 端到端重現）。
- 新語意：`SymbolState` 新增 `long_upnl` / `short_upnl` 兩個分側欄位，
  `unrealized_pnl` 改成**唯讀 property** = `long_upnl + short_upnl`（對所有讀者
  語意不變，仍是「這個 symbol 的浮盈合計」）。`_handle_account_update` 依 `ps`
  各寫各的側，不再有「本次事件看到哪些側」的概念（修訂 12 引入的 `seen` 集合
  一併移除）；`_sync_positions` 的 `agg` 本來就分側，改成一併帶回兩側浮盈。
  「只帶一側」與「同事件帶兩側」自此都自動正確。
- 為什麼合計唯讀（而不是每次寫入時同步更新一個可寫欄位）：這一族缺陷已經出現
  三次，每次的形狀都是「有人拿手上有的那部分資訊去重算合計」。只要合計可寫，
  下一個變形就還有入口；唯讀讓那種寫法在執行時直接炸，不會靜默寫錯。代價是
  少數測試改用 `long_upnl` 指派——已全數更新。
- `ps='BOTH'`（單向持倉模式的淨倉）：本 repo 啟動時強制避險模式，收到 `BOTH`
  代表那個前提在運行中破了。處理方式是把淨倉映射到對應側（`pa >= 0` → LONG、
  `pa < 0` → SHORT）、另一側清乾淨，並記一行 `logger.warning`。靜默忽略最糟
  ——兩側會停在舊快照上被風控當成真值；只清不寫也不行——合計會憑空消失、觸發
  假回撤。未知的 `ps` 則完全不套用（猜側會蓋掉另一側的真值），同樣留 warning。
- 連帶：修訂 13 的節流告警補一條「每 N 輪一行」的語意測試——原測試只驗
  「N-1 輪不印、第 N 輪印」，把 `streak % N == 0` 突變成 `streak >= N`
  （之後每輪都印、實盤洗版）仍然全綠。
