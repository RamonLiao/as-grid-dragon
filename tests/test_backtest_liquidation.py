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


# ── R1 dual-review Important #1：強平必須用盤中最不利價，不能只看收盤 ──────
#
# 撮合已改用 high/low 判穿越（真實 K 線實測 close-only 漏掉 48.5% 成交，見
# tests/test_backtest_matching_realdata.py）。同一個「盤中觸及才是真相」的
# 論證完全適用於強平：盤中已爆倉、收盤回升的 K 線若判為存活，就是修一個 bug
# 的過程中，用同樣的錯誤實作了新功能——讓回測比真實更好看。

def _ohlc_df(rows):
    """rows: [(open, high, low, close), ...]"""
    return pd.DataFrame({
        "open_time": pd.date_range("2024-01-01", periods=len(rows), freq="1min"),
        "open": [r[0] for r in rows], "high": [r[1] for r in rows],
        "low": [r[2] for r in rows], "close": [r[3] for r in rows],
        "volume": [100.0] * len(rows),
    })


def _long_buildup_rows(n=15, decay=0.995):
    """flat 下跌 K 線（open=high=low=close），讓多頭網格沿路加倉，直到堆出
    足夠曝險：後面接一根盤中觸底但收盤回升的 K 線才有東西可以爆。"""
    prices = [100.0]
    p = 100.0
    for _ in range(n):
        p *= decay
        prices.append(p)
    return [(x, x, x, x) for x in prices]


def test_intrabar_low_triggers_liquidation_even_if_close_recovers():
    """盤中觸及維持保證金以下、但收盤回升的 K 線，必須被判定強平。

    控制端已實測（本測試用的 fixture）：
      舊實作（強平判定吃 close）：liquidated=False，equity_curve 走完全部 17 根，
        final_equity≈74.85 —— 盤中 -40% 卻沒爆，只因收盤價從 60 回升到 93。
      修法後：liquidated=True，equity_curve 提前終止於觸發根（17/17，因為觸發
        根正好是最後一根），final_equity≈-189.15。

    本測試先於實作（TDD 紅燈已於控制端用 git show HEAD 抓原始檔驗證：
    對同一組資料，舊碼 liquidated=False）。
    """
    rows = _long_buildup_rows() + [(93.0, 93.5, 60.0, 93.0)]  # 盤中觸及 60，收盤回升到 93
    res = GridBacktester(_ohlc_df(rows), _cfg()).run()

    assert res.liquidated is True
    assert len(res.equity_curve) <= len(rows), "equity_curve 不得晚於強平根之後繼續累積"
    assert res.equity_curve[-1][2] == pytest.approx(res.final_equity)


def test_intrabar_high_triggers_liquidation_for_short_position():
    """空頭鏡像：盤中 high 暴衝穿破維持保證金、但收盤回落的 K 線，必須強平空頭倉位。"""
    prices = [100.0]
    p = 100.0
    for _ in range(15):
        p *= 1.005
        prices.append(p)
    rows = [(x, x, x, x) for x in prices]
    last = prices[-1]
    rows.append((last, last * 1.6, last, last * 0.98))  # 盤中暴衝，收盤回落

    res = GridBacktester(_ohlc_df(rows), _cfg(direction="short")).run()

    assert res.liquidated is True
    assert len(res.equity_curve) <= len(rows)
    assert res.equity_curve[-1][2] == pytest.approx(res.final_equity)


def test_liquidation_price_is_the_adverse_intrabar_price_not_close():
    """強平平倉價必須是盤中最不利價，不是收盤價——用零成本讓數字可精確斷言。

    比較兩組資料：
      A：最後一根 (open=93, high=93.5, low=60, close=93) —— 盤中觸底 60、收盤回升到 93。
      B：最後一根 (60, 60, 60, 60) —— 直接停在 60（新舊實作都會在這根強平，且
         毫無疑義是以 60 平倉）。

    若強平確實吃盤中最不利價（60）而非收盤價（93 vs 60 天差地遠），A、B 兩組的
    realized_pnl 與 final_equity 必須逐分逐毫相等——因為兩者用來平倉的「觸發價」
    在修法後應該完全相同（都是 60），差別只在 A 多了一根 K 線的 close=93 這個
    對強平判定應該完全不起作用的欄位。
    """
    base_rows = _long_buildup_rows()
    rows_a = base_rows + [(93.0, 93.5, 60.0, 93.0)]
    rows_b = base_rows + [(60.0, 60.0, 60.0, 60.0)]

    res_a = GridBacktester(_ohlc_df(rows_a), _cfg()).run()
    res_b = GridBacktester(_ohlc_df(rows_b), _cfg()).run()

    assert res_a.liquidated is True
    assert res_b.liquidated is True
    assert res_a.realized_pnl == pytest.approx(res_b.realized_pnl, abs=1e-9)
    assert res_a.final_equity == pytest.approx(res_b.final_equity, abs=1e-9)


def test_flat_bar_liquidation_unchanged():
    """負向對照：high == low == close 的平坦 K 線，盤中最不利價 == 收盤價，
    行為必須與修法前完全相同——證明本次修法沒有在無盤中波動的一般路徑上
    引入 regression。沿用既有 G-0b1 downtrend 場景的既定期望值。"""
    prices = [100.0] + [100.0 * (0.99 ** i) for i in range(1, 400)]
    res = GridBacktester(_flat_df(prices), _cfg()).run()
    assert res.liquidated is True
    assert res.realized_pnl != 0
    assert res.equity_curve[-1][2] == pytest.approx(res.final_equity)
    assert len(res.equity_curve) < 50
