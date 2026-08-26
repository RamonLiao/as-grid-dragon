"""每日摘要的 REST 同步狀態行。

降級狀態若只靠即時告警，錯過那一封就再也看不到——摘要是它唯一的持續表面。
"""
import pytest

from grid_engine.notifier import TelegramNotifier
from grid_engine.reporting import DailyReporter


class _FakeSync:
    def __init__(self, degraded=False, failures=0, total=0):
        self._degraded = degraded
        self._consecutive_failures = failures
        self._degraded_total = total


def test_line_omitted_when_never_degraded():
    """正常且從未降級 ⇒ 整行省略，不加噪音。"""
    assert TelegramNotifier._format_sync_line(
        {"degraded": False, "consecutive_failures": 0, "degraded_total": 0}) == ""


def test_line_shows_degraded_now():
    line = TelegramNotifier._format_sync_line(
        {"degraded": True, "consecutive_failures": 7, "degraded_total": 2})
    assert "降級中" in line and "7" in line


def test_line_shows_recovered_history():
    """恢復了但今天出過事——這是摘要唯一能講、告警講不了的事。"""
    line = TelegramNotifier._format_sync_line(
        {"degraded": False, "consecutive_failures": 0, "degraded_total": 3})
    assert "正常" in line and "3" in line


@pytest.mark.parametrize("bad", [None, "x", 42, []])
def test_line_omitted_on_bad_input(bad):
    """型別錯不得讓整封摘要發不出去。"""
    assert TelegramNotifier._format_sync_line(bad) == ""


def test_reporter_reads_sync_status():
    reporter = DailyReporter(config=None, state=None, notifier=None, stop_event=None)
    reporter.sync_source = _FakeSync(degraded=True, failures=4, total=1)
    assert reporter._get_sync_status() == {
        "degraded": True, "consecutive_failures": 4, "degraded_total": 1}


def test_reporter_returns_none_without_source():
    reporter = DailyReporter(config=None, state=None, notifier=None, stop_event=None)
    assert reporter._get_sync_status() is None


def test_reporter_swallows_broken_source():
    """讀狀態失敗只能讓那一行消失，不得讓整封摘要發不出去
    （與 _get_watchdog_status / _get_stale_quote_summary 同一條硬性要求）。
    """
    class Boom:
        @property
        def _degraded(self):
            raise RuntimeError("boom")

    reporter = DailyReporter(config=None, state=None, notifier=None, stop_event=None)
    reporter.sync_source = Boom()
    assert reporter._get_sync_status() is None
