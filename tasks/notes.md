# 2026-08-24 價格時效守衛：review verdict 與 findings 計數（dev-rules 要求留痕）

**最終 verdict：`Ship as-is`**（最終 whole-branch 外部輪 opus + scoped re-review 確認）。
測試 **756 passed / 2 skipped**（worktree 基線 713/2，淨增 43）；15 commits；
**尚未 merge、尚未重啟 ⇒ 尚未在生產生效**。

## 各輪 findings 計數（C=blocker / B=should-fix / A=clean）

| 輪次 | 對象 | 結果 |
|---|---|---|
| task review ×7 | Task 1-7 各一輪 | T1 A、T2 A、T3 C0/B2、T4 C0/B1、T5 C0/B1、T6 A、T7 A |
| security-review (opus) | 整條 branch | **1 High + 1 Low** |
| verifier (opus, fresh) | 整條 branch | ACCEPT WITH FINDINGS（9 條自選 mutation 全紅） |
| 最終外部輪 (opus) | 整條 branch 15 commits | **Ship with follow-ups**：C0 / B0 / 4 Minor |
| scoped re-review ×4 | 各 fix wave | 全部 all-addressed、零新增破壞 |

## 這次最重要的三個發現（why 見 tasks/progress.md 對應段）

1. **守衛本身可以變成新的風險來源。** 原設計把 `risk_monitor.check_and_reduce_positions`
   關在 gate 後面，等於在「ticker 斷線但 userData 活著、掛單持續成交、雙邊持倉往上爬」
   這個最需要風控的情境下關掉風控。通則：**判斷某一格該不該被 gate 擋，準則是
   「消不消費 price」，不是「它在函式裡的位置」。**

2. **可注入的全域時鐘在同行程多執行緒下是陷阱。** live bot 是 TUI 同行程的 daemon thread，
   而 backtester 會把 `clock.now()` 換成歷史 epoch ⇒ 邊實盤邊點回測會讓守衛全面停單
   （含成交後止盈補單）。改動前同樣的替換只造成 ATR/funding 偏移，**是這次把軟偏移
   變成硬停機**。修法：守衛專用的 `clock.guard_now()`——它量的是「訊息抵達的牆鐘時間」，
   與情境時鐘是不同的物理量，混用是分類錯誤。

3. **比本守衛更該修的既有問題（backlog 最高優先）**：`sync_service.maybe_sync()`
   **只**從 `_handle_ticker` 呼叫，無週期性 sync task ⇒ bookTicker 一斷，
   REST 持倉同步／移動停損／保證金告警／訂單對帳**本來就全停**。
   在該 failure mode 下本守衛根本不是瓶頸。應做 bookTicker liveness watchdog
   或週期性 `maybe_sync` task。

## backlog（本次產生，皆未做）

- bookTicker liveness watchdog／週期性 `maybe_sync`（見上，優先度最高）
- TUI 加 `max_price_age_sec` 編輯入口（目前逃生門得手改 config JSON + 重啟）
- `clock._now_fn` 改 thread-local／contextvar（可順帶修掉既有 ATR/funding 偏移，
  但改動 `clock.py` 對所有消費端的語意，本次刻意不做）
- 一週後看 `logs/decisions.jsonl` 的 `quote_age` 分佈再定 5 秒門檻
  （樣本稀疏：`_log_decision` 只在 requote 時刻寫）
- `_get_stale_quote_summary` 的 per-symbol `symbols` 明細目前未被渲染（delete-or-render）
- 既有 4 個測試以 5 秒門檻蓋章，對真實牆鐘有新耦合（慢機/斷點會冒無關的紅）

# Notes

## 2026-08-25 價格時效守衛：三條值得留給未來的結論

（branch `feat/price-staleness-guard`，worktree `as-grid-dragon-staleness`，
尚未 merge、尚未重啟——狀態細節見 `tasks/progress.md`「Current Task」。）

### 1. 判斷「該不該被 gate 擋」的準則：消不消費 price，不是它寫在哪個位置

T3 review（opus）發現原設計漏掉 `risk_monitor.check_and_reduce_positions`——
它被放在 `_grid_step` 裡 gate 之後的區塊，直覺上「看起來」該一起被擋，但它完全
不讀 `best_bid` / `best_ask`，下的是市價單（price 參數字面 `0`）。關在守衛後面的
實際後果：在「ticker 斷線、userData 仍活著、掛單持續成交、雙邊持倉往上爬」這個
**最需要風控的情境**下，60 秒冷卻的緊急減倉整個斷線期間不會觸發——守衛反而
關掉了風控。

**為什麼會漏**：計畫作者當時是用「這段程式碼物理上在 `_grid_step` 的哪個位置」
來判斷該不該包進 `if age <= max_age:`，而不是回頭問「它到底有沒有用到那個
可能過期的價格」。這是一個容易重演的思考捷徑——「靠近下單邏輯的東西都該被
擋」聽起來合理，但守衛的契約是保護「用不可信的價格做決策」，不是保護
「所有跟下單同一個函式裡的東西」。

**下次判斷同類問題直接用這條**：先問「這段程式碼讀了 `price` 嗎？」，
答案是否定的話就不該被 gate 擋，就算它物理上寫在 gate 區塊裡面。
已寫死進 spec §4.3.1，避免下次又要重新推導一次。

### 2. 5 秒門檻是猜測值，靠 decision log 的 `quote_age` 一週後收緊——來龍去脈

spec 一開始就承認 `max_price_age_sec` 預設 5 秒沒有實測依據（`logs/decisions.jsonl`
在守衛上線前完全不知道「快照有多舊」這件事，因為 `SymbolState` 原本沒有時間欄位）。
T6 的動機不是「順手加個欄位」，是這條門檻**目前唯一的驗證手段就是等真實流量跑過**：
T6 把 `quote_age`（實測秒數）從 gate 判斷邏輯裡 hoist 出來、無論守衛開關與否都寫進
`_log_decision`，讓正常運作時「快照通常有多舊」有真實分佈可查。
**下一步的具體動作**：branch merge 且引擎重啟跑滿一週後，讀
`logs/decisions.jsonl` 的 `quote_age` 分佈，用實測數字判斷 5 秒是過寬還是過窄，
再調整 `max_price_age_sec`——不要憑感覺調，這正是這條欄位存在的理由。

### 3. 實作期間發現：計畫自己寫的兩個測試是假綠/空斷言，且都只有外部 review 抓到

**#1（T3）**：計畫給的測試 helper `_seed_fresh_quote` 直接呼叫真的
`_handle_ticker`，而 `_handle_ticker` 尾端會呼叫 `adjust_grid` ⇒ 播種階段就已經
下過單、寫入 `bot.last_order_times`。開倉路徑的 10 秒冷卻與 `_grid_cooldown_passed`
用的都是**真實牆鐘 `time.time()`**，測試裡的 `fake_clock` 完全推不動它。連鎖後果：
`test_fresh_quote_places_orders` 因冷卻未過而 `await_count == 0`，斷言 `> 0`
直接失敗（假紅）；`test_stale_quote_places_no_orders` 則是**因為冷卻、不是因為
守衛**而通過（假綠，mutation 也殺不掉）。第一版藥方（只在
`_prime_for_ordering` 加 `bot.last_order_times.clear()`）也不夠——計畫的呼叫順序是
`_prime_for_ordering()` → `_seed_fresh_quote()` → `adjust_grid()`，清除發生在
播種**之前**，播種又會把 dict 重新填滿。正確修法是兩處一起改：
`_seed_fresh_quote` 播種期間用 `try/finally` mock 掉 `bot.adjust_grid`
（`quote_at` 在 `adjust_grid` 被 await **之前**就蓋好，mock 不影響蓋章），
`_prime_for_ordering` 仍保留清空 `last_order_times`（處理同一測試內呼叫兩次
`adjust_grid` 的案例）。

**#2（T5）**：計畫寫的斷言是 `assert isinstance(line, str)`——回傳一行真的告警文字
或回傳空字串都會通過型別檢查，測不出 spec 要求的訊號（「型別錯可以不帶數字，
但『有過期』這件事不能沒有訊號」）。日後如果有人把 except 分支改成
`return ""`（spec 明文禁止的行為），這個測試會照樣綠燈，且原本跑的三條 mutation
都殺不到這個缺口。修法：改成釘住訊號內容本身
（`assert "價格快照過期" in line` + `assert line != ""`），並補一條新 mutation
（把 except 分支改成 `return ""`）證明新斷言真的有牙齒——實跑轉紅：
`AssertionError: assert '價格快照過期' in ''`。

**為什麼只有外部 review 抓得到**：#1 是 controller 在派工前的 pre-flight scan
階段，人工追值域邏輯（冷卻用的是哪個時鐘、fake_clock 蓋不蓋得到）才抓到的，
發生在任何 implementer 動手之前。#2 是外部 task reviewer（sonnet）在 review
階段抓到的——implementer 當下是照計畫文本原樣轉錄程式碼，沒有自己發現斷言太弱。
兩者共通點：**implementer 自己不會抓到，因為它是照著計畫（一份帶著錯誤前提的
文件）在做事**，測試看起來「符合計畫描述」就會被判定完成。這正是 dev-rules
講的「內部 reviewer 會繼承作者框架含錯誤前提，外部 reviewer 只看 code 實際
做什麼」——這裡的「作者」指計畫本身，不是 implementer。

## 2026-08-24 TUI 孤兒 bot：`self.bot = None` 是放棄控制權，不是重置 UI 狀態

**起點**：查 08-14 那個「行程沒重啟卻跑策略初始化」的形態時撞到的（形態本身無害，見上一條）。

**缺陷**（`as_terminal_max.py`，三個，同一個根因）
1. `start_trading()` 等 `bot.state.running` ~20 秒逾時 → `self.bot = None`，但 thread 還活著。
   而 `bot.py` 的 `state.running = True` 設在 `_init_exchange` / `_check_hedge_mode` /
   `acquire_listen_key` **之後** ⇒ 逾時只代表初始化慢，那個 bot 接下來一定會開始掛單。
   參照一丟，`stop_trading` 與 `_handle_shutdown` 都認不得它（兩者都以 `self.bot` 為入口守衛）
   ⇒ **孤兒 bot：會下單、永遠停不掉、Ctrl+C 也不 graceful stop**。
   **觸發面不是理論值**：`08:38:05`~`08:39:11` 的 log 有整整一分鐘 DNS 失敗
   （`nodename nor servname provided`），足以吃掉那 20 秒。
2. `stop_trading()` 的 `join(timeout=5)` 後不看 `is_alive()` ⇒ 舊 bot 還在送單時就清參照，
   使用者可以立刻再啟動第二個 ⇒ **同一帳戶兩個 bot 同時撤單/掛單**。
3. （L5 檢查抓到）`main_menu` 的 `valid_choices` 只在 `_trading_active` 為真時才加 `"s"`
   ⇒ 孤兒狀態下**使用者按不到停止**。只修 1、2 等於修了一條使用者走不到的路徑。

**修法**：唯一真相來源改成 `bot_thread.is_alive()`，`_trading_active` 降級成純顯示旗標。
- `_bot_alive()`：是否還有活著的 bot thread。
- `_release_bot_if_dead()`：**thread 確認已死才放參照**，回傳是否放掉。
- `_push_config_to_bot()`：設定即時套用的守衛改看 `self.bot`（verifier 帶出的六處，
  孤兒狀態下原本會靜默不套用——使用者看到「已保存」卻沒套用，bot 仍拿舊 config 下單）。
- `start_trading` 入口守衛改看 `_bot_alive()`；**thread 已死則先清殘留參照**
  （否則 bot 自己初始化失敗後 `run_bot_thread` 的 finally 只清 `_trading_active`、
  `self.bot` 留著 ⇒ 永遠無法再啟動。這是修這題最容易自己開的新洞）。
- `main_menu` 的 `stoppable = _trading_active or _bot_alive()` 貫穿顯示 / `valid_choices` /
  `choice == "s"` / `choice == "0"` 退出前停止；另加孤兒橫幅（**刻意不讀 `bot.state`**，
  孤兒可能還在初始化，運行時間/浮盈都還沒意義）。

**刻意偏離**：對話裡說停止逾時要發 Telegram 告警，實作沒做——卡住的正是 `bot_loop`，
從那條路徑發告警會跟著卡死。改成 `logger.error` + console 提示可重試。

**驗證**：TDD 先紅（19 條裡 6 條實作前紅）、11 條自跑 mutation + verifier 兩輪自選 5+N 條、
6 條 monkey test（join 前後翻面的 race、`is_alive()` 恆真的卡死 bot、bot 有但 thread 沒有、
bot 沒有但 thread 活著的歷史遺留孤兒）。

**教訓已寫進 lessons 通則 8（假字串斷言）與通則 9（旗標語意變更是全檔級改動）。**

**verifier 三輪**：
- R1 `ACCEPT WITH FINDINGS` → 帶出六處「設定即時套用」仍綁 `_trading_active`
  （實作者實際找到 **6** 處，比 R1 列的 5 處多一個：檔尾 config 重新載入那處）。全修，
  抽出 `_push_config_to_bot()`。
- R2 `ACCEPT WITH FINDINGS` → 一條 mutation **存活**（`manage_symbols` 橫幅守衛零覆蓋）
  + `toggle_symbol:995` 的重啟提示同型未修。兩條全修，補 4 條測試，
  **重跑 R2 那條存活 mutation 現在會紅**。
- R3 **`ACCEPT`**：4 條 mutation 全紅（第一條就是 R2 那條存活的）、4 條新測試逐條做過
  假斷言審查（含確認 `toggle_symbol` 測試沒走 early return——先斷言 `cfg.enabled is False`
  證明真的執行到被測分支）、`_trading_active` 全檔 21 個讀取點掃過確認無誤用。
- 累計 mutation：實作者 14 條 + verifier 三輪自選 ≥12 條，除 R2 那條當場存活外全紅。
  **714 passed / 1 skipped**（基線 685，+29）。


## 2026-08-24 「行程沒重啟卻跑了策略初始化」形態——不是 bug，但旁邊有一個真的

progress.md 從 2026-08-14 掛著的未解形態（`21:21:10` 出現 `[MAX] 初始化完成` +
`Task was destroyed but it is pending!`，但 pid 75367 從 08-12 一路活著沒重啟）**已解**。

**答案**：`[MAX] 初始化完成` 印在 `grid_engine/bot.py:151`，是 `MaxGridBot.__init__` 的最後一行，
而 `MaxGridBot` 唯一的建構點是 `as_terminal_max.py:1236` 的 `TUI.start_trading()`。
⇒ 這行是**「按下啟動交易」一次印一次，不是「行程啟動」一次印一次**。
行程活著、從 TUI 選單停止交易再啟動交易，就會出現這個形態。`Task was destroyed but it is
pending!` 是舊 bot 的 event loop 在 `run_bot_thread` 的 `finally` 裡 `close()` 時，
還有 pending task 沒收乾淨的標準噪音。**形態正確，無需再查。**

今日（08-24）同型可交叉驗證：`09:38:14` 印了一次 `初始化完成`，但目前行程 pid 15765 是
`09:38:45` 才起的（`ps -o lstart`），`09:40:03` 又印一次 ⇒ 08:xx 那次屬於上一個行程，
兩者對得上。

**但查的過程撞到一個真的（新發現，未修，非本次範圍）**：
`as_terminal_max.py:stop_trading()` 的收尾是

    self.bot_thread.join(timeout=5)
    self._trading_active = False
    self.bot = None

**join 逾時後沒有任何檢查**：不看 `is_alive()`、不 log、不擋。逾時的情況下舊 bot thread
還活著（它的 loop 還在跑、`gateway` 還在送單），但 `_trading_active` 已被清成 False，
`self.bot` 參照也被丟掉 ⇒ 使用者可以立刻再按「啟動交易」，`start_trading()` 的
`if self._trading_active` 守衛形同虛設，**同一個帳戶上會有兩個 MaxGridBot 同時撤單/掛單**。

`bot.stop()`（`grid_engine/bot.py`）本身寫得對（set stop_event → cancel 全部 task →
`gateway.shutdown()`），5 秒正常情況夠用；問題純粹在**逾時路徑無聲**。
風險等級：需要「停止交易在 5 秒內沒收完」+「使用者馬上再按啟動」兩件事同時發生，
但代價是真錢上的雙 bot 競爭下單。**屬 Plan track（動的是金流路徑），待使用者裁決。**


## 2026-08-24 非 GCE 的 TODO 清空：摘要欄位統一 coerce + 0a 收尾 + spec 時間表回寫

**1. `notify_daily_pnl` 入口統一 coerce（`grid_engine/notifier.py`）**
接 2026-08-16 verifier 帶出的同型缺口。決策是照當時 notes 寫的方向做「入口統一 coerce」，
不是一個欄位補一次 try/except——理由是硬性要求（「每日摘要不得發不出去」）的邊界在
**訊息組裝**這一層，逐欄位補會隨新欄位再破一次。
- `_coerce_num()`：`float()` + `except Exception`（`__float__` 可拋任意例外）+ **NaN/inf
  也視同無效**（它們不拋例外，但會把 `+nan` / `inf` 印進使用者的摘要）→ fallback `0.0`。
- `_escape()`：`parse_mode="HTML"`，標的名稱含 `<` 會讓 Telegram 回 **400**，等於整封摘要
  掉光。這是「型別對但內容有敵意」的那一半，coerce 管不到。
- `MAX_POSITION_LINES = 20`：Telegram 單則 4096 字元。持倉數量爆掉時截斷並附「另有 N 個
  標的未列」，優於整封發不出去。（現況只有 1 個標的，這條是預防性的。）
- **8 條 mutation 逐條實跑轉紅**（拿掉純量 coerce / positions isinstance / `_escape` /
  行數上限 / fallback `0`→`999` / NaN-inf 檢查 / `pnl_data` 非 dict 守衛 / 倉位 coerce）。
- 唯一行為變更：倉位數量改用 `:g` ⇒ `3.0` 印成 `L:3`（原本 `L:3.0`）。

**2. 0a 最後一格（20:00 摘要是否帶 ⛔ 那行）—— 改用實跑釘死，不靠人工看 Telegram**
log 只能證到「當時在 `given_up`」（`2026-08-16 19:30:09` / `20:30:09` 兩筆節流提醒夾住 20:00）
與「摘要寄出去了」（全 log 零 `Telegram 發送失敗` / 零 `每日摘要發送失敗`）。
**「⛔ 那行有沒有被組進訊息本體」人工不可判定**——摘要照樣會寄出，只是少一行，看起來一切正常。
⇒ 新增 `tests/test_reporting_watchdog.py::TestDailyReporterEndToEndWiring`：走真的
`DailyReporter.run()`（patch 掉 `asyncio.sleep`）+ 真的 `TelegramNotifier`（只 mock `send`），
斷言訊息含 `⛔ userData 監控：已放棄自動重連，需人工介入`。
mutation：`reporting.py` 的 `pnl_data` 拿掉 `"watchdog"` key → 轉紅。
**教訓型的一句**：接線類驗收若只靠「使用者說有收到通知」，斷的是**內容**而不是**通道**時，
人眼永遠不會紅。這種格子要用 end-to-end 測試關，不要留給下一次活體驗收。

**3. spec 時間表回寫**（`docs/superpowers/specs/2026-08-15-userdata-watchdog-design.md`）
§7.3 的「75 分鐘進 `given_up`」劃線標錯，§8.2 新增第 4 項放實測時間軸（118 分鐘）與公式
`max(退避, 600s 靜默) + 等下一次 requote`。**寫死的結論：`given_up` 的抵達時間是 requote
事件的函數，不是時間的函數，安靜市況下無上界。** 驗收準則裡「次數」（2 封 Telegram、
3 次重連）可判定，「時間」不可判定。

全套：**679 passed / 1 skipped**（基線 671，+8）。fast-track，終點 verifier。

**4. verifier(opus) verdict：`ACCEPT WITH FINDINGS`**（fresh context、6 條自選 mutation 全紅、
實跑複核 679/1、`git diff --stat` 證明工作區還原）。兩條 findings **當場補掉，沒進 backlog**：

- **F1 訊息總長度沒有整體上限**（`MAX_POSITION_LINES` 只管持倉行數；極端大浮點或超長標的名
  單靠 1-2 個欄位就能推過 4096）。→ `TelegramNotifier.send()` 入口加 `_truncate()`：
  超過 `TELEGRAM_MAX_CHARS = 4096` 才動作，且**截斷版一律把 HTML 標籤整個拿掉**——
  切一半的 `<b` 與開了沒關的 `<b>` 兩種 Telegram 都回 400「can't parse entities」，
  單純切片等於白截。**送達 > 排版。** 這一層守的是所有通知，不只每日摘要。
- **F2 `reporting.py` 的 positions 組裝段沒有等效守衛**（`_get_watchdog_status` 有，它沒有）。
  關鍵在 `run()` 的外層 `except` 是 `sleep(60)` 後回迴圈頂端重算 `target`，而那時今天的整點
  已過 ⇒ target 直接 +1 天 ⇒ **當天摘要靜默漏送一整天，不是延遲補送**。
  → 抽出 `DailyReporter._collect_positions()`：容器層一個 try（讀不到 symbols 就當無持倉）、
  per-symbol 一個 try（壞掉的標的只讓自己消失）。
- 兩條各補測試 + **5 條 mutation 逐條實跑轉紅**（拿掉 send 截斷 / 截斷但不拿標籤 /
  短訊息也被動到 / 拿掉 per-symbol try / 拿掉 symbols 容器 try）。
- 最終全套：**685 passed / 1 skipped**（基線 671，+14）。
- **scoped 第二輪 verifier(opus)：`ACCEPT`**（4 條自選 mutation 全紅，含「切片邊界不扣
  suffix 長度」與「先切片再去標籤」——後者證明 `_truncate` 裡**去標籤必須在切片之前**，
  順序反了會殘留半個 `</b`）。回歸：`_truncate` 對 `notify_crash`/`notify_start`/
  `notify_risk_alert` 無影響（長度遠低於 4096）；`_collect_positions` 正常路徑逐行等價。
- verdict 計數：**verifier 兩輪（R1 ACCEPT WITH FINDINGS → 修 → R2 ACCEPT）**，fast-track
  免 dual-review 兩輪。


## 2026-08-16 使用者裁決：userData 根因調查停止

**裁決**：不開新 API key、不再往下查根因（TODO 0c 關閉）。

**這代表接受的穩態**：userData stream 永久死著，watchdog 負責讓「它死了」這件事被看見
（判死告警 + 3 次重連 + `given_up` + 每日摘要狀態行），成交統計由 `sync_service` 的
REST 增量拉取維持真值。**面板與 Telegram 的累計已實現數字因此是對的**，這條已解。

**代價（必須記住，日後會咬人）**：
1. `bandit` / `dgt` / `leading_indicator` 的 `record_trade` 唯一餵食來源就是死掉的 userData
   handler。三者生產上皆 `enabled: false` 故現在無實害，但**日後開回任何一個，它會拿到
   全零歷史而且不會警告** —— 這現在是**永久狀態**，不是「等根因修好就沒事」。
   要開之前必須先把 `record_trade` 改接 REST 那條路徑。
2. 成交的即時反應延遲由 WS 推送退化成 REST 輪詢週期（10s）。
3. 若 Binance 端哪天自己好了，`record_event()` 會發「✅ 已恢復推送」並把狀態機復位 ——
   不需要人工動作，但也沒人在等它。

## 2026-08-16 TODO 0b（M1）修復完成 + verifier 帶出的同型缺口

**改動**（fast-track，兩檔）：`grid_engine/notifier.py` `_format_watchdog_line` 的 `given_up`
分支對 `silence_seconds` / `attempts` 做 `float()` / `int()` 轉換並各自包 `try/except Exception`
（fallback `0.0` / `0`）。「需人工介入」那句留在 try 之外 ⇒ 型別錯時只掉數字、不掉訊號。
`tests/test_notifier.py` 新增一條 monkey test（字串／None／list／`__float__` 拋 `KeyError` 的物件）。

**為什麼是 `except Exception` 而不是 `(TypeError, ValueError)`**：這裡的硬性要求是「摘要在
任何情況下都不得發不出去」，而 `__float__` 可以拋任意例外（lessons 通則 3 有同型前例）。
與 `reporting.py:_get_watchdog_status()` 的既有 pattern 一致。

**verdict**：verifier(opus) **ACCEPT WITH FINDINGS**。5 條自選 mutation 殺 4 存 1——存活的是
「fallback 常數 `0`/`0` 改成 `999`」（測試只斷言「需人工介入」在，不看數字）。**已補**：
測試改成逐案斷言字面值（`"0 次"` / `"0 分鐘"`，可轉換的 `"7200"` 仍要算出 `"120 分鐘"`），
實跑該 mutation 現在紅在 `tests/test_notifier.py:455`。全套 **671 passed / 1 skipped**（基線 670 +1）。

**verifier 帶出、本次未修的同型缺口（backlog，非本 TODO 範圍）**
- `notifier.py:142-146`：`total_pnl` / `total_equity` / `margin_usage` / `total_profit` /
  `running_hours` 一樣是 `.get()` 取值後直接 `:+.2f` / `:.1%` 格式化，**無型別守衛**。
- `notifier.py:130-134`：持倉 dict 的 `long` / `short` / `pnl` 非數值時，迴圈內就會炸。
- 兩者都會讓當天摘要發不出去（`reporting.py` 的 `run()` catch-all 只讓它「這輪跳過」）。
  ⇒ 若要把「摘要不得發不出去」做成真正的硬保證，該在 `notify_daily_pnl` 入口統一 coerce，
  而不是一個欄位補一次。**現況：watchdog 那條已守，其餘欄位未守。**

## 2026-08-15 userData 死因調查續：listenKey 輪換假設也被否決，改為工程止血

**結論先講**：根因**仍未確定**，而且很可能在 Binance 端。剩下唯一可測的假設是
「這把 API key 本身在交易所端壞掉」——需要在後台開一把新 key 重測（**未做，掛帳**）。
其餘假設全部被實驗否決。

**08-15 新拿到的證據（依取得順序）**

1. **`LIST_SUBSCRIPTIONS` 顯示 listenKey 確實被登記成有效訂閱。**
   ⇒ 推翻 `ws_client.py:44-47` 註解裡「Binance 對無效 stream name 靜默接受」那個心智模型。
   它不是被靜默丟掉，是**登記了但不推**。這一格是 08-14 那輪沒拿到的。
2. **`ipRestrict: true` 確認，但當下 IP 通過白名單**（REST 全通、listenKey 也是同一 IP POST 的）
   ⇒ IP 假設大幅弱化。
3. **舊 log 裡找到死亡的那一刻**：`log/as_terminal_max.log.archive-20260712` 有 **58,408 筆**
   `[userData]`（`開倉成交` 39,776 筆等）⇒ 這條路徑歷史上完全正常。最後一筆真事件在該檔
   第 1,057,232 行（全檔 2,338,647 行），緊接著就是
   `WebSocket 錯誤: no close frame received or sent` → `已訂閱 userData stream`（重連）
   → **之後 128 萬行 + 新 log 47,168 行，零筆**。**一次斷線重連之後永久死亡。**
4. **`POST /fapi/v1/listenKey` 在 key 仍有效時只回同一把舊 key。** 實測 14:54 / 15:05 / 16:04
   三次 POST 都是 `hC98My4OnDww…FzqgAh`。
   ⇒ 這解釋了為什麼 `6a264d6`（重連時重新 POST）修不好：重新 POST 拿回的是同一把。
   新 log 裡 `已訂閱 userData stream` 出現 **90 次**，重連重取路徑早就走過幾十遍。
   （progress.md 舊記的「這條路徑尚未走到」已過期。）
5. **輪換假設被決定性否決**：`DELETE` → `POST` 確實換出全新 key（做了三次），
   `LIST_SUBSCRIPTIONS` 確認訂上，窗口內交易所端有 **4 筆真實訂單事件**
   （`allOrders` 交叉驗證，17:35:31-32），同連線 bookTicker **2,853 筆** ⇒ **userData 0 筆**。

**方法論教訓（比結論更值錢，這次踩了三次）**

- **窗口內沒有真事件的觀察，證明不了任何事。** 前兩輪探針各作廢一次：一輪窗口內
  `allOrders = 0`（引擎 13 分鐘沒 requote），一輪人工製造的事件落在窗口關閉之後。
  **每次 userData 觀察都必須附「同窗 REST 交叉驗證」，且要在報告裡印出同窗事件數。**
- **探針自己的失效模式必須被儀器化。** v1 探針的 WS task 拋例外後靜默死亡而 heartbeat 照跑，
  導致「bookTicker 凍在 231」被誤讀成資料停了，其實是 task 已經不在了。
  v2 起每條連線都有 reconnect 迴圈 + 例外落 log + 連線代數（gen）計數。
  這與 08-14 漏 keepalive 是同一類錯：**觀測工具沒有自我監控 = 觀測結果不可信。**

**已排除的完整清單**：訂閱方式（A/B 雙路）、stream name 被靜默丟棄（LIST_SUBSCRIPTIONS）、
listenKey 過期／keepalive 沒跑、socket 健康度（bookTicker 對照）、listenKey 卡在伺服器端
壞狀態（三次輪換）、Portfolio Margin、multi-assets、API 權限、IP 白名單（大幅弱化）。

**下一步（掛帳，未做）**：Binance 後台開一把全新 API key（Enable Reading + Enable Futures，
白名單加當前 IP），用同一套探針重測。新 key 有事件 ⇒ 根因是舊 key，生產輪換 key 即可收工；
新 key 也沒有 ⇒ 帳號層級，只能開客服單。

**因此本次的工程對策不是修根因，而是止血**：見
`docs/superpowers/specs/2026-08-15-userdata-watchdog-design.md`。
設計刻意**不假設根因可修**——偵測 + 有限復原 + 讓成交統計改由 REST 取得而不再依賴 userData。

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

---

## 價格時效守衛：security review + verifier findings 修復（2026-08-25）

### 1. 守衛不能共用 `clock.now()`（High，本輪最重要）

live bot 跑在 TUI 同一個行程的 daemon thread（`as_terminal_max.py:1265`），而同一個
TUI 主執行緒提供「執行回測 / 參數優化」選單。`backtest/backtester.py:715` 每根 K 線
`clock.set_clock(lambda: epoch)` 替換模組級全域 `_now_fn`。守衛若用 `clock.now()`
量快照年齡，「一邊實盤一邊點回測」就會讓 `quote_age` 變成巨大負數 ⇒ `age < 0`
對每個 symbol 每個 tick 觸發 ⇒ **全面停止下單（含成交後的止盈補單），持倉繼續累積**，
唯一訊號是每 symbol 每小時一筆 throttled warning。

守衛上線**之前**，同樣的時鐘替換只讓 ATR/funding 計時器軟偏移，不會停止下單——
是本次改動把「軟偏移」變成「硬停機」，屬本 branch 引入的回歸。

**why 這樣修**：守衛量的是「訊息實際抵達本機的**牆鐘**時間」，`now()` 是可被回測
換掉的**情境時鐘**。兩個不同的物理量，原設計混為一談是分類錯誤；分開不是繞路，
是修正分類。`clock.py` 增設 `guard_now()`/`set_guard_clock()`/`reset_guard_clock()`，
`now()` 家族語意與既有呼叫端全部不動。守衛三處（蓋章、比較、節流）必須一致。

**通則**：時間量測要先問「這是情境時間還是牆鐘時間」，再決定用哪個時鐘。凡是量
「外部訊息什麼時候到我這裡」的，一律牆鐘，不可注入。

### 2. 每日摘要不得宣稱日粒度

`stale_quote_counts` 全 repo 無重置點，文案卻寫「今日 N 次」⇒ 跑 30 天會每天報同一個
越滾越大的數字。改成「累計 N 次（自啟動）」。**刻意不做 snapshot-diff 造日增量**：
這套引擎重啟頻繁，reporter 自造的「今日」會隨重啟歸零，比誠實累計更誤導。
措辭誠實 > 假裝有日粒度。

### 3. `max_price_age_sec = 0` 不等於「完全回到改動前」

準確語意：**關閉守衛 = 不再擋單；風控上移（§4.3.1）與 `quote_age` 量測仍然生效，
兩者皆不消費快照價格**。spec §5 已修訂。另：該逃生門**目前沒有 UI 入口**
（`as_terminal_max.py` 與 `web/` 皆無），要動得手改 `config/*.json` 並重啟——
新增 UI 入口列 backlog。

### 4. 寫死一句避免誤讀（重要）

**「每日摘要看到『無此行』不等於『價格是新鮮的』——偵測 feed 整條斷掉是 watchdog
的職責，不是本守衛的。」**

守衛只在 `adjust_grid` 被呼叫時判定，而 `adjust_grid` 的兩個入口是 bookTicker 與
userData。bookTicker 全斷時根本沒人呼叫 `_grid_step` ⇒ 計數不會動；userData 自
2026-07-12 死著 ⇒ 第二條路也不觸發。最嚴重的形態恰恰是這個計數看不見的形態。
那一行量的是「有人來敲門、但手上的價格太舊」。
