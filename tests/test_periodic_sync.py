"""週期性 REST 同步 task：sync_all 回報成敗、告警狀態機、常駐 loop。

驅動源從 _handle_ticker 移到常駐 task 後，「同步有沒有在跑」不再有 tick 當
不在場證明——失敗必須自己會說話，否則只是把靜默停擺換了個位置重演。
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

from grid_engine import clock
from grid_engine.bot import MaxGridBot
from grid_engine.config import GlobalConfig, SymbolConfig
from grid_engine.sync_service import SyncOutcome

SYMBOL = "XRP/USDC:USDC"


def _make_bot():
    """最小 bot fixture，沿用 tests/test_price_staleness_guard.py 的模式。"""
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


@pytest.fixture
def sync(bot):
    """把五個子項全換成成功的 no-op，測試各自再覆寫要失敗的那一個。"""
    s = bot.sync_service
    s._sync_positions = AsyncMock()
    s._sync_orders = AsyncMock()
    s._sync_account = AsyncMock()
    s._sync_funding_rates = AsyncMock()
    s._sync_trade_stats = AsyncMock()
    return s


@pytest.mark.asyncio
async def test_sync_all_reports_all_ok(sync):
    outcome = await sync.sync_all()
    assert isinstance(outcome, SyncOutcome)
    assert outcome.positions_ok and outcome.account_ok
    assert outcome.critical_ok
    assert not outcome.skipped


@pytest.mark.asyncio
async def test_sync_all_reports_skipped_when_lock_held(sync):
    """_sync_lock 已被持有 ⇒ early-return，回 skipped=True 且不參與判定。

    這個 early-return 是既有語意（tests/test_async_offload.py 三條並發測試在守），
    回傳值的加入不得改變它。
    """
    async with sync._sync_lock:
        outcome = await sync.sync_all()
    assert outcome.skipped is True
    assert outcome.critical_ok is True      # skipped 不算失敗
    sync._sync_positions.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_sync_returns_none_when_throttled(sync):
    """節流未過門檻回 None——不算成功也不算失敗。"""
    await sync.maybe_sync()                 # 第一次必過（last_sync_time=0）
    second = await sync.maybe_sync()         # 立刻再來一次，門檻未過
    assert second is None
