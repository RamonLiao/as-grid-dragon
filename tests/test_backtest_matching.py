"""限價單撮合純層：用 bar 的 high/low 判穿越（非 close）。

回測前身用 close 判穿越，在 low 刺穿但 close 未穿越的 K 線上漏掉成交。
真實 1m K 線實測漏掉 48.5% 的多頭進場成交。
見 spec G4。
"""
import pytest

from backtest.matching import entry_crossed, tp_crossed


# ── 多頭進場：買單掛在下方，low 觸及即成交 ──────────────────────────

def test_long_entry_fills_when_low_pierces_even_if_close_stays_above():
    """核心回歸：下影線刺穿掛單價就該成交，不需要收盤站上去。
    舊實作用 close 判斷，這根 K 線會被漏掉。"""
    assert entry_crossed("long", bar_low=98.0, bar_high=101.0, limit=99.0) is True


def test_long_entry_does_not_fill_when_bar_never_reaches_limit():
    assert entry_crossed("long", bar_low=99.5, bar_high=101.0, limit=99.0) is False


def test_long_entry_fills_on_exact_touch():
    """掛單價 == bar 最低價：限價單在該價位可成交（保守但符合 maker 語意）。"""
    assert entry_crossed("long", bar_low=99.0, bar_high=101.0, limit=99.0) is True


# ── 空頭進場：賣單掛在上方，high 觸及即成交 ──────────────────────────

def test_short_entry_fills_when_high_pierces_even_if_close_stays_below():
    assert entry_crossed("short", bar_low=99.0, bar_high=102.0, limit=101.0) is True


def test_short_entry_does_not_fill_when_bar_never_reaches_limit():
    assert entry_crossed("short", bar_low=99.0, bar_high=100.5, limit=101.0) is False


def test_short_entry_fills_on_exact_touch():
    assert entry_crossed("short", bar_low=99.0, bar_high=101.0, limit=101.0) is True


# ── 止盈：方向與進場相反 ─────────────────────────────────────────────

def test_long_tp_fills_when_high_reaches_it():
    """多頭止盈是賣單、掛在上方 → 看 high。"""
    assert tp_crossed("long", bar_low=99.0, bar_high=102.0, limit=101.0) is True


def test_long_tp_does_not_fill_when_high_falls_short():
    assert tp_crossed("long", bar_low=99.0, bar_high=100.5, limit=101.0) is False


def test_short_tp_fills_when_low_reaches_it():
    """空頭止盈是買單、掛在下方 → 看 low。"""
    assert tp_crossed("short", bar_low=98.0, bar_high=101.0, limit=99.0) is True


def test_short_tp_does_not_fill_when_low_falls_short():
    assert tp_crossed("short", bar_low=99.5, bar_high=101.0, limit=99.0) is False


# ── monkey：極端輸入不得崩潰 ────────────────────────────────────────

@pytest.mark.parametrize("side", ["long", "short"])
def test_zero_and_equal_bounds_do_not_raise(side):
    assert isinstance(entry_crossed(side, 0.0, 0.0, 0.0), bool)
    assert isinstance(tp_crossed(side, 100.0, 100.0, 100.0), bool)
