# Progress

## Current Task（2026-08-27 更新）：週期性 REST 同步 task（B1-A）—— **已 merge、已重啟、上線生效**

### ✅ 狀態：`Ship as-is` + 已 merge（`81e7d87`，no-ff）+ **已重啟生效**

- merge commit `81e7d87`（base `6852f7e`，branch `feat/periodic-sync-task` 17 commits）。
  merged 結果上重跑全套：**851 passed / 2 skipped**（跑在 `git archive` 快照裡，不寫 `config/`、`log/`）。
- **尚未 push 到 origin**；branch 與 worktree `../as-grid-dragon-periodic-sync` 都還留著（未清）。
- ✅ **已重啟、已生效（2026-08-27 12:00，使用者手動重啟）**：
  `grid_engine/{sync_service,bot,state}.py` mtime `11:59:45` → 新 pid 24966/24967 起於
  `11:59:53`（晚 8 秒）→ log `12:00:13` 有整段初始化（Bandit 冷啟動／LeadingIndicator／
  MAX 初始化），`12:00:17` 重新訂閱 userData ⇒ 新行程 import 的是新碼。
  `bot.py` 內只剩 `:864` 啟動時的 `sync_once()`，`_handle_ticker` 的同步呼叫已不存在。
- SDD ledger 在 `.superpowers/sdd/2026-08-26-periodic-sync-task/progress.md`（gitignored）。
- verdict 與各輪 findings 計數已落 `tasks/notes.md` 最上方那則。

### ✅ mutation M2-M5 已補完（2026-08-27，解掉三位 verifier 的死結）

三位 verifier 都死在同一個陷阱：**`cd` 出 worktree 會毒化被隔離 session 的 shell**（與 `/tmp`
拼法無關）。主 session 不受該隔離，改用乾淨快照跑，worktree 全程未動：

```
git -C <worktree> archive HEAD | tar -x -C <scratchpad>/mut
cd <scratchpad>/mut && PYTHONPATH=<scratchpad>/mut \
  <LouisLab>/.venv/bin/python -m pytest tests/... -q -p no:cacheprovider
```

（`uv run pytest` 在 sandbox 內不可用——uv project root 是父目錄 `LouisLab/`。）

| Mutation | 結果 |
|---|---|
| M2 `sync_service.py:511` `ws_seq` 比對 → `if False` | KILLED，紅在 `test_periodic_sync.py:510` |
| M3 `state.py:74` 給 `unrealized_pnl` 加 setter | KILLED，紅在 `:887` DID NOT RAISE |
| M4 `bot.py:711` 兩側歸零再寫本次側 | KILLED，4 條紅（含端到端偽市價平倉重現） |
| M5 `sync_service.py:355` 刪掉 sleep 後停機守衛 | KILLED，紅在 `test_stop_set_during_sleep_skips_that_round` |

還原後快照全套 **851 passed / 2 skipped**，與 branch 宣稱一致（獨立複驗，非採信自述）。

### 下一步（依序）

1. ~~重啟引擎~~ ✅ 已完成並驗證（見上）
2. 決定要不要 `git push origin main`（目前 main 領先 origin）
3. 清理：`git worktree remove ../as-grid-dragon-periodic-sync` + `git branch -d feat/periodic-sync-task`
   （worktree 內有 gitignored 的 `.superpowers/sdd/` ledger，刪掉就沒了——結論已在 `tasks/notes.md`）
4. backlog 見下方「其他待辦 6.」

### 驗收現況

| 關卡 | 結果 |
|---|---|
| SDD 7 個 task | 全部 complete（T3/T7 各進一次 fix loop，其餘一次過） |
| 最終 whole-branch review（opus） | Ship with follow-ups：C0 / B4 / M5 → 全數修完，re-review 13/13 ADDRESSED |
| security-review | **零 findings**（≥8 信心） |
| dual-review Round 1（外部輪，opus，不給 spec／不給前面結論） | **Fix required**：C1 + B4 + M5 → 修完 re-review 12/12 ADDRESSED |
| dual-review Round 2（專案規則） | 已查：無新依賴（uv）、monkey testing 8 條、只 stage 指定檔、無 CLAUDE 相關檔入 git |
| verifier #1 | REJECT（工具環境崩潰）——讀得到的部分全 PASS |
| verifier #2 | ACCEPT WITH FINDINGS：read-back 4 項全 PASS、M1 實跑 KILLED；mutation 只完成 1/5 |
| verifier #3 | REJECT（BLOCKED）：0/4，只做到靜態 read-back |

**dual-review 尚未產出 `Ship as-is` verdict ⇒ 依 dev-rules，本任務不得標記完成。**

### 這條 branch 做了什麼

把 `SyncService.maybe_sync()` 從 `_handle_ticker` 移到常駐背景 task 驅動，並讓失敗會說話：
1. `sync_all()` 回傳 `SyncOutcome`（五子項逐項成敗 + `skipped`；`critical_ok` = 持倉 and 帳戶）
2. 節流計時改 `clock.guard_now()`（牆鐘，backtester 換不掉）
3. `_evaluate()` 告警狀態機：關鍵項連續失敗 3 次發一封 Telegram、降級中不重發、恢復發一封
4. `run()`/`stop()`/`_loop_interval()` 常駐 loop（後改走 `sync_once()`，`maybe_sync()` 已刪）
5. **移除 `bot.py:625`** —— 唯一的行為切換點
6. 每日摘要多一行降級狀態 + `last_sync_age` 心跳
7. Monkey testing 8 條 + 驗收準則逐條對帳

### review 過程翻出來的四條真缺陷（都不在原計畫裡）

1. **C1 競態（外部輪抓到，前面所有輪都漏）**：改動前 `sync_all()` 在 `_handle_ticker` 內被 await，
   而 `ws_client.py:97` 是 inline `await handler(data)` ⇒ REST round-trip 期間沒有任何 WS handler 能跑。
   搬到獨立 task 後這個天然序列化消失：`_handle_account_update`（**完全無鎖**寫持倉）會落在 fetch→apply
   窗口裡，接著 REST 拿到 symbol lock 把過期快照寫回去 ⇒ `_grid_step` 用 `long_position == 0` 分岔
   ⇒ 撤掉剛掛好的網格再開一次倉。修法：`SymbolState.ws_seq` per-symbol 版本號，REST 在 fetch 前抓、
   apply 時在鎖內比對，變了就丟棄該 symbol 的快照。
   ⚠️ **最終 whole-branch review 查過同一個不變式並判「撐得住」**——它看的是 apply block 有拿鎖、鎖序單向，
   但鎖只保護 apply 的那一瞬間，不保護 fetch→apply 窗口，而 WS handler 根本不拿鎖。
2. **C1 的修法本身引入新 Critical**：`_handle_account_update` 把**所有** symbol 的 upnl 歸零、只還原 `P` 裡有的；
   本來由下一輪 REST 治好的「被 `P` 漏掉的 symbol」現在被 C1 守衛擋掉 ⇒ 停在假的 upnl=0 ⇒ 同一輪
   `check_trailing_stop` 看到 `drawdown = peak - 0` ⇒ **對健康倉位送市價平倉單**。
   修根因：`ACCOUNT_UPDATE` 的 `P` 是增量、不是全量快照，不得對沒帶到的 symbol 歸零。
3. **同族第三個變形**：`unrealized_pnl` 是 symbol 層合計但 `long/short_position` 是分側的 ⇒ 事件只帶一側時
   另一側的浮盈被抹掉（Binance 文件確證的活路徑：isolated 倉的資金費結算只推發生資金費的那一筆）。
   結構解：`long_upnl`/`short_upnl` 分側存放，合計改成**唯讀 property** ⇒ 在型別層關掉
   「合計被局部資訊重算」這個 pattern。re-reviewer 盤了全 repo 5 處合計欄位，確認這一族在會下單的路徑上關掉了。
4. **REST 那側分側寫入零覆蓋**：對全套 850 條跑兩個 mutation（long/short 寫反、刪掉 `short_upnl` 寫入）
   **兩個都活著**——因為那兩條測試的 fixture 只有單邊持倉、且只斷言合計（合計對「寫反」不敏感）。已補。

### 下次開工的其他待辦（M2-M5 之後）

1. 整合 dual-review 兩輪 → 產出最終 verdict（要 `Ship as-is` 才算完成）
2. verdict + 各輪 findings 計數落 `tasks/notes.md`（dev-rules 要求）
3. 把本段搬進主目錄的 `tasks/progress.md`
4. `superpowers:finishing-a-development-branch` 決定 merge 方式
5. **merge 後必須重啟引擎才生效**。確認方式：`ps -o lstart= -p $(pgrep -f as_terminal_max | head -1)`
   晚於 `ls -lT grid_engine/sync_service.py` 的寫入時刻，並在 log 看到新行程的初始化區塊。
6. backlog（本次產生，皆未做）：
   - `_handle_order_update` 對 `ps='BOTH'` 兩支都不命中 ⇒ 掛單計數不重置也沒 warning，與 account handler 不對稱（pre-existing）
   - `bot.py:749-752` 帳戶層浮盈用 config 內 symbol 加總覆寫交易所真值（顯示用，spec §10 修訂 14 已裁定可接受）
   - 測試收集非決定性：同一 suite 同機第一次跑出 64 個 `partially initialized module 'pandas'` collection error、後兩次全綠
   - `tests/test_periodic_sync_monkey.py:101` docstring 的行號指向 `bot.py:788`，實際是 `:864` 且已改名 `sync_once()`
   - 未能驗證：`web/`、`backtest/` 是否有殘留的 `SymbolState.unrealized_pnl` 寫入端（唯讀 property 命中會是 runtime AttributeError）

---

## 先前狀態（2026-08-25 18:0x）：價格時效守衛已 merge、已 push、**確認生產跑的是新碼**

### ✅ 狀態：上線已生效

- `main == origin/main` @ `ea09993`（+ docs commit `4eb7bf6`）。
- **重啟疑點已解**（本次以 reflog + 檔案 mtime 對時）：
  - `git reflog --date=iso` → fast-forward merge 落在 `2026-08-25 17:22:59`，
    `.git/ORIG_HEAD` 與 `grid_engine/{bot,config,notifier}.py` 的 mtime 同為 **17:22:59**
    （checkout 寫檔時刻，這才是「新碼落地」的可查時間，不是 merge commit）。
  - `ps -o lstart=` → 引擎 pid 67584/67585 啟動於 **17:23:00**，晚於檔案寫入 1 秒。
  - log 佐證：`log/as_terminal_max.log` 17:23:04 有整段初始化（Bandit 冷啟動／
    LeadingIndicator／MAX 初始化），17:23:09 重新訂閱 userData ⇒ 確實是新行程 import 的新碼。
  - `config/trading_config_max.json` 仍停在 07-26、無 `max_price_age_sec`——**這只代表
    config 自那時起沒被存過，不是守衛沒生效**，別再拿它當判準。
- feature branch 與 worktree 已刪除；SDD review 產物依使用者指示清除，結論在 `tasks/notes.md`。
- 工作區只剩既有的 ` M .gitignore`（與本次無關）。
- 順帶觀察：17:33:09 watchdog 判定 userData 靜默 604s 並強制重連（第 1/3 次）——
  即 TODO 0a 的活體驗收仍在跑，重連後 17:33:18 已重新訂閱成功。

### 歷史（保留追溯）

- Work 在 git worktree `as-grid-dragon-staleness`，branch `feat/price-staleness-guard`
  （base `12cdb89`）。**主目錄 `../as-grid-dragon` 全程未動**，生產引擎跑的仍是
  merge 前的舊碼——這份改動**還沒 merge 進 main，也還沒讓引擎重啟過**，
  不生效。下次開工的第一件事是決定 merge 時機與重啟排程，不是預設它已上線。
- branch 上 **10 個 commits**（`12cdb89..8cbd74e`），diff vs main：**11 檔、692 增、9 刪**。
- 最終全套：**743 passed / 2 skipped**。worktree 基線 **713 passed / 2 skipped**
  ⇒ 淨增 **30 個測試**。
  - ⚠️ 計畫文件寫的基線是「主工作目錄」的 714 passed / 1 skipped，**不是這個 worktree
    的數字**——worktree 沒有 gitignore 的 `config/`，`tests/web/test_config_store.py:117`
    多一個 skip（`no real config`），另有一個既有 pass 因此少掉。本報告一律以
    worktree 713/2 為基準，避免日後對不上。
  - Task 7 本身（本次）只加註解，跑完仍是 743 passed / 2 skipped，未變動。

### 這 8 個 task 做了什麼

1. **T1**（`02c7d9f`）：`SymbolState` 加 `quote_at` 時戳欄位，`_handle_ticker` 與
   bid/ask 同一個 block 蓋章。
2. **T2**（`85a2ffa`）：`GlobalConfig.max_price_age_sec`，`0` 是關閉守衛的逃生門。
3. **T3**（`85a2ffa..eab6ee3`，含 fix round）：`_grid_step` 加時效 gate + 節流告警
   `_note_stale_quote`。**這一步發現了整份設計最重要的一個錯誤，見下方「設計錯誤」**。
4. **T4**（`68af178..6da4d9f`）：既有測試補 `quote_at` 蓋章（gate 上線的連帶紅修正，
   `git diff -- grid_engine/` 全程為空，只動測試）。
5. **T5**（`6da4d9f..caeec78`）：每日摘要帶「價格快照過期」計數（0 次不出這行）。
6. **T6**（`caeec78..8cbd74e`）：decision log 加 `quote_age` 欄位，讓 5 秒門檻
   日後能用實測收緊（見下方「待辦」）。
7. **T7**（本次）：`backtest/tick_sim.py` 加註解說明「回測逐 tick 餵資料，快照年齡
   恆為 0，gate 恆通過，是刻意差異不是 bug」；本完成報告。**只加註解，不改邏輯**——
   `git diff --stat -- backtest/tick_sim.py` = `1 file changed, 5 insertions(+)`，
   無刪除、無其他檔案。
8. **T8**：尚未執行（本次任務只到 T7；merge/收尾流程留給下一步）。

### 🔴 設計錯誤（實作期間發現，已修，已修訂 spec 留痕）

T3 review（opus）發現：原設計檢查「gate 之後還有什麼吃 price」時，只找到 DGT 與
bandit 兩個消費端，**漏了 `risk_monitor.check_and_reduce_positions`**。
實查 `risk_monitor.py:66-107`：這個函式**完全不消費價格**——判斷只用兩個持倉量與
`position_threshold`，下的是 price 參數字面為 `0` 的市價單。把它關在守衛後面，
等於在「ticker 斷線、userData 仍活著、掛單持續成交、雙邊持倉往上爬」這個
**最需要風控的情境**下關掉風控（60 秒冷卻的減倉整個斷線期間不觸發）。

**已修**：`check_and_reduce_positions` 上移到 gate 之前（commit `eab6ee3`）。
**已修訂 spec** `docs/superpowers/specs/2026-08-24-price-staleness-guard-design.md`
§4.3.1 留痕。

**通則（下次判斷同類問題時直接用，不要重新推導）**：
判斷某一格該不該被時效 gate 擋住，準則是**它消不消費 `price`**，
不是它在 `_grid_step` 裡的**位置**。不消費價格的副作用（例如市價風控平倉）
必須留在 gate 之前，就算它目前寫在 gate 之後的程式碼區塊裡。

### ⚠️ 觸發面校準（照抄 spec，不得美化）

> 生產上 userData stream 自 2026-07-12 起是死的 ⇒ `_handle_order_update` 那條路徑
> 目前幾乎不觸發。本守衛守的是兩種形態：(1) userData 復活之後；
> (2) bookTicker 單邊卡住但 userData 活著。**log 裡沒有這兩種形態的實證**，
> 優先度排序是推測性的。

**這份改動不得被描述成「修掉了一個已觀測到的生產事故」**——它防的是還沒被
log 證實發生過的兩種形態，是預防性工程，不是事後修復。

### 待辦：5 秒門檻的收緊

`max_price_age_sec` 預設 5 秒是猜測值。T6 已讓每筆 decision log 的紀錄帶
`quote_age`（實測值，秒）。做法：**至少累積一週真實分佈**後，用實測數字判斷
5 秒是過寬還是過窄，再調整 config（不要憑感覺改）。

### Review 戰績（各輪 findings 計數）

| Task | Reviewer | Verdict | Critical | Important | Minor |
|---|---|---|---|---|---|
| T1 | sonnet | Approved | 0 | 0 | 2（deferred） |
| T2 | sonnet | Approved | 0 | 0 | 1（deferred，行為正確，非缺陷） |
| T3 | opus | Approved（fix round 1 後） | 0 | 2（已修） | 3（2 併入本輪修、1 deferred） |
| T4 | sonnet | Approved（fix round 1 後） | 0 | 1（已修） | 0 |
| T5 | sonnet | Needs fixes → Approved（fix round 1 後） | 0 | 1（已修，見下方假綠） | 1（deferred） |
| T6 | sonnet | Approved | 0 | 0 | 1（deferred） |

### Deferred minors（待 triage，共 5 條，尚未修）

1. T1：`test_price_staleness_guard.py` module docstring 承載整體設計論述，對單檔略顯外溢。
2. T1：只有單次 ticker 的案例，缺「第二次 ticker 覆蓋 `quote_at`」的測試。
3. T3：`_last_stale_log_at` 在 symbol 恢復後未清除 ⇒ 恢復後 20 分鐘內再次過期不會
   log（1 小時節流窗口內），只有計數會動。每日摘要是這個情境唯一看得到的表面。
4. T5：`_get_stale_quote_summary` 算出並傳遞 `symbols` 明細，但
   `_format_stale_quote_line` 目前只讀 `total` ⇒ `symbols` 是未使用資料
   （符合 brief 指定的介面形狀，非偏離）。
5. T6：`_log_decision` docstring 帶了設計論述（「5 秒門檻是猜測值…」），
   對一個機械式 I/O helper 略顯外溢；無害，符合該檔既有習慣。

### 實作期間發現：計畫本身寫錯的測試（假綠 / 空斷言）

至少兩處是**計畫文件自己寫錯**，不是實作者手滑，細節見 `tasks/notes.md`
2026-08-25 條：
1. **T3**：計畫給的測試 helper `_seed_fresh_quote` 會經過真的
   `_handle_ticker`→`adjust_grid`，播種時就已下單並污染 `last_order_times`；
   而下單冷卻用的是真牆鐘，`fake_clock` 推不動它 ⇒ 一個測試假紅、一個測試
   會**因為冷卻而不是因為守衛**通過（假綠，mutation 殺不掉）。
2. **T5**：計畫寫的斷言是 `assert isinstance(line, str)`——回傳告警行或空字串
   都會通過，測不出「有過期不能不出現字」這個 spec 要求的訊號。

兩者的抓法不同：#1 是 controller 在 pre-flight scan 階段人工追值域邏輯抓到，
先於任何實作/review；#2 是外部 task reviewer（sonnet）在 review 階段抓到，
implementer 當下是照計畫轉錄程式碼、沒有自己發現。

### Mutation 實跑摘要（各 task 報告有完整清單，這裡只列代表性的）

- T3：opus reviewer 把 gate 整段刪掉重跑 → 18 個測試中 6 個轉紅，且每個都是
  「真的下了單」（如 `assert 2 == 0`）而非被冷卻擋下 ⇒ 確認無假綠。
- T3 fix round：`test_stale_quote_still_runs_risk_reduction` 先紅
  （把 risk_monitor 呼叫搬回 gate 下游 → `assert 0 == 1`）。
- T5 fix round：把 `_format_stale_quote_line` 的 except 分支改成 `return ""`
  （spec 明文禁止的行為）→ 新斷言轉紅：`AssertionError: assert '價格快照過期' in ''`。
- T6：`if max_age > 0:` 外層守衛拿掉、`quote_age` hoist 出 `if` 後的「守衛關閉
  仍記錄」測試 → `assert 0.0 == 999.0`（999.0 是實測值，非注入常數）。

---

## 先前狀態（2026-08-24 12:10）

### 🟢 狀態：本次任務全部完成並已推送；**生產引擎已跑新碼**

- `main == origin/main`（`f7ace40`，本次推 8 commits，含前幾次 session 留著沒推的 3 個）。
- 工作區只剩 ` M .gitignore`（既有，與近期任務無關，使用者未指示處理）。
- **引擎 2026-08-24 `12:06:06` 由使用者重啟**（pid 46150/46151），跑的就是 `f7ace40`
  （commit 於 `11:52:12`，工作區除 `.gitignore` 外 clean）⇒ **本次兩批修復已在真錢上生效**。
  重啟後 `12:06:15` 已訂閱 userData stream、`12:06:16-17` 掛滿 4 張單。
  注意 watchdog 狀態機**已隨重啟復位成 `healthy`**，會重新走一次判死流程
  （前一個行程在 `12:01:11` 還是 `given_up`）。

### 🔴 下次開工必做（依序）

1. **今晚 20:00 每日摘要活體驗收 —— 這是新碼的第一次真實發送。**
   要確認三件事：(a) 使用者實際收到；(b) 帶 watchdog 狀態行（重啟後可能是
   `⚠️ 重連中` 或 `⛔ 需人工介入`，**不會是 08-16 那種一定 given_up**，看 20:00 當下
   狀態機走到哪）；(c) 數字欄位正常（新的 `_coerce_num` 路徑第一次上線）。
   失敗徵兆：log 出現 `每日摘要發送失敗` 或 `Telegram 發送失敗`（目前全 log 自
   `2026-07-12` 起零筆）。
2. **backlog 四項擇一**（全部是 Plan track，動之前要使用者裁決）——見下方 backlog 段。
   使用者 2026-08-24 已在四選一裡挑了 TUI 孤兒 bot（已完成），其餘三項＋GCE 未動。
3. **`tasks/lessons.md` 已 99 行**（遠超 workflow 規則的 ~50 行門檻，加通則 8/9 之前
   就 89 行）。整併是獨立維護任務，**已詢問使用者、尚未裁決**。

### ⛔ Blockers

無。（GCE 固定 IP 是使用者本次明確排除，不是 blocker。）

---

### ✅ 本次（2026-08-24）：非 GCE 的 TODO 全部清完

使用者指示「todo 繼續，除了 GCE 有關，其他清完」。剩下的三項狀態：

1. **0a 最後一格（20:00 摘要帶 ⛔ 那行）→ 關閉**。
   - log 證據：`2026-08-16 19:30:09` 與 `20:30:09` 兩筆 `仍處於 given_up` 節流提醒
     夾住 20:00 ⇒ **當時狀態確實是 `given_up`**；全 log 自 `2026-07-12` 起零
     `Telegram 發送失敗` / 零 `每日摘要發送失敗` ⇒ 那封確實寄出去了。
   - 「⛔ 那行有沒有真的被組進訊息本體」人工看 Telegram 不可判定，改用實跑釘死：
     `tests/test_reporting_watchdog.py::TestDailyReporterEndToEndWiring` 走真的
     `DailyReporter.run()` + 真的 `TelegramNotifier`（只 mock `send`），斷言訊息含
     `⛔ userData 監控：已放棄自動重連，需人工介入` 與 `3 次` / `120 分鐘`。
     mutation 實測：`reporting.py` 的 `pnl_data` 拿掉 `"watchdog"` key → 該測試轉紅。
2. **spec §8.2 時間表回寫 → 完成**（見下）。
3. **`notify_daily_pnl` 其餘欄位型別守衛 → 完成**（見下）。

**未動（使用者指示排除）**：TODO 6 GCE 固定 IP。
**已由使用者裁決關閉**：0c userData 根因調查。

### ✅ 本次追加：TUI 孤兒 bot（使用者選 A：bounded，TDD → verifier）

清完 TODO 後查 08-14 那個「行程沒重啟卻跑策略初始化」的形態（結論：**不是 bug**，
`[MAX] 初始化完成` 是按一次「啟動交易」印一次），**過程中撞到真問題**：

- `start_trading()` 逾時 → `self.bot = None` 但 thread 還活著 ⇒ **孤兒 bot：會掛單、
  永遠停不掉、Ctrl+C 也不 graceful stop**。觸發面不是理論值——今天 `08:38`~`08:39`
  有整整一分鐘 DNS 失敗，足以吃掉那 20 秒等待。
- `stop_trading()` 的 `join(timeout=5)` 後不看 `is_alive()` ⇒ 兩個 bot 同時撤單/掛單。
- （L5 檢查抓到）`main_menu` 的 `valid_choices` 也 gate 在 `_trading_active` 上
  ⇒ 孤兒狀態下**使用者按不到停止**，只修前兩條等於修了一條走不到的路徑。
- （verifier 帶出）六處「設定即時套用」同樣 gate 在它上面 ⇒ 靜默不套用。

修法：唯一真相來源改成 `bot_thread.is_alive()`，`_trading_active` 降級成純顯示旗標；
新增 `_bot_alive()` / `_release_bot_if_dead()` / `_push_config_to_bot()`。
**714 passed / 1 skipped**（基線 685，+29）。詳見 `tasks/notes.md` 2026-08-24 條。
教訓 → lessons 通則 8（假字串斷言）、通則 9（旗標語意變更是全檔級改動）。

#### 3-1 `notify_daily_pnl` 入口統一 coerce（`grid_engine/notifier.py`）
verifier 在 0b 帶出的同型缺口：watchdog 那行守住了，其餘欄位沒守。這次照 notes 的
建議「在入口統一 coerce，而不是一個欄位補一次」：
- 新增 `TelegramNotifier._coerce_num()`（`float()` + `except Exception` + NaN/inf 一律
  視同無效 → fallback `0.0`）、`_escape()`（`& < >`，`parse_mode=HTML` 下標的名稱含
  `<` 會讓 Telegram 回 400 ⇒ 整封摘要掉）、`MAX_POSITION_LINES = 20`（單則 4096 字元上限）。
- `total_pnl` / `total_equity` / `margin_usage` / `total_profit` / `running_hours` 全走
  `_coerce_num`；`positions` 非 dict 直接當空；倉位的 `long` / `short` / `pnl` 同樣 coerce；
  `pnl_data` 整包不是 dict 也不炸。
- **8 條 mutation 全殺**（拿掉純量 coerce / positions 守衛 / `_escape` / 行數上限 /
  fallback 0→999 / NaN-inf 檢查 / `pnl_data` 守衛 / 倉位 coerce），逐條實跑轉紅。
- **679 passed / 1 skipped**（基線 671，+8 新測試）。
- 行為變更一處：倉位數量格式從 `f"L:{pos['long']}"` 改成 `:g` ⇒ `3.0` 現在印 `L:3`
  而不是 `L:3.0`（`0.5` 仍是 `L:0.5`）。純顯示，無斷言依賴。

#### 3-3 verifier findings 當場補掉（不進 backlog）
verifier(opus) **ACCEPT WITH FINDINGS**（6 條自選 mutation 全紅、實跑複核 679/1）。
兩條 findings 已修：
- **F1**：`send()` 入口加 `_truncate()`（`TELEGRAM_MAX_CHARS = 4096`），截斷版**把 HTML
  標籤整個拿掉**——切一半的 `<b` 與沒關的 `<b>` Telegram 都回 400，單純切片等於白截。
  守的是所有通知，不只每日摘要。
- **F2**：抽出 `DailyReporter._collect_positions()`，容器層 + per-symbol 各一層 try。
  原本壞一個標的 → `run()` 外層 `except` sleep(60) 後 target 自動 +1 天 ⇒
  **當天摘要靜默漏送一整天**。
- 5 條 mutation 逐條實跑轉紅。**最終全套 685 passed / 1 skipped（基線 671，+14）。**
- **scoped 第二輪 verifier(opus)：`ACCEPT`**（4 條自選 mutation 全紅、回歸面確認）。

#### 3-2 spec 時間表回寫（`docs/superpowers/specs/2026-08-15-userdata-watchdog-design.md`）
§7.3 的「引擎啟動後 75 分鐘進 `given_up`」已標為**錯誤並劃線**，§8.2 新增第 4 項：
實測時間軸表 + 每階公式 `max(退避, 600s 靜默) + 等下一次 requote`。
**結論寫死在 spec 裡：`given_up` 的抵達時間是 requote 事件的函數，不是時間的函數，
安靜市況下無上界；驗收與告警文案都不得承諾任何固定分鐘數。**

---

### 🟢 0a watchdog 活體驗收（2026-08-16）：狀態機全程走完（引擎 `09:29:08` 重啟，跑 `f4bdd8a`）
**兩封 Telegram 使用者皆已確認收到**（09:39 的 ⚠️、11:27 的 ⛔）。

實測時間軸（`log/as_terminal_max.log`，非推論）：

| 時點 | 事件 | 證據 |
|---|---|---|
| 09:29:08 | 已訂閱 userData stream | 啟動撤 4 + 掛 4 = 8 張單 |
| **09:39:08** | **判死 + 強制重連 1/3** | `8 張單無推送、靜默 605s`；10 秒後重新訂閱 |
| 10:03:45 | requote（撤 4 + 掛 4） | 補滿新證據 |
| **10:04:08** | **重連 2/3** | `靜默 1500s` |
| 10:20:55 | requote | |
| **10:21:08** | **重連 3/3** | `靜默 1020s`；`next_attempt_at = 11:06:08` |
| 11:06:08 | 退避到期，但**沒進 given_up** | 10:21 後零 requote ⇒ `orders_since_event=0`、`_is_dead()` 不成立 |
| 11:26:11 | requote（65 分鐘的安靜期結束） | 補滿新證據 |
| **11:27:08** | **進 `given_up` + ⛔ Telegram 第 2 封** | `已重連 3 次仍無事件推送，停止自動復原` |

⇒ **整條路徑走完，t0+118 分鐘**（09:29:08 → 11:27:08）。之後零重連（終態只 log 不動作，
每 3600s 節流提醒一次），符合 spec §5.2。

- ✅ **證據重取路徑實測走通**：重連後 `orders_since_event` / `last_event_at` 歸零，一定要等
  下一次 requote 補滿 8 張單才會再判死 —— 這正是 dual-review B2 要的行為。
- ✅ log 零 `Telegram 發送失敗`（最後一筆 403 停在 2026-07-12），且**使用者已確認 09:39 那封
  「⚠️ userData stream 疑似靜默失效」實際收到** —— 端到端告警路徑活體驗證完成。
- 🔴 **時間表要回寫 spec §8.2**：實際每階耗時是 `max(退避, 600s 靜默) + 等下一次 requote`。
  spec 的 70 分鐘與「純退避累加 = 80 分」**都低估**，實測 **118 分**（1→2 花 25 分、2→3 花 17 分、
  3→given_up 花 66 分，其中 21 分純粹在等 requote）。
  **正確的描述是：`given_up` 的抵達時間不是時間函數，是 requote 事件函數，安靜市況下無上界。**
  今日 requote 間隔實測 25~80 分（03:50 / 05:12 / 06:13 / 07:13 / 07:38 / 09:29 / 10:03 / 10:20 / 11:26）。

### ✅ 本次完成：0b（M1 型別守衛）+ 0d（lessons 第三次整併）

- **0b**：`grid_engine/notifier.py:164-176` `given_up` 分支的 `silence_seconds`/`attempts`
  改成 `float()`/`int()` + 各自 `try/except Exception`（fallback `0.0`/`0`），「需人工介入」
  留在 try 之外。verifier(opus) **ACCEPT WITH FINDINGS**（5 條自選 mutation 殺 4 存 1，
  存活的「fallback 常數 → 999」已補逐案字面值斷言、實跑紅在 `tests/test_notifier.py:455`）。
  **671 passed / 1 skipped**。細節與 verifier 帶出的同型缺口見 `tasks/notes.md` 2026-08-16 條。
  ⚠️ 註：M1 原記在 `reporting.py`，實際位置是 `notifier.py`。
- **0d**：`tasks/lessons.md` **115 → 89 行**（通則 3 + 通則 6 合併成 13 條假守衛總表；
  三條已內化進 dev-rules 的降為一行錨點）。完整敘事搬 `tasks/lessons-archive.md`（不注入）。
  **仍超過 ~50 行門檻**，再砍就會刪掉還在咬人的判準 —— 待使用者裁決要不要更激進。

### ✅ 先前完成：userData watchdog 全線 merge 進 main
`main` = `69b5022`，13 commits（`caec67e..69b5022`，rebase 後 fast-forward）。
**main 上實跑 670 passed / 1 skipped**（分支起點 589/2 + 80 條新測試；worktree 少的那條
`test_config_store` 在有真實 `config/` 的 main 上會跑）。
（原記「尚未 push」，2026-08-16 查證**已推**，`origin/main` 現在是 `f4bdd8a`。）

- spec：`docs/superpowers/specs/2026-08-15-userdata-watchdog-design.md`（六處更正皆留痕）
- plan：`docs/superpowers/plans/2026-08-15-userdata-watchdog.md`（5 tasks）
- 做了三件事：**偵測**靜默失效並告警、**有限復原**（3 次，退避 300/900/2700 後進 `given_up`）、
  **成交統計改由 REST 增量拉取**（單一 writer，userData handler 停寫）。
- 新增 `grid_engine/userdata_watchdog.py`；改 `ws_client.py`（旗標式 `request_reconnect()`）、
  `order_executor.py`（餵張數）、`bot.py`（餵事件 + 接線）、`sync_service.py`（REST 統計）、
  `reporting.py`/`notifier.py`（每日摘要帶 watchdog 狀態）。
- review 全程：4 輪 per-task + whole-branch(opus) + verifier 兩輪(opus) + security-review(opus)
  + dual-review 外部輪(opus)/Round 2 + 最終 scoped re-review(opus) = **Ship as-is**。

**⚠️ 這是止血不是治本**：stream 仍然是死的。本次讓「它死了會被發現」且「數字是真的」。

### 下次開工必做（依序）

1. ~~**收 0a 最後一格** + 回寫 spec §8.2~~ —— **2026-08-24 完成，見本檔最上方。**
2. ~~**userData 根因**（開新 API key 重測）~~ —— **使用者 2026-08-16 裁決：不做。**
   ⇒ 根因調查到此打住，接受「stream 死著、watchdog 看著、成交統計走 REST」這個穩態。
   若日後要重啟調查：唯一剩下的可測假設仍是「這把 API key 在 Binance 端壞掉」，
   做法是後台開新 key（Enable Reading + Enable Futures + 白名單加當前 IP）用同一套探針重測；
   **已否決的假設完整清單見 `tasks/notes.md` 的 2026-08-15 條**（含 08-14 那輪）。
3. ~~`notify_daily_pnl` 其餘欄位（`total_pnl` / `total_equity` / 持倉 dict）同樣無型別守衛~~
   —— **2026-08-24 完成**（入口統一 coerce，8 條 mutation 全殺），見本檔最上方 3-1。

**⇒ 非 GCE 的 TODO 已全清。剩下的只有 TODO 6（GCE 固定 IP，使用者本次明確排除）
與 backlog。**

### 📋 backlog（**下次的候選清單就是這裡**，全部 Plan track，動之前要使用者裁決）

2026-08-24 使用者在四選一裡挑了「TUI 孤兒 bot」（已完成）。**剩下三項 + GCE：**

| 優先度（我的判斷） | 項目 | 為什麼 |
|---|---|---|
| 高 | `_handle_ticker` 價格時效守衛 | 唯一會直接讓**過期價進 `adjust_grid` 下單**的一項 |
| 中 | GCE 固定 IP（TODO 6） | `-2015 Invalid API-key, IP` 反覆發作時撤單/下單/同步全掛。使用者 2026-08-24 明確排除，需他改變心意才動 |
| 中 | watchdog 牆鐘 → `time.monotonic()` | 時鐘往後跳會讓 watchdog 靜默凍結（真失效時告警延遲數小時）——正是本 spec 要根除的「沒有儀器」重演 |
| 低 | `start_time_ms` 改用交易所時間 | 只影響 Telegram 日報的 `total_profit` 口徑，不影響下單 |

（原始描述保留於下）
- watchdog 用牆鐘量靜默時長與退避 → 應改 `time.monotonic()`（需另開注入點，不能共用現有 `clock`）。
- `start_time_ms` 用本機時鐘當交易所 `since` 起點 → 應改用交易所時間。
- `_handle_ticker` 無價格時效守衛（既有缺口，本次只用頁數上限縮小觸發面）。
- **`bandit` / `dgt` 的 `record_trade` 仍只由死掉的 userData 餵**，而它們回頭影響 `decide()`。
  三者生產上皆 `enabled: false` 故無實害，但**日後要開回任何一個，它們會拿到全零歷史且無警告**。
- 裝死模式（零新單）下狀態機會停在 `degraded` 永遠走不到 `given_up`（設計必然，
  可見性由每日摘要接住）。

---

## 先前狀態（2026-08-14）

### 🔴 userData stream 死因調查——**結論見 `tasks/notes.md`**
根因仍未確定且可能在 Binance 端。08-14/08-15 兩輪實驗已否決：訂閱方式（A/B 雙路）、
stream name 被靜默丟棄（`LIST_SUBSCRIPTIONS` 證明有登記）、listenKey 過期/keepalive、
socket 健康度、**listenKey 卡在伺服器端壞狀態（三次 DELETE+POST 輪換出全新 key 仍零推送）**、
Portfolio Margin、multi-assets、API 權限；IP 白名單大幅弱化（REST 從同一 IP 全通）。
08-15 另在舊 log 找到死亡時點：archive 有 58,408 筆 `[userData]`，最後一筆之後
`WebSocket 錯誤: no close frame` → 重連 → 永久零筆。且 `POST listenKey` 在 key 有效時
只回同一把舊 key ⇒ 這解釋了 `6a264d6` 為何修不好。

### 🔴 生產問題：`-2015 Invalid API-key, IP` 反覆發作（TODO 6 的真正代價）
出現過的 request ip 共 6 個：`223.140.219.162`(1739 筆, 07-18)、`111.241.136.139`(132, 08-10)、
`61.216.73.207`(104)、`36.225.34.156`(15)、`118.166.239.83`(12)、`36.225.15.37`(10, 08-14)。
被擋時撤單/下單/同步全掛。**⇒ TODO 6（GCE 固定 IP）優先度高。**

### ✅ ~~另一個未解形態~~ —— **2026-08-24 已解，不是 bug**
`[MAX] 初始化完成`（`bot.py:151`）印在 `MaxGridBot.__init__` 尾端，而唯一建構點是
`as_terminal_max.py:1236` 的 `TUI.start_trading()` ⇒ **按一次「啟動交易」印一次，
不是行程啟動印一次**。行程活著、從 TUI 停止再啟動交易就是這個形態。
`Task was destroyed` 是舊 loop `close()` 時的殘留 task 噪音。詳見 `tasks/notes.md` 2026-08-24 條。

### ✅ `assumed_leverage` 值域守衛（已 commit `3119e68`）
掛在 `SymbolConfig.__setattr__`（不是 `from_dict`）——dataclass `__init__` 也走 setattr ⇒
唯一涵蓋 from_dict / TUI / web 三個賦值點 / REPL 的咽喉點。非整數**拒絕不截斷**。

<details><summary>更早的 08-14 實驗細節（已被 08-15 結論取代）</summary>

### 原 08-14 記錄
2026-07-30 的 listenKey 修復（`6094f4c`）確實解掉了 `-1125`（最後一筆 08-06 19:10，之後 8 天零筆），
**但 `[userData]` 從 07-12 至今仍是 0 筆**。08-14 實驗證據鏈：

| 實驗 | 條件 | 結果 |
|---|---|---|
| A 路 | `wss://.../ws` + `SUBSCRIBE [listenKey]`（引擎現行作法） | **零事件** |
| B 路 | `wss://.../ws/<listenKey>`（Binance 文件作法） | **零事件** |
| 對照組 | 同一條連線同時訂 `bnbusdc@bookTicker` + listenKey | bookTicker **2360 筆**/5 分鐘，userData **0** |
| 交叉驗證 | REST `allOrders` 查同一時窗 | 22:36:50-51 有 **8 筆** CANCELED/NEW 真實存在 |

listenKey 是 22:32:34 新取的（<60 分鐘未過期，且腳本每 25 分鐘 PUT keepalive），
兩條連線 22:32:35 起全程連著，22:36:50 的 8 筆訂單事件**一筆都沒推過來**。
⇒ **socket 健康、key 有效、事件真實存在，就是不推。**

**因此：不要去改 `ws_client.py:64` 的訂閱方式**——「SUBSCRIBE vs path URL」這個假設已被 B 路實驗直接否決。
（腳本留在 session scratchpad：`ws_userdata_ab.py`、`ws_control.py`。round1 那份無效，
原因是我漏了 listenKey keepalive，60 分鐘後 key 就死了——重跑時務必帶 keepalive。）

**下一個候選假設（未驗證，標明為推測）**：API key `ipRestrict: true`，而家用浮動 IP 一直在換
（log 出現過 6 個不同 IP），實驗當下 22:32→22:40 之間 request ip 就從 `36.225.15.37` 變成 `118.150.131.186`。
若 Binance 在推送時也校驗來源 IP，症狀會**恰好**是這種靜默不推。已排除的：
Portfolio Margin（`enablePortfolioMarginTrading: false`）、multi-assets（`false`）、
權限（`enableReading`/`enableFutures` 皆 true）、socket 健康度（對照組已證）。

### 🔴 生產問題：`-2015 Invalid API-key, IP` 反覆發作（TODO 6 的真正代價）
出現過的 request ip 共 6 個：`223.140.219.162`(1739 筆, 07-18)、`111.241.136.139`(132, 08-10)、
`61.216.73.207`(104)、`36.225.34.156`(15)、`118.166.239.83`(12)、`36.225.15.37`(10, 08-14)。
被擋時撤單/下單/同步全掛，且 `POST listenKey` 失敗 → 引擎「沿用舊值」→ 舊 key 早被廢棄。
**⇒ TODO 6（GCE 固定 IP）優先度應升到僅次於 1b。** 待使用者裁決：關白名單 / 忍到 GCE / 手動補 IP。

### ⚠️ 另一個未解形態
`2026-08-14 21:21:10` log 出現 `[MAX] 初始化完成` + `Task was destroyed but it is pending!`，
但行程 pid 75367 從 08-12 一路活著沒重啟。**行程沒重啟卻跑了一次策略初始化**，形態不對，未查。

### ✅ 本次完成：`assumed_leverage` 值域守衛（TODO 4c 的一項，未 commit）
`grid_engine/config.py` 新增 `_norm_assumed_leverage`，掛在 **`SymbolConfig.__setattr__`**
（不是 `from_dict`）——dataclass `__init__` 也走 setattr ⇒ 那是唯一涵蓋 from_dict / TUI IntPrompt /
web 三個直接賦值點（`2_⚙️:194,306`、`3_🔬:214`）/ REPL 的咽喉點。非整數**拒絕不截斷**（20.9 不會變 20）。
**590 passed / 1 skipped**（基線 579，+11 新測試）。

**verifier 兩輪**：
- 第一輪 ACCEPT WITH FINDINGS：2 條存活 mutation（非整數截斷、legacy key 順序）+ 3 個 UI 繞過點。
  → 全修：守衛改掛 `__setattr__` 一次解掉 UI 繞過；非整數測資從 `5.7` 改成 `7.3`/`20.9`
  （`int(5.7)==5` 恰好等於 fallback ⇒ 原測試是**假守衛**）；補 legacy `leverage` 帶垃圾值。
- 第二輪 ACCEPT WITH FINDINGS：7 條 mutation 殺 6 存 1（`except Exception` 收窄回
  `except (TypeError, ValueError)` 全綠通過 = 防禦碼沒被測到）→ 已補 `__float__` 拋 `KeyError`
  的測試，該 mutation 現在會紅（實測 `1 failed, 10 passed`）。回歸面第二輪查過：roundtrip、
  型別依賴、`__init__` 順序、生產 config 載入後仍 int 5，全 PASS。

（該段當時記為「未 commit」，實際已於 `3119e68` commit。）

</details>

---

## 更早的狀態（2026-07-30）
**TODO 1c 全線完成並 merge + push（2026-07-30）。**
`main == origin/main`（`b7fd7de`，本次推 20 commits）。三條舊 branch 已刪（本地 + 遠端，全部 `--merged main` 確認過）。
工作區只剩 ` M .gitignore`（既有，與近期任務無關，使用者未指示處理）。

### 👉 下次開工建議：重評 TODO 1b —— **它的前提已經被 1c 改掉了**
1b 寫的是「**-68** uPnL 出場路線」，但 2026-07-30 實測：

| | 1b 寫成當下（07-11） | **現在（07-30 12:15）** |
|---|---|---|
| uPnL | -68 | **-33.4** |
| delta | +0.24 → 後來惡化到 +0.40 | **+0.24 且持續收斂中** |
| 強平價 | 412 → 90.8 → 288.98 | **96.49**（距現價 −83%）|
| 可用 | ~4 | **41.38** |

⇒ **1b 的四個選項要重新評估**：(a)「凍結等價回 690」的前提（倉位凍結）早就不成立；
(c)「入金補中性後長期持有」被健檢否決的理由（會被 `tp_quantity` 拆回去）**已經由 1c 修掉**，
但入金會讓 `risk_monitor` 從不可達變成活的生產碼（見 spec §6 的綁定條件）。
**建議先觀察 1-2 週讓新規則自己收斂，再決定要不要動 1b。**使用者 2026-07-30 的裁決正是
「先讓目前的倉位這樣慢慢調整，直到 hedge」。

### ~~🔴 開工前必做的一行驗證（listenKey 修復的最後一哩）~~ **2026-08-14 已執行，結論見最上方**
```bash
grep -c "\[userData\]" log/as_terminal_max.log      # 實測 0 —— 修復並未生效
grep -c "1125" log/as_terminal_max.log              # 149，但最後一筆停在 08-06，確實不再增長
```
⇒ `-1125` 修好了，**userData 仍然全死**。面板成交次數與 Telegram 日報的「累計已實現」
到現在都還不是真的。詳細實驗與已否決的假設見本檔最上方。

---

### 已完成的 1c（保留細節供追溯）
spec `docs/superpowers/specs/2026-07-26-hedge-immune-tp-design.md`（v3 + 2026-07-30 §5.3 事後修訂）、
plan `docs/superpowers/plans/2026-07-26-hedge-immune-tp.md`（8 tasks，全部執行完）。

### 🔴 這份改動已經在真錢上跑了（流程倒序，必須知道）
**引擎 2026-07-27 19:04 重啟，跑的就是 `27be2d4`**（08:57 commit，工作區 clean）。
也就是說 Task 1-5 的 code 先上線、Task 6/7/8 的驗收 3 天後（2026-07-30）才補跑。
**這是流程倒序，不是正常路徑**——驗收若當時 FAIL，要付的代價是回滾生產而不是不上線。
（實際結果：修訂判準後全項 PASS，見下。）dual-review / security-review / verifier **仍未跑**。

### 驗收數字（2026-07-30 實跑，全部落檔在 session scratchpad）
- **A7 全套測試：570 passed / 1 skipped**（基線 546 → +24 新測試）。py_compile 六檔 OK；
  `grep tp_quantity` 零 5 引數殘留；`bot.py` 加倍分支已刪只剩註解；`ui.py` 標籤與 decision 一致。
- **A4 replay 全量**（`a4_replay_result*.txt`）：99,552 筆中 12,349 筆有 diff、9 筆違規 → 全部
  ≤`07-09 19:12`，形態一致（`long.cancel_side` replay=True/logged=False + long orders 空），
  是 `60917cc` 修掉的**舊 code 產物**，與 1c 無關。**排除該窗口後 43,164 筆、6,177 diff、違規 0 → PASS**。
  分類 cls1-only 870 / cls2-only 285 / both 5,022。**spec §5.1 記的 reviewer 分佈有兩欄對不上，已在
  spec 內更正並附成因**（07-12 前 long dead mode 無止盈單、07-12 後 threshold=0.8 使類 2 永不成立）。
  切換點乾淨：diff 末筆 `07-27 10:37`，`10:37→19:02` 停機空檔，重啟後 3 天**零 diff**。
- **A6 tick_sim A/B**（`a6_ab_result.txt`，2.83M events / 38 天 / 2 規則 × 場景 AB × W1W2W3full）：
  首跑**五項超標**（W1 上漲段 eq −7.221/−2.906；A W2、B W2、B W3 的 `final abs(delta)`）→ 依 spec 停下診斷
  （`diag_delta_result.txt`）→ **判準修訂後全項 PASS**，修訂內容與「這是事後改判準」的誠實標註見 **spec §5.3**。
  - `max abs(delta)` **8/8 改善**（B W2 0.76→0.46、A W1 0.48→0.38、A W3 0.36→0.24）
  - 帶號 delta 逐日軌跡 **8/8 每一天都 ≤ 舊規則**（新增的更嚴判準）
  - 三項 `abs(finΔ)` 劣化**全部伴隨窗口末 `min(L,S)` 提高**（B W2：OLD min 0.08 vs NEW **0.28**）
    ⇒ 舊規則的「低 delta」是把兩側都拆光換來的，不是對沖有效
  - `full` 窗口 eq：A **+0.826** / B **+2.919**；maxDD 幾乎全面改善（B W2 **−10.59pp**）；零強平、零拒單
- **A9 保證金**：每層 2.283，可用 17.955 → **只夠再加 7 層**（未低於 3 層門檻，但不寬裕）。

### 生產實測（2026-07-30 09:13，新規則已生效 3 天）
`L 0.42 / S 0.18`、止盈單 **long 0.04（加倍）/ short 0.02（不加倍）** ⇒ spec §7 的活體驗收 **PASS**。
delta 軌跡：`07-26 +0.40` →（重啟）→ `07-28 21:35 +0.52`（下跌段外擴，= 使用者裁決 ② 已接受的取捨，
**實盤印證這個缺口是真的會發生**）→ `07-30 03:42 **+0.24**`。36h 內 L −0.20 / S +0.08，**空頭會自己長回來**，
不再是舊機制下的單調被拆。

### 使用者的三個裁決（不變）
① 目標 = **delta 主動收斂**（非僅保住對沖）；② 對沖側止盈**減半不加倍**（最小改動，接受下跌段仍外擴）；
③ 範圍**含 `risk_monitor`** 雙向減倉改只減大側；④ 上線門檻 = 單測 + replay 結構化 diff + tick_sim 新舊對照。

### review 三件套：全部完成（2026-07-30）
1. **`security-review`：零 findings**。兩個特別點名的風險逐一排除——`tp_quantity` 簽名改動後只有
   兩個 caller 且都已更新（少參數會直接 `TypeError`，不存在靜默傳錯值）；不對稱減倉的量恆在
   `[reduce_qty, 2×reduce_qty]`，負值/零/NaN 不可達，兩筆都保留 `reduce_only=True`。
   附帶非安全備註：**大側最壞單次市價單量由 1× 變 2×reduce_qty**（設計如此，但滑價面變大）。
2. **`dual-review` Round 1 外部輪：`Needs discussion`（零 Critical / 零 Important）**，13/13 mutation 全被殺。
   它獨立拿 99,560 筆實盤 inputs 逐筆重算：**淨曝險側（多）一筆都沒變，改動 100% 落在對沖側（空）**
   ——與 A4 從不同角度對上。整合修復見 commit `7f209f7`（非正 threshold 守衛 + 七處誤導文案 + 兩處 docstring）。
   **外部輪抓到、內部輪沒抓到的**：`ui.py` 那次只修了一處標籤，web 頁1/2/3 與終端還有六處仍宣稱
   「止盈加倍閾值 = position_limit」（現在只是必要條件）。
3. **Round 2 專案規則輪**：抓到 **W1(28 筆) / W3(15 筆) 的 round_trips 低於 quant rules 的 30 次門檻**，
   而 §5.3 裡 gate 失敗的三格全部落在這兩個窗口 ⇒ 雙向失效，A6 的實質證據力集中在 W2 與 full。已入 spec §6。
4. **`verifier`：ACCEPT 8/9**。唯一 REJECT 是它自己挖到的 M10b——`backtester.py:84` 的
   `_legacy_grid_decision` 對 `tp_quantity` 的引數傳遞**零測試覆蓋**（把 `opposite_position` 換成
   `my_position`，571 條測試零反應）。**已補**（commit `9aa13cc`，三條 mutation 各紅一次）。
   ⚠️ 補完後**未再派 verifier 複驗**，是我自己跑它給的 mutation 驗證的。
5. **使用者裁決（2026-07-30）**：接受「gross 實質上限 = 保證金耗盡」，讓現有倉位順著新規則慢慢收斂到 hedge。
   依據與綁定條件（日後入金接近 146 USDC 時 risk_monitor 會變成活的生產碼）已入 spec §6。

**現況：579 passed / 1 skipped。1c 完成並已 merge 進 main（fast-forward `c9e9ff5..6094f4c`，15 commits）。**

### ✅ 重啟驗收（2026-07-30 11:15:41，pid 1318/1319，跑的是 `6094f4c`）
重啟前後快照對照（`<scratchpad>/snapshot_before.txt` / `snapshot_after.txt`，唯讀查交易所）：

| | before 11:12 | after 11:16 |
|---|---|---|
| 倉位 | L 0.42 @652.04 / S 0.18 @571.04 | 同（無跳變）|
| 錢包 / 權益 / 可用 | 148.20 / 114.66 / 41.28 | 148.20 / 114.81 / 41.38 |
| **強平價** | **96.49**（距現價 −83%）| 96.49 |
| 掛單 | 4 張 | 4 張，**id 全換新**（已重掛）|
| 止盈量 | L **0.04** / S **0.02** | L 0.04 / S 0.02 |

- **強平價 96.49**，不是本檔上面舊記的 288.98——delta 由 +0.40 收斂到 +0.24 後尾部風險實質解除。
  **可用餘額 41.28**（07-26 是 18.0），多頭 0.60→0.42 釋放了保證金。
- ⚠️ **錢包 171.53 → 148.20（四天 −23.3），而權益幾乎持平**（113.83 → 114.81）。
  這是浮虧轉實虧的帳務搬家：多頭均價 652 遠高於市價，每張止盈實質是認賠單（TODO 1b 的開放決策）。
  **新規則加快 delta 收斂 = 同時加快這個認賠速度。風險結構變好與帳面已實現變差是同一件事的兩面。**
- 啟動日誌驗收：`Bandit: False, Leading: False`、`0.30%/0.30%`、雙側 `dead_mode=false`、`已訂閱 userData stream`。
- 費率實查仍是 **maker=0 / taker=4bps**（促銷未結束）。
- **`-1125` 已停止**：重啟後至 12:15（跨越兩個 keepalive 時點 11:45:41 / 12:15:41）**零次**，
  對照舊引擎從 07-25 21:18 起每 30 分鐘準時報一次。
  ⚠️ **這是推論不是直接觀測**——keepalive 成功時不打任何 log（`ws_client.py:91-92` 成功路徑無輸出），
  「零 -1125」也可能是 keepalive 根本沒跑。主迴圈持續在打 `[Funding]`、同一 event loop 的
  獨立 task 沒理由單獨停擺，故判定為 PUT 成功。要變成直接觀測需在成功路徑補一行 log（未做）。

### 🔴 掛帳：listenKey 修復的兩條路徑尚未實際走到（不得當成已驗證）
1. **`[userData]` 端到端**：重啟後一小時內**零筆**——不是壞消息，是**沒機會**（沒有成交；
   現價 573.67、掛單在 572.08/575.53 兩邊都沒碰到，實盤網格成交率約 1 筆/天）。
   **這是整個 listenKey 修復的最終目的，在下一筆成交發生前都只是「應該修好了」。**
   驗收方式：`grep "\[userData\]" log/as_terminal_max.log`，出現即代表成交推送真的回來了，
   屆時 `sym_state.total_trades` / `state.total_profit` 才會開始累積（面板與 Telegram 日報的數字才是真的）。
2. **「重連時重取 key」路徑**：重啟後 WS **一次都沒斷**（`已訂閱 userData stream` 只出現 1 次）
   ⇒ `run()` 裡新加的那段還沒被走到。07-29 一天斷 5 次以上，應該很快。

### 📋 backlog（本次順帶發現，皆未處理）
- **停機時 `websockets` 內部 task 未被 cancel**：`Task was destroyed but it is pending!`
  （`Connection.keepalive()`）。`bot.stop()` 只 cancel 自己 tasks list 裡的，管不到函式庫內部。
  既有問題、不影響運行，只是每次停機留一行噪音。
- **keepalive 成功路徑無 log** ⇒ 「沒有錯誤」與「沒有執行」不可區分（見上）。

### 🔴 已修（等重啟生效）：`listenKey` 導致 user data stream 至少 18 天完全沒工作
commit `6a264d6`。**這是獨立缺陷，非 1c 引入**，但與 1c 共用同一個重啟窗口。

**根因**（`grid_engine/ws_client.py`）：`acquire_listen_key()` 只在啟動時呼叫一次，`run()` 重連時沿用舊值。
WS 斷線後伺服器廢棄該 key，而 **Binance 對無效 stream name 靜默接受**（SUBSCRIBE 不回錯、只是永遠
收不到資料）⇒ userData 永久失效，唯一線索是 keepalive 每 30 分鐘的 `-1125`（從 07-25 21:18 起，跨兩次重啟）。

**硬證據：`log/as_terminal_max.log`（07-12 20:05 起）零筆 `[userData]` 事件**，而同期倉位由
`0.58/0.34` 走到 `0.42/0.18`、成交上百筆。`bot.py:593` 的 handler 在任何 FILLED 都會 log 一行 ⇒
**ORDER_TRADE_UPDATE 全程沒被處理**。
- ⚠️ **`sym_state.total_trades` / `state.total_profit` 恆 0** ⇒ 面板成交次數與 **Telegram 每日摘要的
  「累計已實現」是假的**（使用者會看到的數字）
- `bandit` / `leading_indicator` / `dgt` 的 `record_trade` 從未被呼叫（三者皆已關閉，本次無實害）
- 成交後的即時反應全靠 `sync_service` 的 10s REST 輪詢

**修法**：run() 每次連上後、訂閱前重新 acquire（失敗只降級不連坐 bookTicker）；keep_alive_loop 的
except 裡立刻重建 key。**已知限制**：重建只讓下次重連生效，當前連線仍訂在舊 key（run() 阻塞在 recv）。
**未做**：自動重啟、指數退避、userData 靜默失效偵測（範圍控制，等這兩條上線觀察後再說）。

### 🔴 最高優先（新 session 開場必讀）

**0. 健檢 2026-07-26 的兩個發現（詳見 `tasks/health-check-2026-07-26.md`）**

- **網格在系統性拆掉對沖，不是在磨回 -68。** `decision.py:97` `tp_quantity` 的「進 0.02 / 出 0.04」
  在持倉 > `position_limit`(=0.02×5=0.1) 後**兩側都**無條件生效 ⇒ 每往返雙側各淨減 0.02。
  空頭基數小 ⇒ 對沖被拆的相對速度是多頭 3 倍。**逐筆對帳零殘差**：空 0.36→0.20（11 天 -44%），
  多 0.60→0.60，delta +0.24→**+0.40**，強平價 90.8→**288.98**（距現價 -49%）。
  ⇒ **TODO 2 / 1b(c)「入金補中性後長期持有」被否決**——補到 0.58/0.58 會被同一機制拆回 ~0.1。
  投影（機制推論）：空頭平衡點約 `position_limit`≈0.1，delta 走向 ~+0.50。
  **與 mult=40 無關**：加倍的實際觸發條件是 `my_position > 0.1`，`opposite >= 0.8` 那條現行倉位永不成立。
- **BNBUSDC maker 手續費實查 = 0**（`commissionRate`: maker 0 / taker 4bps；19/19 實盤成交全 maker、
  income 零 COMMISSION）。全套回測用 fee 2bps ⇒ **成本假設錯**。補跑 fee=0：預註冊 §6.4 由 FAIL 轉 PASS
  （成本排序 (1.0,1.5,0.5) 跨 slip{0,1,2} 全一致），全程 Δeq 場景B +13.5→**+22.0**、maxDD 5.1% vs 20.0%；
  **但 §6.3 仍 FAIL**（A/W1 上漲段 -16.8）⇒ **factor 0.5→1.0 仍不通過**，但「成本吃光 grinding」這個
  理由被推翻，真障礙是 W1 逆選擇。事後新增 30 cell 已揭露；**holdout 05-01~06-05 仍未開封**。
  ⚠️ 零 maker 費是促銷，非永久 ⇒ 據此的任何上線決策必須綁費率監控。

**1. 引擎狀態：已重啟並在跑**（2026-07-26 20:38，pid 99839）。
- 驗收過：`grid_spacing/take_profit=0.003/0.003`、`position_threshold=0.8`、雙側 `dead_mode=false`、
  四張雙側掛單（21:07 重掛）、`assumed_leverage: 5` 四個 symbol、舊 `leverage` key 全清除。
- 開工前一律先 `ps aux | grep as_terminal_max` 確認實際狀態，不要信本檔。

**2. `#14 mult=40` 的槓桿疑慮已部分解答（2026-07-26 健檢 §8，用實測數字直接算，未重跑回測）**
- 每層 0.02 @5x @570.66 需保證金 2.283，可用 17.955 ⇒ 只能再加 **7 層** → 多頭最多 0.74 < **裝死門檻 0.8**。
- ⇒ **mult=40 在現行資本下是惰性參數**，真正的補倉煞車是**交易所拒單 -2019**，不是策略裝死邏輯。
- 好消息：「回測用 20x 低估保證金 4 倍」不會讓 mult=40 變成災難（限制器不是它）。
  壞消息：煞車從策略層掉到交易所層，而回測**完全沒建模 -2019 拒單通道**。
- ⇒ **正式複核（5x 重跑分段掃描）優先度可降**；該補的是**保證金耗盡路徑的建模**，不是重跑 mult 掃描。

**2b. 以下為 2026-07-26 之前的記錄（背景，已被上面 §2 部分取代）：**
主力 script（`segment_scan.py`）在當時的 session scratchpad、repo 內不存在；同期 `scripts/cost_sensitivity.py:122` 預設 `--leverage 20`，而實盤是 5x。
該決策的核心正是**保證金與裝死邊界**——用 20x 算保證金會低估需求 4 倍。
**「不得再宣稱 mult=40 安全」修正為**：它的回測依據仍不可考，但 §2 的保證金算式顯示它在現行資本下無效
⇒ 不該再說它「危險」也不該說它「安全」，正確說法是「**它不是現在的限制器，-2019 才是**」。
（requote 實驗則已核實乾淨：`scripts/calibration_gate.py:38` 用 `leverage=5.0`。）

### git 狀態（2026-08-16 更新）
`origin/main` = `f4bdd8a`（watchdog 那 13 個 commit **已經推上去了**——舊記的「尚未 push」過期）。
`main` 領先 **2 個 commit 未 push**：`25b0135`（M1 修復）、`c1b5342`（tasks 文件）。
工作區只有 ` M .gitignore`（既有、與近期任務無關，使用者未指示處理）。
舊 branch 已全數清理（`feat/net-exposure-tp`、`feat/backtest-engine-fidelity`、`fix/dead-mode-deadlock`，
本地 + 遠端，刪前皆以 `git branch --merged main` 確認）。現在只剩 `main`。

### ⚠️ 生產現況（**2026-07-30 12:15 實測**，現價 573.67）
| | 07-26 實測 | **07-30 實測（1c 上線 3 天後）** |
|---|---|---|
| 多 | 0.60 @ 666.72 | **0.42 @ 652.04** |
| 空 | 0.20 @ 570.31 | **0.18 @ 571.04** |
| delta | +0.40 | **+0.24**（持續收斂）|
| uPnL | -57.64 | **-33.39** |
| **強平價** | 288.98（距現價 −49%）| **96.49（距現價 −83%）** |
| 錢包 / 權益 / 可用 | 171.53 / 113.83 / 18.0 | **148.20 / 114.81 / 41.38** |
| 費率 | maker 0 / taker 4bps | 同（促銷未結束）|

⚠️ **錢包四天 −23.3 而權益持平** = 浮虧轉實虧的帳務搬家（多頭均價 652 遠高於市價，
每張止盈實質是認賠單）。**新規則加快 delta 收斂 = 同時加快這個認賠速度**——
風險結構變好與帳面已實現變差是同一件事的兩面。

<details><summary>更早的歷史（07-15 / 07-26）</summary>

| | 07-15 00:00 UTC（由成交明細反推） | 07-26 實測 |
|---|---|---|
| 多 | 0.60 @ 690.29 | 0.60 @ 666.72 |
| 空 | **0.36** @ 571.75 | 0.20 @ 570.31 |
| delta | +0.24 | +0.40 |
| uPnL | ~-70.7 | -57.64 |
| 錢包 / 權益 / 可用 | 184.56 / 116.56 / ~6 | 171.53 / 113.83 / 18.0 |
| **強平價** | 90.8 | 288.98（距現價 -49%，原 -84%） |

</details>

歸因（見健檢報告 §4）：**Δ錢包 -13.03 = realized -12.65 + funding -0.38**（零手續費、零轉帳），
其中 -12.10 來自三筆 `sell LONG 0.04` 各 -4（多頭「止盈」價按現價+0.3% 定、不看均價 666.7 ⇒ **實質是認賠單**）。
**權益只掉 2.73**，同期價格 -1.42%：方向性約 -1.9 + funding -0.38 ⇒ **零費率下網格本身大致打平**。
uPnL 改善 +10.4 是把浮虧搬成實虧的帳務搬家，**不是收益**。真正變壞的是風險結構（見上「發現 0」）。
`marginMode` 實測確認 **cross**，兩側 `leverage` 實測 **5.0**。

生產設定：
- 純網格 0.3%/0.3%、thr=0.8（mult=40）、增強全關
- `requote_threshold_factor: 0.5`（2026-07-26 遷移後**明寫進 config 檔**，原本靠程式碼預設；行為不變）
- `assumed_leverage: 5`（2026-07-26 遷移，四個 symbol，原 `leverage: 20`）
  ⇒ **遷移前的回測結果不可再與遷移後直接比較**——保證金/強平模型輸入從 20x 變 5x

## TODO（優先序）

**0a. 【最高】userData watchdog 活體驗收** —— 🟡 進行中（2026-08-16 09:29 起，3/3 次重連已走完，
等 `given_up`）。實測時間軸見 Current Task。
~~**0b. 修 M1**~~ ✅ 2026-08-16 完成（實際在 `notifier.py` 不是 `reporting.py`）。
~~**0c. userData 根因**~~ ❌ **使用者 2026-08-16 裁決不做** —— 調查打住，接受 watchdog + REST 穩態。
~~**0d. `tasks/lessons.md` 第三次整併**~~ ✅ 2026-08-16 完成（115 → 89 行，仍超標）。

---

1. ~~觀察期複檢（07-13 之後）~~ **完成（2026-07-13）：replay PASS，但發現重大 fidelity 落差 → 衍生 TODO 1a**。
   - **Replay PASS**：全量 98,546 筆重放，9 筆 diff 與首檢完全相同（全部 ≤07-09 19:12 Taipei，舊 code 產物）；修復後窗口 32,630 筆零 diff；新 config 窗口 26.5h、147 筆零 diff。
   - **健檢過的項目**：倉位多 0.58@690.29 / 空 0.34@571.75 不變、權益 115.3、可用 5.95、強平 90.84；雙側 4 張掛單持續在 ±0.3% 刷新（最後刷新 = 最後決策 17:19 Taipei，一致）；新窗口 log 零 -2019、Telegram 修復後零 403；funding 26h 僅 -0.02；07-13 14:16 一筆瞬時同步失敗（REST 錯誤，之後決策照跑，非阻礙）。
   - **⚠️ 警訊（健檢主發現）：觀察期 26.5h 網格零成交、零 REALIZED_PNL**。`fetch_my_trades` 確認窗口內僅 4 筆 = 07-12 手動補空；期間價格走 588→568（~3.4%）網格一張未成交。**不是新故障**：income 按日聚合顯示過去一個月成交本來就 0~3 筆/天、REALIZED_PNL 月合計 ≈ **-0.14**。
   - **機制（code 證據）**：`decision.py:125` `should_adjust` 在偏離 anchor ≥ `grid_spacing*0.5`（=0.15%）就撤單重掛，掛單在 ±0.3% → **掛單被追價永遠搬走**，只有「引擎反應間隙內暴走 >0.3%」才成交。backtester 撮合是「上一根掛單吃整根 1m bar 的 high/low」（`backtester.py:712` `_settle` 先結算後 decide()），掛單存活整整 1 分鐘 → **回測成交率 ~17 筆/天（513 trades/月）vs 實盤 ~1 筆/天，高估一個數量級以上**。FIDELITY_NOTES (5) 有揭露「追價以偏離門檻近似」但未量化幅度。
   - **對現行計畫的衝擊**：「網格慢慢磨回 uPnL -68」在實盤成交率下不成立（月已實現 ≈ -0.14，磨回遙遙無期）。thr=0.8 的方向結論不受影響（mult 40/60/100 恆同 = 只是關裝死），但 #14 回測的 Δeq 絕對值全部高估。
1a. ~~成交率斷層的處置~~ **驗證完成（2026-07-15，branch `feat/requote-semantics`，verifier ACCEPT 7/7，dual-review Ship as-is）：數據否決方向 (i)，剩 (ii) 待使用者裁決**。
   - tick 級實驗（aggTrades 06-06~07-13、校準 gate 三道全 PASS、N=166 組合全揭露）：factor=1.0（掛到成交）§6 判準 3 FAIL——W1 上漲段 -21.1 / W3 震盪 -4.5（只贏 W2 下跌 +27.7，逆選擇主導）；成本 2/2bps 下全程排序翻成 0.5 最優（成交 20 倍但費用吃光 grinding）；factor 0.8 的 +14.9 是 threshold=limit 邊界懸崖（成交驟降 9 倍），依預註冊規則不採納。**「磨回 -68」路線被數據關死；現行 0.5 語意在成本現實下可辯護。**
   - 已上線的中性產物：`requote_threshold_factor` 參數化（預設 0.5 bit-identical，replay 9/9 不變）、tick 模擬器 + PositionBook + aggTrades 管線（未來 requote 類實驗基礎設施）、FIDELITY_NOTES (13)。holdout 05-01~06-05 保持未開封（§6 未全過，依鎖不跑）。
   - branch 已 merge 進 main（fast-forward `fa5aed7..f8d51e1`，19 commits，合併後 525 passed），branch 已刪。
1b. ~~**-68 uPnL 出場路線**（四選項）~~ **2026-08-15 實測後建議關閉：問題自己消失了。**
   - 08-15 生產快照（現價 611.29，BNB 自 07-30 的 573.67 漲 +6.6%）：
     多 **0.24 @ 613.55**（原 0.42 @ 652.04）、空 **0.06 @ 601.00**、delta **+0.18**、
     **uPnL −1.16**（原 −33.39）、強平價 **0（不可達）**、可用 **78.97**（原 41.38）。
   - **1b 的四個選項全都是在處理「一個深度水下的凍結多頭倉」，那個倉已經不存在了**——
     均價從 652 掉到 613.55（貼近市價），1c 的新止盈規則 + 一段漲勢把它磨掉了。
   - **代價已付**：近 15 天 `REALIZED_PNL −24.01` / 36 筆，錢包 148.20 → 121.66（−26.5），
     但權益 114.81 → **120.56（+5.75）**。這正是 07-30 記的「浮虧轉實虧」照劇本走完。
   - ⚠️ 附帶推翻一個舊觀察：36 筆/15 天 ⇒ 07-13 健檢記的「實盤 ~1 筆/天、網格幾乎不成交」
     在震盪+趨勢段**不成立**，那個數字是特定窗口的產物。
   - 待使用者確認後即可標記關閉。
1c. ~~**`tp_quantity` 不對稱會吃掉任何人工建立的對沖**~~ **完成並 merge+push（2026-07-30，15 commits `f2f6bbe`..`6094f4c`）**
   - 加倍改為只給淨曝險側；範圍含 `risk_monitor` 不對稱減倉、`bot.py` 死碼、`ui.py` 標籤、七處誤導文案。
   - 驗收：**579 passed / 1 skipped**；A4 replay（排除舊 code 窗口後 43,164 筆零違規）；
     A6 tick_sim A/B（判準經 §5.3 事後修訂後全項 PASS，peakΔ 8/8 改善、逐日軌跡 8/8 不劣化）；A9 可用 7 層。
   - review 四輪：security-review 零 findings / dual-review 外部輪 `Needs discussion`（零 Critical/Important、
     13/13 mutation 全殺）/ Round 2 專案規則（抓到 W1·W3 樣本不足）/ verifier ACCEPT 8/9（M10b 已補）。
   - **實盤成效（上線 3 天）**：delta +0.40 → **+0.24**、強平價 288.98 → **96.49**、uPnL −57.6 → **−33.4**。
   - **代價**：錢包四天 −23.3（浮虧轉實虧加速）。詳見「生產現況」段。
   - 使用者裁決：接受「gross 實質上限 = 保證金耗盡」，讓倉位順著新規則慢慢收斂到 hedge（spec §6）。
1c-old. 原始問題描述（保留供追溯）：（健檢 §5，逐筆對帳零殘差）
   - `decision.py:97`：持倉 > `position_limit`(0.1) → 止盈量加倍 ⇒ 進 0.02/出 0.04，**兩側都適用**，每往返雙側各淨減 0.02。對沖側基數小 ⇒ 相對衰減 3 倍快。
   - 要讓「補中性後長期持有」成立，須讓對沖側免疫（例如加倍只對淨曝險方向那側生效，或對沖倉走獨立帳本不進網格）。**需 brainstorming，Plan track。**
   - **調 mult 救不了它**：加倍的實際觸發是 `my_position > 0.1`，`opposite >= threshold(0.8)` 現行倉位永不成立。
2. ~~入金 ~25 補滿 delta → 完全中性~~ **被健檢否決（2026-07-26）**：補到 0.58/0.58 會被 `tp_quantity` 不對稱拆回 ~0.1。要做須先解 1c。
3. **symbols-set 併發 race**（#10-A 衍生）：修法傾向砍終端 config 選單（單一 writer 根治）
4a. ~~`leverage` → `assumed_leverage` 改名與舊 key 清除~~ **完成（2026-07-26，branch `feat/leverage-rename`，11 commits，546 passed / 1 skipped）**
   - **spec/plan**：`docs/superpowers/specs/2026-07-26-leverage-rename-design.md` + `docs/superpowers/plans/2026-07-26-leverage-rename.md`
   - **原合併 spec（B+C）連兩輪被 quant reviewer 判 Reject**，兩次 blocker 同形態——斷言接線存在而未查證（v1 交易所邊界、v2 行程邊界）。依 R4 拆為 4a（純改名）與 4b（讀實測值）。
   - **生產 config 已遷移**（引擎停機窗口、走 `config_io` flock+原子寫）：四個 symbol 的 `leverage: 20` → `assumed_leverage: 5`（使用者裁決用交易所實測值）。遞迴比對零其他差異、憑證完整。備份 `config/trading_config_max.json.bak-pre-leverage-rename-20260726`。副產品：`requote_threshold_factor: 0.5` 由隱含預設變成明寫（行為不變）。
   - **⚠️ 遷移的後果**：所有遷移前的回測結果**不可再與遷移後直接比較**——保證金與強平模型的輸入從 20x 變成 5x。
   - **⚠️ 「行為零變更」只適用 rename 機制**，不適用整條 branch：另有兩處刻意變更——UI clamp `max_value` 15→125（舊值小於生產值 20，打開頁3 或從頁2 送出就把槓桿靜默降級成 15，是既有 bug）、以及上述生產值 20→5。
   - **review 全程**：3 個 task review → whole-branch review（opus，Ready to merge + 2 必修）→ security-review（零 findings）→ dual-review Round 1 外部輪（Fix required 2 Important → 修 → **Ship as-is**）+ Round 2 專案規則 → verifier **ACCEPT 8/8**（含 Monkey Testing 專門回合）。
   - **verifier 最有價值的發現**：13 條自選 mutation **存活 3 條**——三個映射點改成硬編碼常數，543 條測試全綠。根因是 `tests/web/test_backtest_service.py` fixture 用 `assumed_leverage=20` = dataclass 預設值 ⇒ 斷言是套套邏輯，**這條 branch 的核心主張自己沒有守衛**。已補守衛（測試值 7，同時 ≠ 預設 20 且 ≠ 生產值 5）。lesson 已記。
4b. **【開放】讀交易所實測槓桿**（原 B 路線，範圍縮到引擎行程內）
   - **實測事實**：ccxt 4.5.32 `fetch_positions` 預設走 V3 positionRisk，**不回 `leverage`**；須 `params={'useV2': True}` 才有（實測 5.0，marginMode=**cross**）。
   - **範圍限制（v2 review 抓到）**：web 是獨立行程、只讀落地檔，**拿不到引擎記憶體的實測值** ⇒ web 端須明確承認無實測來源、一律 explicit。
   - **注意**：`grid_engine/rest_gateway.py` 只有單 worker executor，**無重試、無斷路器**——別再假設它有。
4c. **backlog（本次挖出，皆未做）**
   - **`trading_mode` 是與 `leverage` 完全同構的假旋鈕**：生產 config 有、`grid_engine/` 零 reader、唯一實效是頁3 拿它當回測優化器 param-bounds 預設（`web/pages/3:1155` → `backtest/smart_optimizer.py:261`）。位置更誤導——它在頁2 與真旋鈕並列，help 說「不同模式適合不同的持倉週期」。
   - **`assumed_leverage` 零值域驗證**：填 `-5` 回測會跑完 113 筆交易吐出 **+0.155% 正報酬**（保證金數學全錯但不報錯）；`0` 觸發 divide-by-zero warning 後靜默回 0 筆；`nan`/`inf` 會寫出非嚴格 JSON。既有問題，但 UI 上限 15→125 放寬後可及範圍變大。`grid_engine/config.py:17` 已有 `_norm_requote_factor` 先例可照抄。
   - **`backtest/optimizer.py:55` 仍把 `leverage` 當可搜尋參數**（`[5,10,15,20,25,30]`）——與「槓桿不由 repo 決定」的原則直接衝突，且會系統性挑到高槓桿（收益放大、強平模型又用同一個假槓桿）。`backtest/smart_optimizer.py:228` 已寫死 20，兩處作法不一致。
   - `grid_engine/config.py:166` `legacy_api_detected` 死欄位；`scripts/compare_backtest_engines.py:50` 仍 import 已刪的 `core.backtest`。
   - **衍生待辦（重要，非本次範圍）**：#14 的 `mult=40` 上線決策，其回測槓桿假設**不可考**（主力 script 在 session scratchpad、repo 內不存在；同期 `cost_sensitivity.py:122` 預設 20x）。**mult=40 未經 5x 複核，而該決策核心正是保證金與裝死邊界。** requote 實驗則已核實用 5x（`calibration_gate.py:38`）乾淨。
   **2026-07-26 更新**：健檢 §8 用實測保證金算出 mult=40 在現行資本下是惰性參數（到不了 thr=0.8），優先度降；改為「補保證金耗盡/-2019 拒單路徑的建模」。
5. trading_mode 收編 engine schema（等 #4 驗收後）；頁3 clamp 寫回 session 全站排查
6. GCE 部署三件套（VM/setup script/IP 白名單）——部署後 replay 驗收要在 GCE 重跑一次
7. ~~file logger 修繕~~ **全部完成並 commit（`f64ae2f`，2026-07-12 20:05 重啟驗收過）**：新 log 每行帶時間戳、引擎雙側掛單正常；202MB 舊檔已歸檔為 `log/as_terminal_max.log.archive-20260712`（gitignored，含觀察期首日與歷史 -2019/斷路記錄）。main 已與 origin 同步。
8. ~~Telegram 通知接通~~ **完成（2026-07-12 21:43）**：根因兩個——chat_id 誤填 bot 自身 ID（log 三筆 `403 the bot can't send messages to the bot` 佐證）+ 引擎啟動時憑證為空致 reporter 未建。使用者修正 chat_id=1054193397 後 21:43 重啟，之後零失敗記錄。**07-13 20:00 Taipei 首封每日摘要使用者確認收到，端到端驗收完成。**

## Blockers
**無硬阻礙。**

**⚠️ 引擎跑的還是舊碼。** watchdog 已 merge+push 進 main（`b0c6047`），但引擎行程從 08-14 23:20
就活著（`uv run as_terminal_max.py`），**記憶體裡是舊碼**。要生效必須重啟——而重啟正是
TODO 0a 的活體驗收。開工前一律先 `ps aux | grep as_terminal_max` 確認實際跑的是什麼。

**掛帳（非阻礙）**：
- userData 根因未解，watchdog 是止血。唯一剩下的可測假設需要使用者去 Binance 後台開新 key。
- `backtest/config.py:37` `fee_pct` 預設 2bps 與實查值（maker 0）不符 ⇒ 既有回測成本假設偏保守。
  修改前要先決定「促銷費率該不該寫進預設值」（傾向不寫死，改成必填 + 啟動時實查對帳）。

## Recently Completed（2026-08-15）：userData watchdog 全線
- **merge + push 完成**：`main` = `b0c6047`，`origin/main` 同步（推 20 commits `b853da8..b0c6047`）。
  watchdog 本身 13 commits（rebase 後 fast-forward）。**main 上實跑 670 passed / 1 skipped**
  （分支起點 589/2，+80 條測試）。worktree / branch / SDD workspace 皆已清除。
- **做了三件事**：偵測靜默失效並告警、有限復原（3 次，退避 300/900/2700 後進 `given_up`）、
  成交統計改由 REST 增量拉取（單一 writer，userData handler 停寫）。另加每日摘要帶 watchdog 狀態。
- **流程**：brainstorming → spec → writing-plans → SDD 4 tasks（每 task fresh implementer +
  task reviewer）→ whole-branch review(opus) → verifier 兩輪(opus) → security-review(opus)
  → dual-review 外部輪(opus) + Round 2 → 最終 scoped re-review(opus) = **Ship as-is**。
- **最有價值的發現（全部來自「要求 reviewer 自選 mutation」而非照我的清單）**：
  - verifier 自選 18 條，3 條存活。其中 **Critical：刪掉 `sync_all()` 裡的
    `await self._sync_trade_stats()`，625 條全綠**——一條在修「元件對但沒接上且無偵測」的
    分支，自己身上有一模一樣的洞。
  - 外部輪自選 17 條，3 條存活。**兩條打穿核心**：`total_trades += 1` 改寫成 `= x + 1`
    就繞過「單一 writer」守衛；`if never: record_event()` 就繞過接線守衛，102 條全綠。
    **那兩條字串掃描測試是我寫進計畫的，而且我下過 ruling 說可以接受——我裁錯了。**
  - security-review 抓到 `_sync_trade_stats` 是唯一可能讓例外冒泡的 `_sync_*`
    ⇒ 每 5 秒重連的永久迴圈；以及分頁迴圈無頁數上限、inline 在 WS recv 路徑上。
  - Round 2 抓到我在 dispatch 裡「比照兄弟方法」這句**未經查證的前提**被固化進程式碼註解，
    而 `_sync_funding_rates` 根本沒有 try/except（同一條失效路徑仍暢通）。
- **認列不修**（spec §8.1 / §8.2）：monotonic 時鐘、`start_time_ms` 交易所時間校正、
  `_handle_ticker` 價格時效守衛、bandit/dgt 的 `record_trade` 仍由死掉的 userData 餵
  （三者生產停用，但**日後開回會拿到全零歷史且無警告**）、裝死模式下停在 `degraded`
  走不到 `given_up`（設計必然，可見性由每日摘要接住）、五條存活 mutation（防呆第二層、不可達）。
- **本次調查的副產品**：`tasks/notes.md` 補上 userData 死因調查續（listenKey 輪換假設被否決、
  舊 log 找到死亡時點、`POST listenKey` 只回同一把舊 key 解釋了 `6a264d6` 為何修不好）；
  `tasks/lessons.md` 新增通則 6（八種假守衛形態）與「觀測工具沒有自我監控就不可信」。

## Recently Completed（2026-07-30）：TODO 1c 止盈加倍只給淨曝險側 + listenKey 修復
- **1c 全線**：spec v3 → plan 8 tasks → 實作 → A4/A6/A7/A9 驗收 → 四輪 review → 重啟驗收 → merge + push。
  15 commits `f2f6bbe`..`6094f4c`，`main` 推 20 commits（`9908faf..b7fd7de`）。579 passed / 1 skipped。
- **listenKey 修復**（`6a264d6`）：user data stream 至少 18 天完全沒工作，根因是重連時沿用被伺服器
  廢棄的 key，而 Binance 對無效 stream name 靜默接受。`-1125` 重啟後已停止。
- **流程倒序的誠實記錄**：Task 1-5 的 code 先上線（07-27 重啟），Task 6/7/8 的驗收 3 天後才補跑。
  驗收若當時 FAIL，代價是回滾生產而不是不上線。**下次不要再這樣。**
- **A6 gate 事後修訂**：首跑五項超標 → 依 spec 停下診斷 → 判準修訂後全項 PASS。
  修訂內容與「這是看過結果之後才改的判準」的誠實標註全部留在 spec §5.3，並經使用者核可。
- **本次四個外部視角各自抓到不同東西**（值得記住這個分工）：
  - security-review：零 findings（改動無新輸入面）
  - dual-review 外部輪：`ui.py` 標籤只修一處、還有六處文案仍誤導（**內部輪看 diff 看不到**）
  - Round 2 專案規則：W1(28)/W3(15) 交易次數低於 30 次門檻，而 gate 失敗的三格全在這兩窗
  - verifier：M10b —— `_legacy_grid_decision` 的 `tp_quantity` 引數傳遞零測試覆蓋
- **lessons 二次整併** 108→89 行 + 新增通則 5（差值/比率/軌跡型指標）與兩條實戰教訓
  （單點標量比不了軌跡、測 loop 時終止條件不得掛在被測行為上否則 mutation 會 hang 而不是 fail）。
- **backlog（順帶發現，未處理）**：停機時 `websockets` 內部 task 未被 cancel（噪音一行）；
  keepalive 成功路徑無 log ⇒「沒有錯誤」與「沒有執行」不可區分；
  `PositionBook` 是否允許負持倉（spec §8）。

## Recently Completed（2026-07-26）：TODO 4a `leverage` → `assumed_leverage`
- **merge + push 完成**：main `f08ce2c..1aab450`（11 commits，fast-forward），origin 同步，546 passed / 1 skipped。
- **流程**：brainstorming → spec（v1/v2 連兩輪被 quant reviewer 判 Reject，同形態 blocker「斷言接線存在而未查證」——v1 斷在交易所邊界、v2 斷在行程邊界 → 依 R4 換路徑，拆成 4a/4b）→ 4a spec（Approve with changes）→ plan（Reject → 修 → Approve with changes）→ SDD 3 task（每 task fresh implementer + reviewer）→ whole-branch review（opus，Ready to merge + 2 必修）→ security-review（**零 findings**）→ dual-review Round 1 外部輪（Fix required 2 Important → 修 → **Ship as-is**）+ Round 2 專案規則 → verifier **ACCEPT 8/8**（含 Monkey Testing 專門回合）。
- **技術產物**：`config_io.merge_preserve(drop_symbol_keys=...)`（獨立最終 pass，drop 永遠勝出）、`SymbolConfig.__getattr__`/`__setattr__` 舊名雙向攔截、`from_dict` 相容分支（`data = dict(data)` 不竄改呼叫端）、三個映射點的非套套邏輯守衛。
- **實測留痕（重要，別重新發現）**：
  - ccxt 4.5.32 `fetch_positions` **預設走 V3 positionRisk、不回 `leverage`**；須 `params={'useV2': True}`（實測 5.0，`marginMode=cross`）。切 V2 是同一次呼叫加參數，**不是**新增 REST 呼叫。
  - `grid_engine/rest_gateway.py` 全文 21 行，只有單 worker executor，**無重試、無斷路器**。別再假設它有。
  - `config_io.merge_preserve` 只 update **不刪 key** → 任何改名都需配 `drop_symbol_keys`，否則舊 key 永久殘留。
  - `tests/` **零 import** `as_terminal_max` 與 `web/pages/*` ⇒ 那些檔的漏改/語法錯 pytest 完全抓不到，只能靠逐點 read-back + `py_compile`。
  - 全套測試須在 `as-grid-dragon` 子目錄跑；monorepo 根目錄會被 `as-grid-auto/test_position_mode.py` 的 collection-time `sys.exit(1)` 打斷。
- **兩項流程不合規（誠實記錄）**：Red Team Protocol 實作前未列攻擊向量（事後由 spec §4 + 三輪 reviewer 紅隊 + security-review 覆蓋）；Monkey Testing 原本漏做，補在 verifier 那輪。
- **最有價值的發現**：verifier 13 條自選 mutation **存活 3 條**——三個映射點改成硬編碼常數、543 條測試全綠 ⇒ 這條 branch 的核心主張自己沒有守衛。根因是 fixture 用 `assumed_leverage=20` = dataclass 預設值。已補守衛（測試值 7，同時 ≠ 預設 20 且 ≠ 生產值 5）。lesson 已入 `lessons.md`。

## Recently Completed（2026-07-13~15）：requote 語意驗證全計畫
- **流程**：brainstorming → spec（quant reviewer 8 findings 修訂，含 holdout/事件數守門/netted 拒單保守取或）→ plan（reviewer 1 blocker + 7 should-fix 修訂；BLOCKER-1 經親手驗算部分駁回——equity 對帳務基礎不變成立，但可用餘額分歧真實，「保守取或」搬到拒單通道）→ SDD 12 tasks（每 task fresh implementer + reviewer，4 輪 fix 迭代）→ security-review 無 findings → dual-review 外部輪 Ship as-is → verifier ACCEPT 7/7 → merge main（f8d51e1，525 passed）。
- **關鍵技術產物**：`backtest/accounting.py`（PositionBook 雙帳，backtester 委派行為零變）、`backtest/tick_sim.py`（tick 事件模擬器：嚴格穿越/500ms 延遲/5s cooldown/有倉側 OR gate；stale-orders pruning 修過二次方變慢）、`backtest/aggtrades.py`（UTC 日界+完整性驗證+spread 重建）、`scripts/calibration_gate.py`（三 gate，判準 2026-07-14 修訂留痕）、`scripts/requote_experiment.py`（N=166 矩陣）。
- **過程中修訂的判準（全留痕於 spec §4.3/§6.2）**：高端 gate 上界「tick≤1m」前提被實測推翻（1m 每分鐘才重掛 vs tick 5s re-arm，1.47x 是機制非 bug）→ 改 0.2x 下界 + 成交真實性回歸守衛（**注意：該守衛對現行引擎是套套邏輯，非獨立證據**，lessons 有記）；6 月 cap 10x→15x（live 受 -2019 壓制未建模）。
- **重要教訓已入 lessons.md**：套套邏輯驗證（2026-07-15 條）。
- **07-13**：觀察期複檢 replay PASS + 26.5h 零成交發現（TODO 1 詳記）；Telegram 每日摘要端到端確認。

## Recently Completed（2026-07-12）
- **TODO 7 重定向後完成（未 commit：grid_engine/utils.py、as_terminal_max.py 尾兩行、tests/test_logger_file_config.py 新檔）**：原前提「-2019/斷路器只噴終端磁碟無痕」是**誤記**——`log/as_terminal_max.log` 一直在收（歷史 1M+ 筆下單失敗、8 筆斷路，多為舊 config 時代產物）。真缺陷三個：(a) format 只有 `%(message)s`，`datefmt` 是死參數 → 事件無時間戳無法定位；(b) 202MB 單檔無 rotation；(c) `basicConfig` 是 import 副作用，web/streamlit 進程也會裝 handler → 換 RotatingFileHandler 後多 writer rollover 會互抽 fd。修法：`%(asctime)s` + RotatingFileHandler(50MB×3, delay=True) + 抽成 `setup_file_logging(force=True)` 只由 `as_terminal_max.py` `__main__` 呼叫（單一 writer）。439 passed（+5 新測試，全部 mutation red-once，含「pytest logging plugin 讓 basicConfig no-op」的假陰性教訓：先綠再紅順序不能省）。dual-review：外部輪 4 should-fix + 3 nit 全修（force=True/subprocess cwd/註解歸因/部署 checklist），Ship as-is；verifier ACCEPT 5/5（獨立 mutation 2/2，mktemp 隔離零污染）。**生效需重啟**，checklist 見 TODO 7。
- **#4 Task 10 replay 驗收 PASS**：全量 98,402 筆重放，9 筆 diff **全部**落在 07-09 19:12 之前、模式一致（long 進 dead mode 未接管止盈單）——正是 `60917cc`（07-10 10:57）修掉的 bug，屬舊 code 產物。現行 code 窗口（07-10 10:57 起，跨 ~2.2 天）**32,481 筆零 diff**，滿足「≥24h 零 diff」驗收準則（GCE 部署後仍需在 GCE 重跑一次，見 TODO 6）。
- **觀察期首檢（新 config 窗口 14:51~15:40，~1h）**：決策 20-30 分/筆是純網格 0.3% 的預期頻率（舊 config 增強全開才會 ~6s/筆，勿誤判為故障）；交易所 4 張掛單與最後一筆決策 orders 逐張匹配（價格/數量/reduceOnly），活體 decide→execute 一致 ✅；倉位多 0.58/空 0.34、權益 116.07、可用 6.12、強平 90.76 與收工快照一致 ✅；income since 14:51 僅 COMMISSION -0.06 + TRANSFER +35，尚無 REALIZED_PNL（未觸及止盈，窗口太短）；無 -2019 跡象（空頭 0.04→0.34 成交成長證明下單通道暢通）。健檢腳本（read-only）：scratchpad `health_check.py`——fetch positions/balance/open_orders + `fapiprivate_get_income({'startTime': ...})` 聚合 incomeType。
- **#14 全線收工並 merge 進 main**（`a49f6b0`，434 passed）：分段窗口驗證（漲/跌/震盪 × 3 場景 × cost sens）確認 mult=40 跨路徑穩健 → config 6 處變更上線 → 入金 35 → 補空 0.30（分批限價，中途撞出 leverage 假旋鈕：config 20 vs 交易所實際 5x）→ 使用者裁決維持 5x、選 B 部分對沖 → delta +0.50→+0.24，強平價 359→90.8
- lessons.md 整併 202→61 行（六條「靜態成立執行期不成立」同族併通則）；UI 持倉顯示兩位小數
- branch `feat/backtest-engine-fidelity`（37+5 commits）merge + push，main == origin/main

---

## 存檔：#14（2026-07-11 重新定向）：先回測定 threshold，再談改 code。原「修 `dead_mode_price`」的前提被實測推翻——見下。

### ★★★ 議倉裁決（真錢，2026-07-11 實測交易所）
`fetch_positions` / `fetch_balance` 實測（read-only，未下單）：
| | |
|---|---|
| 錢包 / 權益 / 可用 | 150.05 / **82.19** / **4.15**（保證金使用率 95%） |
| 多頭 | **0.58 @ 690.29**，現價 573.49，**uPnL -67.74**（水下 20.4%） |
| 空頭 | 0.08 @ 570.17，-0.27 |
| 強平價 | **412.12**（距現價 -28.1%）；**marginMode = cross**（非 isolated！） |
| 掛單 | `sell LONG @603.05 x0.04 RO`（= 日誌 03:57:29 那張假出場單，還掛著）、`buy SHORT @572.08 x0.04 RO`、`sell SHORT @575.53 x0.02` |

**關鍵推翻**：
1. **均價 690，不是貼近現價** → 方案 A（掛均價×1.003 = 692）要漲 **20.7%**，比現在那張 603 還遠。**A 不解凍、讓它更凍。**（教訓：推薦 A 前沒先量均價，被實測打臉。）
2. **cross margin，非 isolated** → #10-B 判死 hard_stop 的前提（isolated 結構性封頂）**事實層面不成立**。412 全帳戶爆是真尾部。
3. **裝死停擺 104h 不是 bug**，是裝死正確地阻止對水下 20% 倉位加碼。真正壞的是 threshold 以「幣數量」計價（0.4），從 690 一路買到 573 從不看保證金。

### 使用者裁決：看強/中性 + **願意入金** → 走 C（對沖到 delta-neutral，網格慢慢補，不實現虧損）
**但發現致命矛盾**：補空到 0.58 → 雙邊都 > threshold 0.4 → **兩側同時進裝死** → 網格停擺 → 「慢慢補」不發生，只鎖定 -68。「對沖 + 慢慢補」與 `threshold=0.4` **數學不相容**（任何值得對沖的倉位都已超 threshold）。
- C 需配 threshold 提高（讓對沖後雙邊 0.58 回正常網格）。
- 補空到中性數字：補空 **0.50**（0.08→0.58），需保證金 ≈14.3，可用 4.15 → **需入金**（建議 30-40 留 buffer）。delta-neutral 後 uPnL 幾乎不隨價變，鎖 -68，網格賺 0.3% 間距補回，價回 690 平對沖出場。

### ★ 使用者最終裁決：**先回測看 threshold 調多高**（不急著入金/改 config）

**seed 注入工具完成**（3 commits `e5ad948`→`0218871`→`b9821ef`，全套 434 passed，14 seed 測試）：
Config 加 `seed_long/short_qty/price`，`_run_terminal_ui_mode` 持倉初始化後 pre-populate seed lot（margin 扣 balance 不扣 fee），seed=0 bit-identical。
- **review 全走完，verdict = `Ship as-is`**：內部 reviewer（I1 legacy 靜默空倉/I2 FIFO 分歧/M1 inf/M2 套套邏輯）→ 修（`_validate_seed` 前置 raise）→ dual-review 外部輪（**no Critical**；I1 fee_pct=0 假綠/I2 揭露搬進 FIDELITY_NOTES (12)/M3 名不副實/M4 defense-in-depth/M5 NaN）→ 全修 + 3 mutation 驗證（fee/NaN/balance 扣減都 red-once）→ Round 2 專案規則 conform → **verifier ACCEPT 6/6**（fresh-context read-back + 實跑 + 獨立 mutation 3/3）。3 commits e5ad948→0218871→b9821ef，全套 434 passed。
- **統一原則**：seed qty>0 但任何原因無法如實注入（負/inf/NaN/price≤0/方向矛盾/走 legacy）→ 大聲 raise，不得靜默空倉（數字定實盤參數）。
- **關鍵保真限制（FIDELITY_NOTES 12）**：per-lot FIFO 先平 index-0 seed lot、與生產 Binance netted 均價分歧 ⇒ 涉及 seed 部分平倉的 threshold 掃描 realized/final_equity 系統性偏離生產，**只可看方向不可當精確預測**。

**threshold 掃描結果**（seed @ 生產均價，資料 06-06~07-10 單一路徑含 -14.8% 下跌，fee 2bps/slip 1bp 基準；起始 equity ≈ 82 吻合生產）：

| 場景 | mult=20(thr0.4) | mult=29(0.58) | mult≥40(≥0.8) | cost sens 最佳 |
|---|---|---|---|---|
| **現狀 0.58/0.08** | eq88.1 dd0.56 dead100% | eq86.3 | eq86.9 dd0.49 dead0% | **穩定 mult20** |
| **對沖後 0.58/0.58** | eq95.7 dd0.50 dead6.5% | **eq107.2** dead6.3% | eq89.7 dd0.47 dead0% | 穩定 mult29 |

**判讀（誠實）**：
- **`mult=29` 的 107 是過擬合陷阱**：threshold=0.58 恰好卡 seed 持倉量邊界，倉位在邊界反覆進出裝死。lessons「尾部參數最佳點永遠在懸崖邊」典型，**不可信**。
- **穩健訊號 = `mult≥40`**：40/60/100 **完全無差異**（對這段數據不敏感）→ 對沖後雙邊 0.58 < 0.8 回正常網格，兩側掛單，eq89.7、max_dd 最低 0.469。從起始 82 補回 ~8。
- **對沖後（雙邊活）比現狀補得多**：mult≥40 對沖後補 7.7 vs 現狀維持補 5.7。支持 C 路線。
- **局限**：單一歷史路徑、只含一段下跌，未測上漲/震盪。threshold 的真正代價（單邊大趨勢無限加倉）在對沖後被 delta-neutral 吸收，這段數據看不到 → **不能外推到不對沖的情況**。cost sensitivity 內排序穩定但絕對差距小（成本擾動 ±1.3 vs 場景間差距 ~18）。

**⚠️ 這些數字的可信度取決於 seed 注入工具正確性 → 需 dual-review 才能讓使用者據此入金**。
**初步建議**：走 C（入金補空到中性）時，threshold_multiplier 提到 **40**（thr=0.8，讓對沖後雙邊 0.58 回正常網格）。不要信 mult=29。

**分段窗口驗證（2026-07-12，補「單一路徑只測下跌」局限；script 在 session scratchpad `segment_scan.py`）**：
W1 上漲 06-06~06-16（574→618, +7.6%）/ W2 下跌 06-15~07-02（617→550, -10.8%）/ W3 震盪 06-25~07-10（564→576, ±5%）× 3 場景（現狀 150 / 對沖後 150 / **對沖後+入金35=185**，上次掃描沒建模入金）× mult {20,29,40,60,100}，60 回測 + 對沖場景 cost sens 54 回測：
- **對沖後 mult≥40 三段全正**：Δeq 上漲 +0.38 / 下跌 +2.89 / 震盪 +7.68，零強平，40/60/100 恆完全相同（= 對現倉規模等效關裝死，僅留 0.8 防暴衝上限）。上漲段 max_dd 三場景最低。
- **mult=20（現行）對沖後在上漲 -6.17、下跌 -10.81**，三段兩負 → 對沖 + 現行 threshold 確定是壞組合，數學矛盾被分段實測坐實。
- **mult=29 再次確認是懸崖**：震盪段 +21.66 貌似大勝，但 thr=0.58 恰等於 seed 量、贏在裝死邊界反覆進出的 artifact；上漲段輸給 40。跨窗口 best 在 29/40 間搖擺（W1=40、W2/W3=29）→ 依 lessons「排序不跨窗口穩定 + 最佳點在邊界」雙重理由棄 29。
- cost sens：每個窗口內排序對 fee{2,4}bps×slip{0,1,2}bps 全穩定不翻轉。
- 入金 35 變體：Δeq 與 150 版完全相同（網格行為不變），只墊高權益基數、max_dd 比例下降（0.47→0.39），符合預期 = 入金純粹買安全邊際。
- **現狀場景警訊**：W1 上漲段 mult=20 Δeq +24.8 遠勝其他 —— 那是凍結的 0.58 淨多頭在漲勢的方向性收益，不是網格能力；同一凍結在 W2 下跌段 -33.6。**「不對沖、維持現狀」= 押方向**，兩段對照是最直接證據。
- 保真警語不變（FIDELITY_NOTES 12）：涉 seed 部分平倉，數字只看方向不當精確預測。
- **結論維持並強化：走 C → threshold_multiplier=40**；三種路徑型態下皆正、不靠方向、不踩邊界。

### ★ C 路線執行中（2026-07-12）：config 已改，等使用者重啟 + 入金 + 補空
使用者裁決走 C。**config 6 處已改完**（停機後直接編輯 JSON 不走終端選單、原子寫、備份 `config/trading_config_max.json.bak-20260712` 已 gitignore、`GlobalConfig.load()` 實載驗證通過）：
1-3. BNBUSDC `grid_spacing` 0.008→**0.003**、`take_profit_spacing` 0.004→**0.003**（把 bandit arm 0 覆寫出來的實盤實際值寫死成契約）、`threshold_multiplier` 20→**40**（有效 thr=0.8）
4-6. `bandit.enabled`→**false**（#13 BD1-3 不學習+靜默切 arm 尾部風險）、`leading_indicator.enabled`→**false**、`dgt.enabled`→**false**（回測驗證的是零增強純網格；`bot.py:342`/`:488` 單一 gate 確認關 master 即全關）
- `all_enhancements_enabled` 維持 false；動態網格判定**不開**：回測器強制增強中性（FIDELITY_NOTES 3）測不了、delta-neutral 後接刀風險已被對沖吸收、min_spacing 0.002 可能把間距收窄到沒測過的值。要開得先給 backtester 接 ATR 增強線再用 seed 工具驗。
- **副作用已知悉**：mult=40 同時把止盈加倍門檻推到 0.8（`decision.py:97` 一參數兩用，回測同一 decide() 已含此耦合）。

**剩餘步驟（使用者端）**：① 重啟引擎，核對面板 GS/TP=0.30%/0.30%、學習模組停用；② **驗收關鍵**：多頭側應重新出現網格掛單（0.58<0.8 解除裝死），殘留那張 sell LONG @603.05 RO 應被 cancel=True 接管重掛——若多頭側仍零掛單，停下來查，**先不要入金**；③ 入金 ~35 USDC；④ 補空 0.50（0.08→0.58）。

### ★ C 路線執行結果（2026-07-12 完成，最終狀態與計畫的偏差都有使用者裁決）
1. **重啟驗收全過**：14:51 重啟後 `decisions.jsonl` thr=0.8 生效、多頭進場單重現（104h 裝死解除）、舊 @603.05 凍結單被接管換成貼價單。
2. **入金 35 到帳**（錢包 184.6）。停機期間網格自己動過：空頭 0.08→0.04（那張 buy SHORT RO 成交）。
3. **補空執行**（我直接下單，marketable limit 貼買一分批）：0.18 @571.78 + 0.12 @572.22 成交，空頭 0.04→**0.34**。
4. **執行中發現新假旋鈕：config `leverage: 20` 從未推到交易所**（grep 證實引擎無 set_leverage 呼叫），交易所實際 **5x** → 保證金 4 倍於估算，第二批撞 -2019。改槓桿被權限系統擋（正確），使用者裁決**維持 5x**。
5. 5x 下補滿 0.58/0.58 需錢包 ≥207（現 184.6），使用者選 **B：不再入金，補到保證金剩 ~5 buffer 為止** → 最終 delta **+0.24**（原 +0.50 減半），非完全中性。
6. **最終**：多 0.58@690.29 / 空 0.34@571.75，權益 116.0，可用 6.1，**強平價 359→90.8**（尾部風險基本解除），雙側網格掛單正常。
7. **已知氧氣限制**：可用 6.1 在 5x 只夠網格再加 ~2 層（每層 0.02 押 2.29），空頭側連續進場會撞 -2019 斷續熄火（引擎斷路器會擋，不失控）。日後入金 ~25 可補滿剩餘 0.24 到完全中性。
8. UI 順手修：`ui.py:153-154` 持倉顯示 `.1f`→`.2f`（trivial，未 commit）。

---

## 舊任務定義存檔（已作廢）：#14 修 `dead_mode_price`（Plan track）
brainstorming 中發現 `dead_mode_price` 公式 `price×((long/short)/100+1)` 使失衡越大目標越遠（反向風控），且 `if entering or pending_tp<=0` 讓特殊止盈單只掛一次凍結失衡比例。**但議倉實測顯示這公式不是主因**（主因是 threshold 計價 + cross margin + 淨曝險），改它救不了現場。留待 threshold 重做後一併處理。

### ★★ 補資料完成，但**原三選項對照計畫已作廢** —— 前提被實證推翻

**資料已補齊**（本 session 完成，未 commit）：
- K 線 `2026-06-06` ~ **`2026-07-10`**（50199 根，`07-10` 檔 1239 根為當日部分資料）
- funding `data/funding/BNBUSDC.csv` 重抓，107 筆，涵蓋到 `2026-07-10 08:00 UTC`
- ⚠️ 下載前刪掉兩個**毒快取**：`BNBUSDC-1m-2026-07-06.csv` 只有 907 根（當日未過完就存檔，`download()` 的 `if output_path.exists(): continue` 永不回補）；`load_funding()` 的 `if path.exists(): return` 同樣不看區間。備份在 scratchpad。
- 事實：這些 kline 檔是 **Taipei 日界**（每檔 UTC 16:00 → 次日 15:59），因為 `download()` 用 `datetime(y,m,d).timestamp()`（本地時區）。與 `decisions.jsonl`（UTC）對時要差 8 小時。
- ⚠️ `07-10` 檔是部分資料，`download()` 的 skip-if-exists 會讓它**永遠停在 1239 根**。下次要延伸資料前必須先刪它。

### 真因（全部來自 `logs/decisions.jsonl` 73123 筆，非推理）
生產多頭 `long_position` **恆為 0.58**，`long_dead_mode=100%`，`buy_long_orders=0`，跨 104 小時零變動。

1. **裝死死鎖已修**（`60917cc`，`cancel=True` 接管殘留止盈單）。生產引擎 pid 28845 於 `07-10 11:57 Taipei` 重啟，跑的是修好的碼。
2. 重啟後 **12 秒**（`07-10 03:57:29 UTC`）掛出**全程唯一一張**多頭單：`sell long @ 603.05, qty 0.04, reduce_only`。`orders` 張數分佈 `{0: 73122, 1: 1}`。
3. 當時 `price=575.245` → 那張單要求 **+4.83%**。之後最高價 **578.25**，未成交。
4. **根因是 `dead_mode_price` 的公式**：`price × ((long/short)/100 + 1)`。`0.58/0.12=4.83` → +4.83%。日誌裡 `short` 曾低到 `0.02` → 公式要求 **+29%**。**失衡越嚴重，要求的出場漲幅越大** —— 反向風控。
5. 公式本有自癒設計（價漲 → 空頭加倉 → 失衡降 → 止盈價下移），但 `_decide_side` 的 `if entering or pending_tp <= 0:` 讓那張單**只掛一次**、凍結在掛出當下的失衡比例，自癒從未生效。

### 回測的獨立障礙：空倉起跑**到不了** threshold
`position_limit = 0.02×5 = 0.1` 之上止盈量加倍（出 0.04 / 進 0.02）→ 持倉被壓在 **0.28** 平衡點，`threshold=0.4` 永遠碰不到。實測（06-06~07-10，價格最大回撤 **14.83%**，632.23→538.45）：
```
mult=20   → final_equity 105.2423  trades 513  liquidated=False  max_dd 0.1009
mult=1e9  → 完全相同
max_long_pos 0.2800   max_short_pos 0.2800   dead_mode_pct 0.00%
```
**補再多資料都一樣。** 要行使裝死路徑，回測必須支援**注入初始持倉**（seed `long=0.58`）。

### 附帶發現（非阻擋）
- 回測**每根 K 線最多成交一張補倉單**（`_settle` 只有一個 `pend[side]["entry"]`）。實測漏掉 **6.4%** 的層數（219 次成交 / 本可 234 層；5.9% 的 bar 本可吃 ≥2 層）。實盤 tick 級追價會連吃多層。
- `Config.position_threshold=500.0` / `Config.position_limit=100.0` 在主路徑（`_run_terminal_ui_mode`）**從未被讀** —— `backtester.py:565-566` 一律由 multiplier 重算。又兩個假旋鈕（只有 legacy helper `:72-74` 讀它們）。

### 已作廢的舊計畫（保留理由說明，別再撿回來）
「補資料 → 跑 (a) 調高 threshold / (b) 關掉裝死 / (c) 開 GLFT 三選項對照」：
- (a) 只是讓倉位凍結在**更高**的位置，不碰出場問題
- (b) 關掉裝死 = 無上限補倉
- (c) 已由分析定案：`clamp(0.5,1.5)` + `max(iq*0.5, q)` 地板 ⇒ 生產 `gamma=0.1` 下只減 **8.7%**
- **三者都沒碰到真正的缺陷**（出場價公式 + 一次性掛單）

---

## 舊計畫存檔（已作廢，見上）：補資料 → 跑真正的三選項對照

**為什麼是這個，不是寫 Phase A-C 計畫**：
- **(c) 開 GLFT 已被分析回答** —— `glft_quantity()` 的 `clamp(0.5, 1.5)` + `compute_quantity` 的 `max(initial_quantity*0.5, q)` 地板 ⇒ 多頭開倉量**最多砍到一半、永不停止買入、永不賣出**。生產 `gamma=0.1`、`inventory_ratio` 中位數 0.871 ⇒ 實際只減 **8.7%**。回測會證實，但數學已先說了。且開它需要 `all_enhancements_enabled=true`，那會讓 bandit 開始覆寫 `gamma`（`bot.py:359-360`）。
- **(a) 調高 threshold 與 (b) 關掉裝死，現在就能測** —— `threshold_multiplier=1e9` 在功能上等價於關掉裝死（實測 `final_equity`/`trades` 與 `mult=20` **完全相同**，因為根本沒觸發）。**不需要 Phase A。**
- **唯一卡住的是資料。**

### 資料缺口（精確）
| | 範圍 |
|---|---|
| 現有 K 線 | `data/futures/um/daily/klines/BNBUSDC/1m/` 共 31 檔，`2026-06-06` ~ **`2026-07-06`** |
| 生產決策日誌 | `logs/decisions.jsonl`：**`2026-07-05 23:36`** ~ **`2026-07-10 20:34`** ← 多頭 `in_dead=100%` 的那 4 天 |
| **缺口** | **`2026-07-06` ~ `2026-07-11`**（含單邊趨勢段） |

這 31 天內單側持倉從未超過 `0.02 × 20 = 0.4`，**裝死模式一次都沒觸發** ⇒ `mult=20` / `mult=40` / 關掉裝死三者數字**完全相同**。問題出在那 4 天，而那 4 天不在資料裡。

### 具體步驟
1. **補抓 K 線**（會連網、寫 `data/`。生產引擎不讀 `data/`，安全）：
   ```python
   from backtest.data_loader import DataLoader
   DataLoader().download("BNBUSDC", "2026-07-06", "2026-07-11", interval="1m")
   ```
   簽名見 `backtest/data_loader.py:367`。抓完確認 `get_date_range("BNBUSDC","1m")`（`:304`）涵蓋到 07-10。
   ⚠️ funding 快取（`data/funding/BNBUSDC.csv`）也要跟著延伸，否則尾段 `rate=0`（FIDELITY_NOTES 第 (7) 條已揭露）。

2. **確認裝死模式在新資料裡真的會觸發**（否則白做）：
   跑一次 `mult=20` vs `mult=1e9`，斷言 `trades_count` / `final_equity` **不再相同**。若仍相同 → 停下來查為什麼（可能 `initial_quantity` 或 `direction` 設定與生產不符）。

3. **跑對照**。生產有效參數（**注意不是 config 裡的值**）：
   ```
   grid_spacing = 0.003, take_profit_spacing = 0.003   ← bandit arm 0，實盤實際值
   initial_quantity = 0.02, leverage = 20, direction = "both"
   limit_multiplier = 5.0, initial_balance = 100
   fee_pct = 0.0002 (maker), funding_enabled = True
   ```
   `scripts/cost_sensitivity.py` 已支援 `--threshold-multiplier 5,10,20,40,1e9`。

4. **驗收指標（spec §7 分層，不得混用）**：
   - **主**：`liquidated`（布林一票否決）、`final_equity`、`max_drawdown`
   - **次**：`funding_paid`、`dead_mode_pct_long/short`（**尚未實作，屬 Phase A**）、裝死 TP 成交率
   - **禁止作為優化目標**：`trades_count` / `realized_pnl`（martingale 假象，攤平策略的已實現獲利恆為正）、`sharpe_ratio`（1m 報酬 ×√525600，自相關嚴重膨脹；強平的回測實測 `-486.97`）

5. **判讀規則**（spec §8 Phase D）：
   - 任一選項 `liquidated=True` → **該選項淘汰**，不論其他數字
   - 排序若在 fee ∈ {2,4} bps × slippage ∈ {0,1,2} bps 範圍內**翻轉** → **不得下結論**
   - 排序未翻轉也要看**差距 vs 成本擾動**。舊資料實測：最佳/次佳差距 `0.120 → 3.219`（放大 **26.9 倍**），低成本端只差 `0.26` ⇒ **落在雜訊裡，結論脆弱**
   - `threshold_multiplier` 響應曲面**非單調**（`mult=10` 劣於 5 與 20）⇒ 輸出 sensitivity curve，**不要只報單點最佳值**

### 這一步的前置事實（別重新發現一次）
- **實盤間距不是 config 的值**。`bot.py:355-358` 在 `bandit.enabled=true` 時**無條件覆寫** `grid_spacing`/`take_profit_spacing`。生產 60001 筆決策日誌實測恆為 `0.003/0.003`（arm 0），而 config 寫 `0.006/0.004`。已有測試釘死（`tests/test_bandit_overwrites_config.py`）。
- **實驗前置條件**：若結論要套用到實盤，必須 `bandit.enabled=false` + config 顯式設定受測間距，否則 live 與 backtest 跑的不是同一個策略。
- **`threshold_multiplier` 一參數兩用**：`decision.py:97` 的止盈加倍條件也讀 `position_threshold`（`opposite_position >= position_threshold`）。調高它會同時延後裝死觸發**並且**改變對手側止盈加倍時機。optimizer 無法歸因 ⇒ 需要 ablation（把加倍門檻解耦成獨立參數）。

### 未 commit 的東西
- `tasks/progress.md`（本檔）、`tasks/lessons.md`（untracked，新增 3 條通用教訓，共 27 條 189 行）
- branch `feat/backtest-engine-fidelity` **未 merge、未 push**（27 commits，dual-review `Ship as-is`）
- `lessons.md` 已超過 workflow 的 ~50 行門檻。六條同族（假旋鈕 / 死路徑 / 被覆寫 / 重複 class / 未接線欄位 / 隱含不變式，皆為「靜態結構看起來成立、執行期不成立」）可合併成一條通則。

---

### #12 起因：blocker 是假的，但挖出六個真缺陷
progress.md 原記載「backtester 不共用 decision.py」→ **錯**。主路徑 `_run_terminal_ui_mode` 早已完整呼叫 `decide()`（`backtester.py:696-715`）。那句話描述的是 `_legacy_grid_decision`（`initial_quantity<=0` 才走的死路徑）。

真正的問題是**回測引擎本身不可用**。六個缺陷，每個都有實證：

| 缺口 | 修法 | 實證 |
|---|---|---|
| **G4** 撮合兩個錯 | `high`/`low` 判穿越、成交於**掛單價** | 44107 根真實 K 線：漏掉 **48.5%** 成交、每筆送出 **10.38 bps** 幻覺價格改善（= 所建模 slippage 1bp 的 10 倍） |
| — 止盈越權平倉 | clamp 到本根 entry 結算前的持倉 | `trades_count` 4 → 2 |
| **G8** 權益漏算 margin | `equity = balance + open_margin + unrealized` | 恆等式缺口 `0.00e+00`（原 988.2 vs 正確 1007.5） |
| **G6** 無強平建模 | `should_liquidate` + `liquidated` 一票否決 | 必爆組 `final_equity` **-1853 → 強平於價格跌 19%** |
| — 安全檢查靜默失效 | 無效輸入 `raise` 而非回 `False` | `price=0` 曾讓強平檢查恆回「安全」 |
| **G7** 成本非方向中性 | `fee_pct` taker→maker + `FIDELITY_NOTES` 誠實化 | 三個 grep 驗收 + 自動化守門測試 |
| **G5** bandit 覆寫間距 | 釘死為測試（不修 bandit） | 生產 60001 筆：實盤恆 `0.003/0.003`，config 的 `0.006/0.004` **從未生效** |
| — 強平只看收盤價 | 改用盤中最不利價 | `(low=60, close=93)` 修前 `liquidated=False`、修後 `True` |
| — `max_drawdown` 只看收盤價 | 谷底取盤中最不利權益 | wick 使其由 `0.000700 → 0.010406` |

### spec §7「一票否決」的六個現場（不變式橫跨模組）
`optimizer.py` ✅（`eligible` 過濾 + `liquidated` 主排序鍵）、`cost_sensitivity.py` ✅、`smart_optimizer.py` ✅（`TrialPruned`）、`optimizer._calculate_param_importance` ✅、`web/services/backtest_service.py` ✅（警告前置進 `notes`）、`web/pages/` 免改（`iloc[0]` 天然安全）。

### dual-review 戰果（dev-rules 強制）
- 內部：4 輪 task review + 1 輪 opus whole-branch → **0 個 Important**
- 外部：4 輪 fresh-context → **1 Critical + 4 Important**，全部實測重現
- Critical 是**我們自己的 fix 引入的**：`TrialPruned` 讓 prune 從罕見變常態，打破 `self._trials[i].trial_number == i` 這個沒寫下來的不變式 → `IndexError` 殺死 `run_smart_optimization`
- verifier（fresh-context）：ACCEPT 7/7

### ★ Phase D 的前置阻礙（Phase 0 中途發現）
真實 K 線只到 **2026-07-06**，而生產出問題的期間是 **07-06 ~ 07-10**。實測 `threshold_multiplier` 掃描：
```
mult=5  → final_eq 104.458   trades 153
mult=10 → final_eq  98.912   trades 257
mult=20 → final_eq 103.428   trades 490   ← 生產值
mult=40 → 與 mult=20 完全相同
關掉裝死 → 與 mult=20 完全相同
```
**這段資料裡單側持倉從未超過 0.4，裝死模式從未觸發。** `threshold_multiplier` 在生產值附近是**惰性參數** —— 直接跑 optimizer 會得到「它對績效無影響」，可能被誤讀成「裝死模式關掉也行」。且響應曲面**非單調**（`mult=10` 劣於 5 與 20）。

**Phase D 前置**：用 `backtest/data_loader.py` 補抓 BNBUSDC 1m 至 >= 2026-07-10（含單邊趨勢段）。

### 成本敏感度（`scripts/cost_sensitivity.py` 實跑）
排序未翻轉（`mult5` 全程最佳），但最佳/次佳差距被成本擾動放大 **26.9 倍**（0.120 → 3.219）。**「排序沒翻轉」不等於「結論穩健」** —— 差距（0.26）小於成本擾動造成的變化（3.1）。

### #12 Follow-up（非阻擋）
1. `smart_optimizer.py:743` `study.best_value if hasattr(...)` —— `hasattr` 對 property 恆回 `True`（只吞 `AttributeError`），multi-objective study 存取 `best_value` → `RuntimeError`。**既有 bug，與 prune 無關。** ⇒ `web/pages` 的 NSGA-II 多目標選項是**死路徑**。正確寫法 `try/except RuntimeError`。
2. `margin_usage` 對 `equity<=0` 回 `inf` 即使零倉位（純觀測欄位）。
3. `optimizer` 三個 sweep 方法死碼、未加 `ValueError` 保護（docstring 已警告）。
4. `smart_optimizer` 的 `except Exception` 範圍過寬（既有）。
5. **#13 bandit 三缺陷**：BD1 `trade_count_since_update`/`pending_trades` 未持久化（126 次啟動只有 6 個 run 累積到 `update_interval=10` → **#6 的持久化實務上只存了一個永不改變的種子**）；BD2 `_cold_start_init` 注入捏造的 reward `0.5`；BD3 `load_state:521` 的 `.get('current_arm_idx', 0)` 靜默重置 arm。
6. Phase A-C 的實作計畫尚未撰寫（spec 已定案，見 §8）。

### 前次任務存檔
### #10-B 已判死（不做 hard_stop）
使用者裁決：對沖 + **isolated margin**（每 symbol 最大虧損被交易所結構性封頂），不設 PnL 硬止損，要讓網格掛著慢慢補。頁4 那三欄唯讀揭露（`hard_stop_enabled`/`max_loss_pct`/`max_position_loss_pct`）應移除或改成明確的設計聲明，避免未來又被當成「未完成功能」撿回來做。
- 若日後要做風險監控，正確方向是**強平距離監控**（`fetch_positions()` 回傳的 `liquidationPrice` 目前在 `sync_service.py:60-73` 被丟棄），純唯讀 + 通知，不碰下單路徑，也不需 backtest 驗證（backtest 不共用 RiskMonitor）。
- 另一個既有缺口：`check_and_reduce_positions()` 觸發條件是多空**同時**超標（AND），**單邊崩盤不會觸發減倉**。

### Recently Completed
**#10-A config 寫入原子/merge/跨進程鎖（2026-07-08，已 merged+push）**：`main` 1b2dd59..310f091（11 commits），全套 310 passed（294 基線+16 新）。抽 `grid_engine/config_io.py` 共用底層（merge-preserve + pid tmp 原子寫 + fcntl.flock sidecar 鎖），`GlobalConfig.save()` 與 `web/services/config_store.py` 皆 delegate，兩份邏輯合一。修三缺陷：撕裂讀（os.replace）、抹 extras（merge-preserve）、lost-update（flock 序列化 RMW）。順修 config_store 固定-tmp + web 側無跨進程鎖潛伏 bug。
- SDD 全程：4 實作 task 全 spec ✅ review clean → opus whole-branch（Ready to merge）→ dual-review（Ship as-is）。
- 併發守衛實證：flock-off 245/300 key 遺失、flock-on 0。Monkey：真實 config round-trip 零遺失、損毀檔 raise 不截斷。
- **dual-review R1（獨立）抓到 opus final review 漏掉的 Critical**：`config/.gitignore` 漏 backup 副檔名 → 含真實 api_key/secret 的 `.bak` 未被擋（root gitignore 只擋 `*.json`），已補 `*.bak*`（check-ignore exit 0 驗證）。
- **重要 accepted risk**：flock 只保護 top-level/symbol 內未知欄位；**symbols 集合的併發新增/刪除仍 last-writer-wins**（呼叫端持鎖外過期快照時丟失/復活 symbol）。已於 config_io docstring 誠實化。

### TODO（下一步候選，優先序）
1. ~~**#10-B hard_stop**~~ — 已判死，見上方「#10-B 已判死」。
2. **symbols-set 併發 race**（#10-A 衍生）：終端選單持鎖外過期快照 → 併發新增/刪除 symbol 丟失/復活。修法：save 前 reload-and-remerge，或**砍終端 config 選單**（單一 writer 徹底消除 dual-writer 前提，與既有「砍終端 config 選單」backlog 合流，較根治）。
3. trading_mode 收編 engine schema（等 #4 驗收後）。
4. 頁3「clamp 後寫回 session」模式未全站排查。

### Blockers
- **#4 人工 Task 10**：GCE 部署 ≥24h replay zero-diff，卡人工前置（非我可推進）。

---

**#9 web/ 遷移完成（2026-07-06）**：23 commits c924947..1b2dd59，全套 294 passed（270 基線+24 新）。SDD 11 task + final whole-branch review（Ready to merge）+ verifier（ACCEPT 8/8）+ dual-review（Ship as-is）全收斂。**已 push**（2026-07-06 本 session）。剩 #4 人工 Task 10（GCE 部署 ≥24h replay zero-diff）照舊卡人工前置。

### #9 成果摘要
- web 全遷新系統：新增 `web/services/`（config_store merge-preserve+原子寫 / history_reader 容錯讀 / backtest_service 黃金映射+雙優化器歸一）+ tests/web/ 24 測試；砍 bot 生命週期；頁1 降級讀 logs/decisions.jsonl；頁2-4 改接；刪 core/ ui/ exchanges/ main.py（~3千行）；config/models.py 瘦身留 4 indicator config。
- 過程抓到的真 bug（部分現存生產）：Config.to_dict 漏序列化 4 欄致網格優化掉 legacy 引擎（同源 bug 曾影響 as_terminal_max optimize_params，已修）；頁3 exchange_type AttributeError；每單數量 min_value=1.0 擋掉所有真實配置；頁3 clamp 寫回 session 致 check_config_updated rerun 風暴（整頁互動失效，改 mtime 比對修）；頁4 風控 tab hard_stop 回歸（唯讀揭露）；頁4 缺同步防護（lost-update 窗口）。
- Task 8 對比裁決：新舊引擎成本對齊後 return 方向仍相反 → 歸因 #4 刻意撮合重設計（新=實盤等價），使用者裁決接受新引擎為基準（tasks/notes.md）。
- **重要事實**：生產 JSON 已是純 engine schema——trading_mode/hard_stop/exchange_type 在 #9 前就被引擎終端選單 save() 抹掉。merge-preserve 是正確防禦碼但只守 web 側。

### #9 Follow-up backlog（非阻擋）
1. **#10 候選（實盤安全，優先）**：as_terminal_max 終端選單 `GlobalConfig.save()` 非原子+抹 extras → 改走 merge-preserve+原子寫或砍終端 config 選單；同捆「生產 grid_engine 補 hard_stop 實作」（現況硬止損無效，spec 揭露）。
2. trading_mode 收編 engine schema（#4 驗收後）。
3. 頁3「clamp 後寫回 session」模式未全站排查；scripts/compare_backtest_engines.py 的 core import 已死（plan 明定保留歷史）；各 task Minor 累積見 .superpowers/sdd/progress.md。


### #9 brainstorming 補充事實（2026-07-06，修正前置調查）
- 前置調查兩點已過時：core/ 現只剩 bot/backtest/strategy 三檔（無 path_resolver/logging_setup）；coin_selection 的 core import 是 try/except 死分支（模組不存在，恆走 fallback），Phase 2 清死碼即可。
- tests/ 零依賴舊系統（270 tests 全綁新系統），刪舊碼無測試連坐。
- 兩套系統共用 `config/trading_config_max.json`（grid_engine/utils.py:29）；grid_engine 落地檔：logs/decisions.jsonl、logs/bandit_state.json、log/*.log（snapshot 僅記憶體）。
- grid_engine 是 ccxt 直連 Binance（ws_client.py:79 fapiPrivatePutListenKey），exchanges/ adapter 層只剩頁4 在用。

### #9 前置調查 — web/舊系統依賴盤點（2026-07-05，scout 完成，未動碼）
- **web/ 對舊系統的依賴只有 5 個 import 點**：`web/state.py:21`(config.models.GlobalConfig) + `:148`(core.bot.MaxGridBot)、`web/pages/2_⚙️_交易對管理.py:26`(config.models.SymbolConfig)、`web/pages/3_🔬_回測優化.py:29,31`(SymbolConfig + core.backtest.BacktestManager)、`web/pages/4_🛠️_設定.py:37`(exchanges list/display_name)。app.py/theme.py/sidebar.py 乾淨；頁1 只間接經 state.py。
- **回測頁遷移缺口（≤5 條初判）**：① BacktestManager 統一抽象消失，新系統要分別接 `backtest/data_loader.py:DataLoader`(download:376/load:158) + `backtest/backtester.py:GridBacktester.run():504`，需包裝層或改寫（★★★）；② `get_available_dates()` 回傳 List[str] vs 新 `get_date_range():313` 回 (start,end) 元組，頁內邏輯要改（★★）；③ `optimize_params()` 拆成 optimizer.py/smart_optimizer.py 兩套，接口不同（★★★）；④ SymbolConfig → backtest/config.py:Config 參數映射需驗證（★★）；⑤ Monte Carlo 段（line 929-1013）**已經在用新系統 GridBacktester**，只需驗證（★）。
- **重要：刪 core/ 不只是 web 的事** — 新系統自身也踩著 core/：`backtest/data_loader.py` import `core.path_resolver`、`coin_selection/ws_provider.py` import core.logging_setup/error_handler/constants、`indicators/*.py`(dgt/funding/leading/bandit) import config.models、`ui/menu.py` import core.bot+core.backtest（舊終端 UI 整個要一起淘汰或遷移）。#9 範圍應含這些工具模組的去留（path_resolver/logging_setup 等宜先搬出 core/ 成獨立 utils）。
- web 啟動入口 `streamlit run web/app.py`（README 方式2）；健檢 `scripts/check_web_system.py`（45 項，前次 37 pass/4 fail 非阻塞）。scout 未實跑 web，可用性數字來自 WEB_TEST_REPORT.md（2026-01-13，偏舊）。

### #8 清理 — 完成（2026-07-05）
- 全套 **270 passed**（268+2 新測試）。reviewer(opus) LGTM 無 must-fix + verifier ACCEPT 5/5（含 revert 驗證新測試會紅、還原乾淨）。
- **asyncio task 生命週期修復（實質改動）**：`order_executor.py` 斷路通知 task 加 done-callback 完成自移除（修長跑累積洩漏）；`sync_service.py` 風控通知 fire-and-forget 原本裸 create_task 無參照（GC 可能在執行前回收），改掛共享 tasks list + 自移除，`SyncService.__init__` 新增必要參數 `tasks`；`bot.py` stop() 迭代改 `list(self.tasks)` 快照（callback 會在 await 期間變動 list，直接迭代會跳元素）。組裝斷言補 `sync_service.tasks is bot.tasks`。
- **記錄修正（#7 follow-up 三項是誤判，未改碼）**：`grid_engine/backtest.py` 並非「無人引用」——`as_terminal_max.py:11`（live 入口）與 `tests/test_backtest_manager_delegation.py` 都在用（#4 Task 8b 才修過它），**保留不刪**；`web/state.py:54` `bot.reload_config` 合法——web 用的是 `core.bot.MaxGridBot`（state.py:148），core/bot.py:405 有此方法；check_web_system 的 required_methods 檢查的也是 core bot，同樣合法。
- **頂層清理**：`test_web_system.py`/`test_symbol_conversion.py` 是 print 式手動診斷 script（測舊 core/web 系統），git mv 到 `scripts/check_web_system.py`/`scripts/check_symbol_conversion.py`（去 test_ 前綴防 pytest 收集；path 修正後實跑通）；刪生成物 `web_test_report.json` + gitignore。刪 `_handle_order_update` 的 sym_config dead assignment。
- Follow-up（非阻擋，歸 #9 或不修）：stop() 快照後 in-flight 下單失敗理論上可再 append 通知 task 逃出 cancel（best-effort 通知，影響極小）；check_web_system.py 的報告仍寫 repo 頂層（已 gitignore）；scripts 診斷依賴的 streamlit 不在 uv 環境、exchanges adapter 缺 create_order（舊系統既有，#9 一併處理）。

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
- [x] **#8 (P2) 清理** — 完成（2026-07-05），270 passed，reviewer LGTM + verifier ACCEPT。範圍修正：grid_engine/backtest.py **保留**（live 入口 as_terminal_max.py 在用，「無人引用」是誤記）；頂層診斷 script 移 scripts/check_*.py；task 生命週期修復（GC 風險 + 累積洩漏）。詳見上方 Current Task。
- [~] **#9 (長期) 淘汰舊系統**：前置調查完成（2026-07-05，見 Current Task）——web 依賴面 5 個 import 點 + 回測頁 5 缺口初判；**範圍擴大：backtest/coin_selection/indicators/ui 也依賴 core/，需一併處理**。下一步：brainstorming 定遷移方案（Plan track，需使用者確認）

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
