#!/usr/bin/env python3
"""成本敏感度網格：fee × slippage 對回測結論的影響（spec G7 / 守門 G-0c1）。

成本按成交次數收，會系統性偏袒低換手方案。比較換手率差異大的方案時
（例如「關掉裝死模式」vs「維持現狀」），成本模型的誤差可能直接決定排序。
spec §8 Phase D：若排序在 fee ∈ {2,4} bps × slippage ∈ {0,1,2} bps 範圍內翻轉，
不得下結論。

用法:
    uv run python scripts/cost_sensitivity.py <csv/parquet 檔案、glob pattern 或目錄> \
        [--symbol BNBUSDC] [--threshold-multiplier 5,10,20,1e9]

`--threshold-multiplier` 是一組要比較的策略選項（裝死模式 threshold_multiplier）。
極大值（如 1e9）代表「關掉裝死模式」——position_threshold 大到永遠不會觸發，
與 dead_mode_enabled=False 效果等價，但仍走同一套撮合/裝死判斷程式碼路徑。

輸出：每個 (fee, slippage) 組合一行，含各 threshold_multiplier 選項的
final_equity（並標出最佳者）；最後印出排序是否翻轉的結論。
"""
import argparse
import glob
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from backtest.backtester import GridBacktester
from backtest.config import Config

FEES = [0.0002, 0.0004]              # maker 2bps / taker 4bps
SLIPPAGES = [0.0, 0.0001, 0.0002]    # 0 / 1bp / 2bps


def _load(path: str) -> pd.DataFrame:
    """讀取單一或多個 csv/parquet 檔案（支援 glob pattern 與目錄），依 open_time 排序合併。"""
    paths: list[str]
    if os.path.isdir(path):
        paths = sorted(
            glob.glob(os.path.join(path, "*.csv")) + glob.glob(os.path.join(path, "*.parquet"))
        )
    else:
        matched = sorted(glob.glob(path))
        paths = matched if matched else [path]

    if not paths:
        raise FileNotFoundError(f"找不到符合的資料檔案: {path}")

    frames = []
    for p in paths:
        df = pd.read_parquet(p) if p.endswith(".parquet") else pd.read_csv(p)
        if "open_time" in df.columns and not pd.api.types.is_datetime64_any_dtype(df["open_time"]):
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        frames.append(df)

    df = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
    df = df.sort_values("open_time").reset_index(drop=True)
    return df


def _label(mult: float) -> str:
    """threshold_multiplier 的人類可讀標籤。極大值代表『關掉裝死模式』的近似。"""
    if mult >= 1e8:
        return "關掉裝死"
    return f"mult{mult:g}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("data", help="csv/parquet 檔案、glob pattern，或含 csv/parquet 的目錄")
    ap.add_argument("--symbol", default="BNBUSDC")
    ap.add_argument("--initial-balance", type=float, default=100.0)
    ap.add_argument("--initial-quantity", type=float, default=0.02)
    ap.add_argument("--leverage", type=int, default=20)
    ap.add_argument("--grid-spacing", type=float, default=0.003)
    ap.add_argument("--take-profit-spacing", type=float, default=0.003)
    ap.add_argument("--direction", default="both")
    ap.add_argument(
        "--threshold-multiplier",
        default="5,10,20,1e9",
        help="逗號分隔的 threshold_multiplier 清單，代表要比較的策略選項（預設 5,10,20,1e9；"
             "1e9 = 近似『關掉裝死模式』）",
    )
    args = ap.parse_args()

    options = [float(x) for x in args.threshold_multiplier.split(",")]
    labels = [_label(m) for m in options]

    df = _load(args.data)
    print(f"資料: {args.data} -> {len(df)} 根 K 線 "
          f"({df['open_time'].iloc[0]} ~ {df['open_time'].iloc[-1]})")
    print(f"策略選項: {', '.join(labels)}")
    print()

    header = f"{'fee(bps)':>9} {'slip(bps)':>10}"
    for lb in labels:
        header += f" {lb+'.equity':>16}"
    header += f" {'trades('+'/'.join(labels)+')':>28} {'liquidated':>14} {'best':>10}"
    print(header)
    print("-" * len(header))

    # results[(fee, slip)][label] = (final_equity, trades_count, liquidated, peak_margin_usage)
    results: dict = {}

    for fee in FEES:
        for slip in SLIPPAGES:
            row_results = {}
            for mult, lb in zip(options, labels):
                cfg = Config(
                    symbol=args.symbol,
                    initial_balance=args.initial_balance,
                    initial_quantity=args.initial_quantity,
                    leverage=args.leverage,
                    grid_spacing=args.grid_spacing,
                    take_profit_spacing=args.take_profit_spacing,
                    direction=args.direction,
                    terminal_ui_mode=True,
                    fee_pct=fee,
                    slippage_bps=slip,
                    threshold_multiplier=mult,
                )
                r = GridBacktester(df.copy(), cfg).run()
                row_results[lb] = (r.final_equity, r.trades_count, r.liquidated, r.peak_margin_usage)
            results[(fee, slip)] = row_results

            best_lb = max(row_results, key=lambda k: row_results[k][0])
            liq_flags = [lb for lb, v in row_results.items() if v[2]]

            line = f"{fee*1e4:>9.1f} {slip*1e4:>10.1f}"
            for lb in labels:
                line += f" {row_results[lb][0]:>16.3f}"
            trades_str = "/".join(str(row_results[lb][1]) for lb in labels)
            line += f" {trades_str:>28}"
            liq_str = ",".join(liq_flags) if liq_flags else "-"
            line += f" {liq_str:>14}"
            line += f" {best_lb:>10}"
            print(line)

            if liq_flags:
                print(f"  !! liquidated=True: {liq_flags} —— spec §7 一票否決，不進優化目標函數")

    # ---- 結論：排序是否翻轉 ----
    print()
    print("=" * 70)
    best_per_row = {}
    for key, row_results in results.items():
        sorted_labels = sorted(row_results, key=lambda k: row_results[k][0], reverse=True)
        best_per_row[key] = sorted_labels

    distinct_best = {tuple(v)[0] for v in best_per_row.values()}
    if len(distinct_best) > 1:
        print("警告：不同成本設定下最佳選項不同 —— 排序翻轉！")
        print("spec §8 Phase D 規定：排序若在合理成本範圍內翻轉，不得下結論。")
        for key, sl in best_per_row.items():
            fee, slip = key
            print(f"  fee={fee*1e4:.1f}bps slip={slip*1e4:.1f}bps -> 最佳={sl[0]}")
    else:
        best_label = next(iter(distinct_best))
        print(f"排序未翻轉：所有 fee×slippage 組合下最佳選項皆為 {best_label}")

        # 最佳與次佳的差距在成本擾動下如何變化
        gaps = []
        for key, row_results in results.items():
            vals = sorted((v[0] for v in row_results.values()), reverse=True)
            if len(vals) >= 2:
                gaps.append((key, vals[0] - vals[1]))
        if gaps:
            min_gap = min(g for _, g in gaps)
            max_gap = max(g for _, g in gaps)
            ratio = (max_gap / min_gap) if min_gap > 0 else float("inf")
            print(f"最佳/次佳差距（final_equity）: 最小 {min_gap:.3f} ~ 最大 {max_gap:.3f}"
                  f"（成本擾動下放大 {ratio:.1f} 倍）")
            print("提醒：差距若小於成本擾動造成的變化，結論仍不穩健。")
            for key, g in gaps:
                fee, slip = key
                print(f"  fee={fee*1e4:.1f}bps slip={slip*1e4:.1f}bps -> gap={g:.3f}")

    print()
    print("判讀：liquidated=True 的參數組一票否決，不進優化目標函數（spec §7）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
