"""request_reconnect() 行為測試。

重點在「設旗標 + 內層迴圈自行 break」而不是從外部關 socket 或拋例外——
ws_client.py 開頭的 characterization 註解鎖定了「例外冒泡 = 重連」這條不變式，
借用它會讓「handler 出錯」與「watchdog 故意觸發」無法區分。
"""
import asyncio
import pytest

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
