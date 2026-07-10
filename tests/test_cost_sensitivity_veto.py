"""spec §7 一票否決：liquidated=True 的參數組不得進入 best/gap 的計算。

歷史脈絡：這條規則已經被磨損過兩次——backtest/optimizer.py 一度用「哨兵值」
近似否決（連錯三次才改成顯式 eligible 過濾），scripts/cost_sensitivity.py 則
一度只印了一行文字「一票否決」，但 best/gap 的實際計算仍吃全部選項（含強平）。
本檔測試 backtest/cost_sensitivity_core.py 的 select_best/compute_gap，
確保「否決」是程式碼行為，不只是印出來的字。
"""
from backtest.cost_sensitivity_core import select_best, compute_gap


def test_liquidated_option_never_selected_as_best_even_with_highest_equity():
    # 強平選項帳面 final_equity 最高（強平發生在回測尾段前累積出的假象）
    row_results = {
        "mult5": (100.0, 10, False, 0.5),
        "mult10": (95.0, 8, False, 0.4),
        "mult20": (999.0, 50, True, 0.99),  # 強平但 equity 帳面最高 → 不得選中
    }
    best_lb, best_equity = select_best(row_results)
    assert best_lb != "mult20"
    assert best_lb == "mult5"
    assert best_equity == 100.0


def test_all_liquidated_row_yields_no_best():
    row_results = {
        "mult5": (100.0, 10, True, 0.99),
        "mult10": (95.0, 8, True, 0.98),
    }
    best_lb, best_equity = select_best(row_results)
    assert best_lb is None
    assert best_equity is None


def test_gap_computed_only_over_eligible_options():
    # 最高者強平 → gap 應為第二、三名之差，而非第一、二名之差
    row_results = {
        "mult5": (999.0, 50, True, 0.99),   # 最高，但強平
        "mult10": (100.0, 10, False, 0.5),  # eligible 第一
        "mult20": (90.0, 8, False, 0.4),    # eligible 第二
    }
    gap = compute_gap(row_results)
    assert gap == 10.0  # 100.0 - 90.0，不是 999.0 - 100.0
