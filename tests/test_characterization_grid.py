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
    bot.order_executor.place_order = AsyncMock()
    bot.order_executor.cancel_orders_for_side = AsyncMock()
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

    bot.order_executor.cancel_orders_for_side.assert_awaited_once_with(SYMBOL, "long")
    # tp_price = 2.5*(1+0.004)=2.51, entry = 2.5*(1-0.006)=2.485
    calls = bot.order_executor.place_order.await_args_list
    assert calls[0] == call(SYMBOL, "sell", pytest.approx(2.51), 3.0, True, "long")
    assert calls[1] == call(SYMBOL, "buy", pytest.approx(2.485), 3.0, False, "long")


# ──────────────────────────── 多頭：裝死模式 ────────────────────────────

@pytest.mark.asyncio
async def test_dead_mode_entry_takes_over_the_side_so_grid_can_stop_adding():
    """進入裝死必須接管整側掛單：撤掉正常模式的殘留單（含開倉單，否則「停止補倉」
    只是不掛新單、舊單照樣成交），再掛出自己的特殊止盈。"""
    bot = _make_bot()
    sc = bot.config.symbols[SYMBOL]
    st = _state(bot, latest_price=2.5, long_position=70, short_position=0,
                sell_long_orders=0, long_dead_mode=False)
    await bot._place_grid(SYMBOL, sc, "long")
    bot.order_executor.cancel_orders_for_side.assert_awaited_once_with(SYMBOL, "long")
    assert st.long_dead_mode is True
    # 無對手倉 → fallback 1.05 → 2.625；tp_qty：long_pos(70) > limit(15) → 加倍 = 6
    bot.order_executor.place_order.assert_awaited_once_with(
        SYMBOL, "sell", pytest.approx(2.625), 6.0, True, "long")


@pytest.mark.asyncio
async def test_dead_mode_entry_replaces_stale_tp_instead_of_yielding_to_it():
    """進入裝死時帳上已有正常模式的止盈單：必須撤掉並改掛 dead_mode_price 那張。

    回歸守衛：舊實作的 `if pending_tp <= 0` 讓殘留單擋住特殊止盈，倉位永遠
    降不回 threshold 以下 → 該側永久停擺（生產實證 104.5h / 63619 筆決策，
    long 側 has_tp=0%、exit_dead=0）。
    """
    bot = _make_bot()
    sc = bot.config.symbols[SYMBOL]
    _state(bot, latest_price=2.5, long_position=70, short_position=0,
           sell_long_orders=1, long_dead_mode=False)
    await bot._place_grid(SYMBOL, sc, "long")
    bot.order_executor.cancel_orders_for_side.assert_awaited_once_with(SYMBOL, "long")
    bot.order_executor.place_order.assert_awaited_once_with(
        SYMBOL, "sell", pytest.approx(2.625), 6.0, True, "long")


@pytest.mark.asyncio
async def test_dead_mode_steady_state_does_not_rehang_tp_every_tick():
    """已在裝死中且止盈單還掛著：不重掛。

    `should_adjust` 在裝死下恆為 True（無開倉單），生產約每 6 秒一個 tick，
    每 tick 撤單重掛會直接餵給 order_executor 的下單斷路器。節流靠此分支。
    """
    bot = _make_bot()
    sc = bot.config.symbols[SYMBOL]
    _state(bot, latest_price=2.5, long_position=70, sell_long_orders=1, long_dead_mode=True)
    await bot._place_grid(SYMBOL, sc, "long")
    bot.order_executor.place_order.assert_not_awaited()
    bot.order_executor.cancel_orders_for_side.assert_not_awaited()


@pytest.mark.asyncio
async def test_dead_mode_price_with_opposite_position():
    """裝死有對手倉：r = (my/opp)/100 + 1。my=70,opp=35 → r=1.02 → 2.55。"""
    bot = _make_bot()
    sc = bot.config.symbols[SYMBOL]
    _state(bot, latest_price=2.5, long_position=70, short_position=35, sell_long_orders=0)
    await bot._place_grid(SYMBOL, sc, "long")
    price_arg = bot.order_executor.place_order.await_args_list[0].args[2]
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

    bot.order_executor.cancel_orders_for_side.assert_awaited_once_with(SYMBOL, "short")
    # tp_price = 2.5*(1-0.004)=2.49, entry = 2.5*(1+0.006)=2.515
    calls = bot.order_executor.place_order.await_args_list
    assert calls[0] == call(SYMBOL, "buy", pytest.approx(2.49), 3.0, True, "short")
    assert calls[1] == call(SYMBOL, "sell", pytest.approx(2.515), 3.0, False, "short")


# ──────────────────────────── 空頭鏡像：裝死模式 ────────────────────────────

@pytest.mark.asyncio
async def test_dead_mode_entry_takes_over_the_side_so_grid_can_stop_adding_short():
    """空頭鏡像：進入裝死接管整側掛單，再掛特殊止盈(buy)。"""
    bot = _make_bot()
    sc = bot.config.symbols[SYMBOL]
    st = _state(bot, latest_price=2.5, long_position=0, short_position=70,
                buy_short_orders=0, short_dead_mode=False)
    await bot._place_grid(SYMBOL, sc, "short")
    bot.order_executor.cancel_orders_for_side.assert_awaited_once_with(SYMBOL, "short")
    assert st.short_dead_mode is True
    # 無對手倉 → fallback 0.95 → 2.375；tp_qty：short_pos(70) > limit(15) → 加倍 = 6
    bot.order_executor.place_order.assert_awaited_once_with(
        SYMBOL, "buy", pytest.approx(2.375), 6.0, True, "short")


@pytest.mark.asyncio
async def test_dead_mode_with_pending_tp_does_nothing_short():
    """裝死空頭且已有 pending tp（buy_short_orders>0）：不下單。"""
    bot = _make_bot()
    sc = bot.config.symbols[SYMBOL]
    _state(bot, latest_price=2.5, long_position=0, short_position=70,
           buy_short_orders=1, short_dead_mode=True)
    await bot._place_grid(SYMBOL, sc, "short")
    bot.order_executor.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_dead_mode_price_with_opposite_position_short():
    """裝死空頭有對手倉：r = (my/opp)/100 + 1，價格 = base/r。
    my=70,opp=35 → r=1.02 → 2.5/1.02 ≈ 2.450980。"""
    bot = _make_bot()
    sc = bot.config.symbols[SYMBOL]
    _state(bot, latest_price=2.5, long_position=35, short_position=70, buy_short_orders=0)
    await bot._place_grid(SYMBOL, sc, "short")
    price_arg = bot.order_executor.place_order.await_args_list[0].args[2]
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
