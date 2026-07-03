"""下單路徑加固測試

核心契約 (2026-07-03 架構審查修復 #1):
  - 每筆訂單帶唯一 clientOrderId（newClientOrderId），可追溯、可去重。
  - 下單失敗 → 該 symbol 進入指數退避封鎖，封鎖期內跳過開倉單。
  - 連續失敗達斷路閾值 → 長冷卻 + Telegram 通知（僅轉換邊緣發一次）。
  - reduce_only（止盈/平倉）不受封鎖 — 能出場永遠比能進場重要。
  - 成功下單 → 重置失敗計數與封鎖。
  - 有倉位時 adjust_grid 受 position_adjust_cooldown 頻率下限約束。
"""

import asyncio
import time as _time

import pytest
from unittest.mock import MagicMock, AsyncMock

from grid_engine.bot import (
    MaxGridBot,
    ORDER_BACKOFF_BASE,
    ORDER_BACKOFF_CAP,
    ORDER_CIRCUIT_THRESHOLD,
    ORDER_CIRCUIT_COOLDOWN,
)
from grid_engine.config import GlobalConfig, SymbolConfig

SYM = "BNB/USDC:USDC"


def _make_bot():
    cfg = GlobalConfig()
    bot = MaxGridBot(cfg)
    bot.exchange = MagicMock()
    bot.exchange.create_order.return_value = {"id": "1"}
    return bot


def _make_trading_bot(cooldown=5.0):
    """帶一個啟用交易對、雙邊有倉位的 bot，增強模組全關。"""
    cfg = GlobalConfig()
    cfg.position_adjust_cooldown = cooldown
    sym_cfg = SymbolConfig(symbol="BNBUSDC")
    sym_cfg.enabled = True
    cfg.symbols["BNBUSDC"] = sym_cfg
    cfg.dgt.enabled = False
    cfg.bandit.enabled = False
    cfg.leading_indicator.enabled = False
    bot = MaxGridBot(cfg)
    bot.exchange = MagicMock()
    ccxt_symbol = sym_cfg.ccxt_symbol
    st = bot.state.symbols[ccxt_symbol]
    st.latest_price = 600.0
    st.best_bid = 599.9
    st.best_ask = 600.1
    st.long_position = 1.0
    st.short_position = 1.0
    bot._place_grid = AsyncMock()
    return bot, ccxt_symbol


# ──────────────────────────── clientOrderId ────────────────────────────

class TestClientOrderId:
    @pytest.mark.asyncio
    async def test_client_order_id_attached_and_unique(self):
        bot = _make_bot()
        await bot.place_order(SYM, "buy", 600.0, 1.0)
        await bot.place_order(SYM, "buy", 600.0, 1.0)

        calls = bot.exchange.create_order.call_args_list
        assert len(calls) == 2
        ids = []
        for c in calls:
            params = c.kwargs.get("params") or c.args[-1]
            # 用 ccxt unified key（binance→newClientOrderId、bybit→orderLinkId 由 ccxt 映射）
            assert "clientOrderId" in params
            ids.append(params["clientOrderId"])
        assert ids[0] != ids[1]

    @pytest.mark.asyncio
    async def test_market_order_also_gets_client_order_id(self):
        bot = _make_bot()
        await bot.place_order(SYM, "sell", 0, 1.0, True, "long", "market")
        params = bot.exchange.create_order.call_args.kwargs.get("params")
        assert params and "clientOrderId" in params


# ──────────────────────────── backoff ────────────────────────────

class TestBackoff:
    @pytest.mark.asyncio
    async def test_failure_blocks_next_order(self):
        bot = _make_bot()
        bot.exchange.create_order.side_effect = Exception("Margin is insufficient")

        assert await bot.place_order(SYM, "buy", 600.0, 1.0) is None
        assert await bot.place_order(SYM, "buy", 600.0, 1.0) is None
        # 第二次在封鎖期內，不應打到交易所
        assert bot.exchange.create_order.call_count == 1

    @pytest.mark.asyncio
    async def test_backoff_grows_exponentially(self):
        bot = _make_bot()
        bot.exchange.create_order.side_effect = Exception("boom")

        await bot.place_order(SYM, "buy", 600.0, 1.0)
        first_block = bot._order_block_until[SYM] - _time.time()
        bot._order_block_until[SYM] = 0  # 模擬封鎖期已過
        await bot.place_order(SYM, "buy", 600.0, 1.0)
        second_block = bot._order_block_until[SYM] - _time.time()

        assert first_block == pytest.approx(ORDER_BACKOFF_BASE, abs=0.5)
        assert second_block == pytest.approx(ORDER_BACKOFF_BASE * 2, abs=0.5)

    @pytest.mark.asyncio
    async def test_backoff_capped(self):
        bot = _make_bot()
        bot.exchange.create_order.side_effect = Exception("boom")
        bot._order_fail_counts[SYM] = ORDER_CIRCUIT_THRESHOLD - 2  # 下一次未達斷路

        await bot.place_order(SYM, "buy", 600.0, 1.0)
        block = bot._order_block_until[SYM] - _time.time()
        assert block <= ORDER_BACKOFF_CAP + 0.5

    @pytest.mark.asyncio
    async def test_success_resets_failures(self):
        bot = _make_bot()
        bot._order_fail_counts[SYM] = 3

        await bot.place_order(SYM, "buy", 600.0, 1.0)
        assert bot._order_fail_counts[SYM] == 0

    @pytest.mark.asyncio
    async def test_reduce_only_success_does_not_reset_streak(self):
        """有倉位時每輪 TP(成功)+補倉(失敗) 交錯，斷路器仍須數得到連續失敗。

        這正是 BTC Margin insufficient 43 萬次的場景：若 reduce_only 成功
        會重置計數，斷路閾值永遠達不到。
        """
        bot = _make_bot()
        bot._order_fail_counts[SYM] = 4

        await bot.place_order(SYM, "sell", 610.0, 1.0, reduce_only=True, position_side="long")
        assert bot._order_fail_counts[SYM] == 4  # 不因 TP 成功歸零

    @pytest.mark.asyncio
    async def test_block_is_per_symbol(self):
        bot = _make_bot()
        bot._order_block_until[SYM] = _time.time() + 999
        other = "BTC/USDC:USDC"
        await bot.place_order(other, "buy", 60000.0, 0.001)
        assert bot.exchange.create_order.call_count == 1


# ──────────────────────────── circuit breaker ────────────────────────────

class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_trips_at_threshold_with_long_cooldown_and_single_notify(self):
        bot = _make_bot()
        bot.exchange.create_order.side_effect = Exception("boom")
        bot.notifier = MagicMock()
        bot.notifier.send = AsyncMock()
        bot._order_fail_counts[SYM] = ORDER_CIRCUIT_THRESHOLD - 1

        await bot.place_order(SYM, "buy", 600.0, 1.0)  # 第 threshold 次失敗
        await asyncio.sleep(0)  # 讓 fire-and-forget task 跑

        block = bot._order_block_until[SYM] - _time.time()
        assert block == pytest.approx(ORDER_CIRCUIT_COOLDOWN, abs=1.0)
        assert bot.notifier.send.await_count == 1

        # 封鎖期內再叫 place_order：被跳過，不再通知
        await bot.place_order(SYM, "buy", 600.0, 1.0)
        await asyncio.sleep(0)
        assert bot.notifier.send.await_count == 1

    @pytest.mark.asyncio
    async def test_interleaved_tp_success_entry_failure_still_trips_circuit(self):
        """外部 review must-fix 的完整場景：有倉位時每輪 TP 成功 + 補倉失敗，
        交錯 threshold 輪後斷路器必須觸發（431K Margin insufficient 的根治驗證）。"""
        bot = _make_bot()

        def flaky(symbol, type_, side, qty, price=None, params=None):
            if params and params.get("reduce_only"):
                return {"id": "tp"}
            raise Exception("Margin is insufficient")

        bot.exchange.create_order.side_effect = flaky
        for _ in range(ORDER_CIRCUIT_THRESHOLD):
            await bot.place_order(SYM, "sell", 610.0, 1.0, reduce_only=True, position_side="long")
            bot._order_block_until[SYM] = 0  # 模擬退避期已過
            await bot.place_order(SYM, "buy", 600.0, 1.0)

        block = bot._order_block_until[SYM] - _time.time()
        assert block == pytest.approx(ORDER_CIRCUIT_COOLDOWN, abs=1.0)

    @pytest.mark.asyncio
    async def test_reduce_only_bypasses_block(self):
        bot = _make_bot()
        bot._order_block_until[SYM] = _time.time() + 999

        await bot.place_order(SYM, "sell", 610.0, 1.0, reduce_only=True, position_side="long")
        assert bot.exchange.create_order.call_count == 1

    @pytest.mark.asyncio
    async def test_open_order_skipped_while_blocked(self):
        bot = _make_bot()
        bot._order_block_until[SYM] = _time.time() + 999

        assert await bot.place_order(SYM, "buy", 600.0, 1.0) is None
        assert bot.exchange.create_order.call_count == 0


# ──────────────────────────── 有倉位 adjust_grid 冷卻 ────────────────────────────

class TestPositionAdjustCooldown:
    @pytest.mark.asyncio
    async def test_no_position_branch_skips_cancel_while_blocked(self):
        """封鎖期內無倉位分支不該白撤單（撤了又下不了新單 → 純 API churn）"""
        bot, sym = _make_trading_bot()
        st = bot.state.symbols[sym]
        st.long_position = 0
        st.short_position = 0
        bot._order_block_until[sym] = _time.time() + 999
        bot.cancel_orders_for_side = AsyncMock()

        await bot.adjust_grid(sym)
        bot.cancel_orders_for_side.assert_not_called()

    @pytest.mark.asyncio
    async def test_positioned_adjust_respects_cooldown(self):
        bot, sym = _make_trading_bot(cooldown=5.0)

        await bot.adjust_grid(sym)
        await bot.adjust_grid(sym)  # 緊接著第二次，冷卻未過

        # 雙邊各只掛一次網格
        assert bot._place_grid.await_count == 2

    @pytest.mark.asyncio
    async def test_cooldown_expiry_allows_readjust(self):
        bot, sym = _make_trading_bot(cooldown=5.0)

        await bot.adjust_grid(sym)
        # 模擬冷卻已過
        bot.last_order_times[f"{sym}_long_grid"] = _time.time() - 6
        bot.last_order_times[f"{sym}_short_grid"] = _time.time() - 6
        await bot.adjust_grid(sym)

        assert bot._place_grid.await_count == 4

    @pytest.mark.asyncio
    async def test_cooldown_zero_disables(self):
        bot, sym = _make_trading_bot(cooldown=0.0)

        await bot.adjust_grid(sym)
        await bot.adjust_grid(sym)

        assert bot._place_grid.await_count == 4


# ──────────────────────────── config ────────────────────────────

class TestPositionAdjustCooldownConfig:
    def test_default_and_roundtrip(self):
        cfg = GlobalConfig()
        assert cfg.position_adjust_cooldown == 5.0
        cfg.position_adjust_cooldown = 12.5
        restored = GlobalConfig.from_dict(cfg.to_dict())
        assert restored.position_adjust_cooldown == 12.5

    def test_backward_compat_missing_key(self):
        restored = GlobalConfig.from_dict({})
        assert restored.position_adjust_cooldown == 5.0

    @pytest.mark.parametrize("garbage", ["abc", None, [], {}, -5, "-1"])
    def test_garbage_falls_back_to_default(self, garbage):
        restored = GlobalConfig.from_dict({"position_adjust_cooldown": garbage})
        assert restored.position_adjust_cooldown == 5.0

    def test_numeric_string_accepted(self):
        restored = GlobalConfig.from_dict({"position_adjust_cooldown": "3.5"})
        assert restored.position_adjust_cooldown == 3.5

    def test_zero_is_valid_disable_value(self):
        restored = GlobalConfig.from_dict({"position_adjust_cooldown": 0})
        assert restored.position_adjust_cooldown == 0.0

    # ── monkey：極端數值不能讓網格永久停擺 ──

    @pytest.mark.parametrize("evil", ["inf", float("inf"), "nan", float("nan"), 1e308 * 10])
    def test_non_finite_falls_back_to_default(self, evil):
        restored = GlobalConfig.from_dict({"position_adjust_cooldown": evil})
        assert restored.position_adjust_cooldown == 5.0

    def test_bool_true_treated_as_number_not_crash(self):
        # bool 是 int 子類：True → 1.0，可接受，重點是不炸
        restored = GlobalConfig.from_dict({"position_adjust_cooldown": True})
        assert restored.position_adjust_cooldown == 1.0


class TestOrderGuardMonkey:
    @pytest.mark.asyncio
    async def test_client_order_id_within_binance_36_char_limit(self):
        bot = _make_bot()
        bot._order_seq = 10**9  # 模擬長期運行後的序號
        await bot.place_order(SYM, "buy", 600.0, 1.0)
        params = bot.exchange.create_order.call_args.kwargs.get("params")
        assert len(params["clientOrderId"]) <= 36

    @pytest.mark.asyncio
    async def test_huge_fail_count_does_not_overflow(self):
        bot = _make_bot()
        bot.exchange.create_order.side_effect = Exception("boom")
        bot._order_fail_counts[SYM] = 10**6  # 遠超閾值
        await bot.place_order(SYM, "buy", 600.0, 1.0)  # 不應 OverflowError
        block = bot._order_block_until[SYM] - _time.time()
        assert block == pytest.approx(ORDER_CIRCUIT_COOLDOWN, abs=1.0)

    @pytest.mark.asyncio
    async def test_failure_of_reduce_only_still_registers_backoff(self):
        """止盈單失敗也累積退避狀態（但下次 reduce_only 仍放行）"""
        bot = _make_bot()
        bot.exchange.create_order.side_effect = Exception("boom")
        await bot.place_order(SYM, "sell", 610.0, 1.0, reduce_only=True, position_side="long")
        assert bot._order_fail_counts[SYM] == 1
        # reduce_only 不受封鎖，重試仍會打到交易所
        await bot.place_order(SYM, "sell", 610.0, 1.0, reduce_only=True, position_side="long")
        assert bot.exchange.create_order.call_count == 2
