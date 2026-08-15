"""接線測試：訊號要真的從 order_executor / handler 流進 watchdog。

參照 tests/test_bot_requote_wiring.py 的作法——這類測試存在的理由是
「元件本身正確但沒被接上」這個失效模式，單測抓不到。
"""
import asyncio
from pathlib import Path

import pytest

from grid_engine.order_executor import OrderExecutor

BOT_PY = Path(__file__).resolve().parents[1] / "grid_engine" / "bot.py"


class SpyWatchdog:
    def __init__(self):
        self.orders = 0
        self.events = 0

    def record_order_action(self):
        self.orders += 1

    def record_event(self):
        self.events += 1


class FakeGateway:
    async def call(self, fn, *a, **kw):
        return fn(*a, **kw)


class FakeExchange:
    def create_order(self, *a, **kw):
        return {"id": "1"}

    def fetch_open_orders(self, symbol):
        return [{"id": "9", "side": "buy",
                 "info": {"positionSide": "LONG", "origQty": "0.02"},
                 "reduceOnly": False}]

    def cancel_order(self, oid, symbol):
        return {"id": oid}


class FakeCtx:
    exchange = FakeExchange()
    precisions = {"BNB/USDC:USDC": {"price": 2, "amount": 2, "min_amount": 0.01}}


class FakeLocks:
    def get(self, symbol):
        return asyncio.Lock()


def make_executor(wd):
    return OrderExecutor(
        gateway=FakeGateway(), ctx=FakeCtx(), state=None, notifier=None,
        config=None, locks=FakeLocks(), stop_event=asyncio.Event(),
        tasks=[], watchdog=wd)


def test_place_order_records_action():
    wd = SpyWatchdog()
    ex = make_executor(wd)
    asyncio.run(ex.place_order("BNB/USDC:USDC", "buy", 600.0, 0.02,
                               position_side="long"))
    assert wd.orders == 1


def test_cancel_records_action():
    wd = SpyWatchdog()
    ex = make_executor(wd)
    asyncio.run(ex.cancel_orders_for_side("BNB/USDC:USDC", "long"))
    assert wd.orders == 1


def test_executor_works_without_watchdog():
    """watchdog=None 時不得爆炸（回測/測試路徑不接 watchdog）。"""
    ex = make_executor(None)
    assert asyncio.run(ex.place_order("BNB/USDC:USDC", "buy", 600.0, 0.02,
                                      position_side="long")) is not None


def _handler_body(src: str, name: str) -> str:
    start = src.index(f"async def {name}")
    end = src.index("\n    async def ", start + 10)
    return src[start:end]


def test_order_handler_calls_record_event():
    """ORDER_TRADE_UPDATE 是 watchdog 唯一的復原訊號來源
    （order_executor.py 保證下/撤單必觸發它）。缺了 record_event() 就永遠不會恢復。

    ⚠️ 這條**不是**行為守衛：`in body` 只掃字面字串。外部 reviewer 實跑的 mutation
    `if data.get('__never__'): self.userdata_watchdog.record_event()` —— 字串還在、
    行為死掉（watchdog 從此永遠不會恢復、必然走到 given_up），102 條測試全綠。
    真正的守衛是下面的 test_order_update_resets_watchdog_behaviourally；
    這條留著當便宜的第二道防線（掃到整行被刪掉的情況）。
    """
    src = BOT_PY.read_text(encoding="utf-8")
    body = _handler_body(src, "_handle_order_update")
    assert "self.userdata_watchdog.record_event()" in body


def test_account_handler_does_not_call_record_event():
    """finding 1 的守衛：ACCOUNT_UPDATE（含每 8 小時一次的資金費事件）不得重置
    watchdog，否則會在 ORDER_TRADE_UPDATE 單邊死亡時無限續杯，讓硬上限形同虛設。"""
    src = BOT_PY.read_text(encoding="utf-8")
    body = _handler_body(src, "_handle_account_update")
    assert "self.userdata_watchdog.record_event()" not in body


def test_watchdog_run_is_scheduled():
    src = BOT_PY.read_text(encoding="utf-8")
    assert "self.userdata_watchdog.run()" in src


def test_order_update_resets_watchdog_behaviourally():
    """dual-review A2：ORDER_TRADE_UPDATE 必須**真的**把 watchdog 從 degraded
    拉回 healthy。這是正方向的守衛（test_account_update_alone_does_not_reset_watchdog
    只守反方向）。

    mutation（外部 reviewer 實跑）：把 handler 裡那行改成
        if data.get('__never__'): self.userdata_watchdog.record_event()
    字面字串還在、行為死掉 ⇒ watchdog 永遠不會恢復、必然燒完三次重連走到
    given_up 並發出「需人工介入」的 ⛔ 告警。
    這條測試在該 mutation 下紅在 `assert wd.state == "healthy"`（會停在 "degraded"）。
    """
    from grid_engine import clock
    from grid_engine.bot import MaxGridBot
    from grid_engine.config import GlobalConfig, SymbolConfig
    from grid_engine.userdata_watchdog import (
        DEFAULT_ORDER_THRESHOLD, DEFAULT_SILENCE_SECONDS,
    )

    holder = {"t": 1_000_000.0}
    clock.set_clock(lambda: holder["t"])
    try:
        cfg = GlobalConfig()
        cfg.symbols = {"BNBUSDC": SymbolConfig(symbol="BNBUSDC",
                                               ccxt_symbol="BNB/USDC:USDC")}
        bot = MaxGridBot(cfg)

        async def _noop_adjust(symbol):
            return None

        bot.adjust_grid = _noop_adjust
        bot._maybe_persist_bandit_state = lambda: None

        wd = bot.userdata_watchdog
        for _ in range(DEFAULT_ORDER_THRESHOLD):
            wd.record_order_action()
        holder["t"] += DEFAULT_SILENCE_SECONDS + 1
        wd.check()
        assert wd.state == "degraded"
        assert wd.attempts == 1

        sym_state = bot.state.symbols["BNB/USDC:USDC"]
        sym_state.buy_long_orders = 5.0
        asyncio.run(bot._handle_order_update({
            "e": "ORDER_TRADE_UPDATE",
            "o": {"s": "BNBUSDC", "X": "FILLED", "S": "BUY", "ps": "LONG",
                  "rp": "1.5", "p": "600.0", "q": "0.02"},
        }))

        assert sym_state.buy_long_orders == 0, \
            "FILLED 分支沒被走到，這條測試就沒有守到任何東西"
        assert wd.state == "healthy", \
            "ORDER_TRADE_UPDATE 必須真的把 watchdog 拉回 healthy（唯一的復原入口）"
        assert wd.attempts == 0, "復原時 attempts 必須歸零，退避重新起算"
        assert wd.orders_since_event == 0
        assert wd.last_event_at == holder["t"], "靜默計時必須被推進到事件當下"
    finally:
        clock.reset_clock()


def test_order_update_resets_watchdog_even_on_unknown_symbol():
    """record_event() 必須排在 handler 最前面：事件屬於未配置的 symbol 時
    handler 會 early-return，但「stream 活著」這個事實與 symbol 無關，
    仍然必須算成復原訊號（否則只交易 A、收到 B 的事件時 watchdog 照樣判死）。"""
    from grid_engine import clock
    from grid_engine.bot import MaxGridBot
    from grid_engine.config import GlobalConfig, SymbolConfig
    from grid_engine.userdata_watchdog import (
        DEFAULT_ORDER_THRESHOLD, DEFAULT_SILENCE_SECONDS,
    )

    holder = {"t": 1_000_000.0}
    clock.set_clock(lambda: holder["t"])
    try:
        cfg = GlobalConfig()
        cfg.symbols = {"BNBUSDC": SymbolConfig(symbol="BNBUSDC",
                                               ccxt_symbol="BNB/USDC:USDC")}
        bot = MaxGridBot(cfg)
        wd = bot.userdata_watchdog
        for _ in range(DEFAULT_ORDER_THRESHOLD):
            wd.record_order_action()
        holder["t"] += DEFAULT_SILENCE_SECONDS + 1
        wd.check()
        assert wd.state == "degraded"

        asyncio.run(bot._handle_order_update({
            "e": "ORDER_TRADE_UPDATE",
            "o": {"s": "NOTCONFIGURED", "X": "FILLED", "S": "BUY", "ps": "LONG"},
        }))
        assert wd.state == "healthy"
    finally:
        clock.reset_clock()


def test_account_update_alone_does_not_reset_watchdog():
    """finding 1 行為守衛（非原始碼掃描）：只收到 ACCOUNT_UPDATE、完全沒有
    ORDER_TRADE_UPDATE 的情境下，watchdog 不得被重置。資金費結算
    （m="FUNDING_FEE"，每 8 小時一次）走的正是 ACCOUNT_UPDATE，若它也會重置，
    會在 ORDER_TRADE_UPDATE 單邊死亡時把狀態機無限拉回 healthy，
    讓「3 次強制重連後放棄」的硬上限形同虛設。"""
    from grid_engine import clock
    from grid_engine.bot import MaxGridBot
    from grid_engine.config import GlobalConfig
    from grid_engine.userdata_watchdog import (
        DEFAULT_ORDER_THRESHOLD, DEFAULT_SILENCE_SECONDS,
    )

    holder = {"t": 1_000_000.0}
    clock.set_clock(lambda: holder["t"])
    try:
        bot = MaxGridBot(GlobalConfig())
        wd = bot.userdata_watchdog

        for _ in range(DEFAULT_ORDER_THRESHOLD):
            wd.record_order_action()
        holder["t"] += DEFAULT_SILENCE_SECONDS + 1
        wd.check()
        assert wd.state == "degraded"
        assert wd.attempts == 1

        # 強制重連時證據已被重取（dual-review B2），所以先補一張新單，
        # 才能檢查 ACCOUNT_UPDATE 有沒有把「新累積的證據」清掉。
        wd.record_order_action()
        asyncio.run(bot._handle_account_update({"a": {"B": [], "P": []}}))

        assert wd.state == "degraded", "ACCOUNT_UPDATE 不得把 watchdog 拉回 healthy"
        assert wd.attempts == 1, "ACCOUNT_UPDATE 不得歸零 attempts"
        assert wd.orders_since_event == 1, "ACCOUNT_UPDATE 不得清掉累積中的張數證據"
    finally:
        clock.reset_clock()
