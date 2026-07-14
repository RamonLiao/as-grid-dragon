"""退化路徑等價守門（spec §9）：tick sim 在「1 tick = 1 bar close、零延遲、
零 cooldown、factor=0.5」下，與 GridBacktester 餵 h=l=c=price 的退化 bar 應
產生相同 fills 序列與 final_equity（容差 1e-9）。

差異白名單：無（有 diff 即 FAIL——兩邊帳務同一份 PositionBook，決策同一份
decide()，唯一自由度是撮合序，而退化 bar 下 touch/crossing 邊界不會出現：
fixture 價格全部嚴格穿越，不踩 == limit 的邊界）。

等價域分析（Task 8 report §gap）：
- flat 側 gate 語意差（tick=AND-of-absence vs backtester decide() 的 should_adjust
  = buy_o<=0 OR sell_o<=0）被 fixture 繞開：每步 return 皆 > grid_spacing*factor
  (=0.0015)，deviation gate 在兩邊都恆觸發，故 AND/OR 差異不顯現（兩引擎每個
  事件都重掛）。詳見 .superpowers/sdd/task-8-report.md。
- 1m `_settle` 用 touch（low<=limit），tick 用嚴格穿越（price<limit）：fixture
  價格全部嚴格穿越掛單價、不踩 == 邊界，故 touch/strict 同結果。
"""
import pandas as pd
import pytest


def _price_path():
    # 構造會產生 entry+TP 至少各一次的路徑（嚴格穿越，不踩 == 邊界）。
    # 每步 return 皆 > grid_spacing*factor=0.0015，deviation gate 兩邊恆觸發。
    return [100.0, 99.65, 99.90, 100.31, 99.95, 99.60, 100.40]


def test_degenerate_equivalence():
    from backtest.tick_sim import TickSimConfig, run_tick_sim
    from backtest.backtester import GridBacktester
    from backtest.config import Config

    path = _price_path()
    events = pd.DataFrame({
        "ts_ms": [i * 60_000 for i in range(len(path))],
        "price": path,
        "qty": [1.0] * len(path),
    })
    tick_cfg = TickSimConfig(
        grid_spacing=0.003, take_profit_spacing=0.003,
        initial_quantity=0.02, leverage=5.0, initial_balance=1000.0,
        fee_pct=0.0002, slippage_bps=0.0, threshold_multiplier=40.0,
        requote_threshold_factor=0.5, cooldown_sec=0.0, decision_delay_ms=0,
    )
    r_tick = run_tick_sim(events, tick_cfg)

    bt_cfg = Config(
        initial_balance=1000.0, initial_quantity=0.02, leverage=5,
        grid_spacing=0.003, take_profit_spacing=0.003, fee_pct=0.0002,
        slippage_bps=0.0, funding_enabled=False, threshold_multiplier=40.0,
        direction="both",
    )
    df = pd.DataFrame({
        "open": path, "high": path, "low": path, "close": path,
        "open_time": pd.to_datetime([i * 60_000 for i in range(len(path))],
                                    unit="ms", utc=True),
    })
    # 實際簽名：GridBacktester(df, config).run()（run() 讀 self.df，不吃參數）。
    r_bar = GridBacktester(df, bt_cfg).run()

    assert r_tick.final_equity == pytest.approx(r_bar.final_equity, abs=1e-9)
    # 口徑對齊（plan review SF-2）：backtester 的 trades 只在 _close append
    # （entry 不記 trade），所以拿 TP 成交數對 TP 成交數，不拿全部 fills。
    tp_fills = [f for f in r_tick.fills if f["kind"] == "tp"]
    assert len(tp_fills) == r_bar.trades_count
