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


def test_circuit_notify_task_tracked_then_self_removes():
    """斷路通知 task：排入 tasks（執行前防 GC + stop 可 cancel），完成後自移除——
    長跑 bot 每次斷路都 append 卻從不清，tasks 會無限累積（#8 修的洩漏）。"""
    from unittest.mock import AsyncMock

    from grid_engine.order_executor import ORDER_CIRCUIT_THRESHOLD

    ex = _make_executor()
    ex.notifier.send = AsyncMock()

    async def main():
        for _ in range(ORDER_CIRCUIT_THRESHOLD):
            ex._register_order_failure("X", Exception("boom"))
        assert len(ex.tasks) == 1               # 已入列（GC 安全）
        await asyncio.gather(*ex.tasks)         # task 跑完
        await asyncio.sleep(0)                  # done callback 在下個 tick 觸發
        assert ex.tasks == []                   # 自移除，不累積

    asyncio.run(main())
    ex.notifier.send.assert_awaited_once()


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
        locks=SymbolLocks(), notifier=notifier, risk_monitor=risk, tasks=[],
    )

    async def main():
        await svc._sync_account()
        await asyncio.sleep(0)   # 讓 fire-and-forget create_task 跑起來

    asyncio.run(main())
    risk.check_trailing_stop.assert_awaited_once()
    risk.check_risk_and_notify.assert_called_once()


def test_sync_risk_task_tracked_then_self_removes():
    """風控通知 fire-and-forget task 必須先入共享 tasks（原版無參照，GC 可能在
    執行前回收 task）、完成後自移除（每 10s sync 一次，永不清會累積）。"""
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

    shared_tasks = []
    svc = SyncService(
        gateway=RestGateway(), ctx=ctx, config=MagicMock(), state=GlobalState(),
        locks=SymbolLocks(), notifier=notifier, risk_monitor=risk, tasks=shared_tasks,
    )

    async def main():
        await svc._sync_account()
        assert len(shared_tasks) == 1           # 已入列（GC 安全）
        await asyncio.gather(*shared_tasks)     # task 跑完
        await asyncio.sleep(0)                  # done callback 在下個 tick 觸發
        assert shared_tasks == []               # 自移除，不累積

    asyncio.run(main())
    risk.check_risk_and_notify.assert_awaited_once()


# ──────────────────────────── MaxGridBot 組裝斷言 ────────────────────────────

def _make_bot():
    from grid_engine.bot import MaxGridBot
    from grid_engine.config import GlobalConfig
    return MaxGridBot(GlobalConfig())


def test_bot_wiring_shares_single_instances():
    """gateway/locks/ctx/stop_event 必須全組件同一實例——
    複製成兩份 = ccxt 並發打非 thread-safe Session / 原子區失效 / 停機失效"""
    bot = _make_bot()
    assert bot.order_executor.gateway is bot.gateway
    assert bot.sync_service.gateway is bot.gateway
    assert bot.ws_client.gateway is bot.gateway
    assert bot.order_executor.locks is bot.locks
    assert bot.sync_service.locks is bot.locks
    assert bot.order_executor.ctx is bot.ctx
    assert bot.sync_service.ctx is bot.ctx
    assert bot.ws_client.ctx is bot.ctx
    assert bot.order_executor._stop_event is bot._stop_event
    assert bot.ws_client._stop_event is bot._stop_event
    assert bot.reporter._stop_event is bot._stop_event
    assert bot.order_executor.tasks is bot.tasks
    assert bot.sync_service.tasks is bot.tasks
    assert bot.sync_service.risk_monitor is bot.risk_monitor
    assert bot.risk_monitor.order_executor is bot.order_executor


def test_bot_two_phase_init_propagates_to_components():
    """_init_exchange 後組件讀到真 exchange/funding_manager（防 None 快照，spec C1）"""
    from unittest.mock import MagicMock
    bot = _make_bot()
    assert bot.order_executor.ctx.exchange is None
    bot.exchange = MagicMock()            # 走 property → ctx
    bot.funding_manager = MagicMock()
    assert bot.order_executor.ctx.exchange is bot.exchange
    assert bot.sync_service.ctx.funding_manager is bot.funding_manager


def test_monkey_cross_component_lock_contention():
    """SyncService 原子區與 bot 網格鏈（adjust_grid skip-if-locked）搶同一把
    symbol lock：50 並發下鎖內臨界區不得交錯（#3 語意跨組件仍成立）"""
    locks = SymbolLocks()
    sym = "BNB/USDC:USDC"
    in_critical = []
    violations = []

    async def worker(i):
        lock = locks.get(sym)
        if i % 3 == 0 and lock.locked():
            return                      # 模擬 adjust_grid 的 skip-if-locked
        async with lock:
            in_critical.append(i)
            if len(in_critical) > 1:
                violations.append(tuple(in_critical))
            await asyncio.sleep(0)      # 讓出 event loop，製造交錯機會
            in_critical.remove(i)

    async def main():
        await asyncio.gather(*[worker(i) for i in range(50)])

    asyncio.run(main())
    assert violations == []
