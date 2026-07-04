"""可注入時鐘。實盤預設真實牆鐘（零行為差異）；回測以 set_clock 餵 K 線時間，
讓 ATR 快取 / volume 窗口 / funding 更新間隔在回放時間軸下真正有效。"""
import time
from typing import Callable

_now_fn: Callable[[], float] = time.time


def now() -> float:
    return _now_fn()


def set_clock(fn: Callable[[], float]) -> None:
    global _now_fn
    _now_fn = fn


def reset_clock() -> None:
    global _now_fn
    _now_fn = time.time
