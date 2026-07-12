#!/usr/bin/env python3
"""成本敏感度網格：fee × slippage 對回測結論的影響（spec G7 / 守門 G-0c1）。

成本按成交次數收，會系統性偏袒低換手方案。比較換手率差異大的方案時
（例如「關掉裝死模式」vs「維持現狀」），成本模型的誤差可能直接決定排序。
spec §8 Phase D：若排序在 fee ∈ {2,4} bps × slippage ∈ {0,1,2} bps 範圍內翻轉，
不得下結論。

spec §7「一票否決」：liquidated=True 的參數組不進 best/gap 的計算（見
backtest/cost_sensitivity_core.py 的 select_best/compute_gap）。這條規則歷史上
被磨損過兩次（optimizer 哨兵值、本 script 曾經只印文字不過濾），本次以顯式
eligible 過濾實作，並有 tests/test_cost_sensitivity_veto.py 鎖住行為。

副作用警告：`--funding-enabled`（Config 預設 funding_enabled=True）在本地缺
`data/funding/<symbol>.csv` 快取時，會透過 backtest/data_loader.py 呼叫
`_create_exchange("binance")` 對外發出網路請求（fetch_funding_rate_history）。
失敗時 data_loader 內部用 `except Exception: pass` 靜默吞掉，使用者會拿到
`funding_paid=0` 卻不知道發生過連網嘗試。本 script 執行前會檢查快取檔是否存在
並印出警告（見 `_check_funding_cache`）。

用法:
    uv run python scripts/cost_sensitivity.py <csv/parquet 檔案、glob pattern 或目錄> \
        [--symbol BNBUSDC] [--threshold-multiplier 5,10,20,1e9]

`--threshold-multiplier` 是一組要比較的策略選項（裝死模式 threshold_multiplier）。
極大值（如 1e9）代表「關掉裝死模式」——position_threshold 大到永遠不會觸發，
與 dead_mode_enabled=False 效果等價，但仍走同一套撮合/裝死判斷程式碼路徑。

輸出：每個 (fee, slippage) 組合一行，含各 threshold_multiplier 選項的
final_equity（並標出最佳者；liquidated=True 的選項標 `*`，不進 best/gap 評選）；
最後印出排序是否翻轉的結論。輸出前會印出完整生效 Config，供結果重現。
"""
import argparse
import glob
import os
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from backtest.backtester import GridBacktester
from backtest.config import Config
from backtest.cost_sensitivity_core import select_best, compute_gap

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


def _label(mult: float, initial_quantity: float) -> str:
    """threshold_multiplier 的人類可讀標籤，附上實際 position_threshold 值供讀者判斷。

    不用硬編界線（如 mult >= 1e8）判斷「是否等同關掉裝死」——那條界線本身是否
    合理取決於資料期間內可能的最大持倉，隨資料而變。改成直接標出 threshold 值，
    讀者可自行對照該次回測的實際持倉量判斷這個選項是否形同「關閉」。
    """
    threshold = initial_quantity * mult
    tag = "關掉裝死" if mult >= 1e8 else f"mult{mult:g}"
    return f"{tag}(thr={threshold:.4g})"


def _check_funding_cache(symbol: str, funding_enabled: bool) -> None:
    """funding_enabled=True 且本地無快取 → 警告：會靜默嘗試連網，失敗則靜默得到 funding_paid=0。"""
    if not funding_enabled:
        return
    cache_path = Path(__file__).resolve().parent.parent / "data" / "funding" / f"{symbol}.csv"
    if not cache_path.exists():
        print(f"!! 警告: {cache_path} 不存在。funding_enabled=True 會嘗試呼叫 "
              f"backtest/data_loader.py 的 load_funding() 對外連網抓取（binance）；"
              f"失敗時會被靜默吞掉（except Exception: pass），使用者拿到的 "
              f"funding_paid=0 可能只是網路失敗的結果，而非真的沒有 funding 現金流。")
        print(f"   建議：先用 DataLoader.load_funding() 預抓快取到 {cache_path}，"
              f"再執行本 script。")
        print()


def _print_config_header(cfg: Config) -> None:
    """印出完整生效 Config（排除 property），供讀者從輸出反推設定、重現結果。"""
    d = asdict(cfg)
    varying = {"fee_pct", "slippage_bps", "threshold_multiplier"}
    print("參數:")
    for k, v in d.items():
        note = "（此三項在網格上變動，見下表）" if k in varying else ""
        print(f"  {k} = {v} {note}")
    print()


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
    labels = [_label(m, args.initial_quantity) for m in options]

    _check_funding_cache(args.symbol, Config().funding_enabled)

    df = _load(args.data)
    print(f"資料: {args.data} -> {len(df)} 根 K 線 "
          f"({df['open_time'].iloc[0]} ~ {df['open_time'].iloc[-1]})")
    print(f"策略選項: {', '.join(labels)}")
    print()

    # 印出實際生效的 Config（挑任一組合；fee/slip/threshold_multiplier 這三項在網格上變動）
    sample_cfg = Config(
        symbol=args.symbol,
        initial_balance=args.initial_balance,
        initial_quantity=args.initial_quantity,
        leverage=args.leverage,
        grid_spacing=args.grid_spacing,
        take_profit_spacing=args.take_profit_spacing,
        direction=args.direction,
        terminal_ui_mode=True,
        fee_pct=FEES[0],
        slippage_bps=SLIPPAGES[0],
        threshold_multiplier=options[0],
    )
    _print_config_header(sample_cfg)

    header = f"{'fee(bps)':>9} {'slip(bps)':>10}"
    for lb in labels:
        header += f" {lb+'.equity':>24}"
    header += f" {'peak_mu('+'/'.join(labels)+')':>30}"
    header += f" {'trades('+'/'.join(labels)+')':>28} {'liquidated':>14} {'best':>10}"
    print(header)
    print("-" * len(header))
    print("圖例：* = 已強平（liquidated=True），不進 best/gap 評選（spec §7 一票否決）。")
    print()

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

            best_lb, best_equity = select_best(row_results)
            liq_flags = [lb for lb, v in row_results.items() if v[2]]

            line = f"{fee*1e4:>9.1f} {slip*1e4:>10.1f}"
            for lb in labels:
                eq = row_results[lb][0]
                mark = "*" if row_results[lb][2] else " "
                line += f" {eq:>22.3f}{mark}"
            peak_mu_str = "/".join(f"{row_results[lb][3]:.3f}" for lb in labels)
            line += f" {peak_mu_str:>30}"
            trades_str = "/".join(str(row_results[lb][1]) for lb in labels)
            line += f" {trades_str:>28}"
            liq_str = ",".join(liq_flags) if liq_flags else "-"
            line += f" {liq_str:>14}"
            line += f" {(best_lb if best_lb is not None else '—'):>10}"
            print(line)

            if best_lb is None:
                print(f"  !! fee={fee*1e4:.1f}bps slip={slip*1e4:.1f}bps："
                      f"全部選項皆強平，此成本設定下無可用結論。")
            elif liq_flags:
                print(f"  !! liquidated=True: {liq_flags} —— spec §7 一票否決，"
                      f"不進 best/gap 評選（僅列出其 final_equity 供檢視）")

    # ---- 結論：排序是否翻轉 ----
    # 只用有 eligible best 的行（best_lb is not None）判斷排序穩定性；
    # 全部選項皆強平的行不納入「排序是否翻轉」的判斷。
    print()
    print("=" * 70)
    best_per_row = {}
    excluded_rows = []
    for key, row_results in results.items():
        best_lb, _ = select_best(row_results)
        if best_lb is None:
            excluded_rows.append(key)
            continue
        best_per_row[key] = best_lb

    if excluded_rows:
        print("下列成本設定全部選項皆強平，已排除於排序翻轉判斷之外：")
        for fee, slip in excluded_rows:
            print(f"  fee={fee*1e4:.1f}bps slip={slip*1e4:.1f}bps")

    if not best_per_row:
        print("所有成本設定下皆全部選項強平 —— 無可用結論。")
    else:
        distinct_best = set(best_per_row.values())
        if len(distinct_best) > 1:
            print("警告：不同成本設定下最佳選項不同 —— 排序翻轉！")
            print("spec §8 Phase D 規定：排序若在合理成本範圍內翻轉，不得下結論。")
            for key, lb in best_per_row.items():
                fee, slip = key
                print(f"  fee={fee*1e4:.1f}bps slip={slip*1e4:.1f}bps -> 最佳={lb}")
        else:
            best_label = next(iter(distinct_best))
            print(f"排序未翻轉：所有 fee×slippage 組合下最佳選項皆為 {best_label}"
                  f"（不含強平全滅的行；若上方有排除行，此結論僅涵蓋未排除的部分）")

            # 最佳與次佳的差距在成本擾動下如何變化（只用 eligible 選項計算）
            gaps = []
            for key in best_per_row:
                g = compute_gap(results[key])
                if g is not None:
                    gaps.append((key, g))
            if gaps:
                min_gap = min(g for _, g in gaps)
                max_gap = max(g for _, g in gaps)
                ratio = (max_gap / min_gap) if min_gap > 0 else float("inf")
                print(f"最佳/次佳差距（final_equity，僅計 eligible 選項）: "
                      f"最小 {min_gap:.3f} ~ 最大 {max_gap:.3f}（成本擾動下放大 {ratio:.1f} 倍）")
                print("提醒：差距若小於成本擾動造成的變化，結論仍不穩健。")
                for key, g in gaps:
                    fee, slip = key
                    print(f"  fee={fee*1e4:.1f}bps slip={slip*1e4:.1f}bps -> gap={g:.3f}")

    print()
    print("判讀：liquidated=True 的參數組一票否決（不進 best/gap 的計算），"
          "由 backtest/cost_sensitivity_core.py 的 select_best/compute_gap 顯式過濾"
          "（spec §7；見 tests/test_cost_sensitivity_veto.py）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
