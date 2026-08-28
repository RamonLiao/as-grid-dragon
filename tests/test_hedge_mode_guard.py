"""持倉模式（position mode）守衛測試。

兩道守衛守同一個前提：**這隻 bot 只能在 hedge（雙向持倉）模式下運作**。
order_executor.place_order 對每一張網格單都帶 positionSide（order_executor.py:90-91），
而 position mode 是幣安帳戶層設定 —— one-way 模式下這些單會被整批拒絕，
bot 一張單都下不出去，只會一路撞下單斷路器。

  守衛 1（啟動）：_check_hedge_mode 確立不了 hedge 就 raise，由 run() 內那個
                  except Exception 區塊（送 notify_crash(f"初始化失敗: {e}")
                  的那一段）接成乾淨返回，不啟動。
  守衛 2（運行期）：_handle_order_update 對 ps 非 LONG/SHORT 的成交事件早退，
                  不重置掛單計數、不餵 bandit、不重掛網格。
"""
import asyncio

import ccxt
import pytest
from unittest.mock import AsyncMock, MagicMock

from grid_engine import clock
from grid_engine.bot import MaxGridBot
from grid_engine.config import GlobalConfig, SymbolConfig

SYMBOL = "XRP/USDC:USDC"
SYMBOL2 = "BNB/USDC:USDC"

# 本檔是這組 helper/fixture 的單一出處；tests/test_hedge_mode_guard_monkey.py
# 直接 import 它們（曾經兩邊各有一份逐字重複的副本，而且已經開始分歧）。


def _sym(raw, ccxt_symbol, enabled=True):
    return SymbolConfig(
        symbol=raw, ccxt_symbol=ccxt_symbol, enabled=enabled,
        take_profit_spacing=0.003, grid_spacing=0.003, initial_quantity=0.02,
        limit_multiplier=5.0, threshold_multiplier=20.0,
    )


def _make_bot(enabled=True, extra_symbol=False):
    cfg = GlobalConfig()
    cfg.symbols = {SYMBOL: _sym("XRPUSDC", SYMBOL, enabled)}
    if extra_symbol:
        cfg.symbols[SYMBOL2] = _sym("BNBUSDC", SYMBOL2, enabled)
    cfg.bandit.enabled = False
    bot = MaxGridBot(cfg)
    bot.order_executor.place_order = AsyncMock()
    return bot


def _snapshot(st):
    """四個掛單計數 + ws_seq。早退路徑對 sym_state 零寫入 ⇒ 必須逐位元不變。"""
    return (st.buy_long_orders, st.sell_long_orders,
            st.buy_short_orders, st.sell_short_orders, st.ws_seq)


def _exchange(mode_results, switch_error=None):
    """mode_results: fetch_position_mode 依序回傳的 hedged 值（True/False/None），
    或 Exception 實例（該次呼叫拋出）。清單耗盡後重複最後一項。"""
    ex = MagicMock()
    seq = list(mode_results)

    def _fetch(symbol=None, **kw):
        item = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(item, Exception):
            raise item
        return {"info": {}, "hedged": item}

    ex.fetch_position_mode.side_effect = _fetch
    if switch_error is not None:
        ex.fapiPrivatePostPositionSideDual.side_effect = switch_error
    return ex


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """複驗間隔不要真的睡 —— 但要留下呼叫紀錄，證明間隔存在。"""
    calls = []
    monkeypatch.setattr("grid_engine.bot.time.sleep", lambda s: calls.append(s))
    yield calls
    clock.reset_clock()
    clock.reset_guard_clock()


class TestCheckHedgeMode:
    def test_already_hedged_passes_without_switching(self):
        bot = _make_bot()
        bot.exchange = _exchange([True])
        bot._check_hedge_mode()
        bot.exchange.fapiPrivatePostPositionSideDual.assert_not_called()

    def test_fetch_failure_aborts_startup(self):
        """查不到就不啟動（使用者 2026-08-28 裁決：不寬容「查不到」）。"""
        bot = _make_bot()
        bot.exchange = _exchange([RuntimeError("network down")])
        with pytest.raises(RuntimeError, match="查詢持倉模式失敗"):
            bot._check_hedge_mode()
        bot.exchange.fapiPrivatePostPositionSideDual.assert_not_called()

    def test_hedged_none_aborts_startup(self):
        """ccxt safe_bool 在 dualSidePosition 缺失時回 None —— 未知不等於 False，
        不得當成「非 hedge」去切換，也不得當成「是 hedge」放行。"""
        bot = _make_bot()
        bot.exchange = _exchange([None])
        with pytest.raises(RuntimeError, match="未回報持倉模式"):
            bot._check_hedge_mode()
        bot.exchange.fapiPrivatePostPositionSideDual.assert_not_called()

    def test_switch_rejected_and_still_not_hedged_aborts_startup(self):
        """帳戶有持倉/掛單時幣安會拒絕 dualSidePosition 切換 —— 原本這被
        `except Exception: pass` 吞掉，bot 帶著錯誤的模式假設繼續啟動。

        2026-08-28 修訂（security F2）：切換報錯**不再**直接判死，而是落到複驗、
        以最終實際狀態裁決。所以這條測試守的東西也跟著變精確：
        「切換被拒 **且複驗確認帳戶實際仍非雙向** ⇒ 才 raise」。
        它現在同時釘住三件事，缺一都算守衛被弱化：
          1. 切換失敗 + 複驗全失敗 ⇒ 仍然 raise（硬失敗裁決沒有被 F2 放寬）；
          2. 複驗**真的有跑**（call_count == 4 = 1 次初查 + 3 次複驗），
             不是「報錯就跳過複驗直接 raise」的舊行為；
          3. 交易所原始錯誤原文（-4068）出現在訊息裡 —— 操作員要能據原文判斷
             成因，而不是被寫死的「去平倉」結論牽著走（I-3）。
        """
        bot = _make_bot()
        # 哨兵：斷言字面值必須是「原始錯誤真的被插進訊息」才會出現的東西。
        # 先前這裡斷言的是 "-4068"，但 -4068 同時被寫死在訊息模板裡 ⇒
        # 把整段 {switch_err} 拿掉，測試照樣綠（外部 review 實測）。
        bot.exchange = _exchange(
            [False], switch_error=RuntimeError("-4068 QK7X-SWITCH-ORIGINAL-ERR"))
        with pytest.raises(RuntimeError, match="切換持倉模式被交易所拒絕") as ei:
            bot._check_hedge_mode()
        assert "QK7X-SWITCH-ORIGINAL-ERR" in str(ei.value), \
            "訊息必須帶交易所原始錯誤原文，不能只有模板裡寫死的 -4068"
        assert bot.exchange.fetch_position_mode.call_count == 4

    def test_operator_guidance_survives_a_giant_exchange_error(self):
        """給人的指引必須排在交易所原文**之前**，且原文要截斷。

        `notify_crash` 只送前 500 字（notifier 的既有行為，不改它），而交易所
        維護/5xx 會讓 ccxt 把整包 HTML body 塞進例外訊息。指引排在原文後面就會
        被截掉，操作員只讀到「可能是持倉問題」⇒ 拿真錢去手動平倉。
        """
        bot = _make_bot()
        huge = "<html>" + "B" * 50_000 + "</html>"
        bot.exchange = _exchange([False], switch_error=RuntimeError(huge))
        with pytest.raises(RuntimeError) as ei:
            bot._check_hedge_mode()
        msg = str(ei.value)
        head = msg[:500]  # notify_crash 實際會送出去的那一段
        assert "不要預設是持倉問題就去平倉" in head, "指引不得被交易所原文擠出截斷線"
        assert "那些都不該用平倉來處理" in head
        assert "原文已截斷" in msg
        assert len(msg) < 1000, "整條訊息不得被 5 萬字的 HTML body 撐爆"

    @pytest.mark.parametrize("exc_factory", [
        lambda: ccxt.ExchangeNotAvailable("<html>" + "B" * 80_000 + "</html>"),
        lambda: ccxt.BadRequest("<html>" + "B" * 80_000 + "</html>"),
    ], ids=["retryable-ExchangeNotAvailable", "definitive-BadRequest"])
    def test_initial_fetch_failure_message_is_bounded(self, _no_real_sleep, exc_factory):
        """初查失敗那條 raise 也必須截斷交易所原文。

        它是 `_check_hedge_mode` 裡第三個注入點，先前只有另外兩處套了 `_clip`
        ⇒ 這條路徑實測會產生 8 萬字的訊息，每次啟動失敗往日誌灌 80KB。
        危害比切換那條低（這條的指引本來就排在原文**前面**，不會被擠掉），
        但「維護中的 ExchangeNotAvailable 重試 3 次後從這裡 raise」是很常見的
        真實路徑，所以兩種例外（可重試 / 不可重試）都釘。
        """
        bot = _make_bot()
        bot.exchange = _exchange([exc_factory()])
        with pytest.raises(RuntimeError, match="查詢持倉模式失敗") as ei:
            bot._check_hedge_mode()
        msg = str(ei.value)
        assert len(msg) < 1000, f"訊息未截斷（實得 {len(msg)} 字）"
        assert "原文已截斷" in msg
        assert "拒絕啟動" in msg, "截斷不得把給人的說明也一起吃掉"

    def test_verification_accepts_only_the_exact_true(self):
        """複驗和初查一樣，只有**明確的 True** 才算確認。

        `if again:` 這種寫法會讓字串 'true'、數字 1 這類 truthy 值矇混過關 ⇒
        bot 帶著沒被證實的模式假設啟動，正是這道守衛要擋的事。
        """
        bot = _make_bot()
        bot.exchange = _exchange([False, 'true'])  # 複驗永遠回 truthy 但非 True
        with pytest.raises(RuntimeError, match="切換呼叫沒報錯但實際未生效"):
            bot._check_hedge_mode()
        assert bot.exchange.fetch_position_mode.call_count == 4

    def test_last_verify_error_is_carried_into_the_final_message(self):
        """複驗過程的查詢錯誤必須被記住並帶進最終訊息 —— 否則操作員看到的是
        「複驗仍非雙向持倉模式」，完全不知道其實是三次複驗都根本沒查成功。"""
        bot = _make_bot()
        bot.exchange = _exchange(
            [False, ccxt.RequestTimeout("VF3Q-VERIFY-ORIGINAL-ERR")])
        with pytest.raises(RuntimeError) as ei:
            bot._check_hedge_mode()
        assert "最後一次查詢錯誤" in str(ei.value)
        assert "VF3Q-VERIFY-ORIGINAL-ERR" in str(ei.value)

    def test_switch_error_but_verify_confirms_hedged_starts_normally(self):
        """幣安 -4059「No need to change position side」＝帳戶本來就已經是目標
        狀態（ccxt 映射成 BadRequest）；「POST 已生效但回應 timeout」同理。
        以呼叫回傳裁決會把這兩種良性結果判死 ⇒ 假陽性擋下啟動，而擋下啟動的
        代價是既有持倉同時失去追蹤止盈與網格管理。以最終狀態裁決才是對的。"""
        bot = _make_bot()
        bot.exchange = _exchange(
            [False, True],
            switch_error=ccxt.BadRequest("-4059 No need to change position side"))
        bot._check_hedge_mode()  # 不得 raise
        bot.exchange.fapiPrivatePostPositionSideDual.assert_called_once()

    def test_initial_fetch_retries_network_error_then_succeeds(self, _no_real_sleep):
        """初查的網路瞬斷不等於「確認不了」。複驗容忍 3 次而初查零重試是不對稱的，
        且代價不只是 bot 沒跑：sync_service 在 raise 之後才啟動，追蹤止盈只由它
        驅動 ⇒ 交易所上的既有持倉會同時失去風控。"""
        bot = _make_bot()
        bot.exchange = _exchange([
            ccxt.RequestTimeout("read timeout"),
            ccxt.RequestTimeout("read timeout"),
            True,
        ])
        bot._check_hedge_mode()  # 不得 raise
        assert bot.exchange.fetch_position_mode.call_count == 3
        assert _no_real_sleep == [1.0, 1.0], "重試之間必須有間隔，否則等於沒重試"
        bot.exchange.fapiPrivatePostPositionSideDual.assert_not_called()

    def test_initial_fetch_network_error_exhausted_still_aborts(self, _no_real_sleep):
        """重試只是不把一次抖動當成「查不到」；重試耗盡仍然 raise —— 使用者
        2026-08-28 裁決「確認不了就不啟動」不因 F1 而被放寬。"""
        bot = _make_bot()
        bot.exchange = _exchange([ccxt.RequestTimeout("read timeout")])
        with pytest.raises(RuntimeError, match="查詢持倉模式失敗"):
            bot._check_hedge_mode()
        assert bot.exchange.fetch_position_mode.call_count == 3
        assert _no_real_sleep == [1.0, 1.0]
        bot.exchange.fapiPrivatePostPositionSideDual.assert_not_called()

    @pytest.mark.parametrize("exc_factory", [
        lambda: ccxt.OperationFailed("-1001 DISCONNECTED"),
        lambda: ccxt.OperationFailed("-1000 UNKNOWN"),
        lambda: ccxt.RequestTimeout("-1007 TIMEOUT"),
        lambda: ccxt.RateLimitExceeded("-1003 TOO_MANY_REQUESTS"),
        lambda: ccxt.ExchangeNotAvailable("System is under maintenance."),
        lambda: ccxt.InvalidNonce("-1021 recvWindow"),
        lambda: ccxt.DDoSProtection("418"),
        lambda: ccxt.BadResponse("malformed json"),
    ], ids=["OperationFailed-1001", "OperationFailed-1000", "RequestTimeout",
            "RateLimitExceeded", "ExchangeNotAvailable", "InvalidNonce",
            "DDoSProtection", "BadResponse"])
    def test_transient_failures_are_retried(self, _no_real_sleep, exc_factory):
        """「請求沒完成、狀態未知」的一整族都必須重試。

        分界線是 ccxt 的 `OperationFailed`，**不是** `NetworkError` ——
        後者是前者的**子類**（實測 `issubclass(NetworkError, OperationFailed)`
        為 True，反向為 False）。只判 `NetworkError` 會漏掉直接掛在
        `OperationFailed` 底下的 -1000 UNKNOWN / -1001 DISCONNECTED /
        -1006 UNEXPECTED_RESP，而幣安官方文件正是說這幾個該重試 ⇒ 一次瞬斷
        就擋下啟動，既有持倉同時失去追蹤止盈與網格管理。

        類別名寫死，不從 except 的參數反推。
        """
        bot = _make_bot()
        bot.exchange = _exchange([exc_factory()])
        with pytest.raises(RuntimeError, match="查詢持倉模式失敗"):
            bot._check_hedge_mode()
        assert bot.exchange.fetch_position_mode.call_count == 3
        assert _no_real_sleep == [1.0, 1.0]

    @pytest.mark.parametrize("exc_factory", [
        lambda: ccxt.BadRequest("-1022 Signature not valid"),
        lambda: ccxt.AuthenticationError("-2015 Invalid API-key"),
        lambda: ccxt.PermissionDenied("no futures permission"),
        lambda: ccxt.OperationRejected("-4068 position side cannot be changed"),
        lambda: ccxt.NotSupported("testnet disabled"),
        lambda: RuntimeError("not a ccxt error at all"),
    ], ids=["BadRequest", "AuthenticationError", "PermissionDenied",
            "OperationRejected", "NotSupported", "plain-RuntimeError"])
    def test_definitive_failures_are_not_retried(self, _no_real_sleep, exc_factory):
        """「請求完成了，交易所說不行」重試三次結果必然相同 —— 只會多送兩次
        注定失敗的簽名請求並讓啟動多卡 2 秒。這族必須第一次就 raise。"""
        bot = _make_bot()
        bot.exchange = _exchange([exc_factory()])
        with pytest.raises(RuntimeError, match="查詢持倉模式失敗"):
            bot._check_hedge_mode()
        assert bot.exchange.fetch_position_mode.call_count == 1
        assert _no_real_sleep == []

    def test_switch_then_verify_succeeds(self, _no_real_sleep):
        """切換在交易所端非同步生效：第一次複驗仍讀到舊值，第二次才確認。"""
        bot = _make_bot()
        bot.exchange = _exchange([False, False, True])
        bot._check_hedge_mode()
        bot.exchange.fapiPrivatePostPositionSideDual.assert_called_once()
        assert bot.exchange.fetch_position_mode.call_count == 3
        assert _no_real_sleep == [1.0], "第二次複驗前必須有間隔，否則等於沒複驗"

    def test_verify_never_confirms_aborts_startup(self, _no_real_sleep):
        """切換呼叫沒拋錯，但模式實際沒變 —— 這正是「不複驗就會漏掉」的形態。

        match 用「切換呼叫沒報錯但實際未生效」而不是「複驗」：F2 之後兩條 raise
        都含「複驗」二字，只 match 它就分不出走的是哪條路徑。
        """
        bot = _make_bot()
        bot.exchange = _exchange([False])
        with pytest.raises(RuntimeError, match="切換呼叫沒報錯但實際未生效"):
            bot._check_hedge_mode()
        assert bot.exchange.fetch_position_mode.call_count == 4  # 1 次初查 + 3 次複驗
        assert _no_real_sleep == [1.0, 1.0]

    def test_no_enabled_symbol_skips_check(self):
        """沒有啟用中的 symbol，本來就不會下單 —— 不該因為查不到模式而擋下啟動。"""
        bot = _make_bot(enabled=False)
        bot.exchange = _exchange([RuntimeError("should not be called")])
        bot._check_hedge_mode()
        bot.exchange.fetch_position_mode.assert_not_called()

    def test_fetch_returns_non_dict_aborts_startup(self):
        """fetch_position_mode 回的東西不是 dict（例如 None、字串）—— 這不是
        「沒回報 dualSidePosition 欄位」以外的第三種未知形態，同樣不得放行。
        `_fetch_hedged` 的 `if not isinstance(mode, dict): return None, None`
        分支必須真的被行使到，不能只靠 dict-但欄位缺失 的測試間接覆蓋。"""
        bot = _make_bot()
        ex = MagicMock()
        ex.fetch_position_mode.return_value = None  # 非 dict 回應
        bot.exchange = ex
        with pytest.raises(RuntimeError, match="未回報持倉模式"):
            bot._check_hedge_mode()
        bot.exchange.fapiPrivatePostPositionSideDual.assert_not_called()


def _filled_event(position_side, side="BUY", realized_pnl="1.5", symbol="XRPUSDC"):
    return {"o": {
        "s": symbol, "X": "FILLED", "S": side,
        "ps": position_side, "rp": realized_pnl,
        "p": "0.5", "q": "10",
    }}


@pytest.fixture
def order_bot():
    """掛單計數初值刻意設成非 0：若設 0，「沒重置」與「重置了」不可分辨
    （lessons 通則 3.3：fixture 不得把待測維度壓成退化值）。

    配兩個 symbol：節流以 symbol 為 key，單 symbol 的 fixture 測不出
    「A 的告警把 B 壓住了」，也測不出訊息裡印的是哪個 symbol。"""
    bot = _make_bot(extra_symbol=True)
    bot.adjust_grid = AsyncMock()
    bot.bandit_optimizer.record_trade = MagicMock()
    for sym in (SYMBOL, SYMBOL2):
        st = bot.state.symbols[sym]
        st.buy_long_orders = 3
        st.sell_long_orders = 4
        st.buy_short_orders = 5
        st.sell_short_orders = 6
        st.ws_seq = 7
    return bot


class TestOrderUpdatePositionSideGuard:
    @pytest.mark.asyncio
    async def test_both_position_side_is_not_applied(self, order_bot):
        """ps='BOTH' ⇒ 帳戶在單向持倉模式，分側狀態沒有正確映射。
        套用會把成交記到錯的一側、重置錯的掛單計數。"""
        st = order_bot.state.symbols[SYMBOL]
        await order_bot._handle_order_update(_filled_event("BOTH"))

        assert (st.buy_long_orders, st.sell_long_orders) == (3, 4)
        assert (st.buy_short_orders, st.sell_short_orders) == (5, 6)
        assert st.ws_seq == 7, "早退必須發生在 ws_seq 遞增之前"
        order_bot.bandit_optimizer.record_trade.assert_not_called()
        order_bot.adjust_grid.assert_not_called()

    @pytest.mark.asyncio
    async def test_both_is_not_recorded_as_short_in_bandit(self, order_bot):
        """改動前 `trade_side = 'long' if ps == 'LONG' else 'short'` 會把
        BOTH 靜默記成 short，汙染 bandit 的分側統計。"""
        await order_bot._handle_order_update(_filled_event("BOTH"))
        order_bot.bandit_optimizer.record_trade.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_position_side_is_not_applied(self, order_bot):
        st = order_bot.state.symbols[SYMBOL]
        await order_bot._handle_order_update(_filled_event("SIDEWAYS"))
        assert (st.buy_long_orders, st.ws_seq) == (3, 7)
        order_bot.adjust_grid.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_position_side_is_not_applied(self, order_bot):
        """事件缺 `ps` 欄位 ⇒ `order_data.get('ps', '')` 給出空字串。改動前它會落到
        `trade_side = 'long' if ps == 'LONG' else 'short'` 被記成 short、遞增
        ws_seq、重置掛單計數並重掛網格 —— 空字串必須跟 BOTH 一樣走早退。
        （最終 review MUT-3：把 `''` 加進放行清單原本 866 全綠。）"""
        st = order_bot.state.symbols[SYMBOL]
        await order_bot._handle_order_update(_filled_event(""))

        assert (st.buy_long_orders, st.sell_long_orders) == (3, 4)
        assert (st.buy_short_orders, st.sell_short_orders) == (5, 6)
        assert st.ws_seq == 7
        order_bot.bandit_optimizer.record_trade.assert_not_called()
        order_bot.adjust_grid.assert_not_called()

    @pytest.mark.asyncio
    async def test_warning_names_the_raw_symbol_and_the_actual_ps(self, order_bot, caplog):
        """告警要指得出「哪個 symbol、收到什麼值」，否則操作員拿它沒辦法診斷。

        釘住的是訊息裡印的是 **交易所原始 symbol**（XRPUSDC）而不是 ccxt symbol
        （XRP/USDC:USDC）—— 節流 key 用 ccxt_symbol、訊息用 symbol_raw，兩者
        對調過去測不出來（最終 review MUT-4：參數對調 866 全綠）。
        斷言整段連續字面值，才能讓「印成 ccxt symbol」這個 mutation 紅。
        """
        with caplog.at_level("WARNING"):
            await order_bot._handle_order_update(_filled_event("BOTH"))

        msgs = [r.getMessage() for r in caplog.records if "非 LONG/SHORT" in r.getMessage()]
        assert len(msgs) == 1
        assert "[userData] XRPUSDC positionSide='BOTH' 非 LONG/SHORT" in msgs[0]

    @pytest.mark.asyncio
    async def test_long_still_applied_after_guard(self, order_bot):
        """守衛不得誤傷正常路徑。"""
        st = order_bot.state.symbols[SYMBOL]
        await order_bot._handle_order_update(_filled_event("LONG", side="BUY"))
        assert st.buy_long_orders == 0
        assert st.sell_long_orders == 4, "只該重置本次成交的那一格"
        assert st.ws_seq == 8
        order_bot.bandit_optimizer.record_trade.assert_called_once_with(1.5, 'long')
        order_bot.adjust_grid.assert_awaited_once_with(SYMBOL)

    @pytest.mark.asyncio
    async def test_short_still_applied_after_guard(self, order_bot):
        st = order_bot.state.symbols[SYMBOL]
        await order_bot._handle_order_update(_filled_event("SHORT", side="SELL"))
        assert st.sell_short_orders == 0
        assert st.buy_short_orders == 5
        order_bot.bandit_optimizer.record_trade.assert_called_once_with(1.5, 'short')

    @pytest.mark.asyncio
    async def test_warning_is_throttled_but_guard_is_not(self, order_bot, caplog):
        """節流只准影響 log，不准影響早退 —— 第二筆事件一樣不得被套用。"""
        st = order_bot.state.symbols[SYMBOL]
        with caplog.at_level("WARNING"):
            await order_bot._handle_order_update(_filled_event("BOTH"))
            await order_bot._handle_order_update(_filled_event("BOTH"))

        hits = [r for r in caplog.records if "非 LONG/SHORT" in r.getMessage()]
        assert len(hits) == 1, "同一 symbol 的重複事件不得洗版"
        assert (st.buy_long_orders, st.ws_seq) == (3, 7), "第二筆一樣不得套用"


@pytest.fixture
def fake_guard_clock():
    """可推進的假守衛時鐘（比照 tests/test_price_staleness_guard.py 的 fake_clock）。

    注入 set_guard_clock 而非 set_clock：節流用的是 clock.guard_now()。
    """
    t = {"now": 1_000_000.0}
    clock.set_guard_clock(lambda: t["now"])

    def advance(seconds):
        t["now"] += seconds
    yield advance
    clock.reset_guard_clock()


class TestUnknownPositionSideLogThrottleWindow:
    @pytest.mark.asyncio
    async def test_warning_resumes_after_throttle_window(self, order_bot, caplog,
                                                            fake_guard_clock):
        """節流窗口過後，告警必須恢復 —— 不是「第一次之後永遠靜音」。
        只釘「窗口內至多一次」測不出「節流條件被改成永久靜音」這種 mutation
        （例如把門檻改成一個天文數字）。期望值寫死秒數，不從常數換算，
        避免常數改了期望值跟著位移、斷言失去意義。
        """
        with caplog.at_level("WARNING"):
            await order_bot._handle_order_update(_filled_event("BOTH"))
            fake_guard_clock(3601.0)
            await order_bot._handle_order_update(_filled_event("BOTH"))

        hits = [r for r in caplog.records if "非 LONG/SHORT" in r.getMessage()]
        assert len(hits) == 2

    @pytest.mark.asyncio
    async def test_warning_stays_silent_up_to_the_window_edge(self, order_bot, caplog,
                                                              fake_guard_clock):
        """窗口的**下界**：距離上一則 3599 秒仍不得再印。

        上面那條只證明「窗口過了會再 log」，把窗口從 3600 改成 1.0 秒它照樣綠
        （外部 review 實測 mutation 存活）—— 節流等於沒有上限也測不出來。
        期望值寫死秒數，不從常數換算。
        """
        with caplog.at_level("WARNING"):
            await order_bot._handle_order_update(_filled_event("BOTH"))
            fake_guard_clock(3599.0)
            await order_bot._handle_order_update(_filled_event("BOTH"))

        hits = [r for r in caplog.records if "非 LONG/SHORT" in r.getMessage()]
        assert len(hits) == 1, "窗口未過就再印 ⇒ 節流窗口被縮短或失效"


class TestStartupWiring:
    """守衛 1 的「接線」：run() 必須讓 _check_hedge_mode 的 raise 真的擋下啟動。

    這個 class 釘的不是 _check_hedge_mode 本身（上面 TestCheckHedgeMode 已經釘了），
    而是 spec Goal 1 的不變式：**確立不了 hedge 就不啟動**。少了它，任何人把
    run() 裡那句 gateway.call(self._check_hedge_mode) 包回 `except Exception: pass`
    （＝這條 branch 要修掉的 anti-pattern）都能全套綠燈通過。
    """

    @pytest.mark.asyncio
    async def test_hedge_check_failure_stops_startup_before_listen_key(self):
        bot = _make_bot()
        bot._init_exchange = MagicMock()
        bot._check_hedge_mode = MagicMock(
            side_effect=RuntimeError("[MAX] 查詢持倉模式失敗（測試注入）"))
        bot.ws_client.acquire_listen_key = AsyncMock()
        bot.sync_service.sync_once = AsyncMock()
        bot.notifier.notify_crash = AsyncMock()
        # 以下純粹是為了「守衛被繞過」那一側能乾淨地紅：若例外被吞掉，run() 會
        # 一路走到常駐 task 與 while not _stop_event 迴圈而永不返回 —— 那樣
        # mutation 得到的是 hang，不是失敗的斷言。先 set stop event 並把常駐
        # 部件換成 AsyncMock，讓「被繞過」變成「跑完後斷言紅」。
        bot._stop_event.set()
        bot.ws_client.run = AsyncMock()
        bot.ws_client.keep_alive_loop = AsyncMock()
        bot.userdata_watchdog.run = AsyncMock()
        bot.sync_service.run = AsyncMock()
        bot.stop = AsyncMock()

        await asyncio.wait_for(bot.run(), timeout=5)

        # acquire_listen_key 是 _check_hedge_mode 之後的下一步：只要它被呼叫，
        # 就代表 raise 沒有擋住啟動序列（例外被吞掉了）。
        bot.ws_client.acquire_listen_key.assert_not_called()
        bot.sync_service.sync_once.assert_not_called()
        assert bot.state.running is False
        bot.notifier.notify_crash.assert_awaited_once()
        assert not bot.tasks, "初始化失敗不得 create_task 任何常駐 task"
