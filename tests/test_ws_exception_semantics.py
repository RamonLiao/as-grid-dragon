"""WS 例外語意 characterization（spec I4，等價陷阱）：
- ticker handler 例外 → 冒泡到重連迴圈 → connected=False + 重連（現行為）
- account/order handler 自帶 try 吞例外 → 不觸發重連
WsClient 不得用 try 包 callback，否則 ticker 語意被改。"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from grid_engine.ws_client import WsClient
from grid_engine.context import ExchangeContext
from grid_engine.rest_gateway import RestGateway


class _FakeWs:
    """吐一則訊息後永遠 pending 的假 websocket"""
    def __init__(self, messages):
        self._messages = list(messages)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, _):
        pass

    async def recv(self):
        if self._messages:
            return self._messages.pop(0)
        await asyncio.sleep(3600)

    async def ping(self):
        pass


def _make_client(handlers, stop_event, state):
    cfg = MagicMock()
    cfg.websocket_url = "wss://x"
    cfg.symbols = {}
    return WsClient(gateway=RestGateway(), ctx=ExchangeContext(), config=cfg,
                    state=state, stop_event=stop_event, handlers=handlers)


def test_ticker_handler_exception_triggers_reconnect():
    stop = asyncio.Event()
    state = MagicMock()
    boom = AsyncMock(side_effect=RuntimeError("handler bug"))
    client = _make_client({"bookTicker": boom}, stop, state)

    msg = json.dumps({"e": "bookTicker", "s": "X", "b": "1", "a": "2"})
    connect_calls = []
    connected_at_reconnect = []

    def fake_connect(*a, **kw):
        connect_calls.append(1)
        if len(connect_calls) >= 2:
            # 重連當下 outer except 必須已把 connected 拉下來
            # （重連成功後會再設回 True，故在此時點取樣而非測試尾端斷言）
            connected_at_reconnect.append(state.connected)
            stop.set()          # 第二次連線 = 已重連，收工
        return _FakeWs([msg])

    async def main():
        with patch("grid_engine.ws_client.websockets.connect", side_effect=fake_connect), \
             patch("grid_engine.ws_client.asyncio.sleep", new=AsyncMock()):
            await asyncio.wait_for(client.run(), timeout=5)

    asyncio.run(main())
    assert boom.await_count >= 1
    assert len(connect_calls) >= 2               # 例外導致重連
    assert connected_at_reconnect == [False]     # outer except 有把 connected 拉下來


def test_stop_event_exits_run_loop():
    stop = asyncio.Event()
    stop.set()
    client = _make_client({}, stop, MagicMock())

    async def main():
        await asyncio.wait_for(client.run(), timeout=2)   # 立即返回，不連線

    asyncio.run(main())
