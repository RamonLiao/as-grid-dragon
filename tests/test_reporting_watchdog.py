"""DailyReporter 讀 watchdog 狀態的守衛。

硬性要求：取狀態失敗絕不能讓每日摘要發不出去——這裡守的正是「_get_watchdog_status
的 except 是否真的把例外吞掉降級成 None」，而不是靠 reporting.py 現有的
run() 外層 except（那個會 sleep(60) 重試，等於讓當天摘要延遲/丟失，是被明文
禁止的行為）。
"""
import asyncio

from grid_engine import clock
from grid_engine.reporting import DailyReporter


class FakeWatchdog:
    def __init__(self, state="healthy", last_event_at=0.0, attempts=0):
        self.state = state
        self.last_event_at = last_event_at
        self.attempts = attempts


class ExplodingWatchdog:
    """讀任何屬性都炸，模擬 watchdog 內部壞掉的最壞情況。"""
    @property
    def state(self):
        raise RuntimeError("boom")


def make_reporter(watchdog=None):
    return DailyReporter(config=None, state=None, notifier=None,
                          stop_event=asyncio.Event(), watchdog=watchdog)


def test_watchdog_none_returns_none():
    reporter = make_reporter(watchdog=None)
    assert reporter._get_watchdog_status() is None


def test_watchdog_healthy_status_extracted():
    holder = {"t": 1000.0}
    clock.set_clock(lambda: holder["t"])
    try:
        wd = FakeWatchdog(state="healthy", last_event_at=1000.0, attempts=0)
        reporter = make_reporter(watchdog=wd)
        status = reporter._get_watchdog_status()
        assert status == {"state": "healthy", "silence_seconds": 0.0, "attempts": 0}
    finally:
        clock.reset_clock()


def test_watchdog_given_up_status_extracted():
    holder = {"t": 1000.0 + 7200}
    clock.set_clock(lambda: holder["t"])
    try:
        wd = FakeWatchdog(state="given_up", last_event_at=1000.0, attempts=3)
        reporter = make_reporter(watchdog=wd)
        status = reporter._get_watchdog_status()
        assert status["state"] == "given_up"
        assert status["silence_seconds"] == 7200.0
        assert status["attempts"] == 3
    finally:
        clock.reset_clock()


def test_watchdog_exception_degrades_to_none_not_raises():
    """紅在：ExplodingWatchdog.state 拋 RuntimeError 時，若 _get_watchdog_status
    沒有自己的 try/except，這個呼叫本身就會拋出，取值失敗直接讓呼叫端整段
    摘要組裝失敗——這正是硬性要求禁止的行為。"""
    reporter = make_reporter(watchdog=ExplodingWatchdog())
    status = reporter._get_watchdog_status()  # 不得拋例外
    assert status is None


class TestDailyReporterEndToEndWiring:
    """0a 最後一格：reporting.run() → notifier 的接線是否真的把 given_up 那行
    帶進使用者收到的每日摘要。

    活體驗收只證到「2026-08-16 20:00 那封摘要有寄出、當下 watchdog 確實在
    given_up」；「⛔ 那行有沒有被組進訊息本體」靠人工看 Telegram 不可判定，
    這裡用實跑把它釘死（用真的 TelegramNotifier，只 mock 掉 send）。
    """

    @staticmethod
    async def _run_once(watchdog, notifier):
        import types
        from unittest.mock import AsyncMock, patch

        stop_event = asyncio.Event()
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
                                 stop_event=stop_event, watchdog=watchdog)

        calls = {"n": 0}

        async def fake_sleep(_seconds):
            calls["n"] += 1
            if calls["n"] >= 2:      # 第一輪送出摘要後才收工
                stop_event.set()

        with patch("grid_engine.reporting.asyncio.sleep", AsyncMock(side_effect=fake_sleep)):
            await asyncio.wait_for(reporter.run(), timeout=5)

    def test_given_up_reaches_the_telegram_message(self):
        """紅在：reporting.py 的 pnl_data 少帶 "watchdog" 這個 key（接線斷掉）
        —— 摘要照樣寄得出去，但 ⛔ 那行不見了，人工完全看不出來。"""
        from unittest.mock import AsyncMock

        from grid_engine.notifier import TelegramNotifier

        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        holder = {"t": 1000.0 + 7200}
        clock.set_clock(lambda: holder["t"])
        try:
            wd = FakeWatchdog(state="given_up", last_event_at=1000.0, attempts=3)
            asyncio.run(self._run_once(wd, notifier))
        finally:
            clock.reset_clock()

        notifier.send.assert_called_once()
        msg = notifier.send.call_args[0][0]
        assert "⛔" in msg
        assert "userData 監控：已放棄自動重連，需人工介入" in msg
        assert "3 次" in msg and "120 分鐘" in msg
        assert "94.49" in msg          # 既有欄位不得因 watchdog 行而掉

    def test_exploding_watchdog_still_sends_the_rest_of_the_summary(self):
        """watchdog 壞掉時摘要仍要寄出，只是不帶那行。"""
        from unittest.mock import AsyncMock

        from grid_engine.notifier import TelegramNotifier

        notifier = TelegramNotifier(bot_token="123:ABC", chat_id="456")
        notifier.send = AsyncMock(return_value=True)
        asyncio.run(self._run_once(ExplodingWatchdog(), notifier))

        notifier.send.assert_called_once()
        msg = notifier.send.call_args[0][0]
        assert "每日損益摘要" in msg
        assert "userData 監控" not in msg


class TestCollectPositionsGuards:
    """單一標的狀態壞掉不得讓當天摘要整封漏送。

    run() 的外層 `except Exception` 是 `sleep(60)` 後回迴圈頂端重算 target，
    而那時今天的整點已過 ⇒ target 直接 +1 天 ⇒ **當天摘要靜默漏送一整天**，
    不是延遲補送。所以組裝段必須自己吞掉例外（verifier 2026-08-24 findings #2）。
    """

    @staticmethod
    def _reporter(state):
        return DailyReporter(config=None, state=state, notifier=None,
                             stop_event=asyncio.Event(), watchdog=None)

    def test_broken_symbol_is_skipped_not_fatal(self):
        """紅在：_collect_positions 的 per-symbol try/except 被拿掉 ⇒
        HostileState 的屬性存取直接往外炸。"""
        import types

        class HostileState:
            @property
            def long_position(self):
                raise RuntimeError("boom")

        good = types.SimpleNamespace(long_position=0.5, short_position=0.0,
                                     unrealized_pnl=1.5)
        state = types.SimpleNamespace(symbols={"BAD/USDC:USDC": HostileState(),
                                               "BNB/USDC:USDC": good})
        positions = self._reporter(state)._collect_positions()
        assert "BAD/USDC:USDC" not in positions
        assert positions["BNB/USDC:USDC"]["long"] == 0.5

    def test_broken_symbols_container_degrades_to_empty(self):
        """紅在：拿掉 self.state.symbols.items() 外面那層 try ⇒ AttributeError。"""
        import types

        state = types.SimpleNamespace(symbols="not a dict")
        assert self._reporter(state)._collect_positions() == {}

    def test_missing_attribute_symbol_is_skipped(self):
        """屬性根本不存在（舊 state 物件）也只跳過該標的。"""
        import types

        state = types.SimpleNamespace(symbols={"X/USDC:USDC": types.SimpleNamespace()})
        assert self._reporter(state)._collect_positions() == {}
