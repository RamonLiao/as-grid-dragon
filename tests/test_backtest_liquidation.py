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


# ── 無效輸入 → raise，而非靜默回 False（review 項目 1/2）──────────────────
# 這是強平安全檢查函數，函數內 False 唯一合法語意是「安全」。對髒資料回傳
# False 等於在帳戶已崩潰時宣稱「沒事」。改為 raise，讓失敗變大聲。

def test_price_zero_raises_instead_of_silently_bypassing_liquidation():
    """price<=0 時舊實作會讓 notional<=0，誤判為『無倉位』而永不強平——
    即使實際持有大量倉位。這是資料污染（除零、NaN 填補失敗）靜默關閉
    強平檢查的路徑，必須 raise。"""
    with pytest.raises(ValueError):
        should_liquidate(equity=4.0, long_pos=10.0, short_pos=0.0,
                         price=0.0, maintenance_margin_rate=0.005)


def test_price_negative_raises():
    with pytest.raises(ValueError):
        should_liquidate(equity=4.0, long_pos=10.0, short_pos=0.0,
                         price=-100.0, maintenance_margin_rate=0.005)


def test_price_nan_raises():
    with pytest.raises(ValueError):
        should_liquidate(equity=4.0, long_pos=10.0, short_pos=0.0,
                         price=float("nan"), maintenance_margin_rate=0.005)


def test_equity_nan_raises_instead_of_silently_returning_false():
    """NaN <= x 在 Python 恆為 False → 舊實作對 equity=NaN 一律回 False，
    即帳戶已崩潰卻回報『安全』。必須 raise 而非靜默過關。"""
    with pytest.raises(ValueError):
        should_liquidate(equity=float("nan"), long_pos=10.0, short_pos=0.0,
                         price=100.0, maintenance_margin_rate=0.005)


def test_equity_infinite_raises():
    with pytest.raises(ValueError):
        should_liquidate(equity=float("-inf"), long_pos=10.0, short_pos=0.0,
                         price=100.0, maintenance_margin_rate=0.005)


def test_negative_maintenance_margin_rate_raises():
    with pytest.raises(ValueError):
        should_liquidate(equity=4.0, long_pos=10.0, short_pos=0.0,
                         price=100.0, maintenance_margin_rate=-0.005)


def test_negative_long_pos_raises():
    with pytest.raises(ValueError):
        should_liquidate(equity=4.0, long_pos=-10.0, short_pos=0.0,
                         price=100.0, maintenance_margin_rate=0.005)


def test_negative_short_pos_raises():
    with pytest.raises(ValueError):
        should_liquidate(equity=4.0, long_pos=0.0, short_pos=-10.0,
                         price=100.0, maintenance_margin_rate=0.005)


# ── 整合 ─────────────────────────────────────────────────────────────

def test_relentless_downtrend_with_no_dead_mode_triggers_liquidation():
    """G-0b1：單邊崩盤 + 裝死關閉 + 高槓桿 → 必爆，且回測提前終止。

    這正是選項 (b)「關掉裝死模式」的尾部風險。沒有強平建模時，回測會讓
    倉位無限累積、equity 變負而照跑到底，於是 optimizer 誤判 (b) 最好。

    控制端已實測：equity_curve 長度 22、final_equity = 0.7563。斷言涵蓋：
      - liquidated is True：強平旗標有被設。
      - realized_pnl != 0：證明 _close() 真的被呼叫、強平損益進了 trades，
        不是只 break 迴圈卻沒平倉（若強平區塊漏呼叫 _close，這條會抓到）。
      - equity_curve[-1][2] == final_equity：強平後記錄的末點權益與
        result.final_equity 一致，不是兩套不同步的權益計算。
      - len(equity_curve) < 50：提前終止的合理上界（實測 22）。單獨這條
        會被「提前 1 根就 break」的假實作騙過，但那種假實作平不了倉，
        會被上面的 realized_pnl != 0 擋下。
    """
    prices = [100.0] + [100.0 * (0.99 ** i) for i in range(1, 400)]
    res = GridBacktester(_flat_df(prices), _cfg()).run()
    assert res.liquidated is True
    assert res.realized_pnl != 0
    assert res.equity_curve[-1][2] == pytest.approx(res.final_equity)
    # 提前終止：權益曲線長度應短於 K 線數，且落在合理早的範圍內
    assert len(res.equity_curve) < len(prices)
    assert len(res.equity_curve) < 50


def test_liquidation_with_slippage_and_fee_still_liquidates():
    """G-0b1 場景疊加真實成本模型（slippage_bps + fee_pct），而非全 0 捷徑。

    _close() 對強平平倉價套用 apply_slippage(..., "tp", slippage_bps)，使平倉
    價比 should_liquidate() 判斷當下用的 price 更不利；再疊 fee_pct 扣手續費。
    這代表 liquidated=True 時 final_equity 可能比「用觸發價瞬間平倉」算出來
    的更差，甚至仍為負。

    語意重點（不是數值重點）：liquidated=True 不保證 final_equity >= 0；
    liquidated 是一票否決訊號，下游（optimizer / spec §7）不得假設「爆倉後
    equity 歸零」或「爆倉後 equity 非負」，只能把 liquidated=True 當成
    「此參數組合不合格」的旗標。因此這裡刻意不斷言 final_equity 的正負號。
    """
    prices = [100.0] + [100.0 * (0.99 ** i) for i in range(1, 400)]
    res = GridBacktester(
        _flat_df(prices), _cfg(slippage_bps=0.001, fee_pct=0.0004)
    ).run()
    assert res.liquidated is True


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
