"""#2/#3 並發安全測試

核心契約 (2026-07-03 架構審查修復 #2+#3):
  - 所有 ccxt REST 走單 worker executor：不阻塞 event loop、天然序列化。
  - adjust_grid per-symbol 互斥，忙碌時 skip 不排隊。
  - sync_all 防重入；REST apply 為原子區塊。
  - 停機後不再送單，executor 收斂。
"""

import asyncio
import threading
import time as _time

import pytest
from unittest.mock import MagicMock

from grid_engine.bot import MaxGridBot
from grid_engine.config import GlobalConfig


def _make_bot():
    bot = MaxGridBot(GlobalConfig())
    bot.exchange = MagicMock()
    return bot


class TestRestHelper:
    @pytest.mark.asyncio
    async def test_rest_runs_fn_and_returns_result(self):
        bot = _make_bot()
        assert await bot.gateway.call(lambda a, b=0: a + b, 1, b=2) == 3

    @pytest.mark.asyncio
    async def test_rest_does_not_block_event_loop(self):
        """REST 慢呼叫期間 event loop 心跳必須照跳。"""
        bot = _make_bot()
        beats = []

        async def heartbeat():
            for _ in range(4):
                beats.append(_time.monotonic())
                await asyncio.sleep(0.05)

        hb = asyncio.create_task(heartbeat())
        await bot.gateway.call(_time.sleep, 0.3)
        await hb
        gaps = [b - a for a, b in zip(beats, beats[1:])]
        assert max(gaps) < 0.2, f"event loop 被卡住: gaps={gaps}"

    @pytest.mark.asyncio
    async def test_rest_serializes_concurrent_calls(self):
        """單 worker：同一時刻最多 1 個 REST 在跑。"""
        bot = _make_bot()
        active, peak = [0], [0]
        lk = threading.Lock()

        def work():
            with lk:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            _time.sleep(0.05)
            with lk:
                active[0] -= 1

        await asyncio.gather(*[bot.gateway.call(work) for _ in range(5)])
        assert peak[0] == 1

    @pytest.mark.asyncio
    async def test_rest_propagates_exception(self):
        bot = _make_bot()

        def boom():
            raise ValueError("x")

        with pytest.raises(ValueError):
            await bot.gateway.call(boom)


class TestAsyncOrderPath:
    @pytest.mark.asyncio
    async def test_place_order_goes_through_executor(self):
        """create_order 必須在 executor thread 執行，不在 event loop thread。"""
        bot = _make_bot()
        seen = {}

        def fake_create(*a, **kw):
            seen["thread"] = threading.current_thread().name
            return {"id": "1"}

        bot.exchange.create_order = fake_create
        bot.precisions["BNB/USDC:USDC"] = {"price": 2, "amount": 1, "min_amount": 0.1}
        result = await bot.order_executor.place_order("BNB/USDC:USDC", "buy", 600.0, 1.0)
        assert result == {"id": "1"}
        assert seen["thread"].startswith("ccxt-rest")

    @pytest.mark.asyncio
    async def test_place_order_skipped_after_stop(self):
        """停機後 place_order 直接 return None，不打 exchange。"""
        bot = _make_bot()
        bot._stop_event.set()
        assert await bot.order_executor.place_order("BNB/USDC:USDC", "buy", 600.0, 1.0) is None
        bot.exchange.create_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_order_failure_still_backs_off(self):
        """回歸：executor 內拋例外 → 退避計數照常累加。"""
        bot = _make_bot()
        bot.exchange.create_order = MagicMock(side_effect=RuntimeError("boom"))
        assert await bot.order_executor.place_order("BNB/USDC:USDC", "buy", 600.0, 1.0) is None
        assert bot.order_executor._order_fail_counts["BNB/USDC:USDC"] == 1


def _make_synced_bot():
    from grid_engine.config import SymbolConfig
    cfg = GlobalConfig()
    sym_cfg = SymbolConfig(symbol="BNBUSDC")
    sym_cfg.enabled = True
    cfg.symbols["BNBUSDC"] = sym_cfg
    bot = MaxGridBot(cfg)
    bot.exchange = MagicMock()
    bot.exchange.fetch_positions.return_value = []
    bot.exchange.fetch_open_orders.return_value = []
    bot.exchange.fetch_balance.return_value = {"info": {"assets": []}, "total": {}, "free": {}}
    bot.funding_manager = None
    return bot


class TestAsyncSync:
    @pytest.mark.asyncio
    async def test_sync_all_offloads_fetches(self):
        bot = _make_synced_bot()
        threads = set()

        def rec(*a, **kw):
            threads.add(threading.current_thread().name)
            return []

        bot.exchange.fetch_positions = rec
        bot.exchange.fetch_open_orders = rec
        bot.exchange.fetch_balance = MagicMock(
            side_effect=lambda *a, **kw: (threads.add(threading.current_thread().name),
                                          {"info": {"assets": []}, "total": {}, "free": {}})[1])
        await bot.sync_all()
        assert threads and all(t.startswith("ccxt-rest") for t in threads)

    @pytest.mark.asyncio
    async def test_sync_orders_applies_counts(self):
        """回歸：async 化後掛單計數彙總邏輯不變。"""
        bot = _make_synced_bot()
        sym = list(bot.state.symbols)[0]
        bot.exchange.fetch_open_orders.return_value = [
            {"side": "buy", "info": {"origQty": "2", "positionSide": "LONG"}},
            {"side": "sell", "info": {"origQty": "3", "positionSide": "LONG"}},
        ]
        await bot._sync_orders()
        st = bot.state.symbols[sym]
        assert st.buy_long_orders == 2 and st.sell_long_orders == 3


class TestConcurrencyLocks:
    @pytest.mark.asyncio
    async def test_adjust_grid_skips_when_locked(self):
        """同 symbol 並發觸發：一個在跑，其餘直接 skip 不排隊。"""
        bot = _make_synced_bot()
        sym = list(bot.state.symbols)[0]
        st = bot.state.symbols[sym]
        st.latest_price = st.best_bid = st.best_ask = 600.0
        entered = []

        async def slow_step(ccxt_symbol, sym_config):
            entered.append(ccxt_symbol)
            await asyncio.sleep(0.1)

        bot._grid_step = slow_step
        await asyncio.gather(*[bot.adjust_grid(sym) for _ in range(5)])
        assert len(entered) == 1, "adjust_grid 未互斥或發生排隊"

    @pytest.mark.asyncio
    async def test_adjust_grid_lock_released_on_exception(self):
        bot = _make_synced_bot()
        sym = list(bot.state.symbols)[0]
        st = bot.state.symbols[sym]
        st.latest_price = st.best_bid = st.best_ask = 600.0

        async def boom(ccxt_symbol, sym_config):
            raise RuntimeError("x")

        bot._grid_step = boom
        with pytest.raises(RuntimeError):
            await bot.adjust_grid(sym)
        assert not bot.locks.get(sym).locked()

    @pytest.mark.asyncio
    async def test_sync_all_no_reentry(self):
        """sync 進行中再觸發 → 直接 return，fetch 只跑一輪。"""
        bot = _make_synced_bot()
        calls = []

        def slow_fetch(*a, **kw):
            calls.append(1)
            _time.sleep(0.1)
            return []

        bot.exchange.fetch_positions = slow_fetch
        await asyncio.gather(bot.sync_all(), bot.sync_all(), bot.sync_all())
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_sync_apply_waits_for_adjust_lock(self):
        """adjust_grid 持鎖期間，_sync_orders 的 apply 不得插入。"""
        bot = _make_synced_bot()
        sym = list(bot.state.symbols)[0]
        st = bot.state.symbols[sym]
        st.buy_long_orders = 99  # adjust 期間的「決策依據」
        bot.exchange.fetch_open_orders.return_value = []

        lock = bot.locks.get(sym)
        async with lock:  # 模擬 adjust_grid 持鎖中
            sync_task = asyncio.create_task(bot._sync_orders())
            await asyncio.sleep(0.05)
            assert st.buy_long_orders == 99, "sync 在 adjust 持鎖期間改寫了掛單計數"
        await sync_task
        assert st.buy_long_orders == 0  # 釋放後才 apply


class TestMonkey:
    @pytest.mark.asyncio
    async def test_ticker_storm_50_concurrent(self):
        """50 個並發 adjust_grid + 3 個 sync_all 同時轟：不死鎖、不炸例外。"""
        bot = _make_synced_bot()
        sym = list(bot.state.symbols)[0]
        st = bot.state.symbols[sym]
        st.latest_price = st.best_bid = st.best_ask = 600.0
        bot.exchange.create_order.return_value = {"id": "1"}
        await asyncio.wait_for(
            asyncio.gather(
                *[bot.adjust_grid(sym) for _ in range(50)],
                bot.sync_all(), bot.sync_all(), bot.sync_all(),
            ),
            timeout=10,
        )
        assert not bot.locks.get(sym).locked()
        assert not bot._sync_lock.locked()

    @pytest.mark.asyncio
    async def test_rest_exception_storm_releases_all_locks(self):
        """每個 REST 都炸：鎖不得洩漏，之後仍可正常進入。"""
        bot = _make_synced_bot()
        sym = list(bot.state.symbols)[0]
        st = bot.state.symbols[sym]
        st.latest_price = st.best_bid = st.best_ask = 600.0
        for m in ("fetch_positions", "fetch_open_orders", "fetch_balance",
                  "create_order", "cancel_order"):
            setattr(bot.exchange, m, MagicMock(side_effect=RuntimeError("boom")))
        await asyncio.gather(
            *[bot.adjust_grid(sym) for _ in range(10)],
            bot.sync_all(),
            return_exceptions=True,
        )
        assert not bot.locks.get(sym).locked()
        assert not bot._sync_lock.locked()
        # 復原後可再進入
        bot.exchange.fetch_positions = MagicMock(return_value=[])
        bot.exchange.fetch_open_orders = MagicMock(return_value=[])
        bot.exchange.fetch_balance = MagicMock(
            return_value={"info": {"assets": []}, "total": {}, "free": {}})
        await bot.sync_all()

    @pytest.mark.asyncio
    async def test_stop_mid_flight(self):
        """REST in-flight 時 stop：不掛死、事後不再送單。"""
        bot = _make_synced_bot()
        bot.exchange.create_order = MagicMock(
            side_effect=lambda *a, **kw: (_time.sleep(0.2), {"id": "1"})[1])
        bot.precisions[list(bot.state.symbols)[0]] = {"price": 2, "amount": 1, "min_amount": 0.1}
        sym = list(bot.state.symbols)[0]
        order_task = asyncio.create_task(bot.order_executor.place_order(sym, "buy", 600.0, 1.0))
        await asyncio.sleep(0.05)
        await asyncio.wait_for(bot.stop(), timeout=5)
        await asyncio.wait_for(order_task, timeout=5)
        calls_after_stop = bot.exchange.create_order.call_count
        assert (await bot.order_executor.place_order(sym, "buy", 600.0, 1.0)) is None
        assert bot.exchange.create_order.call_count == calls_after_stop

    @pytest.mark.asyncio
    async def test_run_init_failure_shuts_down_executor(self):
        """回歸：run() 初始化失敗 early-return 前必須 shutdown executor，否則長駐 app 每次失敗啟動洩漏一條 thread。"""
        bot = _make_bot()
        bot._init_exchange = MagicMock(side_effect=RuntimeError("boom"))
        await bot.run()
        with pytest.raises(RuntimeError):
            bot.gateway._executor.submit(lambda: None)

