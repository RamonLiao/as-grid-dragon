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


# ──────────────────────────── 核心 regression ────────────────────────────

class TestRESTOwnsAvailableAndMargin:
    def test_sync_account_sets_available_and_margin(self):
        bot = _make_bot()
        bot.exchange = _fake_exchange(total=100.0, free=80.0)
        bot._sync_account()
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
        bot._sync_account()

        # 模擬 WS ACCOUNT_UPDATE，wb 變了，但只該動 wallet_balance
        await bot._handle_account_update({
            "a": {"B": [{"a": "USDC", "wb": "123.45", "cw": "999.99"}], "P": []}
        })

        acc = bot.state.get_account("USDC")
        assert acc.wallet_balance == 123.45         # WS 擁有此欄位 → 更新
        assert acc.available_balance == 80.0        # REST 真值 → 不可被 cw 覆寫
        assert acc.margin_used == 20.0              # REST 真值 → 不可被歸零

    def test_margin_clamped_to_zero_when_free_ge_total(self):
        bot = _make_bot()
        bot.exchange = _fake_exchange(total=50.0, free=50.0)
        bot._sync_account()
        assert bot.state.get_account("USDC").margin_used == 0

        bot.exchange = _fake_exchange(total=50.0, free=60.0)  # free>total（理論上不會，但要防）
        bot._sync_account()
        assert bot.state.get_account("USDC").margin_used == 0

    def test_margin_usage_zero_when_no_equity(self):
        bot = _make_bot()
        bot.exchange = _fake_exchange(total=0.0, free=0.0)
        bot._sync_account()
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
        bot._sync_account()
        await bot._handle_account_update({"a": {"B": [{"a": "USDC", "wb": "garbage"}]}})
        # 整段失敗被吞，REST 真值不受影響
        acc = bot.state.get_account("USDC")
        assert acc.available_balance == 80.0
        assert acc.margin_used == 20.0
