"""spec §7 一票否決：liquidated=True 的參數組不得進入 best/gap 的計算。

歷史脈絡：這條規則已經被磨損過兩次——backtest/optimizer.py 一度用「哨兵值」
近似否決（連錯三次才改成顯式 eligible 過濾），scripts/cost_sensitivity.py 則
一度只印了一行文字「一票否決」，但 best/gap 的實際計算仍吃全部選項（含強平）。
本檔測試 backtest/cost_sensitivity_core.py 的 select_best/compute_gap，
確保「否決」是程式碼行為，不只是印出來的字。
"""
from backtest.cost_sensitivity_core import select_best, compute_gap


def test_liquidated_option_never_selected_as_best_even_with_highest_equity():
    """守 spec §7 一票否決：強平選項即使帳面分數最高也不得入選。

    若本測試失紅，代表 select_best() 沒有尊重 liquidated 旗標，
    scripts/cost_sensitivity.py 的排序邏輯會把「回測期間實際爆倉」的參數組
    當成合格解推薦給使用者，使用者可能據此調整實盤參數。

    為什麼「列出來的文字說尊重 liquidated」不等於程式碼真的尊重：本檔的
    前一個版本，select_best/compute_gap 確實印出了「會排除強平」的說明，
    但實際計算卻照樣吃全部選項，因此留下可被動態利用的漏洞。
    """
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
    """守 spec §7 一票否決的邊界情況：所有參數組都強平時無最佳解。

    若本測試失紅，代表當所有候選都被否決時，select_best() 沒有正確回傳
    (None, None)，可能會返回某個爆倉組的 final_equity 或其他誤導的值。
    使用者一旦信了這個回傳值，就會選用一組在實盤上必然爆倉的參數。
    """
    row_results = {
        "mult5": (100.0, 10, True, 0.99),
        "mult10": (95.0, 8, True, 0.98),
    }
    best_lb, best_equity = select_best(row_results)
    assert best_lb is None
    assert best_equity is None


def test_gap_computed_only_over_eligible_options():
    """守 spec §7 一票否決：gap 計算必須排除強平組合。

    若本測試失紅，代表 compute_gap() 的分母計算沒有尊重 liquidated 旗標，
    會錯誤地用「帳面最高組 vs 第二高組」的差距代替「合格組間的差距」。
    這會導致 cost_sensitivity 分析輸出虛假的 convergence gap，
    使用者無法正確判斷參數的敏感性。

    之所以要明確測試 gap，是因為它與 select_best 的邏輯分離（雖然都檢查
    liquidated），單純相信「印出來的 best 沒含強平」仍不足以保證 gap 計算
    也排除了它們。
    """
    # 最高者強平 → gap 應為第二、三名之差，而非第一、二名之差
    row_results = {
        "mult5": (999.0, 50, True, 0.99),   # 最高，但強平
        "mult10": (100.0, 10, False, 0.5),  # eligible 第一
        "mult20": (90.0, 8, False, 0.4),    # eligible 第二
    }
    gap = compute_gap(row_results)
    assert gap == 10.0  # 100.0 - 90.0，不是 999.0 - 100.0
