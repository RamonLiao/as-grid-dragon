# 當前任務 Spec：價格時效守衛（price staleness guard）

完整設計：`docs/superpowers/specs/2026-08-24-price-staleness-guard-design.md`（權威出處）

**任務定義被修正過**：backlog 原記「`_handle_ticker` 無價格時效守衛」定位錯誤。
守衛裝在 ticker handler 入口無用（那裡的價格按定義新鮮）。真缺口在 `adjust_grid`
的**第二個呼叫端** `bot.py:668`（`_handle_order_update` 成交後），它用的是上一次
ticker 留下的殘值 `best_bid`/`best_ask`，而 `_grid_step` 的 `bot.py:405`/`419`
把這兩個值**直接餵給 `place_order()`**，零時效檢查。

**觸發面校準**：生產上 userData stream 是死的 ⇒ 668 路徑目前幾乎不觸發。本項守的是
「userData 復活後」與「bookTicker 單邊卡住但 userData 活著」兩種形態，**log 裡無實證**，
優先度排序是推測性的，驗收文案不得宣稱修掉了已觀測到的生產事故。

## Goals
1. 價格快照帶「本機抵達時戳」（`SymbolState.quote_at`），成為一等欄位。
2. 下單前判定快照年齡，超門檻則跳過本次網格調整，不下新單。
3. 過期是可觀測事件：節流 log + 每日摘要一行（計數為 0 不出）。
4. `quote_age` 落進 decision log，讓 5 秒這個猜測門檻日後能用實測收緊。

## Non-goals
不撤舊單；不做 REST 補價 fallback；不動 `ui.py`；不改 watchdog 的牆鐘（獨立 backlog 項）；
不改 `backtest/tick_sim.py` 決策邏輯（僅加註解）；不引入 `PriceQuote` 打包型別。

## Security / Safety constraints
- 守衛唯一副作用是「不下單」與「寫 log / 計數」。不得撤單、改倉、發 REST 請求。
- 不得改寫 `best_bid` / `best_ask` / `latest_price`——只讀不寫。
- 守衛的時鐘用 `clock.guard_now()`（守衛專用牆鐘），不得與可被 backtester
  替換的 `clock.now()` 共用——共用會讓「一邊實盤一邊回測」全面停單（設計 §4.2）。
- `max_price_age_sec = 0`（關閉）= **不再擋單**；風控上移與 `quote_age` 量測仍然
  生效，兩者皆不消費快照價格（設計 §5；原本寫「完全回到改動前」不準確）。
  該逃生門**目前無 UI 入口**，要動得手改 `config/*.json` 並重啟。
- 每日摘要「無此行」**不等於**「價格是新鮮的」——feed 整條斷掉時根本沒人呼叫
  `_grid_step`，計數不會動；偵測 feed 全斷是 watchdog 的職責，不是本守衛的。
- 不得影響止盈單路徑與 `sync_service` 的 REST 同步。

## 可判定驗收準則
1. 11 條測試各自先紅一次，重點是第 4 條：`_handle_order_update` → `adjust_grid`
   走殘值快照被擋（本次真正要修的形態）。清單見設計文件 §8.1。
2. 全套測試全綠，基線 **714 passed / 1 skipped**，新增數量明列。
3. 非 trivial ⇒ fresh-context `verifier`；Plan track + 命中 Red Team Protocol ⇒
   `security-review` → `dual-review` 外部輪，未拿 `Ship as-is` 不得標記完成。
4. 改動需重啟引擎才生效；progress 須分開記「已 commit」與「已重啟生效」。
