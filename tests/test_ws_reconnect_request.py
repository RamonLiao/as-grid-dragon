"""request_reconnect() 行為測試。

重點在「設旗標 + 內層迴圈自行 break」而不是從外部關 socket 或拋例外——
ws_client.py 開頭的 characterization 註解鎖定了「例外冒泡 = 重連」這條不變式，
借用它會讓「handler 出錯」與「watchdog 故意觸發」無法區分。
"""
import asyncio
import json

import pytest

from grid_engine import ws_client as ws_client_module
from grid_engine.ws_client import WsClient


def make_client():
    return WsClient(gateway=None, ctx=None, config=None, state=None,
                    stop_event=asyncio.Event(), handlers={})


def test_flag_defaults_false():
    assert make_client()._reconnect_requested is False


def test_request_sets_flag():
    c = make_client()
    c.request_reconnect()
    assert c._reconnect_requested is True


def test_consume_clears_flag():
    """旗標必須是一次性的，否則會變成永久重連迴圈。"""
    c = make_client()
    c.request_reconnect()
    assert c._consume_reconnect_request() is True
    assert c._reconnect_requested is False
    assert c._consume_reconnect_request() is False


def test_request_is_idempotent():
    c = make_client()
    c.request_reconnect()
    c.request_reconnect()
    assert c._consume_reconnect_request() is True
    assert c._consume_reconnect_request() is False


class _FakeState:
    """記錄 connected 的每一次寫入，用來驗證 break 路徑有沒有把它切回 False。"""

    def __init__(self):
        self.history = []
        self._connected = None

    @property
    def connected(self):
        return self._connected

    @connected.setter
    def connected(self, value):
        self._connected = value
        self.history.append(value)


class _FakeConfig:
    websocket_url = "wss://fake-for-test"
    symbols = {}


class _FakeWs:
    """假 async context manager：recv() 在第一條連線上觸發 request_reconnect()，
    在第二條連線上直接收尾（set stop_event），藉此把 run() 逼出 while True。"""

    def __init__(self, conn_no, client, stop_event):
        self._conn_no = conn_no
        self._client = client
        self._stop_event = stop_event

    async def recv(self):
        if self._conn_no == 1:
            self._client.request_reconnect()
        else:
            self._stop_event.set()
        return json.dumps({"e": "unknown"})

    async def send(self, msg):
        pass

    async def ping(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


def test_run_break_reconnects_without_touching_exception_path(monkeypatch):
    """整合行為守衛：watchdog 觸發的重連走 break，不走 characterization 鎖定的
    「例外冒泡」路徑，而且必須在斷線瞬間把 state.connected 切回 False。

    這條測試專門守兩件事（對應 review 的兩個 Important）：
    1. `self.state.connected = False` 沒有被刪掉（否則 UI 在重連窗口期仍顯示已連線）。
    2. `break` 沒有被改成 `raise`（否則會借用 outer except 的「例外冒泡=重連」語意，
       跟本 task 存在的理由矛盾）。
    """
    state = _FakeState()
    stop_event = asyncio.Event()
    client = WsClient(gateway=None, ctx=None, config=_FakeConfig(), state=state,
                       stop_event=stop_event, handlers={})

    connect_calls = {"n": 0}

    def fake_connect(url, ssl=None):
        connect_calls["n"] += 1
        return _FakeWs(connect_calls["n"], client, stop_event)

    monkeypatch.setattr(ws_client_module.websockets, "connect", fake_connect)

    errors = []
    monkeypatch.setattr(ws_client_module.logger, "error", lambda msg: errors.append(msg))

    sleep_calls = []

    async def fake_sleep(secs):
        sleep_calls.append(secs)

    monkeypatch.setattr(ws_client_module.asyncio, "sleep", fake_sleep)

    asyncio.run(client.run())

    # 重連確實發生了兩次 connect（第一次被旗標打斷，第二次靠 stop_event 收尾）。
    assert connect_calls["n"] == 2
    # 沒有經過 outer except：既沒有 log 錯誤，也沒有觸發 5 秒退避 sleep。
    assert errors == []
    assert 5 not in sleep_calls
    # connected 的完整軌跡：連上 → watchdog 觸發斷線切 False → 重連再切回 True。
    assert state.history == [True, False, True]
