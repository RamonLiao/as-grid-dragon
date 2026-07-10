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
    """equity_curve 的每一點也要含 open margin —— max_drawdown 從它算出來。

    實測對照（同一筆資料）：
      修法前（equity 漏算 open_margin，commit 306e9f4）：worst_equity = 969.6170
      修法後（equity 含 open_margin，commit e10952f）：worst_equity = 993.9700

    原斷言 worst_equity > 900.0 在修法前就會通過，無法守住「equity_curve 必須含 open_margin」
    這件事。改為 990.0，落在兩個實測值之間，能確保：
      修法前會紅（969.617 不 > 990.0）
      修法後會綠（993.970 > 990.0）
    """
    prices = [100.0] * 3 + [99.0, 98.0, 97.0, 96.0, 95.0]
    res = GridBacktester(_flat_df(prices), _zero_cost_cfg()).run()
    # 全程未平倉、價格單調下跌 → 權益最低點不該低於「本金 - 未實現虧損」
    worst_equity = min(e[2] for e in res.equity_curve)
    assert worst_equity > 990.0, (
        f"權益曲線最低點 {worst_equity} 過低，疑似漏算 open margin"
    )


def test_flat_price_no_position_equity_equals_initial_balance():
    """負向對照：沒開過倉時，權益恆等於本金（修正前後都該成立）。"""
    res = GridBacktester(_flat_df([100.0] * 5), _zero_cost_cfg(direction="long",
                                                               grid_spacing=0.5)).run()
    assert res.final_equity == pytest.approx(1000.0, abs=1e-9)


# ── margin_usage 純層 ────────────────────────────────────────────────

from backtest.liquidation import margin_usage


def test_margin_usage_is_notional_over_leverage_over_equity():
    # 倉位名目 = (2+0) * 100 = 200；margin = 200/10 = 20；equity = 1000
    assert margin_usage(2.0, 0.0, 100.0, 10.0, 1000.0) == pytest.approx(0.02)


def test_margin_usage_sums_both_sides():
    # hedge mode：多空兩邊都佔保證金
    assert margin_usage(2.0, 3.0, 100.0, 10.0, 1000.0) == pytest.approx(0.05)


def test_margin_usage_is_inf_when_equity_non_positive():
    """equity <= 0 → 定義為 inf，避免除零；下游一律視為已強平。"""
    assert margin_usage(1.0, 0.0, 100.0, 10.0, 0.0) == float("inf")
    assert margin_usage(1.0, 0.0, 100.0, 10.0, -5.0) == float("inf")


def test_margin_usage_zero_when_no_position():
    assert margin_usage(0.0, 0.0, 100.0, 10.0, 1000.0) == 0.0


def test_backtest_result_reports_peak_margin_usage():
    """peak_margin_usage 是強平距離的代理（spec §7），純觀測不影響決策。"""
    prices = [100.0] * 3 + [99.0, 98.0, 97.0]
    res = GridBacktester(_flat_df(prices), _zero_cost_cfg()).run()
    assert res.peak_margin_usage > 0.0
    assert res.peak_margin_usage < 1.0


# ── max_drawdown 必須吃盤中最不利權益，不是收盤價的 equity_curve ──────────

def _ohlc_df(rows):
    return pd.DataFrame({
        "open_time": pd.date_range("2024-01-01", periods=len(rows), freq="1min"),
        "open": [r[0] for r in rows], "high": [r[1] for r in rows],
        "low": [r[2] for r in rows], "close": [r[3] for r in rows],
        "volume": [100.0] * len(rows),
    })


def _wick_cfg(**kw):
    """position_threshold 在第一次進場後立刻被超過(threshold_multiplier=0.5，
    initial_quantity=1.0 → threshold=0.5) → 第二根之後倉位轉入裝死模式，
    只會掛「裝死止盈」(reduce-only，價格遠高於現價)，不會再掛新的網格進場單。
    這保證後續 wick 的 low 不會被誤判成觸發了額外進場——wick 前後兩個
    fixture 除了最後一根的 high/low 之外，倉位路徑逐根相同，唯一差異就是
    最後一根「盤中最不利價」，才能把 max_drawdown 的效果單獨隔離出來量測。
    """
    base = dict(symbol="BNBUSDC", initial_balance=1000.0, initial_quantity=1.0,
                leverage=5, grid_spacing=0.01, take_profit_spacing=0.01,
                direction="long", terminal_ui_mode=True,
                fee_pct=0.0, slippage_bps=0.0, funding_enabled=False,
                threshold_multiplier=0.5)
    base.update(kw)
    return Config(**base)


_WICK_BUILDUP_ROWS = [
    (100, 100, 100, 100),  # bar1：零倉位 bootstrap，掛進場單 @99（grid_spacing 1%）
    (99, 99, 98, 99),      # bar2：low=98 觸及 99 → 進場成交；持倉超過 threshold(0.5) → 轉裝死
    (99, 99, 99, 99),      # bar3：裝死模式，只掛遠價位止盈單，不再開新倉
    (99, 99, 99, 99),      # bar4：同上，持倉路徑穩定
]


def test_max_drawdown_uses_intrabar_trough_not_close():
    """max_drawdown 的谷底必須吃盤中最不利權益，不能只吃收盤價的 equity_curve。

    「盤中觸及才是真相」這條原則此前只套用了一半：撮合（用 high/low 判穿越）
    與強平判定（用本根盤中最不利價）都已修正，唯獨 spec §7 欽定的兩個主指標
    之一 max_drawdown 仍從收盤價的 equity_curve 算出——對「存活但盤中反覆
    逼近爆倉、收盤回升」的策略（正是評估『關掉裝死模式』高換手方案時最關心
    的尾部風險）系統性低估真實盤中回撤。

    兩個 fixture 除最後一根 K 線的 high/low 外逐根相同（見 _wick_cfg docstring
    如何避免 wick 觸發額外進場單，隔離出「只有評估用的最不利價不同」這個變因）：
      無 wick：最後一根 (99, 99, 99, 99) —— 盤中無波動。
      有 wick：最後一根 (99, 99.2, 85, 99) —— 盤中觸低 85（收盤價不變）。

    本測試先於實作驗證過現行碼會紅：用 `git show HEAD:backtest/backtester.py`
    抓修法前的版本重跑同一組 fixture，兩者 max_drawdown 逐位元相等
    （皆為 0.0，因為 equity_curve 只認收盤價，wick 完全不可見）。
    """
    no_wick_rows = _WICK_BUILDUP_ROWS + [(99, 99, 99, 99)]
    wick_rows = _WICK_BUILDUP_ROWS + [(99, 99.2, 85, 99)]

    res_no_wick = GridBacktester(_ohlc_df(no_wick_rows), _wick_cfg()).run()
    res_wick = GridBacktester(_ohlc_df(wick_rows), _wick_cfg()).run()

    assert res_no_wick.liquidated is False and res_wick.liquidated is False, (
        "本測試要量測『存活但盤中逼近爆倉』的 max_drawdown，wick 不該深到觸發強平"
    )
    assert res_wick.max_drawdown > res_no_wick.max_drawdown, (
        f"有 wick 的 max_drawdown({res_wick.max_drawdown}) 應嚴格大於無 wick 的"
        f"({res_no_wick.max_drawdown})——現行碼若兩者相等，代表 max_drawdown 仍在"
        "吃收盤價的 equity_curve，看不見盤中 -14% 的 wick。"
    )


def test_equity_curve_still_uses_close_prices():
    """負向對照：equity_curve 恆用收盤價，不受盤中 wick 影響。

    equity_curve 是「畫給人看的曲線」，不是風險指標——risk 指標是 max_drawdown。
    兩者刻意用不同基準：max_drawdown 的谷底吃盤中最不利權益（見上一條測試），
    但 equity_curve 若也跟著盤中價格鋸齒狀跳動，會讓圖表失去可讀性（見
    backtester.py 主迴圈內「這是『權益曲線』的 equity...」註解）。

    本測試鎖住這個刻意的不對稱：wick 只影響 max_drawdown，不影響 equity_curve
    本身的數值——even 是同一組 fixture，equity_curve 應逐點相等。
    """
    no_wick_rows = _WICK_BUILDUP_ROWS + [(99, 99, 99, 99)]
    wick_rows = _WICK_BUILDUP_ROWS + [(99, 99.2, 85, 99)]

    res_no_wick = GridBacktester(_ohlc_df(no_wick_rows), _wick_cfg()).run()
    res_wick = GridBacktester(_ohlc_df(wick_rows), _wick_cfg()).run()

    no_wick_equities = [e[2] for e in res_no_wick.equity_curve]
    wick_equities = [e[2] for e in res_wick.equity_curve]
    assert wick_equities == pytest.approx(no_wick_equities), (
        "equity_curve 應恆用收盤價計算，不該因盤中 wick 而改變——"
        "若此斷言失敗，代表 equity_curve 被誤改成吃盤中最不利價，"
        "圖表會失去可讀性（鋸齒狀跳動）。"
    )
    # min(equity_curve) 可能高於 max_drawdown 隱含的谷底——兩者不保證一致，
    # 這正是本次修法刻意引入的不對稱（FIDELITY_NOTES (9b)）：曲線本身貼著本金
    # （看不出 wick），但 max_drawdown 已經吃到盤中最不利價，理應更大。
    min_curve_equity = min(wick_equities)
    implied_trough = 1000.0 * (1 - res_wick.max_drawdown)
    assert min_curve_equity >= 990.0, "本測試的 fixture 前提：曲線本身應貼著本金，看不出 wick"
    assert implied_trough < min_curve_equity, (
        "max_drawdown 隱含的谷底應低於 equity_curve 觀察到的最低點——"
        "否則 max_drawdown 沒有真的吃到盤中最不利價。"
    )
