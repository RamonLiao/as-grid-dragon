"""接線測試：訊號要真的從 order_executor / handler 流進 watchdog。

參照 tests/test_bot_requote_wiring.py 的作法——這類測試存在的理由是
「元件本身正確但沒被接上」這個失效模式，單測抓不到。
"""
import asyncio
import pytest

from grid_engine.order_executor import OrderExecutor


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


def test_handlers_call_record_event():
    """兩個 userData handler 都必須餵 watchdog，否則判準永遠不會復原。"""
    src = open("grid_engine/bot.py", encoding="utf-8").read()
    for name in ("_handle_account_update", "_handle_order_update"):
        start = src.index(f"async def {name}")
        end = src.index("\n    async def ", start + 10)
        assert "self.userdata_watchdog.record_event()" in src[start:end], name


def test_watchdog_run_is_scheduled():
    src = open("grid_engine/bot.py", encoding="utf-8").read()
    assert "self.userdata_watchdog.run()" in src
