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


# ──────────────────────────── OrderExecutor ────────────────────────────

def _make_executor():
    from unittest.mock import MagicMock
    from grid_engine.order_executor import OrderExecutor
    return OrderExecutor(
        gateway=MagicMock(), ctx=ExchangeContext(), state=MagicMock(),
        notifier=MagicMock(), config=MagicMock(), locks=SymbolLocks(),
        stop_event=asyncio.Event(), tasks=[],
    )


def test_is_blocked_matches_block_until():
    import time
    ex = _make_executor()
    assert ex.is_blocked("X") is False
    ex._order_block_until["X"] = time.time() + 60
    assert ex.is_blocked("X") is True
    ex._order_block_until["X"] = time.time() - 1
    assert ex.is_blocked("X") is False


# ──────────────────────────── SyncService × RiskMonitor 整合 ────────────────────────────

def test_sync_account_triggers_risk_and_trailing():
    """_sync_account 成功路徑必觸發 check_risk_and_notify(create_task) 與
    check_trailing_stop(await)——跨組件接線斷掉時全套 patch 遷移抓不到這條。"""
    from unittest.mock import AsyncMock, MagicMock

    from grid_engine.sync_service import SyncService
    from grid_engine.state import GlobalState

    risk = MagicMock()
    risk.check_risk_and_notify = AsyncMock()
    risk.check_trailing_stop = AsyncMock()
    notifier = MagicMock()
    notifier.enabled = True
    ctx = ExchangeContext()
    ctx.exchange = MagicMock()
    ctx.exchange.fetch_balance = MagicMock(return_value={"info": {"assets": []}, "total": {}, "free": {}})

    svc = SyncService(
        gateway=RestGateway(), ctx=ctx, config=MagicMock(), state=GlobalState(),
        locks=SymbolLocks(), notifier=notifier, risk_monitor=risk,
    )

    async def main():
        await svc._sync_account()
        await asyncio.sleep(0)   # 讓 fire-and-forget create_task 跑起來

    asyncio.run(main())
    risk.check_trailing_stop.assert_awaited_once()
    risk.check_risk_and_notify.assert_called_once()
