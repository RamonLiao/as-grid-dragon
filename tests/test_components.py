"""葉子組件測試：ExchangeContext 兩階段 / SymbolLocks 鎖同一性 / RestGateway 單 worker 與停機"""
import asyncio
import threading

import pytest

from grid_engine.context import ExchangeContext
from grid_engine.locks import SymbolLocks
from grid_engine.rest_gateway import RestGateway


def test_exchange_context_starts_empty_and_mutable():
    ctx = ExchangeContext()
    assert ctx.exchange is None
    assert ctx.precisions == {}
    assert ctx.funding_manager is None
    ctx.exchange = object()
    ctx.precisions["BNB/USDC:USDC"] = {"price": 4}
    assert ctx.exchange is not None


def test_symbol_locks_identity():
    locks = SymbolLocks()
    a = locks.get("BNB/USDC:USDC")
    b = locks.get("BNB/USDC:USDC")
    c = locks.get("BTC/USDC:USDC")
    assert a is b          # 同 symbol 同一把鎖（不變式 2 的根基）
    assert a is not c
    assert isinstance(a, asyncio.Lock)


def test_rest_gateway_single_worker_serializes():
    gw = RestGateway()
    seen = []

    def record(i):
        seen.append(threading.current_thread().name)
        return i

    async def main():
        results = await asyncio.gather(*[gw.call(record, i) for i in range(5)])
        return results

    results = asyncio.run(main())
    assert results == [0, 1, 2, 3, 4]
    assert len(set(seen)) == 1                      # 全部同一 worker thread
    assert seen[0].startswith("ccxt-rest")
    gw.shutdown()


def test_rest_gateway_shutdown_rejects_new_calls():
    gw = RestGateway()
    gw.shutdown()

    async def main():
        with pytest.raises(RuntimeError):
            await gw.call(lambda: 1)

    asyncio.run(main())
