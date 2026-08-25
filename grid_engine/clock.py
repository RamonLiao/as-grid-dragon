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


# --- 守衛專用時鐘（與 now() 刻意分離）---------------------------------------
#
# now() 是「情境時鐘」：backtester 每根 K 線用 set_clock() 把它換成歷史 epoch。
# 守衛量的是另一個物理量——「訊息實際抵達本機的牆鐘時間」。原設計把兩者混為
# 一談是分類錯誤，分開不是繞路，是修正分類。

_guard_now_fn: Callable[[], float] = time.time


def guard_now() -> float:
    """守衛專用時鐘：量「訊息實際抵達本機的牆鐘時間」。

    刻意與 now() 分開：now() 是情境時鐘，backtester 每根 K 線用 set_clock()
    (backtest/backtester.py:715) 把它換成歷史 epoch；而 live bot 與回測跑在
    同一個行程（as_terminal_max.py:1265 的 daemon thread）。若守衛共用 now()，
    使用者一邊實盤一邊點回測就會讓 quote_age 變成巨大負數，使守衛對每個
    symbol、每個 tick 觸發、全面停止下單（含成交後的止盈補單），而持倉持續
    累積——唯一訊號只有每 symbol 每小時一筆 throttled warning。

    本函式不得被 backtester 替換；set_guard_clock() 只給測試注入用。
    """
    return _guard_now_fn()


def set_guard_clock(fn: Callable[[], float]) -> None:
    """僅供測試注入假的守衛時鐘。生產程式碼與回測皆不得呼叫。"""
    global _guard_now_fn
    _guard_now_fn = fn


def reset_guard_clock() -> None:
    global _guard_now_fn
    _guard_now_fn = time.time
