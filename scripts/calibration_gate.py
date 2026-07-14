"""校準 gate（Task 9）：tick 模擬器上線前的三道守門，任一 FAIL 則整條計畫暫停。

判定純函數（judge_*）為唯一權威，單元測試於 tests/test_calibration_gate.py。
main() 為薄殼：下載 aggTrades → 壓縮 → 載 funding → 跑 tick/1m 模擬 → 逐 gate 判定。

三 gate（spec §4.3 / task-9-brief）：
  低端  factor=0.5 現倉 seed，07-12(06:51 UTC 起)~--end，對照 live≈0 筆 → sim_fills/day <= 2.0
  高端  factor=1.0 flat，06-16~06-24 tick vs 同窗口 1m bars 同參數
        → tick >= 0.2*bar（下界）+ 成交真實性驗證 violations == 0（spec 2026-07-14 修訂，上界已移除）
  6 月  factor=0.5 flat，06-06~06-30 逐日 fills，對照 live COMMISSION 逐日聚合（cap 15x，spec 2026-07-14 修訂）

用法：uv run python scripts/calibration_gate.py --end 2026-07-13
      exit code 0 = 三 gate 全 PASS。
"""
import argparse
import datetime as dt
import sys
import time
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.aggtrades import AggTradesLoader, compress_events
from backtest.config import Config
from backtest.data_loader import DataLoader
from backtest.tick_sim import TickSimConfig, run_tick_sim

_UTC = dt.timezone.utc
SYMBOL = "BNBUSDC"

# --- 生產參數（低端 gate 用；高端/6月只換 factor 與 seed）------------------
PROD = dict(
    grid_spacing=0.003, take_profit_spacing=0.003, initial_quantity=0.02,
    leverage=5.0, initial_balance=184.6, fee_pct=0.0002, slippage_bps=0.0001,
    threshold_multiplier=40.0, limit_multiplier=5.0,
    cooldown_sec=5.0, decision_delay_ms=500,
)
SEED = dict(seed_long_qty=0.58, seed_long_price=690.29,
            seed_short_qty=0.34, seed_short_price=571.75)

# --- Live ground truth（寫死，出處註明）------------------------------------
# 低端 live = 0 筆：fetch_my_trades 於 2026-07-13 健檢，現倉自建倉後零成交。
LOW_LIVE_FILLS_PER_DAY = 0.0
# 6 月 live COMMISSION 逐日聚合（出處 2026-07-13 健檢；其餘日為 0）。
JUNE_LIVE_DAILY = {
    "2026-06-17": 3, "2026-06-19": 1, "2026-06-22": 1,
    "2026-06-23": 1, "2026-06-25": 3, "2026-06-28": 1,
}


# ===========================================================================
# 判定純函數（gate 邏輯的唯一權威）
# ===========================================================================
def judge_low_gate(sim_fills_per_day: float) -> bool:
    """低端：live≈0，sim 每日成交不得虛增（<= 2.0 筆/日）。"""
    return sim_fills_per_day <= 2.0


def judge_high_gate(tick_fills: int, bar_fills: int, crossing_violations: int) -> bool:
    """高端（spec §4.3 2026-07-14 修訂）：上界移除，改兩條件並存：
      (a) 下界：tick >= 0.2x bar（偵測 fill 引擎系統性死亡）；
      (b) 成交真實性機械驗證：violations == 0（每筆 fill 於 fill 時刻必須存在
          嚴格穿越 limit 的原始事件，違規 = 偷跑）。
    bar==0 → 分母失效，強制 False 並要求換窗口（該窗口無成交、無鑑別力）。"""
    if bar_fills == 0:
        return False
    return tick_fills >= 0.2 * bar_fills and crossing_violations == 0


def judge_june_alignment(sim_daily: dict, live_daily: dict) -> bool:
    """6 月對齊：
      (1) live>0 的日子 sim 也 >0 的比例 >= 0.5；
      (2) sim 月總量 <= 15x live 月總量（量級不炸）。
    live 全無活躍日 → 無對齊基準，保守 False。"""
    live_active = [d for d, n in live_daily.items() if n > 0]
    if not live_active:
        return False
    hit = sum(1 for d in live_active if sim_daily.get(d, 0) > 0)
    ratio = hit / len(live_active)
    sim_total = sum(sim_daily.values())
    live_total = sum(live_daily.values())
    return ratio >= 0.5 and sim_total <= 15 * live_total


def _crossing_violations(fills: list, events: pd.DataFrame) -> int:
    """成交真實性機械驗證（spec §4.3 高端 gate 2026-07-14 修訂）：每筆 fill 在
    fill 時刻（ts_ms）必須存在嚴格穿越 limit 的原始事件，否則視為偷跑（violation）。
    buy fill（long entry / short tp）要求事件價 < limit；
    sell fill（short entry / long tp）要求事件價 > limit。"""
    ts_index = defaultdict(list)
    for row in events.itertuples(index=False):
        ts_index[int(row.ts_ms)].append(float(row.price))
    violations = 0
    for f in fills:
        is_buy = (f["side"] == "long" and f["kind"] == "entry") or \
                 (f["side"] == "short" and f["kind"] == "tp")
        limit = f["price"]
        prices = ts_index.get(int(f["ts_ms"]), [])
        crossed = any(p < limit for p in prices) if is_buy else any(p > limit for p in prices)
        if not crossed:
            violations += 1
    return violations


# ===========================================================================
# 資料/模擬薄殼
# ===========================================================================
def _tick_cfg(factor: float, funding_events, seed: bool) -> TickSimConfig:
    return TickSimConfig(
        grid_spacing=PROD["grid_spacing"], take_profit_spacing=PROD["take_profit_spacing"],
        initial_quantity=PROD["initial_quantity"], leverage=PROD["leverage"],
        initial_balance=PROD["initial_balance"], fee_pct=PROD["fee_pct"],
        slippage_bps=PROD["slippage_bps"], threshold_multiplier=PROD["threshold_multiplier"],
        limit_multiplier=PROD["limit_multiplier"], requote_threshold_factor=factor,
        cooldown_sec=PROD["cooldown_sec"], decision_delay_ms=PROD["decision_delay_ms"],
        funding_events=funding_events,
        **(SEED if seed else {}),
    )


def _day_iter(start: str, end: str):
    d = dt.datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=_UTC)
    end_d = dt.datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=_UTC)
    today = dt.datetime.now(_UTC).strftime("%Y-%m-%d")
    while d <= end_d:
        ds = d.strftime("%Y-%m-%d")
        d += timedelta(days=1)
        if ds >= today:      # 未過完當日不納入（與 loader 一致）
            continue
        yield ds


def load_events(agg: AggTradesLoader, start: str, end: str, since_ms=None, until_ms=None):
    """下載缺日（尚未發佈的近日 404 則跳過）→ concat → 裁切 [since_ms, until_ms) → 壓縮。
    回傳 (compressed_events, days_loaded)。"""
    import requests
    days = []
    for ds in _day_iter(start, end):
        try:
            agg.download(SYMBOL, ds, ds)
        except requests.HTTPError as e:
            print(f"  [skip] aggTrades {ds} 未發佈/下載失敗: {e}")
            continue
        days.append(ds)
    parts = [agg.load_day(SYMBOL, ds) for ds in days]
    df = pd.concat(parts, ignore_index=True)
    if since_ms is not None:
        df = df[df["ts_ms"] >= since_ms]
    if until_ms is not None:
        df = df[df["ts_ms"] < until_ms]
    return compress_events(df.reset_index(drop=True)), days


def load_funding_events(start: str, end: str):
    """真實 funding 歷史 → [(epoch_sec, rate)]，裁切到 [start, end) 窗口。"""
    kl = DataLoader()
    s = dt.datetime.strptime(start, "%Y-%m-%d")
    e = dt.datetime.strptime(end, "%Y-%m-%d")
    fmap = kl.load_funding(SYMBOL, s, e)
    start_epoch = s.replace(tzinfo=_UTC).timestamp()
    end_epoch = (e.replace(tzinfo=_UTC) + timedelta(days=1)).timestamp()
    return sorted((sec, rate) for sec, rate in fmap.items()
                  if start_epoch <= sec < end_epoch)


def _fills_by_day(fills, tz_offset_hours: int = 0) -> dict:
    """逐日 fills 聚合。tz_offset_hours=8 → 台北日界（live 健檢 ground truth 的日界）。"""
    tz = dt.timezone(timedelta(hours=tz_offset_hours))
    daily = defaultdict(int)
    for f in fills:
        day = dt.datetime.fromtimestamp(f["ts_ms"] / 1000, tz=tz).strftime("%Y-%m-%d")
        daily[day] += 1
    return dict(daily)


# ===========================================================================
# 三 gate 執行
# ===========================================================================
def run_low_gate(agg, end: str) -> dict:
    start = "2026-07-12"
    since = int(dt.datetime(2026, 7, 12, 6, 51, tzinfo=_UTC).timestamp() * 1000)  # 14:51 Taipei
    ev, days = load_events(agg, start, end, since_ms=since)
    fev = load_funding_events(start, end)
    cfg = _tick_cfg(0.5, fev, seed=True)
    t = time.time()
    r = run_tick_sim(ev, cfg)
    daily = _fills_by_day(r.fills)
    n_days = max(1, len(days))
    fpd = len(r.fills) / n_days
    return {"name": "低端 (low)", "elapsed": time.time() - t,
            "pass": judge_low_gate(fpd),
            "detail": {"total_fills": len(r.fills), "n_days": n_days,
                       "fills_per_day": round(fpd, 4), "round_trips": r.round_trips,
                       "rejected_entries": r.rejected_entries, "liquidated": r.liquidated,
                       "daily": daily, "n_events": len(ev), "days_loaded": days,
                       "threshold": "<= 2.0", "live_fills_per_day": LOW_LIVE_FILLS_PER_DAY}}


def run_high_gate(agg) -> dict:
    start, end = "2026-06-16", "2026-06-24"

    # 1m 對照窗口先定錨：kline 日檔是台北日界（UTC [d-1 16:00, d 15:59]，歷史 8h
    # 偏移產物），故 tick 事件流必須裁到與 bars 完全相同的 UTC 區間，「同窗口」
    # 才成立——不能各用各的日界。
    kl = DataLoader()
    bars = kl.load(SYMBOL, start, end)
    t0_ms = int(bars["open_time"].min().timestamp() * 1000)
    t1_ms = int(bars["open_time"].max().timestamp() * 1000) + 60_000  # 末根 bar 收盤

    # aggTrades 需含前一 UTC 日（窗口起點落在 06-15 16:00 UTC）
    agg_start = (dt.datetime.strptime(start, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    ev, _days = load_events(agg, agg_start, end, since_ms=t0_ms, until_ms=t1_ms)
    fev = [(sec, rate) for sec, rate in load_funding_events(agg_start, end)
           if t0_ms / 1000 <= sec < t1_ms / 1000]
    fmap = {sec: rate for sec, rate in fev}

    t = time.time()
    tick = run_tick_sim(ev, _tick_cfg(1.0, fev, seed=False))
    tick_elapsed = time.time() - t

    # 1m 對照：同窗口、同參數、flat，factor=1.0（Step 0 已接線）
    cfg = Config(symbol=SYMBOL, initial_balance=PROD["initial_balance"],
                 initial_quantity=PROD["initial_quantity"], leverage=int(PROD["leverage"]),
                 take_profit_spacing=PROD["take_profit_spacing"], grid_spacing=PROD["grid_spacing"],
                 fee_pct=PROD["fee_pct"], slippage_bps=PROD["slippage_bps"],
                 limit_multiplier=PROD["limit_multiplier"], threshold_multiplier=PROD["threshold_multiplier"],
                 requote_threshold_factor=1.0, direction="both", terminal_ui_mode=True)
    from backtest.backtester import GridBacktester
    t = time.time()
    bar = GridBacktester(bars, cfg, funding_map=fmap).run()
    bar_elapsed = time.time() - t

    tick_fills = tick.round_trips
    bar_fills = bar.trades_count
    crossing_violations = _crossing_violations(tick.fills, ev)
    return {"name": "高端 (high)", "elapsed": tick_elapsed + bar_elapsed,
            "pass": judge_high_gate(tick_fills, bar_fills, crossing_violations),
            "detail": {"tick_round_trips": tick_fills, "bar_trades_count": bar_fills,
                       "ratio": round(tick_fills / bar_fills, 4) if bar_fills else None,
                       "band": ">= 0.2x bar（下界）+ crossing_violations == 0（真實性驗證，"
                               "上界已移除，spec 2026-07-14 修訂）",
                       "crossing_violations": crossing_violations,
                       "tick_total_fills": len(tick.fills), "tick_rejected": tick.rejected_entries,
                       "tick_liquidated": tick.liquidated, "bar_liquidated": bar.liquidated,
                       "n_events": len(ev), "n_bars": len(bars),
                       "window_utc": f"[{pd.Timestamp(t0_ms, unit='ms')}, {pd.Timestamp(t1_ms, unit='ms')})",
                       "tick_sec": round(tick_elapsed, 1), "bar_sec": round(bar_elapsed, 1)}}


def run_june_gate(agg) -> dict:
    start, end = "2026-06-06", "2026-06-30"
    ev, _days = load_events(agg, start, end)
    fev = load_funding_events(start, end)
    t = time.time()
    r = run_tick_sim(ev, _tick_cfg(0.5, fev, seed=False))
    # 判定用台北日界聚合：live COMMISSION 逐日數字出自使用者健檢（台北時區日期）；
    # UTC 聚合另行印出供對照。
    daily = _fills_by_day(r.fills, tz_offset_hours=8)
    daily_utc = _fills_by_day(r.fills, tz_offset_hours=0)
    sim_total = sum(daily.values())
    live_total = sum(JUNE_LIVE_DAILY.values())
    live_active = [d for d in JUNE_LIVE_DAILY if JUNE_LIVE_DAILY[d] > 0]
    hit = [d for d in live_active if daily.get(d, 0) > 0]
    return {"name": "6 月對齊 (june)", "elapsed": time.time() - t,
            "pass": judge_june_alignment(daily, JUNE_LIVE_DAILY),
            "detail": {"sim_total": sim_total, "live_total": live_total,
                       "magnitude_ratio": round(sim_total / live_total, 4) if live_total else None,
                       "magnitude_cap": "<= 15x live（spec 2026-07-14 修訂：10x→15x）",
                       "hit_ratio": round(len(hit) / len(live_active), 4) if live_active else None,
                       "hit_days": hit, "live_active_days": live_active,
                       "sim_daily_taipei": daily, "sim_daily_utc": daily_utc,
                       "live_daily": JUNE_LIVE_DAILY,
                       "round_trips": r.round_trips, "liquidated": r.liquidated,
                       "n_events": len(ev)}}


def _print_gate(g: dict):
    verdict = "PASS" if g["pass"] else "FAIL"
    print(f"\n{'='*66}")
    print(f"  GATE: {g['name']}   →   {verdict}   ({g['elapsed']:.1f}s)")
    print(f"{'='*66}")
    for k, v in g["detail"].items():
        print(f"  {k:22} = {v}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--end", required=True, help="低端 gate 事件流終點 YYYY-MM-DD")
    args = ap.parse_args(argv)

    agg = AggTradesLoader()
    print(f"# 校準 gate 執行  symbol={SYMBOL}  --end={args.end}  now={dt.datetime.now(_UTC).isoformat()}")

    gates = [run_low_gate(agg, args.end), run_high_gate(agg), run_june_gate(agg)]
    for g in gates:
        _print_gate(g)

    all_pass = all(g["pass"] for g in gates)
    print(f"\n{'#'*66}")
    for g in gates:
        print(f"#  {g['name']:20} {'PASS' if g['pass'] else 'FAIL'}")
    print(f"#  OVERALL: {'ALL PASS' if all_pass else 'FAIL — 計畫暫停，回報使用者'}")
    print(f"{'#'*66}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
