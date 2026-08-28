"""TUI 對 MaxGridBot 的生命週期所有權。

守的是同一句話：**`self.bot = None` 不得在 bot thread 還活著時執行**——
那不是「重置 UI 狀態」，那是放棄對一個還在真錢帳戶上下單的物件的唯一控制權。
一旦放棄，`stop_trading` 與 `_handle_shutdown` 都認不得它（兩者都以 `self.bot`
為入口守衛），該 bot 變成孤兒：會繼續掛單，且再也停不掉。

觸發面不是理論值：`bot.run()` 的前三步（_init_exchange / _check_hedge_mode /
acquire_listen_key）全要連外，而 `state.running` 是在它們**之後**才設 True。
2026-08-24 08:38~08:39 的 log 有整整一分鐘的 DNS 失敗
（`nodename nor servname provided`），足以吃掉 start_trading 的 ~20 秒等待。
"""
import threading
import time

import pytest

import as_terminal_max as tm


class FakeState:
    def __init__(self, running=False):
        self.running = running


class FakeBot:
    """假 bot：只提供 TUI 會碰到的介面。"""
    def __init__(self, config=None, running=False):
        self.state = FakeState(running)
        self.stop_called = 0

    async def stop(self):
        self.stop_called += 1


class FakeThread:
    def __init__(self, alive=True):
        self._alive = alive
        self.join_calls = 0

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        self.join_calls += 1


class FakeLoop:
    def __init__(self, running=True):
        self._running = running

    def is_running(self):
        return self._running


def make_menu(bot=None, thread=None, loop=None, trading_active=False):
    """繞過 MainMenu.__init__ —— 它會註冊 signal handler、讀真實 config/、
    並 touch /tmp/.as-grid-running（那是生產引擎的 restart marker，測試不得碰）。"""
    menu = object.__new__(tm.MainMenu)
    menu.config = None
    menu.bot = bot
    menu.bot_thread = thread
    menu.bot_loop = loop
    menu._trading_active = trading_active
    return menu


@pytest.fixture(autouse=True)
def _no_blocking_io(monkeypatch):
    monkeypatch.setattr(tm.Prompt, "ask", lambda *a, **k: "")
    monkeypatch.setattr(tm.console, "print", lambda *a, **k: None)


class TestStartTradingTimeout:
    """start_trading 等 state.running 逾時 = 初始化慢，不是 bot 不存在。"""

    def _arm(self, monkeypatch, thread_alive=True):
        created = {}

        def fake_bot_factory(config):
            bot = FakeBot(config)
            created["bot"] = bot
            return bot

        class StubThread:
            def __init__(self, target=None, daemon=None):
                self._target = target
                self._alive = thread_alive

            def start(self):
                pass

            def is_alive(self):
                return self._alive

            def join(self, timeout=None):
                pass

        monkeypatch.setattr(tm, "MaxGridBot", fake_bot_factory)
        monkeypatch.setattr(tm.threading, "Thread", StubThread)
        monkeypatch.setattr(tm.time, "sleep", lambda s: None)
        return created

    def _config(self):
        import types
        sym = types.SimpleNamespace(enabled=True)
        return types.SimpleNamespace(api_key="k", symbols={"BNB/USDC:USDC": sym})

    def test_timeout_keeps_bot_reference_while_thread_alive(self, monkeypatch):
        """紅在：現行 code 在逾時分支做 `self.bot = None`，thread 卻還活著
        ⇒ 孤兒 bot 會繼續掛單且 stop_trading/_handle_shutdown 都認不得它。"""
        created = self._arm(monkeypatch, thread_alive=True)
        menu = make_menu()
        menu.config = self._config()

        menu.start_trading()

        assert menu.bot is created["bot"], "thread 還活著時不得丟掉 bot 參照"
        assert menu.bot_thread is not None

    def test_timeout_clears_reference_when_thread_already_dead(self, monkeypatch):
        """thread 已死 ⇒ 沒有孤兒，參照該清掉，否則下次啟動會被自己的守衛擋住。"""
        self._arm(monkeypatch, thread_alive=False)
        menu = make_menu()
        menu.config = self._config()

        menu.start_trading()

        assert menu.bot is None
        assert menu._trading_active is False

    def test_refuses_to_start_a_second_bot_while_thread_alive(self, monkeypatch):
        """紅在：現行入口守衛只看 `_trading_active`，而逾時路徑已把它留在 False
        ⇒ 同一帳戶上會有兩個 MaxGridBot 同時撤單/掛單。"""
        created = self._arm(monkeypatch, thread_alive=True)
        menu = make_menu(bot=FakeBot(), thread=FakeThread(alive=True),
                         trading_active=False)
        menu.config = self._config()
        first = menu.bot

        menu.start_trading()

        assert "bot" not in created, "舊 thread 還活著時不得建構第二個 bot"
        assert menu.bot is first

    def test_stale_dead_thread_does_not_block_restart(self, monkeypatch):
        """bot 自己死掉（初始化失敗）留下的殘留參照，不得讓使用者永遠不能再啟動。"""
        created = self._arm(monkeypatch, thread_alive=False)
        menu = make_menu(bot=FakeBot(), thread=FakeThread(alive=False),
                         trading_active=False)
        menu.config = self._config()

        menu.start_trading()

        assert "bot" in created, "thread 已死的殘留參照必須被清掉並允許重新啟動"


class TestStopTradingJoinTimeout:
    """join 逾時 = 舊 bot 還在跑，不是停好了。"""

    def test_keeps_everything_when_join_times_out(self):
        """紅在：現行 code 不看 is_alive()，逾時後照樣清 `self.bot`
        ⇒ 舊 bot 還在送單，使用者卻可以立刻再啟動一個。"""
        bot = FakeBot()
        thread = FakeThread(alive=True)
        menu = make_menu(bot=bot, thread=thread, loop=FakeLoop(running=False),
                         trading_active=True)

        menu.stop_trading()

        assert menu.bot is bot, "thread 還活著時不得丟掉 bot 參照"
        assert menu.bot_thread is thread
        assert thread.join_calls == 1

    def test_clears_when_thread_actually_died(self):
        thread = FakeThread(alive=False)
        menu = make_menu(bot=FakeBot(), thread=thread, loop=FakeLoop(running=False),
                         trading_active=True)

        menu.stop_trading()

        assert menu.bot is None
        assert menu.bot_thread is None
        assert menu.bot_loop is None
        assert menu._trading_active is False

    def test_can_retry_stop_after_a_timeout(self):
        """逾時後必須還能再按停止（參照留著才辦得到）。"""
        bot = FakeBot()
        thread = FakeThread(alive=True)
        menu = make_menu(bot=bot, thread=thread, loop=FakeLoop(running=False),
                         trading_active=True)

        menu.stop_trading()
        thread._alive = False
        menu.stop_trading()

        assert thread.join_calls == 2
        assert menu.bot is None

    def test_orphan_from_start_timeout_can_be_stopped(self):
        """端到端：start_trading 逾時留下的孤兒，stop_trading 必須認得
        —— 現行入口守衛 `if not self._trading_active` 會直接 return。"""
        bot = FakeBot()
        thread = FakeThread(alive=False)
        menu = make_menu(bot=bot, thread=thread, loop=FakeLoop(running=False),
                         trading_active=False)   # 逾時路徑正是 False

        menu.stop_trading()

        assert menu.bot is None, "孤兒 bot 必須停得掉"


class TestHandleShutdown:
    def test_stops_orphan_bot_even_when_trading_active_is_false(self):
        """紅在：現行守衛是 `if self._trading_active and self.bot and ...`
        ⇒ 孤兒狀態下 Ctrl+C 不會 graceful stop，掛單與 listenKey 都不收。"""
        import asyncio

        bot = FakeBot()
        loop = asyncio.new_event_loop()
        stopped = {}

        def fake_run_coroutine_threadsafe(coro, lp):
            coro.close()
            stopped["called"] = True

            class F:
                def result(self, timeout=None):
                    return None
            return F()

        thread = FakeThread(alive=True)
        menu = make_menu(bot=bot, thread=thread, loop=FakeLoop(running=True),
                         trading_active=False)

        import unittest.mock as mock
        with mock.patch.object(tm.asyncio, "run_coroutine_threadsafe",
                               fake_run_coroutine_threadsafe):
            with pytest.raises(SystemExit):
                menu._handle_shutdown(15, None)

        loop.close()
        assert stopped.get("called") is True

    def test_no_bot_exits_cleanly(self):
        menu = make_menu()
        with pytest.raises(SystemExit):
            menu._handle_shutdown(15, None)


class TestMainMenuGating:
    """選單本身也不能只看 _trading_active —— 否則孤兒狀態下按不到停止。"""

    class _Done(Exception):
        """哨兵：main_menu 是無窮迴圈，choice "s" 不會 break，用例外跳出。"""

    def _drive_once(self, menu, monkeypatch, choice="0"):
        """跑 main_menu 一輪並攔下 valid_choices。"""
        seen = {}
        calls = {"n": 0}

        def fake_ask(prompt, choices=None, default=None, **kw):
            if choices is None:            # 「按 Enter 繼續」之類
                return ""
            seen["choices"] = list(choices)
            calls["n"] += 1
            if calls["n"] > 1:             # 第二輪就收工，避免無窮迴圈
                raise self._Done()
            return choice

        def fake_stop():
            seen["stopped"] = True

        monkeypatch.setattr(tm.Prompt, "ask", fake_ask)
        monkeypatch.setattr(tm.Confirm, "ask", lambda *a, **k: True)
        monkeypatch.setattr(menu, "show_banner", lambda: None)
        monkeypatch.setattr(menu, "stop_trading", fake_stop)
        try:
            menu.main_menu()
        except self._Done:
            pass
        return seen

    def _menu(self, **kw):
        import types
        menu = make_menu(**kw)
        menu.config = types.SimpleNamespace(symbols={})
        return menu

    def test_orphan_state_offers_stop(self, monkeypatch):
        """紅在：valid_choices 只在 `_trading_active` 為真時才 append "s"，
        而孤兒狀態下它正好是 False ⇒ 使用者按不到停止，bot 停不掉。"""
        menu = self._menu(bot=FakeBot(), thread=FakeThread(alive=True),
                          trading_active=False)
        seen = self._drive_once(menu, monkeypatch, choice="s")
        assert "s" in seen["choices"]
        assert seen.get("stopped") is True

    def test_exit_stops_orphan_before_breaking(self, monkeypatch):
        """紅在：choice "0" 的守衛若只看 _trading_active，孤兒狀態下直接 break，
        bot 沒被 stop 就結束（掛單與 listenKey 都不收）。"""
        menu = self._menu(bot=FakeBot(), thread=FakeThread(alive=True),
                          trading_active=False)
        seen = self._drive_once(menu, monkeypatch, choice="0")
        assert seen.get("stopped") is True

    def test_orphan_state_is_visible_on_the_banner(self, monkeypatch):
        """孤兒狀態必須看得見 —— 使用者不會去按一個他不知道存在的停止鍵。

        紅在：拿掉 main_menu 開頭的 `if self.bot and not self._trading_active`
        那段橫幅（選單看起來就像沒在交易，但 bot 還在真錢上下單）。
        """
        printed = []
        monkeypatch.setattr(tm.console, "print",
                            lambda *a, **k: printed.append(" ".join(str(x) for x in a)))
        menu = self._menu(bot=FakeBot(), thread=FakeThread(alive=True),
                          trading_active=False)
        self._drive_once(menu, monkeypatch, choice="0")
        blob = "\n".join(printed)
        # 用橫幅獨有的字串斷言：選單選項那行也含「bot 仍在運行」，
        # 拿它當斷言會被滿足，等於沒測到橫幅（M11 存活的原因）。
        assert "啟動未確認" in blob

    def test_idle_menu_shows_no_orphan_banner(self, monkeypatch):
        printed = []
        monkeypatch.setattr(tm.console, "print",
                            lambda *a, **k: printed.append(" ".join(str(x) for x in a)))
        menu = self._menu()
        self._drive_once(menu, monkeypatch, choice="0")
        assert "啟動未確認" not in "\n".join(printed)

    def test_idle_menu_has_no_stop_option(self, monkeypatch):
        menu = self._menu()
        seen = self._drive_once(menu, monkeypatch, choice="0")
        assert "s" not in seen["choices"]
        assert seen.get("stopped") is None


class TestLifecycleMonkey:
    """極端測試 —— 故意把生命週期玩壞（專案規則：unit/integration 後必做）。"""

    def test_thread_flips_to_dead_between_join_and_check(self):
        """thread 在 join() 當下還活著、檢查的瞬間死掉（真實 race）：
        必須放手，不能留下永遠停不掉的假孤兒。"""
        class FlakyThread(FakeThread):
            def join(self, timeout=None):
                self.join_calls += 1
                self._alive = False      # join 回來就死了

        thread = FlakyThread(alive=True)
        menu = make_menu(bot=FakeBot(), thread=thread, loop=FakeLoop(False),
                         trading_active=True)
        menu.stop_trading()
        assert menu.bot is None and menu.bot_thread is None

    def test_thread_resurrects_is_alive_never_releases(self):
        """反過來：is_alive() 一直回 True（bot 真的卡死）。
        重複按停止不得放手、不得炸、不得讓 start_trading 過關。"""
        bot = FakeBot()
        thread = FakeThread(alive=True)
        menu = make_menu(bot=bot, thread=thread, loop=FakeLoop(False),
                         trading_active=True)
        for _ in range(20):
            menu.stop_trading()
        assert menu.bot is bot
        assert menu._bot_alive() is True

    def test_all_none_state_is_safe(self):
        menu = make_menu()
        menu.stop_trading()
        assert menu._release_bot_if_dead() is True
        assert menu._bot_alive() is False

    def test_bot_set_but_thread_none(self):
        """bot 建好了但 thread 還沒起（start_trading 中途被打斷）。"""
        menu = make_menu(bot=FakeBot(), thread=None, loop=None, trading_active=False)
        menu.stop_trading()
        assert menu.bot is None, "沒有 thread ⇒ 沒有孤兒，該放手"

    def test_thread_alive_but_bot_reference_lost(self):
        """歷史遺留狀態（舊版 code 產生的孤兒）：bot 是 None 但 thread 還活著。
        stop_trading 不得當成「沒有運行中的交易」直接 return。"""
        thread = FakeThread(alive=True)
        menu = make_menu(bot=None, thread=thread, loop=FakeLoop(False),
                         trading_active=False)
        menu.stop_trading()
        assert thread.join_calls == 1, "還活著的 thread 必須被 join 嘗試"

    def test_loop_not_running_does_not_call_stop(self):
        """bot_loop 已停 ⇒ 不得 run_coroutine_threadsafe（會拋 RuntimeError）。"""
        bot = FakeBot()
        menu = make_menu(bot=bot, thread=FakeThread(alive=False),
                         loop=FakeLoop(running=False), trading_active=True)
        menu.stop_trading()
        assert bot.stop_called == 0


class TestPushConfigToBot:
    """設定「即時套用」不得綁 _trading_active。

    孤兒狀態（啟動逾時／停止未完成 ⇒ `_trading_active` 為 False 但 bot 還在跑）下，
    舊守衛 `if self._trading_active and self.bot:` 會**靜默跳過**：使用者看到
    「已保存」但沒有「已即時套用」，而那個 bot 仍拿舊 config 在真錢帳戶上下單。
    """

    def test_pushes_when_bot_exists_even_if_trading_active_false(self):
        """紅在：守衛改回 `self._trading_active and self.bot`。"""
        bot = FakeBot()
        menu = make_menu(bot=bot, thread=FakeThread(alive=True), trading_active=False)
        menu.config = {"marker": 1}

        assert menu._push_config_to_bot() is True
        assert bot.config == {"marker": 1}

    def test_no_bot_means_no_push(self):
        menu = make_menu()
        menu.config = {"marker": 1}
        assert menu._push_config_to_bot() is False

    def test_normal_running_path_still_pushes(self):
        bot = FakeBot()
        menu = make_menu(bot=bot, thread=FakeThread(alive=True), trading_active=True)
        menu.config = {"marker": 2}
        assert menu._push_config_to_bot() is True
        assert bot.config == {"marker": 2}

    def test_no_settings_screen_pushes_config_with_its_own_guard(self):
        """結構性斷言（刻意的）：`self.bot.config = self.config` 只准出現在
        `_push_config_to_bot()` 裡一次。

        六個「即時套用」呼叫點各自跑一次互動式設定畫面成本太高，改用來源檢查
        擋回歸——新增設定畫面時照抄舊 pattern（自己帶 `_trading_active` 守衛）
        會在這裡紅。
        """
        import inspect
        src = inspect.getsource(tm)
        assert src.count("self.bot.config = self.config") == 1
        assert src.count("_push_config_to_bot()") >= 6


class TestOtherScreensSeeOrphan:
    """其餘畫面的「運行中」提示同樣不得綁 _trading_active。

    verifier 2026-08-24 R2 的 mutation #4 存活點：manage_symbols 的橫幅原本零覆蓋。
    """

    def _capture(self, monkeypatch):
        printed = []
        monkeypatch.setattr(tm.console, "print",
                            lambda *a, **k: printed.append(" ".join(str(x) for x in a)))
        return printed

    def _menu(self, **kw):
        import types
        menu = make_menu(**kw)
        menu.config = types.SimpleNamespace(symbols={}, save=lambda: None)
        return menu

    def _drive_manage_symbols(self, menu, monkeypatch):
        monkeypatch.setattr(menu, "show_banner", lambda: None)
        monkeypatch.setattr(tm.Prompt, "ask", lambda *a, **k: "0")
        menu.manage_symbols()

    def test_manage_symbols_shows_running_hint_for_orphan(self, monkeypatch):
        """紅在：守衛改回 `if self._trading_active:` —— 孤兒狀態下使用者以為
        沒在交易，改了參數卻其實會即時套用到還在跑的 bot。"""
        printed = self._capture(monkeypatch)
        menu = self._menu(bot=FakeBot(), thread=FakeThread(alive=True),
                          trading_active=False)
        self._drive_manage_symbols(menu, monkeypatch)
        assert any("修改參數會即時套用" in line for line in printed)

    def test_manage_symbols_hides_hint_when_idle(self, monkeypatch):
        printed = self._capture(monkeypatch)
        menu = self._menu()
        self._drive_manage_symbols(menu, monkeypatch)
        assert not any("修改參數會即時套用" in line for line in printed)

    def test_toggle_symbol_warns_about_restart_for_orphan(self, monkeypatch):
        """紅在：守衛改回 `if self._trading_active:`。孤兒 bot 一樣跑著舊的
        啟用/停用狀態，一樣需要重啟才生效。"""
        import types

        printed = self._capture(monkeypatch)
        monkeypatch.setattr(tm.Prompt, "ask", lambda *a, **k: "")
        cfg = types.SimpleNamespace(enabled=True)
        menu = self._menu(bot=FakeBot(), thread=FakeThread(alive=True),
                          trading_active=False)
        menu.config.symbols = {"BNB/USDC:USDC": cfg}
        monkeypatch.setattr(tm.IntPrompt, "ask", lambda *a, **k: 1)
        menu.toggle_symbol()
        assert cfg.enabled is False              # 真的切了，不是走 early return
        assert any("需要重啟交易才能生效" in line for line in printed)

    def test_toggle_symbol_no_restart_warning_when_no_bot(self, monkeypatch):
        import types

        printed = self._capture(monkeypatch)
        monkeypatch.setattr(tm.Prompt, "ask", lambda *a, **k: "")
        monkeypatch.setattr(tm.IntPrompt, "ask", lambda *a, **k: 1)
        menu = self._menu()
        menu.config.symbols = {"BNB/USDC:USDC": types.SimpleNamespace(enabled=True)}
        menu.toggle_symbol()
        assert not any("需要重啟交易才能生效" in line for line in printed)


# ---------------------------------------------------------------------------
# 「thread 已死就別再等」的量測式契約
# ---------------------------------------------------------------------------

import types  # noqa: E402

# 只 import 純 helper，**不 import 對面的 autouse fixture**：`_no_real_sleep`
# 一旦被帶進本模組的命名空間就會對整個檔案生效，讓每條測試都跑在「全域
# time.sleep 被換掉」的狀態下 —— 本檔不需要那個，而且它會讓未來任何真的需要
# sleep 的測試靜默失真。measure_hedge_guard 自己會用 monkeypatch 接管守衛的
# sleep，不依賴那個 fixture。
from tests.test_hedge_mode_guard import (  # noqa: E402
    TUI_START_BUDGET_SEC, HEDGE_GUARD_PATHS, VirtualClock, measure_hedge_guard,
)

# 用整句、不用「Bot 已結束」這種放寬過的前綴：前綴匹配等於允許後半句被改成
# 任何東西（包含被改回「交易未啟動」那句假話）。
FAILED_MARK = "Bot 已結束（背景 thread 不在運行）"
STILL_INIT_MARK = "Bot 仍在初始化中"
STARTED_MARK = "交易已在背景啟動"
# 這條分支獨有的字面值：thread 已死不代表交易所上乾淨。
OPEN_ORDERS_WARNING = "交易所上可能已經有掛單/持倉"


class ScheduledState:
    """`running` 在虛擬時間 `running_at` 之後才變 True（None = 永遠不會）。"""

    def __init__(self, clock, running_at):
        self._clock = clock
        self._running_at = running_at

    @property
    def running(self):
        return self._running_at is not None and self._clock.t >= self._running_at


class ScheduledBot:
    def __init__(self, clock, running_at):
        self.state = ScheduledState(clock, running_at)

    async def stop(self):
        pass


class ScheduledThread:
    """在虛擬時間 `dies_at` 之後 `is_alive()` 變 False（None = 一直活著）。

    刻意不開真的 thread：整條時間軸由 TUI 自己的 `time.sleep` 推進，
    所以測試是決定性的，且 mutation 的結果一定是「量到錯的數字」而不是 hang
    ——`for _ in range(100)` 的上界與被測條件無關，迴圈一定會結束。
    """

    def __init__(self, clock, dies_at):
        self._clock = clock
        self._dies_at = dies_at

    def start(self):
        pass

    def is_alive(self):
        return self._dies_at is None or self._clock.t < self._dies_at

    def join(self, timeout=None):
        pass


def _run_start_trading(monkeypatch, clock, running_at=None, dies_at=None):
    """實際跑 `start_trading`，回 `(定案時的虛擬秒數, 印出來的全部文字)`。"""
    printed = []
    bot_box = {}

    def fake_bot_factory(config):
        bot_box["bot"] = ScheduledBot(clock, running_at)
        return bot_box["bot"]

    def fake_thread(target=None, daemon=None):
        return ScheduledThread(clock, dies_at)

    monkeypatch.setattr(tm, "MaxGridBot", fake_bot_factory)
    monkeypatch.setattr(tm.threading, "Thread", fake_thread)
    monkeypatch.setattr(tm.time, "sleep", clock.sleep)
    monkeypatch.setattr(tm.console, "print",
                        lambda *a, **k: printed.append(str(a[0]) if a else ""))

    sym = types.SimpleNamespace(enabled=True)
    menu = make_menu()
    menu.config = types.SimpleNamespace(api_key="k",
                                        symbols={"BNB/USDC:USDC": sym})
    menu.start_trading()
    return clock.t, "\n".join(printed), menu


class TestStartTradingDetectsDeadThread:
    """TUI 的等待必須在 thread 死掉的那一刻停，而不是空轉滿預算。

    `bot.run()` 的初始化段是硬失敗設計：任一步 raise ⇒ notify_crash +
    gateway.shutdown() + return ⇒ thread 乾淨結束、`state.running` 永遠是
    False。只看 `running` 的舊碼會在原地等滿 20 秒才印「初始化較慢」。
    """

    def test_fast_death_is_reported_at_the_moment_it_dies(self, monkeypatch):
        """紅在：第一段迴圈沒有 is_alive() 偵測 ⇒ 量到的是 20.0 不是 2.0。"""
        elapsed, out, menu = _run_start_trading(
            monkeypatch, VirtualClock(), dies_at=2.0)

        assert elapsed == pytest.approx(2.0, abs=0.15), \
            f"thread 2 秒就死了，TUI 卻等了 {elapsed:.1f}s"
        assert FAILED_MARK in out
        assert STILL_INIT_MARK not in out, "thread 已死時不得說它還在初始化"
        assert menu.bot is None and menu._trading_active is False

    def test_the_failure_message_never_claims_the_exchange_is_clean(self, monkeypatch):
        """thread 已死 ≠ 交易所上沒東西。

        有一個窄窗口：bot 已經把 running 設成 True、甚至已經掛了單，然後才崩潰
        結束 thread —— 0.1s 取樣的輪詢在兩次取樣之間就會錯過那個 True，於是走進
        這條「已結束」分支。此時說「交易未啟動」是最貴的一種假話：使用者會以為
        交易所上乾乾淨淨而不去查。

        紅在：刪掉那行提醒、或把文案改回宣稱「交易未啟動」。
        """
        _, out, _ = _run_start_trading(monkeypatch, VirtualClock(), dies_at=2.0)

        assert OPEN_ORDERS_WARNING in out, \
            "失敗文案必須提醒去確認交易所上的掛單/持倉"
        assert "交易未啟動" not in out, \
            "TUI 拿不到 bot 是否掛過單，不得斷言「交易未啟動」"

    def test_death_inside_the_second_loop_is_reported_at_that_moment(self, monkeypatch):
        """紅在：只在第一段迴圈加偵測、第二段沒加 ⇒ 量到 20.0 不是 14.0。"""
        elapsed, out, menu = _run_start_trading(
            monkeypatch, VirtualClock(), dies_at=14.0)

        assert elapsed == pytest.approx(14.0, abs=0.15), \
            f"thread 在第 14 秒死掉，TUI 卻等到 {elapsed:.1f}s"
        assert FAILED_MARK in out
        assert menu.bot is None

    def test_a_thread_still_alive_at_the_budget_keeps_the_honest_message(self, monkeypatch):
        """還活著就是還活著：等滿預算後說「仍在初始化」是實話，參照要留著。

        紅在：預算被改大（量到 25.0）、兩段迴圈被刪（量到 0.0）。
        """
        elapsed, out, menu = _run_start_trading(
            monkeypatch, VirtualClock(), dies_at=25.0)

        assert elapsed == pytest.approx(TUI_START_BUDGET_SEC, abs=0.15), \
            f"TUI 的等待預算是 20 秒，實際等了 {elapsed:.1f}s"
        assert STILL_INIT_MARK in out
        assert FAILED_MARK not in out, "thread 還活著時不得宣告失敗"
        assert OPEN_ORDERS_WARNING not in out, \
            "還活著時不該叫人去查掛單——那是「已結束」分支專屬的提醒"
        assert menu.bot is not None, "還活著的 bot 不得被放手（會變成停不掉的孤兒）"

    def test_success_inside_the_first_loop(self, monkeypatch):
        elapsed, out, menu = _run_start_trading(
            monkeypatch, VirtualClock(), running_at=5.0)

        assert elapsed == pytest.approx(5.0, abs=0.15)
        assert STARTED_MARK in out
        assert menu._trading_active is True

    def test_success_inside_the_second_loop(self, monkeypatch):
        """紅在：第二段輪詢迴圈被整段刪掉 ⇒ 12 秒才就緒的 bot 會被當成沒啟動。"""
        elapsed, out, menu = _run_start_trading(
            monkeypatch, VirtualClock(), running_at=12.0)

        assert elapsed == pytest.approx(12.0, abs=0.15), \
            f"bot 在第 12 秒就緒，TUI 卻在 {elapsed:.1f}s 才定案"
        assert STARTED_MARK in out
        assert menu._trading_active is True


class TestStartupFailureIsVisibleQuickly:
    """端到端（量測 → 量測）：守衛硬失敗的實際耗時，餵給 TUI 的實際等待。

    兩段都是真的跑出來的：前半跑 `MaxGridBot._check_hedge_mode`（真實守衛 +
    虛擬時鐘的 fake exchange），量到它 raise 的時間；後半把那個時間當成 bot
    thread 的死亡時刻，跑真實的 `start_trading`，量使用者看到定案訊息的時間。
    """

    def test_common_hard_failure_surfaces_at_the_guard_failure_time(self, monkeypatch):
        guard_sec, raised = measure_hedge_guard(
            monkeypatch, **HEDGE_GUARD_PATHS["單向→切換被拒→複驗全False"])
        assert isinstance(raised, RuntimeError), "這條路徑必須是硬失敗"

        elapsed, out, menu = _run_start_trading(
            monkeypatch, VirtualClock(), dies_at=guard_sec)

        assert elapsed == pytest.approx(guard_sec, abs=0.15), (
            f"守衛在 {guard_sec:.2f}s 就 raise，使用者卻等到 {elapsed:.1f}s")
        assert elapsed < TUI_START_BUDGET_SEC / 2, \
            "最常見的硬失敗必須遠早於 20 秒預算就顯示"
        assert FAILED_MARK in out
        assert menu.bot is None
