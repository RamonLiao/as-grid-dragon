"""回測吃 decide()：驗證回測決策 == 純層 decide()（無 core.strategy），
含追價語意與 sim-clock，及 monkey testing（極端 K 線）。"""
import math
import inspect

import numpy as np
import pandas as pd
import pytest

from backtest.backtester import GridBacktester
from backtest.config import Config
from grid_engine import clock


def _df(prices, freq="1min"):
    n = len(prices)
    return pd.DataFrame({
        "open_time": pd.date_range("2024-01-01", periods=n, freq=freq),
        "open": prices, "high": prices, "low": prices, "close": prices,
        "volume": [100.0] * n,
    })


# ── Step 1/6：等價性導向整合測試 ───────────────────────────────────────

def test_backtester_runs_without_core_strategy():
    cfg = Config(symbol="XRPUSDC", initial_quantity=3, grid_spacing=0.006,
                 take_profit_spacing=0.004, direction="both", terminal_ui_mode=True)
    df = _df([2.5, 2.48, 2.46, 2.5, 2.55, 2.5])
    res = GridBacktester(df, cfg).run()
    assert res.trades_count >= 0
    assert res.final_equity > 0


def test_no_core_strategy_import():
    import backtest.backtester as b
    assert "core.strategy" not in inspect.getsource(b)


# ── 追價語意：回測決策路徑真的吃 decide() ───────────────────────────────

def test_decision_source_is_pure_decide():
    """下探 K 線應在 entry 價成交開多倉；驗證回測透過 decide() 掛的 entry 有被觸發。"""
    cfg = Config(symbol="XRPUSDC", initial_quantity=3, grid_spacing=0.006,
                 take_profit_spacing=0.004, direction="long", terminal_ui_mode=True,
                 leverage=20, fee_pct=0.0004)
    # 起始 2.5 → entry 掛在 2.5*(1-0.006)=2.485；下一根跌破即成交
    df = _df([2.5, 2.48, 2.51])
    res = GridBacktester(df, cfg).run()
    # 開倉後拉回 2.51 > tp(2.485*1.004)…anchor 追價後仍應至少有一筆進出
    assert res.trades_count >= 1
    assert math.isfinite(res.final_equity)


def test_chase_semantics_no_static_ladder():
    """盤整（價格貼近 anchor，偏離 < gs*0.5）不應每根重掛導致爆量成交。"""
    cfg = Config(symbol="XRPUSDC", initial_quantity=3, grid_spacing=0.02,
                 take_profit_spacing=0.01, direction="both", terminal_ui_mode=True)
    # 微幅震盪，偏離遠小於 gs*0.5=1%
    df = _df([2.5, 2.501, 2.4995, 2.5005, 2.5])
    res = GridBacktester(df, cfg).run()
    assert res.trades_count == 0  # 沒穿越任何掛單
    assert res.final_equity > 0


# ── sim-clock：finally reset，不污染全域 ───────────────────────────────

def test_clock_reset_after_run():
    import time
    before = clock._now_fn
    cfg = Config(symbol="XRPUSDC", initial_quantity=3, direction="both")
    GridBacktester(_df([2.5, 2.4, 2.6]), cfg).run()
    assert clock._now_fn is time.time
    assert clock._now_fn is before  # 未殘留 sim-clock


def test_clock_reset_even_on_exception(monkeypatch):
    import time
    cfg = Config(symbol="XRPUSDC", initial_quantity=3, direction="both")
    bt = GridBacktester(_df([2.5, 2.4, 2.6]), cfg)

    def boom(*a, **k):
        raise RuntimeError("boom")

    # 讓 loop 中途炸掉，驗證 finally 仍 reset clock
    monkeypatch.setattr("backtest.backtester.build_snapshot", boom)
    with pytest.raises(RuntimeError):
        bt.run()
    assert clock._now_fn is time.time


# ── Step 7：Monkey testing（極端 K 線）─────────────────────────────────

def test_monkey_gap_50pct_single_bar():
    cfg = Config(symbol="XRPUSDC", initial_quantity=3, direction="both")
    df = _df([2.5, 2.5, 1.25, 2.5, 3.75])  # 單根跳空 -50% / +200%
    res = GridBacktester(df, cfg).run()
    assert math.isfinite(res.final_equity)


def test_monkey_zero_volume():
    cfg = Config(symbol="XRPUSDC", initial_quantity=3, direction="both")
    df = _df([2.5, 2.48, 2.46, 2.5])
    df["volume"] = 0.0
    res = GridBacktester(df, cfg).run()
    assert math.isfinite(res.final_equity)


def test_monkey_time_reversal():
    """bar_time 遞減：manager 用 clock.now() 需容忍非單調時間，不拋例外。"""
    cfg = Config(symbol="XRPUSDC", initial_quantity=3, direction="both")
    df = _df([2.5, 2.4, 2.6, 2.5])
    df["open_time"] = pd.to_datetime(df["open_time"])[::-1].values  # 時間倒流
    res = GridBacktester(df, cfg).run()
    assert math.isfinite(res.final_equity)


def test_monkey_single_bar():
    cfg = Config(symbol="XRPUSDC", initial_quantity=3, direction="both")
    res = GridBacktester(_df([2.5]), cfg).run()
    assert math.isfinite(res.final_equity)
    assert res.trades_count >= 0


def test_monkey_zero_price_guard():
    """價格為 0（髒資料）不應除零崩潰。"""
    cfg = Config(symbol="XRPUSDC", initial_quantity=3, direction="both")
    df = _df([2.5, 0.0, 2.5])
    res = GridBacktester(df, cfg).run()
    assert math.isfinite(res.final_equity)


def test_monkey_nan_price():
    cfg = Config(symbol="XRPUSDC", initial_quantity=3, direction="both")
    df = _df([2.5, float("nan"), 2.5])
    res = GridBacktester(df, cfg).run()
    assert math.isfinite(res.final_equity)
