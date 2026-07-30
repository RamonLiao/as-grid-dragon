"""listenKey 必須在每次（重）連時重新取得，並在 keepalive 失敗時自我修復。

2026-07-30 實測到的生產缺陷：`acquire_listen_key()` 只在 bot 啟動時呼叫一次，
`run()` 重連時沿用舊值。WS 斷線後伺服器廢棄該 key，而 Binance 對無效 stream name
**靜默接受**（SUBSCRIBE 不回錯，只是永遠收不到資料）⇒ userData stream 永久失效，
唯一線索是 keepalive 每 30 分鐘的 `-1125`。整份 log（07-12~07-30）零筆 `[userData]`
事件，而同期成交上百筆——成交推送全程沒在工作，靠 10s REST sync 兜底。

三條守衛：
1. 每次連上都重新 acquire（不是只在啟動時）
2. acquire 失敗不得連坐 bookTicker（後者是 decide() 的觸發來源）
3. keepalive 失敗時重建 key，不是只 log
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from grid_engine.ws_client import WsClient
from grid_engine.context import ExchangeContext
from grid_engine.rest_gateway import RestGateway


class _FakeWs:
    def __init__(self, sent):
        self._sent = sent

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, payload):
        self._sent.append(json.loads(payload))

    async def recv(self):
        raise ConnectionError("斷線")   # 立刻觸發外層 except → 重連

    async def ping(self):
        pass


def _make_client(stop_event, state, symbols=None):
    cfg = MagicMock()
    cfg.websocket_url = "wss://x"
    cfg.symbols = symbols if symbols is not None else {}
    return WsClient(gateway=RestGateway(), ctx=ExchangeContext(), config=cfg,
                    state=state, stop_event=stop_event, handlers={})


def _run_n_reconnects(client, stop, n, sent):
    """讓 run() 跑 n 次連線後停止。"""
    calls = []

    def fake_connect(*a, **kw):
        calls.append(1)
        if len(calls) >= n:
            stop.set()
        return _FakeWs(sent)

    async def drive():
        with patch("grid_engine.ws_client.websockets.connect", side_effect=fake_connect), \
             patch("asyncio.sleep", new=AsyncMock()):
            await client.run()

    asyncio.run(drive())
    return len(calls)


def test_listen_key_is_reacquired_on_every_reconnect():
    """核心 regression：三次連線 ⇒ 三次 acquire，不是只有第一次。"""
    stop = asyncio.Event()
    client = _make_client(stop, MagicMock())
    keys = iter(["key_1", "key_2", "key_3", "key_4"])

    async def fake_acquire():
        client.listen_key = next(keys)

    client.acquire_listen_key = fake_acquire
    sent = []
    n = _run_n_reconnects(client, stop, 3, sent)

    assert n == 3
    subscribed = [m["params"][0] for m in sent if m.get("id") == 2]
    assert subscribed == ["key_1", "key_2", "key_3"], (
        f"每次重連都必須訂閱新取得的 key，實際 {subscribed}——"
        "若是 ['key_1','key_1','key_1'] 代表沿用了啟動時那把（就是生產的 bug）")


def test_acquire_failure_does_not_break_ticker_subscription():
    """取 key 失敗只能降級 userData，不得連坐 bookTicker——那會讓引擎完全停擺。"""
    stop = asyncio.Event()
    sym = MagicMock()
    sym.enabled = True
    sym.ws_symbol = "bnbusdc"
    client = _make_client(stop, MagicMock(), symbols={"BNB/USDC:USDC": sym})
    client.listen_key = None

    async def boom():
        raise RuntimeError("REST 掛了")

    client.acquire_listen_key = boom
    sent = []
    _run_n_reconnects(client, stop, 1, sent)

    ticker_subs = [m for m in sent if m.get("id") == 1]
    assert ticker_subs and ticker_subs[0]["params"] == ["bnbusdc@bookTicker"], (
        "listenKey 取得失敗時，bookTicker 訂閱仍必須送出")


def test_keepalive_failure_rebuilds_listen_key():
    """PUT 失敗（-1125）後必須重建 key，不能只 log 一行就繼續空轉。

    ⚠️ 迴圈的終止條件刻意掛在**每輪必經的 `asyncio.sleep`**，不是掛在 `acquire_listen_key`
    或 `gateway.call` 上。理由：前者正是被測行為，後者在迴圈裡的執行位置會隨實作改變——
    任何一種都會讓「移除重建」的 mutation 變成**無限迴圈（hang）而不是失敗**。
    hang 的測試在 CI 上是 timeout 不是 red，鑑別力等於零（這是實際踩到後才改的寫法）。
    """
    stop = asyncio.Event()
    client = _make_client(stop, MagicMock())
    client.listen_key = "dead_key"
    rebuilt = []

    async def fake_acquire():
        rebuilt.append(1)
        client.listen_key = "fresh_key"

    client.acquire_listen_key = fake_acquire

    async def fake_call(fn, *a, **kw):
        raise RuntimeError('binance {"code":-1125,"msg":"This listenKey does not exist."}')

    client.gateway.call = fake_call

    rounds = []

    async def fake_sleep(_):
        rounds.append(1)
        if len(rounds) >= 3:     # 每輪必經 ⇒ 無論實作怎麼改都保證終止
            stop.set()

    async def drive():
        with patch("grid_engine.ws_client.asyncio.sleep", new=fake_sleep):
            await client.keep_alive_loop()

    asyncio.run(drive())

    assert rebuilt, "keepalive 失敗後必須呼叫 acquire_listen_key 重建"
    assert client.listen_key == "fresh_key", (
        f"重建後 listen_key 必須換成新值，實際 {client.listen_key}——"
        "停在 dead_key 就是生產那條空轉 5 天的路徑")
