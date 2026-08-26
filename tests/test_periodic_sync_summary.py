"""每日摘要的 REST 同步狀態行。

降級狀態若只靠即時告警，錯過那一封就再也看不到——摘要是它唯一的持續表面。
"""
import pytest

from grid_engine import clock
from grid_engine.notifier import TelegramNotifier
from grid_engine.reporting import DailyReporter


class _FakeSync:
    def __init__(self, degraded=False, failures=0, total=0, last_sync_time=0.0,
                 interval=10.0):
        self._degraded = degraded
        self._consecutive_failures = failures
        self._degraded_total = total
        self.last_sync_time = last_sync_time
        self._interval = interval

    def _loop_interval(self):
        return self._interval


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
    status = reporter._get_sync_status()
    assert status["degraded"] is True
    assert status["consecutive_failures"] == 4
    assert status["degraded_total"] == 1


def test_reporter_reports_heartbeat_age():
    """心跳年齡必須進摘要資料（review I1）：degraded/degraded_total 只在 loop
    活著時才會動，loop 死了它們永遠是 False/0；last_sync_time 是唯一由「同步
    真的跑完」推進的量。缺這個欄位，摘要就無法區分「一切正常」與「整條停擺」。
    """
    reporter = DailyReporter(config=None, state=None, notifier=None, stop_event=None)
    reporter.sync_source = _FakeSync(last_sync_time=clock.guard_now() - 300.0)
    status = reporter._get_sync_status()
    assert 290.0 < status["last_sync_age"] < 310.0
    assert status["sync_interval"] == 10.0


def test_reporter_heartbeat_is_none_when_never_synced():
    """last_sync_time 初值 0 直接相減會得到 ~1.8e9 秒的假年齡。用 None 表達
    「無年齡可算」，交給 formatter 用專屬文案講——不是省略。
    """
    reporter = DailyReporter(config=None, state=None, notifier=None, stop_event=None)
    reporter.sync_source = _FakeSync(last_sync_time=0.0)
    assert reporter._get_sync_status()["last_sync_age"] is None


def test_reporter_keeps_degraded_state_when_heartbeat_unreadable():
    """心跳讀不到只讓那兩個鍵缺席，不得連累已經讀到的降級狀態、更不得讓整行
    消失——降級中的告警是主訊號。
    """
    class HalfBroken:
        _degraded = True
        _consecutive_failures = 5
        _degraded_total = 2

        @property
        def last_sync_time(self):
            raise RuntimeError("boom")

    reporter = DailyReporter(config=None, state=None, notifier=None, stop_event=None)
    reporter.sync_source = HalfBroken()
    status = reporter._get_sync_status()
    assert status["degraded"] is True
    assert "last_sync_age" not in status


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


# --- 心跳：loop 整條死掉時，摘要那行必須與「一切正常」長得不一樣（review I1）---

def _hb(age, interval=10.0, **kw):
    d = {"degraded": False, "consecutive_failures": 0, "degraded_total": 0,
         "last_sync_age": age, "sync_interval": interval}
    d.update(kw)
    return d


def test_heartbeat_never_synced_warns():
    """從未同步過：last_sync_age=None。sync_all() 現在在 bot.run() 啟動時就會
    蓋章，摘要發送時（最快也是啟動後數小時）還沒蓋過 = 一輪都沒成功結束過。
    """
    line = TelegramNotifier._format_sync_line(_hb(None))
    assert "停擺" in line and "從未" in line


def test_heartbeat_fresh_sync_stays_quiet():
    """剛同步完：健康狀態不加噪音，維持整行省略。"""
    assert TelegramNotifier._format_sync_line(_hb(5.0)) == ""


def test_heartbeat_stall_warns_unconditionally():
    """停擺超過門檻 max(60, 6*interval)：無條件印警告，優先於其他分支。

    拿掉 _format_sync_line 的心跳段，這條會在這一行紅——因為 degraded=False
    且 degraded_total=0（loop 死掉時它們的必然值）會走回「整行省略」。
    """
    line = TelegramNotifier._format_sync_line(_hb(1800.0))
    assert "停擺" in line and "30" in line


def test_heartbeat_threshold_has_60s_floor():
    """interval 極小（測試/誤設 0.01）時門檻不得跟著塌成毫秒級：60 秒地板。"""
    assert TelegramNotifier._format_sync_line(_hb(30.0, interval=0.01)) == ""
    assert "停擺" in TelegramNotifier._format_sync_line(_hb(90.0, interval=0.01))


def test_heartbeat_threshold_scales_with_interval():
    """interval 大時門檻跟著放大：6 輪餘裕吸收單次 REST 抖動與重試。"""
    assert TelegramNotifier._format_sync_line(_hb(300.0, interval=60.0)) == ""
    assert "停擺" in TelegramNotifier._format_sync_line(_hb(400.0, interval=60.0))


def test_heartbeat_negative_age_does_not_vanish():
    """牆鐘往回跳：不得讓這行消失（那會被讀成「剛同步過」）。沿用
    bot._note_stale_quote 對時鐘後跳的既有態度——重新錨定成告警，不吞掉。
    """
    line = TelegramNotifier._format_sync_line(_hb(-500.0))
    assert line != ""
    assert "時鐘後跳" in line


@pytest.mark.parametrize("bad", ["x", None, float("nan"), float("inf"), object()])
def test_heartbeat_bad_age_degrades_to_warning(bad):
    """age 欄位壞掉一律降級成告警行，不得整行消失——這行本身就是儀器。
    （None 走「從未同步」專屬文案，同樣不省略。）
    """
    assert TelegramNotifier._format_sync_line(_hb(bad)) != ""


def test_heartbeat_absent_falls_back_to_old_behaviour():
    """鍵缺席（舊呼叫端／reporter 心跳讀取降級）只退回舊行為，不假裝健康
    也不誤報停擺。"""
    assert TelegramNotifier._format_sync_line(
        {"degraded": True, "consecutive_failures": 3, "degraded_total": 1}) != ""


def test_stall_line_wins_over_degraded_line():
    """心跳優先：停擺時就算 degraded 也印停擺——「同步整條沒在跑」比
    「連續失敗 N 次」更接近真相，後者的計數本身就已經不可信了。"""
    line = TelegramNotifier._format_sync_line(
        _hb(3600.0, degraded=True, consecutive_failures=9))
    assert "停擺" in line


# --- #7：dict 型別對但欄位值壞 ---

def test_degraded_line_survives_broken_failure_count():
    """`int(None)` 原本會讓整個 formatter 走 except → return ""，也就是
    **降級中的警告整行被吞掉**——fail-silent，正是這條 branch 要根除的形態。
    壞欄位只能降級成不帶數字的保守文案，主訊號不能掉。
    """
    line = TelegramNotifier._format_sync_line(
        {"degraded": True, "consecutive_failures": None, "degraded_total": 1})
    assert "降級中" in line
    assert "None" not in line


@pytest.mark.parametrize("bad", [None, "abc", object(), []])
def test_recovered_line_survives_broken_total(bad):
    """非降級路徑的累計數壞掉同樣不得靜默消失——欄位壞本身就是該被看見的異常。"""
    line = TelegramNotifier._format_sync_line(
        {"degraded": False, "consecutive_failures": 0, "degraded_total": bad})
    assert line != ""
    assert "異常" in line


# --- B3（dual-review Important）：門檻的天花板 --------------------------------

def test_heartbeat_threshold_has_ceiling():
    """interval 被設成巨大但**有限**的值時，6*interval 會讓停擺門檻大到永不告警
    ——停擺偵測被一個設定值靜默關掉。1 小時的天花板擋住這件事。

    mutation（實跑過）：把 `min(..., SYNC_STALE_CEILING_SEC)` 拿掉
    ⇒ 紅在第二個斷言（門檻變成 6*86400=518400s，4000s 的停擺完全不告警）。
    """
    # interval=86400（一天）：舊算法門檻 518400s，新算法被夾到 3600s
    assert TelegramNotifier._format_sync_line(_hb(3000.0, interval=86400.0)) == ""
    assert "停擺" in TelegramNotifier._format_sync_line(_hb(4000.0, interval=86400.0))


# --- M12：age 與 interval 的 try 必須拆開 --------------------------------------

def test_broken_interval_does_not_poison_healthy_age():
    """`interval` 欄位壞掉不得讓**健康的 age** 也被判成「心跳讀取異常」：
    主訊號（心跳）不該因為門檻參數讀不到就變成告警或消失。
    壞掉的 interval 退回 0.0 ⇒ 門檻落到 60s 這個安全地板。
    """
    # age 健康（5s < 60s 地板）⇒ 整行省略，不誤報
    assert TelegramNotifier._format_sync_line(_hb(5.0, interval="abc")) == ""
    # age 真的停擺 ⇒ 照樣印停擺，而不是「心跳讀取異常」
    line = TelegramNotifier._format_sync_line(_hb(1800.0, interval="abc"))
    assert "停擺" in line
    assert "異常" not in line
