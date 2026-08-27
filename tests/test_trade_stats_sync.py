"""REST 成交統計測試。

單一 writer 是硬約束：userData handler 與 REST 同時寫 total_trades/total_profit
的話，userData 一旦復活數字就會翻倍。
"""
import asyncio
import math
from pathlib import Path

import pytest

from grid_engine import clock
from grid_engine.state import GlobalState, SymbolState
from grid_engine.sync_service import (
    SyncService, TRADE_STATS_INTERVAL, TRADE_STATS_SINCE_MARGIN_MS,
    TRADE_STATS_MAX_PAGES_PER_SYNC,
)

BOT_PY = Path(__file__).resolve().parents[1] / "grid_engine" / "bot.py"


class FakeGateway:
    async def call(self, fn, *a, **kw):
        return fn(*a, **kw)


class FakeExchange:
    def __init__(self, pages):
        self.pages = pages          # list[list[dict]]，依序回傳
        self.calls = 0

    def fetch_my_trades(self, symbol=None, since=None, limit=None):
        page = self.pages[min(self.calls, len(self.pages) - 1)]
        self.calls += 1
        return page


class FakeCtx:
    def __init__(self, exchange):
        self.exchange = exchange
        self.funding_manager = None


class FakeSymCfg:
    def __init__(self, symbol="BNB/USDC:USDC"):
        self.enabled = True
        self.ccxt_symbol = symbol


class FakeConfig:
    def __init__(self):
        self.symbols = {"BNBUSDC": FakeSymCfg()}
        self.sync_interval = 10


def trade(tid, pnl, ts=1_700_000_000_000):
    return {"id": str(tid), "timestamp": ts, "info": {"realizedPnl": str(pnl)}}


def make_service(pages):
    ex = FakeExchange(pages)
    state = GlobalState()
    state.symbols["BNB/USDC:USDC"] = SymbolState(symbol="BNB/USDC:USDC")
    svc = SyncService(gateway=FakeGateway(), ctx=FakeCtx(ex), config=FakeConfig(),
                      state=state, locks=None, notifier=None, risk_monitor=None,
                      tasks=[], start_time_ms=1_699_000_000_000)
    return svc, state, ex


@pytest.fixture
def frozen_clock():
    """可推進的假**守衛**時鐘（`guard_now()`）。

    2026-08-26 dual-review B4 起改注入 set_guard_clock 而非 set_clock：
    `_sync_trade_stats` 的 60s 節流（比較 + finally 蓋章）量的是本機牆鐘，
    不是情境時鐘。`clock.now()` 會被 backtester 每根 K 線換成歷史 epoch，而
    live bot 與回測跑在同一個行程——節流若掛在 now() 上，回測期間差值是大負數
    ⇒ 每輪 early-return、成交統計靜默凍結；回測結束 reset_clock() 後時間戳卡在
    歷史 epoch ⇒ 節流永久失效 ⇒ 每 10s 打一次 fetch_my_trades。
    本檔既有斷言的語意完全沒變：holder["t"] 推進的仍然是「_sync_trade_stats
    節流看到的那個時間」，只是那個時間現在是 guard 時鐘。
    """
    holder = {"t": 1_000_000.0}
    clock.set_guard_clock(lambda: holder["t"])
    yield holder
    clock.reset_guard_clock()


def test_counts_and_sums(frozen_clock):
    svc, state, _ = make_service([[trade(1, "0.5"), trade(2, "-0.25")]])
    asyncio.run(svc._sync_trade_stats())
    st = state.symbols["BNB/USDC:USDC"]
    assert st.total_trades == 2
    assert st.total_profit == pytest.approx(0.25)
    assert state.total_trades == 2
    assert state.total_profit == pytest.approx(0.25)


def test_incremental_no_double_count(frozen_clock):
    """同一筆成交重複出現在後續回應中，不得被算第二次。"""
    svc, state, _ = make_service([
        [trade(1, "0.5"), trade(2, "-0.25")],
        [trade(1, "0.5"), trade(2, "-0.25"), trade(3, "1.0")],
    ])
    asyncio.run(svc._sync_trade_stats())
    frozen_clock["t"] += TRADE_STATS_INTERVAL
    asyncio.run(svc._sync_trade_stats())
    st = state.symbols["BNB/USDC:USDC"]
    assert st.total_trades == 3
    assert st.total_profit == pytest.approx(1.25)


def test_throttled(frozen_clock):
    svc, _, ex = make_service([[trade(1, "0.5")]])
    asyncio.run(svc._sync_trade_stats())
    asyncio.run(svc._sync_trade_stats())     # 節流內，不得再打 API
    assert ex.calls == 1


def test_failure_does_not_zero_counters(frozen_clock):
    """REST 失敗必須保留既有數值，不得當成 0 筆寫回去。"""
    svc, state, ex = make_service([[trade(1, "0.5")]])
    asyncio.run(svc._sync_trade_stats())
    assert state.symbols["BNB/USDC:USDC"].total_trades == 1

    def boom(**kw):
        raise RuntimeError("REST down")

    ex.fetch_my_trades = boom
    frozen_clock["t"] += TRADE_STATS_INTERVAL
    asyncio.run(svc._sync_trade_stats())
    assert state.symbols["BNB/USDC:USDC"].total_trades == 1
    assert state.symbols["BNB/USDC:USDC"].total_profit == pytest.approx(0.5)


class SinceFilterExchange:
    """比 FakeExchange 更真實：依 since 過濾（模擬真實交易所 API），
    用來驗證分頁同一毫秒邊界不漏抓。"""

    def __init__(self, all_trades, page_limit=1000):
        self.all_trades = all_trades   # 依 id 升冪排序
        self.page_limit = page_limit
        self.since_calls = []

    def fetch_my_trades(self, symbol=None, since=None, limit=None):
        self.since_calls.append(since)
        page = [t for t in self.all_trades if t["timestamp"] >= since]
        return page[:self.page_limit]


def test_pagination_same_millisecond_boundary_no_loss(frozen_clock):
    """整頁 1000 筆剛好都卡在同一毫秒，頁尾之後還有同毫秒的成交時不能漏抓。

    若分頁用 since = last_ts + 1 往後推，頁尾之後同毫秒的成交會被跳過
    （since 大於它們的 timestamp，篩不到）。這裡驗證改用 since = last_ts
    （inclusive）之後，配合 tid dedup，不會漏掉這筆。
    """
    boundary_ts = 1_700_000_000_999
    # 前 999 筆各自不同毫秒；第 1000 筆（頁尾，觸發滿頁分頁）與頁外的第 1001 筆
    # 共用同一毫秒 boundary_ts —— 這是 since=last_ts+1 會漏抓的邊界情境。
    all_trades = [trade(i, "0.01", ts=1_700_000_000_000 + i) for i in range(1, 1000)]
    all_trades.append(trade(1000, "0.01", ts=boundary_ts))
    all_trades.append(trade(1001, "0.01", ts=boundary_ts))
    ex = SinceFilterExchange(all_trades, page_limit=1000)

    state = GlobalState()
    state.symbols["BNB/USDC:USDC"] = SymbolState(symbol="BNB/USDC:USDC")
    svc = SyncService(gateway=FakeGateway(), ctx=FakeCtx(ex), config=FakeConfig(),
                      state=state, locks=None, notifier=None, risk_monitor=None,
                      tasks=[], start_time_ms=1_700_000_000_000)

    asyncio.run(svc._sync_trade_stats())

    st = state.symbols["BNB/USDC:USDC"]
    assert st.total_trades == 1001, "id=1001 落在頁尾之後、同毫秒，不能被 since+1 跳過"


class FlakyPagedExchange:
    """依 since 過濾 + 模擬分頁上限，並可指定在第幾次呼叫時炸一次（只炸一次）。
    用來重現「分頁中途失敗」造成重複計數的 bug（review Critical-2）。"""

    def __init__(self, all_trades, page_limit=1000, fail_at_call=None):
        self.all_trades = all_trades
        self.page_limit = page_limit
        self.fail_at_call = fail_at_call
        self.calls = 0
        self._failed_once = False

    def fetch_my_trades(self, symbol=None, since=None, limit=None):
        self.calls += 1
        if self.fail_at_call == self.calls and not self._failed_once:
            self._failed_once = True
            raise RuntimeError("REST down mid-pagination")
        page = [t for t in self.all_trades if t["timestamp"] >= since]
        return page[:self.page_limit]


def test_mid_pagination_failure_does_not_double_count(frozen_clock):
    """分頁中途失敗：第 1 頁成功、第 2 頁失敗，整輪必須整批丟棄，不能半套用。
    重試成功後，總數必須等於真值，不能把第 1 頁重複算一次。
    """
    # 1004 筆：第 1 頁吃滿 1000，第 2 頁剩 4 筆
    all_trades = [trade(i, "0.01", ts=1_700_000_000_000 + i) for i in range(1, 1005)]
    ex = FlakyPagedExchange(all_trades, page_limit=1000, fail_at_call=2)

    state = GlobalState()
    state.symbols["BNB/USDC:USDC"] = SymbolState(symbol="BNB/USDC:USDC")
    svc = SyncService(gateway=FakeGateway(), ctx=FakeCtx(ex), config=FakeConfig(),
                      state=state, locks=None, notifier=None, risk_monitor=None,
                      tasks=[], start_time_ms=1_700_000_000_000)

    asyncio.run(svc._sync_trade_stats())     # 第 2 頁炸掉，整輪應該整批丟棄
    st = state.symbols["BNB/USDC:USDC"]
    assert st.total_trades == 0, "分頁中途失敗不能半套用第 1 頁已算的部分"

    frozen_clock["t"] += TRADE_STATS_INTERVAL
    asyncio.run(svc._sync_trade_stats())     # 重試：這次全部成功
    assert st.total_trades == 1004, "重試成功後總數必須等於真值，不能把第 1 頁算兩次"
    assert st.total_profit == pytest.approx(10.04)


def test_full_page_same_millisecond_terminates_without_infinite_loop(frozen_clock):
    """整頁 1000 筆全部卡在同一毫秒（真正無法用 timestamp 分頁的情況）：
    必須有限步內終止（靠 since 推不動判斷），不能無限迴圈；且不漏套用已抓到的部分。
    """
    same_ts = 1_700_000_000_000
    all_trades = [trade(i, "0.01", ts=same_ts) for i in range(1, 1001)]
    ex = SinceFilterExchange(all_trades, page_limit=1000)

    state = GlobalState()
    state.symbols["BNB/USDC:USDC"] = SymbolState(symbol="BNB/USDC:USDC")
    svc = SyncService(gateway=FakeGateway(), ctx=FakeCtx(ex), config=FakeConfig(),
                      state=state, locks=None, notifier=None, risk_monitor=None,
                      tasks=[], start_time_ms=same_ts - 1)

    asyncio.run(svc._sync_trade_stats())     # 若卡在無限迴圈，這行會 hang，測試逾時失敗

    st = state.symbols["BNB/USDC:USDC"]
    assert st.total_trades == 1000, "已抓到的整頁仍要套用，不因終止而丟棄"
    # 第 1 次 fetch 拿到滿頁（since 從 start 推進到 same_ts），第 2 次 fetch 發現
    # since 推不動（still same_ts）才終止 —— 剛好 2 次呼叫，證明不是無限迴圈。
    assert len(ex.since_calls) == 2


def test_steady_state_after_exceeding_page_limit_does_not_freeze(frozen_clock):
    """成交數一旦破千觸發分頁，之後每輪的新成交必須持續被算到，不能被
    「同毫秒卡死」防線誤擋而永久凍結在破千那個數字上（review Critical-1）。
    """
    all_trades = [trade(i, "0.01", ts=1_700_000_000_000 + i) for i in range(1, 1201)]
    ex = SinceFilterExchange(all_trades, page_limit=1000)

    state = GlobalState()
    state.symbols["BNB/USDC:USDC"] = SymbolState(symbol="BNB/USDC:USDC")
    svc = SyncService(gateway=FakeGateway(), ctx=FakeCtx(ex), config=FakeConfig(),
                      state=state, locks=None, notifier=None, risk_monitor=None,
                      tasks=[], start_time_ms=1_700_000_000_000)

    asyncio.run(svc._sync_trade_stats())
    st = state.symbols["BNB/USDC:USDC"]
    assert st.total_trades == 1200

    frozen_clock["t"] += TRADE_STATS_INTERVAL
    for i in range(1201, 1206):
        all_trades.append(trade(i, "0.01", ts=1_700_000_000_000 + i))
    asyncio.run(svc._sync_trade_stats())
    assert st.total_trades == 1205, "破千後的新成交（+5）不能被凍結防線誤擋"

    frozen_clock["t"] += TRADE_STATS_INTERVAL
    for i in range(1206, 1216):
        all_trades.append(trade(i, "0.01", ts=1_700_000_000_000 + i))
    asyncio.run(svc._sync_trade_stats())
    assert st.total_trades == 1215, "連續多輪新成交（再 +10）持續正確累加"


def test_since_cursor_persists_across_rounds(frozen_clock):
    """游標必須跨輪推進，不能每輪重打 start_time_ms（review 修復輪 2 Important：
    這個機制本身之前完全沒測試守住，撤銷持久化全套照綠）。用下一輪實際打出去的
    since 參數直接驗證機制存在，不靠計數側面推論。
    """
    start = 1_700_000_000_000
    all_trades = [trade(1, "0.5", ts=start + 10_000)]   # 遠超過 margin(5s)，cursor 會明顯偏離 start
    ex = SinceFilterExchange(all_trades, page_limit=1000)

    state = GlobalState()
    state.symbols["BNB/USDC:USDC"] = SymbolState(symbol="BNB/USDC:USDC")
    svc = SyncService(gateway=FakeGateway(), ctx=FakeCtx(ex), config=FakeConfig(),
                      state=state, locks=None, notifier=None, risk_monitor=None,
                      tasks=[], start_time_ms=start)

    asyncio.run(svc._sync_trade_stats())
    frozen_clock["t"] += TRADE_STATS_INTERVAL
    asyncio.run(svc._sync_trade_stats())

    # 第 2 輪打出去的 since 必須是「上輪看到的最大 ts − margin」，不能還是 start_time_ms。
    assert ex.since_calls[-1] == start + 10_000 - TRADE_STATS_SINCE_MARGIN_MS


def test_margin_catches_late_arriving_trade_within_window(frozen_clock):
    """安全邊際存在的理由：REST 端偶有到達延遲——timestamp 落在邊際窗內、
    但直到下一輪才在回應中可見的成交，要靠邊際兜住；margin=0 會漏抓
    （review 修復輪 2 Important：這個機制之前也完全沒測試守住）。
    """
    start = 1_700_000_000_000
    all_trades = [trade(1, "0.01", ts=start + 50_000)]
    ex = SinceFilterExchange(all_trades, page_limit=1000)

    state = GlobalState()
    state.symbols["BNB/USDC:USDC"] = SymbolState(symbol="BNB/USDC:USDC")
    svc = SyncService(gateway=FakeGateway(), ctx=FakeCtx(ex), config=FakeConfig(),
                      state=state, locks=None, notifier=None, risk_monitor=None,
                      tasks=[], start_time_ms=start)

    asyncio.run(svc._sync_trade_stats())
    st = state.symbols["BNB/USDC:USDC"]
    assert st.total_trades == 1

    # id=2 的 timestamp 比上輪看到的最大 ts 早 2s（落在 5s 邊際窗內），但這一輪才
    # 第一次在 REST 回應中出現——模擬到達延遲。
    frozen_clock["t"] += TRADE_STATS_INTERVAL
    all_trades.append(trade(2, "0.02", ts=start + 48_000))
    asyncio.run(svc._sync_trade_stats())
    assert st.total_trades == 2, "落在邊際窗內、遲到才可見的成交必須被抓到"


def test_since_floor_does_not_regress(frozen_clock):
    """游標只能單調前進，候選值比既有 floor 小時不能把游標往回拉——否則會不必要
    地重掃更長歷史，把 I1 的效能訴求打回原形（review 修復輪 2 Important）。

    觸發情境：某輪回應中「頁尾最後一筆」(trades[-1]) 的 timestamp 不是這頁真正
    最大的 ts（list 順序與 ts 大小不同步的邊界情況），算出的候選值會小於既有 floor。
    """
    start = 1_700_000_000_000
    all_trades = [trade(1, "0.01", ts=start + 50_000)]
    ex = SinceFilterExchange(all_trades, page_limit=1000)

    state = GlobalState()
    state.symbols["BNB/USDC:USDC"] = SymbolState(symbol="BNB/USDC:USDC")
    svc = SyncService(gateway=FakeGateway(), ctx=FakeCtx(ex), config=FakeConfig(),
                      state=state, locks=None, notifier=None, risk_monitor=None,
                      tasks=[], start_time_ms=start)

    asyncio.run(svc._sync_trade_stats())   # cursor -> start + 45_000

    frozen_clock["t"] += TRADE_STATS_INTERVAL
    # id=2 的 ts 比 id=1 小，但在 list 中排在後面 => 這輪 trades[-1] 的 ts 比既有
    # floor 的推算基準還小，候選值 = (start+46_000) - 5_000 = start+41_000 < 既有 floor。
    all_trades.append(trade(2, "0.01", ts=start + 46_000))
    asyncio.run(svc._sync_trade_stats())

    frozen_clock["t"] += TRADE_STATS_INTERVAL
    asyncio.run(svc._sync_trade_stats())

    assert ex.since_calls[-1] == start + 45_000, "游標不能被較小的候選值往回拉"


def test_sync_all_actually_calls_sync_trade_stats(frozen_clock, monkeypatch):
    """verifier-fix finding 1：sync_all() 必須真的呼叫 _sync_trade_stats()，不能被
    靜默拔線。其餘四個 _sync_* 換成 no-op，只用 spy 包住 _sync_trade_stats 本尊
    （不是讀原始碼字串）——刪掉 sync_all() 裡那一行呼叫，這個測試必須紅在
    `assert called == [True]`。
    """
    svc, state, ex = make_service([[trade(1, "0.5")]])

    called = []
    orig = svc._sync_trade_stats

    async def spy():
        called.append(True)
        await orig()

    async def noop():
        return None

    monkeypatch.setattr(svc, "_sync_trade_stats", spy)
    monkeypatch.setattr(svc, "_sync_positions", noop)
    monkeypatch.setattr(svc, "_sync_orders", noop)
    monkeypatch.setattr(svc, "_sync_account", noop)
    monkeypatch.setattr(svc, "_sync_funding_rates", noop)

    asyncio.run(svc.sync_all())

    assert called == [True], "sync_all() 沒有真的呼叫 _sync_trade_stats()"
    st = state.symbols["BNB/USDC:USDC"]
    assert st.total_trades == 1, "spy 轉呼叫本尊後，統計要真的被寫入"


def test_default_start_time_ms_uses_now_not_epoch(monkeypatch):
    """verifier-fix finding 4：不傳 start_time_ms 時，口徑要是「本次啟動以來」
    （= 建構當下的 now），不能靜默退化成 epoch(0) 等於拉全部歷史。"""
    monkeypatch.setattr("grid_engine.sync_service._time", lambda: 1_700_000_000.0)
    ex = FakeExchange([[]])
    state = GlobalState()
    state.symbols["BNB/USDC:USDC"] = SymbolState(symbol="BNB/USDC:USDC")
    svc = SyncService(gateway=FakeGateway(), ctx=FakeCtx(ex), config=FakeConfig(),
                      state=state, locks=None, notifier=None, risk_monitor=None,
                      tasks=[])
    assert svc.start_time_ms == 1_700_000_000_000, \
        "預設值必須是建構當下的 now(ms)，不是 0(epoch)"


def test_malformed_realized_pnl_does_not_freeze_symbol(frozen_clock):
    """verifier-fix finding 3：單筆 realizedPnl 畸形（例如 'abc'）不得毒死整個
    symbol 那一輪——正常那幾筆要照樣計入、游標要推進、下一輪不能撞回同一筆卡死。
    """
    bad = trade(1, "0.5")
    bad["info"]["realizedPnl"] = "abc"   # float() 會拋 ValueError
    good1 = trade(2, "0.3")
    good2 = trade(3, "0.2")

    svc, state, ex = make_service([[bad, good1, good2]])
    asyncio.run(svc._sync_trade_stats())

    st = state.symbols["BNB/USDC:USDC"]
    assert st.total_trades == 2, "正常的兩筆必須被計入，不能整批被畸形那筆拖累丟棄"
    assert st.total_profit == pytest.approx(0.5)

    # 游標必須推進過畸形那筆的 id，下一輪重打同樣三筆(含壞的那筆)不能再撞到它、
    # 也不能重複計入好的兩筆。
    frozen_clock["t"] += TRADE_STATS_INTERVAL
    ex.pages.append([bad, good1, good2, trade(4, "1.0")])
    asyncio.run(svc._sync_trade_stats())
    assert st.total_trades == 3, "下一輪只有新增的 id=4 該被算，不能卡在畸形那筆重複掃描"
    assert st.total_profit == pytest.approx(1.5)


def test_malformed_info_none_does_not_freeze_symbol(frozen_clock):
    """verifier-fix finding 3：info=None（非缺欄位而是顯式 None）同樣要逐筆隔離，
    不得讓 .get() on None 的 AttributeError 毒死整個 symbol 那一輪。"""
    bad = trade(1, "0.5")
    bad["info"] = None
    good = trade(2, "0.3")

    svc, state, ex = make_service([[bad, good]])
    asyncio.run(svc._sync_trade_stats())

    st = state.symbols["BNB/USDC:USDC"]
    assert st.total_trades == 1, "info=None 那筆要被跳過，但正常那筆要照樣計入"
    assert st.total_profit == pytest.approx(0.3)


def test_malformed_last_timestamp_does_not_lose_page(frozen_clock):
    """verifier-fix finding 3：頁尾最後一筆 timestamp 畸形，仍要保留這一頁已經算好
    的筆數/盈虧（視同缺 timestamp 處理），不能讓整輪被拋出的例外整批丟棄。"""
    t1 = trade(1, "0.5")
    t2 = trade(2, "0.3")
    t2["timestamp"] = "not-a-number"

    svc, state, ex = make_service([[t1, t2]])
    asyncio.run(svc._sync_trade_stats())

    st = state.symbols["BNB/USDC:USDC"]
    assert st.total_trades == 2, "頁尾 timestamp 畸形不能讓已算好的整頁被丟棄"
    assert st.total_profit == pytest.approx(0.8)


@pytest.mark.parametrize("bad_pnl", ["nan", "inf", "-inf"])
def test_malformed_realized_pnl_nan_inf_does_not_poison_total(frozen_clock, bad_pnl):
    """verifier finding Important-1：realizedPnl 為 'nan'/'inf'/'-inf' 時 float() 不拋
    例外，逐筆 try/except 接不到；total_profit 用 += 累加、無重置點，一旦混入
    NaN/inf 會永久污染並印上 Telegram 日報。修法：用 math.isfinite() 擋掉，跳過此筆、
    其餘正常計入，total_profit 必須保持有限值。
    """
    bad = trade(1, bad_pnl)
    good = trade(2, "1.0")

    svc, state, ex = make_service([[bad, good]])
    asyncio.run(svc._sync_trade_stats())

    st = state.symbols["BNB/USDC:USDC"]
    assert st.total_trades == 1, "非有限值那筆必須被跳過，不計入 total_trades"
    assert math.isfinite(st.total_profit), \
        "total_profit 不得被 NaN/inf 污染（修復前這裡會是 nan/inf）"
    assert st.total_profit == pytest.approx(1.0), "正常那筆要照樣被計入"
    assert math.isfinite(state.total_profit), "全域彙總也不得被污染"

    # 游標必須推進過這筆的 id，且下一輪一筆正常成交要能救回來（不永久卡死）。
    frozen_clock["t"] += TRADE_STATS_INTERVAL
    ex.pages.append([bad, good, trade(3, "1.0")])
    asyncio.run(svc._sync_trade_stats())
    assert st.total_trades == 2, "下一輪只有新增的 id=3 該被算"
    assert st.total_profit == pytest.approx(2.0), \
        "修復前：一旦污染成 nan，這裡即使之後有正常筆 +1.0 也救不回來"


def test_malformed_trade_cursor_advances_only_once_per_bad_trade(frozen_clock, caplog):
    """verifier finding Minor-2：tid dedup 游標推進必須在欄位解析**之前**（程式碼中的
    註解宣稱如此），否則同一筆畸形資料會每輪重複被掃到、重複打 warning log（實測
    mutant：把推進順序移到解析之後，43 條測試仍全綠 ⇒ 是「會執行的註解」）。
    這裡直接鎖住「連續多輪下，同一筆畸形資料只產生一次 warning」這個可觀察行為。
    """
    # 只有一筆畸形資料、沒有更高 id 的正常筆同批出現：若混入更高 id 的正常筆，
    # 那筆本身就會把 page_max_id 推過畸形筆的 id，掩蓋掉「畸形筆本身有沒有推進
    # 游標」這件事——之前一版測試就是這樣被 mutant 騙過去的（43 條全綠）。
    bad = trade(1, "0.5")
    bad["info"]["realizedPnl"] = "abc"   # float() 拋 ValueError，走畸形分支

    svc, state, ex = make_service([[bad]])

    import logging
    caplog.set_level(logging.WARNING, logger="as_grid_max")

    asyncio.run(svc._sync_trade_stats())
    warn_count_round1 = sum(1 for r in caplog.records if "id=1" in r.message)
    assert warn_count_round1 == 1, "第一輪應該只對畸形那筆 warning 一次"

    # 下一輪同一頁重打（含同一筆壞資料）：若游標推進順序被移到解析之後，
    # tid=1 會因為畸形而 continue 在 page_max_id 推進之前，導致 page_max_id 沒被
    # 推進到 1，下一輪同一筆壞資料會再次通過 `tid <= page_max_id` 檢查、重新解析、
    # 重新記警告。
    frozen_clock["t"] += TRADE_STATS_INTERVAL
    ex.pages.append([bad])
    asyncio.run(svc._sync_trade_stats())

    total_warn_for_bad_id = sum(1 for r in caplog.records if "id=1" in r.message)
    assert total_warn_for_bad_id == 1, (
        "同一筆畸形資料在多輪下只該被記警告一次——游標必須先於欄位解析推進，"
        "否則每輪都會重新掃到同一筆壞資料、重複記警告（mutant 行為：warnings=[6] "
        "而非 [1]）"
    )


def test_narrow_time_monkeypatch_does_not_leak_to_global_time_module(monkeypatch):
    """verifier finding Minor-4：舊測試 monkeypatch
    `grid_engine.sync_service.time.time` 解析到的是全域 `time` 模組物件，等於把
    `time.time` 全程序換掉。這裡驗證改用 `from time import time as _time` 之後，
    monkeypatch `grid_engine.sync_service._time` 只影響 sync_service 模組命名空間
    裡的引用，不會外溢到真正的 stdlib `time.time`。
    """
    import time as real_time_module
    original = real_time_module.time

    monkeypatch.setattr("grid_engine.sync_service._time", lambda: 1_700_000_000.0)

    ex = FakeExchange([[]])
    state = GlobalState()
    state.symbols["BNB/USDC:USDC"] = SymbolState(symbol="BNB/USDC:USDC")
    svc = SyncService(gateway=FakeGateway(), ctx=FakeCtx(ex), config=FakeConfig(),
                      state=state, locks=None, notifier=None, risk_monitor=None,
                      tasks=[])
    assert svc.start_time_ms == 1_700_000_000_000

    assert real_time_module.time is original, (
        "monkeypatch 不得外溢到真正的 stdlib time.time"
        "（修復前：patch grid_engine.sync_service.time.time 等於整程序都被換掉）"
    )


def test_summary_aggregation_exception_does_not_propagate(frozen_clock):
    """security-fix Medium-1：結尾彙總 `sum(s.total_trades for s in ...)` 若拋例外，
    不得冒泡出 `_sync_trade_stats`（冒泡 = WS handler 例外 = 強制重連，見 finding
    說明）。這裡不是模擬——用一個讀取 total_trades 時刪除自己的 trap 物件，在
    sum() 真正迭代 dict.values() 期間觸發真正的
    `RuntimeError: dictionary changed size during iteration`。
    """
    class ExplodingSymbolState:
        def __init__(self, container, key):
            self.container = container
            self.key = key
            self.total_profit = 0.0

        @property
        def total_trades(self):
            del self.container[self.key]   # 迭代中途改變 dict 大小
            return 0

    svc, state, ex = make_service([[trade(1, "0.5")]])
    trap_key = "TRAP/USDC:USDC"
    state.symbols[trap_key] = ExplodingSymbolState(state.symbols, trap_key)

    # 修復前：RuntimeError 會從這裡冒出去，測試在這一行直接失敗（asyncio.run 重拋）。
    asyncio.run(svc._sync_trade_stats())

    assert trap_key not in state.symbols, "trap 的刪除自身副作用必須已發生，證明真的跑進了 sum()"
    # 個別 symbol 的正常同步不應被外層保險影響——BNB 那筆該算的還是要算到。
    assert state.symbols["BNB/USDC:USDC"].total_trades == 1


def test_page_cap_stops_and_resumes_across_rounds(frozen_clock, caplog):
    """security-fix Medium-2：分頁迴圈頁數必須有上限（跑在 WS recv 迴圈內，無上限
    會讓 ping/watchdog/recv 全部被卡住）。超限要停、記 warning，且下一輪要能從
    已推進的游標續拉，不漏不重。
    """
    import logging
    caplog.set_level(logging.WARNING, logger="as_grid_max")

    start = 1_700_000_000_000
    # ⚠️ 測資與下面的斷言都用寫死的字面值，不從 TRADE_STATS_MAX_PAGES_PER_SYNC 推導
    # （dual-review A3）：舊版 `1000 * (MAX + 3)` 是從被測常數本身推導測資 ⇒ 常數
    # 改錯它只會跟著放大、照樣綠（自洽測試）。
    total_ids = 13_000            # = 1000 × (10 頁上限 + 3)
    all_trades = [trade(i, "0.01", ts=start + i) for i in range(1, total_ids + 1)]
    ex = SinceFilterExchange(all_trades, page_limit=1000)

    state = GlobalState()
    state.symbols["BNB/USDC:USDC"] = SymbolState(symbol="BNB/USDC:USDC")
    svc = SyncService(gateway=FakeGateway(), ctx=FakeCtx(ex), config=FakeConfig(),
                      state=state, locks=None, notifier=None, risk_monitor=None,
                      tasks=[], start_time_ms=start)

    asyncio.run(svc._sync_trade_stats())
    st = state.symbols["BNB/USDC:USDC"]

    # 修復前：沒有頁數上限，第一輪就會一路撈到 total_ids，下面兩個斷言都會紅。
    assert len(ex.since_calls) == 10, (
        "單輪分頁次數必須被硬上限（10 頁，字面值）擋住，不能無限跑下去"
    )
    assert st.total_trades < total_ids, "頁數上限應該讓第一輪撈不完，留給下一輪續拉"
    assert any("達單輪上限" in r.message for r in caplog.records), "超限必須記 warning"

    # 之後多輪續拉：不漏（最終要等於 total_ids）、不重（不能超過 total_ids）。
    for _ in range(20):
        if st.total_trades >= total_ids:
            break
        frozen_clock["t"] += TRADE_STATS_INTERVAL
        asyncio.run(svc._sync_trade_stats())

    assert st.total_trades == total_ids, "多輪續拉後總數必須精確等於真值，不漏不重"


def test_malformed_trade_id_warns_once_per_round_not_per_trade(frozen_clock, caplog):
    """security-fix Low-3：trade id 解析失敗（`int(t.get('id'))` 拋例外）不得是靜默
    continue——與下方 realizedPnl/timestamp 的處理不對稱，會讓 total_trades 靜默
    少算且無任何線索。同一輪即使多筆 id 畸形，也只記一次 warning（節流），不逐筆洗版。
    """
    import logging
    caplog.set_level(logging.WARNING, logger="as_grid_max")

    bad1 = trade(1, "0.1")
    bad1["id"] = None
    bad2 = trade(2, "0.2")
    bad2["id"] = "not-a-number"
    good = trade(3, "0.3")

    svc, state, ex = make_service([[bad1, bad2, good]])
    asyncio.run(svc._sync_trade_stats())

    st = state.symbols["BNB/USDC:USDC"]
    assert st.total_trades == 1, "id 畸形的兩筆不該計入，正常那筆要照樣算"
    assert st.total_profit == pytest.approx(0.3)

    id_warnings = [r for r in caplog.records if "id 解析失敗" in r.message]
    # 修復前：這條路徑是靜默 continue，完全不記 log，這裡會紅在 len(...) == 0。
    assert len(id_warnings) == 1, (
        "同一輪內即使多筆 id 畸形，也只該記一次警告（節流），"
        f"實際記了 {len(id_warnings)} 次"
    )


def test_trade_stats_constants_are_pinned():
    """dual-review A3：這三個常數是規格值，不是任意數字。上面所有測試都從模組本身
    import 它們來驅動輸入/推進時間（自洽），改常數值全套照綠 —— 外部 reviewer 實跑
    的 mutation：TRADE_STATS_INTERVAL 60.0 → 5.0，102 條相關測試全綠。
    watchdog 那邊已有 test_watchdog_constants_are_pinned 的先例，同一份洞見這裡補上。

    - 60s：與 sync_interval(10s) 解耦省 API 權重（改小 = 靜默放大 API 權重）。
    - 10 頁：單輪分頁硬上限，跑在 WS recv 迴圈內（改大 = ping/watchdog/recv 被卡住）。
    - 5000ms：分頁游標回退的安全邊際（改小 = 到達延遲的成交被漏抓）。
    """
    assert TRADE_STATS_INTERVAL == 60.0
    assert TRADE_STATS_MAX_PAGES_PER_SYNC == 10
    assert TRADE_STATS_SINCE_MARGIN_MS == 5_000


class _TrapSymbolState:
    """讀取 total_trades 時刪掉自己 —— 在 sum() 真正迭代 dict.values() 期間觸發
    真正的 `RuntimeError: dictionary changed size during iteration`，用來讓
    `_sync_trade_stats_body()` 在最終彙總處拋例外（不是 mock 出來的假例外）。"""

    def __init__(self, container, key):
        self.container = container
        self.key = key
        self.total_profit = 0.0

    @property
    def total_trades(self):
        del self.container[self.key]
        return 0


def test_body_exception_does_not_disable_throttle(frozen_clock):
    """dual-review B1：`_last_trade_stats_at` 原本是 `_sync_trade_stats_body()` 的
    最後一行 ⇒ body 拋例外（正是外層保險要接的那種）時時間戳永不推進，之後每一次
    sync_all()（每 10s）都重打 fetch_my_trades —— 靜默變成 6 倍 API 權重，且每輪
    重做同一批 pending 計算。修法：時間戳移進 finally。

    mutation：把 `self._last_trade_stats_at = clock.now()` 移回 body 末尾
    ⇒ 紅在最後的 `assert ex.calls == 1`（實際會是 3）。
    """
    svc, state, ex = make_service([[trade(1, "0.5")]])
    trap_key = "TRAP/USDC:USDC"
    state.symbols[trap_key] = _TrapSymbolState(state.symbols, trap_key)

    asyncio.run(svc._sync_trade_stats())      # body 在最終彙總處拋例外，被外層保險接住
    assert ex.calls == 1
    assert trap_key not in state.symbols, "trap 必須真的被迭代到，證明 body 確實拋了例外"

    # 模擬時間只走 2 秒（遠小於 60s 節流窗）：sync_all() 每 10s 一次的真實節奏
    frozen_clock["t"] += 2.0
    asyncio.run(svc._sync_trade_stats())
    frozen_clock["t"] += 2.0
    asyncio.run(svc._sync_trade_stats())

    assert ex.calls == 1, (
        "body 拋例外後 60s 節流仍須生效；時間戳留在 body 最後一行會讓它永久失效，"
        f"每 10s 重打一次 fetch_my_trades（實際打了 {ex.calls} 次）"
    )


class BoomFundingManager:
    def update_funding_rate(self, symbol):
        raise RuntimeError("funding REST down")


def test_funding_rate_exception_does_not_propagate_out_of_sync_all(frozen_clock, caplog):
    """dual-review C1：`_sync_funding_rates` 原本完全沒有 try/except，而它在
    sync_all() 裡排在 `_sync_trade_stats` **之前** —— security-fix Medium-1 想擋的
    「例外冒泡 → 每 5 秒重連的永久迴圈」從這個兄弟方法仍然暢通
    （外層保險的註解宣稱兄弟方法都不拋例外，那句話當時是假的）。

    mutation：拿掉 `_sync_funding_rates` 的 try/except
    ⇒ RuntimeError 從 `asyncio.run(svc.sync_all())` 那行直接冒出來，測試紅在該行。
    """
    import logging
    svc, state, ex = make_service([[trade(1, "0.5")]])
    svc.ctx.funding_manager = BoomFundingManager()
    caplog.set_level(logging.ERROR, logger="as_grid_max")

    asyncio.run(svc.sync_all())

    assert any("funding rate 失敗" in r.message for r in caplog.records), \
        "失敗必須留 log，不得靜默吞掉"
    assert state.symbols["BNB/USDC:USDC"].total_trades == 1, \
        "funding 失敗不得連坐排在它後面的 _sync_trade_stats"


class OutOfOrderPageExchange:
    """滿頁回應，但頁尾那筆的 timestamp 比該頁最大 ts 小（list 順序與 ts 不同步）。
    用來逼出 `nxt < since` —— 只比 `nxt == since` 的舊判定擋不住的那一半。"""

    BASE = 1_700_000_000_000

    def __init__(self):
        self.since_calls = []

    def fetch_my_trades(self, symbol=None, since=None, limit=None):
        self.since_calls.append(since)
        page = [trade(i, "0.01", ts=self.BASE + 10_000 + i) for i in range(1, 1000)]
        page.append(trade(1000, "0.01", ts=self.BASE))   # 頁尾 ts 比 since 起點還小
        return page


def test_cursor_never_regresses_when_last_trade_is_not_page_max_ts(frozen_clock):
    """dual-review C4：卡死判定只比 `nxt == since`，`nxt < since` 會讓 since **倒退**
    （頁尾 `trades[-1]` 不是該頁最大 ts 時）。`==` 只守住「完全不動」這一個點，
    不是完整的單調性守衛。修法：改成 `nxt <= since`。

    mutation：把 `if nxt <= since` 改回 `if nxt == since`
    ⇒ 紅在 `assert ex.since_calls == [start]`（會變成 [start, BASE]，游標倒退了）。
    """
    ex = OutOfOrderPageExchange()
    start = OutOfOrderPageExchange.BASE + 5_000

    state = GlobalState()
    state.symbols["BNB/USDC:USDC"] = SymbolState(symbol="BNB/USDC:USDC")
    svc = SyncService(gateway=FakeGateway(), ctx=FakeCtx(ex), config=FakeConfig(),
                      state=state, locks=None, notifier=None, risk_monitor=None,
                      tasks=[], start_time_ms=start)

    asyncio.run(svc._sync_trade_stats())

    assert ex.since_calls == [start], \
        "頁尾 ts 小於 since 時游標不得倒退（倒退會讓下一頁重撈已掃過的區間）"
    assert state.symbols["BNB/USDC:USDC"].total_trades == 1000, \
        "已抓到的整頁仍要套用"


def test_userdata_handler_no_longer_writes_counters():
    """單一 writer 守衛（第二道防線：原始碼掃描）。

    ⚠️ 這條**不是**行為守衛：子字串掃描換個寫法就繞得過去（外部 reviewer 實跑的
    mutation：把 `+= 1` 寫成 `x = x + 1`，102 條相關測試全綠、雙寫全面復活）。
    真正的守衛是下面的 test_order_update_does_not_write_trade_counters。
    這條留著當便宜的第二道防線（掃到明顯的原樣復原），不當作唯一依據。
    """
    src = BOT_PY.read_text(encoding="utf-8")
    start = src.index("async def _handle_order_update")
    end = src.index("async def run(self)", start)
    body = src[start:end]
    assert "total_trades += 1" not in body
    assert "total_profit += realized_pnl" not in body


FILLED_EVENT = {
    "e": "ORDER_TRADE_UPDATE",
    "o": {"s": "BNBUSDC", "X": "FILLED", "S": "BUY", "ps": "LONG",
          "rp": "1.5", "p": "600.0", "q": "0.02"},
}


def make_bot():
    """建構真實 MaxGridBot（單一 symbol），並把會碰外部世界的兩處換成 no-op。

    參照 tests/test_userdata_watchdog_wiring.py 的
    test_account_update_alone_does_not_reset_watchdog 的作法。
    """
    from grid_engine.bot import MaxGridBot
    from grid_engine.config import GlobalConfig, SymbolConfig

    cfg = GlobalConfig()
    cfg.symbols = {"BNBUSDC": SymbolConfig(symbol="BNBUSDC",
                                           ccxt_symbol="BNB/USDC:USDC")}
    bot = MaxGridBot(cfg)

    async def _noop_adjust(symbol):
        return None

    bot.adjust_grid = _noop_adjust                 # 不下單、不碰交易所
    bot._maybe_persist_bandit_state = lambda: None  # 不寫檔
    return bot


def test_order_update_does_not_write_trade_counters():
    """單一 writer 的**行為**守衛（dual-review A1）。

    userData handler 與 REST 同時寫 total_trades/total_profit 的話，userData 一旦
    復活，使用者看到的成交數與已實現盈虧會直接翻倍。上面的原始碼掃描擋不住換寫法
    （外部 reviewer 的 mutation：
        sym_state.total_trades = sym_state.total_trades + 1
        self.state.total_trades = self.state.total_trades + 1
        sym_state.total_profit = sym_state.total_profit + realized_pnl
    加回 FILLED 分支 ⇒ 雙寫全面復活、102 條測試全綠）。

    這裡直接餵一筆真的 FILLED 事件進真的 bot，斷言四個計數器全部沒有被動到。
    mutation 下會紅在 `assert sym_state.total_trades == 0`（會是 1）。
    """
    bot = make_bot()
    sym_state = bot.state.symbols["BNB/USDC:USDC"]
    sym_state.buy_long_orders = 5.0     # 用來證明 FILLED 分支真的被走到

    asyncio.run(bot._handle_order_update(FILLED_EVENT))

    assert sym_state.buy_long_orders == 0, \
        "FILLED 分支沒被走到，這條測試就沒有守到任何東西"
    assert sym_state.total_trades == 0, \
        "handler 不得再寫 total_trades（單一 writer 是 sync_service）"
    assert sym_state.total_profit == 0, \
        "handler 不得再寫 total_profit（單一 writer 是 sync_service）"
    assert bot.state.total_trades == 0, "全域計數器同樣不得被 handler 寫"
    assert bot.state.total_profit == 0, "全域計數器同樣不得被 handler 寫"


def test_rest_sync_is_the_only_counter_writer_end_to_end():
    """雙寫翻倍的完整情境：同一筆成交先被 REST 同步算過，再走一次 userData handler
    （userData 復活時就是這個順序）。總數必須維持 1 筆，不能變 2。"""
    bot = make_bot()
    st = bot.state.symbols["BNB/USDC:USDC"]

    ex = FakeExchange([[trade(1, "1.5")]])
    bot.sync_service.ctx = FakeCtx(ex)
    bot.sync_service.gateway = FakeGateway()
    bot.sync_service.start_time_ms = 1_699_000_000_000

    holder = {"t": 1_000_000.0}
    clock.set_clock(lambda: holder["t"])
    try:
        asyncio.run(bot.sync_service._sync_trade_stats())
        assert st.total_trades == 1
        assert st.total_profit == pytest.approx(1.5)

        asyncio.run(bot._handle_order_update(FILLED_EVENT))

        assert st.total_trades == 1, "同一筆成交被 REST + userData 各算一次 = 翻倍"
        assert st.total_profit == pytest.approx(1.5), "已實現盈虧同樣不得翻倍"
    finally:
        clock.reset_clock()
