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


def test_userdata_handler_no_longer_writes_counters():
    """單一 writer 守衛：handler 原始碼不得再累加這兩個計數器。"""
    src = open("grid_engine/bot.py", encoding="utf-8").read()
    start = src.index("async def _handle_order_update")
    end = src.index("async def run(self)", start)
    body = src[start:end]
    assert "total_trades += 1" not in body
    assert "total_profit += realized_pnl" not in body
