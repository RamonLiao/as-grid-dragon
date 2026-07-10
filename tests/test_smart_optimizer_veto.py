"""Task 9b：SmartOptimizer（Optuna 路徑）補上 spec §7 一票否決。

背景：spec §7 規定 BacktestResult.liquidated == True 的參數組是一票否決，
不進優化目標函數 —— 沒有強平建模時「無限加倉 + 不止損」在算術上是必勝策略，
優化器一定會選它；加了強平建模後，若優化器不尊重這個旗標，等於白加。

這條不變式已在三個現場被磨損過（規格裡的強動詞「否決」系統性退化成弱動詞
「扣分/排序/提醒」，且測試抓不到，因為觸發條件罕見）：
- backtest/optimizer.py（grid search）已修：显式 eligible 過濾。
- scripts/cost_sensitivity.py 已修：原本只印一行文字，實際仍把強平組算進 best/gap。
- backtest/smart_optimizer.py（Optuna）：本檔要證明的就是這裡的修復。

若本檔任何一條測試轉紅，代表 spec §7 的一票否決在 Optuna 路徑上失效，
優化器可能選出「爆倉但帳面分數很高」的參數組合，等於白加了強平建模。

用 optuna.TrialPruned 而非「回傳最差分數」：TrialPruned 讓 trial 狀態變成
PRUNED，完全不進 best_trial / best_params / best_trials 候選集，也不出現在
study.trials_dataframe() 的正常統計裡。回傳 -1e6 或 (1e6,1e6,1e6) 只是讓分
數難看，trial 仍是 COMPLETE，一樣會污染後續統計 —— 這正是前三個現場被修掉
的磨損形式，不該在這裡重蹈。

注意：TrialPruned 繼承自 Exception。若不特別攔截，原本包住 `_run_backtest`
呼叫的 `except Exception as e: return <最差分數>` 會把它吞掉，讓一票否決
形同虛設又難以被發現（因為 log 訊息看起來像「trial 失敗」，容易被忽略）。
"""
import logging

import pytest
import optuna
from optuna.trial import TrialState

from backtest.config import Config
from backtest.backtester import BacktestResult
from backtest.smart_optimizer import SmartOptimizer, OptimizationObjective


def _tiny_df():
    import pandas as pd
    prices = [100.0, 100.1, 99.9, 100.2, 99.8]
    return pd.DataFrame({
        "open_time": pd.date_range("2024-01-01", periods=len(prices), freq="1min"),
        "open": prices, "high": prices, "low": prices, "close": prices,
        "volume": [100.0] * len(prices),
    })


def _optimizer(logger=None):
    return SmartOptimizer(
        _tiny_df(),
        base_config=Config(symbol="BNBUSDC", initial_balance=100.0, leverage=10,
                            direction="long"),
        logger=logger or logging.getLogger("test_smart_optimizer_veto"),
    )


def _fake_result(liquidated: bool, return_pct: float = 0.05, sharpe: float = 1.0) -> BacktestResult:
    """建一個不需要跑真實 GridBacktester 的假回測結果。"""
    return BacktestResult(
        final_equity=100.0 * (1 + return_pct),
        return_pct=return_pct,
        max_drawdown=0.1,
        realized_pnl=0.0,
        unrealized_pnl=0.0,
        total_pnl=0.0,
        trades_count=5,
        win_rate=0.6,
        profit_factor=1.5,
        sharpe_ratio=sharpe,
        direction="long",
        config=Config(),
        liquidated=liquidated,
    )


def _ask_trial(opt: SmartOptimizer) -> optuna.Trial:
    """拿一個真正的 optuna.Trial（不是 mock），確保 trial.suggest_float 等
    行為與正式跑 optimize() 時一致。"""
    study = optuna.create_study(direction="maximize",
                                 sampler=optuna.samplers.TPESampler(seed=0))
    return study.ask()


class TestLiquidatedTrialIsPruned:
    """核心行為：liquidated=True 的 trial 必須被剪除，不能被打分。

    若這條紅了，代表 spec §7 的一票否決在 Optuna 路徑上失效：優化器會把
    爆倉組合當成一個「分數很高」的正常 trial 納入評分與統計。
    """

    def test_liquidated_trial_is_pruned_not_scored(self, monkeypatch):
        """守 spec §7 一票否決在 _optuna_objective 層：呼叫端收到 TrialPruned，
        不是虛偽的「超低分」。

        若本測試失紅，代表 liquidated=True 的 trial 沒有被正確剪除，
        而是被某個捕捉所有 Exception 的 handler 吞掉後回傳虛偽的最差分數。
        這樣優化器會把爆倉組合當成「完成但失敗」的試驗，仍能進入統計計算。

        用 TrialPruned 而非「回傳 -1e6」：TrialPruned 讓 trial 狀態變成
        PRUNED，完全不進 best_trial / best_params 候選集；回傳 -1e6 只是讓
        分數難看，trial 仍是 COMPLETE，污染後續統計。TrialPruned 繼承自
        Exception，若不特別在既有 `except Exception` 之前攔截，會被吞掉，
        一票否決又一次退化成虛偽的扣分（且難以被發現，因為 log 訊息看起來
        像普通 trial 失敗）。
        """
        opt = _optimizer()

        # 刻意讓 liquidated=True 的假結果帶著「看起來很棒」的高分，模擬
        # martingale 恆等式（無限加倉不止損，帳面上收益率極高）。若一票
        # 否決沒生效，這組會被目標函數當成最佳解。
        fake = _fake_result(liquidated=True, return_pct=9.99, sharpe=99.0)
        monkeypatch.setattr(SmartOptimizer, "_run_backtest", lambda self, params: fake)

        trial = _ask_trial(opt)

        with pytest.raises(optuna.TrialPruned):
            opt._optuna_objective(trial, OptimizationObjective.SHARPE)


class TestPrunedTrialNeverBecomesBest:
    """整合行為：跑一個小 study，會強平的參數組合絕不能贏得 best_params，
    且 study 裡必須留下 PRUNED 狀態的證據（否則剪除只是「看起來剪了」）。
    """

    def test_pruned_trial_never_becomes_best_params(self, monkeypatch):
        """整合驗證：跑一個完整的 Optuna study，被剪除的 trial 絕不能贏得
        best_params，且 study 中必須有 PRUNED 狀態的試驗（證明剪除發生過，
        而不是被悄悄吞掉了）。

        若本測試失紅，代表優化器選出了「回測期間爆倉但帳面分數很高」的
        參數組合當最佳解，等於白加了強平建模。沒有強平時，無限加倉 + 不
        止損在算術上是必勝策略（martingale 恆等式），加了建模後優化器仍
        選爆倉組，代表 spec §7 的一票否決機制失效。

        測試特意給強平的參數組超高分數，逼優化器「如果沒實施剪除就一定
        會選中它」。同時檢查 study.trials 中確實有 PRUNED 狀態的紀錄，
        確保剪除不只是「程式碼裡寫了」而是「真的發生過」。
        """
        opt = _optimizer()

        call_count = {"n": 0}

        def fake_run_backtest(self, params):
            call_count["n"] += 1
            # 用參數本身決定是否強平：take_profit_spacing 較大的那批視為
            # 「爆倉」，同時給它離譜的高 sharpe，逼優化器如果沒剪除就一定
            # 會選中它。
            if params["take_profit_spacing"] > 0.008:
                return _fake_result(liquidated=True, return_pct=9.99, sharpe=99.0)
            return _fake_result(liquidated=False, return_pct=0.02, sharpe=0.5)

        monkeypatch.setattr(SmartOptimizer, "_run_backtest", fake_run_backtest)

        study = optuna.create_study(direction="maximize",
                                     sampler=optuna.samplers.TPESampler(seed=42))
        study.optimize(
            lambda trial: opt._optuna_objective(trial, OptimizationObjective.SHARPE),
            n_trials=15,
        )

        assert call_count["n"] > 0
        pruned = [t for t in study.trials if t.state == TrialState.PRUNED]
        completed = [t for t in study.trials if t.state == TrialState.COMPLETE]

        assert len(completed) > 0, "至少要有正常完成的 trial 供 best_params 使用"
        # best_params 必須來自沒有強平的參數區（take_profit_spacing <= 0.008）
        assert study.best_params["take_profit_spacing"] <= 0.008

        # 若母體參數空間裡有落入強平區的採樣，必須看到它被剪除，而不是
        # 悄悄被 except Exception 吞掉、回傳低分後仍記為 COMPLETE。
        any_liquidated_sampled = any(
            t.params.get("take_profit_spacing", 0) > 0.008
            for t in study.trials
        )
        if any_liquidated_sampled:
            assert len(pruned) > 0


class TestTrialPrunedNotSwallowedByExceptExcept:
    """機制驗證：TrialPruned 繼承自 Exception，若 `_optuna_objective` 裡的
    `except Exception` 沒有特別排除它，會把 PRUNED 誤判成「trial 失敗」，
    回傳 -1e6 並記為 COMPLETE —— 一票否決形同虛設，且 log 訊息容易被誤讀
    成普通的 trial 失敗，難以被發現。
    """

    def test_trial_pruned_propagates_through_optuna_objective(self, monkeypatch):
        """機制驗證：TrialPruned 不能被既有的 `except Exception` 吞掉。

        背景：TrialPruned 繼承自 Exception。若 _optuna_objective 內包住
        _run_backtest 的異常捕捉沒有特別排除 TrialPruned，會把它當成
        「trial 失敗」處理（log 警告 + 回傳 -1e6），trial 狀態改為 COMPLETE
        而非 PRUNED。這樣一票否決形同虛設，且由於 log 訊息看起來像普通
        失敗，難以被發現。

        若本測試失紅，代表 TrialPruned 被吞掉了。結果雖然仍是「最終 best
        沒選爆倉組」（因為 -1e6 分數太低），但機制完全破裂 ——
        study.trials_dataframe() 會記錄虛偽的失敗，多目標優化的 best_trials
        會被污染，後續統計無法區分「正確的剪除」和「掩蓋的故障」。

        此測試直接證明 _optuna_objective 的呼叫端收到的是真的 TrialPruned
        異常，而非被吞掉後的虛偽返回值。
        """
        opt = _optimizer()
        fake = _fake_result(liquidated=True, return_pct=9.99, sharpe=99.0)
        monkeypatch.setattr(SmartOptimizer, "_run_backtest", lambda self, params: fake)

        trial = _ask_trial(opt)

        # 直接證明呼叫端收到的是 TrialPruned，而不是被吞掉後的 -1e6 回傳值。
        with pytest.raises(optuna.TrialPruned):
            opt._optuna_objective(trial, OptimizationObjective.SHARPE)

    def test_non_trial_pruned_exception_still_returns_worst_score(self, monkeypatch):
        """對照：非 TrialPruned 的例外仍走既有設計 —— log 警告 + 回傳最差
        分數，本任務不改動這段既有行為。"""
        opt = _optimizer()

        def boom(self, params):
            raise RuntimeError("模擬非強平相關的隨機錯誤")

        monkeypatch.setattr(SmartOptimizer, "_run_backtest", boom)

        trial = _ask_trial(opt)
        value = opt._optuna_objective(trial, OptimizationObjective.SHARPE)
        assert value == -1e6


class TestNonLiquidatedTrialStillScoredNormally:
    """負向對照：liquidated=False 的正常 trial 不能被誤傷，仍要照常打分並
    回傳有限數值，否則代表這次修復把否決範圍改得太寬。
    """

    def test_non_liquidated_trial_still_scored_normally(self, monkeypatch):
        """負向對照：liquidated=False 的正常 trial 不能被誤傷。

        spec §7 的一票否決只針對 liquidated=True 的情況。若實作不當把範圍
        改得太寬（例如對所有 trial 丟 TrialPruned），會誤傷正常的回測結果，
        導致優化器無法訓練。本測試驗證修復沒有超度打擊 —— 合格組合仍要
        照常計分並回傳有限的數值。

        若本測試失紅，代表一票否決的實作邏輯有誤，正常 trial 被錯誤地
        剪除或給了虛偽的分數，優化器會失去合格候選，無法進行優化。
        """
        opt = _optimizer()
        fake = _fake_result(liquidated=False, return_pct=0.02, sharpe=0.75)
        monkeypatch.setattr(SmartOptimizer, "_run_backtest", lambda self, params: fake)

        trial = _ask_trial(opt)
        value = opt._optuna_objective(trial, OptimizationObjective.SHARPE)

        assert value == pytest.approx(0.75)


class TestOptimizeSurvivesPositionIndexVsTrialNumberMismatch:
    """dual-review R3 Critical：self._trials 的位置索引 != optuna 的
    trial.number，一旦有 trial 被剪除就會炸掉或悄悄配錯資料。

    背景：本 branch 把強平從「return -1e6（COMPLETE，會 append 進
    self._trials，位置索引與 trial.number 對齊）」改成「raise
    TrialPruned（PRUNED，完全不 append 進 self._trials）」。這修好了
    spec §7 一票否決，但同時打破了一個從沒人寫下來的不變式：
    `self._trials[i].trial_number == i`。這個不變式在 prune 幾乎不會
    發生時剛好成立；現在 prune 是常態路徑（任何強平的 trial 都會被
    剪除），於是 `optimize()` 裡 `self._trials[study.best_trial.number]`
    這行位置索引直接錯位。

    optuna 的 trial.number 是「含 PRUNED trial」的全域序號；
    self._trials 只含 COMPLETE trial 且照 append 順序排列。只要曾經有
    任何 trial 被剪除，這兩者就不再相等。
    """

    def test_optimize_survives_pruned_trials_and_reports_the_winners_own_metrics(
        self, monkeypatch
    ):
        """整合驗證：跑真正的 optimize()（不是手工建 study），前幾個 trial
        強平被剪除，最佳解落在最後一個 trial —— 修復前這裡應該直接
        IndexError，因為 study.best_trial.number（全域序號，含 PRUNED）
        會超出 self._trials（只含 COMPLETE）的長度。

        若本測試失紅（IndexError），代表 optimize() 的 best_metrics 抓取
        邏輯又走回了「用位置索引 self._trials[trial.number]」這個在
        prune 常態化後不成立的假設，會直接把 run_smart_optimization 這個
        對外服務層 API 整個炸掉。
        """
        opt = _optimizer()

        call_count = {"n": 0}

        def fake_run_backtest(self, params):
            call_count["n"] += 1
            n = call_count["n"]
            if n <= 3:
                # 前 3 個 trial（trial_number 0,1,2）強平，全部被剪除，
                # 不會進 self._trials。
                return _fake_result(liquidated=True, return_pct=9.99, sharpe=99.0)
            # 之後遞增的 sharpe，最佳解落在最後一個 trial
            # （trial_number = n - 1 = 9，是全域序號裡的最大值）。
            return _fake_result(
                liquidated=False, return_pct=0.01 * n, sharpe=0.1 * n
            )

        monkeypatch.setattr(SmartOptimizer, "_run_backtest", fake_run_backtest)

        result = opt.optimize(
            n_trials=10,
            objective=OptimizationObjective.SHARPE,
            n_startup_trials=10,
            show_progress=False,
        )

        # 修復前：study.best_trial.number=9（全域序號），
        # len(self._trials)=7（只有 trial 3..9 這 7 個 COMPLETE），
        # self._trials[9] -> IndexError，整個 optimize() 炸掉。
        assert result.best_metrics != {}
        # best_metrics 必須真的屬於贏家那個 trial（sharpe=0.1*10=1.0），
        # 不是位置索引錯位後配到的別的 trial。
        assert result.best_metrics["sharpe_ratio"] == pytest.approx(1.0)

    def test_optimize_reports_correct_metrics_when_prune_precedes_winner(
        self, monkeypatch
    ):
        """Failure B（靜默錯配）：prune 發生在最佳 trial 之前，且
        best_trial.number 仍小於 len(self._trials)，所以位置索引不會
        IndexError —— 但會悄悄配到另一個 trial 的 metrics。

        這個失敗模式不會讓程式崩潰，使用者會看到「看起來正常」的優化
        結果，但 best_metrics（Sharpe / return / drawdown）其實屬於
        另一組參數。而這個 optimizer 的輸出會被用來選實盤參數 —— 錯配
        的後果是使用者依據錯的績效數字上線一組參數。

        若本測試失紅，代表 best_metrics 的內容跟 best_params 對不上：
        贏家的 sharpe 應該是全場最高（99.0），若拿到的是別的數字，代表
        位置索引又配錯了 trial。
        """
        opt = _optimizer()

        call_count = {"n": 0}

        def fake_run_backtest(self, params):
            call_count["n"] += 1
            n = call_count["n"]
            if n <= 2:
                # trial_number 0,1 強平被剪除。
                return _fake_result(liquidated=True, return_pct=9.99, sharpe=99.0)
            if n == 3:
                # trial_number=2：全場最佳解，sharpe 遠高於其餘所有 trial。
                return _fake_result(liquidated=False, return_pct=0.5, sharpe=99.0)
            # trial_number 3..9：sharpe 遞增但遠低於贏家，避免蓋過它。
            return _fake_result(
                liquidated=False, return_pct=0.01 * n, sharpe=0.1 * (n - 3)
            )

        monkeypatch.setattr(SmartOptimizer, "_run_backtest", fake_run_backtest)

        result = opt.optimize(
            n_trials=10,
            objective=OptimizationObjective.SHARPE,
            n_startup_trials=10,
            show_progress=False,
        )

        # self._trials 的 append 順序：[trial2(sharpe=99), trial3(0.1),
        # trial4(0.2), ..., trial9(0.7)]，共 8 筆。best_trial.number=2，
        # 2 < len(self._trials)=8，位置索引 self._trials[2] 不會
        # IndexError，但拿到的其實是 trial_number=4（sharpe=0.2），
        # 不是贏家 trial_number=2（sharpe=99.0）—— 這就是靜默錯配。
        assert result.best_metrics["sharpe_ratio"] == pytest.approx(99.0)


class TestMultiObjectivePathDoesNotUsePositionalTrialIndex:
    """驗證多目標路徑（_multi_objective / pareto_front）沒有同樣的位置
    索引假設。

    `grep -n "_trials\\[" backtest/smart_optimizer.py` 確認全檔唯一一處
    對 self._trials 做位置索引的地方就是本次修掉的那一行（單目標路徑）。
    _multi_objective 收集 best_trials 的方式完全不同：它直接從
    `study.best_trials` 拿 `trial.params` / `trial.values`（見
    `optimize()` 裡 objective == MULTI_OBJECTIVE 分支，約 line 656-672），
    從未用 `self._trials[trial.number]` 這種位置索引去查 metrics，所以
    不受本次修的 bug 影響 —— 不需要額外的整合測試來證明「沒有這個 bug」。

    注意（範圍外發現，未在本次修復）：多目標路徑本身有一個*不同*且更早
    存在的 bug，與本次 Critical 無關：`optimize()` line 743
    `study.best_value if hasattr(study, 'best_value') else self._best_value`
    —— Optuna 的 `Study.best_value` 是一個 property，`hasattr()` 恆真，
    但在多目標 study 上存取它會拋 `RuntimeError`（不是 `AttributeError`），
    所以這個 hasattr 防呆完全沒用。這會讓任何呼叫
    `optimize(objective=MULTI_OBJECTIVE)` 的路徑，只要曾經有一個 trial
    被剪除（讓 study 裡同時存在 PRUNED 與 COMPLETE），就在建構
    SmartOptimizationResult 時整個炸掉。這個 bug 不在本次白名單
    （backtest/smart_optimizer.py 的其餘正確性）授權範圍內的「同一個
    position-index 假設」，是獨立問題，留給後續任務處理，此處僅記錄。
    """

    def test_no_positional_trial_index_outside_the_fixed_line(self):
        """結構驗證：全檔只有一處位置索引寫法，且已修復。"""
        import re
        from pathlib import Path

        src = Path("backtest/smart_optimizer.py").read_text(encoding="utf-8")
        matches = [
            line for line in src.splitlines() if re.search(r"_trials\[", line)
        ]
        # 唯一應該存在的是說明性註解裡提到 self._trials[study.best_trial.number]
        # 的那一行文字，不是真正的可執行索引語句。
        assert len(matches) == 1
        assert "不可用位置索引" in matches[0]
