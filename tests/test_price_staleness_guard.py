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

    門檻必須設得比「現在的假時鐘值」還大：若不這麼做，quote_at=0 時
    age = now - 0 一定 > 一個小門檻，會被 age > max_age 那支條件連帶擋下，
    測不出 quote_at <= 0 這支專屬檢查是否存在（mutation 殺不掉——這就是
    假紅/假綠的坑）。門檻從 clock.now() 動態導出，不寫死常數：若日後
    fake_clock fixture 的起始值改成真實 epoch（~1.7e9），這裡不會無聲失效。
    """
    threshold = clock.now() + 1_000_000.0
    bot.config.max_price_age_sec = threshold
    state = _prime_for_ordering(bot)
    state.latest_price = 100.0          # 有價格但沒有時戳
    state.best_bid = state.best_ask = 100.0
    assert state.quote_at == 0
    # 前提斷言：確保門檻真的隔離出唯一能擋下本案例的條件——
    # 若這個 assert 本身失敗，代表 fake_clock 起始值已經漲到讓上面的
    # +1_000_000 緩衝不夠用，測試本身要先修，不能悄悄退回假紅。
    assert clock.now() - 0 < threshold

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


@pytest.mark.asyncio
async def test_stale_quote_still_runs_risk_reduction(bot, fake_clock):
    """守衛的契約是「不要用不可信的價格下單」，不是「價格過期就全面停擺」。

    check_and_reduce_positions 下的是市價單（price 參數字面上是 0），完全不消費
    _grid_step 的 price/quote_at；把它關在 gate 後面，等於在 ticker 斷線、userData
    仍活、雙邊持倉往上爬——最需要風控的情境——關掉風控。gate 只該擋 place_order。
    """
    bot.config.max_price_age_sec = 5.0
    _prime_for_ordering(bot)
    await _seed_fresh_quote(bot)
    bot.risk_monitor.check_and_reduce_positions = AsyncMock()

    fake_clock(600.0)
    await bot.adjust_grid(SYMBOL)

    assert bot.order_executor.place_order.await_count == 0
    assert bot.risk_monitor.check_and_reduce_positions.await_count == 1


@pytest.mark.asyncio
async def test_stale_log_resumes_after_throttle_window(bot, fake_clock, caplog):
    """節流窗口過後，告警必須恢復——不是「第一次之後永遠靜音」。

    只釘「窗口內至多一次」測不出這個 regression：把節流條件改成 `if last == 0.0:`
    （只在從未 log 過時 log 一次，之後永遠不再 log）一樣能讓「至多一次」那半通過。
    這條測窗口過後的第二次 log 有沒有真的發生。
    """
    from grid_engine.bot import STALE_QUOTE_LOG_SECONDS
    import logging
    bot.config.max_price_age_sec = 5.0
    _prime_for_ordering(bot)
    await _seed_fresh_quote(bot)
    fake_clock(600.0)

    with caplog.at_level(logging.WARNING):
        await bot.adjust_grid(SYMBOL)
        fake_clock(STALE_QUOTE_LOG_SECONDS + 1)
        await bot.adjust_grid(SYMBOL)

    hits = [r for r in caplog.records if "價格快照過期" in r.getMessage()]
    assert len(hits) == 2


@pytest.mark.asyncio
async def test_stale_log_message_distinguishes_scenario(bot, fake_clock, caplog):
    """三種擋單情境的告警文案必須能一眼分辨，不能一律印 age（從未收過報價時
    age = now - 0 ≈ 1.7e9；時鐘後跳時 age 是負數，兩者對值班的人都無意義）。
    """
    import logging
    bot.config.max_price_age_sec = 5.0

    # 情境一：從未收過報價（quote_at == 0）
    state = _prime_for_ordering(bot)
    state.latest_price = 100.0
    with caplog.at_level(logging.WARNING):
        await bot.adjust_grid(SYMBOL)
    never_msg = caplog.records[-1].getMessage()
    assert "從未" in never_msg
    assert "1755" not in never_msg and "1787" not in never_msg and "1000000.0" not in never_msg

    caplog.clear()

    # 情境二：時鐘後跳（age < 0）
    _prime_for_ordering(bot)
    await _seed_fresh_quote(bot)
    fake_clock(-3600.0)
    with caplog.at_level(logging.WARNING):
        await bot.adjust_grid(SYMBOL)
    rewind_msg = caplog.records[-1].getMessage()
    assert "後跳" in rewind_msg or "倒退" in rewind_msg

    caplog.clear()
    fake_clock(3600.0)  # 校正回原本時間軸，避免污染後續狀態

    # 情境三：正常過期（age > max_age）
    _prime_for_ordering(bot)
    await _seed_fresh_quote(bot)
    fake_clock(600.0)
    with caplog.at_level(logging.WARNING):
        await bot.adjust_grid(SYMBOL)
    expired_msg = caplog.records[-1].getMessage()
    assert "600.0s" in expired_msg

    # 三種文案彼此不同
    assert len({never_msg, rewind_msg, expired_msg}) == 3


@pytest.mark.asyncio
async def test_decision_log_carries_quote_age(bot, fake_clock, tmp_path):
    """儀器：5 秒門檻是猜測值，要靠這個欄位的實測分佈日後收緊。"""
    import json as _json
    log_path = tmp_path / "decisions.jsonl"
    bot._decision_log_path = str(log_path)
    state = _prime_for_ordering(bot)
    state.long_position = 1.0            # 有倉 → 走到 decide() 與 _log_decision
    state.buy_long_orders = 0.0
    await _seed_fresh_quote(bot)

    fake_clock(2.0)
    await bot.adjust_grid(SYMBOL)

    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert lines, "決策未落檔"
    rec = _json.loads(lines[-1])
    assert rec["quote_age"] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_decision_log_quote_age_recorded_even_when_guard_disabled(bot, fake_clock, tmp_path):
    """max_price_age_sec = 0 只關『擋單』，不得連『量測』也一起關掉——
    否則正好在最想觀察門檻是否合理的時候（守衛已關）失去資料。
    """
    import json as _json
    bot.config.max_price_age_sec = 0
    log_path = tmp_path / "decisions.jsonl"
    bot._decision_log_path = str(log_path)
    state = _prime_for_ordering(bot)
    state.long_position = 1.0
    state.buy_long_orders = 0.0
    await _seed_fresh_quote(bot)

    fake_clock(999.0)   # 遠超預設 5 秒門檻，但守衛已關，不應擋單
    await bot.adjust_grid(SYMBOL)

    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert lines, "決策未落檔"
    rec = _json.loads(lines[-1])
    assert rec["quote_age"] == pytest.approx(999.0)


from grid_engine.notifier import TelegramNotifier
from grid_engine.reporting import DailyReporter


def test_stale_quote_line_omitted_when_zero():
    """計數為 0 不出這一行——正常狀態不加噪音。"""
    assert TelegramNotifier._format_stale_quote_line({"total": 0, "symbols": {}}) == ""
    assert TelegramNotifier._format_stale_quote_line(None) == ""
    assert TelegramNotifier._format_stale_quote_line("not a dict") == ""


def test_stale_quote_line_present_when_nonzero():
    line = TelegramNotifier._format_stale_quote_line(
        {"total": 42, "symbols": {"XRP/USDC:USDC": 42}})
    assert "價格快照過期" in line
    assert "42" in line
    assert line.endswith("\n")


def test_stale_quote_line_survives_garbage_counts():
    """型別錯不得讓整封摘要發不出去——降級成不帶數字，訊號本身不能掉。"""
    line = TelegramNotifier._format_stale_quote_line({"total": "abc", "symbols": None})
    assert "價格快照過期" in line      # 訊號本身不能掉
    assert line != ""


def test_reporter_collects_stale_counts():
    class _Src:
        stale_quote_counts = {"XRP/USDC:USDC": 3, "BNB/USDC:USDC": 4}

    import asyncio as _a
    r = DailyReporter(GlobalConfig(), None, None, _a.Event(), stale_quote_source=_Src())
    assert r._get_stale_quote_summary() == {
        "total": 7, "symbols": {"XRP/USDC:USDC": 3, "BNB/USDC:USDC": 4}}


def test_reporter_stale_counts_failure_is_swallowed():
    """取不到就不顯示那行，絕不能讓每日摘要發不出去（沿用 watchdog 那行的硬性要求）。"""
    class _Boom:
        @property
        def stale_quote_counts(self):
            raise RuntimeError("boom")

    import asyncio as _a
    r = DailyReporter(GlobalConfig(), None, None, _a.Event(), stale_quote_source=_Boom())
    assert r._get_stale_quote_summary() is None


class TestStaleQuoteReachesTelegram:
    """紅在：reporting.py 的 pnl_data 少帶 "stale_quotes" 這個 key（接線斷掉）。"""

    @staticmethod
    async def _run_once(stale_source, notifier):
        import asyncio as _a
        import types
        from unittest.mock import AsyncMock, patch

        stop_event = _a.Event()
        config = types.SimpleNamespace(telegram_daily_pnl_hour=20)
        sym_state = types.SimpleNamespace(long_position=0.5, short_position=0.0,
                                          unrealized_pnl=1.5)
        state = types.SimpleNamespace(
            symbols={"BNB/USDC:USDC": sym_state},
            start_time=None,
            total_unrealized_pnl=1.5,
            total_equity=94.49,
            margin_usage=0.193,
            total_profit=12.3,
        )
        reporter = DailyReporter(config=config, state=state, notifier=notifier,
                                 stop_event=stop_event,
                                 stale_quote_source=stale_source)

        calls = {"n": 0}

        async def fake_sleep(_seconds):
            calls["n"] += 1
            if calls["n"] >= 2:      # 第一輪送出摘要後才收工
                stop_event.set()

        with patch("grid_engine.reporting.asyncio.sleep",
                   AsyncMock(side_effect=fake_sleep)):
            await _a.wait_for(reporter.run(), timeout=5)

    def test_stale_count_reaches_the_telegram_message(self):
        import asyncio as _a
        from unittest.mock import AsyncMock

        class _Src:
            stale_quote_counts = {"BNB/USDC:USDC": 17}

        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        _a.run(self._run_once(_Src(), notifier))

        notifier.send.assert_called_once()
        msg = notifier.send.call_args[0][0]
        assert "價格快照過期" in msg
        assert "17" in msg
        assert "94.49" in msg          # 既有欄位不得因新增那行而掉

    def test_zero_stale_count_leaves_summary_clean(self):
        """0 次時整封摘要不得出現那一行——正常狀態不加噪音。"""
        import asyncio as _a
        from unittest.mock import AsyncMock

        class _Src:
            stale_quote_counts = {}

        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        _a.run(self._run_once(_Src(), notifier))

        msg = notifier.send.call_args[0][0]
        assert "每日損益摘要" in msg
        assert "價格快照過期" not in msg
