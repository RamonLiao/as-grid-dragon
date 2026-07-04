# 回測成本模型（滑價 + 資金費率）設計 — #5 (P1)

**日期**：2026-07-04
**狀態**：設計定案，待實作
**動機**：回測成本模型缺滑價、缺 funding 現金流結算（實盤有 `FundingRateManager` 但只做 position bias、不結算現金流；funding 現金流實盤靠交易所自動扣款經 account sync 進 balance）。回測因此系統性偏樂觀。

## 目標

在回測主路徑注入兩項真實成本，讓回測 PnL 更貼近實盤：

1. **滑價**：固定 bps 向不利方向偏移成交價。
2. **資金費率現金流**：對開倉部位每 8h 結算 `notional × rate`，rate 取自真實歷史。

fidelity-first：兩者**預設全開**。這會讓回測數字系統性變差（更真實），舊回測數字不再可比（同 #4 已接受的語義）。

## 非目標（Scope 外）

- legacy 回測路徑（`_run_legacy_mode`，`initial_quantity<=0`，deprecated）不加成本模型，維持現狀，僅在 `FIDELITY_NOTES` 標明。
- maker/taker 區分滑價（回測全當限價以收盤價成交，區分意義不大）。
- 波動率/ATR 比例型滑價。
- funding 影響 position bias（已是既有 signal 功能 `FundingRateManager.get_position_bias`，與現金流結算無關，不動）。

## 架構

新增純成本模組 `backtest/costs.py`，與 `grid_engine/decision.py`（決策純層）並列為第二個純層邊界。成本語意集中一處、可 bug-for-bug 單測、不污染回測 loop。

回測有兩條 fill 路徑：主路徑 `BacktestEngine._run_terminal_ui_mode`（`terminal_ui_mode and initial_quantity>0`）與 legacy `_run_legacy_mode`（OO `_process_long/short_orders`，deprecated）。**只改主路徑**。

### 元件

#### 1. `backtest/costs.py`（純函數）

```python
def apply_slippage(price: float, side: str, action: str, bps: float) -> float:
    """成交價往不利方向偏移。
    action ∈ {"entry", "tp"}；side ∈ {"long", "short"}。
    - long  entry: price * (1 + bps)   # 買貴
    - long  tp:    price * (1 - bps)   # 賣便宜
    - short entry: price * (1 - bps)   # 賣便宜
    - short tp:    price * (1 + bps)   # 買貴（回補）
    bps<=0 或非有限 → 視為 0（不偏移）。
    """

def funding_charge(positions: list, rate: float, side: str, mark_price: float) -> float:
    """回傳該側 funding 現金流（正=付出，需扣 balance）。
    notional = Σ(pos.qty) * mark_price
    - long:  +notional * rate   # 正 rate 多頭付錢，負 rate 收錢
    - short: -notional * rate   # 相反
    rate 非有限 → 視為 0。positions 空 → 0。
    """
```

純函數、無副作用、無 I/O。

#### 2. `backtest/config.py`

新增兩欄（fidelity-first 預設）：

| 欄位 | 型別 | 預設 | 說明 |
|---|---|---|---|
| `slippage_bps` | float | `0.0001`（1 bp） | 每次成交向不利方向偏移比例 |
| `funding_enabled` | bool | `True` | 是否結算 funding 現金流 |

- `to_dict`/`from_dict` 兩欄雙向。
- `from_dict` 非法值 fallback：`slippage_bps` 負值/NaN → `0.0001`；`funding_enabled` 非 bool → `True`。
- 向後相容：舊 config 無這兩欄 → 套預設（`data.get(...)`）。

#### 3. `backtest/data_loader.py`

新增 `load_funding(symbol, start, end) -> dict[int, float]`：

- 本地快取路徑 `data/funding/<symbol>.csv`（欄位：`settlement_time`（epoch 秒或 ISO）、`funding_rate`）。
- 本地缺 → 呼 `exchange.fetch_funding_rate_history(symbol, since, limit)` 拖區間、存 CSV（mirror 既有 OHLCV auto-fetch 的 skip-if-exists 模式）。
- 回傳 `{settlement_epoch: rate}` map，供 backtester 依結算時點查詢。
- 抓取失敗/區間缺漏 → 該時點**無 entry**；結算時查不到 → rate=0（不猜），並確保 `FIDELITY_NOTES` 已揭露缺漏語義。

#### 4. `backtest/backtester.py::_run_terminal_ui_mode`（接線三處）

1. **滑價**：`_open`/`_close` 內 `fill_price` 先過 `apply_slippage(price, side, action, cfg.slippage_bps)`；`_open`→action="entry"、`_close`→action="tp"。margin/fee/pnl 全部改用偏移後價格。
2. **funding 結算**：loop 開始前 `load_funding` 建 map + 記 `last_settled_epoch`。每根 bar 推進後，找出 `(last_settled_epoch, bar_epoch]` 區間內所有 8h settlement 時點；逐一對 long/short 開倉部位呼 `funding_charge(positions, rate, side, price)`、`balance -= charge`，並 append 一筆 `{"type": "funding", "pnl": -charge, "timestamp": ...}`。單根跨多個 8h 邊界要全部結算。`funding_enabled=False` → 整段跳過（balance 不動 = 等價舊行為）。
3. `_build_bundle` **不變**（funding bias 仍中性；現金流與 bias 是兩回事）。

#### 5. `FIDELITY_NOTES` 更新

- 移除 (1) 的「無滑價」、(3) 的「funding 於回測退化中性(全關)」中關於 funding 現金流的暗示。
- 新增：主路徑含 `slippage_bps` 滑價成本 + 8h funding 現金流結算（真實歷史，缺漏時點 rate=0）；legacy 路徑（`initial_quantity<=0`）不含成本模型。
- 保留 leading/ATR/GLFT 於回測退化中性的說明（與本次無關）。

## 資料流

```
config(slippage_bps, funding_enabled)
        │
        ▼
run() → _run_terminal_ui_mode()
        │
        ├─ data_loader.load_funding(symbol, start, end) → {settlement_epoch: rate}
        │        └─ 本地 data/funding/<symbol>.csv 命中 / 缺則 fetch_funding_rate_history 存檔
        │
        └─ for each bar:
             ├─ _settle → _open/_close(fill_price = apply_slippage(close, side, action, bps))
             └─ 跨 8h settlement → funding_charge(positions, rate, side, price) → balance -= charge
```

## 錯誤處理 / 邊界

| 情境 | 行為 |
|---|---|
| `slippage_bps` 負值 / NaN | config fallback 至 0.0001；`apply_slippage` 內 `<=0`/非有限 → 不偏移 |
| funding 檔全缺、抓取失敗 | 結算時查無 → rate=0；不 raise、不中斷回測 |
| 極端 rate（如 ±0.75% 上限） | 照收，`funding_charge` 不 clamp（真實資料本就有上限） |
| timestamp 倒流 | settlement 掃描以 `(last_settled_epoch, bar_epoch]` 為界；倒流則區間為空 → 不重複結算 |
| 單根 K 線跨多個 8h 邊界 | 掃描區間內**所有** settlement，逐一結算 |
| NaN rate 混入 map | `funding_charge` 非有限 → 視為 0 |
| 空倉時 settlement | notional=0 → charge=0，**不 append trade**（避免污染 trades_count/win_rate）；測試鎖定 |

## 測試（TDD，red→green，每 task 一 commit）

**`costs.py` 純函數單測**
- `apply_slippage` 四方向 × entry/tp 正確偏移；bps=0/負/NaN → 不偏移。
- `funding_charge` long/short 正負號；正/負 rate；空倉→0；多倉 notional 加總；NaN rate→0。

**data_loader funding 快取**
- 本地命中不重抓（mock exchange 不被呼叫）。
- 缺檔 → fetch → 存 CSV → 二次讀命中。
- 缺漏時點結算回 rate=0。
- settlement timestamp join 正確。

**backtester 整合**
- 開滑價：同一情境 entry 成本↑、TP 淨收益↓（對照零滑價 baseline）。
- funding 跨 8h：balance 依 notional×rate 精確變動；多頭正 rate 扣款、負 rate 收款；空頭相反。
- `funding_enabled=False`：balance 與零 funding baseline 完全一致（等價舊行為守門）。
- 單根跨多 8h 邊界全部結算。

**Monkey（極端測試把程式玩壞）**
- 負 bps、極端 rate（±0.75%）、timestamp 倒流、funding 檔全缺、單根跨多個 8h 邊界、NaN rate、空倉 settlement、rate map 空。

**config**
- roundtrip（to_dict→from_dict 保值）、monkey（非法值 fallback）、向後相容（舊 config 無新欄位套預設）。

## 驗收

- 全套測試綠（數量報數字）。
- `funding_enabled=False` + `slippage_bps=0` 時，回測 equity 曲線與加成本前**逐點一致**（等價守門，證明成本模型是純疊加、無意外副作用）。
- 純層 `costs.py` 單測覆蓋四方向滑價 + long/short funding 正負號。
- dual-review 兩輪收斂。
