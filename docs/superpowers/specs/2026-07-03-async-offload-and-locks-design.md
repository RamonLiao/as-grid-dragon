# 設計：#2 REST 卸載至 thread + #3 並發鎖（2026-07-03）

## 問題

1. **#2 阻塞 event loop**：`place_order`（bot.py:343）、`cancel_orders_for_side`（bot.py:402）、`sync_all` 系列（bot.py:153-341）、`_keep_alive_loop` 的 listenKey 呼叫（bot.py:943-944）都是同步 ccxt REST，直接在 async 函數內執行。REST 慢時整個 event loop 卡死：WS 心跳斷線、ticker 積壓、Telegram 通知全部停擺。
2. **#3 無鎖並發**：一旦 #2 讓 adjust_grid 中途可 await，WS handler（`_handle_account_update` / `_handle_order_update`）與 `sync_all` 可在 adjust_grid 兩次 REST 之間插入，改寫 `SymbolState` 持倉/掛單計數與 `AccountBalance`，導致基於過期狀態下單。

## 技術路線（已定）

**to_thread 系 + 序列化**，不遷移 ccxt.async_support / ccxt.pro。理由：改動最小、REST 語意與現行完全一致（本來就是串行），風險最低；ccxt.pro 全換留給 #7 拆分時再評估。

## #2 設計

### REST 卸載
- Bot `__init__` 新增專用 `self._rest_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ccxt-rest")`。
  - **必須單 worker**：同步 ccxt 實例（共用 requests.Session）不保證 thread-safe；單 worker 天然序列化且保 FIFO。不用 `asyncio.to_thread`（走預設多 worker pool，會並發打同一 Session）。
- 新增 helper：
  ```python
  async def _rest(self, fn, *args, **kwargs):
      loop = asyncio.get_running_loop()
      return await loop.run_in_executor(self._rest_executor, partial(fn, *args, **kwargs))
  ```
- 轉 async 的方法（內部所有 `self.exchange.*` 呼叫改走 `self._rest(...)`）：
  - `place_order` → `async`（呼叫點：adjust_grid 643/659、_place_grid 713/715/733-738）
  - `cancel_orders_for_side` → `async`（呼叫點：641/657、725）
  - `sync_all` / `_sync_positions` / `_sync_orders` / `_sync_account` → `async`（呼叫點：_handle_ticker 766、run() 初始化 1019）
  - `_keep_alive_loop` 的 `fapiPrivatePutListenKey` / `_get_listen_key` 走 `_rest`
- 呼叫點全部補 `await`。#1 的 backoff/斷路器邏輯（在 place_order 內）不變，只是包進 async。
- **停機**：`stop()` 收尾 `self._rest_executor.shutdown(wait=False, cancel_futures=True)`（排隊中的取消、in-flight 自然結束；`wait=True` 會在 stop 時反過來卡住 event loop）；`place_order` 進入時檢查 `self._stop_event.is_set()`，已停機直接 return None。

### 範圍外
- `exchanges/binance.py:364/376`（舊系統 listenKey 同步呼叫）不動 — 該目錄已排定 #9 整體淘汰。
- 回測、web、UI 層不動。

## #3 設計

### 鎖結構
| 鎖 | 保護對象 | 持有者 |
|---|---|---|
| `self._symbol_locks[ccxt_symbol]`（per-symbol `asyncio.Lock`） | 該 symbol 的網格決策+下單序列、掛單計數/持倉的 REST apply | `adjust_grid` 全 body；`_sync_orders`/`_sync_positions` 的 per-symbol apply 區段 |
| `self._sync_lock`（單一 `asyncio.Lock`） | `sync_all` 防重入 | `sync_all` |

- **adjust_grid：skip-if-locked**。入口 `if lock.locked(): return`，否則 `async with` 持有整個 body。ticker 高頻推送，排隊只會積壓過期決策；跳過後下一 tick 自然以最新價重跑。
- **sync_all：skip-if-running**。`if self._sync_lock.locked(): return`，避免 REST 慢時多個 sync 疊加。
- **鎖序固定**：sync 路徑 = `_sync_lock` → `_symbol_locks[s]`（逐一取放）；adjust 路徑只取 `_symbol_locks[s]`。永不反向，無死鎖環。
- **WS handlers 不加鎖**：`_handle_account_update`/`_handle_order_update` 為純同步變異（無 await），在單線 event loop 內天然原子。加鎖反而讓高頻 WS 事件在 adjust_grid 的 REST 期間排隊、延遲入帳。

### 原子 apply 規則（防過期覆寫）
REST sync 一律「**fetch 在 executor thread、apply 為單一無 await 的同步區塊**」：
```
data = await self._rest(self.exchange.fetch_open_orders, ...)   # 可被插隊，無妨
async with self._symbol_locks[s]:
    # 以下無任何 await：讀 data → 寫 SymbolState，一氣呵成
```
已知殘餘風險（接受）：fetch 完成到 apply 之間若有成交，REST 快照短暫過期 — 10 秒週期 sync 自癒，且 #1 的 `position_adjust_cooldown` 已限制其傷害。此為現行行為，非本次退步。

## 攻擊向量與防禦（Red Team）

1. Ticker 風暴 + REST 變慢 → adjust_grid 積壓 → **skip-if-locked**，不排隊。
2. 死鎖 → **固定鎖序** sync→symbol，adjust 只拿 symbol。
3. ccxt Session 並發競爭 → **max_workers=1 專用 executor**。
4. 停機後 executor thread 仍送單 → 下單前查 `self._stop_event.is_set()` + `shutdown(wait=False, cancel_futures=True)`。
5. 舊 REST 快照蓋新 WS 事件 → 原子 apply + 週期自癒（既有可接受風險）。

## 測試計畫

新增 `tests/test_async_offload.py`（fake exchange 注入 `time.sleep`）：
1. **不阻塞**：fake `create_order` sleep 0.3s，並發跑一個 100ms 心跳 task，斷言心跳 jitter < 100ms（證明 event loop 未被卡住）。
2. **REST 序列化**：fake 記錄執行緒併發度，並發呼叫 place_order×N，斷言同一時刻只有 1 個在跑。
3. **adjust_grid 不重疊**：同 symbol 並發觸發 N 次，計數器斷言 body 執行區間互斥、且被 skip 的次數 = N-1。
4. **sync 防重入**：sync_all 並發×3，實際 fetch 只跑 1 次。
5. **停機競態**：stop 後 place_order 直接 return，不打 exchange。
6. **Monkey**：REST 拋例外時鎖必釋放（下一 tick 可再進入）；executor 內例外正確傳回 await 點；0/負 sleep、極端並發 50 tick。

回歸：既有 109 tests 全綠（place_order 等轉 async 後，test_order_guard.py 呼叫點需改 `asyncio.run`/await 包裝 — 屬測試配管調整，斷言不變）。

## 不做的事（YAGNI）

- 不遷移 ccxt.async_support / ccxt.pro。
- 不動 exchanges/、core/（舊系統，#9 處理）。
- 不給 WS handler 加鎖。
- 不做 REST 呼叫並行化（維持串行，與現狀等價）。
- 不順便拆 god class（#7）。
