"""WS 純傳輸組件：連線/訂閱/重連/listenKey 續期。

例外語意（characterization 鎖定）：handler callback 例外「必須」冒泡到本迴圈
的 outer except → connected=False + sleep 5s + 重連。不得用 try 包 callback。
"""
import asyncio
import json
import ssl
from typing import Callable, Dict, Optional

import certifi
import websockets

from .utils import logger


class WsClient:
    def __init__(self, gateway, ctx, config, state, stop_event: asyncio.Event,
                 handlers: Dict[str, Callable]):
        self.gateway = gateway
        self.ctx = ctx
        self.config = config
        self.state = state
        self._stop_event = stop_event
        self.handlers = handlers
        self.listen_key: Optional[str] = None

    def _fetch_listen_key(self) -> str:
        response = self.ctx.exchange.fapiPrivatePostListenKey()
        return response.get("listenKey")

    async def acquire_listen_key(self):
        self.listen_key = await self.gateway.call(self._fetch_listen_key)

    async def run(self):
        ssl_context = ssl.create_default_context(cafile=certifi.where())

        while not self._stop_event.is_set():
            try:
                async with websockets.connect(self.config.websocket_url, ssl=ssl_context) as ws:
                    self.state.connected = True

                    streams = []
                    for cfg in self.config.symbols.values():
                        if cfg.enabled:
                            streams.append(f"{cfg.ws_symbol}@bookTicker")

                    if streams:
                        await ws.send(json.dumps({"method": "SUBSCRIBE", "params": streams, "id": 1}))

                    if self.listen_key:
                        await ws.send(json.dumps({"method": "SUBSCRIBE", "params": [self.listen_key], "id": 2}))
                        logger.info("[WebSocket] 已訂閱 userData stream")

                    while not self._stop_event.is_set():
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=30)
                            data = json.loads(msg)

                            event_type = data.get('e', '')

                            handler = self.handlers.get(event_type)
                            if handler:
                                await handler(data)

                        except asyncio.TimeoutError:
                            await ws.ping()
            except Exception as e:
                self.state.connected = False
                if not self._stop_event.is_set():
                    logger.error(f"WebSocket 錯誤: {e}")
                    await asyncio.sleep(5)

    async def keep_alive_loop(self):
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(1800)
                if not self._stop_event.is_set():
                    await self.gateway.call(self.ctx.exchange.fapiPrivatePutListenKey)
                    self.listen_key = await self.gateway.call(self._fetch_listen_key)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"更新 listenKey 失敗: {e}")
