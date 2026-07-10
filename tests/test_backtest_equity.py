"""G8：權益核算必須包含未平倉位鎖住的 margin。

_open() 把 margin 從 balance 扣除並存進倉位，_close() 才加回。
equity = balance + unrealized 漏了 + sum(open margin)。
⇒ 只要有未平倉位，final_equity 系統性低估、max_drawdown 系統性虛增，
   偏誤幅度與持倉規模成正比。這直接命中 spec §7 欽定的兩個主指標。
見 spec 缺口 G8。
"""
import pandas as pd
import pytest

from backtest.backtester import GridBacktester
from backtest.config import Config


def _flat_df(prices):
    return pd.DataFrame({
        "open_time": pd.date_range("2024-01-01", periods=len(prices), freq="1min"),
        "open": prices, "high": prices, "low": prices, "close": prices,
        "volume": [100.0] * len(prices),
    })


def _zero_cost_cfg(**kw):
    base = dict(symbol="BNBUSDC", initial_balance=1000.0, initial_quantity=0.5,
                leverage=10, grid_spacing=0.006, take_profit_spacing=0.004,
                direction="long", terminal_ui_mode=True,
                fee_pct=0.0, slippage_bps=0.0, funding_enabled=False)
    base.update(kw)
    return Config(**base)


def test_final_equity_includes_margin_locked_in_open_positions():
    """G-0b0：零成本下，final_equity 必須 == 本金 + 已實現 + 未實現。

    單調下跌讓多頭一路開倉（不止盈），末根收盤價回到起點。
    修正前實測：final_equity=988.2，正確值 1007.5，缺口 19.3
    （= 4 張未平倉位的 margin，每張 ≈ price*0.5/10）。
    """
    prices = [100.0] * 3 + [99.0, 98.0, 97.0, 96.0, 95.0] + [100.0]
    res = GridBacktester(_flat_df(prices), _zero_cost_cfg()).run()

    expected = 1000.0 + res.realized_pnl + res.unrealized_pnl
    assert res.final_equity == pytest.approx(expected, abs=1e-6), (
        f"final_equity={res.final_equity} != 本金+已實現+未實現={expected}；"
        f"缺口 {expected - res.final_equity}（未平倉位鎖住的 margin）"
    )


def test_equity_curve_never_dips_below_balance_plus_unrealized():
    """equity_curve 的每一點也要含 open margin —— max_drawdown 從它算出來。"""
    prices = [100.0] * 3 + [99.0, 98.0, 97.0, 96.0, 95.0]
    res = GridBacktester(_flat_df(prices), _zero_cost_cfg()).run()
    # 全程未平倉、價格單調下跌 → 權益最低點不該低於「本金 - 未實現虧損」
    worst_equity = min(e[2] for e in res.equity_curve)
    assert worst_equity > 900.0, (
        f"權益曲線最低點 {worst_equity} 過低，疑似漏算 open margin"
    )


def test_flat_price_no_position_equity_equals_initial_balance():
    """負向對照：沒開過倉時，權益恆等於本金（修正前後都該成立）。"""
    res = GridBacktester(_flat_df([100.0] * 5), _zero_cost_cfg(direction="long",
                                                               grid_spacing=0.5)).run()
    assert res.final_equity == pytest.approx(1000.0, abs=1e-9)
