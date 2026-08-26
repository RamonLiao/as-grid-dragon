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
    bot.sync_service.maybe_sync = AsyncMock()
    bot.sync_service.sync_all = AsyncMock()

    await bot._handle_ticker({"s": "XRPUSDC", "b": "100.0", "a": "100.2"})

    bot.sync_service.maybe_sync.assert_not_called()
    bot.sync_service.sync_all.assert_not_called()


def test_handle_ticker_source_has_no_sync_call(bot):
    """原始碼層級守衛：日後有人「順手」把同步加回熱路徑會直接紅。"""
    src = inspect.getsource(MaxGridBot._handle_ticker)
    assert "maybe_sync" not in src
    assert "sync_all" not in src


def test_bot_run_creates_sync_task():
    """run() 的 task 清單必須含 sync_service.run——它是唯一驅動源，
    沒被建立等於 REST 同步完全消失（比改動前更糟，見 spec R1）。
    """
    src = inspect.getsource(MaxGridBot.run)
    assert "self.sync_service.run()" in src


def test_reporter_sync_source_is_wired(bot):
    """後置指派容易漏——漏了摘要那行永遠不出現，而且不會有人發現。"""
    assert bot.reporter.sync_source is bot.sync_service
