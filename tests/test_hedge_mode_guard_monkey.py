"""持倉模式守衛的 Monkey Testing（專案規則 `python-quant.md`：unit/integration 之後
一定要做極端測試，想辦法把程式玩壞）。

`test_hedge_mode_guard.py` 走的是正常路徑與設計中的守衛路徑。這一檔專門餵它
**不該出現的東西**：交易所回畸形資料、WS 事件缺欄位或型別亂七八糟、高頻洗版、
時鐘倒退、併發。每條測試的 docstring 要說清楚它釘的是「真缺陷已修」還是
「這是可接受的降級」。

本輪抓到的真缺陷（已修）：
  `hedged` 是無法判讀的 truthy 值（例如字串 `'true'`）時，舊碼只擋 `None`，
  於是這種值會掉進「偵測到單向持倉模式 ⇒ 送 POST 切換」分支 ——
  拿一個讀不懂的值去改使用者的**帳戶層**設定。現在收斂成
  「只有明確的 False 才切換，其餘一律 raise 且不送 POST」。
"""
import asyncio

import ccxt
import pytest
from unittest.mock import MagicMock

from grid_engine import clock

# helper / fixture 的單一出處是 tests/test_hedge_mode_guard.py；這裡直接 import。
# pytest 會把 import 進本模組命名空間的 fixture 一併註冊，_no_real_sleep 的
# autouse 也照樣生效。（先前兩檔各有一份逐字重複的副本並已開始分歧。）
from tests.test_hedge_mode_guard import (  # noqa: F401  (fixture 靠 import 註冊)
    SYMBOL, SYMBOL2, _filled_event, _make_bot, _snapshot,
    _no_real_sleep, fake_guard_clock, order_bot,
)


def _exchange_returning(value, switch_error=None, switch_return=None):
    """fetch_position_mode 固定回傳 `value`（整包 dict，不是 hedged 欄位）。"""
    ex = MagicMock()
    ex.fetch_position_mode.return_value = value
    if switch_error is not None:
        ex.fapiPrivatePostPositionSideDual.side_effect = switch_error
    elif switch_return is not None:
        ex.fapiPrivatePostPositionSideDual.return_value = switch_return
    return ex


# --------------------------------------------------------------------------
# 1. 交易所回畸形資料
# --------------------------------------------------------------------------

class TestMalformedPositionModeResponse:
    @pytest.mark.parametrize("hedged", [
        'true',                      # 字串 truthy —— 就是這條抓到真缺陷
        'false',
        1,
        0,
        {'nested': None},
        [],
        'ㄏㄜˊ' * 3,                  # 非 ASCII
        'x' * 100_000,               # 超大字串
    ], ids=["str-true", "str-false", "int-1", "int-0", "nested-dict",
            "empty-list", "non-ascii", "huge-str"])
    def test_uninterpretable_hedged_aborts_without_touching_the_account(self, hedged):
        """**真缺陷（已修）**：只有明確的 `False` 才代表「確定是單向、該切換」。

        舊碼只擋 `hedged is None`，所以任何無法判讀的值都會掉進切換分支，
        拿讀不懂的值去 POST 改帳戶層的 dualSidePosition —— 那是本 bot 唯一會
        改帳戶狀態的呼叫，也會波及同帳戶的其他倉位/機器人。

        釘住兩件事：raise（確認不了就不啟動），且 **POST 一次都不准送**。
        """
        bot = _make_bot()
        bot.exchange = _exchange_returning({"info": {}, "hedged": hedged})
        with pytest.raises(RuntimeError, match="未回報持倉模式"):
            bot._check_hedge_mode()
        bot.exchange.fapiPrivatePostPositionSideDual.assert_not_called()

    @pytest.mark.parametrize("payload", [{}, {"info": {}}, {"hedged": None}],
                             ids=["empty-dict", "info-only", "explicit-none"])
    def test_missing_hedged_field_aborts_without_touching_the_account(self, payload):
        """欄位整個缺席（ccxt safe_bool 回 None）同樣是「未知」，不得切換。"""
        bot = _make_bot()
        bot.exchange = _exchange_returning(payload)
        with pytest.raises(RuntimeError, match="未回報持倉模式"):
            bot._check_hedge_mode()
        bot.exchange.fapiPrivatePostPositionSideDual.assert_not_called()

    def test_baseexception_from_fetch_is_not_swallowed(self):
        """KeyboardInterrupt / SystemExit **不該**被守衛吞成「查詢失敗」。

        它們是「使用者/系統要求停止」，不是交易所故障；轉譯成 RuntimeError 會讓
        Ctrl-C 變成一封「查詢持倉模式失敗」的假故障通知。`except Exception`
        不攔 BaseException，這條把該性質釘住。
        """
        bot = _make_bot()
        ex = MagicMock()
        ex.fetch_position_mode.side_effect = KeyboardInterrupt()
        bot.exchange = ex
        with pytest.raises(KeyboardInterrupt):
            bot._check_hedge_mode()
        ex.fapiPrivatePostPositionSideDual.assert_not_called()

    def test_baseexception_from_switch_is_not_swallowed(self):
        """切換呼叫的 BaseException 同理：F2 的「錯誤先存起來、交給複驗裁決」
        只適用於 Exception，不得把 Ctrl-C 也吃掉變成繼續複驗 3 秒。"""
        bot = _make_bot()
        bot.exchange = _exchange_returning({"hedged": False},
                                          switch_error=KeyboardInterrupt())
        with pytest.raises(KeyboardInterrupt):
            bot._check_hedge_mode()

    def test_garbage_switch_return_value_is_ignored_verification_decides(self):
        """切換呼叫的**回傳值**一概不採信（回 None、回字串、回 dict 都一樣），
        裁決權只在複驗查詢。這是可接受且刻意的降級：以最終狀態為準嚴格優於
        以呼叫回傳為準。"""
        bot = _make_bot()
        ex = MagicMock()
        ex.fetch_position_mode.side_effect = [
            {"hedged": False}, {"hedged": True}]
        ex.fapiPrivatePostPositionSideDual.return_value = "🙃 not a dict"
        bot.exchange = ex
        bot._check_hedge_mode()  # 不得 raise
        assert ex.fetch_position_mode.call_count == 2


# --------------------------------------------------------------------------
# 2. WS 事件畸形
# --------------------------------------------------------------------------

class TestMalformedOrderEvents:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("data", [
        None, [], "not a dict", 42, {"o": None}, {"o": []}, {},
    ], ids=["none", "list", "str", "int", "o-none", "o-list", "no-o"])
    async def test_garbage_envelope_never_mutates_state_nor_escapes(self, order_bot, data):
        """整包 data / `o` 是垃圾：handler 的外層 except 吞掉並 log，**不得**讓
        例外冒到 WS 迴圈（會炸掉 userData 連線），也不得改到任何狀態。
        這是可接受的降級：畸形事件本來就沒有可套用的內容。"""
        before = _snapshot(order_bot.state.symbols[SYMBOL])
        await order_bot._handle_order_update(data)  # 不得 raise
        assert _snapshot(order_bot.state.symbols[SYMBOL]) == before
        order_bot.adjust_grid.assert_not_called()
        order_bot.bandit_optimizer.record_trade.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ps", [
        None, 0, 1, -1, 3.14, {}, [], (), True, False,
        "long", "Long", "LONG ", " LONG", "BOTH", "𝕃𝕆ℕ𝔾", "ｌｏｎｇ",
        "LONG\x00", "LONG" * 50_000,
    ], ids=lambda v: repr(v)[:24])
    async def test_hostile_position_side_values_all_take_the_early_return(self, order_bot, ps):
        """positionSide 的任何非精確 `'LONG'`/`'SHORT'` 值都必須早退。

        特別包含 `'long'`、`'Long'`、`'LONG '`（大小寫/空白）與 `True`/`1`：
        這些「看起來很像」的值若被放行，會被記到錯的一側。守衛用的是精確比對，
        不是 upper()/strip()，這條把該性質釘死 —— 想放寬得先改測試。
        """
        st = order_bot.state.symbols[SYMBOL]
        before = _snapshot(st)
        await order_bot._handle_order_update(_filled_event(ps))
        assert _snapshot(st) == before, "早退必須發生在 ws_seq 遞增與計數重置之前"
        order_bot.bandit_optimizer.record_trade.assert_not_called()
        order_bot.adjust_grid.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("rp", ["NaN", "inf", "-inf", "1e400", "-0", "0"],
                             ids=["nan", "inf", "-inf", "overflow", "neg-zero", "zero"])
    async def test_pathological_realized_pnl_does_not_defeat_the_guard(self, order_bot, rp):
        """`rp` 在守衛**之前**就被 float() 掉。餵 NaN/inf 想繞過早退是不行的：
        早退只看 positionSide，與 rp 無關。釘住「rp 不是守衛的旁路」。"""
        st = order_bot.state.symbols[SYMBOL]
        before = _snapshot(st)
        await order_bot._handle_order_update(_filled_event("BOTH", realized_pnl=rp))
        assert _snapshot(st) == before
        order_bot.bandit_optimizer.record_trade.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("rp", ["abc", "", "0x10", None], ids=["abc", "empty", "hex", "none"])
    async def test_non_numeric_realized_pnl_drops_the_whole_event(self, order_bot, rp):
        """**可接受的降級（既有行為，非本 branch 引入）**：`float(rp)` 在守衛之前，
        非數值會拋 ValueError 被 handler 外層 except 接住 ⇒ 整筆事件被丟棄。

        為什麼可接受：丟棄 = 零狀態寫入，下一輪 REST 同步會把計數拉回交易所真值；
        比「猜一個 pnl 記進 bandit」安全。注意 `""`/`None` 會被 `or 0` 救成 0.0，
        所以它們是正常套用的路徑 —— 這裡一併釘住兩種結局的分界。
        """
        st = order_bot.state.symbols[SYMBOL]
        before = _snapshot(st)
        await order_bot._handle_order_update(
            _filled_event("LONG", realized_pnl=rp))  # 不得 raise
        if rp in ("", None):
            assert _snapshot(st) != before, "空值被 `or 0` 救成 0.0，屬正常套用路徑"
        else:
            assert _snapshot(st) == before, "解析失敗必須零寫入"
            order_bot.bandit_optimizer.record_trade.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_symbol_returns_before_any_lookup(self, order_bot):
        """`s` 是設定裡沒有的 symbol ⇒ 在守衛之前就 return，不得 KeyError。"""
        before = _snapshot(order_bot.state.symbols[SYMBOL])
        await order_bot._handle_order_update(_filled_event("BOTH", symbol="DOGEUSDT"))
        await order_bot._handle_order_update(_filled_event("LONG", symbol=""))
        assert _snapshot(order_bot.state.symbols[SYMBOL]) == before


# --------------------------------------------------------------------------
# 3. 高頻 / 時鐘 / 併發
# --------------------------------------------------------------------------

class TestThrottleAndConcurrencyUnderStress:
    @pytest.mark.asyncio
    async def test_thousand_events_log_once_but_none_is_applied(self, order_bot, caplog):
        """同一 symbol 連發 1000 筆 BOTH：log 只准一條（不洗版），但 1000 筆
        **每一筆**都必須早退 —— 節流一旦漏進行為層，第 2 筆之後就會靜默套用。"""
        st = order_bot.state.symbols[SYMBOL]
        before = _snapshot(st)
        with caplog.at_level("WARNING"):
            for _ in range(1000):
                await order_bot._handle_order_update(_filled_event("BOTH"))

        hits = [r for r in caplog.records if "非 LONG/SHORT" in r.getMessage()]
        assert len(hits) == 1
        assert _snapshot(st) == before
        order_bot.adjust_grid.assert_not_called()
        # 節流表不得隨事件數增長（無界成長 = 記憶體洩漏）
        assert len(order_bot._last_unknown_ps_log_at) == 1

    @pytest.mark.asyncio
    async def test_two_symbols_interleaved_do_not_silence_each_other(self, order_bot, caplog):
        """多 symbol 交錯：節流以 symbol 為 key，A 的告警不得壓住 B 的第一則。
        兩則訊息各自指名自己的交易所原始 symbol。"""
        with caplog.at_level("WARNING"):
            for _ in range(20):
                await order_bot._handle_order_update(_filled_event("BOTH", symbol="XRPUSDC"))
                await order_bot._handle_order_update(_filled_event("BOTH", symbol="BNBUSDC"))

        msgs = [r.getMessage() for r in caplog.records if "非 LONG/SHORT" in r.getMessage()]
        assert len(msgs) == 2
        assert any("[userData] XRPUSDC positionSide='BOTH' 非 LONG/SHORT" in m for m in msgs)
        assert any("[userData] BNBUSDC positionSide='BOTH' 非 LONG/SHORT" in m for m in msgs)
        assert _snapshot(order_bot.state.symbols[SYMBOL2]) == (3, 4, 5, 6, 7)

    @pytest.mark.asyncio
    async def test_clock_going_backwards_fails_open_never_silent(self, order_bot, caplog):
        """**真缺陷（已修）**：守衛時鐘倒退（NTP 校正、休眠喚醒）會讓告警靜音。

        舊條件只有 `now - last < 3600`：倒退讓差值變負 ⇒ 恆為真 ⇒ 從倒退那刻起
        靜音「倒退量 + 一個窗口」（本例是一天又一小時）。這條告警是「帳戶持倉
        模式被外部改掉」的唯一運行期通知，靜音一天等於沒有。
        （security review 記的是「倒退只會多印（fail open）」—— 方向剛好相反，
        沒有人實測過；這是 monkey testing 的產出。）

        修法：條件改成 `0.0 <= delta < 窗口`，倒退直接放行並重設錨點 ⇒ 真的
        fail open。第三筆（時鐘跳回未來且已過窗口）也必須再印一次。
        """
        t = {"now": 1_000_000.0}
        clock.set_guard_clock(lambda: t["now"])
        with caplog.at_level("WARNING"):
            await order_bot._handle_order_update(_filled_event("BOTH"))
            t["now"] -= 86_400.0           # 時鐘倒退一天
            await order_bot._handle_order_update(_filled_event("BOTH"))
            t["now"] += 86_400.0 + 3601.0  # 再跳回未來
            await order_bot._handle_order_update(_filled_event("BOTH"))

        hits = [r for r in caplog.records if "非 LONG/SHORT" in r.getMessage()]
        assert len(hits) == 3, "倒退必須 fail open（多印），不得永久靜音"
        assert _snapshot(order_bot.state.symbols[SYMBOL]) == (3, 4, 5, 6, 7)

    @pytest.mark.asyncio
    async def test_concurrent_handlers_never_leak_a_single_mutation(self, order_bot, caplog):
        """200 筆 BOTH 併發（gather）+ 混入 20 筆未知值：早退路徑對 sym_state
        零寫入，所以無論交錯順序如何，狀態都必須逐位元不變。
        任何一筆漏進去都會讓 ws_seq 或某個計數位移。"""
        st = order_bot.state.symbols[SYMBOL]
        before = _snapshot(st)
        events = ([_filled_event("BOTH") for _ in range(200)]
                  + [_filled_event("SIDEWAYS") for _ in range(20)])
        with caplog.at_level("WARNING"):
            await asyncio.gather(*(order_bot._handle_order_update(e) for e in events))

        assert _snapshot(st) == before
        order_bot.adjust_grid.assert_not_called()
        order_bot.bandit_optimizer.record_trade.assert_not_called()
        hits = [r for r in caplog.records if "非 LONG/SHORT" in r.getMessage()]
        assert len(hits) == 1, "併發下節流仍不得洗版"

    @pytest.mark.asyncio
    async def test_guard_does_not_leak_across_to_the_valid_path_under_stress(self, order_bot):
        """壓力測試不得把正常路徑也一起弄壞：1000 筆 BOTH 之後，一筆合法的
        LONG 仍必須被完整套用（重置該格計數 + 遞增 ws_seq + 餵 bandit）。
        沒有這條，「守衛把所有東西都擋掉」也會全綠。"""
        st = order_bot.state.symbols[SYMBOL]
        for _ in range(1000):
            await order_bot._handle_order_update(_filled_event("BOTH"))
        await order_bot._handle_order_update(_filled_event("LONG", side="BUY"))

        assert st.buy_long_orders == 0
        assert st.sell_long_orders == 4
        assert st.ws_seq == 8
        order_bot.bandit_optimizer.record_trade.assert_called_once_with(1.5, 'long')
        order_bot.adjust_grid.assert_awaited_once_with(SYMBOL)
