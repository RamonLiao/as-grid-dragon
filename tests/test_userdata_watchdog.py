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


def refuel(wd):
    """重新餵滿判死門檻所需的張數。

    dual-review B2 之後，每次強制重連都會把證據（orders_since_event / last_event_at）
    歸零 —— 下一次判死必須靠**新**證據，所以要走多次判死的測試每輪都得重新餵。
    """
    for _ in range(DEFAULT_ORDER_THRESHOLD):
        wd.record_order_action()


def drive_to_given_up(wd, holder):
    """把狀態機一路推到 given_up（3 次強制重連後放棄）。

    每一步都重新餵證據並推進「max(退避, 靜默門檻)+1」秒：兩個閘門都跨過去，
    確保推進的是狀態機本身而不是碰巧卡在某個閘門上。
    """
    waits = [DEFAULT_SILENCE_SECONDS + 1] + [
        max(b, DEFAULT_SILENCE_SECONDS) + 1 for b in BACKOFF_SECONDS
    ]
    for wait in waits:
        refuel(wd)
        holder["t"] += wait
        wd.check()


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


@pytest.mark.asyncio
async def test_detects_and_reconnects_once(frozen_clock):
    wd, ws, notifier = make_wd()
    refuel(wd)
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS + 1
    wd.check()
    assert wd.state == "degraded"
    assert ws.reconnects == 1
    assert wd.attempts == 1
    await asyncio.sleep(0)
    assert len(notifier.sent) == 1
    # 立刻再 check 不得重複重連（退避未到 + 證據已重取）
    wd.check()
    assert ws.reconnects == 1


@pytest.mark.asyncio
async def test_backoff_sequence_then_give_up(frozen_clock):
    """退避必須是 300/900/2700，第 4 次評估進終態。

    dual-review B2 之後每次強制重連都重取證據，所以每一輪都要重新餵滿門檻張數
    （舊版靠同一批陳舊證據連續判死三次——那正是 B2 要修掉的缺陷，舊測試把它當
    正確答案鎖住了）。改寫後仍然是有效守衛，三件事都還釘著：

    1. 退避 300：300 < 靜默門檻 600，被靜默閘門遮住、無法用「時間差」側面測到，
       改成直接斷言 next_attempt_at 的數值（硬編碼字面值，不從模組推導）。
    2. 退避 900 / 2700：兩者都 > 600，證據在「差 1 秒」與「到點」兩個時點都已備妥，
       唯一的差異就是退避閘門 —— 差 1 秒不得動作、到點才動作。
    3. 3 次上限：第 4 次評估必須進 given_up 且不得再多重連一次，終態後再久、
       再多證據都不得重連。
    """
    wd, ws, notifier = make_wd()

    refuel(wd)
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS + 1
    wd.check()                       # attempt 1
    assert ws.reconnects == 1
    assert wd.attempts == 1
    assert wd.next_attempt_at == frozen_clock["t"] + 300.0, "第 1 次退避必須是 300s"

    # attempt 2：退避 300 早已過，等新證據湊滿（K 張單 + 600s 靜默）
    refuel(wd)
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS + 1
    wd.check()
    assert ws.reconnects == 2
    assert wd.attempts == 2
    assert wd.next_attempt_at == frozen_clock["t"] + 900.0, "第 2 次退避必須是 900s"

    # attempt 3：證據在 899s 時就已備妥（899 > 600），純粹由退避閘門擋住
    refuel(wd)
    frozen_clock["t"] += 900.0 - 1
    wd.check()
    assert ws.reconnects == 2, "退避 900s 未到就重連了"
    frozen_clock["t"] += 1
    wd.check()
    assert ws.reconnects == 3
    assert wd.attempts == 3
    assert wd.next_attempt_at == frozen_clock["t"] + 2700.0, "第 3 次退避必須是 2700s"

    # 第 4 次評估：退避 2700 未到不得進終態，到點才放棄
    refuel(wd)
    frozen_clock["t"] += 2700.0 - 1
    wd.check()
    assert wd.state == "degraded", "退避 2700s 未到就進終態了"
    frozen_clock["t"] += 1
    wd.check()
    assert wd.state == "given_up"
    assert ws.reconnects == 3, "上限 3 次：進終態不得再多重連一次"

    # 終態後不論再過多久、再累積多少證據都不得重連
    refuel(wd)
    frozen_clock["t"] += 100_000
    wd.check()
    assert ws.reconnects == len(BACKOFF_SECONDS)
    await asyncio.sleep(0)
    assert len(notifier.sent) == 2   # 判死一封 + 放棄一封


@pytest.mark.asyncio
async def test_event_resets_everything(frozen_clock):
    wd, ws, notifier = make_wd()
    refuel(wd)
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS + 1
    wd.check()
    assert wd.attempts == 1

    wd.record_event()
    assert wd.state == "healthy"
    assert wd.attempts == 0
    assert wd.orders_since_event == 0
    assert wd.next_attempt_at == 0.0
    await asyncio.sleep(0)
    assert len(notifier.sent) == 2   # 判死一封 + 恢復一封

    # 重置後必須重新累積才會再判死
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS + 1
    wd.check()
    assert ws.reconnects == 1


def test_event_leaves_given_up_state(frozen_clock):
    wd, ws, _ = make_wd()
    drive_to_given_up(wd, frozen_clock)
    assert wd.state == "given_up"

    wd.record_event()
    assert wd.state == "healthy"


def test_given_up_periodically_reminds(frozen_clock, caplog):
    """finding 3：given_up 不該完全靜默。節流提醒（每 GIVEN_UP_REMINDER_SECONDS）
    要在間隔內不重複，間隔到了要再打一次。"""
    from grid_engine.userdata_watchdog import GIVEN_UP_REMINDER_SECONDS

    wd, ws, _ = make_wd()
    drive_to_given_up(wd, frozen_clock)
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

    ⚠️ check() 在強制重連時也會把 last_event_at 錨到當下（dual-review B2），所以
    record_event() 之前必須先讓時鐘走一段（下面的 +100），否則兩者的值相同、
    mutation 抓不到 —— 「測資裡混入其他因素讓缺陷被繞過」的老陷阱。
    """
    wd, ws, notifier = make_wd()
    refuel(wd)
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS + 1
    wd.check()
    assert wd.state == "degraded"
    assert ws.reconnects == 1

    frozen_clock["t"] += 100          # 讓 record_event() 的錨點明顯晚於 check() 的
    wd.record_event()
    assert wd.state == "healthy"

    # 推進的時間 < silence_seconds，但湊滿門檻張數：若 last_event_at 沒被更新，
    # now - last_event_at 會用回舊的 last_event_at，超過 silence_seconds，立刻誤判死。
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS - 1
    refuel(wd)
    wd.check()
    assert wd.state == "healthy", \
        "record_event() 沒更新 last_event_at，600s 推送延遲餘裕消失了"
    assert ws.reconnects == 1, "不該有新的重連——餘裕還沒到"


@pytest.mark.asyncio
async def test_first_alert_sent_even_if_reconnect_raises(frozen_clock):
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

    await asyncio.sleep(0)
    assert len(notifier.sent) == 1, \
        "request_reconnect() 掛掉不能連累第一封告警發不出去"
    assert ws.reconnects == 1


def test_reconnect_refetches_evidence_quiet_market_not_killed_again(frozen_clock):
    """dual-review B2：重連請求發出時必須同時重取證據。

    情境（外部 reviewer 實跑重現）：重連**成功修好了 stream**，但市場安靜、
    完全沒有新單（實盤成交率曾低到 ~1 筆/天，且裝死模式不 requote）。
    不重置證據的話，orders_since_event / last_event_at 只有 record_event() 會清，
    於是同一批陳舊證據會在 300/900/2700 秒後再判死兩次 —— 65 分鐘內燒完三次
    強制重連、發出「需人工介入」的 ⛔ 告警，而 stream 其實是好的；每次假重連
    都把 state.connected 切 False、中斷 bookTicker，在有實倉時製造 decide() 盲窗。

    mutation：拿掉 check() 裡 request_reconnect() 前的
    `self.orders_since_event = 0` / `self.last_event_at = now`
    ⇒ 紅在 `assert ws.reconnects == 1`（會變成 3）。
    """
    wd, ws, notifier = make_wd()
    refuel(wd)
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS + 1
    wd.check()
    assert ws.reconnects == 1
    assert wd.orders_since_event == 0, "重連請求發出時必須把張數證據歸零"
    assert wd.last_event_at == frozen_clock["t"], "重連請求發出時必須把靜默計時重新錨定"

    # 之後市場完全安靜：沒有任何新單，時間一路走過三個退避週期
    for _ in range(200):
        frozen_clock["t"] += CHECK_INTERVAL
        wd.check()

    assert ws.reconnects == 1, \
        "重連後沒有新證據（安靜市場），不得用同一批陳舊證據再判死"
    assert wd.state == "degraded", "不得在零新證據的情況下燒到 given_up"
    assert wd.attempts == 1


def test_new_evidence_after_reconnect_still_triggers_next_attempt(frozen_clock):
    """B2 的反方向：真的還壞著（重連後又累積了 K 張單且持續零推送）時，
    退避到點仍然必須進行第 2 次強制重連 —— 不能因為重取證據就變成永不再試。"""
    wd, ws, _ = make_wd()
    refuel(wd)
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS + 1
    wd.check()
    assert ws.reconnects == 1

    refuel(wd)
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS + 1
    wd.check()
    assert ws.reconnects == 2, "重連後累積了新證據，退避到點必須再試一次"


# ---- monkey testing（dual-review C2：極端輸入 / 極端呼叫序列）----

def test_clock_rewind_does_not_freeze_state_machine(frozen_clock):
    """時鐘倒退（NTP 校正/容器時間同步/人工改時間）不得讓 next_attempt_at
    卡死在永遠到不了的未來 —— 那等於 watchdog 自己靜默失效，正是本元件要根除的
    失效模式（spec §8.1 已認列的風險）。

    mutation：拿掉 check() 開頭的 _reanchor_if_clock_rewound() 呼叫
    ⇒ 紅在 `assert ws.reconnects == 2`（倒退後永遠等不到 next_attempt_at）。
    """
    wd, ws, _ = make_wd()

    # 先在一個「很晚」的時點觸發第 1 次重連 ⇒ next_attempt_at 落在很晚 + 300
    frozen_clock["t"] = 5_000_000_000.0
    refuel(wd)
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS + 1
    wd.check()
    assert ws.reconnects == 1
    assert wd.next_attempt_at > 5_000_000_000.0

    # 時鐘倒退到正常值：所有時間基準都變成「未來」
    frozen_clock["t"] = 1_000_000.0
    wd.check()
    assert wd.next_attempt_at <= frozen_clock["t"] + max(BACKOFF_SECONDS), \
        "倒退後時間基準必須被重新錨定，不能停在永遠到不了的未來"
    assert wd.last_event_at <= frozen_clock["t"]

    # 重新累積證據 + 走過一個退避上限後，狀態機必須還活著
    refuel(wd)
    frozen_clock["t"] += max(BACKOFF_SECONDS) + 1
    wd.check()
    assert ws.reconnects == 2, "時鐘倒退後狀態機必須仍能繼續判死/重連，不得永久卡死"


def test_clock_rewind_guard_is_noop_in_normal_operation(frozen_clock):
    """防呆不得誤傷正常流程：時鐘只前進時，重新錨定一次都不該發生。"""
    wd, ws, _ = make_wd()
    refuel(wd)
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS + 1
    wd.check()
    anchored = (wd.last_event_at, wd.next_attempt_at)

    frozen_clock["t"] += 1        # 只前進
    wd.check()
    assert (wd.last_event_at, wd.next_attempt_at) == anchored, \
        "時鐘正常前進時不得觸發重新錨定"


def test_huge_order_action_count_does_not_change_semantics(frozen_clock):
    """record_order_action() 被呼叫極大次數（極端輸入）：判準是 `>= K`，
    多出來的張數不得讓退避/上限失效，也不得溢位成別的行為。"""
    wd, ws, _ = make_wd()
    for _ in range(1_000_000):
        wd.record_order_action()
    assert wd.orders_since_event == 1_000_000

    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS + 1
    wd.check()
    assert ws.reconnects == 1, "百萬張單也只該觸發一次重連"
    assert wd.attempts == 1
    assert wd.orders_since_event == 0, "重連後證據歸零（不論之前累積多大）"

    # 再連續 check 一萬次也不得多出任何一次重連（沒有新證據）
    for _ in range(10_000):
        frozen_clock["t"] += CHECK_INTERVAL
        wd.check()
    assert ws.reconnects == 1


def test_repeated_check_in_every_state_is_idempotent(frozen_clock, caplog):
    """check() 在各狀態下被連續呼叫（同一時點、零時間推進）都必須是冪等的：
    healthy 不動作、degraded 不重複重連、given_up 不重複提醒/不重連。"""
    wd, ws, notifier = make_wd()

    for _ in range(100):             # healthy
        wd.check()
    assert ws.reconnects == 0
    assert wd.state == "healthy"

    refuel(wd)
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS + 1
    wd.check()
    for _ in range(100):             # degraded，同一時點
        wd.check()
    assert ws.reconnects == 1
    assert wd.attempts == 1

    drive_to_given_up(wd, frozen_clock)
    assert wd.state == "given_up"
    reconnects_at_give_up = ws.reconnects

    import logging
    caplog.set_level(logging.WARNING, logger="as_grid_max")
    caplog.clear()
    for _ in range(100):             # given_up，同一時點
        wd.check()
    assert ws.reconnects == reconnects_at_give_up
    reminders = [r for r in caplog.records if "given_up" in r.message]
    assert len(reminders) == 0, "終態下同一時點連續 check 不得洗版提醒"


def test_notify_without_event_loop_only_logs(frozen_clock, caplog):
    """dual-review D：無 running loop 時只留 log，不得退回 asyncio.run(...)。

    對齊 order_executor.py 的既有 pattern（專案規則 9：兩個 pattern 互斥時選一個）。
    這裡在同步環境呼叫 check()：必須不拋例外、必須留下 log，而且 notifier 不會被送到。
    """
    import logging
    wd, ws, notifier = make_wd()
    caplog.set_level(logging.WARNING, logger="as_grid_max")

    refuel(wd)
    frozen_clock["t"] += DEFAULT_SILENCE_SECONDS + 1
    wd.check()                       # 同步環境，無 running loop

    assert ws.reconnects == 1, "通知路徑的降級不得影響狀態機與重連"
    assert notifier.sent == [], "無 loop 時不得同步跑掉 coroutine"
    assert any("無 event loop" in r.message for r in caplog.records), \
        "無 loop 時必須留 log，不能靜默丟訊息"


def test_watchdog_has_no_trading_surface():
    """安全約束：watchdog 不得具備下單/撤單能力。"""
    forbidden = {"place_order", "cancel_order", "cancel_orders_for_side",
                 "close_symbol_positions", "create_order"}
    assert forbidden.isdisjoint(dir(UserDataWatchdog))
