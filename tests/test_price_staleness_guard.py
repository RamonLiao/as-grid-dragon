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


async def _seed_fresh_quote(bot, price=100.0):
    """走真的 _handle_ticker 蓋章，不手動塞 quote_at——這樣測到的是真接線。

    播種期間 mock 掉 adjust_grid：_handle_ticker 尾端會呼叫它，不隔離的話播種
    本身就會下單並寫進 last_order_times，讓後續斷言被真實牆鐘的 10 秒冷卻污染
    （fake_clock 推不動 time.time()）。quote_at 在 adjust_grid 被 await 之前
    就蓋好，所以 mock 它不影響蓋章。
    """
    real_adjust = bot.adjust_grid
    bot.adjust_grid = AsyncMock()
    bot.sync_service.maybe_sync = AsyncMock()
    try:
        await bot._handle_ticker({"s": "XRPUSDC", "b": str(price), "a": str(price)})
    finally:
        bot.adjust_grid = real_adjust


def _prime_for_ordering(bot):
    """把狀態擺成「flat 側缺單」⇒ _should_adjust_grid 無條件 True ⇒ 會走到下單。

    一併清掉 last_order_times：冷卻用真實牆鐘計時，fake_clock 推不動，同一個
    測試內第二次呼叫 adjust_grid 會被它擋掉。冷卻是「會不會下單」這個狀態的一部分。
    """
    bot.last_order_times.clear()
    state = bot.state.symbols[SYMBOL]
    state.long_position = 0.0
    state.short_position = 0.0
    state.buy_long_orders = 0.0
    state.sell_long_orders = 0.0
    state.buy_short_orders = 0.0
    state.sell_short_orders = 0.0
    return state


@pytest.mark.asyncio
async def test_fresh_quote_places_orders(bot, fake_clock):
    """基準線：快照新鮮 → 正常下單。沒有這條，過期測試可能只是因為別的原因不下單。"""
    _prime_for_ordering(bot)
    await _seed_fresh_quote(bot)
    bot.order_executor.place_order.reset_mock()

    await bot.adjust_grid(SYMBOL)

    assert bot.order_executor.place_order.await_count > 0


@pytest.mark.asyncio
async def test_stale_quote_places_no_orders(bot, fake_clock):
    """快照超過 max_price_age_sec → 一張單都不許下。"""
    bot.config.max_price_age_sec = 5.0
    _prime_for_ordering(bot)
    await _seed_fresh_quote(bot)
    bot.order_executor.place_order.reset_mock()
    bot.order_executor.cancel_orders_for_side.reset_mock()

    fake_clock(5.1)
    await bot.adjust_grid(SYMBOL)

    assert bot.order_executor.place_order.await_count == 0
    # non-goal：過期不撤單（撤單同樣需要準確的價格認知）
    assert bot.order_executor.cancel_orders_for_side.await_count == 0


@pytest.mark.asyncio
async def test_order_update_path_with_stale_residual_is_blocked(bot, fake_clock):
    """本次真正要修的形態：_handle_order_update → adjust_grid 用上一次 ticker
    留下的殘值 best_bid/best_ask，中間隔多久完全不受控。
    """
    bot.config.max_price_age_sec = 5.0
    state = _prime_for_ordering(bot)
    await _seed_fresh_quote(bot)
    bot.order_executor.place_order.reset_mock()

    fake_clock(600.0)          # ticker 這 10 分鐘沒再來，但成交事件來了
    await bot._handle_order_update({
        "o": {"s": "XRPUSDC", "X": "FILLED", "S": "BUY", "ps": "LONG",
              "q": "0.02", "z": "0.02", "ap": "100.0", "rp": "0"},
    })

    assert bot.order_executor.place_order.await_count == 0
    assert state.best_bid == 100.0      # 守衛只讀不寫，殘值原樣保留


@pytest.mark.asyncio
async def test_never_received_quote_is_blocked(bot, fake_clock):
    """quote_at == 0（從未收過 ticker）不得被當成「年齡 = now」而放行。

    門檻刻意設得比 fake_clock 起始值（1_000_000）還大：若不這麼做，
    quote_at=0 時 age = now - 0 一定 > 預設門檻 5.0，會被 age > max_age
    那支條件連帶擋下，測不出 quote_at <= 0 這支專屬檢查是否存在
    （mutation 殺不掉——這就是假紅/假綠的坑）。
    """
    bot.config.max_price_age_sec = 2_000_000.0
    state = _prime_for_ordering(bot)
    state.latest_price = 100.0          # 有價格但沒有時戳
    state.best_bid = state.best_ask = 100.0
    assert state.quote_at == 0

    await bot.adjust_grid(SYMBOL)

    assert bot.order_executor.place_order.await_count == 0


@pytest.mark.asyncio
async def test_clock_rewind_blocks_then_self_heals(bot, fake_clock):
    """牆鐘往前跳 → age 為負 → 擋（安全側）；下一筆 ticker 重新蓋章即自癒。"""
    _prime_for_ordering(bot)
    await _seed_fresh_quote(bot)
    bot.order_executor.place_order.reset_mock()

    fake_clock(-3600.0)                 # 時鐘倒退一小時
    await bot.adjust_grid(SYMBOL)
    assert bot.order_executor.place_order.await_count == 0

    await _seed_fresh_quote(bot, price=100.0)   # 下一筆 ticker 重新蓋章
    _prime_for_ordering(bot)
    bot.order_executor.place_order.reset_mock()
    await bot.adjust_grid(SYMBOL)
    assert bot.order_executor.place_order.await_count > 0


@pytest.mark.asyncio
async def test_zero_threshold_disables_guard(bot, fake_clock):
    """max_price_age_sec = 0 → 行為完全回到改動前（生產緊急逃生門）。"""
    bot.config.max_price_age_sec = 0
    _prime_for_ordering(bot)
    await _seed_fresh_quote(bot)
    bot.order_executor.place_order.reset_mock()

    fake_clock(86400.0)                 # 一整天沒報價也照下
    await bot.adjust_grid(SYMBOL)

    assert bot.order_executor.place_order.await_count > 0


@pytest.mark.asyncio
async def test_threshold_change_takes_effect_immediately(bot, fake_clock):
    """TUI 的「設定即時套用」改門檻必須立刻生效——gate 不得快取 config 值。"""
    bot.config.max_price_age_sec = 5.0
    _prime_for_ordering(bot)
    await _seed_fresh_quote(bot)
    bot.order_executor.place_order.reset_mock()

    fake_clock(30.0)
    await bot.adjust_grid(SYMBOL)
    assert bot.order_executor.place_order.await_count == 0

    bot.config.max_price_age_sec = 60.0         # 熱改門檻
    _prime_for_ordering(bot)
    await bot.adjust_grid(SYMBOL)
    assert bot.order_executor.place_order.await_count > 0


@pytest.mark.asyncio
async def test_stale_events_are_counted_and_log_is_throttled(bot, fake_clock, caplog):
    """過期必須可觀測，但不得洗版：計數每次都加，log 每 3600 秒才一筆。"""
    import logging
    bot.config.max_price_age_sec = 5.0
    _prime_for_ordering(bot)
    await _seed_fresh_quote(bot)
    fake_clock(600.0)

    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            await bot.adjust_grid(SYMBOL)

    assert bot.stale_quote_counts[SYMBOL] == 5
    hits = [r for r in caplog.records if "價格快照過期" in r.getMessage()]
    assert len(hits) == 1
