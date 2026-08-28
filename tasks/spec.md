# 當前任務 Spec：hedge 模式啟動守衛 + `ps='BOTH'` 事件防禦網

（前一個任務「價格時效守衛」的 spec 見 `docs/superpowers/specs/2026-08-24-price-staleness-guard-design.md`，已 merge 上線。）

**任務來源**：`tasks/progress.md` backlog「`_handle_order_update` 對 `ps='BOTH'` 兩支都不命中
⇒ 掛單計數不重置也沒 warning，與 account handler 不對稱（pre-existing）」。

## 任務定義被修正過（重要）

backlog 原記的定位是「補一個 BOTH 分支」。查證後定位錯誤，真缺口在上游：

- **已量過（讀 code）**：`grid_engine/order_executor.py:90-91` 對**每一張**單無條件帶
  `params['positionSide']`（只要呼叫端給了 `position_side`，而網格路徑
  `bot.py:557` 一律給）。
- **已量過（ccxt 4.5.32 原始碼 `binance.py:12762-12795`）**：`fetch_position_mode()`
  回傳 `{'info': <fapi 原始回應>, 'hedged': <safe_bool(dualSidePosition)>}`。
  `safe_bool` 在欄位缺失時回 **`None`**，所以 `mode['hedged']` 不會 KeyError，
  但可能是 `None`（未知）而不只是 `True`/`False`。
- **推測（未實測，需標註）**：Binance USDⓈ-M 的 position mode 是**帳戶層**設定，
  one-way 模式下帶 `positionSide` 的下單會被拒（-4061）。此點未對交易所實測，
  但 `_check_hedge_mode` 的存在本身即是這個前提的既有證據。

⇒ 若帳戶真的處在 one-way 模式，這隻 bot **一張單都下不出去**，只會一路撞
`_register_order_failure` 直到斷路；不會有 `FILLED` 事件，因此
`_handle_order_update` 的 BOTH 分支是**永遠到不了的路徑**。在那裡「補功能」等於
做 one-way 模式的半吊子支援。

真缺口是 `grid_engine/bot.py:227-236` 的 `_check_hedge_mode`：
`except Exception: pass` 吞掉一切、切換 `dualSidePosition` 後**不複驗**，於是
bot 帶著未經證實的模式假設繼續啟動。Binance 在有持倉/掛單時會拒絕該切換。

## Goals

1. **啟動時硬性確立 hedge 模式**：確認不了就不啟動。
   `_check_hedge_mode` 改為在下列任一情況 `raise`：
   (a) `fetch_position_mode` 拋例外；(b) `hedged` 為 `None`（未知）；
   (c) 切換 `dualSidePosition=true` 後複驗仍非 hedge。
   （使用者 2026-08-28 裁決：一律 raise 停機，不寬容「查不到」。）
2. **失敗要說話**：raise 的訊息要能分辨上述三種情況。
3. **`_handle_order_update` 的 `ps` 判讀補齊防禦網**（不是功能支援）：
   `BOTH` 與任何未知值 → 節流 `logger.warning` + 本筆不套用，
   不重置掛單計數、不餵 bandit、不呼叫 `adjust_grid`。
4. 修掉 `bot.py:796` 的誤分類：`trade_side = 'long' if ps == 'LONG' else 'short'`
   會把 `BOTH` 記成 `short` 餵進 `bandit_optimizer.record_trade()`，
   汙染學習資料且靜默。

## Non-goals

- **不支援 one-way 模式**。不改 `order_executor.place_order` 的 `positionSide` 傳法，
  不做「偵測到 one-way 就改用無 positionSide 下單」的降級路徑。
- 不動 `_handle_account_update`（`bot.py:711-741`）——它已有 `BOTH` 分支與未知值
  warning，本次只是讓 order handler 對齊它。
- 不動 `run()` 的錯誤處理管線（見下方「已量過」——現成的就夠用）。
- 不加自動重試 / 自動修復持倉以便切換模式。
- 不順手重構 `_handle_order_update` 的其餘部分。

## Security / Safety constraints

- **本次改動不得新增任何下單、撤單、改倉行為**。唯一新增的外部呼叫是
  `_check_hedge_mode` 內對 `fetch_position_mode` 的**唯讀**複驗查詢
  （既有的 `fapiPrivatePostPositionSideDual` 切換呼叫維持原樣，不新增第二次切換）。
- **raise 的爆炸半徑已量過**：`_check_hedge_mode` 在 `bot.py:run()` 的 try 內
  （`await self.gateway.call(self._check_hedge_mode)`），例外由 `bot.py:865-871`
  接住 → `logger.error` + `notify_crash` 一封 + `state.running = False`
  + `gateway.shutdown()` + `return`。**是乾淨返回而非行程崩潰**，所以不會觸發
  docker restart policy、不會產生重啟迴圈與 Telegram 轟炸。本次不得改動這段。
- 複驗用的 `time.sleep` 只允許出現在 `_check_hedge_mode` 內：該函式是**同步**的、
  跑在 `gateway.call` 的 worker thread，不阻塞 event loop。複驗上限 3 次、間隔 1s
  ⇒ 啟動路徑最壞多耗 ~3s。不得改成 `asyncio.sleep`（函式非 async）。
- `_handle_order_update` 對 `BOTH` 早退後不呼叫 `adjust_grid`。
  ⚠️ **2026-08-28 修訂**：原文寫「這是刻意的，帳戶模式錯誤時重掛網格只會製造
  更多被拒的單」——**該宣稱已被最終 review 證偽**。`_handle_ticker` 對每筆
  bookTicker 都無條件呼叫 `adjust_grid`，且 `sync_service` 的掛單統計只認
  LONG/SHORT ⇒ one-way 下四個掛單計數會被 REST 寫成 0 ⇒ `_should_adjust_grid`
  恆為 True ⇒ 每 tick 都在重掛。早退**沒有**阻止重掛。
  早退真正避免的是「把成交套用到錯的一側」（掛單計數、bandit 分側統計），
  不是「阻止重掛網格」——後者由 ticker 路徑主導，本守衛管不到。
  「不在 one-way 下重掛網格」若要成立，得在 ticker 路徑加守衛，那超出本次
  spec 範圍（Non-goals：不支援 one-way 模式、不新增降級路徑）。
- warning 節流以 `ccxt_symbol` 為 key（比照既有 `_last_stale_log_at` 的做法），
  避免每筆成交刷一條。節流**只影響 log，不影響早退行為**。
- 不得改動 `sym_state.ws_seq += 1` 的位置與時機（`bot.py:810`）——它的原子性註解
  是上一個任務 C1 競態修復的一部分。BOTH 早退發生在遞增**之前**。

## 可判定驗收準則

1. **基線**：`851 passed / 2 skipped`（2026-08-28 於 `git archive HEAD` 乾淨快照實測，
   `HEAD == be51cf9`）。改動後全套不得退步，新增測試數量須明列。
2. 每條新守衛各配一個**實跑** mutation 並說出**紅在哪一行斷言**（不接受「應該會紅」）。
   至少涵蓋：
   - M-A：`_check_hedge_mode` 的 raise 換成 `pass` ⇒ 必須有測試紅
   - M-B：複驗那步刪掉（切換後直接視為成功）⇒ 必須有測試紅
   - M-C：`_handle_order_update` 的 `BOTH` 早退改成 `pass`（往下走）⇒ 必須有測試紅
   - M-D（**2026-08-28 修訂，見下方「Spec 修訂紀錄」**）：把 `BOTH` 從早退守衛的
     條件裡放行（`not in ('LONG','SHORT','BOTH')`）⇒ 必須有測試紅
3. 斷言字串必須是被測分支**獨有**的字面值（lessons 通則 8）；
   fixture 不得把待測維度壓成退化值（通則 3.3）——例如測 `BOTH` 早退時，
   掛單計數初值不得為 0，否則「沒重置」與「重置了」不可分辨。
4. Plan track + 命中 Red Team Protocol ⇒ 依序：`security-review` →
   fresh-context `verifier`（read-back + 實跑）→ `dual-review` 兩輪。
   **未拿到 `Ship as-is` verdict 不得標記完成**，verdict + 各輪 findings 計數落
   `tasks/notes.md`。
5. 改動需**重啟引擎**才生效。`tasks/progress.md` 須分開記「已 commit」與
   「已重啟生效」，後者的證據是：`ps -o lstart=` 的行程啟動時刻晚於
   `ls -lT grid_engine/bot.py` 的寫入時刻，且 log 有新行程的初始化區塊。

## Red Team（實作前列，dev-rules Red Team Protocol）

| # | 攻擊向量 | 防禦 |
|---|---|---|
| 1 | raise → container 重啟迴圈 + Telegram 轟炸 | 不需處理：`run():865-871` 已是乾淨返回（見 Safety 第 2 條，已讀 code 確認） |
| 2 | `hedged` 為 `None`（ccxt `safe_bool` 在欄位缺失時的回傳） | 顯式區分 `None`（未知）與 `False`（明確 one-way），兩者都 raise 但訊息不同 |
| 3 | `dualSidePosition` 切換在交易所端非同步生效，立刻複驗讀到舊值 → 誤停機 | 複驗最多 3 次、間隔 1s |
| 4 | 現況 `break` 在 `if` 內：沒切換時對每個 symbol 重複 fetch | position mode 是帳戶層 ⇒ 只取第一個 enabled symbol 查一次；無 enabled symbol 則跳過（bot 本來也沒事做） |
| 5 | BOTH 每筆成交刷一條 warning | 以 `ccxt_symbol` 為 key 節流；節流不影響早退行為 |

## Spec 修訂紀錄

**2026-08-28（寫實作計畫時發現，未進 code）：M-D 的定義修訂。**

原定義「`trade_side` 改回 `'long' if ps == 'LONG' else 'short'` ⇒ 必須紅」是一條
**無效 mutation**：加入 Goal 3 的早退守衛後，`BOTH` 永遠到不了 `trade_side` 那一行，
該 mutation 與原碼行為等價，不可能被殺死。若照原文執行，會得到一個「mutation 存活」
的假警訊，或更糟——為了讓它紅而把守衛拆掉。

改為「把 `BOTH` 從守衛條件裡放行」，守的是同一件事（BOTH 不得被記成 short 餵進
bandit），且可被殺死。它與 M-C 的鑑別差異：M-C 殺掉整道守衛，M-D 只殺掉 `BOTH`
這一個值——分開跑才證明得了守衛涵蓋 `BOTH` 而不只是未知值。

實作計畫：`docs/superpowers/plans/2026-08-28-hedge-mode-startup-guard.md`
