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

        # watchdog 觸發的重連請求：只設旗標，由 run() 內層迴圈自行 break。
        # 不從外部關 socket、也不對 run() 拋例外——後者會污染本檔開頭
        # characterization 註解鎖定的「例外冒泡 = 重連」語意。
        self._reconnect_requested = False

    def _fetch_listen_key(self) -> str:
        response = self.ctx.exchange.fapiPrivatePostListenKey()
        return response.get("listenKey")

    async def acquire_listen_key(self):
        self.listen_key = await self.gateway.call(self._fetch_listen_key)

    def request_reconnect(self):
        """請求下一次迴圈檢查時斷開重連（最壞延遲 = recv timeout 30s）。"""
        self._reconnect_requested = True

    def _consume_reconnect_request(self) -> bool:
        """讀取並清除旗標。一次性語意：清不掉會變成永久重連迴圈。"""
        if self._reconnect_requested:
            self._reconnect_requested = False
            return True
        return False

    async def run(self):
        ssl_context = ssl.create_default_context(cafile=certifi.where())

        while not self._stop_event.is_set():
            try:
                async with websockets.connect(self.config.websocket_url, ssl=ssl_context) as ws:
                    # 陳舊旗標清零（見 dual-review C3）：旗標若在「連線之外」被設起
                    # （例如上一條連線已因例外斷開、或 watchdog 在重連空窗期呼叫），
                    # 它會存活到新連線，在第一則訊息之後立刻再斷一次 ⇒ 一次請求換
                    # 兩次重連（多一次 decide() 盲窗）。新連線讓先前的重連請求失去意義。
                    self._reconnect_requested = False
                    self.state.connected = True

                    # listenKey 在 WS 斷線後會被伺服器廢棄 ⇒ 每次（重）連都必須重新取得。
                    # 舊碼只在啟動時 acquire 一次，重連時沿用死 key，而 Binance 對無效的
                    # stream name **靜默接受**（SUBSCRIBE 不回錯、只是永遠收不到資料）
                    # ⇒ userData 永久失效，唯一線索是 keepalive 每 30 分鐘的 -1125。
                    # 2026-07-25~30 實測：整份 log 零筆 [userData] 事件，而同期成交上百筆。
                    try:
                        await self.acquire_listen_key()
                    except Exception as e:
                        # 取不到就沿用舊 key 降級運行：**不要讓 userData 的失敗連坐 bookTicker**，
                        # 後者是 decide() 的觸發來源，斷了整個引擎就停擺。
                        logger.warning(f"[WebSocket] 重連時取得 listenKey 失敗，沿用舊值: {e}")

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

                        if self._consume_reconnect_request():
                            logger.warning("[WebSocket] 收到重連請求，主動斷開重連")
                            self.state.connected = False
                            break
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
                # PUT 失敗（實測恆為 -1125 "This listenKey does not exist"）代表伺服器端
                # 已經沒有這把 key。舊碼只 log 就繼續，self.listen_key 因此停在死值直到
                # 人工重啟——實測空轉了 5 天。這裡立刻重建。
                # ⚠️ 重建只讓**下次重連**訂到活的 key；當前這條連線仍訂在舊 key 上，
                #    本迴圈無法主動觸發重連（run() 阻塞在 ws.recv()）。
                try:
                    await self.acquire_listen_key()
                    logger.info("[WebSocket] 已重新取得 listenKey（下次重連生效）")
                except Exception as e2:
                    logger.error(f"重新取得 listenKey 也失敗: {e2}")
