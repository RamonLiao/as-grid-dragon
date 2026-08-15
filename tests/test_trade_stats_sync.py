"""REST 成交統計測試。

單一 writer 是硬約束：userData handler 與 REST 同時寫 total_trades/total_profit
的話，userData 一旦復活數字就會翻倍。
"""
import asyncio
import pytest

from grid_engine import clock
from grid_engine.state import GlobalState, SymbolState
from grid_engine.sync_service import SyncService, TRADE_STATS_INTERVAL


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
    holder = {"t": 1_000_000.0}
    clock.set_clock(lambda: holder["t"])
    yield holder
    clock.reset_clock()


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


def test_userdata_handler_no_longer_writes_counters():
    """單一 writer 守衛：handler 原始碼不得再累加這兩個計數器。"""
    src = open("grid_engine/bot.py", encoding="utf-8").read()
    start = src.index("async def _handle_order_update")
    end = src.index("async def run(self)", start)
    body = src[start:end]
    assert "total_trades += 1" not in body
    assert "total_profit += realized_pnl" not in body
