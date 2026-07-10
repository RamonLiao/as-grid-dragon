"""Task 5b: 哨兵值的嚴格性驗證

背景：GridOptimizer._run_single_backtest() 當 should_liquidate() 拋 ValueError 時，
回傳一個哨兵 dict，表示該組參數已淘汰。哨兵值需滿足：對該指標的正常排序邏輯，
淘汰組永遠排在最後、不會奪冠。

核心問題（實測發現）：
1. max_drawdown 真實可達 1.1726（強平後權益為負），哨兵值 1.0 不是上界
   → 改 float("inf")
2. final_equity 真實可達 -17.2579（強平滑價侵蝕），哨兵值 0.0 不是下界
   → 改 float("-inf")
3. sharpe_ratio 的 -1e6 同理是魔術數字，不是下界
   → 改 float("-inf")

此測試使用實測數據（控制端提供的必爆場景配置）驗證排序邏輯。
"""
import pandas as pd
import pytest

from backtest.config import Config
from backtest.optimizer import GridOptimizer


def _disaster_case_df():
    """必爆場景：真實回測會產生 max_drawdown=1.1726, final_equity=-17.2579 的災難組

    控制端實測數據引用。
    """
    prices = [100.0] + [100.0 * (0.99 ** i) for i in range(1, 400)]
    return pd.DataFrame({
        "open_time": pd.date_range("2024-01-01", periods=len(prices), freq="1min"),
        "open": prices,
        "high": prices,
        "low": prices,
        "close": prices,
        "volume": [100.0] * len(prices),
    })


def test_sentinel_max_drawdown_never_wins_ascending_true(monkeypatch):
    """max_drawdown 排序（ascending=True），淘汰組永遠排最後

    改動前會紅：哨兵值 1.0 被真實值 > 1.0 贏，淘汰組排首位
    改動後會綠：哨兵值 inf，淘汰組排最後
    """
    from backtest import optimizer as optimizer_module

    df = _disaster_case_df()

    # 創建一個會拋 ValueError 的 mock 和一個正常的回測
    original_run = optimizer_module.GridBacktester.run
    call_count = {"n": 0}

    def _alternating_run(self):
        call_count["n"] += 1
        # 只在前 2 次調用（grid search 主迴圈）中，第 2 次拋異常
        # 第 3 次及以後（最佳配置重新執行）照常回傳
        if call_count["n"] <= 2:
            if call_count["n"] == 2:
                raise ValueError("模擬上游防禦破洞")
            return original_run(self)
        else:
            return original_run(self)

    monkeypatch.setattr(optimizer_module.GridBacktester, "run", _alternating_run)

    opt = GridOptimizer(
        df,
        base_config=Config(
            symbol="BNBUSDC",
            initial_balance=100.0,
            initial_quantity=0.5,
            leverage=20,
            grid_spacing=0.005,
            take_profit_spacing=0.001,
            direction="long",
            terminal_ui_mode=True,
            fee_pct=0.0004,
            slippage_bps=0.001,
            funding_enabled=False,
            threshold_multiplier=1e9,
        ),
        param_ranges={
            "take_profit_spacing": [0.001, 0.002],
            "grid_spacing": [0.005],
            "leverage": [20],
        },
    )

    result = opt.run(metric="max_drawdown", ascending=True, n_jobs=1)

    # 關鍵斷言：第一位永不是淘汰組
    best_row = result.all_results.iloc[0]
    assert best_row["liquidated"] is not True, (
        "淘汰組（liquidated=True）排到第一位！max_drawdown 哨兵值不夠大"
    )


def test_sentinel_final_equity_never_wins_ascending_false(monkeypatch):
    """final_equity 排序（ascending=False），淘汰組永遠排最後

    改動前會紅：哨兵值 0.0 > 真實災難值 -17.26，淘汰組排首位
    改動後會綠：哨兵值 -inf，淘汰組排最後
    """
    from backtest import optimizer as optimizer_module

    df = _disaster_case_df()

    original_run = optimizer_module.GridBacktester.run
    call_count = {"n": 0}

    def _alternating_run(self):
        call_count["n"] += 1
        if call_count["n"] <= 2:
            if call_count["n"] == 2:
                raise ValueError("模擬上游防禦破洞")
            return original_run(self)
        else:
            return original_run(self)

    monkeypatch.setattr(optimizer_module.GridBacktester, "run", _alternating_run)

    opt = GridOptimizer(
        df,
        base_config=Config(
            symbol="BNBUSDC",
            initial_balance=100.0,
            initial_quantity=0.5,
            leverage=20,
            grid_spacing=0.005,
            take_profit_spacing=0.001,
            direction="long",
            terminal_ui_mode=True,
            fee_pct=0.0004,
            slippage_bps=0.001,
            funding_enabled=False,
            threshold_multiplier=1e9,
        ),
        param_ranges={
            "take_profit_spacing": [0.001, 0.002],
            "grid_spacing": [0.005],
            "leverage": [20],
        },
    )

    result = opt.run(metric="final_equity", ascending=False, n_jobs=1)

    best_row = result.all_results.iloc[0]
    assert best_row["liquidated"] is not True, (
        "淘汰組（liquidated=True）排到第一位！final_equity 哨兵值不夠小（負向）"
    )


def test_sentinel_sharpe_ratio_never_wins_ascending_false(monkeypatch):
    """sharpe_ratio 排序（ascending=False），淘汰組永遠排最後

    改動前會紅：哨兵值 -1e6 可能被真實負值贏，淘汰組排首位
    改動後會綠：哨兵值 -inf，淘汰組排最後
    """
    from backtest import optimizer as optimizer_module

    df = _disaster_case_df()

    original_run = optimizer_module.GridBacktester.run
    call_count = {"n": 0}

    def _alternating_run(self):
        call_count["n"] += 1
        if call_count["n"] <= 2:
            if call_count["n"] == 2:
                raise ValueError("模擬上游防禦破洞")
            return original_run(self)
        else:
            return original_run(self)

    monkeypatch.setattr(optimizer_module.GridBacktester, "run", _alternating_run)

    opt = GridOptimizer(
        df,
        base_config=Config(
            symbol="BNBUSDC",
            initial_balance=100.0,
            initial_quantity=0.5,
            leverage=20,
            grid_spacing=0.005,
            take_profit_spacing=0.001,
            direction="long",
            terminal_ui_mode=True,
            fee_pct=0.0004,
            slippage_bps=0.001,
            funding_enabled=False,
            threshold_multiplier=1e9,
        ),
        param_ranges={
            "take_profit_spacing": [0.001, 0.002],
            "grid_spacing": [0.005],
            "leverage": [20],
        },
    )

    result = opt.run(metric="sharpe_ratio", ascending=False, n_jobs=1)

    best_row = result.all_results.iloc[0]
    assert best_row["liquidated"] is not True, (
        "淘汰組（liquidated=True）排到第一位！sharpe_ratio 哨兵值不夠小"
    )


def test_sentinel_return_pct_correctly_ranked(monkeypatch):
    """return_pct 排序（ascending=False），淘汰組（-1.0）永遠排最後

    return_pct 下界是 -1.0，現有值正確。此測驗證。
    """
    from backtest import optimizer as optimizer_module

    df = _disaster_case_df()

    original_run = optimizer_module.GridBacktester.run
    call_count = {"n": 0}

    def _alternating_run(self):
        call_count["n"] += 1
        if call_count["n"] <= 2:
            if call_count["n"] == 2:
                raise ValueError("模擬上游防禦破洞")
            return original_run(self)
        else:
            return original_run(self)

    monkeypatch.setattr(optimizer_module.GridBacktester, "run", _alternating_run)

    opt = GridOptimizer(
        df,
        base_config=Config(
            symbol="BNBUSDC",
            initial_balance=100.0,
            initial_quantity=0.5,
            leverage=20,
            grid_spacing=0.005,
            take_profit_spacing=0.001,
            direction="long",
            terminal_ui_mode=True,
            fee_pct=0.0004,
            slippage_bps=0.001,
            funding_enabled=False,
            threshold_multiplier=1e9,
        ),
        param_ranges={
            "take_profit_spacing": [0.001, 0.002],
            "grid_spacing": [0.005],
            "leverage": [20],
        },
    )

    result = opt.run(metric="return_pct", ascending=False, n_jobs=1)

    best_row = result.all_results.iloc[0]
    assert best_row["liquidated"] is not True, (
        "淘汰組排到第一位"
    )


def test_sentinel_values_are_correct_in_exception_path():
    """直接測試 _run_single_backtest 的異常路徑回傳值

    驗證改動後的哨兵值是 inf/-inf。
    """
    from backtest import optimizer as optimizer_module

    def _raise(self):
        raise ValueError("test")

    opt = GridOptimizer(
        _disaster_case_df(),
        base_config=Config(symbol="BNBUSDC", initial_balance=100.0, leverage=10),
    )

    original_run = optimizer_module.GridBacktester.run
    try:
        optimizer_module.GridBacktester.run = _raise
        result = opt._run_single_backtest({
            "take_profit_spacing": 0.001,
            "grid_spacing": 0.002,
            "leverage": 10,
        })

        # 驗證改動後的哨兵值
        assert result["max_drawdown"] == float("inf"), (
            "max_drawdown 哨兵應改成 inf"
        )
        assert result["final_equity"] == float("-inf"), (
            "final_equity 哨兵應改成 -inf"
        )
        assert result["sharpe_ratio"] == float("-inf"), (
            "sharpe_ratio 哨兵應改成 -inf"
        )
        # 檢查其他哨兵值
        assert result["return_pct"] == -1.0
        assert result["trades"] == 0
        assert result["win_rate"] == 0.0
        assert result["profit_factor"] == 0.0
    finally:
        optimizer_module.GridBacktester.run = original_run
