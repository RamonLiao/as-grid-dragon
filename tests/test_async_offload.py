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
        assert await bot._rest(lambda a, b=0: a + b, 1, b=2) == 3

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
        await bot._rest(_time.sleep, 0.3)
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

        await asyncio.gather(*[bot._rest(work) for _ in range(5)])
        assert peak[0] == 1

    @pytest.mark.asyncio
    async def test_rest_propagates_exception(self):
        bot = _make_bot()

        def boom():
            raise ValueError("x")

        with pytest.raises(ValueError):
            await bot._rest(boom)
