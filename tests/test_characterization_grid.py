"""Characterization tests：鎖死重構前 bot 網格決策行為。
搬移到 decision.py 後，這些斷言必須不改而綠。

覆蓋：_place_grid（正常模式/裝死模式，多空鏡像）、_should_adjust_grid。
"""
import pytest
from unittest.mock import AsyncMock, call

from grid_engine.bot import MaxGridBot
from grid_engine.config import GlobalConfig, SymbolConfig
from grid_engine.enhancements import MaxEnhancement

SYMBOL = "XRP/USDC:USDC"


def _make_bot(**enh_kwargs):
    cfg = GlobalConfig()
    cfg.symbols = {SYMBOL: SymbolConfig(
        symbol="XRPUSDC", ccxt_symbol=SYMBOL, enabled=True,
        take_profit_spacing=0.004, grid_spacing=0.006, initial_quantity=3,
        limit_multiplier=5.0, threshold_multiplier=20.0,
    )}
    cfg.max_enhancement = MaxEnhancement(**enh_kwargs)  # 預設全關 → manager 回中性值
    bot = MaxGridBot(cfg)
    bot.place_order = AsyncMock()
    bot.cancel_orders_for_side = AsyncMock()
    return bot


def _state(bot, **kw):
    st = bot.state.symbols[SYMBOL]
    for k, v in kw.items():
        setattr(st, k, v)
    return st


# ──────────────────────────── 多頭：正常模式 ────────────────────────────

@pytest.mark.asyncio
async def test_normal_mode_long_places_tp_and_entry():
    """正常模式（持倉 < threshold）：撤舊 + 止盈 + 補倉，價格用 GridStrategy 公式。"""
    bot = _make_bot()
    sc = bot.config.symbols[SYMBOL]
    _state(bot, latest_price=2.5, long_position=10, short_position=0,
           sell_long_orders=0)  # threshold = 3*20 = 60，10 < 60 → 正常
    await bot._place_grid(SYMBOL, sc, "long")

    bot.cancel_orders_for_side.assert_awaited_once_with(SYMBOL, "long")
    # tp_price = 2.5*(1+0.004)=2.51, entry = 2.5*(1-0.006)=2.485
    calls = bot.place_order.await_args_list
    assert calls[0] == call(SYMBOL, "sell", pytest.approx(2.51), 3.0, True, "long")
    assert calls[1] == call(SYMBOL, "buy", pytest.approx(2.485), 3.0, False, "long")


# ──────────────────────────── 多頭：裝死模式 ────────────────────────────

@pytest.mark.asyncio
async def test_dead_mode_enter_places_special_tp_no_cancel():
    """裝死（持倉 > threshold=60）且無 pending tp：只掛特殊止盈，不撤單，設 dead flag。"""
    bot = _make_bot()
    sc = bot.config.symbols[SYMBOL]
    st = _state(bot, latest_price=2.5, long_position=70, short_position=0,
                sell_long_orders=0, long_dead_mode=False)
    await bot._place_grid(SYMBOL, sc, "long")
    bot.cancel_orders_for_side.assert_not_awaited()
    assert st.long_dead_mode is True
    # 無對手倉 → fallback 1.05 → 2.625；tp_qty：long_pos(70) > limit(15) → 加倍 = 6
    bot.place_order.assert_awaited_once_with(
        SYMBOL, "sell", pytest.approx(2.625), 6.0, True, "long")


@pytest.mark.asyncio
async def test_dead_mode_with_pending_tp_does_nothing():
    """裝死且已有 pending tp（sell_long_orders>0）：不下單。"""
    bot = _make_bot()
    sc = bot.config.symbols[SYMBOL]
    _state(bot, latest_price=2.5, long_position=70, sell_long_orders=1, long_dead_mode=True)
    await bot._place_grid(SYMBOL, sc, "long")
    bot.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_dead_mode_price_with_opposite_position():
    """裝死有對手倉：r = (my/opp)/100 + 1。my=70,opp=35 → r=1.02 → 2.55。"""
    bot = _make_bot()
    sc = bot.config.symbols[SYMBOL]
    _state(bot, latest_price=2.5, long_position=70, short_position=35, sell_long_orders=0)
    await bot._place_grid(SYMBOL, sc, "long")
    price_arg = bot.place_order.await_args_list[0].args[2]
    assert price_arg == pytest.approx(2.55)


# ──────────────────────────── 空頭鏡像：正常模式 ────────────────────────────

@pytest.mark.asyncio
async def test_normal_mode_short_places_tp_and_entry():
    """正常模式空頭：撤舊 + cover(buy@tp) + 開空(sell@entry)。"""
    bot = _make_bot()
    sc = bot.config.symbols[SYMBOL]
    _state(bot, latest_price=2.5, long_position=0, short_position=10,
           buy_short_orders=0)  # threshold = 60，10 < 60 → 正常
    await bot._place_grid(SYMBOL, sc, "short")

    bot.cancel_orders_for_side.assert_awaited_once_with(SYMBOL, "short")
    # tp_price = 2.5*(1-0.004)=2.49, entry = 2.5*(1+0.006)=2.515
    calls = bot.place_order.await_args_list
    assert calls[0] == call(SYMBOL, "buy", pytest.approx(2.49), 3.0, True, "short")
    assert calls[1] == call(SYMBOL, "sell", pytest.approx(2.515), 3.0, False, "short")


# ──────────────────────────── 空頭鏡像：裝死模式 ────────────────────────────

@pytest.mark.asyncio
async def test_dead_mode_enter_places_special_tp_no_cancel_short():
    """裝死空頭且無 pending tp：只掛特殊止盈(buy)，不撤單，設 dead flag。"""
    bot = _make_bot()
    sc = bot.config.symbols[SYMBOL]
    st = _state(bot, latest_price=2.5, long_position=0, short_position=70,
                buy_short_orders=0, short_dead_mode=False)
    await bot._place_grid(SYMBOL, sc, "short")
    bot.cancel_orders_for_side.assert_not_awaited()
    assert st.short_dead_mode is True
    # 無對手倉 → fallback 0.95 → 2.375；tp_qty：short_pos(70) > limit(15) → 加倍 = 6
    bot.place_order.assert_awaited_once_with(
        SYMBOL, "buy", pytest.approx(2.375), 6.0, True, "short")


@pytest.mark.asyncio
async def test_dead_mode_with_pending_tp_does_nothing_short():
    """裝死空頭且已有 pending tp（buy_short_orders>0）：不下單。"""
    bot = _make_bot()
    sc = bot.config.symbols[SYMBOL]
    _state(bot, latest_price=2.5, long_position=0, short_position=70,
           buy_short_orders=1, short_dead_mode=True)
    await bot._place_grid(SYMBOL, sc, "short")
    bot.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_dead_mode_price_with_opposite_position_short():
    """裝死空頭有對手倉：r = (my/opp)/100 + 1，價格 = base/r。
    my=70,opp=35 → r=1.02 → 2.5/1.02 ≈ 2.450980。"""
    bot = _make_bot()
    sc = bot.config.symbols[SYMBOL]
    _state(bot, latest_price=2.5, long_position=35, short_position=70, buy_short_orders=0)
    await bot._place_grid(SYMBOL, sc, "short")
    price_arg = bot.place_order.await_args_list[0].args[2]
    assert price_arg == pytest.approx(2.5 / 1.02)


# ──────────────────────────── _should_adjust_grid：多頭 ────────────────────────────

def test_should_adjust_grid_no_orders_returns_true():
    bot = _make_bot()
    sc = bot.config.symbols[SYMBOL]
    _state(bot, latest_price=2.5, buy_long_orders=0, sell_long_orders=0,
           last_grid_price_long=2.5)
    assert bot._should_adjust_grid(sc, bot.state.symbols[SYMBOL], "long") is True


def test_should_adjust_grid_deviation_below_threshold_false():
    """有掛單且偏離 < grid_spacing*0.5(=0.003)：不重掛。"""
    bot = _make_bot()
    sc = bot.config.symbols[SYMBOL]
    _state(bot, latest_price=2.5, buy_long_orders=1, sell_long_orders=1,
           last_grid_price_long=2.5)  # deviation 0 < 0.003
    assert bot._should_adjust_grid(sc, bot.state.symbols[SYMBOL], "long") is False


def test_should_adjust_grid_deviation_above_threshold_true():
    bot = _make_bot()
    sc = bot.config.symbols[SYMBOL]
    _state(bot, latest_price=2.52, buy_long_orders=1, sell_long_orders=1,
           last_grid_price_long=2.5)  # deviation 0.008 >= 0.003
    assert bot._should_adjust_grid(sc, bot.state.symbols[SYMBOL], "long") is True


# ──────────────────────────── _should_adjust_grid：空頭鏡像 ────────────────────────────

def test_should_adjust_grid_no_orders_returns_true_short():
    bot = _make_bot()
    sc = bot.config.symbols[SYMBOL]
    _state(bot, latest_price=2.5, buy_short_orders=0, sell_short_orders=0,
           last_grid_price_short=2.5)
    assert bot._should_adjust_grid(sc, bot.state.symbols[SYMBOL], "short") is True


def test_should_adjust_grid_deviation_below_threshold_false_short():
    bot = _make_bot()
    sc = bot.config.symbols[SYMBOL]
    _state(bot, latest_price=2.5, buy_short_orders=1, sell_short_orders=1,
           last_grid_price_short=2.5)  # deviation 0 < 0.003
    assert bot._should_adjust_grid(sc, bot.state.symbols[SYMBOL], "short") is False


def test_should_adjust_grid_deviation_above_threshold_true_short():
    bot = _make_bot()
    sc = bot.config.symbols[SYMBOL]
    _state(bot, latest_price=2.52, buy_short_orders=1, sell_short_orders=1,
           last_grid_price_short=2.5)  # deviation 0.008 >= 0.003
    assert bot._should_adjust_grid(sc, bot.state.symbols[SYMBOL], "short") is True
