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


@pytest.fixture
def fake_clock():
    """可推進的假守衛時鐘。注入 set_guard_clock 而非 set_clock：

    live bot 與 backtester 同行程，clock.now() 會被 backtester 換成歷史 epoch。
    同步節流量的是「本機牆鐘」，與情境時鐘是不同的物理量，混用是分類錯誤。
    """
    t = {"now": 1_000_000.0}
    clock.set_guard_clock(lambda: t["now"])

    def advance(seconds):
        t["now"] += seconds
    yield advance
    clock.reset_guard_clock()


@pytest.mark.asyncio
async def test_maybe_sync_throttle_uses_guard_clock(sync, fake_clock):
    """節流以守衛時鐘計時：推進時間就該再同步一次。"""
    first = await sync.maybe_sync()
    assert first is not None
    assert await sync.maybe_sync() is None          # 門檻未過

    fake_clock(sync.config.sync_interval + 1)
    assert await sync.maybe_sync() is not None      # 過門檻


def test_module_time_helper_still_exists():
    """_time 不得整個刪除：test_trade_stats_sync.py 正在 monkeypatch 它，
    那是 TRADE_STATS_INTERVAL 在用的計時來源。
    """
    from grid_engine import sync_service
    assert callable(sync_service._time)
