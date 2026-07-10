"""G6：強平建模。

回測前身沒有強平：_open() 保證金不足時只 return False，倉位永不被平，
equity 可為負而回測照跑到底 ⇒「無限加倉 + 不爆倉」是算術上的必勝策略。
選項 (b)「關掉裝死模式」的全部風險都在這裡，沒有強平就無法評估。
見 spec 缺口 G6、守門 G-0b1 / G-0b2。
"""
import pandas as pd
import pytest

from backtest.backtester import GridBacktester
from backtest.config import Config
from backtest.liquidation import should_liquidate


def _flat_df(prices):
    return pd.DataFrame({
        "open_time": pd.date_range("2024-01-01", periods=len(prices), freq="1min"),
        "open": prices, "high": prices, "low": prices, "close": prices,
        "volume": [100.0] * len(prices),
    })


def _cfg(**kw):
    base = dict(symbol="BNBUSDC", initial_balance=100.0, initial_quantity=0.5,
                leverage=20, grid_spacing=0.002, take_profit_spacing=0.5,
                direction="long", terminal_ui_mode=True,
                fee_pct=0.0, slippage_bps=0.0, funding_enabled=False,
                threshold_multiplier=1e9)   # 裝死永不觸發 → 無限加倉
    base.update(kw)
    return Config(**base)


# ── 純層 ─────────────────────────────────────────────────────────────

def test_should_liquidate_when_equity_below_maintenance_margin():
    # 名目 = 10 * 100 = 1000；維持保證金 = 1000 * 0.005 = 5
    assert should_liquidate(equity=4.0, long_pos=10.0, short_pos=0.0,
                            price=100.0, maintenance_margin_rate=0.005) is True


def test_should_not_liquidate_when_equity_above_maintenance_margin():
    assert should_liquidate(equity=6.0, long_pos=10.0, short_pos=0.0,
                            price=100.0, maintenance_margin_rate=0.005) is False


def test_should_liquidate_when_equity_negative():
    assert should_liquidate(equity=-1.0, long_pos=1.0, short_pos=0.0,
                            price=100.0, maintenance_margin_rate=0.005) is True


def test_no_position_never_liquidates():
    """沒有倉位就沒有維持保證金需求，權益再低也不強平。"""
    assert should_liquidate(equity=0.01, long_pos=0.0, short_pos=0.0,
                            price=100.0, maintenance_margin_rate=0.005) is False


# ── 整合 ─────────────────────────────────────────────────────────────

def test_relentless_downtrend_with_no_dead_mode_triggers_liquidation():
    """G-0b1：單邊崩盤 + 裝死關閉 + 高槓桿 → 必爆，且回測提前終止。

    這正是選項 (b)「關掉裝死模式」的尾部風險。沒有強平建模時，回測會讓
    倉位無限累積、equity 變負而照跑到底，於是 optimizer 誤判 (b) 最好。
    """
    prices = [100.0] + [100.0 * (0.99 ** i) for i in range(1, 400)]
    res = GridBacktester(_flat_df(prices), _cfg()).run()
    assert res.liquidated is True
    # 提前終止：權益曲線長度應短於 K 線數
    assert len(res.equity_curve) < len(prices)


def test_normal_range_bound_market_does_not_liquidate():
    """G-0b2：正常震盪 + 充足本金 → liquidated 必須是 False。"""
    prices = [100.0, 99.6, 100.2, 99.8, 100.4, 99.9, 100.1]
    res = GridBacktester(_flat_df(prices),
                         _cfg(initial_balance=100000.0, leverage=5,
                              threshold_multiplier=20.0)).run()
    assert res.liquidated is False


def test_liquidation_flag_defaults_false():
    res = GridBacktester(_flat_df([100.0] * 5),
                         _cfg(initial_balance=100000.0, grid_spacing=0.5)).run()
    assert res.liquidated is False
