"""帳戶餘額/保證金更新邏輯測試

核心契約 (2026-06-03 fix):
  - WS ACCOUNT_UPDATE 只擁有 wallet_balance + 持倉浮盈的寫入權。
  - REST _sync_account 獨佔 available_balance / margin_used 的真值。
  - WS 更新「絕不可」覆寫 REST 寫入的 available/margin。
"""

import pytest
from unittest.mock import MagicMock

from grid_engine.bot import MaxGridBot
from grid_engine.config import GlobalConfig, SymbolConfig


def _make_bot(symbols=None):
    cfg = GlobalConfig()
    if symbols:
        cfg.symbols = symbols
    bot = MaxGridBot(cfg)
    # 重建 symbol state（__init__ 已依 cfg 建好，但測試可能改 cfg）
    return bot


def _fake_exchange(total, free):
    ex = MagicMock()
    ex.fetch_balance.return_value = {
        "total": {"USDC": total},
        "free": {"USDC": free},
    }
    return ex


def _fake_exchange_info(assets, total=None, free=None):
    """模擬幣安合約 fetch_balance：info.assets 帶原值，頂層 total=marginBalance。"""
    ex = MagicMock()
    payload = {"info": {"assets": assets}}
    if total is not None:
        payload["total"] = {"USDC": total}
    if free is not None:
        payload["free"] = {"USDC": free}
    ex.fetch_balance.return_value = payload
    return ex


# ──────────────────────────── 核心 regression ────────────────────────────

class TestRESTOwnsAvailableAndMargin:
    @pytest.mark.asyncio
    async def test_sync_account_sets_available_and_margin(self):
        bot = _make_bot()
        bot.exchange = _fake_exchange(total=100.0, free=80.0)
        await bot._sync_account()
        acc = bot.state.get_account("USDC")
        assert acc.wallet_balance == 100.0
        assert acc.available_balance == 80.0
        assert acc.margin_used == 20.0

    @pytest.mark.asyncio
    async def test_ws_update_must_not_clobber_rest_values(self):
        """這是 2026-06-03 bug 的本質：WS 更新不得覆寫 REST 的 available/margin。"""
        bot = _make_bot()
        # 先讓 REST 寫入真值
        bot.exchange = _fake_exchange(total=100.0, free=80.0)
        await bot._sync_account()

        # 模擬 WS ACCOUNT_UPDATE，wb 變了，但只該動 wallet_balance
        await bot._handle_account_update({
            "a": {"B": [{"a": "USDC", "wb": "123.45", "cw": "999.99"}], "P": []}
        })

        acc = bot.state.get_account("USDC")
        assert acc.wallet_balance == 123.45         # WS 擁有此欄位 → 更新
        assert acc.available_balance == 80.0        # REST 真值 → 不可被 cw 覆寫
        assert acc.margin_used == 20.0              # REST 真值 → 不可被歸零

    @pytest.mark.asyncio
    async def test_margin_clamped_to_zero_when_free_ge_total(self):
        bot = _make_bot()
        bot.exchange = _fake_exchange(total=50.0, free=50.0)
        await bot._sync_account()
        assert bot.state.get_account("USDC").margin_used == 0

        bot.exchange = _fake_exchange(total=50.0, free=60.0)  # free>total（理論上不會，但要防）
        await bot._sync_account()
        assert bot.state.get_account("USDC").margin_used == 0

    @pytest.mark.asyncio
    async def test_margin_usage_zero_when_no_equity(self):
        bot = _make_bot()
        bot.exchange = _fake_exchange(total=0.0, free=0.0)
        await bot._sync_account()
        assert bot.state.margin_usage == 0


# ──────────────────────────── Monkey / 極端輸入 ────────────────────────────

class TestAccountUpdateMonkey:
    @pytest.mark.asyncio
    async def test_empty_payload_no_crash(self):
        bot = _make_bot()
        await bot._handle_account_update({})  # 完全空 → 不該拋

    @pytest.mark.asyncio
    async def test_missing_a_key(self):
        bot = _make_bot()
        await bot._handle_account_update({"e": "ACCOUNT_UPDATE"})

    @pytest.mark.asyncio
    async def test_balances_is_none(self):
        bot = _make_bot()
        await bot._handle_account_update({"a": {"B": None}})  # for bal in None → 被 except 接住

    @pytest.mark.asyncio
    async def test_none_and_missing_wb(self):
        bot = _make_bot()
        await bot._handle_account_update({"a": {"B": [
            {"a": "USDC", "wb": None},
            {"a": "USDT"},  # 完全沒 wb
        ]}})
        assert bot.state.get_account("USDC").wallet_balance == 0
        assert bot.state.get_account("USDT").wallet_balance == 0

    @pytest.mark.asyncio
    async def test_non_stable_asset_ignored(self):
        bot = _make_bot()
        before = bot.state.get_account("USDC").wallet_balance
        await bot._handle_account_update({"a": {"B": [{"a": "BTC", "wb": "999"}]}})
        assert bot.state.get_account("USDC").wallet_balance == before

    @pytest.mark.asyncio
    async def test_negative_and_huge_values(self):
        bot = _make_bot()
        await bot._handle_account_update({"a": {"B": [{"a": "USDC", "wb": "-500"}]}})
        assert bot.state.get_account("USDC").wallet_balance == -500.0
        await bot._handle_account_update({"a": {"B": [{"a": "USDC", "wb": "1e18"}]}})
        assert bot.state.get_account("USDC").wallet_balance == 1e18

    @pytest.mark.asyncio
    async def test_malformed_bal_entry(self):
        bot = _make_bot()
        # bal 缺 'a' → asset='' → 不在白名單 → 跳過，不崩
        await bot._handle_account_update({"a": {"B": [{"wb": "100"}]}})

    @pytest.mark.asyncio
    async def test_position_none_fields_no_crash(self):
        bot = _make_bot()
        await bot._handle_account_update({"a": {"B": [], "P": [
            {"s": "BNBUSDC", "pa": None, "up": None, "ps": "LONG"},
        ]}})

    @pytest.mark.asyncio
    async def test_garbage_wb_string_caught(self):
        """非數字字串 → float() 拋 → 被 except 接住，不污染狀態。"""
        bot = _make_bot()
        bot.exchange = _fake_exchange(total=100.0, free=80.0)
        await bot._sync_account()
        await bot._handle_account_update({"a": {"B": [{"a": "USDC", "wb": "garbage"}]}})
        # 整段失敗被吞，REST 真值不受影響
        acc = bot.state.get_account("USDC")
        assert acc.available_balance == 80.0
        assert acc.margin_used == 20.0


# ─────────────── 核心 regression: REST 不得重複計算浮盈 ───────────────

class TestRESTNoDoubleCountUnrealized:
    """2026-06-xx bug: ccxt 頂層 total=marginBalance(已含浮盈)，
    舊碼 wallet_balance=total 後又 equity=wallet+upnl → 浮盈算兩次，權益失真。
    正解：從 info.assets 取 walletBalance(不含浮盈)，equity=wallet+upnl 才對。"""

    @pytest.mark.asyncio
    async def test_equity_matches_binance_margin_balance(self):
        # 重現截圖：錢包 125.55、浮盈 -31.06 → 權益應為保證金餘額 94.49
        bot = _make_bot()
        bot.exchange = _fake_exchange_info([{
            "asset": "USDC",
            "walletBalance": "125.5485",
            "unrealizedProfit": "-31.0574",
            "availableBalance": "15.50",
            "initialMargin": "78.99",
        }])
        await bot._sync_account()
        acc = bot.state.get_account("USDC")
        assert acc.wallet_balance == pytest.approx(125.5485)
        assert acc.unrealized_pnl == pytest.approx(-31.0574)
        # 關鍵：權益 = wallet + upnl = marginBalance，不是 64
        assert acc.equity == pytest.approx(94.4911)
        assert acc.available_balance == pytest.approx(15.50)
        assert acc.margin_used == pytest.approx(78.99)
        # 保證金率 = 倉位保證金 / 權益
        assert acc.margin_ratio == pytest.approx(78.99 / 94.4911)

    @pytest.mark.asyncio
    async def test_does_not_use_top_level_total(self):
        # 即使頂層 total(marginBalance) 同時存在，也該以 info.assets 為準，不重複加浮盈
        bot = _make_bot()
        bot.exchange = _fake_exchange_info(
            [{"asset": "USDC", "walletBalance": "100",
              "unrealizedProfit": "-10", "availableBalance": "50", "initialMargin": "40"}],
            total=90.0, free=50.0,
        )
        await bot._sync_account()
        acc = bot.state.get_account("USDC")
        assert acc.equity == pytest.approx(90.0)  # 100 + (-10)，非 90 + (-10)

    @pytest.mark.asyncio
    async def test_fallback_when_asset_missing_in_info(self):
        # info.assets 沒有該幣 → fallback：total 視為 marginBalance，還原錢包餘額，equity 不雙算
        bot = _make_bot()
        bot.exchange = _fake_exchange_info([], total=94.49, free=15.50)
        await bot._sync_account()
        acc = bot.state.get_account("USDC")
        # 無持倉 upnl=0 → wallet=94.49, equity=94.49（不會變 188）
        assert acc.equity == pytest.approx(94.49)


class TestSyncAccountInfoMonkey:
    @pytest.mark.asyncio
    async def test_info_missing_fields_default_zero(self):
        bot = _make_bot()
        bot.exchange = _fake_exchange_info([{"asset": "USDC"}])  # 啥欄位都沒
        await bot._sync_account()
        acc = bot.state.get_account("USDC")
        assert acc.wallet_balance == 0
        assert acc.unrealized_pnl == 0
        assert acc.margin_used == 0
        assert acc.equity == 0

    @pytest.mark.asyncio
    async def test_info_none_values(self):
        bot = _make_bot()
        bot.exchange = _fake_exchange_info([{
            "asset": "USDC", "walletBalance": None,
            "unrealizedProfit": None, "availableBalance": None, "initialMargin": None,
        }])
        await bot._sync_account()
        assert bot.state.get_account("USDC").equity == 0

    @pytest.mark.asyncio
    async def test_assets_is_none_no_crash(self):
        bot = _make_bot()
        ex = MagicMock()
        ex.fetch_balance.return_value = {"info": {"assets": None}, "total": {}, "free": {}}
        bot.exchange = ex
        await bot._sync_account()  # assets=None → `or []` → fallback，不崩
        assert bot.state.get_account("USDC").equity == 0

    @pytest.mark.asyncio
    async def test_garbage_wallet_balance_caught(self):
        bot = _make_bot()
        bot.exchange = _fake_exchange_info([{"asset": "USDC", "walletBalance": "garbage"}])
        await bot._sync_account()  # float('garbage') 拋 → 被 _sync_account except 吞，不污染
        assert bot.state.get_account("USDC").equity == 0

    @pytest.mark.asyncio
    async def test_huge_and_negative_equity(self):
        bot = _make_bot()
        bot.exchange = _fake_exchange_info([{
            "asset": "USDC", "walletBalance": "1e18", "unrealizedProfit": "-5e17",
            "availableBalance": "0", "initialMargin": "0",
        }])
        await bot._sync_account()
        assert bot.state.get_account("USDC").equity == pytest.approx(5e17)


class TestRiskAlertSwitch:
    """telegram_risk_alert_enabled 風控警報獨立開關"""

    def _make_risky_bot(self, risk_alert_enabled):
        bot = _make_bot()
        bot.config.telegram_risk_alert_enabled = risk_alert_enabled
        bot.config.risk.enabled = True
        bot.config.risk.margin_threshold = 0.5
        bot.notifier = MagicMock()
        bot.notifier.enabled = True
        bot.state.margin_usage = 0.9  # 超標
        return bot

    @pytest.mark.asyncio
    async def test_alert_sent_when_enabled(self):
        bot = self._make_risky_bot(True)
        from unittest.mock import AsyncMock
        bot.notifier.notify_risk_alert = AsyncMock()
        await bot._check_risk_and_notify()
        bot.notifier.notify_risk_alert.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_alert_suppressed_when_disabled(self):
        bot = self._make_risky_bot(False)
        from unittest.mock import AsyncMock
        bot.notifier.notify_risk_alert = AsyncMock()
        await bot._check_risk_and_notify()
        bot.notifier.notify_risk_alert.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disabled_does_not_consume_cooldown(self):
        """關閉期間不更新冷卻計時，重開後立即可發"""
        bot = self._make_risky_bot(False)
        from unittest.mock import AsyncMock
        bot.notifier.notify_risk_alert = AsyncMock()
        await bot._check_risk_and_notify()
        assert bot.last_risk_alert_time == 0
        bot.config.telegram_risk_alert_enabled = True
        await bot._check_risk_and_notify()
        bot.notifier.notify_risk_alert.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cooldown_from_config(self):
        """冷卻秒數讀 config：第二次超標在冷卻內不發、過冷卻後再發"""
        import time as _time
        from unittest.mock import AsyncMock
        bot = self._make_risky_bot(True)
        bot.config.telegram_risk_alert_cooldown = 60
        bot.notifier.notify_risk_alert = AsyncMock()
        await bot._check_risk_and_notify()
        await bot._check_risk_and_notify()  # 冷卻內 → 不發
        assert bot.notifier.notify_risk_alert.await_count == 1
        bot.last_risk_alert_time = _time.time() - 61  # 模擬冷卻已過
        await bot._check_risk_and_notify()
        assert bot.notifier.notify_risk_alert.await_count == 2
