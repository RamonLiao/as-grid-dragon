# 止盈加倍只給淨曝險側 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓止盈量加倍只發生在「淨曝險方向」那一側，使 delta 主動收斂到 0，而不是把使用者手動建立的對沖倉系統性拆掉。

**Architecture:** 改動集中在兩個純邏輯點——`grid_engine/decision.py` 的 `tp_quantity`（加上「我是較大側」的閘門、刪掉 `opposite >= threshold` 子條件）與 `grid_engine/risk_monitor.py` 的雙向減倉（大側多減 `min(reduce_qty, gap)`，讓「`|delta|` 永不增加」成為可證明不變式）。其餘四處是連帶一致性：`backtest/backtester.py` 的第二個 caller、`grid_engine/bot.py` 的死碼、`grid_engine/ui.py` 會說謊的面板標籤、以及一個會因本改動失去鑑別力的既有測試。

**Tech Stack:** Python 3（`../.venv`）、pytest、ccxt（本計畫不呼叫交易所）、既有 `backtest/tick_sim.py` 事件模擬器。

**Spec:** `docs/superpowers/specs/2026-07-26-hedge-immune-tp-design.md`（v3，quant spec reviewer 兩輪後可開工）

## Global Constraints

- **測試一律在 `as-grid-dragon` 子目錄跑**：`../.venv/bin/python -m pytest`。monorepo 根目錄會被 `as-grid-auto/test_position_mode.py` 的 collection-time `sys.exit(1)` 打斷。
- **測試基線：546 passed / 1 skipped。** 每個 task 結束時全套必須綠。
- **`delta` 一律定義為 `long_position − short_position`（帶號）。**
- **每個新守衛必須先在真實缺陷（mutation）前紅一次。** 從未紅過的測試等於會執行的註解。
- **測試值不得等於被測欄位的預設值**（`SymbolConfig` 預設 `initial_quantity=3`、`threshold_multiplier=20`、`limit_multiplier=5`；`TickSimConfig` 預設 `threshold_multiplier=40.0`、`limit_multiplier=5.0`）。
- **只 stage 明確指定的檔案**（`git add <file>...`）。禁止 `git add -A` / `git add .`。
- **不要重啟或干擾正在跑的生產引擎**（`ps aux | grep as_terminal_max`）。不要寫 `config/`、`logs/`、`data/`。
- **臨時腳本一律寫在 scratchpad**，不進 repo。
- **不新增 config flag。** A/B 對照靠 scratchpad monkeypatch。

---

### Task 1: `tp_quantity` 新規則 + 簽名變更 + 全部 caller

**Files:**
- Modify: `grid_engine/decision.py:97-100`（`tp_quantity`）、`grid_engine/decision.py:135`（`compute_quantity` 呼叫）
- Modify: `backtest/backtester.py:84-85`（`_legacy_grid_decision` 的第二個 caller）
- Test: `tests/test_decision.py:26-29`（改寫）

**Interfaces:**
- Consumes: 無（本計畫第一個 task）
- Produces: `tp_quantity(base_qty: float, my_position: float, opposite_position: float, position_limit: float) -> float` —— **4 個位置參數**，`position_threshold` 已移除。後續 task 不直接呼叫它，但 Task 5 的 risk_monitor 與 Task 4 的 ui 標籤共用同一個「我是較大側」判準（`my > opposite`，嚴格大於）。

- [ ] **Step 1: 寫新的真值表測試（會失敗）**

把 `tests/test_decision.py:26-29` 的 `test_tp_quantity_doubles_over_limit` **整個替換**成下面兩個測試。
注意：舊測試的 `:28`（`assert d.tp_quantity(3, 10, 60, 15, 60) == 6  # opp>=threshold`）
斷言的正是本 spec 要刪的子條件，**必須刪掉，不是改簽名了事**。

```python
def test_tp_quantity_doubles_only_for_the_net_exposure_side():
    """止盈量加倍只給「淨曝險方向」那側（spec §3.1）。

    對沖側（較小側）維持 1×，否則 0.02進/0.04出 的不對稱會系統性拆掉人工建立的
    對沖（2026-07-26 實盤 11 天實證：空頭 0.36→0.20）。
    """
    # my > limit 且 my > opposite → 加倍
    assert d.tp_quantity(3, 20, 10, 15) == 6
    assert d.tp_quantity(3, 20, 0, 15) == 6

    # my > limit 但 my <= opposite（我是對沖側）→ 不加倍
    assert d.tp_quantity(3, 20, 20, 15) == 3, "兩側相等時不得加倍（嚴格大於）"
    assert d.tp_quantity(3, 20, 30, 15) == 3, "我是較小側 → 不得加倍（否則拆對沖）"

    # my <= limit → 不加倍（無論對手側多大）
    assert d.tp_quantity(3, 15, 0, 15) == 3, "position_limit 是嚴格大於"
    assert d.tp_quantity(3, 10, 0, 15) == 3

    # spec §4 向量 3：裝死側反而是較小側（對手更大）→ 新規則不加倍。
    # 這是**刻意的行為變更**（裝死出清變慢）；舊規則會因 my>limit 而加倍。
    assert d.tp_quantity(3, 61, 70, 15) == 3, (
        "裝死側若同時是較小側，止盈量不再加倍——刻意的行為變更（spec §4 向量 3）"
    )


def test_tp_quantity_no_longer_doubles_on_large_opposite():
    """已刪除 `or opposite_position >= position_threshold` 子條件（spec §1）。

    該子條件唯一可達且有效的情形是「我不是淨曝險側時仍加倍我」= 最大化拆對沖；
    全量 logs/decisions.jsonl（99,270 筆）實測 98,399 筆屬此類。
    """
    # 舊規則會因 opposite=60 >= threshold=60 而回 6；新規則不看對手側是否超門檻
    assert d.tp_quantity(3, 10, 60, 15) == 3
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `../.venv/bin/python -m pytest tests/test_decision.py::test_tp_quantity_doubles_only_for_the_net_exposure_side tests/test_decision.py::test_tp_quantity_no_longer_doubles_on_large_opposite -v`
Expected: **FAIL**，`TypeError: tp_quantity() missing 1 required positional argument: 'position_threshold'`

- [ ] **Step 3: 改 `tp_quantity`**

`grid_engine/decision.py:97-100` 整個替換：

```python
def tp_quantity(base_qty, my_position, opposite_position, position_limit):
    """止盈量加倍只給「淨曝險方向」那側。

    對沖側（較小側）維持 1× ⇒ 進出對稱、消耗速度減半（不是免疫，見 spec §2）。
    原 `or opposite_position >= position_threshold` 已**刻意刪除**：它唯一可達且有效
    的情形是「我不是淨曝險側時仍加倍我」= 最大化拆對沖（2026-07-26 全量 log 實測
    98,399 筆屬此類）。行為變更的完整 diff 分類見 spec §5.1。
    """
    if my_position > position_limit and my_position > opposite_position:
        return base_qty * 2
    return base_qty
```

- [ ] **Step 4: 改 `compute_quantity` 的呼叫（decision.py:135）**

```python
        q = tp_quantity(q, my_pos, opp_pos, inputs.position_limit)
```

（`DecisionInputs.position_threshold` 欄位**保留**——`is_dead_mode` 還在用，`bot.py:242-243` 不動。）

- [ ] **Step 5: 改 `backtest/backtester.py:84-85` 的第二個 caller**

⚠️ **這是 spec v1 漏掉的 blocker。** `_legacy_grid_decision` 以 5 個位置參數呼叫，
不改會 `TypeError`。它的唯一測試（`tests/test_backtest_seed_position.py:210`）只斷言
「走 legacy 會 raise」⇒ **全套測試綠也抓不到這個 TypeError**。

```python
    tp_qty = tp_quantity(base_qty, my_position, opposite_position, cfg.position_limit)
```

同一函數 `:83` 的 `is_dead_mode(my_position, cfg.position_threshold)` **不動**。

- [ ] **Step 6: grep 證明零殘留**

Run: `grep -rn "tp_quantity" grid_engine backtest scripts tests`
Expected: 每個呼叫點都是 4 個引數。定義在 `grid_engine/decision.py`，
呼叫在 `grid_engine/decision.py`（`compute_quantity`）、`backtest/backtester.py`、`tests/test_decision.py`。
**不得有任何 5 引數呼叫。**

- [ ] **Step 7: 跑新測試確認通過**

Run: `../.venv/bin/python -m pytest tests/test_decision.py -v`
Expected: PASS（含 `:119` 的既有斷言 `tps[0].quantity == 6.0`——那筆是 `long=70 / short=0 / limit=15`，新規則下 `70>15 且 70>0` 仍加倍，**值不變、不需修改**）

- [ ] **Step 8: mutation —— 確認新測試真的會紅**

暫時把 `tp_quantity` 的條件改回舊版 `if my_position > position_limit or opposite_position >= position_threshold:`（需暫時把參數加回來），跑：
Run: `../.venv/bin/python -m pytest tests/test_decision.py -v`
Expected: `test_tp_quantity_doubles_only_for_the_net_exposure_side` 與 `test_tp_quantity_no_longer_doubles_on_large_opposite` **都 FAIL**。
確認後**還原**成 Step 3 的版本。

- [ ] **Step 9: 跑全套測試**

Run: `../.venv/bin/python -m pytest -q`
Expected: 全綠。⚠️ 預期 `tests/test_backtest_matching.py` 那條 clamp 測試**仍然是綠的**——
那正是 Task 2 要處理的問題（它會靜默失去鑑別力，不會變紅）。

- [ ] **Step 10: Commit**

```bash
git add grid_engine/decision.py backtest/backtester.py tests/test_decision.py
git commit -m "feat(decision): 止盈量加倍只給淨曝險側

tp_quantity 加上 `my_position > opposite_position` 閘門，並刪除
`or opposite_position >= position_threshold` 子條件（簽名去掉該參數）。

動機（tasks/health-check-2026-07-26.md 逐筆對帳，零殘差）：進 0.02 / 出 0.04 的
不對稱在持倉 > position_limit(0.1) 後兩側都生效 ⇒ 每往返雙側各淨減 0.02，對基數小
的對沖側傷害成比例更大。11 天內對沖 0.36→0.20（-44%），delta +0.24→+0.40，
強平價 90.8→288.98。

同步改 backtest/backtester.py:84 的第二個 caller（_legacy_grid_decision）——該路徑
唯一的測試只斷言「會 raise」，全套測試綠抓不到簽名不符的 TypeError。"
```

---

### Task 2: 修復會因 Task 1 靜默失去鑑別力的 clamp 測試

**Files:**
- Modify: `tests/test_backtest_matching.py:146-155`（`_both_side_doubling_cfg` → 改名並重寫）
- Modify: `tests/test_backtest_matching.py:157-199`（測試的 docstring、cfg 呼叫、三條斷言值）

**Interfaces:**
- Consumes: Task 1 的 `tp_quantity`（4 引數、`my > opposite` 閘門）
- Produces: 無（純測試修復）

**背景（spec §3.5 / v1 BL2）：** 現行 fixture **刻意**設 `limit_multiplier=100.0`（讓
`my > limit` 不可能）+ `threshold_multiplier=1.0`，好讓加倍**只**由 Task 1 刪掉的子條件觸發。
Task 1 之後兩側各 1.0（`m == o`）⇒ tp qty 由 2.0 變 1.0 ⇒ `min(qty, prior_qty)` clamp 變 no-op，
而三條斷言值（`trades_count 2` / `realized 2.0` / `unrealized 1.2`）**恰好完全不變**
⇒ 測試繼續綠但已無法對它防的 bug 紅一次。

- [ ] **Step 1: 確認問題真的存在（先看它「假綠」）**

Run: `../.venv/bin/python -m pytest "tests/test_backtest_matching.py::test_tp_fill_cannot_close_more_than_the_position_that_existed_before_this_bars_entry" -v`
Expected: **PASS**（Task 1 之後仍綠 = 正是問題）。

再暫時把 `backtest/backtester.py` 的 tp clamp（`closable = min(...)` 那行）改成不 clamp，重跑：
Expected: **仍然 PASS** ⇒ 證實鑑別力已歸零。確認後**還原** clamp。

- [ ] **Step 2: 重寫 fixture**

`tests/test_backtest_matching.py:146-155` 整段替換（含改名，舊名 `_both_side_doubling_cfg` 已名不副實）：

```python
def _net_exposure_doubling_cfg(**kw):
    """讓止盈加倍在「加倍只給淨曝險側」規則下仍然觸發，以便行使 reduce_only clamp。

    _zero_cost_cfg 預設 direction="long" ⇒ short_position 恆為 0 ⇒ `my > opposite`
    必然成立。limit_multiplier=0.5 給出 position_limit = 1.0*0.5 = 0.5 < 1.0，
    所以一次進場成交（qty=1.0）後 `my > limit` 也成立 → tp qty = 2.0。
    threshold_multiplier=100 讓 position_threshold=100，is_dead_mode(1.0, 100)=False，
    裝死分支不介入。

    注意：舊版 fixture 靠已刪除的 `opposite >= threshold` 子條件觸發加倍，改動後會
    靜默變成永綠空殼（tp qty 1.0 使 clamp 成為 no-op，而斷言值恰好不變）。
    """
    return _zero_cost_cfg(limit_multiplier=0.5, threshold_multiplier=100.0, **kw)
```

- [ ] **Step 3: 改測試本體的 cfg 呼叫與斷言值**

`tests/test_backtest_matching.py` 內：
- `GridBacktester(df, _both_side_doubling_cfg())` → `GridBacktester(df, _net_exposure_doubling_cfg())`
- docstring 的「構造」段落改成單側版本（three-bar K 線不變）：

```
      bar1 close=100 → 掛 long entry@99.4(=100*0.994)，qty=1.0（direction="long"，無 short 側）。
      bar2 (low=99, high=101) → long entry 成交 @99.4，long_position=1.0。
        重決策：m=1.0 > position_limit=0.5 且 m > opposite=0 → tp qty 加倍為 2.0 @100.4。
      bar3 (low=99, high=101) 同時穿越 entry@99.4 與 tp@100.4。
        entry 先結算 → long=2.0（bar2 那 1.0 + bar3 新開的 1.0）。
        止盈量 2.0 > 「entry 結算前」的持倉 1.0（bar2 那筆）→ clamp 被行使。
```
- 三條斷言值改為單側版本：

```python
    assert res.trades_count == 1, (
        f"trades_count={res.trades_count}：若為 2，代表止盈把 bar3 剛開的倉也平掉了"
        "（reduce_only 不可能平未來才存在的倉）"
    )
    assert res.realized_pnl == pytest.approx(1.0, abs=1e-6), (
        f"realized_pnl={res.realized_pnl}：若為 2.0，代表 bar3 新倉被錯誤地計入已實現獲利"
    )
    assert res.unrealized_pnl == pytest.approx(0.6, abs=1e-6), (
        f"unrealized_pnl={res.unrealized_pnl}：若為 0.0，代表 bar3 新倉已被平掉"
    )
```

（依據：`realized = (100.4 − 99.4) × 1.0 = 1.0`；末根 close=100，剩 1 手 @99.4 ⇒ `unrealized = 0.6`。）

- [ ] **Step 4: 跑測試確認通過**

Run: `../.venv/bin/python -m pytest "tests/test_backtest_matching.py::test_tp_fill_cannot_close_more_than_the_position_that_existed_before_this_bars_entry" -v`
Expected: PASS

- [ ] **Step 5: mutation —— A8 驗收（關鍵）**

暫時拿掉 `backtest/backtester.py` 的 tp clamp（`min(qty, prior_qty)` → 直接用 `qty`），跑：
Run: 同 Step 4
Expected: **FAIL**，且三條斷言的實際值應為 `trades_count=2 / realized=2.0 / unrealized=0.0`。
確認後**還原** clamp。**這一步不通過就不得繼續**——它是本 task 存在的唯一理由。

- [ ] **Step 6: 確認 fixture 無外溢**

Run: `grep -n "_both_side_doubling_cfg\|_net_exposure_doubling_cfg" tests/test_backtest_matching.py`
Expected: 舊名零殘留；新名只有定義 + 一個使用點。

- [ ] **Step 7: 跑全套測試**

Run: `../.venv/bin/python -m pytest -q`
Expected: 全綠。

- [ ] **Step 8: Commit**

```bash
git add tests/test_backtest_matching.py
git commit -m "test(backtest): 修復 reduce_only clamp 測試——改用淨曝險側觸發加倍

舊 fixture 刻意用 limit_multiplier=100 + threshold_multiplier=1.0，讓加倍只由
`opposite >= threshold` 這條（已於前一 commit 刪除）子條件觸發。改動後 tp qty
由 2.0 變 1.0，clamp 成為 no-op，而三條斷言值恰好完全不變 ⇒ 測試繼續綠但鑑別力
歸零（lessons 通則 3：斷言錯了會紅，資料退化只會一直綠）。

改為單側不對稱場景（direction=\"long\" 使 short 恆 0 ⇒ my > opposite 必成立，
limit_multiplier=0.5 使 position_limit=0.5 < 1.0）。已驗證拿掉 clamp 後三條斷言
全紅（trades_count 2 / realized 2.0 / unrealized 0.0）。"
```

---

### Task 3: 刪掉 `bot.py` 的加倍死碼

**Files:**
- Modify: `grid_engine/bot.py:250-280`（`_get_adjusted_quantity`）

**Interfaces:**
- Consumes: 無
- Produces: 無（`_get_adjusted_quantity` 的對外簽名不變，仍收 `is_take_profit` 參數）

**背景（spec §3.3）：** 該函數內含一份與 `tp_quantity` 同義的加倍邏輯，但兩個呼叫點
（`bot.py:401`、`:415`）都傳 `is_take_profit=False`，且只在 `position == 0` 的開倉引導路徑
⇒ 加倍分支是死碼。留著它 = 留一份與新規則矛盾的誤導拷貝。

- [ ] **Step 1: 先證明它真的是死碼**

Run: `grep -rn "_get_adjusted_quantity" grid_engine backtest scripts tests web as_terminal_max.py`
Expected: 只有 `bot.py:250`（定義）+ `bot.py:401`、`bot.py:415`（呼叫，**都傳 `False`**）。
若出現任何傳 `True` 或 `is_take_profit=True` 的呼叫點 → **停下，本 task 的前提不成立**。

- [ ] **Step 2: 刪掉加倍分支**

`grid_engine/bot.py:261-271` 的整段（`if is_take_profit:` 到 `base_qty *= 2` 那四個分支）刪除，
並把緊接的 `if not is_take_profit:` 簡化。改動後函數體：

```python
        max_cfg = self.config.max_enhancement
        base_qty = sym_config.initial_quantity

        # 止盈量加倍已統一由 grid_engine.decision.tp_quantity() 負責（decide() 路徑）。
        # 這裡原有一份同義拷貝，但兩個呼叫點（:401/:415）都傳 is_take_profit=False
        # 且只在 position == 0 的開倉引導路徑 → 該分支是死碼，已刪。
        # is_take_profit 參數保留：呼叫端仍顯式傳 False，且 GLFT 調整只作用於非止盈量。
        if not is_take_profit:
            base_qty = self.glft_controller.adjust_order_quantity(
                base_qty, side,
                sym_state.long_position, sym_state.short_position,
                max_cfg
            )
```

（`if not is_take_profit:` 之後的 funding bias 等既有邏輯**原樣保留**，不要一起動。）

- [ ] **Step 3: 語法檢查**

Run: `../.venv/bin/python -m py_compile grid_engine/bot.py && echo OK`
Expected: `OK`。⚠️ 依 lessons：`tests/` **零 import** `as_terminal_max` 與 `web/pages/*`，
`grid_engine/bot.py` 的漏改也大多抓不到 ⇒ `py_compile` + 逐點 read-back 是必要步驟。

- [ ] **Step 4: 跑全套測試**

Run: `../.venv/bin/python -m pytest -q`
Expected: 全綠。

- [ ] **Step 5: Commit**

```bash
git add grid_engine/bot.py
git commit -m "refactor(bot): 刪掉 _get_adjusted_quantity 的止盈加倍死碼

兩個呼叫點（bot.py:401/:415）都傳 is_take_profit=False 且只在 position == 0 的
開倉引導路徑 ⇒ 加倍分支從未執行。留著它會是一份與 decision.tp_quantity 新規則
矛盾的誤導拷貝（lessons 通則 1：重複實作）。"
```

---

### Task 4: `ui.py` 的 `×2` 標籤同步加淨曝險條件

**Files:**
- Modify: `grid_engine/ui.py:125-133`（抽出純函數 + 改判定）
- Create: `tests/test_ui_status_labels.py`

**Interfaces:**
- Consumes: Task 1 建立的判準（`my > opposite`，嚴格大於）
- Produces: `grid_engine.ui.position_status_labels(long_position, short_position, position_limit, position_threshold) -> list[str]`

**背景（spec §3.4 / v2 NF2）：** 現行判定只看 `position > position_limit`、**不看對手側**
⇒ Task 1 之後小側實際是 1× 但面板仍顯示「×2」。這會直接**誤觸 spec §7 回退表第 4 條**
（「小側止盈 qty 出現 0.04」），讓操作者以為新規則沒生效。
`tests/` 目前**零 import** `grid_engine/ui.py`，所以先把判定抽成純函數才測得到。

- [ ] **Step 1: 寫測試（會失敗，函數還不存在）**

Create `tests/test_ui_status_labels.py`：

```python
"""面板狀態標籤：`×2` 必須與 decision.tp_quantity 的實際行為一致。

若標籤說「空×2」而實際止盈量是 1×，操作者會誤判新規則沒生效
（spec §7 回退表第 4 條的誤觸來源）。
"""
from grid_engine.ui import position_status_labels

# 生產參數：initial_quantity 0.02 × limit_multiplier 5 = 0.1
#           initial_quantity 0.02 × threshold_multiplier 40 = 0.8
LIMIT, THRESHOLD = 0.1, 0.8


def test_only_the_net_exposure_side_is_labelled_doubled():
    # 生產現況：多 0.60 / 空 0.20，兩側都 > limit 0.1，但只有多頭是淨曝險側
    assert position_status_labels(0.60, 0.20, LIMIT, THRESHOLD) == ["[yellow]多×2[/]"]
    # 鏡像
    assert position_status_labels(0.20, 0.60, LIMIT, THRESHOLD) == ["[yellow]空×2[/]"]


def test_equal_sides_get_no_doubling_label():
    # 嚴格大於：相等時兩側都不加倍，故都不該標
    assert position_status_labels(0.60, 0.60, LIMIT, THRESHOLD) == []


def test_below_limit_gets_no_label():
    assert position_status_labels(0.05, 0.0, LIMIT, THRESHOLD) == []
    assert position_status_labels(LIMIT, 0.0, LIMIT, THRESHOLD) == [], "limit 是嚴格大於"


def test_dead_mode_label_takes_precedence_and_ignores_opposite():
    # 裝死是「我的持倉超過 threshold」，與對手側無關 → 不加淨曝險條件
    assert position_status_labels(0.9, 1.0, LIMIT, THRESHOLD) == ["[red bold]多裝死[/]"]
    assert position_status_labels(0.9, 1.0, LIMIT, THRESHOLD) != ["[yellow]多×2[/]"]


def test_both_sides_dead_mode():
    assert position_status_labels(0.9, 0.95, LIMIT, THRESHOLD) == [
        "[red bold]多裝死[/]", "[red bold]空裝死[/]",
    ]
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `../.venv/bin/python -m pytest tests/test_ui_status_labels.py -v`
Expected: **FAIL**，`ImportError: cannot import name 'position_status_labels'`

- [ ] **Step 3: 在 `grid_engine/ui.py` 模組層新增純函數**

放在 `class TerminalUI` **之前**（模組層，`:16` 附近）：

```python
def position_status_labels(long_position, short_position, position_limit, position_threshold):
    """面板狀態標籤。`×2` 的判定必須與 decision.tp_quantity 一致：
    加倍只給淨曝險側（`my > opposite`，嚴格大於）。裝死判定只看自己這側。"""
    labels = []
    if long_position > position_threshold:
        labels.append("[red bold]多裝死[/]")
    elif long_position > position_limit and long_position > short_position:
        labels.append("[yellow]多×2[/]")
    if short_position > position_threshold:
        labels.append("[red bold]空裝死[/]")
    elif short_position > position_limit and short_position > long_position:
        labels.append("[yellow]空×2[/]")
    return labels
```

- [ ] **Step 4: 讓 `create_symbols_panel` 改用它**

`grid_engine/ui.py:125-133` 的 `status_parts = []` 到第二個 `elif` 區塊，整段替換成：

```python
            status_parts = position_status_labels(
                sym_state.long_position, sym_state.short_position,
                sym_config.position_limit, sym_config.position_threshold,
            )
```

（緊接的 `if not status_parts:` 分支**原樣保留**，不要動。）

- [ ] **Step 5: 跑測試確認通過**

Run: `../.venv/bin/python -m pytest tests/test_ui_status_labels.py -v`
Expected: PASS（5 passed）

- [ ] **Step 6: mutation —— 確認新守衛會紅**

把 Step 3 的兩個 `and long_position > short_position` / `and short_position > long_position` 拿掉，跑：
Run: 同 Step 5
Expected: `test_only_the_net_exposure_side_is_labelled_doubled` 與
`test_equal_sides_get_no_doubling_label` **FAIL**。確認後還原。

- [ ] **Step 7: 語法檢查 + 全套測試**

Run: `../.venv/bin/python -m py_compile grid_engine/ui.py && ../.venv/bin/python -m pytest -q`
Expected: 全綠（比基線多 5 個測試）。

- [ ] **Step 8: Commit**

```bash
git add grid_engine/ui.py tests/test_ui_status_labels.py
git commit -m "fix(ui): ×2 標籤加上淨曝險條件，並抽成可測的純函數

原判定只看 position > position_limit、不看對手側 ⇒ 加倍規則改動後，小側實際是
1× 但面板仍顯示「×2」，會誤觸 spec §7 回退表第 4 條、讓操作者以為新規則沒生效。

tests/ 原本零 import grid_engine/ui.py ⇒ 先把判定抽成 position_status_labels()
純函數才測得到。裝死判定維持只看自己這側（與對手側無關）。"
```

---

### Task 5: `risk_monitor` 雙向減倉改為不對稱（不 overshoot）

**Files:**
- Modify: `grid_engine/risk_monitor.py:66-95`（`check_and_reduce_positions`）
- Create: `tests/test_risk_monitor_reduce.py`

**Interfaces:**
- Consumes: 無（獨立於 Task 1-4）
- Produces: 無（`check_and_reduce_positions(sym_config, sym_state)` 簽名不變）

**背景（spec §3.2）：** 現行「兩側各市價減 `position_threshold × 0.1`」是**等量**減倉
⇒ `delta` **完全不變**（它降的是 gross，不是中性度）。
spec v2 提案的「大側固定 2×」在 `gap < reduce_qty/2` 時會 **overshoot**——反例
`long=0.66 / short=0.64` 使 `|delta|` 由 0.02 **惡化到 0.06**；且**任何一次觸發後雙側都掉到
門檻以下 ⇒ 沒有下一輪**可以修正。v3 改用 `extra = min(reduce_qty, gap)`，讓不 overshoot
成為**可證明的不變式**，並順帶消掉浮點 `==` 分支。

- [ ] **Step 1: 寫測試（會失敗）**

Create `tests/test_risk_monitor_reduce.py`：

```python
"""雙向減倉的三條不變式（spec §3.2）。

斷言不變式而非逐案例列舉——v2 的「大側固定 2×」在 gap < reduce_qty/2 時會把
|delta| 推大（反例 0.66/0.64：0.02 → 0.06），而觸發後雙側都掉到門檻以下、
沒有下一輪可以修正。
"""
import asyncio

import pytest

from grid_engine.config import SymbolConfig
from grid_engine.risk_monitor import RiskMonitor
from grid_engine.state import GlobalState, SymbolState


class _RecordingExecutor:
    def __init__(self):
        self.orders = []

    async def place_order(self, symbol, side, price, quantity,
                          reduce_only, position_side, order_type):
        self.orders.append({
            "side": side, "quantity": quantity, "reduce_only": reduce_only,
            "position_side": position_side, "order_type": order_type,
        })
        return {"id": "x"}


def _run(long_pos, short_pos):
    """觸發一次雙向減倉，回傳 (減多量, 減空量, reduce_qty)。"""
    # initial_quantity=0.02 × threshold_multiplier=40 → position_threshold=0.8
    # 兩者皆非 SymbolConfig 預設（預設 3 / 20），避免測試值 == 預設值的假綠
    cfg = SymbolConfig(symbol="BNBUSDC", ccxt_symbol="BNB/USDC:USDC",
                       initial_quantity=0.02, threshold_multiplier=40.0)
    assert cfg.position_threshold == pytest.approx(0.8)
    reduce_qty = cfg.position_threshold * 0.1          # 0.08
    local_threshold = cfg.position_threshold * 0.8     # 0.64
    assert long_pos >= local_threshold and short_pos >= local_threshold, "測試狀態必須能觸發"

    state = GlobalState()
    sym_state = SymbolState(symbol="BNBUSDC",
                            long_position=long_pos, short_position=short_pos)
    ex = _RecordingExecutor()
    rm = RiskMonitor(config=None, state=state, order_executor=ex, notifier=None)
    asyncio.run(rm.check_and_reduce_positions(cfg, sym_state))

    cut_long = sum(o["quantity"] for o in ex.orders if o["position_side"] == "long")
    cut_short = sum(o["quantity"] for o in ex.orders if o["position_side"] == "short")
    for o in ex.orders:
        assert o["reduce_only"] is True and o["order_type"] == "market"
    return cut_long, cut_short, reduce_qty


@pytest.mark.parametrize("long_pos,short_pos", [
    (0.66, 0.64),                    # gap 0.02 < reduce_qty/2 → v2 在這裡 overshoot
    (0.70, 0.64),                    # gap 0.06
    (0.80, 0.64),                    # gap 0.16 > reduce_qty
    (0.64, 0.80),                    # short 為大側
    (0.65, 0.65),                    # gap == 0
    (0.6400000000000001, 0.64),      # 浮點雜訊：不得落進錯誤分支
])
def test_reduce_never_increases_abs_delta_and_never_flips_sign(long_pos, short_pos):
    cut_long, cut_short, _ = _run(long_pos, short_pos)
    old_delta = long_pos - short_pos
    new_delta = (long_pos - cut_long) - (short_pos - cut_short)

    assert abs(new_delta) <= abs(old_delta) + 1e-12, (
        f"|delta| 變大了：{abs(old_delta):.6f} → {abs(new_delta):.6f}"
    )
    assert old_delta * new_delta >= -1e-12, (
        f"delta 變號（overshoot）：{old_delta:+.6f} → {new_delta:+.6f}"
    )


@pytest.mark.parametrize("long_pos,short_pos", [
    (0.66, 0.64), (0.80, 0.64), (0.64, 0.80), (0.65, 0.65),
])
def test_gross_strictly_decreases(long_pos, short_pos):
    cut_long, cut_short, reduce_qty = _run(long_pos, short_pos)
    gap = abs(long_pos - short_pos)
    expected_total = 2 * reduce_qty + min(reduce_qty, gap)
    assert cut_long + cut_short == pytest.approx(expected_total), "gross 下降量不符 spec §3.2"
    assert cut_long + cut_short > 0


def test_equal_sides_fall_back_to_symmetric_reduction():
    cut_long, cut_short, reduce_qty = _run(0.65, 0.65)
    assert cut_long == pytest.approx(reduce_qty)
    assert cut_short == pytest.approx(reduce_qty), "gap == 0 必須退回現行的雙側等量減倉"


def test_no_order_when_below_local_threshold():
    cfg = SymbolConfig(symbol="BNBUSDC", ccxt_symbol="BNB/USDC:USDC",
                       initial_quantity=0.02, threshold_multiplier=40.0)
    sym_state = SymbolState(symbol="BNBUSDC", long_position=0.60, short_position=0.20)
    ex = _RecordingExecutor()
    rm = RiskMonitor(config=None, state=GlobalState(), order_executor=ex, notifier=None)
    asyncio.run(rm.check_and_reduce_positions(cfg, sym_state))
    assert ex.orders == [], "生產現況（0.60/0.20）不該觸發雙向減倉"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `../.venv/bin/python -m pytest tests/test_risk_monitor_reduce.py -v`
Expected: **FAIL**——`test_reduce_never_increases_abs_delta_and_never_flips_sign[0.66-0.64]`
之外多數會過（現行等量減倉本來就不動 delta），但
`test_gross_strictly_decreases` 會 FAIL（現行總減量恆為 `2 × reduce_qty`，缺 `min(reduce_qty, gap)`）。
⚠️ **先確認 FAIL 的是哪幾條並記下**——這是判斷新守衛有鑑別力的依據。

- [ ] **Step 3: 改 `check_and_reduce_positions`**

`grid_engine/risk_monitor.py:78-89` 的整個 `if ... 雙向減倉` 區塊替換成：

```python
        if sym_state.long_position >= local_threshold and sym_state.short_position >= local_threshold:
            # 大側多減 min(reduce_qty, gap)：讓「|delta| 永不增加、永不變號」成為
            # 不變式而非案例分析（spec §3.2）。夾到 gap 也順帶消掉浮點相等比較——
            # gap == 0 時 extra == 0，自動退回雙側等量減倉（= 舊行為）。
            gap = abs(sym_state.long_position - sym_state.short_position)
            extra = min(reduce_qty, gap)
            long_qty = reduce_qty + (extra if sym_state.long_position > sym_state.short_position else 0.0)
            short_qty = reduce_qty + (extra if sym_state.short_position > sym_state.long_position else 0.0)

            logger.info(
                f"[風控] {sym_config.symbol} 多空持倉均超過 {local_threshold}，開始雙向減倉"
                f"（多 {long_qty} / 空 {short_qty}，gap={gap}）"
            )

            if sym_state.long_position > 0:
                await self.order_executor.place_order(ccxt_symbol, 'sell', 0, long_qty, True, 'long', 'market')
                logger.info(f"[風控] {sym_config.symbol} 市價平多 {long_qty}")

            if sym_state.short_position > 0:
                await self.order_executor.place_order(ccxt_symbol, 'buy', 0, short_qty, True, 'short', 'market')
                logger.info(f"[風控] {sym_config.symbol} 市價平空 {short_qty}")

            self.state.last_reduce_time[ccxt_symbol] = time.time()
```

- [ ] **Step 4: 跑測試確認通過**

Run: `../.venv/bin/python -m pytest tests/test_risk_monitor_reduce.py -v`
Expected: PASS（11 passed）

- [ ] **Step 5: mutation A —— 改回一律等量**

把 `extra = min(reduce_qty, gap)` 改成 `extra = 0.0`，跑：
Run: 同 Step 4
Expected: `test_gross_strictly_decreases[0.8-0.64]` **FAIL**（總減量少了 `min(reduce_qty, gap)`）。
確認後還原。

- [ ] **Step 6: mutation B —— 改回 v2 的固定 2×（關鍵）**

把 `extra = min(reduce_qty, gap)` 改成 `extra = reduce_qty`，跑：
Run: 同 Step 4
Expected: `test_reduce_never_increases_abs_delta_and_never_flips_sign[0.66-0.64]` **FAIL**，
訊息應顯示 `|delta| 變大了：0.020000 → 0.060000` 或 `delta 變號`。
確認後還原。**這一步是整個 task 最重要的驗收**——它證明測試抓得到 v2 的真實缺陷。

- [ ] **Step 7: 跑全套測試**

Run: `../.venv/bin/python -m pytest -q`
Expected: 全綠。

- [ ] **Step 8: Commit**

```bash
git add grid_engine/risk_monitor.py tests/test_risk_monitor_reduce.py
git commit -m "fix(risk): 雙向減倉改為不對稱，且 |delta| 永不增加

現行「兩側各減 threshold*0.1」是等量減倉 ⇒ delta 完全不變（降的是 gross，不是
中性度）。改為大側多減 min(reduce_qty, gap)：
  new_delta = sign(delta) * max(0, gap - reduce_qty)
⇒ |delta| 永不增加、永不變號，gross 嚴格下降 2*reduce_qty + extra。

夾到 gap 而非固定 2×：後者在 gap < reduce_qty/2 時會 overshoot（反例 0.66/0.64
使 |delta| 由 0.02 惡化到 0.06），而任何一次觸發後雙側都掉到門檻以下、沒有下一輪
可以修正。夾到 gap 也消掉了浮點相等比較（gap==0 時 extra==0 自動退回舊行為）。

已驗證兩條 mutation 各紅一次：extra=0（回等量）與 extra=reduce_qty（v2 提案）。

注意：本路徑在現行資本下不可達（雙側同時 >=0.64 所需保證金遠超可用 17.96）
⇒ 這是 regression guard，不是 live fix 的證據（spec §6）。"
```

---

### Task 6: A4 —— replay 全量結構化 diff 驗證

**Files:**
- Create: `<scratchpad>/replay_diff_check.py`（**不進 repo**）

**Interfaces:**
- Consumes: Task 1 的新 `tp_quantity`
- Produces: 無（驗收產物）

**背景（spec §5.1）：** 新規則是舊規則的真子集 ⇒ 所有 diff 單向（止盈 qty 由 2× 降 1×）。
**A4 是實作 guard，不是策略證據**（spec §6）——它只顯示一階 qty 差，不含路徑分歧。

- [ ] **Step 1: 寫驗證腳本**

寫到 scratchpad（路徑用 `echo $TMPDIR` 或本 session 的 scratchpad 目錄）：

```python
"""A4：全量 replay 結構化 diff 驗證（spec §5.1）。唯讀，只讀 logs/decisions.jsonl。"""
import json
import sys
from pathlib import Path

REPO = Path("/Users/ramonliao/Documents/理財/加密貨幣/量化交易/LouisLab/as-grid-dragon")
sys.path.insert(0, str(REPO))

from grid_engine.replay import load_records, replay_record   # noqa: E402

LOG = REPO / "logs" / "decisions.jsonl"

def side_state(inp, side):
    my = inp["long_position"] if side == "long" else inp["short_position"]
    opp = inp["short_position"] if side == "long" else inp["long_position"]
    return my, opp, inp["position_limit"], inp["position_threshold"]

def classify(my, opp, limit, threshold):
    new_doubles = my > limit and my > opp
    cls1 = my > limit and my <= opp
    cls2 = opp >= threshold and not new_doubles
    return cls1, cls2

total = diffs = 0
violations = []
counts = {"cls1_only": 0, "cls2_only": 0, "both": 0}

for rec in load_records(LOG):
    total += 1
    replayed = replay_record(rec)
    expected = rec["decision"]
    if replayed == expected:
        continue
    diffs += 1
    for side in ("long", "short"):
        r_side, e_side = replayed[side], expected[side]
        if r_side == e_side:
            continue
        # 斷言 1：diff 只在 reduce_only 訂單的 quantity
        r_others = [{k: v for k, v in o.items() if k != "quantity"} for o in r_side["orders"]]
        e_others = [{k: v for k, v in o.items() if k != "quantity"} for o in e_side["orders"]]
        rest_r = {k: v for k, v in r_side.items() if k != "orders"}
        rest_e = {k: v for k, v in e_side.items() if k != "orders"}
        if rest_r != rest_e or r_others != e_others:
            violations.append(("非 quantity 欄位有差", rec["ts"], side)); continue
        for ro, eo in zip(r_side["orders"], e_side["orders"]):
            if ro["quantity"] == eo["quantity"]:
                continue
            if not eo["reduce_only"]:
                violations.append(("差在非 reduce_only 訂單", rec["ts"], side)); continue
            # 斷言 3：恰為兩倍、單向
            if abs(ro["quantity"] * 2 - eo["quantity"]) > 1e-12:
                violations.append((f"非兩倍關係 {ro['quantity']}→{eo['quantity']}", rec["ts"], side)); continue
            # 斷言 2：該側屬類 1 或類 2
            my, opp, limit, threshold = side_state(rec["inputs"], side)
            cls1, cls2 = classify(my, opp, limit, threshold)
            if not (cls1 or cls2):
                violations.append(("不屬類1也不屬類2", rec["ts"], side)); continue
            key = "both" if (cls1 and cls2) else ("cls1_only" if cls1 else "cls2_only")
            counts[key] += 1

print(f"總筆數 {total}、有 diff 的筆數 {diffs}")
print(f"分類：{counts}（合計 {sum(counts.values())}）")
print(f"違規 {len(violations)} 筆")
for v in violations[:20]:
    print("  ", v)
print("A4:", "PASS" if not violations else "FAIL")
```

- [ ] **Step 2: 執行**

Run: `../.venv/bin/python <scratchpad>/replay_diff_check.py`
Expected:
- `違規 0 筆`、`A4: PASS`
- 分類數量應接近 reviewer 實測的 `cls2_only ≈ 81,957 / both ≈ 16,442 / cls1_only ≈ 870`
  （檔案在成長，數字會略增；**比例明顯不符就要停下查**）
- ⚠️ 若出現任何違規 → **停下報告，不要自行放寬斷言**。

- [ ] **Step 3: 落檔結果**

把 stdout 存成 `<scratchpad>/a4_replay_result.txt`，在最終報告引用數字。**不 commit。**

---

### Task 7: A6 + A9 —— tick_sim 新舊對照與保證金試算

**Files:**
- Create: `<scratchpad>/ab_tick_sim.py`（**不進 repo**）

**Interfaces:**
- Consumes: Task 1 的新 `tp_quantity`
- Produces: 無（驗收產物）

- [ ] **Step 1: 寫 A/B 腳本**

```python
"""A6：新舊 tp_quantity 規則的 tick_sim 對照（spec §5.2）+ A9 保證金試算。

舊規則的 shim 必須自行注入 threshold——改簽名後 compute_quantity 以 4 引數呼叫，
直接貼 HEAD 版舊函式會 TypeError（spec §5.2 / v1 SF4）。
"""
import sys
import time
from pathlib import Path

REPO = Path("/Users/ramonliao/Documents/理財/加密貨幣/量化交易/LouisLab/as-grid-dragon")
sys.path.insert(0, str(REPO))

import grid_engine.decision as d                                   # noqa: E402
from backtest.aggtrades import AggTradesLoader                     # noqa: E402
from backtest.tick_sim import TickSimConfig                        # noqa: E402
from scripts.requote_experiment import (                           # noqa: E402
    Cell, run_cell, compute_windows, load_events, load_funding_events, SCENARIOS, PROD,
)

# ── 舊規則 shim ────────────────────────────────────────────────
_THRESHOLD = PROD["initial_quantity"] * PROD["threshold_multiplier"]   # 0.02 × 40 = 0.8

def _tp_quantity_legacy(base_qty, my_position, opposite_position, position_limit):
    if my_position > position_limit or opposite_position >= _THRESHOLD:
        return base_qty * 2
    return base_qty

_tp_quantity_new = d.tp_quantity

# ── 前提 assert（v1 N1：預設值忘設也看不出來）────────────────────
_probe = TickSimConfig(grid_spacing=0.003, take_profit_spacing=0.003,
                       initial_quantity=0.02, leverage=5.0, initial_balance=100.0)
assert _probe.threshold_multiplier == 40.0, "TickSimConfig 預設值變了，請重新確認"
assert _probe.limit_multiplier == 5.0, "TickSimConfig 預設值變了，請重新確認"
assert _THRESHOLD == 0.8, _THRESHOLD

windows, _, _ = compute_windows("2026-06-06", "2026-07-10", "2026-07-13")
agg = AggTradesLoader()
events, days = load_events(agg, "2026-06-06", "2026-07-13")
funding = load_funding_events("2026-06-06", "2026-07-13")
print(f"events={len(events)} days={len(days)}")

def delta_path(res, scen):
    """由 fills 重建 delta 軌跡（spec §5：qty 累加，不受 FIFO 均價分歧影響）。"""
    sc = SCENARIOS[scen]
    lp, sp = sc["seed_long_qty"], sc["seed_short_qty"]
    peak = abs(lp - sp)
    for f in sorted(res.fills, key=lambda x: x["ts_ms"]):
        sign = 1 if f["kind"] == "entry" else -1
        if f["side"] == "long":
            lp += sign * f["qty"]
        else:
            sp += sign * f["qty"]
        peak = max(peak, abs(lp - sp))
    return peak, abs(lp - sp)

rows = []
for rule_name, fn in (("OLD", _tp_quantity_legacy), ("NEW", _tp_quantity_new)):
    d.tp_quantity = fn
    for scen in ("A", "B"):
        for wname, (ws, we) in windows.items():
            # factor=0.5 = 生產的 requote_threshold_factor（PROD dict 內沒有這個 key，
            # 它是 Cell 的第一個欄位）。fee=0/slip=0：實查 BNBUSDC maker 費率為 0。
            c = Cell(0.5, scen, wname, ws, we, 0, 0, 500, 5.0, "ab")
            t = time.time()
            r = run_cell(c, events, funding)
            # run_cell 回傳 dict，需要 fills → 直接改用 res 物件不可行，故此處
            # 以 run_cell 的統計欄位為主，delta 由下面第二輪單獨取得
            r.update(rule=rule_name, secs=time.time() - t)
            rows.append(r)
            print(f"  {rule_name} {scen} {wname:5s} eq={r['final_equity']:9.3f} "
                  f"dd={r['max_dd']:.4f} liq={r['liquidated']} rt={r['round_trips']} "
                  f"rej={r['rejected_rate']:.4f} ({r['secs']:.0f}s)", flush=True)
d.tp_quantity = _tp_quantity_new    # 還原，避免污染同 process 後續呼叫

print("\n=== A6 對照表 ===")
hdr = f"{'scen':5s} {'win':6s} {'rule':5s} {'final_eq':>10s} {'maxDD':>8s} {'liq':>5s} {'rt':>6s} {'rej%':>7s}"
print(hdr)
for r in rows:
    print(f"{r['scenario']:5s} {r['window']:6s} {r['rule']:5s} {r['final_equity']:>10.3f} "
          f"{r['max_dd']:>8.4f} {str(r['liquidated']):>5s} {r['round_trips']:>6} "
          f"{r['rejected_rate']*100:>6.2f}%")

# ── A9 保證金試算（照健檢 §8 的算法）────────────────────────────
px, q, lev, avail = 570.66, 0.02, 5.0, 17.955
per_layer = q * px / lev
print(f"\n=== A9 保證金試算 ===\n每層 {q} @{px} @{lev}x 需保證金 {per_layer:.3f}")
print(f"可用 {avail} → 可再加 {int(avail // per_layer)} 層")
if int(avail // per_layer) < 3:
    print("⚠️ 氧氣不足（可用層數 < 3），必須在上線報告中明確標示")
```

- [ ] **Step 2: 先確認 delta 指標拿得到**

`run_cell` 回傳 dict、不含 `fills`。**先跑一個 cell 確認**：若 `run_cell` 的回傳確實沒有
`fills`，改成直接呼叫 `backtest.tick_sim.run_tick_sim(ev, cfg)` 取 `TickSimResult`
（`_make_cfg` / `_slice_events` / `_slice_funding` 都可從 `scripts.requote_experiment` import），
再用 `delta_path()` 算 `max abs(delta)` 與 `final abs(delta)`。
**delta 是 A6 的主判準（spec §6：equity 只看方向），拿不到就不算完成。**

- [ ] **Step 3: 執行並逐窗判 gate**

Run: `../.venv/bin/python <scratchpad>/ab_tick_sim.py`
逐窗（W1/W2/W3/full × 場景 A/B）判 spec A6 的五道 gate：
1. `max abs(delta)` 與 `final abs(delta)`：NEW ≤ OLD
2. 零強平（兩規則都是 `liquidated=False`）
3. `final_equity` 劣化 ≤ 1.0 USDC
4. `max_drawdown` 劣化 ≤ 2 個百分點
5. `rejected_entries` 增幅 ≤ 50%（基準為 0 時改判「NEW > 5 筆即超標」）

**任一項超標 → 停下報告，不自行放行。**

- [ ] **Step 4: 落檔結果**

存成 `<scratchpad>/a6_ab_result.txt`。**不 commit。**

---

### Task 8: 收官 —— 全套測試、read-back、上線清單

**Files:**
- Modify: `tasks/progress.md`

- [ ] **Step 1: 全套測試**

Run: `../.venv/bin/python -m pytest -q`
Expected: `546 + 新增測試數` passed / 1 skipped，零 FAIL。報**數量**不報形容詞。

- [ ] **Step 2: 逐點 read-back（`tests/` 抓不到的檔）**

依 lessons：`tests/` 零 import `as_terminal_max` 與 `web/pages/*`。逐一 `py_compile` 並
重新讀一遍改動處，確認語意正確：

Run: `../.venv/bin/python -m py_compile grid_engine/bot.py grid_engine/ui.py grid_engine/risk_monitor.py grid_engine/decision.py backtest/backtester.py && echo OK`

- [ ] **Step 3: grep 全域一致性**

Run: `grep -rn "tp_quantity" grid_engine backtest scripts tests`
Expected: 零 5 引數呼叫。

Run: `grep -rn "position_limit" grid_engine/ui.py grid_engine/bot.py`
Expected: `ui.py` 只在 `position_status_labels` 內使用；`bot.py` 不再有加倍分支。

- [ ] **Step 4: 更新 progress.md**

把 TODO 1c 標為「code 完成，等 dual-review + verifier」，記錄：
新增測試數、A4 的分類數字、A6 的逐窗 gate 結果、A9 的可用層數。

- [ ] **Step 5: Commit**

```bash
git add tasks/progress.md
git commit -m "docs(tasks): TODO 1c code 完成——A4/A6/A9 驗收數字入檔"
```

- [ ] **Step 6: 交回主對話**

**不要自行 merge、不要重啟引擎。** 依 dev-rules 還需要：
1. `security-review` skill（改動命中 Red Team Protocol 適用範圍：會改真錢下單行為）
2. `dual-review` skill Round 1 外部輪（fresh-context，**不給 spec 與任何自述**）+ Round 2 專案規則
3. fresh-context `verifier`（read-back + 實跑 + 獨立 mutation + **Monkey Testing 專門回合**）
4. 上線前：依 spec §7 手動撤掉交易所那 4 張掛單，再重啟引擎
5. 上線後：依 spec §7 的活體 grep 驗收（小側止盈 qty 應為 0.02、大側 0.04）與回退條件表

---

## Self-Review

**1. Spec coverage**

| spec 項 | 對應 task |
|---|---|
| §3.1 `tp_quantity` 新規則 + 簽名 | Task 1 |
| §3.2 `risk_monitor` 不對稱減倉（v3 不變式） | Task 5 |
| §3.3 `bot.py` 死碼 | Task 3 |
| §3.4 `backtester.py:84` 第二個 caller | Task 1 Step 5 |
| §3.4 `tests/test_decision.py:28` 刪除被刪子條件的斷言 | Task 1 Step 1 |
| §3.4 `ui.py:126-133` 標籤 | Task 4 |
| §3.5 clamp 測試改造 | Task 2 |
| §4 向量 1（相等不加倍） | Task 1 Step 1、Task 4 Step 1 |
| §4 向量 2（零倉 / 單側零倉） | Task 1 Step 1（`my <= limit`、`opp = 0`）|
| §4 向量 3（裝死側是小側） | Task 1 Step 1 末段斷言（self-review 發現原本漏掉，已補進去） |
| §4 向量 4（減倉 overshoot） | Task 5 Step 6 mutation B |
| §4 向量 5（極小 limit 的 symbol） | spec §8 backlog，本計畫不做（僅 BNBUSDC enabled）|
| §4 向量 6（gross 更大 → 拒單） | Task 7 Step 3 gate 5 |
| A1~A3 | Task 1 |
| A4 | Task 6 |
| A5 | Task 5 |
| A6 | Task 7 |
| A7 | 每個 task 最後一步 + Task 8 |
| A8 | Task 2 Step 5 |
| A9 | Task 7 Step 1 尾段 |
| §7 上線/回退 | Task 8 Step 6 |

**2. Placeholder scan** —— 無 TBD/TODO；每個 code step 都有可直接貼的完整程式碼。
Task 7 Step 2 有一個**條件分支**（`run_cell` 是否回傳 `fills`），已寫明兩條路徑與判斷方式，
不是 placeholder；`<scratchpad>` 是本 session 的實際路徑佔位，執行時替換。

**3. Type consistency** —— `tp_quantity` 全程 4 引數（Task 1 定義、Task 6/7 的 shim 同簽名）；
`position_status_labels` 在 Task 4 定義與測試中簽名一致（4 個位置參數、回傳 `list[str]`）；
`place_order` 的 7 個位置參數與 `risk_monitor.py` 現行呼叫、Task 5 測試的 `_RecordingExecutor`
一致；`SCENARIOS` / `PROD` / `Cell` / `run_cell` 名稱與 `scripts/requote_experiment.py` 實際一致。
