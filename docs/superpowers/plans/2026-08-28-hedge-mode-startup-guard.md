# Hedge 模式啟動守衛 + `ps='BOTH'` 事件防禦網 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 bot 在啟動時硬性確立帳戶處於 hedge（雙向持倉）模式，確立不了就不啟動；並在 userData 事件層對 `positionSide='BOTH'` 加一道不會靜默的防禦網。

**Architecture:** 兩處改動，都在 `grid_engine/bot.py`。(1) `_check_hedge_mode` 從「查一下、失敗就 `pass`」改成「查 → 必要時切換 → **複驗** → 三種失敗各自 `raise`」，raise 由既有的 `run()` except（`bot.py:865-871`）接住做乾淨返回。(2) `_handle_order_update` 在 `FILLED` 分支開頭對非 `LONG`/`SHORT` 的 `positionSide` 早退 + 節流 warning，比照 `_handle_account_update:740-741` 既有的處理方式。不新增檔案以外的模組，不改動下單路徑。

**Tech Stack:** Python 3.x, asyncio, ccxt 4.5.32 (binance USDⓈ-M), pytest / pytest-asyncio, unittest.mock

**Spec:** `tasks/spec.md`（本 repo，2026-08-28 版：「hedge 模式啟動守衛 + `ps='BOTH'` 事件防禦網」）

## Global Constraints

- 測試基線 **851 passed / 2 skipped**（2026-08-28 於 `git archive HEAD` 乾淨快照實測，`HEAD == be51cf9`）。改動後不得退步。
- **所有工作都在 worktree 內完成，指令 cwd 一律是 worktree 根目錄，不得 `cd` 出去**（會毒化 subagent 的 shell）。live engine 跑在主目錄，worktree 內的測試寫 `config/`/`logs/` 碰不到它。
- 本次改動**不得新增任何下單、撤單、改倉行為**。唯一新增的外部呼叫是 `_check_hedge_mode` 內對 `fetch_position_mode` 的**唯讀**複驗查詢。
- **不得改動 `bot.py:865-871`** 的 `run()` except 區塊（raise 的接手處，爆炸半徑已量過）。
- **不得改動 `sym_state.ws_seq += 1` 的位置與時機**（`bot.py:810`）——那是上一個任務 C1 競態修復的一部分；BOTH 早退必須發生在該遞增**之前**。
- 複驗的 `time.sleep` 只允許出現在 `_check_hedge_mode` 內（該函式是同步的、跑在 `gateway.call` 的 worker thread）。不得改成 `asyncio.sleep`。
- 不支援 one-way 模式：不得改 `order_executor.place_order` 的 `positionSide` 傳法，不得新增「偵測到 one-way 就改用無 positionSide 下單」的降級路徑。
- `git add` 只 stage 明確列出的檔案，禁止 `git add -A` / `git add .`。

### 測試指令

**在 worktree 根目錄直接跑，不要 `cd` 到 worktree 以外的任何路徑。**

worktree 是獨立目錄，測試若寫 `config/` 或 `logs/` 只會寫進 worktree，碰不到
正在運行的 live engine（主目錄）。因此不需要 `git archive` 快照那一套。

```bash
PYTHONPATH=. /Users/ramonliao/Documents/理財/加密貨幣/量化交易/LouisLab/.venv/bin/python \
  -m pytest tests/test_hedge_mode_guard.py -q -p no:cacheprovider
```

⚠️ **`cd` 出 worktree 會毒化 subagent 的 shell**（2026-08-27 三位 verifier 實踩，
見 `tasks/lessons.md` 與 memory `worktree-shell-poisoning`）。所有指令的 cwd
一律是 worktree 根目錄。

（`uv run pytest` 在此 sandbox 不可用——uv project root 是父目錄 `LouisLab/`。）

### Mutation 的做法

**先 commit 實作，再跑 mutation。** 這樣還原就是一行 `git checkout -- <path>`，
不會有「還原時把未提交的成果一起洗掉」的風險（lessons L1 事故）。

每個 mutation 的三步：
1. 用 Edit 改那一行（只改那一行）
2. 跑該測試檔，記下**紅在哪一行斷言**與實得值
3. `git checkout -- grid_engine/bot.py` 還原，重跑確認回綠

## 檔案結構

| 檔案 | 責任 | 動作 |
|---|---|---|
| `grid_engine/bot.py:63` 附近 | 新增兩個模組常數 | Modify |
| `grid_engine/bot.py:148` 附近（`__init__`） | 新增 `_last_unknown_ps_log_at` 節流狀態 | Modify |
| `grid_engine/bot.py:227-236` | `_check_hedge_mode` 重寫 + 新增 `_fetch_hedged` helper | Modify |
| `grid_engine/bot.py:786-819` | `_handle_order_update` 的 `FILLED` 分支加早退守衛 + 新增 `_note_unknown_position_side` | Modify |
| `tests/test_hedge_mode_guard.py` | 兩個守衛的全部測試 | Create |

兩個守衛共用「position mode 這件事」這一個關注點，測試放同一檔；`bot.py` 已是大檔但本次只加 ~60 行，不做拆檔（spec Non-goals）。

---

### Task 1: `_check_hedge_mode` 啟動守衛

**Files:**
- Modify: `grid_engine/bot.py:63`（常數）、`grid_engine/bot.py:227-236`（函式重寫）
- Test: `tests/test_hedge_mode_guard.py`（Create）

**Interfaces:**
- Consumes: `self.exchange.fetch_position_mode(symbol=...)`（ccxt 4.5.32，回 `{'info': dict, 'hedged': bool | None}`，`safe_bool` 在 `dualSidePosition` 缺失時回 `None` —— 見 `binance.py:12791`）；`self.exchange.fapiPrivatePostPositionSideDual({'dualSidePosition': 'true'})`
- Produces:
  - `MaxGridBot._fetch_hedged(self, ccxt_symbol: str) -> tuple[bool | None, str | None]` —— 回 `(hedged, err)`。`err` 非 `None` 代表查詢本身失敗（字串為錯誤訊息）；`hedged` 為 `None` 代表交易所沒回報該欄位。
  - `MaxGridBot._check_hedge_mode(self) -> None` —— 成功則靜默返回，三種失敗各 `raise RuntimeError`。
  - 模組常數 `HEDGE_MODE_VERIFY_ATTEMPTS = 3`、`HEDGE_MODE_VERIFY_DELAY_SEC = 1.0`

- [ ] **Step 1: 寫失敗測試**

建立 `tests/test_hedge_mode_guard.py`：

```python
"""持倉模式（position mode）守衛測試。

兩道守衛守同一個前提：**這隻 bot 只能在 hedge（雙向持倉）模式下運作**。
order_executor.place_order 對每一張網格單都帶 positionSide（order_executor.py:90-91），
而 position mode 是幣安帳戶層設定 —— one-way 模式下這些單會被整批拒絕，
bot 一張單都下不出去，只會一路撞下單斷路器。

  守衛 1（啟動）：_check_hedge_mode 確立不了 hedge 就 raise，由 run() 的
                  except（bot.py:865-871）接成乾淨返回，不啟動。
  守衛 2（運行期）：_handle_order_update 對 ps 非 LONG/SHORT 的成交事件早退，
                  不重置掛單計數、不餵 bandit、不重掛網格。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from grid_engine import clock
from grid_engine.bot import MaxGridBot
from grid_engine.config import GlobalConfig, SymbolConfig

SYMBOL = "XRP/USDC:USDC"


def _make_bot(enabled=True):
    cfg = GlobalConfig()
    cfg.symbols = {SYMBOL: SymbolConfig(
        symbol="XRPUSDC", ccxt_symbol=SYMBOL, enabled=enabled,
        take_profit_spacing=0.003, grid_spacing=0.003, initial_quantity=0.02,
        limit_multiplier=5.0, threshold_multiplier=20.0,
    )}
    cfg.bandit.enabled = False
    bot = MaxGridBot(cfg)
    bot.order_executor.place_order = AsyncMock()
    return bot


def _exchange(mode_results, switch_error=None):
    """mode_results: fetch_position_mode 依序回傳的 hedged 值（True/False/None），
    或 Exception 實例（該次呼叫拋出）。清單耗盡後重複最後一項。"""
    ex = MagicMock()
    seq = list(mode_results)

    def _fetch(symbol=None, **kw):
        item = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(item, Exception):
            raise item
        return {"info": {}, "hedged": item}

    ex.fetch_position_mode.side_effect = _fetch
    if switch_error is not None:
        ex.fapiPrivatePostPositionSideDual.side_effect = switch_error
    return ex


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """複驗間隔不要真的睡 —— 但要留下呼叫紀錄，證明間隔存在。"""
    calls = []
    monkeypatch.setattr("grid_engine.bot.time.sleep", lambda s: calls.append(s))
    yield calls
    clock.reset_clock()
    clock.reset_guard_clock()


class TestCheckHedgeMode:
    def test_already_hedged_passes_without_switching(self):
        bot = _make_bot()
        bot.exchange = _exchange([True])
        bot._check_hedge_mode()
        bot.exchange.fapiPrivatePostPositionSideDual.assert_not_called()

    def test_fetch_failure_aborts_startup(self):
        """查不到就不啟動（使用者 2026-08-28 裁決：不寬容「查不到」）。"""
        bot = _make_bot()
        bot.exchange = _exchange([RuntimeError("network down")])
        with pytest.raises(RuntimeError, match="查詢持倉模式失敗"):
            bot._check_hedge_mode()
        bot.exchange.fapiPrivatePostPositionSideDual.assert_not_called()

    def test_hedged_none_aborts_startup(self):
        """ccxt safe_bool 在 dualSidePosition 缺失時回 None —— 未知不等於 False，
        不得當成「非 hedge」去切換，也不得當成「是 hedge」放行。"""
        bot = _make_bot()
        bot.exchange = _exchange([None])
        with pytest.raises(RuntimeError, match="未回報持倉模式"):
            bot._check_hedge_mode()
        bot.exchange.fapiPrivatePostPositionSideDual.assert_not_called()

    def test_switch_rejected_by_exchange_aborts_startup(self):
        """帳戶有持倉/掛單時幣安會拒絕 dualSidePosition 切換 —— 原本這被
        `except Exception: pass` 吞掉，bot 帶著錯誤的模式假設繼續啟動。"""
        bot = _make_bot()
        bot.exchange = _exchange([False], switch_error=RuntimeError("-4068"))
        with pytest.raises(RuntimeError, match="切換持倉模式被交易所拒絕"):
            bot._check_hedge_mode()

    def test_switch_then_verify_succeeds(self, _no_real_sleep):
        """切換在交易所端非同步生效：第一次複驗仍讀到舊值，第二次才確認。"""
        bot = _make_bot()
        bot.exchange = _exchange([False, False, True])
        bot._check_hedge_mode()
        bot.exchange.fapiPrivatePostPositionSideDual.assert_called_once()
        assert bot.exchange.fetch_position_mode.call_count == 3
        assert _no_real_sleep == [1.0], "第二次複驗前必須有間隔，否則等於沒複驗"

    def test_verify_never_confirms_aborts_startup(self, _no_real_sleep):
        """切換呼叫沒拋錯，但模式實際沒變 —— 這正是「不複驗就會漏掉」的形態。"""
        bot = _make_bot()
        bot.exchange = _exchange([False])
        with pytest.raises(RuntimeError, match="複驗"):
            bot._check_hedge_mode()
        assert bot.exchange.fetch_position_mode.call_count == 4  # 1 次初查 + 3 次複驗
        assert _no_real_sleep == [1.0, 1.0]

    def test_no_enabled_symbol_skips_check(self):
        """沒有啟用中的 symbol，本來就不會下單 —— 不該因為查不到模式而擋下啟動。"""
        bot = _make_bot(enabled=False)
        bot.exchange = _exchange([RuntimeError("should not be called")])
        bot._check_hedge_mode()
        bot.exchange.fetch_position_mode.assert_not_called()
```

- [ ] **Step 2: 跑測試確認它紅**

```bash
PYTHONPATH=. /Users/ramonliao/Documents/理財/加密貨幣/量化交易/LouisLab/.venv/bin/python \
  -m pytest tests/test_hedge_mode_guard.py -q -p no:cacheprovider
```

Expected: FAIL —— 舊實作 `except Exception: pass`，`test_fetch_failure_aborts_startup` 等會是 `DID NOT RAISE`。

- [ ] **Step 3: 加模組常數**

在 `grid_engine/bot.py:63` 的 `STALE_QUOTE_LOG_SECONDS` 旁：

```python
STALE_QUOTE_LOG_SECONDS = 3600.0  # 價格過期 log 節流間隔（秒），不洗版
# 切換 dualSidePosition 後交易所端非同步生效，立刻複驗可能讀到舊值
HEDGE_MODE_VERIFY_ATTEMPTS = 3
HEDGE_MODE_VERIFY_DELAY_SEC = 1.0
```

- [ ] **Step 4: 重寫 `_check_hedge_mode`**

把 `grid_engine/bot.py:227-236` 整段換成：

```python
    def _fetch_hedged(self, ccxt_symbol: str):
        """查帳戶持倉模式，回 `(hedged, err)`。

        兩種「不是 True/False」要分開回報，因為它們的訊息與後續動作不同：
          - `err` 非 None：查詢本身失敗（網路/限流/權限）。
          - `hedged is None`：查得到但交易所沒回報 dualSidePosition。
            ccxt 4.5.32 的 `safe_bool` 在欄位缺失時回 None（binance.py:12791），
            所以 `mode['hedged']` 不會 KeyError，但會是三態。
        """
        try:
            mode = self.exchange.fetch_position_mode(symbol=ccxt_symbol)
        except Exception as e:
            return None, str(e)
        if not isinstance(mode, dict):
            return None, None
        return mode.get('hedged'), None

    def _check_hedge_mode(self):
        """啟動守衛：確立帳戶處於 hedge（雙向持倉）模式，確立不了就 raise。

        為什麼是硬失敗而不是告警續跑：`order_executor.place_order` 對每一張
        網格單都帶 `positionSide`（order_executor.py:90-91），而 position mode
        是幣安**帳戶層**設定 —— one-way 模式下這些單會被整批拒絕，bot 一張單
        都下不出去，只會一路撞 `_register_order_failure` 直到斷路。帶著未經
        證實的模式假設啟動，等於把「完全不能交易」偽裝成一串看不懂的下單失敗。

        raise 由 `run()` 的 except（bot.py:865-871）接住 → notify_crash 一封
        + gateway.shutdown() + return，是**乾淨返回而非行程崩潰**，不會觸發
        container restart policy 造成重啟迴圈。

        position mode 是帳戶層設定，因此只查一次（取第一個啟用中的 symbol），
        不逐 symbol 重複查。
        """
        sym_config = next(
            (c for c in self.config.symbols.values() if c.enabled), None
        )
        if sym_config is None:
            return  # 沒有啟用中的 symbol ⇒ 不會下單 ⇒ 這個前提無關緊要

        hedged, err = self._fetch_hedged(sym_config.ccxt_symbol)
        if hedged is True:
            return
        if err is not None:
            raise RuntimeError(
                f"[MAX] 查詢持倉模式失敗，無法確認帳戶是否為雙向持倉模式，"
                f"拒絕啟動（本 bot 的每張單都帶 positionSide，單向模式下會被"
                f"整批拒絕）：{err}"
            )
        if hedged is None:
            raise RuntimeError(
                "[MAX] 交易所未回報持倉模式（dualSidePosition 欄位缺失），"
                "無法確認是否為雙向持倉模式，拒絕啟動"
            )

        logger.warning(
            f"[MAX] 偵測到帳戶為單向持倉模式，嘗試切換為雙向持倉模式"
            f"（{sym_config.symbol}）"
        )
        try:
            self.exchange.fapiPrivatePostPositionSideDual(
                {'dualSidePosition': 'true'}
            )
        except Exception as e:
            raise RuntimeError(
                f"[MAX] 切換持倉模式被交易所拒絕，拒絕啟動"
                f"（帳戶有持倉或掛單時無法切換，需先手動平倉/撤單）：{e}"
            ) from e

        for attempt in range(HEDGE_MODE_VERIFY_ATTEMPTS):
            if attempt:
                time.sleep(HEDGE_MODE_VERIFY_DELAY_SEC)
            again, _ = self._fetch_hedged(sym_config.ccxt_symbol)
            if again is True:
                logger.info("[MAX] 已切換為雙向持倉模式並複驗通過")
                return

        raise RuntimeError(
            f"[MAX] 切換持倉模式後複驗 {HEDGE_MODE_VERIFY_ATTEMPTS} 次仍非"
            f"雙向持倉模式，拒絕啟動（切換呼叫沒報錯但實際未生效）"
        )
```

- [ ] **Step 5: 跑測試確認全綠**

Run: 同 Step 2 指令。Expected: `7 passed`。

- [ ] **Step 6: 先 Commit（見 Step 9 的訊息），再跑 mutation**

先提交才能用 `git checkout --` 安全還原 mutation。

- [ ] **Step 7: 實跑 mutation M-A（最後的 raise → return）**

改 `_check_hedge_mode` 結尾的 `raise RuntimeError(f"[MAX] 切換持倉模式後複驗...")` 為 `return`，重跑。
Expected RED: `test_verify_never_confirms_aborts_startup` —— `DID NOT RAISE <class 'RuntimeError'>`。
**還原改動**後重跑確認回綠。

- [ ] **Step 8: 實跑 mutation M-B（刪掉複驗迴圈）**

把 `for attempt in range(...)` 整個迴圈與其後的 raise 換成 `return`（＝切換呼叫沒拋錯就當成功），重跑。
Expected RED: `test_verify_never_confirms_aborts_startup`（`DID NOT RAISE`）**與** `test_switch_then_verify_succeeds`（`fetch_position_mode.call_count` 實得 1、期望 3）。
**還原改動**後重跑確認回綠。

- [ ] **Step 9: 實跑 mutation M-E（`hedged is None` 當成通過）**

把 `if hedged is None: raise ...` 換成 `if hedged is None: return`，重跑。
Expected RED: `test_hedged_none_aborts_startup` —— `DID NOT RAISE`。
**還原改動**後重跑確認回綠。

- [ ] **Step 10: Commit（若前面已提交，本步只確認 `git status` 乾淨）**

```bash
git add grid_engine/bot.py tests/test_hedge_mode_guard.py
git commit -m "fix(bot): hedge 模式啟動守衛改為硬失敗 + 切換後複驗

_check_hedge_mode 原本 except Exception: pass 吞掉一切、切換
dualSidePosition 後不複驗。幣安在帳戶有持倉/掛單時會拒絕該切換，
於是 bot 帶著未經證實的模式假設啟動；而 one-way 模式下每張帶
positionSide 的網格單都會被拒，等於一張單都下不出去。

改為三種失敗各自 raise（查詢失敗 / 未回報欄位 / 複驗不過），由
run() 既有的 except 接成乾淨返回。position mode 是帳戶層設定，
改為只查第一個啟用中的 symbol。"
```

---

### Task 2: `_handle_order_update` 對 `ps='BOTH'` 的防禦網

**Files:**
- Modify: `grid_engine/bot.py:63` 附近（常數）、`grid_engine/bot.py:148` 附近（`__init__` 節流狀態）、`grid_engine/bot.py:786` 起（`FILLED` 分支開頭）、新增 `_note_unknown_position_side`
- Test: `tests/test_hedge_mode_guard.py`（Modify，接在 Task 1 之後）

**Interfaces:**
- Consumes: Task 1 無直接依賴（兩個守衛獨立）；沿用既有的 `clock.guard_now()`、`self._last_stale_log_at` 的節流慣例
- Produces:
  - `MaxGridBot._note_unknown_position_side(self, ccxt_symbol: str, symbol_raw: str, position_side: str) -> None`
  - 模組常數 `UNKNOWN_PS_LOG_SECONDS = 3600.0`
  - `self._last_unknown_ps_log_at: Dict[str, float]`

- [ ] **Step 1: 寫失敗測試**

追加到 `tests/test_hedge_mode_guard.py` 檔尾：

```python
def _filled_event(position_side, side="BUY", realized_pnl="1.5"):
    return {"o": {
        "s": "XRPUSDC", "X": "FILLED", "S": side,
        "ps": position_side, "rp": realized_pnl,
        "p": "0.5", "q": "10",
    }}


@pytest.fixture
def order_bot():
    """掛單計數初值刻意設成非 0：若設 0，「沒重置」與「重置了」不可分辨
    （lessons 通則 3.3：fixture 不得把待測維度壓成退化值）。"""
    bot = _make_bot()
    bot.adjust_grid = AsyncMock()
    bot.bandit_optimizer.record_trade = MagicMock()
    st = bot.state.symbols[SYMBOL]
    st.buy_long_orders = 3
    st.sell_long_orders = 4
    st.buy_short_orders = 5
    st.sell_short_orders = 6
    st.ws_seq = 7
    return bot


class TestOrderUpdatePositionSideGuard:
    @pytest.mark.asyncio
    async def test_both_position_side_is_not_applied(self, order_bot):
        """ps='BOTH' ⇒ 帳戶在單向持倉模式，分側狀態沒有正確映射。
        套用會把成交記到錯的一側、重置錯的掛單計數。"""
        st = order_bot.state.symbols[SYMBOL]
        await order_bot._handle_order_update(_filled_event("BOTH"))

        assert (st.buy_long_orders, st.sell_long_orders) == (3, 4)
        assert (st.buy_short_orders, st.sell_short_orders) == (5, 6)
        assert st.ws_seq == 7, "早退必須發生在 ws_seq 遞增之前"
        order_bot.bandit_optimizer.record_trade.assert_not_called()
        order_bot.adjust_grid.assert_not_called()

    @pytest.mark.asyncio
    async def test_both_is_not_recorded_as_short_in_bandit(self, order_bot):
        """改動前 `trade_side = 'long' if ps == 'LONG' else 'short'` 會把
        BOTH 靜默記成 short，汙染 bandit 的分側統計。"""
        await order_bot._handle_order_update(_filled_event("BOTH"))
        order_bot.bandit_optimizer.record_trade.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_position_side_is_not_applied(self, order_bot):
        st = order_bot.state.symbols[SYMBOL]
        await order_bot._handle_order_update(_filled_event("SIDEWAYS"))
        assert (st.buy_long_orders, st.ws_seq) == (3, 7)
        order_bot.adjust_grid.assert_not_called()

    @pytest.mark.asyncio
    async def test_long_still_applied_after_guard(self, order_bot):
        """守衛不得誤傷正常路徑。"""
        st = order_bot.state.symbols[SYMBOL]
        await order_bot._handle_order_update(_filled_event("LONG", side="BUY"))
        assert st.buy_long_orders == 0
        assert st.sell_long_orders == 4, "只該重置本次成交的那一格"
        assert st.ws_seq == 8
        order_bot.bandit_optimizer.record_trade.assert_called_once_with(1.5, 'long')
        order_bot.adjust_grid.assert_awaited_once_with(SYMBOL)

    @pytest.mark.asyncio
    async def test_short_still_applied_after_guard(self, order_bot):
        st = order_bot.state.symbols[SYMBOL]
        await order_bot._handle_order_update(_filled_event("SHORT", side="SELL"))
        assert st.sell_short_orders == 0
        assert st.buy_short_orders == 5
        order_bot.bandit_optimizer.record_trade.assert_called_once_with(1.5, 'short')

    @pytest.mark.asyncio
    async def test_warning_is_throttled_but_guard_is_not(self, order_bot, caplog):
        """節流只准影響 log，不准影響早退 —— 第二筆事件一樣不得被套用。"""
        st = order_bot.state.symbols[SYMBOL]
        with caplog.at_level("WARNING"):
            await order_bot._handle_order_update(_filled_event("BOTH"))
            await order_bot._handle_order_update(_filled_event("BOTH"))

        hits = [r for r in caplog.records if "單向持倉模式" in r.getMessage()]
        assert len(hits) == 1, "同一 symbol 的重複事件不得洗版"
        assert (st.buy_long_orders, st.ws_seq) == (3, 7), "第二筆一樣不得套用"
```

- [ ] **Step 2: 跑測試確認它紅**

Run: 同 Task 1 Step 2 指令。
Expected: FAIL —— `test_both_position_side_is_not_applied` 紅在 `assert st.ws_seq == 7`（實得 8），`test_both_is_not_recorded_as_short_in_bandit` 紅在 `assert_not_called` 收到 `call(1.5, 'short')`。

- [ ] **Step 3: 加常數與節流狀態**

在 Task 1 加的常數下方：

```python
UNKNOWN_PS_LOG_SECONDS = 3600.0  # 非 LONG/SHORT 的 positionSide 事件 log 節流
```

在 `grid_engine/bot.py:152` 的 `self._last_stale_at: Dict[str, float] = {}` 之後：

```python
        # 非 LONG/SHORT 的 positionSide 事件 log 節流（比照 _last_stale_log_at）
        self._last_unknown_ps_log_at: Dict[str, float] = {}
```

- [ ] **Step 4: 加 `_note_unknown_position_side`**

放在 `_handle_order_update` 之前：

```python
    def _note_unknown_position_side(self, ccxt_symbol: str, symbol_raw: str,
                                    position_side: str):
        """節流記錄「收到非 LONG/SHORT 的 positionSide」。

        只影響 log，不影響呼叫端的早退 —— 節流跟行為綁在一起就會變成
        「第二筆之後靜默套用」，那正是這道守衛要擋的事。
        """
        now = clock.guard_now()
        if now - self._last_unknown_ps_log_at.get(ccxt_symbol, 0.0) < UNKNOWN_PS_LOG_SECONDS:
            return
        self._last_unknown_ps_log_at[ccxt_symbol] = now
        logger.warning(
            f"[userData] {symbol_raw} positionSide={position_side!r} 非 LONG/SHORT，"
            f"本筆成交不套用（分側狀態無對應）—— 帳戶可能已被改成單向持倉模式，"
            f"此模式下網格單會被交易所整批拒絕"
        )
```

- [ ] **Step 5: 在 `FILLED` 分支開頭加早退守衛**

`grid_engine/bot.py:786` 的 `if order_status == 'FILLED':` 之後，緊接在該行下方插入（在既有的 `# total_trades / total_profit ...` 註解之前）：

```python
                if position_side not in ('LONG', 'SHORT'):
                    # ps='BOTH'（帳戶處在單向持倉模式）或未知值：本 bot 的狀態
                    # 是分側的（long/short_position、四個掛單計數、分側 dead
                    # mode），BOTH 沒有正確映射 —— 套用會把成交記到錯的一側
                    # （bandit）並重置錯的掛單計數。比照 _handle_account_update
                    # 對未知 ps 的處理（:740），本筆不套用。
                    #
                    # 正常情況下這條到不了：_check_hedge_mode 已在啟動時擋掉
                    # 單向模式，且單向模式下網格單根本下不出去（沒有成交就沒有
                    # FILLED 事件）。這是模式在運行期間被外部改掉時的防線。
                    # 刻意**不**呼叫 adjust_grid：模式錯時重掛網格只會製造更多
                    # 被拒的單。
                    self._note_unknown_position_side(
                        ccxt_symbol, symbol_raw, position_side)
                    return
```

- [ ] **Step 6: 跑測試確認全綠**

Run: 同 Step 2 指令。Expected: `13 passed`（Task 1 的 7 條 + 本 Task 的 6 條）。

- [ ] **Step 7: 先 Commit（見 Step 10 的訊息），再跑 mutation**

先提交才能用 `git checkout --` 安全還原 mutation。

- [ ] **Step 8: 實跑 mutation M-C（刪掉早退的 `return`）**

把 Step 5 那段的 `return` 刪掉（保留 warning，讓流程往下走），重跑。
Expected RED: `test_both_position_side_is_not_applied` —— 紅在 `assert st.ws_seq == 7`（實得 8）。
**還原改動**後重跑確認回綠。

- [ ] **Step 9: 實跑 mutation M-D（把 BOTH 放行）**

把守衛條件改成 `if position_side not in ('LONG', 'SHORT', 'BOTH'):`，重跑。
Expected RED: `test_both_is_not_recorded_as_short_in_bandit` —— `record_trade` 收到 `call(1.5, 'short')`，`assert_not_called` 失敗。
（M-D 與 M-C 的差別：M-C 殺掉整道守衛，M-D 只殺掉 `BOTH` 這一個值 —— 分開跑才證明得了守衛涵蓋 `BOTH` 而不只是未知值。）
**還原改動**後重跑確認回綠。

- [ ] **Step 10: 實跑 mutation M-F（節流改成永不 log）**

把 `_note_unknown_position_side` 的 `logger.warning(...)` 整段刪掉，重跑。
Expected RED: `test_warning_is_throttled_but_guard_is_not` —— 紅在 `assert len(hits) == 1`（實得 0）。
**還原改動**後重跑確認回綠。

- [ ] **Step 11: Commit（若前面已提交，本步只確認 `git status` 乾淨）**

```bash
git add grid_engine/bot.py tests/test_hedge_mode_guard.py
git commit -m "fix(bot): _handle_order_update 對非 LONG/SHORT 的 positionSide 早退

原本 LONG/SHORT 兩支都不命中時靜默走完整段：掛單計數不重置、
trade_side 的 else 分支把 BOTH 記成 short 餵進 bandit、還照樣
重掛網格。與 _handle_account_update（已有 BOTH 分支與未知值
warning）不對稱。

改為在 FILLED 分支開頭早退 + 節流 warning。節流只影響 log，
不影響早退行為。"
```

---

### Task 3: 全套回歸與 spec 對帳

**Files:**
- Modify: `tasks/progress.md`、`tasks/notes.md`（驗收紀錄）

**Interfaces:**
- Consumes: Task 1、Task 2 的 commit
- Produces: 可交付的驗收數據（測試計數、mutation 結果表）

- [ ] **Step 1: 乾淨快照跑全套**

```bash
PYTHONPATH=. /Users/ramonliao/Documents/理財/加密貨幣/量化交易/LouisLab/.venv/bin/python \
  -m pytest tests -q -p no:cacheprovider
```

Expected: `864 passed, 2 skipped`（基線 851 + 新增 13）。若實得數字不同，以實得為準並在回報中說明差異來源，**不得**改測試去湊數。

- [ ] **Step 2: 逐條對帳 spec 的「可判定驗收準則」**

對 `tasks/spec.md` 的 5 條準則逐條寫下「達成 / 未達成 + 證據」。準則 2 的 mutation 表要寫成：

| Mutation | 結果 | 紅在哪一行斷言 |
|---|---|---|

- [ ] **Step 3: 更新 `tasks/progress.md`**

把本任務移到 Current Task，明確分開記「已 commit」與「已重啟生效」（後者此時尚未達成）。

- [ ] **Step 4: Commit**

```bash
git add tasks/progress.md tasks/notes.md
git commit -m "docs: hedge 模式守衛任務的驗收數據與 mutation 結果"
```

---

## 完成後的必經關卡（不在 task 內，由主 session 執行）

1. `security-review` skill（Plan track + 命中 Red Team Protocol）
2. fresh-context `verifier`（read-back + 實跑，不吃實作者自述）
3. `dual-review` skill 兩輪 —— **未拿到 `Ship as-is` 不得標記完成**
4. verdict + 各輪 findings 計數落 `tasks/notes.md`
5. merge 後**必須重啟引擎**才生效；重啟證據見 spec 準則 5

## Spec 偏離紀錄（dev-rules：偏離不得沉默）

**M-D 的定義已修訂。** spec 原寫「M-D：`trade_side` 改回 `'long' if ps == 'LONG' else 'short'` ⇒ 必須紅」。加入 Task 2 的早退守衛後，`BOTH` 永遠到不了 `trade_side` 那行，該 mutation 與原碼**行為等價**，因此**不可能被殺死** —— 是一條無效 mutation。改為「M-D：把 `BOTH` 從守衛條件裡放行（`not in ('LONG','SHORT','BOTH')`）」，它守的是同一件事（BOTH 不得被記成 short），且可被殺死。已在 Task 2 Step 8 說明與 M-C 的鑑別差異。此修訂需同步回寫 `tasks/spec.md`。
