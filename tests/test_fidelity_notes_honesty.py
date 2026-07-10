"""
FIDELITY_NOTES 誠實性自動化守門

誠實化不是一件做完的事，是一個要持續成立的性質。

FIDELITY_NOTES 是使用者可見輸出，寫進 BacktestResult.notes。若內容說謊或過時，
就是對使用者宣告「回測參數 ≈ 實盤參數」，實際上卻跑了不同的策略或成本模型。

手動 grep 驗收（spec G-0c1/G-0c2）在 review 當下有效，之後無人守。本模組就是
讓它持續成立的機制：每次 PR 都會檢查 FIDELITY_NOTES 的關鍵性質，防止日後誰把
fee_pct 改回 taker、在 notes 重新寫入「刻意保守下界」、或指向虛空的 forward reference。
"""
import os
from pathlib import Path
import pytest
from backtest.backtester import FIDELITY_NOTES
from backtest.config import Config


class TestFidelityNotesHonesty:
    """FIDELITY_NOTES 的關鍵性質守門測試"""

    def test_notes_do_not_claim_conservative_lower_bound(self):
        """成本下界宣稱必須精誠化

        撮合修正前，回測每筆成交送出 mean 10.38 bps 的幻覺價格改善（是所建模 slippage 1bp 的 10 倍），
        「保守下界」的宣稱從來就是錯的。

        修正後成本仍非方向中性（fee 與 slippage 都按成交次數收，系統性偏袒低換手方案），
        也不能宣稱下界。任何「照 config 建 Config 跑回測」的做法測的都不是實盤策略。

        使用者若看到「保守下界」會誤認為結果必然優於實盤。若日後有人改回舊宣稱文本，
        本測試轉紅。
        """
        assert "刻意保守下界" not in FIDELITY_NOTES, (
            "FIDELITY_NOTES 嘗試宣稱保守下界，但成本模型實際上會系統性偏袒低換手方案。"
            "這對使用者說謊，導致對結果誤解為「必然優於實盤」。"
        )
        assert "不宣稱保守下界" in FIDELITY_NOTES, (
            "FIDELITY_NOTES 應明確說明「不宣稱保守下界」。若訊息改為宣稱下界，"
            "使用者就會被誤導。"
        )

    def test_notes_do_not_claim_close_price_fills(self):
        """撮合價格必須準確揭露

        Task 2 已把撮合改成「用 high/low 判穿越、成交於掛單價」。舊語意留在 notes 裡就是
        對使用者說謊，隱含下單會「當根收盤價成交」，實際操作恆定於掛單價。

        若有人後來把撮合改回收盤價，或改動 notes 但忘記同步，本測試轉紅。
        """
        assert "當根收盤價成交" not in FIDELITY_NOTES, (
            "FIDELITY_NOTES 不得宣稱撮合於收盤價。實際撮合策略是用 high/low 判穿越、"
            "成交於掛單價。宣稱收盤價是對使用者說謊。"
        )
        assert "掛單價" in FIDELITY_NOTES, (
            "FIDELITY_NOTES 應明確說明撮合於「掛單價」。若改為收盤價或其他價格，"
            "本測試轉紅。"
        )

    def test_notes_reference_an_existing_cost_sensitivity_script(self):
        """FIDELITY_NOTES 中提到的檔案必須存在

        notes 是使用者可見輸出。它引用的檔案若不存在就是指向虛空的 forward reference。
        之前出現過兩次：should_liquidate docstring 指向當時還沒寫的 notes 條目；
        notes 指向當時還沒建的 scripts/cost_sensitivity.py。

        本測試防止 forward reference 重演：notes 寫了檔案名，檔案就必須在。
        若有人刪除了 cost_sensitivity.py 但忘記改 notes，或反之，本測試轉紅。
        """
        # 定位專案根（pytest cwd 不一定 == 專案根）
        project_root = Path(__file__).resolve().parents[1]
        script_path = project_root / "scripts" / "cost_sensitivity.py"

        assert "cost_sensitivity" in FIDELITY_NOTES, (
            "FIDELITY_NOTES 應提及 cost_sensitivity 以指導高換手策略的成本比較。"
            "若刪除了此提及，使用者無法知道如何正確使用高換手方案。"
        )
        assert script_path.exists(), (
            f"FIDELITY_NOTES 引用 scripts/cost_sensitivity.py，但檔案不存在：{script_path}。"
            "這是對使用者的 forward reference：無法執行 notes 指導的驗證步驟。"
        )

    def test_notes_disclose_bandit_overwrites_config_spacing(self):
        """Bandit 行為差異必須明確揭露

        實盤 bandit.enabled=true 時會【無條件覆寫】grid_spacing/take_profit_spacing。
        任何「照 config 建 Config 跑回測」的做法測的都不是實盤策略。

        notes 必須說明這個行為，防止使用者誤認為「config 裡的間距 == 實盤間距」。
        若有人改回舊文本或刪掉 bandit 提及，本測試轉紅。
        """
        assert "bandit" in FIDELITY_NOTES, (
            "FIDELITY_NOTES 應揭露 bandit 會覆寫 config 值。若刪除此警告，"
            "使用者會誤認為 config 值與實盤一致。"
        )
        assert "grid_engine/bot.py" in FIDELITY_NOTES, (
            "FIDELITY_NOTES 應指向實際發生覆寫的程式碼位置（grid_engine/bot.py）。"
            "若改為其他檔案或刪除檔案位置，使用者無法查證此警告。"
        )

    def test_notes_disclose_liquidation_model_is_a_single_rate_proxy(self):
        """強平模型的簡化必須揭露

        should_liquidate 用單一 maintenance_margin_rate 代理幣安的分層階梯。
        這個簡化必須揭露，否則使用者會誤認為「強平判定 == 實盤強平判定」。

        若有人改回舊文本、刪掉「強平」或「maintenance」提及、或改為其他詞彙，
        本測試轉紅。
        """
        has_liquidation_mention = "強平" in FIDELITY_NOTES
        has_rate_mention = "maintenance" in FIDELITY_NOTES or "maintenance_margin_rate" in FIDELITY_NOTES

        assert has_liquidation_mention, (
            "FIDELITY_NOTES 應明確提及「強平」模型。若刪除此詞彙，"
            "使用者無法識別回測強平邏輯與實盤的差異。"
        )
        assert has_rate_mention, (
            "FIDELITY_NOTES 應明確說明強平用「maintenance 費率」代理。"
            "若刪除此提及，使用者無法知道簡化程度。"
        )
        # 驗證 notes 中提到代理/簡化的語意
        assert "代理" in FIDELITY_NOTES, (
            "FIDELITY_NOTES 應指明強平模型是對實盤的『代理』或『近似』。"
            "若改為絕對陳述，使用者會誤認為完全等同實盤。"
        )

    def test_notes_fee_claim_matches_actual_default(self):
        """成本模型的文件與實作必須同步

        notes 是文件、Config 是實作。兩者必須同步，否則「按 config 跑回測」
        就會測不同的成本模型。

        若有人改了 Config 預設費率卻沒改 notes，或反之，本測試轉紅。
        這條測試就是那個同步機制。
        """
        # 讀 Config 的預設費率
        cfg = Config(symbol="TESTUSDC")
        actual_fee_pct = cfg.fee_pct

        # 預設應為 maker 0.02% = 0.0002
        expected_fee_pct = 0.0002
        assert actual_fee_pct == expected_fee_pct, (
            f"Config 預設 fee_pct 改變了：預期 {expected_fee_pct}，實際 {actual_fee_pct}。"
            "必須同步更新 FIDELITY_NOTES 中的費率說明。"
        )

        # notes 中應有 "maker 0.02%" 的提及
        assert "maker 0.02%" in FIDELITY_NOTES, (
            "FIDELITY_NOTES 應明確說明預設費率為『maker 0.02%』。"
            "若改變預設費率，必須同步改 notes；若誤改為 taker，使用者會被系統性多罰費用。"
        )

        # 驗證 notes 中費率說明與實際 Config 一致
        # 0.0002 = 0.02%
        assert f"{actual_fee_pct:.4f}" in FIDELITY_NOTES or "0.02%" in FIDELITY_NOTES, (
            f"FIDELITY_NOTES 費率說明與 Config 預設值不一致。"
            f"notes 寫的『maker 0.02%』與實際 {actual_fee_pct} (={actual_fee_pct*100}%) 應一致。"
        )
