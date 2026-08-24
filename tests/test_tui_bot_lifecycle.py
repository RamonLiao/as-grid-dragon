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
