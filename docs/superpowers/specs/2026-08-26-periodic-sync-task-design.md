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
        self._evaluate(await self.sync_all())        # 修訂：不再經過 maybe_sync
    except asyncio.CancelledError:
        break
    except Exception as e:
        logger.error(f"[sync] 週期同步失敗: {e}")
        self._evaluate(None, loop_error=True)
        if not slept:                                # 修訂：I3 的忙迴圈防禦
            try:
                await asyncio.sleep(MIN_SYNC_INTERVAL)
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
- `_loop_interval()`：`config.sync_interval` 非數／NaN／`<= 0` → 用 **1.0 秒**
  fallback 並記一次 warning（**2026-08-26 修訂**：它必須是 total function，
  `except Exception`——`self.config` 為 None 時的 `AttributeError` 會讓
  `await asyncio.sleep(self._loop_interval())` 整句沒被執行，見 §10 的 I3）。
  不夾的話 `sleep(0)` 會變成忙迴圈打爆 REST 配額；夾到下限而不是 fallback 預設值，
  是因為使用者刻意調小是合法意圖，只有非法值才需要糾正。

### 5.6 每日摘要多一行

`notifier.py` 加 `_format_sync_line(sync)`，與既有 `_format_watchdog_line` /
`_format_stale_quote_line` 同一 pattern，接進 `notify_daily_pnl`：

**2026-08-26 修訂**：加一段**心跳**，優先於下列所有分支：
`last_sync_age`（= `guard_now() - last_sync_time`，由 `_get_sync_status` 一併帶出，
formatter 是 staticmethod、不得讀全域狀態，故門檻用的 `sync_interval` 也一起帶）
超過 `max(60, 6 * sync_interval)` ⇒ 無條件印停擺警告；`None`（從未同步過）與
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

**未修訂、明確留到 backlog（Ruling 6）**：`_sync_trade_stats()` 的節流仍用
`clock.now()`（情境時鐘）而非 `guard_now()`。既有問題、非本 branch 引入，修它
要動 `tests/test_trade_stats_sync.py` 的 frozen_clock fixture。
