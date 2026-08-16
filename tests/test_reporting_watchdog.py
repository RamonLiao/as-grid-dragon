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
