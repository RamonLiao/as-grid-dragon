"""單一 driver 接線：同步只由常駐 task 驅動，ticker handler 不再碰它。

留著 ticker 呼叫的版本裡，週期 task 幾乎永遠只是空跑節流檢查，沒有測試能
區分它有沒有真的在工作——這種「平常永遠不生效」的守衛最容易腐爛。
"""
import asyncio
import inspect
from unittest.mock import AsyncMock

import pytest

from grid_engine import clock
from grid_engine.bot import MaxGridBot
from grid_engine.config import GlobalConfig, SymbolConfig
from grid_engine.sync_service import SyncService

SYMBOL = "XRP/USDC:USDC"


def _make_bot():
    cfg = GlobalConfig()
    cfg.symbols = {SYMBOL: SymbolConfig(
        symbol="XRPUSDC", ccxt_symbol=SYMBOL, enabled=True,
        take_profit_spacing=0.003, grid_spacing=0.003, initial_quantity=0.02,
        limit_multiplier=5.0, threshold_multiplier=20.0,
    )}
    cfg.bandit.enabled = False
    bot = MaxGridBot(cfg)
    bot.order_executor.place_order = AsyncMock()
    bot.order_executor.cancel_orders_for_side = AsyncMock()
    return bot


@pytest.fixture
def bot():
    b = _make_bot()
    yield b
    clock.reset_clock()
    clock.reset_guard_clock()


@pytest.mark.asyncio
async def test_handle_ticker_does_not_drive_sync(bot):
    """釘死單一 driver：ticker 進來只更新報價與網格，不觸發 REST 同步。"""
    bot.adjust_grid = AsyncMock()
    bot.sync_service.sync_all = AsyncMock()

    await bot._handle_ticker({"s": "XRPUSDC", "b": "100.0", "a": "100.2"})

    bot.sync_service.sync_all.assert_not_called()


def test_handle_ticker_source_has_no_sync_call(bot):
    """cheap tripwire，**不是保證**：日後有人「順手」把同步加回熱路徑，
    只要那行字面上出現 `sync_all` 就會直接紅。

    它與 test_handle_ticker_does_not_drive_sync 不獨立——後者才是行為守衛。
    這條只多守一件事：有人把呼叫加回去但用 mock 讓行為測試仍綠的情況。
    反過來也很脆：把同步抽成 `self._drive_sync()` 這種 helper 再呼叫，字串
    比對就完全失效，而行為測試照樣抓得到。所以它只是便宜的第二道，不能當
    成單一 driver 的保證。
    """
    src = inspect.getsource(MaxGridBot._handle_ticker)
    assert "maybe_sync" not in src
    assert "sync_all" not in src


@pytest.mark.asyncio
async def test_bot_run_creates_sync_task(bot):
    """run() 真的把 sync_service.run() 的 task 放進 bot.tasks，且它還活著。

    這條守的是 spec 的最高風險 R1：移除 ticker driver 之後，這個 task 沒被
    建立 = REST 同步完全消失，比改動前更糟。原版是對 `inspect.getsource(run)`
    的字串比對——管得到「那行字還在不在」，管不到 runtime（把 create_task 換成
    一個永遠不會被跑到的分支、或把 run() 改成 `if False: create_task(...)`，
    字串比對照樣綠）。這裡改成斷言 runtime 事實：那個 coroutine 的 task 確實
    在 bot.tasks 裡，而且 not done()。

    取捨：完整跑 bot.run() 會連 WS、打 REST，測試環境不可接受。這裡把「真正
    做 I/O 的東西」全部換掉（gateway.call、acquire_listen_key、ws_client.run /
    keep_alive_loop、userdata_watchdog.run、五個子同步），**但不動接線本身**
    ——sync_service.run() 是真的那一個，tasks.extend 也是真的那一段。被 mock
    掉的都在待驗行為的上游，不會替接線背書。
    """
    bot.gateway.call = AsyncMock()
    bot.ws_client.acquire_listen_key = AsyncMock()
    bot.ws_client.run = AsyncMock()
    bot.ws_client.keep_alive_loop = AsyncMock()
    bot.userdata_watchdog.run = AsyncMock()
    for name in ("_sync_positions", "_sync_orders", "_sync_account",
                 "_sync_funding_rates", "_sync_trade_stats"):
        setattr(bot.sync_service, name, AsyncMock())
    bot.config.sync_interval = 999    # loop 只會停在第一個 sleep，不打任何東西

    run_task = asyncio.create_task(bot.run())
    try:
        await asyncio.sleep(0.05)     # 讓 run() 跑完初始化、建完 tasks
        sync_tasks = [
            t for t in bot.tasks
            if getattr(t.get_coro(), "cr_code", None) is SyncService.run.__code__
        ]
        assert len(sync_tasks) == 1, "sync_service.run() 的 task 沒被建立"
        assert not sync_tasks[0].done(), "常駐同步 task 已經結束了"
    finally:
        bot._stop_event.set()
        await asyncio.wait_for(run_task, timeout=5.0)


@pytest.mark.asyncio
async def test_bot_startup_sync_result_is_evaluated(bot):
    """啟動時那次 sync_all() 的回傳值必須進 _evaluate()（review M1）。

    丟掉回傳值的話，「開機當下 REST 就壞掉（key 被撤、IP 被擋）」這一輪完全
    不計數——那正是最該立刻知道的情境。
    """
    bot.gateway.call = AsyncMock()
    bot.ws_client.acquire_listen_key = AsyncMock()
    bot.ws_client.run = AsyncMock()
    bot.ws_client.keep_alive_loop = AsyncMock()
    bot.userdata_watchdog.run = AsyncMock()
    bot.config.sync_interval = 999
    bot.sync_service._sync_positions = AsyncMock(return_value=False)
    for name in ("_sync_orders", "_sync_account", "_sync_funding_rates", "_sync_trade_stats"):
        setattr(bot.sync_service, name, AsyncMock(return_value=True))

    run_task = asyncio.create_task(bot.run())
    try:
        await asyncio.sleep(0.05)
        assert bot.sync_service._consecutive_failures == 1
    finally:
        bot._stop_event.set()
        await asyncio.wait_for(run_task, timeout=5.0)


def test_reporter_sync_source_is_wired(bot):
    """後置指派容易漏——漏了摘要那行永遠不出現，而且不會有人發現。"""
    assert bot.reporter.sync_source is bot.sync_service


@pytest.mark.asyncio
async def test_sync_loop_stops_on_shared_stop_event(bot):
    """`bot._stop_event.set()` 必須停得下這條 loop（review M2）。

    SyncService 原本自造私有 `_stop_event`，與 ws_client / userdata_watchdog /
    reporter 不同構。當時沒出事只是因為 `bot.stop()` 還會 `task.cancel()`
    ——但那代表「共享停機訊號已 set，一條**會下單**的 loop（_sync_account →
    check_trailing_stop → close_symbol_positions 會送市價平倉單）仍在跑」。
    改成吃共享事件後，這條會在 timeout 上紅：私有事件版本收不到這個 set，
    loop 會一直睡下去直到 wait_for 逾時。
    """
    for name in ("_sync_positions", "_sync_orders", "_sync_account",
                 "_sync_funding_rates", "_sync_trade_stats"):
        setattr(bot.sync_service, name, AsyncMock())
    bot.config.sync_interval = 0.01

    assert bot.sync_service._stop_event is bot._stop_event   # 同一個實例，不是複製
    task = asyncio.create_task(bot.sync_service.run())
    await asyncio.sleep(0.05)
    bot._stop_event.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert task.done() and task.exception() is None
