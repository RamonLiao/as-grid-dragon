"""UserDataWatchdog 狀態機測試。

判準是「orders_since_event >= K 且 now - last_event_at >= N」——兩者同時成立。
只看時間會在真正安靜的時段誤報（引擎裝死、價格不動不 requote，實盤成交率曾低到
~1 筆/天）；只看張數則沒給推送延遲留餘裕。下面每條測試都在守衛其中一半。
"""
import asyncio
import pytest

from grid_engine import clock
from grid_engine.userdata_watchdog import (
    UserDataWatchdog, BACKOFF_SECONDS, DEFAULT_ORDER_THRESHOLD,
    DEFAULT_SILENCE_SECONDS, CHECK_INTERVAL,
)


class FakeWs:
    def __init__(self):
        self.reconnects = 0

    def request_reconnect(self):
        self.reconnects += 1


class FakeNotifier:
    enabled = True

    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)
        return True


@pytest.fixture
def frozen_clock():
    holder = {"t": 1_000_000.0}
    clock.set_clock(lambda: holder["t"])
    yield holder
    clock.reset_clock()


def make_wd(**kw):
    ws, notifier = FakeWs(), FakeNotifier()
    wd = UserDataWatchdog(ws_client=ws, notifier=notifier, tasks=[],
                          stop_event=asyncio.Event(), **kw)
    return wd, ws, notifier


def test_starts_healthy(frozen_clock):
    wd, ws, _ = make_wd()
    assert wd.state == "healthy"
    wd.check()
    assert ws.reconnects == 0


def test_quiet_period_does_not_trigger(frozen_clock):
    """安靜時段：時間到了但沒下單 -> 不得判死。守衛判準的 `and`。"""
    wd, ws, notifier = make_wd()
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS + 1
    wd.check()
    assert wd.state == "healthy"
    assert ws.reconnects == 0
    assert notifier.sent == []


def test_orders_without_elapsed_time_does_not_trigger(frozen_clock):
    """下了單但時間還沒到 -> 不得判死（留推送延遲餘裕）。"""
    wd, ws, _ = make_wd()
    for _ in range(DEFAULT_ORDER_THRESHOLD):
        wd.record_order_action()
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS - 1
    wd.check()
    assert wd.state == "healthy"
    assert ws.reconnects == 0


def test_below_order_threshold_does_not_trigger(frozen_clock):
    """張數不足門檻 -> 不得判死。守衛 K。"""
    wd, ws, _ = make_wd()
    for _ in range(DEFAULT_ORDER_THRESHOLD - 1):
        wd.record_order_action()
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS + 1
    wd.check()
    assert wd.state == "healthy"
    assert ws.reconnects == 0


def test_detects_and_reconnects_once(frozen_clock):
    wd, ws, notifier = make_wd()
    for _ in range(DEFAULT_ORDER_THRESHOLD):
        wd.record_order_action()
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS + 1
    wd.check()
    assert wd.state == "degraded"
    assert ws.reconnects == 1
    assert wd.attempts == 1
    assert len(notifier.sent) == 1
    # 立刻再 check 不得重複重連（退避未到）
    wd.check()
    assert ws.reconnects == 1


def test_backoff_sequence_then_give_up(frozen_clock):
    """退避必須是 300/900/2700，第 4 次評估進終態。"""
    wd, ws, notifier = make_wd()
    for _ in range(DEFAULT_ORDER_THRESHOLD):
        wd.record_order_action()
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS + 1
    wd.check()                       # attempt 1
    assert ws.reconnects == 1

    for i, wait in enumerate(BACKOFF_SECONDS):
        frozen_clock["t"] += wait - 1
        wd.check()                   # 退避未滿，不得動作
        assert ws.reconnects == i + 1, f"退避 {wait}s 未到就重連了"
        frozen_clock["t"] += 1
        wd.check()
        if i < len(BACKOFF_SECONDS) - 1:
            assert ws.reconnects == i + 2
        else:
            assert wd.state == "given_up"
            assert ws.reconnects == len(BACKOFF_SECONDS)

    # 終態後不論再過多久都不得重連
    frozen_clock["t"] += 100_000
    wd.check()
    assert ws.reconnects == len(BACKOFF_SECONDS)
    assert len(notifier.sent) == 2   # 判死一封 + 放棄一封


def test_event_resets_everything(frozen_clock):
    wd, ws, notifier = make_wd()
    for _ in range(DEFAULT_ORDER_THRESHOLD):
        wd.record_order_action()
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS + 1
    wd.check()
    assert wd.attempts == 1

    wd.record_event()
    assert wd.state == "healthy"
    assert wd.attempts == 0
    assert wd.orders_since_event == 0
    assert wd.next_attempt_at == 0.0
    assert len(notifier.sent) == 2   # 判死一封 + 恢復一封

    # 重置後必須重新累積才會再判死
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS + 1
    wd.check()
    assert ws.reconnects == 1


def test_event_leaves_given_up_state(frozen_clock):
    wd, ws, _ = make_wd()
    for _ in range(DEFAULT_ORDER_THRESHOLD):
        wd.record_order_action()
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS + 1
    wd.check()
    for wait in BACKOFF_SECONDS:
        frozen_clock["t"] += wait
        wd.check()
    assert wd.state == "given_up"

    wd.record_event()
    assert wd.state == "healthy"


def test_given_up_periodically_reminds(frozen_clock, caplog):
    """finding 3：given_up 不該完全靜默。節流提醒（每 GIVEN_UP_REMINDER_SECONDS）
    要在間隔內不重複，間隔到了要再打一次。"""
    from grid_engine.userdata_watchdog import GIVEN_UP_REMINDER_SECONDS

    wd, ws, _ = make_wd()
    for _ in range(DEFAULT_ORDER_THRESHOLD):
        wd.record_order_action()
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS + 1
    wd.check()
    for wait in BACKOFF_SECONDS:
        frozen_clock["t"] += wait
        wd.check()
    assert wd.state == "given_up"

    caplog.clear()
    with caplog.at_level("WARNING"):
        # 間隔內連續 check 多次 -> 不得重複提醒
        for _ in range(5):
            frozen_clock["t"] += 60
            wd.check()
        reminders = [r for r in caplog.records if "given_up" in r.message]
        assert len(reminders) == 0

        frozen_clock["t"] += GIVEN_UP_REMINDER_SECONDS
        wd.check()
        reminders = [r for r in caplog.records if "given_up" in r.message]
        assert len(reminders) == 1


def test_backoff_seconds_values_are_pinned():
    """BACKOFF_SECONDS 的實際數值是規格常數，不是任意遞增序列。
    test_backoff_sequence_then_give_up 是從模組本身讀 BACKOFF_SECONDS 來驅動時間推進，
    改動這個常數的值不會讓它轉紅（自洽），必須另外硬編碼釘住。"""
    assert BACKOFF_SECONDS == (300.0, 900.0, 2700.0)


def test_watchdog_constants_are_pinned():
    """K / N / CHECK_INTERVAL 是規格常數（判準：orders_since_event>=4 且
    silence>=600s，每 60s 檢查一次）。上面所有測試都從模組本身 import 這幾個
    值來驅動輸入，改常數值全套照綠（自洽），必須另外硬編碼釘住字面值。"""
    assert DEFAULT_ORDER_THRESHOLD == 4
    assert DEFAULT_SILENCE_SECONDS == 600.0
    assert CHECK_INTERVAL == 60.0


def test_record_event_updates_last_event_at(frozen_clock):
    """verifier-fix finding 2：record_event() 必須真的推進 last_event_at。若拿掉
    `self.last_event_at = clock.now()` 這行，last_event_at 永遠停在舊值，之後只要
    湊滿門檻張數就會立刻判死——600s 的推送延遲餘裕永久消失。
    mutation test：拿掉那一行賦值，下面 `assert wd.state == "healthy"` 必須紅。
    """
    wd, ws, notifier = make_wd()
    for _ in range(DEFAULT_ORDER_THRESHOLD):
        wd.record_order_action()
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS + 1
    wd.check()
    assert wd.state == "degraded"
    assert ws.reconnects == 1

    wd.record_event()
    assert wd.state == "healthy"

    # 推進的時間 < silence_seconds，但湊滿門檻張數：若 last_event_at 沒被更新，
    # now - last_event_at 會用回舊的 last_event_at，超過 silence_seconds，立刻誤判死。
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS - 1
    for _ in range(DEFAULT_ORDER_THRESHOLD):
        wd.record_order_action()
    wd.check()
    assert wd.state == "healthy", \
        "record_event() 沒更新 last_event_at，600s 推送延遲餘裕消失了"
    assert ws.reconnects == 1, "不該有新的重連——餘裕還沒到"


def test_first_alert_sent_even_if_reconnect_raises(frozen_clock):
    """verifier-fix finding 5：_notify() 必須排在 request_reconnect() 之前。若
    request_reconnect() 拋例外而 _notify() 排在它後面，第一封「⚠️ 疑似靜默失效」
    會永遠發不出去（run() 的 broad except 接住例外，watchdog 不死，但這封信沒了）。
    mutation test：把 _notify 挪回 request_reconnect() 之後，
    `assert len(notifier.sent) == 1` 必須紅（notifier.sent 會是空的，因為
    request_reconnect() 先拋例外，_notify 那行永遠執行不到）。
    """
    class BoomWs(FakeWs):
        def request_reconnect(self):
            super().request_reconnect()
            raise RuntimeError("ws down")

    ws = BoomWs()
    notifier = FakeNotifier()
    wd = UserDataWatchdog(ws_client=ws, notifier=notifier, tasks=[],
                          stop_event=asyncio.Event())
    for _ in range(DEFAULT_ORDER_THRESHOLD):
        wd.record_order_action()
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS + 1

    with pytest.raises(RuntimeError):
        wd.check()

    assert len(notifier.sent) == 1, \
        "request_reconnect() 掛掉不能連累第一封告警發不出去"
    assert ws.reconnects == 1


def test_watchdog_has_no_trading_surface():
    """安全約束：watchdog 不得具備下單/撤單能力。"""
    forbidden = {"place_order", "cancel_order", "cancel_orders_for_side",
                 "close_symbol_positions", "create_order"}
    assert forbidden.isdisjoint(dir(UserDataWatchdog))
