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
        opt = _optimizer()
        fake = _fake_result(liquidated=False, return_pct=0.02, sharpe=0.75)
        monkeypatch.setattr(SmartOptimizer, "_run_backtest", lambda self, params: fake)

        trial = _ask_trial(opt)
        value = opt._optuna_objective(trial, OptimizationObjective.SHARPE)

        assert value == pytest.approx(0.75)
