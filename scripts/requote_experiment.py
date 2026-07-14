"""Task 10：追價語意實驗矩陣 runner + §6 判準預判報告。

在校準過（Task 9 三 gate 全 PASS）的 tick 模擬器上跑 factor {0.5,1.0,1.5} 的完整
對比矩陣（spec §5），輸出每 cell 指標並對 spec §6.1-6.6 逐條 PASS/FAIL/inconclusive
做預判（6.7 holdout 除外——holdout 05-01~06-05 從未開封）。

判準純函數（build_matrix / gate_cells / verdict_preview / bps_to_fraction）為唯一
權威，單元測試於 tests/test_requote_experiment.py。main() 為薄殼：載資料 → 切窗口
→ 跑矩陣 → 彙總 → 寫報告。

上游複用（不重造）：scripts.calibration_gate 的 load_events / load_funding_events /
PROD / SYMBOL；backtest.tick_sim.run_tick_sim；backtest.aggtrades.estimate_spread。

單位（SF-6 latent bug）：TickSimConfig.fee_pct / slippage_bps 底層都是 fraction
（apply_slippage 直接 price*(1±bps)）。矩陣 cost 以 bps 表示，進 cfg 前一律過
bps_to_fraction（×1e-4）。基準 cost = fee 2bps / slip 1bps，與 PROD 對齊。

用法：uv run python scripts/requote_experiment.py --end 2026-07-13 \
        --out tasks/requote-experiment-results.md
"""
import argparse
import datetime as dt
import sys
import time
from dataclasses import dataclass
from itertools import product
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.aggtrades import AggTradesLoader, compress_events, estimate_spread
from backtest.tick_sim import TickSimConfig, run_tick_sim
from scripts.calibration_gate import (
    PROD, SYMBOL, load_events, load_funding_events, _UTC,
)

# ---------------------------------------------------------------------------
# 常數：矩陣維度（spec §5）
# ---------------------------------------------------------------------------
FACTORS = (0.5, 1.0, 1.5)
BASELINE_FACTOR = 0.5                       # Δeq 對照基準（現行語意）
COSTS = tuple(product((2, 4), (0, 1, 2)))   # (fee_bps, slip_bps)：6 組
BASELINE_COST = (2, 1)                       # 基準 fee 2bps / slip 1bps（= PROD）
DELAYS_EXTRA = (0, 1000)                     # 延遲掃描（500ms 已在 main 矩陣）
REJECT_DEGRADE_THRESHOLD = 0.30              # §6.5：拒單率 > 30% 降級

# 資本場景（spec §5）：A=現狀、B=入金 25 補中性 seed
SCENARIOS = {
    "A": dict(initial_balance=184.6, seed_long_qty=0.58, seed_long_price=690.29,
              seed_short_qty=0.34, seed_short_price=571.75),
    "B": dict(initial_balance=209.6, seed_long_qty=0.58, seed_long_price=690.29,
              seed_short_qty=0.58, seed_short_price=571.75),
}


# ===========================================================================
# 純函數（判準邏輯的唯一權威）
# ===========================================================================
def bps_to_fraction(bps: float) -> float:
    """basis points → fraction。1 bp = 0.0001（×1e-4）。"""
    return bps * 1e-4


@dataclass(frozen=True)
class Cell:
    factor: float
    scenario: str          # 'A' | 'B'
    window: str            # 'W1'|'W2'|'W3'|'full'
    win_start: str         # 'YYYY-MM-DD'
    win_end: str
    fee_bps: int
    slip_bps: int
    delay_ms: int
    cooldown_sec: float
    group: str             # 'main' | 'delay' | 'winner'


def build_matrix(windows: dict) -> list:
    """展開 main + delay 掃描 cell（優勝者掃描為實跑後另建，不在此）。

    windows: {name: (start, end)}，含 W1/W2/W3/full。
    main : factor × scenario × window × cost（6）= 144
    delay: factor × scenario × full × 基準 cost × {0,1000}ms = 12
    """
    cells = []
    for factor, scen, (wname, (ws, we)), (fee, slip) in product(
            FACTORS, SCENARIOS, windows.items(), COSTS):
        cells.append(Cell(factor, scen, wname, ws, we, fee, slip,
                          delay_ms=500, cooldown_sec=5.0, group="main"))
    fee_b, slip_b = BASELINE_COST
    ws, we = windows["full"]
    for factor, scen, delay in product(FACTORS, SCENARIOS, DELAYS_EXTRA):
        cells.append(Cell(factor, scen, "full", ws, we, fee_b, slip_b,
                          delay_ms=delay, cooldown_sec=5.0, group="delay"))
    return cells


def build_winner_sweep(winner_factor: float, windows: dict) -> list:
    """優勝者局部穩健掃描（spec §5 / §6.6）：基準 cost、全程窗口、兩場景。
    factor ±20%（2 新點）+ cooldown {2.5,5,10}s（cooldown=5 與 factor 點交叉）。"""
    fee_b, slip_b = BASELINE_COST
    ws, we = windows["full"]
    cells = []
    factors = [round(winner_factor * 0.8, 4), round(winner_factor * 1.2, 4)]
    for scen, f in product(SCENARIOS, factors):
        cells.append(Cell(f, scen, "full", ws, we, fee_b, slip_b,
                          delay_ms=500, cooldown_sec=5.0, group="winner"))
    for scen, cd in product(SCENARIOS, (2.5, 5.0, 10.0)):
        cells.append(Cell(winner_factor, scen, "full", ws, we, fee_b, slip_b,
                          delay_ms=500, cooldown_sec=cd, group="winner"))
    return cells


def gate_cells(results: list, min_events: int = 30) -> list:
    """獨立事件數（round_trips）守門：< min_events 的 cell 統計上不可信，濾除（spec §5）。"""
    return [r for r in results if r["round_trips"] >= min_events]


def _baseline(results, factor, scenario, window):
    for r in results:
        if (r["factor"] == factor and r["scenario"] == scenario
                and r["window"] == window and (r["fee_bps"], r["slip_bps"]) == BASELINE_COST
                and r.get("group", "main") == "main"):
            return r
    return None


def verdict_preview(results: list, calib_pass: bool = True,
                    winner_sweep: dict | None = None) -> dict:
    """spec §6.1-6.6 逐條預判（6.7 holdout 除外）。回傳 {"6.1": "PASS"|"FAIL"|"inconclusive", ...}。

    動詞照 spec 落地：§6.5 觸發是「降級（DEGRADE）」不是扣分；達標段不足是
    「inconclusive」不是硬裁。
    """
    v = {}

    # 6.1 校準 gate（外部輸入；Task 9 已全 PASS）
    v["6.1"] = "PASS" if calib_pass else "FAIL"

    # 6.2 新語意(1.0) 零強平，兩場景全窗口
    f10 = [r for r in results if r["factor"] == 1.0]
    if not f10:
        v["6.2"] = "inconclusive"
    elif any(r["liquidated"] for r in f10):
        v["6.2"] = "FAIL"
    else:
        v["6.2"] = "PASS"

    # 6.3 新語意 Δeq 三段全 ≥ 舊 + 全程為正；只計事件 ≥30 的 cell；達標段 < 2 → inconclusive
    seg = [r for r in results
           if r["factor"] == 1.0 and r["window"] in ("W1", "W2", "W3")
           and (r["fee_bps"], r["slip_bps"]) == BASELINE_COST
           and r.get("group", "main") == "main"]
    qualifying = [r for r in seg if r["round_trips"] >= 30]
    if len(qualifying) < 2:
        v["6.3"] = "inconclusive"
    else:
        full = [r for r in results
                if r["factor"] == 1.0 and r["window"] == "full"
                and (r["fee_bps"], r["slip_bps"]) == BASELINE_COST
                and r.get("group", "main") == "main"]
        seg_ok = all(r["delta_eq"] >= 0 for r in qualifying)
        full_ok = bool(full) and all(r["delta_eq"] > 0 for r in full)
        v["6.3"] = "PASS" if (seg_ok and full_ok) else "FAIL"

    # 6.4 cost sens 內排序不翻轉（全程窗口，逐場景比 factor 排序跨 cost 一致）
    v["6.4"] = _verdict_cost_sens(results)

    # 6.5 場景 A 拒單率 > 30% → 降級
    a10 = [r for r in results if r["factor"] == 1.0 and r["scenario"] == "A"]
    if not a10:
        v["6.5"] = "inconclusive"
    elif any(r["rejected_rate"] > REJECT_DEGRADE_THRESHOLD for r in a10):
        v["6.5"] = "DEGRADE"
    else:
        v["6.5"] = "PASS"

    # 6.6 優勝者穩健掃描（實跑後提供；未提供 → inconclusive）
    if winner_sweep is None:
        v["6.6"] = "inconclusive"
    else:
        v["6.6"] = "PASS" if winner_sweep.get("no_lone_peak") else "FAIL"

    return v


def _verdict_cost_sens(results: list) -> str:
    """全程窗口，逐場景取各 cost 下 factor 的 final_equity 排序，跨 cost 一致 → PASS。"""
    orderings = {}
    for scen in SCENARIOS:
        for fee, slip in COSTS:
            cell = {r["factor"]: r["final_equity"] for r in results
                    if r["window"] == "full" and r["scenario"] == scen
                    and (r["fee_bps"], r["slip_bps"]) == (fee, slip)
                    and r.get("group", "main") == "main"}
            if len(cell) < len(FACTORS):
                continue
            order = tuple(sorted(cell, key=lambda f: cell[f], reverse=True))
            orderings.setdefault(scen, set()).add(order)
    if not orderings:
        return "inconclusive"
    return "PASS" if all(len(s) == 1 for s in orderings.values()) else "FAIL"


# ===========================================================================
# 實跑薄殼
# ===========================================================================
def _make_cfg(cell: Cell, funding_events: list) -> TickSimConfig:
    sc = SCENARIOS[cell.scenario]
    return TickSimConfig(
        grid_spacing=PROD["grid_spacing"], take_profit_spacing=PROD["take_profit_spacing"],
        initial_quantity=PROD["initial_quantity"], leverage=PROD["leverage"],
        initial_balance=sc["initial_balance"],
        fee_pct=bps_to_fraction(cell.fee_bps),
        slippage_bps=bps_to_fraction(cell.slip_bps),
        threshold_multiplier=PROD["threshold_multiplier"],
        limit_multiplier=PROD["limit_multiplier"],
        requote_threshold_factor=cell.factor,
        cooldown_sec=cell.cooldown_sec, decision_delay_ms=cell.delay_ms,
        funding_events=funding_events,
        seed_long_qty=sc["seed_long_qty"], seed_long_price=sc["seed_long_price"],
        seed_short_qty=sc["seed_short_qty"], seed_short_price=sc["seed_short_price"],
    )


def _day_bounds_ms(start: str, end: str) -> tuple:
    """[start 00:00 UTC, end+1 00:00 UTC) 的 ms 邊界。"""
    s = int(dt.datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=_UTC).timestamp() * 1000)
    e = int((dt.datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=_UTC)
             + dt.timedelta(days=1)).timestamp() * 1000)
    return s, e


def _slice_events(all_events: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    s, e = _day_bounds_ms(start, end)
    return all_events[(all_events["ts_ms"] >= s) & (all_events["ts_ms"] < e)].reset_index(drop=True)


def _slice_funding(all_funding: list, start: str, end: str) -> list:
    s, e = _day_bounds_ms(start, end)
    return [(sec, r) for sec, r in all_funding if s / 1000 <= sec < e / 1000]


def _reject_rate(res) -> float:
    entry_fills = sum(1 for f in res.fills if f["kind"] == "entry")
    attempts = entry_fills + res.rejected_entries
    return res.rejected_entries / attempts if attempts > 0 else 0.0


def run_cell(cell: Cell, all_events: pd.DataFrame, all_funding: list) -> dict:
    ev = _slice_events(all_events, cell.win_start, cell.win_end)
    fev = _slice_funding(all_funding, cell.win_start, cell.win_end)
    res = run_tick_sim(ev, _make_cfg(cell, fev))
    return {
        "factor": cell.factor, "scenario": cell.scenario, "window": cell.window,
        "group": cell.group, "fee_bps": cell.fee_bps, "slip_bps": cell.slip_bps,
        "delay_ms": cell.delay_ms, "cooldown_sec": cell.cooldown_sec,
        "final_equity": res.final_equity, "max_dd": res.max_drawdown,
        "liquidated": res.liquidated,
        "fills": len(res.fills), "round_trips": res.round_trips,
        "rejected_rate": _reject_rate(res), "requote_count": res.requote_count,
        "n_events": len(ev), "delta_eq": None,   # 稍後填（vs factor=0.5 同窗同本同 cost）
    }


def _fill_delta_eq(results: list):
    """每 cell 的 Δeq = final_equity - 同 scenario/window/cost/delay/cooldown 的 factor=0.5 cell。"""
    base_idx = {}
    for r in results:
        if r["factor"] == BASELINE_FACTOR:
            key = (r["scenario"], r["window"], r["fee_bps"], r["slip_bps"],
                   r["delay_ms"], r["cooldown_sec"], r["group"])
            base_idx[key] = r["final_equity"]
    for r in results:
        key = (r["scenario"], r["window"], r["fee_bps"], r["slip_bps"],
               r["delay_ms"], r["cooldown_sec"], r["group"])
        base = base_idx.get(key)
        r["delta_eq"] = (r["final_equity"] - base) if base is not None else None


# ---------------------------------------------------------------------------
# W 切點：讀 1m close 06-06~07-10，最高/最低點日切三段（<5 天段與相鄰合併）
# ---------------------------------------------------------------------------
def compute_windows(w_start: str, w_split_end: str, full_end: str) -> tuple:
    """回傳 (windows_dict, notes)。windows: {W1,W2,W3,full}: (start,end)。"""
    from backtest.data_loader import DataLoader
    kl = DataLoader()
    bars = kl.load(SYMBOL, w_start, w_split_end)
    bars = bars.copy()
    bars["day"] = bars["open_time"].dt.strftime("%Y-%m-%d")
    by_day = bars.groupby("day")["close"]
    day_max = by_day.max()
    high_day = day_max.idxmax()
    low_day = by_day.min().idxmin()

    notes = [f"1m close 06-06~{w_split_end}：最高點日={high_day}（{day_max.max():.2f}）、"
             f"最低點日={low_day}（{by_day.min().min():.2f}）。kline 日檔為台北日界，"
             f"事件流為 UTC，窗口邊界 tz 對齊為近似（regime 分段用途，可接受）。"]

    def _addday(ds, n):
        return (dt.datetime.strptime(ds, "%Y-%m-%d") + dt.timedelta(days=n)).strftime("%Y-%m-%d")

    def _dcount(a, b):
        return (dt.datetime.strptime(b, "%Y-%m-%d") - dt.datetime.strptime(a, "%Y-%m-%d")).days + 1

    # 依 spec 定義切段；若 high/low 順序反轉導致 W2 無效，退化為兩段並標注
    raw = []
    if high_day <= low_day:
        raw = [("W1", w_start, high_day, "上升→峰"),
               ("W2", _addday(high_day, 1), low_day, "峰→谷"),
               ("W3", _addday(low_day, 1), w_split_end, "谷→末")]
    else:
        raw = [("W1", w_start, low_day, "下降→谷"),
               ("W2", _addday(low_day, 1), high_day, "谷→峰"),
               ("W3", _addday(high_day, 1), w_split_end, "峰→末")]
        notes.append("最低點日早於最高點日 → 段序調整為 谷/峰 排列。")

    # 過濾非法（start>end）並合併 <5 天段到相鄰段
    segs = [(n, s, e, r) for n, s, e, r in raw
            if dt.datetime.strptime(s, "%Y-%m-%d") <= dt.datetime.strptime(e, "%Y-%m-%d")]
    merged = []
    for n, s, e, r in segs:
        if _dcount(s, e) < 5 and merged:
            pn, ps, pe, pr = merged[-1]
            merged[-1] = (pn, ps, e, pr + "+" + r)
            notes.append(f"{n}（{s}~{e}，<5 天）併入 {pn}。")
        else:
            merged.append((n, s, e, r))
    # 重新命名為 W1/W2/W3（合併後可能 <3 段）
    windows, regimes = {}, {}
    for i, (_, s, e, r) in enumerate(merged, 1):
        windows[f"W{i}"] = (s, e)
        regimes[f"W{i}"] = r
    windows["full"] = (w_start, full_end)
    notes.append("窗口切點：" + "；".join(f"{k}={v[0]}~{v[1]}({regimes.get(k,'full')})"
                                          for k, v in windows.items()))
    return windows, notes, regimes


# ---------------------------------------------------------------------------
# spread 抖動敏感度（spec §4.2b）：基準 cell ±half-spread 抖動觸發價重跑
# ---------------------------------------------------------------------------
def spread_sensitivity(base_cell: Cell, all_events: pd.DataFrame, all_funding: list,
                       half_frac: float) -> dict:
    ev = _slice_events(all_events, base_cell.win_start, base_cell.win_end)
    fev = _slice_funding(all_funding, base_cell.win_start, base_cell.win_end)
    base = run_tick_sim(ev, _make_cfg(base_cell, fev))
    out = {"base_fills": len(base.fills), "base_requote": base.requote_count, "variants": {}}
    for tag, mult in (("+half", 1 + half_frac), ("-half", 1 - half_frac)):
        ev2 = ev.copy()
        ev2["price"] = ev2["price"] * mult
        r = run_tick_sim(ev2, _make_cfg(base_cell, fev))
        fill_chg = abs(len(r.fills) - len(base.fills)) / max(1, len(base.fills))
        rq_chg = abs(r.requote_count - base.requote_count) / max(1, base.requote_count)
        out["variants"][tag] = {"fills": len(r.fills), "requote": r.requote_count,
                                "fill_chg": fill_chg, "requote_chg": rq_chg}
    out["sensitive"] = any(x["fill_chg"] > 0.2 or x["requote_chg"] > 0.2
                           for x in out["variants"].values())
    return out


# ===========================================================================
# 報告
# ===========================================================================
def _fmt_cell_row(r) -> str:
    de = "n/a" if r["delta_eq"] is None else f"{r['delta_eq']:+.3f}"
    rt = f"{r['round_trips']}" + ("⚠" if r["round_trips"] < 30 else "")
    return (f"| {r['factor']} | {r['scenario']} | {r['window']} | "
            f"{r['fee_bps']}/{r['slip_bps']} | {r['delay_ms']} | {r['cooldown_sec']} | "
            f"{r['final_equity']:.3f} | {de} | {r['max_dd']*100:.2f}% | "
            f"{'Y' if r['liquidated'] else 'n'} | {r['fills']} | {rt} | "
            f"{r['rejected_rate']*100:.1f}% | {r['requote_count']} |")


def _pick_winner(results: list) -> float:
    """全程窗口、基準 cost、兩場景 final_equity 加總最高的 factor（僅供穩健掃描定錨）。"""
    score = {}
    for f in FACTORS:
        cells = [r for r in results if r["factor"] == f and r["window"] == "full"
                 and (r["fee_bps"], r["slip_bps"]) == BASELINE_COST
                 and r.get("group", "main") == "main"]
        if cells:
            score[f] = sum(c["final_equity"] for c in cells)
    return max(score, key=score.get) if score else 1.0


def write_report(path: str, *, results, windows, w_notes, regimes, spread_dist,
                 spread_sens, verdict, winner, timing, calib_note, n_total,
                 gated, winner_sweep_results, winner_sweep_summary):
    L = []
    L.append("# 追價語意實驗矩陣 — 結果與 §6 判準預判\n")
    L.append(f"生成：{dt.datetime.now(_UTC).isoformat()}  symbol={SYMBOL}\n")
    L.append("> holdout 05-01~06-05 全程未開封（§6.7 定案後才跑，不在本報告）。\n")

    L.append("\n## 0. 執行摘要\n")
    L.append(f"- 總組合數 N = {n_total}（main 144 + delay 12 + winner {len(winner_sweep_results)}）。")
    L.append(f"- 事件數守門（round_trips ≥ 30）後有效 cell：{len(gated)} / {len(results)}。")
    L.append(f"- 定錨優勝 factor（全程+基準 cost 兩場景 equity 加總最高）：**{winner}**。")
    L.append(f"- 總耗時：{timing['total']:.1f}s（單 cell 全程窗口計時 {timing['probe']:.2f}s，"
             f"外推見 §7）。")

    L.append("\n## 1. §6.1-6.6 判準預判（逐條，動詞照 spec）\n")
    labels = {
        "6.1": "校準 gate PASS",
        "6.2": "新語意(1.0) 零強平（兩場景全窗口）",
        "6.3": "Δeq W1/W2/W3 全 ≥ 舊 且全程為正（只計事件≥30；達標段<2→inconclusive）",
        "6.4": "cost sens 內排序不翻轉",
        "6.5": "場景 A 拒單率 ≤30%（>30% → 降級為上線需綁入金）",
        "6.6": "優勝者局部穩健掃描無孤峰",
    }
    L.append("| 判準 | 預判 | 說明 |")
    L.append("|---|---|---|")
    for k in ("6.1", "6.2", "6.3", "6.4", "6.5", "6.6"):
        L.append(f"| §{k} {labels[k]} | **{verdict[k]}** | |")
    L.append("\n§6.7 Holdout OOS：定案後單獨跑，本報告不預判（holdout 讀一次即失效）。\n")

    L.append("\n## 2. 窗口切點（W1/W2/W3）\n")
    for n in w_notes:
        L.append(f"- {n}")

    L.append("\n## 3. spread 分布（slip 假設依據，spec §4.2a）\n")
    L.append(f"- 全程 aggTrades：median={spread_dist['median_bps']:.3f}bps、"
             f"p90={spread_dist['p90_bps']:.3f}bps、n_pairs={spread_dist['n_pairs']}。")
    L.append(f"- slip {{0,1,2}}bps 覆蓋 median~p90，涵蓋觀測 spread 量級，假設合理。")

    L.append("\n## 4. spread 抖動敏感度（基準 cell ±half-spread 觸發價）\n")
    if spread_sens:
        L.append(f"- half-spread = median/2 = {spread_sens.get('half_frac', 0)*1e4:.3f}bps。")
        s = spread_sens["result"]
        L.append(f"- base fills={s['base_fills']}, requote={s['base_requote']}。")
        for tag, x in s["variants"].items():
            L.append(f"  - {tag}: fills={x['fills']}（Δ{x['fill_chg']*100:.1f}%）、"
                     f"requote={x['requote']}（Δ{x['requote_chg']*100:.1f}%）")
        L.append(f"- **對 spread 噪音{'敏感（結論信心降級）' if s['sensitive'] else '不敏感'}**。")

    L.append("\n## 5. 全 cell 表（main + delay）\n")
    L.append("factor | 本 | 窗 | fee/slip(bps) | delay | cd | final_eq | Δeq | maxDD | 強平 | fills | round_trips | 拒單率 | requote")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in sorted(results, key=lambda r: (r["group"] != "main", r["window"],
                                            r["scenario"], r["factor"],
                                            r["fee_bps"], r["slip_bps"], r["delay_ms"])):
        L.append(_fmt_cell_row(r))
    L.append("\n（round_trips 標 ⚠ = <30，樣本不足統計上不可信。）\n")

    if winner_sweep_results:
        L.append("\n## 6. 優勝者局部穩健掃描（factor±20% + cooldown{2.5,5,10}s）\n")
        L.append("factor | 本 | cd | final_eq | Δeq | round_trips")
        L.append("|---|---|---|---|---|---|")
        for r in sorted(winner_sweep_results, key=lambda r: (r["scenario"], r["cooldown_sec"], r["factor"])):
            de = "n/a" if r["delta_eq"] is None else f"{r['delta_eq']:+.3f}"
            L.append(f"| {r['factor']} | {r['scenario']} | {r['cooldown_sec']} | "
                     f"{r['final_equity']:.3f} | {de} | {r['round_trips']} |")
        L.append(f"\n- 掃描結論：{winner_sweep_summary}")

    L.append("\n## 7. 計時外推\n")
    L.append(f"- 單 cell（全程窗口，最重）計時：{timing['probe']:.2f}s。")
    L.append(f"- main+delay 156 cell 實測總耗時：{timing['matrix']:.1f}s。")
    L.append(f"- 全部（含 spread 敏感度 + 優勝掃描）：{timing['total']:.1f}s "
             f"= {timing['total']/60:.1f} 分。")

    L.append("\n## 8. 校準 gate 狀態\n")
    L.append(f"- {calib_note}")

    L.append("\n## 9. 已知局限（spec §8，隨結果交付）\n")
    L.append("- 單 symbol、in-sample（06-06 起）、無 walk-forward；OOS 僅 holdout 一段（未開封）。")
    L.append("- 只看方向與相對排序，不當精確預測；aggTrades 是成交流非報價流，spread 噪音殘餘偏差已標注。")
    L.append("- 高端 gate 不約束成交率量級上限，Δeq 絕對值可能偏樂觀，由方向排序+拒單/強平判準+holdout+上線首週觀察承接。")

    Path(path).write_text("\n".join(L) + "\n", encoding="utf-8")


# ===========================================================================
# main
# ===========================================================================
def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--end", required=True, help="全程窗口終點 YYYY-MM-DD")
    ap.add_argument("--out", required=True, help="報告輸出路徑")
    ap.add_argument("--probe-only", action="store_true",
                    help="只跑單 cell 計時外推，不跑全矩陣（>2h 風險預估）")
    args = ap.parse_args(argv)

    t_start = time.time()
    W_START = "2026-06-06"
    W_SPLIT_END = "2026-07-10"     # W 切點判定範圍（1m close 快取上界）

    agg = AggTradesLoader()
    print(f"# 實驗矩陣  symbol={SYMBOL}  full=06-06~{args.end}")

    # 載全程事件（07-01~07-11 缺日 loader 自動下載；holdout 05-01~06-05 絕不觸及）
    all_events, days = load_events(agg, W_START, args.end)
    all_funding = load_funding_events(W_START, args.end)
    print(f"  events={len(all_events)}  days={len(days)}  funding={len(all_funding)}")

    # spread 分布（全程）
    raw_full = pd.concat([agg.load_day(SYMBOL, d) for d in days], ignore_index=True)
    spread_dist = estimate_spread(raw_full)
    half_frac = (spread_dist["median_bps"] * 1e-4) / 2 if spread_dist["n_pairs"] else 0.0

    windows, w_notes, regimes = compute_windows(W_START, W_SPLIT_END, args.end)
    print(f"  windows={windows}")

    cells = build_matrix(windows)

    # 單 cell 計時外推（最重 = 全程窗口）
    probe_cell = next(c for c in cells if c.window == "full")
    tp = time.time()
    _ = run_cell(probe_cell, all_events, all_funding)
    probe = time.time() - tp
    n_full = sum(1 for c in cells if c.window == "full")
    n_sub = len(cells) - n_full
    full_ev = len(_slice_events(all_events, *windows["full"]))
    # 子窗口平均事件比例估算耗時
    est = probe * n_full + probe * n_sub * 0.4    # 子窗口約 <半 full
    print(f"  probe(full cell)={probe:.2f}s  外推 main+delay≈{est:.0f}s（{est/60:.1f}分）")

    if args.probe_only or est > 2 * 3600:
        print(f"  [ABORT] 外推 {est/60:.1f} 分" +
              (" > 2h，先回報" if est > 2 * 3600 else "（probe-only）"))
        return _write_probe_report(args.out, probe, est, windows, w_notes,
                                    spread_dist, len(all_events), n_full, n_sub)

    # 跑全矩陣
    t_mat = time.time()
    results = []
    for i, c in enumerate(cells):
        results.append(run_cell(c, all_events, all_funding))
        if (i + 1) % 20 == 0:
            print(f"    {i+1}/{len(cells)} cells  ({time.time()-t_mat:.0f}s)")
    _fill_delta_eq(results)
    matrix_elapsed = time.time() - t_mat

    gated = gate_cells(results)
    winner = _pick_winner(results)

    # 優勝者局部穩健掃描
    sweep_cells = build_winner_sweep(winner, windows)
    sweep_results = [run_cell(c, all_events, all_funding) for c in sweep_cells]
    # sweep 的 Δeq 對 factor=0.5 全程基準（per-scenario）
    for r in sweep_results:
        b = next((x["final_equity"] for x in results
                  if x["factor"] == BASELINE_FACTOR and x["scenario"] == r["scenario"]
                  and x["window"] == "full" and (x["fee_bps"], x["slip_bps"]) == BASELINE_COST
                  and x.get("group") == "main"), None)
        r["delta_eq"] = (r["final_equity"] - b) if b is not None else None
    sweep_summary, no_lone_peak = summarize_sweep(sweep_results, results, winner)

    # spread 抖動敏感度（基準 cell = 優勝 factor、場景 A、全程、基準 cost）
    base_cell = next(c for c in cells if c.factor == winner and c.scenario == "A"
                     and c.window == "full" and (c.fee_bps, c.slip_bps) == BASELINE_COST
                     and c.group == "main")
    ss = spread_sensitivity(base_cell, all_events, all_funding, half_frac)
    spread_sens = {"half_frac": half_frac, "result": ss}

    verdict = verdict_preview(results, calib_pass=True,
                              winner_sweep={"no_lone_peak": no_lone_peak})

    total = time.time() - t_start
    timing = {"probe": probe, "matrix": matrix_elapsed, "total": total}
    calib_note = "Task 9 三 gate（低端/高端/6月）已全 PASS（見 scripts/calibration_gate.py 實跑）。"
    n_total = len(results) + len(sweep_results)

    write_report(args.out, results=results, windows=windows, w_notes=w_notes,
                 regimes=regimes, spread_dist=spread_dist, spread_sens=spread_sens,
                 verdict=verdict, winner=winner, timing=timing, calib_note=calib_note,
                 n_total=n_total, gated=gated, winner_sweep_results=sweep_results,
                 winner_sweep_summary=sweep_summary)
    print(f"\n報告寫入 {args.out}  總耗時 {total:.1f}s")
    for k in ("6.1", "6.2", "6.3", "6.4", "6.5", "6.6"):
        print(f"  §{k} = {verdict[k]}")
    return 0


def summarize_sweep(sweep_results, main_results, winner):
    """孤峰判定（spec §5/§6.6，lessons 懸崖規則）：任一擾動下 Δeq **排序翻轉**或
    **衰減 >50%** = 孤峰不採納。逐場景判定——跨場景加總會掩蓋單場景翻轉。

    排序翻轉兩型：
      (a) 鄰點 factor 的 Δeq > 優勝（優勝非局部最佳 → 不採納單點，報 curve）；
      (b) cooldown 擾動下優勝 Δeq 翻負（優勝輸給 0.5 基準 → factor 排序翻轉）。
    衰減：優勝 Δeq 為正時，擾動點 Δeq < 0.5×優勝。
    """
    lone = False
    reasons = []
    for scen in sorted({r["scenario"] for r in sweep_results}):
        win_delta = next((r["delta_eq"] for r in main_results
                          if r["factor"] == winner and r["scenario"] == scen
                          and r["window"] == "full"
                          and (r["fee_bps"], r["slip_bps"]) == BASELINE_COST
                          and r.get("group") == "main" and r["delta_eq"] is not None), None)
        if win_delta is None:
            continue
        for r in sweep_results:
            if r["scenario"] != scen or r["delta_eq"] is None:
                continue
            d = r["delta_eq"]
            if r["factor"] != winner:               # 鄰點 factor（cooldown=基準）
                if d > win_delta:
                    lone = True
                    reasons.append(f"[{scen}] factor {r['factor']} Δeq({d:+.3f}) > "
                                   f"優勝({win_delta:+.3f}) → 排序翻轉")
                elif win_delta > 0 and d < 0.5 * win_delta:
                    lone = True
                    reasons.append(f"[{scen}] factor {r['factor']} Δeq({d:+.3f}) 衰減 >50%")
            else:                                    # cooldown 擾動
                if win_delta > 0 and d < 0:
                    lone = True
                    reasons.append(f"[{scen}] cooldown {r['cooldown_sec']}s "
                                   f"Δeq({d:+.3f}) 翻負 → 排序翻轉（輸給 0.5）")
                elif win_delta > 0 and d < 0.5 * win_delta:
                    lone = True
                    reasons.append(f"[{scen}] cooldown {r['cooldown_sec']}s "
                                   f"Δeq({d:+.3f}) 衰減 >50%")
    summary = ("孤峰（不採納單點，報 sensitivity curve）：" + "；".join(reasons)) if lone \
        else "無孤峰：逐場景鄰點/cooldown 擾動均未見排序翻轉或 >50% 衰減。"
    return summary, (not lone)


def _write_probe_report(path, probe, est, windows, w_notes, spread_dist,
                        n_events, n_full, n_sub) -> int:
    L = ["# 追價語意實驗矩陣 — 計時外推（矩陣未跑）\n",
         f"生成：{dt.datetime.now(_UTC).isoformat()}\n",
         "\n## 計時外推\n",
         f"- 單 cell（全程窗口）：{probe:.2f}s；全程事件數={n_events}。",
         f"- main+delay 156 cell（{n_full} full + {n_sub} 子窗口）外推：{est:.0f}s = {est/60:.1f} 分。",
         "\n## 窗口切點\n"] + [f"- {n}" for n in w_notes] + [
         "\n## spread 分布\n",
         f"- median={spread_dist['median_bps']:.3f}bps p90={spread_dist['p90_bps']:.3f}bps "
         f"n_pairs={spread_dist['n_pairs']}。",
         "\n## 決策\n",
         f"- 外推 {est/60:.1f} 分" + (" > 2h → 依 brief 先回報使用者等指示，不硬跑。"
                                       if est > 2*3600 else "（probe-only 模式）。")]
    Path(path).write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"probe 報告寫入 {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
