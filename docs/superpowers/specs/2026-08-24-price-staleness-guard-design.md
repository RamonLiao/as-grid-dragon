# 價格時效守衛（price staleness guard）設計

日期：2026-08-24
狀態：設計已由使用者確認，待實作

## 0. 這項任務的定義被修正過（先讀這段）

backlog 原本記的是「`_handle_ticker` 無價格時效守衛」。**這個定位是錯的。**

守衛裝在 `_handle_ticker` 入口幾乎沒有作用——那一格剛收到 ticker，價格按定義就是
新鮮的。真正的缺口在**另一個消費端**：

- `adjust_grid` 有兩個呼叫端：
  - `grid_engine/bot.py:539` — `_handle_ticker`（每筆 bookTicker）
  - `grid_engine/bot.py:668` — `_handle_order_update`（ORDER_TRADE_UPDATE FILLED 之後）
- **668 那條路徑用的 `sym_state.best_bid` / `best_ask` 是上一次 ticker 留下的殘值**，
  中間隔多久完全不受控。
- `_grid_step` 的 `bot.py:405` / `419` 把 `best_bid` / `best_ask` **直接餵給
  `place_order()`**，零時效檢查。
- `SymbolState`（`grid_engine/state.py:12-50`）**完全沒有時間欄位**；WS bookTicker 的
  `E` / `T` 時間戳在 `bot.py:521-523` 就被丟掉。

⇒ 正確的設計是「**價格快照帶時效戳，在下單前判定**」，不是「在 ticker handler 加檢查」。

### 觸發面現況校準（避免高估急迫性）

生產上 userData stream 自 2026-07-12 起是死的 ⇒ **668 那條路徑目前幾乎不觸發**。
本守衛守的是兩種形態：

1. userData 復活之後（`bookTicker` 與 `userData` 是兩條獨立的存活性）；
2. `bookTicker` 單邊卡住但 `userData` 活著。

**log 裡沒有這兩種形態的實證**，優先度排序是推測性的。這一點必須留痕，不得在驗收
文案裡宣稱「修掉了一個已觀測到的生產事故」。

## 1. Goals

1. 價格快照帶上「本機抵達時戳」，成為 `SymbolState` 的一等欄位。
2. 下單前判定快照年齡；超過門檻則**跳過本次網格調整**，不下新單。
3. 過期是可觀測事件：節流 log + 每日摘要一行。
4. 每次決策把實際 `quote_age` 落進 decision log，**讓 5 秒這個猜測值日後能用實測收緊**。

## 2. Non-goals

- 不撤舊單（過期時做動作比不做危險——撤單同樣需要準確的價格認知）。
- 不做 REST 補價 fallback（新增失敗路徑，且 `-2015 Invalid API-key, IP` 被擋時
  REST 一樣掛，那正是最需要它的時候）。
- 不改 `ui.py` 的顯示路徑。
- 不把 watchdog 的牆鐘改成 `time.monotonic()`（那是 backlog 的獨立項；混進來會讓
  兩件事互相污染）。
- 不改 `backtest/tick_sim.py` 的決策邏輯（只加註解，見 §6）。
- 不引入 `PriceQuote` 打包型別（方案 B，見 §3）。

## 3. 方案取捨

| 方案 | 內容 | 判定 |
|---|---|---|
| **A（採用）** | `SymbolState` 加 `quote_at` 時戳欄位；gate 放在 `_grid_step` 頂端 | 一處 gate 守住兩個呼叫端；改動集中 |
| B（不採用） | 把 bid/ask/mid/時戳打包成不可分割的 `PriceQuote` dataclass，下單點只能拿 snapshot | 型別保證更強，但要改 `ui.py:133`、`bot.py` 六個讀取點、`tick_sim`，換來同一個 runtime 行為。對 bug-fix 級守衛是過早抽象 |

B 留作日後真的出現第三個下單路徑時再考慮。

## 4. 設計

### 4.1 時戳寫入

`_handle_ticker`（`bot.py:531-535`）設 `best_bid` / `best_ask` / `latest_price` 的**同一個
同步 block** 內加：

```python
state.quote_at = clock.now()
```

該 block 內無 `await` ⇒ 時戳與價格不可能分家。

### 4.2 時鐘來源（2026-08-25 修訂，security review Finding 1）

用 `grid_engine/clock.py` 的 **`guard_now()`**（守衛專用牆鐘），**不用 `now()`**，
也不用 `time.monotonic()`。

**原設計錯誤**：原本寫「用 `clock.now()`（可注入，回測相容）」。但 `now()` 是
**情境時鐘**——`backtest/backtester.py:715` 每根 K 線都 `clock.set_clock(lambda: epoch)`
替換模組級全域 `_now_fn`，整個回測期間都是歷史 epoch。而 live bot 與回測跑在
**同一個行程**（`as_terminal_max.py:1265` 的 daemon thread，TUI 主執行緒同時提供
「執行回測 / 參數優化」選單）。

⇒ 使用者一邊實盤一邊點回測時：`quote_at` 是牆鐘蓋的、`now()` 回歷史 epoch ⇒
`quote_age` 是巨大負數 ⇒ `age < 0` 那支條件對**每個 symbol、每個 tick** 觸發 ⇒
**全面停止下單，連成交後的止盈補單都停**（`_handle_order_update` → `adjust_grid`
同樣經過 gate），而持倉繼續累積；唯一訊號是每 symbol 每小時一筆 throttled warning。
本守衛上線前，同樣的時鐘替換只讓 ATR/funding 計時器軟偏移，**不會停止下單**。

**修訂後的規則**：守衛量的是「訊息**實際抵達本機的牆鐘時間**」，`now()` 量的是
「情境時間」。這是兩個**不同的物理量**，原設計把它們混為一談是分類錯誤；分開不是
繞路，是修正分類。`clock.py` 因此增設**不被 backtester 替換**的
`guard_now()` / `set_guard_clock()` / `reset_guard_clock()`（後兩者僅供測試注入）。
`now()` / `set_clock()` / `reset_clock()` 的語意與既有呼叫端**一律不動**
（`reporting.py` / `enhancements.py` / `sync_service.py` / watchdog 保持現狀，
呼應 §2 non-goal「不改 watchdog 的牆鐘」）。

守衛的**三處**必須一致用 `guard_now()`，否則就是另一種混用：
1. `_handle_ticker` 的 `quote_at` 蓋章；
2. gate 的 `quote_age` 比較；
3. `_note_stale_quote` 的節流計時。

牆鐘（而非 monotonic）的取捨不變：
牆鐘風險已被 gate 的形狀吸收，這是明列的已知取捨：

- 時鐘往後跳 → `age` 變大 → 擋單（**安全側**）。
- 時鐘往前跳 → `age` 為負 → 也擋，且下一筆 ticker 重新 stamp 即自癒。

### 4.3 Gate 落點與形狀

放在 `_grid_step` 頂端，**緊接現有的 `price <= 0: return` 之後**（同一形狀）：

```python
age = clock.guard_now() - sym_state.quote_at
if sym_state.quote_at <= 0 or age < 0 or age > self.config.max_price_age_sec:
    self._note_stale_quote(ccxt_symbol, age)
    return
```

**為什麼 `_grid_step` 是正確的咽喉點**：`adjust_grid` 的兩個呼叫端都經過它，且
per-symbol lock 已在外層（`bot.py:335-338`）。

**為什麼 early-return 在這裡語意安全**（dev-rules L5 檢查）：`_grid_step` 在下單之前
還做兩件事——DGT 邊界 `check_and_reset(ccxt_symbol, price, ...)` 與 bandit 參數套用
——**兩者都吃 `price`**，價格不可信時本來就不該跑。跳過不遺失狀態（下一筆 ticker
會補做）。

#### 4.3.1 例外：緊急減倉必須在 gate 之前（2026-08-24 修訂，Task 3 review 發現）

**原設計錯誤**：只檢查了 DGT 與 bandit，漏了 `risk_monitor.check_and_reduce_positions`
（原本落在 gate 下游）。

`risk_monitor.py:66-107` 實查結果：該函式**完全不消費價格**——觸發判斷只用
`sym_state.long_position` / `sym_state.short_position` / `sym_config.position_threshold`，
且下的是 `place_order(ccxt_symbol, 'sell', 0, qty, True, 'long', 'market')`，
**price 參數字面上是 `0`，市價單**。

⇒ 把它關在守衛後面是**方向錯誤的**：守衛的契約是「不要用不可信的價格下單」，
而市價平倉不消費那個價格。實際後果是在「ticker 斷線、userData 仍活著、掛單持續
成交、雙邊持倉往上爬」這個**最需要風控的情境**下關掉風控（60 秒冷卻的減倉在整個
斷線期間永不觸發）。

**修訂後的規則**：`check_and_reduce_positions` 上移到 `if price <= 0: return` 之後、
gate 之前。搬移的語意安全性已檢查：它讀的三個值都不被 DGT / bandit 觸碰（那兩者
動的是 `grid_spacing` / `gamma` / 邊界）。

**通則（寫死在此，避免重演）**：判斷「這一格該不該被時效 gate 擋住」的準則
**不是「它在 `_grid_step` 裡的位置」，而是「它消不消費 `price`」**。不消費價格的
副作用（市價風控平倉）必須留在 gate 之前。

### 4.4 config

`GlobalConfig` 新增：

```python
max_price_age_sec: float = 5.0  # 價格快照最大可用年齡（秒），0 = 關閉守衛
```

正規化照現有 `_parse_position_adjust_cooldown` 的形狀（`grid_engine/config.py:261-268`）：
非法 / 非有限 / 負值 → fallback `5.0`；**`0` 是合法的「關閉守衛」值**。
需同步加進 `GlobalConfig.to_dict()` 與 `from_dict()`。

gate 每次讀 `self.config.max_price_age_sec`、不快取 ⇒ TUI 的「設定即時套用」
（`_push_config_to_bot`）改了門檻立即生效。

**預設 5 秒是猜測值**：現有 `logs/decisions.jsonl` 只在走到 `decide()` 那格才寫
（近 6 小時 BNB/USDC 僅 35 筆、p50 間隔 242 秒），那是決策頻率不是 ticker 抵達頻率；
repo 裡沒有任何地方記錄 ticker 抵達時間 ⇒ **門檻無法從現有 log 推導**。故先設保守值
並裝儀器（§4.6），一週後用實測分佈收緊。

### 4.5 告警

`_note_stale_quote(ccxt_symbol, age)`：

- per-symbol 累計計數；
- 節流 log，沿用 watchdog 的節流 pattern（3600 秒一次）；
- 每日摘要加一行：`reporting.py` 餵資料 + `notifier.py` 組字，照既有 watchdog 那行的
  作法（含 `_coerce_num` 型別守衛路徑）。**計數為 0 時不出這一行**（正常狀態不加噪音）。

理由：門檻設太小會讓網格靜默停擺，而「沒有儀器」正是 watchdog spec 要根除的形態，
不得在這裡重演。

### 4.6 儀器

`_log_decision()` 的紀錄加頂層欄位 `quote_age`（float，秒）。這樣一週後有真實分佈可用。

注意這條路徑**只涵蓋「沒被擋下」的決策**（被擋就 early-return，走不到 `_log_decision`）
⇒ 它量的是「正常情況下快照有多舊」，用來判斷 5 秒門檻是否過寬或過窄；
「被擋了幾次」由 §4.5 的計數與節流 log 負責。兩者互補，缺一都看不全。

## 5. Security / Safety constraints

- 守衛的唯一副作用是「**不下單**」與「寫 log / 計數」。**不得**具備撤單、改倉、
  發 REST 請求的能力。
- 守衛不得改變 `best_bid` / `best_ask` / `latest_price` 的值——只讀不寫。
- 守衛不得影響止盈單路徑與 `sync_service` 的 REST 同步。
- 守衛的時鐘不得與可被回測替換的 `clock.now()` 共用（見 §4.2）。

#### `max_price_age_sec = 0` 的準確語意（2026-08-25 修訂，verifier Finding 4）

原文寫「必須讓行為**完全回到改動前**」。嚴格說**不成立**，§5 這句與 §4.3.1 互相打架。
準確描述是：

> **關閉守衛 = 不再擋單；風控上移與 `quote_age` 量測仍然生效，兩者皆不消費快照價格。**

具體而言，即使 `max_price_age_sec = 0`：

- (a) `check_and_reduce_positions` 已被**無條件**移到 DGT/bandit 之前（§4.3.1），
  不隨開關回位。語意中性已實查：它只讀 `long_position`/`short_position`/
  `position_threshold`（= `initial_quantity * threshold_multiplier`），而 DGT 只動
  邊界、bandit 只寫 `grid_spacing`/`take_profit_spacing`/`gamma`，都不碰那兩個值；
  且移動後仍在 `order_blocked = is_blocked(...)` 之前，相對順序不變。
- (b) `quote_age` 仍會計算並寫進 decision log（§4.6 刻意如此：最想觀察門檻是否
  合理的時候，正是守衛被關掉的時候）。

兩者都**不消費快照價格**，所以逃生門仍然守得住它真正該守的東西：不會有任何一張
單因為守衛而被擋。

**逃生門目前沒有 UI 入口**（2026-08-25 實查：`max_price_age_sec` 在
`as_terminal_max.py` 與 `web/` 全 repo 皆無編輯入口）。實務上要動它必須手改
`config/*.json` 並重啟引擎。不要以為有按鈕可按。新增 UI 入口列入 backlog，
不在本輪範圍。

#### 「無此行」不是健康訊號（2026-08-25 補，verifier Finding 5）

**每日摘要看到『無此行』不等於『價格是新鮮的』——偵測 feed 整條斷掉是 watchdog
的職責，不是本守衛的。**

推導：守衛只在 `adjust_grid` 被呼叫時才判定，而 `adjust_grid` 的兩個入口是
bookTicker 與 userData。bookTicker **全斷**時根本沒人呼叫 `_grid_step` ⇒ 計數
不會動；而 userData 自 2026-07-12 死著 ⇒ 第二條路也不觸發。⇒ 最嚴重的形態
（feed 整條斷）恰恰是這個計數**看不見**的形態。摘要那一行量的是「有人來敲門、
但手上的價格太舊」，不是「價格新鮮」。

## 6. `backtest/tick_sim.py`

`tick_sim.py:197-224` 的決策 gate 是逐 tick 餵資料，`age` 恆為 0 ⇒ gate 恆通過，
**邏輯不需要改**。但要在該處加註解說明「live 端有 price staleness gate、回測恆真」，
免得日後做 fidelity 比對時把這個差異誤判成 bug。

## 7. Red Team（實作前列出，dev-rules 要求）

| 攻擊向量 | 防禦 |
|---|---|
| 時鐘往後跳 → 永久擋單 | 下一筆 ticker 重新 stamp，自癒；且 `bot.py:539` 路徑的快照永遠新鮮 |
| 門檻太小 → 網格靜默停擺 | 節流告警 + 每日摘要一行接住（§4.5） |
| 多 symbol 只死一個 | `quote_at` 是 per-`SymbolState` 欄位，互不影響 |
| 時戳與價格分家（await 插入） | 同一同步 block，無 await 介入點（§4.1） |
| TUI 熱改 config 不生效 | gate 讀 `self.config` 不快取；納入測試 |

## 8. 可判定驗收準則

### 8.1 測試（TDD，每條必須先在真實缺陷面前紅一次）

1. `_handle_ticker` 收到 ticker 後 `sym_state.quote_at` 被設為 `clock.now()`。
2. 快照新鮮 → `adjust_grid` 正常走到 `place_order`。
3. 快照過期（用 `set_clock` 推進時間）→ `_grid_step` early-return，`place_order`
   呼叫次數為 0。
4. **`_handle_order_update` → `adjust_grid` 走殘值快照 → 被擋**（本次真正要修的形態）。
5. `quote_at == 0`（從未收過 ticker）→ 被擋。
6. `age < 0`（時鐘後跳）→ 被擋，且下一筆 ticker 之後恢復下單。
7. `max_price_age_sec = 0` → 守衛關閉，行為與改動前一致。
8. `GlobalConfig.from_dict` 餵垃圾值（`"abc"` / `None` / `-1` / `nan` / `inf`）→ fallback `5.0`。
9. 節流告警：連續 N 次過期只 log 一次。
10. decision log 紀錄含 `quote_age` 欄位。
11. TUI 熱改 `max_price_age_sec` 後 gate 立即採用新值。

### 8.2 數量

- 全套測試全綠。基線 **714 passed / 1 skipped**，新增數量在完成報告中明列。

### 8.3 流程

- 非 trivial ⇒ 完成後派 fresh-context `verifier`（read-back + 實跑測試）。
- Plan track 且命中 Red Team Protocol 適用範圍（會下單的核心邏輯）⇒ 走
  `security-review` → `dual-review` 外部輪，未拿到 `Ship as-is` 不得標記完成。

### 8.4 部署

改動**需重啟引擎才生效**。實作完成不等於生產生效，重啟由使用者執行，
progress 必須分別記錄「已 commit」與「已重啟生效」兩個狀態。

## 9. 改動檔案

| 檔案 | 改什麼 |
|---|---|
| `grid_engine/state.py` | `SymbolState` 加 `quote_at: float = 0` |
| `grid_engine/bot.py` | `_handle_ticker` 寫時戳；`_grid_step` 加 gate；新增 `_note_stale_quote`；`_log_decision` 加 `quote_age` |
| `grid_engine/config.py` | `GlobalConfig.max_price_age_sec` + `_parse_max_price_age` + `to_dict`/`from_dict` |
| `grid_engine/reporting.py` | 每日摘要餵過期計數 |
| `grid_engine/notifier.py` | 每日摘要組出過期那一行 |
| `backtest/tick_sim.py` | 僅加註解（§6） |
| `tests/` | 新測試檔 |
