import pandas as pd
from datetime import datetime, timedelta
from backtest.config import Config
from backtest.backtester import GridBacktester


def _make_df(prices, start=datetime(2001, 9, 9)):
    return pd.DataFrame({
        "open_time": [start + timedelta(minutes=i) for i in range(len(prices))],
        "open": prices, "high": prices, "low": prices,
        "close": prices, "volume": [1.0] * len(prices),
    })


def _cfg(**kw):
    base = dict(symbol="BTCUSDC", initial_balance=100000.0, initial_quantity=1.0,
                leverage=20, take_profit_spacing=0.004, grid_spacing=0.006,
                direction="long", slippage_bps=0.0, funding_enabled=True)
    base.update(kw)
    return Config(**base)


def test_funding_charged_when_settlement_crossed():
    # 讓第一根就進場並持倉，第 3 根時間點命中一個 settlement
    prices = [100.0, 99.0, 99.0, 99.0]
    df = _make_df(prices)
    third_epoch = int(df["open_time"].iloc[2].timestamp())
    fmap = {third_epoch: 0.0001}
    r = GridBacktester(df, _cfg(), funding_map=fmap).run()
    assert r.funding_paid > 0  # 多頭持倉 + 正 rate → 付款


def test_funding_off_zero_paid():
    prices = [100.0, 99.0, 99.0, 99.0]
    df = _make_df(prices)
    third_epoch = int(df["open_time"].iloc[2].timestamp())
    r = GridBacktester(df, _cfg(funding_enabled=False), funding_map={third_epoch: 0.0001}).run()
    assert r.funding_paid == 0.0


def test_funding_does_not_pollute_trade_metrics():
    # 含 funding vs 無 funding：trades_count/win_rate/profit_factor 不變
    prices = [100.0, 99.0, 100.5, 99.5, 100.8, 99.2, 101.0, 99.0]
    df = _make_df(prices)
    epochs = {int(df["open_time"].iloc[i].timestamp()): 0.0001 for i in (2, 5)}
    with_f = GridBacktester(df.copy(), _cfg(), funding_map=epochs).run()
    no_f = GridBacktester(df.copy(), _cfg(funding_enabled=False), funding_map=epochs).run()
    assert with_f.trades_count == no_f.trades_count
    assert with_f.win_rate == no_f.win_rate
    assert with_f.profit_factor == no_f.profit_factor


def test_funding_multiple_settlements_in_one_bar():
    # 兩個 settlement 都 <= 某根 bar epoch → 都結算
    prices = [100.0, 99.0, 99.0]
    df = _make_df(prices)
    e1 = int(df["open_time"].iloc[1].timestamp())
    e_mid = e1 + 5  # 落在 bar1 與 bar2 之間，bar2 時一起結
    e2 = int(df["open_time"].iloc[2].timestamp())
    r = GridBacktester(df, _cfg(), funding_map={e1: 0.0001, e_mid: 0.0001}).run()
    # 兩筆都被結算（近似 notional 相同）→ 大於單筆
    r1 = GridBacktester(df, _cfg(), funding_map={e1: 0.0001}).run()
    assert r.funding_paid > r1.funding_paid
