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


# ── 整合：backtester 真的用 high/low 且成交於掛單價 ──────────────────

import pandas as pd

from backtest.backtester import GridBacktester
from backtest.config import Config


def _ohlc_df(bars):
    """bars: list of (open, high, low, close)"""
    return pd.DataFrame({
        "open_time": pd.date_range("2024-01-01", periods=len(bars), freq="1min"),
        "open": [b[0] for b in bars],
        "high": [b[1] for b in bars],
        "low": [b[2] for b in bars],
        "close": [b[3] for b in bars],
        "volume": [100.0] * len(bars),
    })


def _zero_cost_cfg(**kw):
    base = dict(symbol="BNBUSDC", initial_balance=100000.0, initial_quantity=1.0,
                leverage=10, take_profit_spacing=0.004, grid_spacing=0.006,
                direction="long", terminal_ui_mode=True,
                fee_pct=0.0, slippage_bps=0.0, funding_enabled=False)
    base.update(kw)
    return Config(**base)


def test_long_entry_fills_at_limit_price_not_at_close():
    """G-0a2：零成本下，成交價必須嚴格等於掛單價。

    第 1 根 close=100 → 掛進場限價 100*(1-0.006) = 99.4。
    第 2 根 low=98（刺穿 99.4）但 close=100（未穿越）→ 正確實作應成交於 99.4。
    第 3 根收在 99.5（**高於掛單價**）→ 舊實作在這根也不會成交。

    三種實作產生三個互相可區分的 unrealized_pnl（qty=1）：
      舊（close 判穿越、成交於 close）     → 兩根都不成交 → 0.0，final_equity == 100000
      半修（low 判穿越、仍成交於 close）    → 成交於 98    → 99.5 - 98.0  = 1.5
      正確（low 判穿越、成交於 limit）      → 成交於 99.4  → 99.5 - 99.4 = 0.1

    第一條斷言擋掉「沒開倉」（舊實作），第二條擋掉「成交於 close」（半修）。

    註：末根 close 不可設成 99.4 —— 那會讓舊實作在第 3 根以 close=99.4 成交，
    結果與正確實作在第 2 根成交於 99.4 數值完全相同，fixture 就失去鑑別力。
    （此陷阱在計畫初稿中真實發生過。）
    """
    df = _ohlc_df([
        (100.0, 100.0, 100.0, 100.0),   # 掛單：entry @ 99.4
        (100.0, 100.5,  98.0, 100.0),   # low 刺穿 99.4 → 應成交於 99.4
        (99.5,   99.5,  99.5,  99.5),   # 末根收在 99.5（> 99.4）→ 舊實作仍不成交
    ])
    res = GridBacktester(df, _zero_cost_cfg()).run()
    assert res.final_equity != pytest.approx(100000.0, abs=1e-9), (
        "沒有開倉：low 未被用來判穿越（舊實作行為）"
    )
    assert res.unrealized_pnl == pytest.approx(0.1, abs=1e-6), (
        f"成交價不等於掛單價 99.4（unrealized={res.unrealized_pnl}；"
        f"若為 1.5 表示成交於該根 close=98）"
    )


def test_long_entry_does_not_fill_when_low_never_reaches_limit():
    """負向對照：low 沒到掛單價就不該成交。"""
    df = _ohlc_df([
        (100.0, 100.0, 100.0, 100.0),   # entry @ 99.4
        (100.0, 100.5,  99.5, 100.0),   # low=99.5 > 99.4 → 不成交
        (100.0, 100.0, 100.0, 100.0),
    ])
    res = GridBacktester(df, _zero_cost_cfg()).run()
    assert res.unrealized_pnl == pytest.approx(0.0, abs=1e-9)
    assert res.trades_count == 0


# ── 回歸：止盈不得平掉本根才剛開的倉（reduce_only 語意） ────────────────

def _net_exposure_doubling_cfg(**kw):
    """讓止盈加倍在「加倍只給淨曝險側」規則下仍然觸發，以便行使 reduce_only clamp。

    _zero_cost_cfg 預設 direction="long" ⇒ short_position 恆為 0 ⇒ `my > opposite`
    必然成立。limit_multiplier=0.5 給出 position_limit = 1.0*0.5 = 0.5 < 1.0，
    所以一次進場成交（qty=1.0）後 `my > limit` 也成立 → tp qty = 2.0。
    threshold_multiplier=100 讓 position_threshold=100，is_dead_mode(1.0, 100)=False，
    裝死分支不介入。

    注意：舊版 fixture 靠已刪除的 `opposite >= threshold` 子條件觸發加倍，改動後會
    靜默變成永綠空殼（tp qty 1.0 使 clamp 成為 no-op，而斷言值恰好不變）。
    """
    return _zero_cost_cfg(limit_multiplier=0.5, threshold_multiplier=100.0, **kw)


def test_tp_fill_cannot_close_more_than_the_position_that_existed_before_this_bars_entry():
    """止盈單是 reduce_only：交易所只允許平『成交當下已存在』的倉位。
    high/low 判穿越後，同一根 K 線常常「進場」與「止盈」同時觸及（Task 2 G4 前幾乎
    不可能，現在很常見）。_settle 讓 entry 先於 tp 結算（保守：先增加曝險），但 _close
    走 FIFO 平倉，若止盈量大於「entry 結算前」的持倉量，會一路平到本根才剛開的倉——
    這張倉在止盈單成交的當下根本還不存在，回測卻樂觀地把它記為獲利了結。

    構造：
      bar1 close=100 → 掛 long entry@99.4(=100*0.994)，qty=1.0（direction="long"，無 short 側）。
      bar2 (low=99, high=101) → long entry 成交 @99.4，long_position=1.0。
        重決策：m=1.0 > position_limit=0.5 且 m > opposite=0 → tp qty 加倍為 2.0 @100.4。
      bar3 (low=99, high=101) 同時穿越 entry@99.4 與 tp@100.4。
        entry 先結算 → long=2.0（bar2 那 1.0 + bar3 新開的 1.0）。
        止盈量 2.0 > 「entry 結算前」的持倉 1.0（bar2 那筆）→ clamp 被行使。

    期望（clamp 到 prior_qty）：止盈只平掉 bar2 那筆（entry 結算前已存在的倉），
    bar3 新開的倉留著、算進 unrealized：
      trades_count == 1（long 1 筆，平 1.0）
      realized_pnl == (100.4 − 99.4) × 1.0 == 1.0
      unrealized_pnl == 0.6（final close=100，剩 1 手 @99.4）

    修法前（bug）：止盈量 2.0 用 FIFO 吃光兩筆倉位（bar2 舊倉 + bar3 新倉），
    trades_count == 2、realized_pnl == 2.0、unrealized_pnl == 0.0 —— 把還不存在的倉記成獲利。
    """
    df = _ohlc_df([
        (100.0, 100.0, 100.0, 100.0),   # bar1: 建立初始掛單（entry@99.4）
        (100.0, 101.0,  99.0, 100.0),   # bar2: entry 成交 → 觸發加倍止盈
        (100.0, 101.0,  99.0, 100.0),   # bar3: entry 與 tp 同根同觸發（本測試核心場景）
    ])
    res = GridBacktester(df, _net_exposure_doubling_cfg()).run()

    assert res.trades_count == 1, (
        f"trades_count={res.trades_count}：若為 2，代表止盈把 bar3 剛開的倉也平掉了"
        "（reduce_only 不可能平未來才存在的倉）"
    )
    assert res.realized_pnl == pytest.approx(1.0, abs=1e-6), (
        f"realized_pnl={res.realized_pnl}：若為 2.0，代表 bar3 新倉被錯誤地計入已實現獲利"
    )
    assert res.unrealized_pnl == pytest.approx(0.6, abs=1e-6), (
        f"unrealized_pnl={res.unrealized_pnl}：若為 0.0，代表 bar3 新倉已被平掉"
    )


# ── monkey：髒 high/low 型別不得讓 run() 崩潰 ────────────────────────

@pytest.mark.parametrize("dirty_value", [None, "not_a_number"])
def test_run_degrades_to_close_instead_of_crashing_when_high_low_have_wrong_type(dirty_value):
    """high/low 若來自 object dtype 欄位，可能混入 None 或字串（例如上游資料源
    某根 K 線缺值、或型別轉換失敗但未整根丟棄）。防禦迴圈只有 try/finally（沒有
    except），math.isfinite(None) / math.isfinite('x') 會直接拋 TypeError 讓 run()
    整個崩潰，而不是照設計退化為用 close 撮合。加 isinstance 守衛後應正常跑完、
    不拋例外——退化行為與 price 欄位既有的防禦一致（同檔 line ~649）。"""
    df = _ohlc_df([
        (100.0, 100.0, 100.0, 100.0),
        (100.0, 100.5,  99.5, 100.0),
        (100.0, 100.0, 100.0, 100.0),
    ])
    df["high"] = df["high"].astype(object)
    df["low"] = df["low"].astype(object)
    df.loc[1, "high"] = dirty_value
    df.loc[1, "low"] = dirty_value

    res = GridBacktester(df, _zero_cost_cfg()).run()  # 不得拋例外

    assert res.trades_count >= 0  # 只要跑完就是通過；退化根用 close 撮合
