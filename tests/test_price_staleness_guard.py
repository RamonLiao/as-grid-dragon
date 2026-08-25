"""價格時效守衛：快照帶抵達時戳，過期不下單。

真缺口不在 _handle_ticker（那裡的價格按定義新鮮），而在 adjust_grid 的第二個
呼叫端 _handle_order_update(bot.py:668)——它用上一次 ticker 留下的殘值
best_bid/best_ask，而 _grid_step(405/419) 把這兩個值直接餵給 place_order()。
"""
from unittest.mock import AsyncMock

import pytest

from grid_engine import clock
from grid_engine.bot import MaxGridBot
from grid_engine.config import GlobalConfig, SymbolConfig

SYMBOL = "XRP/USDC:USDC"


def _make_bot():
    """沿用 tests/test_bot_requote_wiring.py 的最小 bot fixture 模式。
    bandit 關閉：預設 enabled=True 會在 _grid_step 覆寫 grid_spacing。
    """
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
    clock.reset_clock()   # 絕不殘留 sim-clock 給後續測試


@pytest.fixture
def fake_clock():
    """可推進的假時鐘。回傳 advance(seconds)。"""
    t = {"now": 1_000_000.0}
    clock.set_clock(lambda: t["now"])

    def advance(seconds):
        t["now"] += seconds
    yield advance
    clock.reset_clock()


@pytest.mark.asyncio
async def test_handle_ticker_stamps_quote_at(bot, fake_clock):
    """ticker 進來時，quote_at 必須與 bid/ask 在同一次更新中被蓋章。"""
    bot.adjust_grid = AsyncMock()          # 隔離：本測試只驗蓋章
    bot.sync_service.maybe_sync = AsyncMock()
    state = bot.state.symbols[SYMBOL]
    assert state.quote_at == 0

    await bot._handle_ticker({"s": "XRPUSDC", "b": "100.0", "a": "100.2"})

    assert state.best_bid == 100.0
    assert state.best_ask == 100.2
    assert state.quote_at == clock.now()


from grid_engine.config import GlobalConfig as _GC


def test_max_price_age_default_is_five():
    assert _GC().max_price_age_sec == 5.0


@pytest.mark.parametrize("bad", ["abc", None, -1, float("nan"), float("inf"), object()])
def test_max_price_age_garbage_falls_back(bad):
    """垃圾值不得流進 runtime loop——非法一律 fallback 5.0（config from_dict 正規化）。"""
    cfg = _GC.from_dict({"max_price_age_sec": bad})
    assert cfg.max_price_age_sec == 5.0


def test_max_price_age_zero_is_legal_disable():
    """0 是合法的「關閉守衛」值，不得被 fallback 吃掉——它是生產緊急逃生門。"""
    cfg = _GC.from_dict({"max_price_age_sec": 0})
    assert cfg.max_price_age_sec == 0.0


def test_max_price_age_round_trips_through_to_dict():
    cfg = _GC()
    cfg.max_price_age_sec = 12.5
    assert _GC.from_dict(cfg.to_dict()).max_price_age_sec == 12.5
