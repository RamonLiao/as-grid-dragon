"""cost_sensitivity.py 的純函數核心（一票否決邏輯）。

抽出成獨立模組（而非留在 scripts/cost_sensitivity.py 內）是因為 scripts/ 不是
package，無法用一般 import 方式在 tests/ 下測試；放進 backtest/ 讓
`from backtest.cost_sensitivity_core import select_best, compute_gap` 可直接
import，不需 importlib.util 動態載入這種較脆弱的路徑。

spec §7：liquidated=True 的參數組一票否決，不進優化目標函數（不得被選為 best，
也不得計入 best/次佳 gap 的計算）。
"""
from __future__ import annotations

RowResults = dict  # label -> (final_equity, trades_count, liquidated, peak_margin_usage)


def select_best(row_results: RowResults) -> tuple[str | None, float | None]:
    """在未強平（liquidated=False）的選項中挑 final_equity 最高者。

    回傳 (best_label, best_equity)；若全部選項都強平，回傳 (None, None)。
    """
    eligible = {k: v for k, v in row_results.items() if not v[2]}
    if not eligible:
        return None, None
    best_lb = max(eligible, key=lambda k: eligible[k][0])
    return best_lb, eligible[best_lb][0]


def compute_gap(row_results: RowResults) -> float | None:
    """最佳與次佳 final_equity 的差距，只在未強平的選項間計算。

    若 eligible 選項少於 2 個，回傳 None（無法算 gap）。
    """
    eligible_vals = sorted((v[0] for v in row_results.values() if not v[2]), reverse=True)
    if len(eligible_vals) < 2:
        return None
    return eligible_vals[0] - eligible_vals[1]
