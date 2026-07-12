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

NOTE (Task 5b review R3)：以下 4 條「never_wins」測試原本只用 2 組參數
（1 組真實災難 + 1 組 ValueError 淘汰），實測發現這個 df 在給定 base_config
下極端到「連 take_profit_spacing 較保守的那組也真的觸發強平」——也就是說
這兩組原本就沒有任何合格的贏家，old 排序法之所以測出「贏家不是淘汰組」，
純屬巧合（碰巧真實災難組的哨兵/實測值在該次排序沒有排到第一位），不是
邏輯保證。R3 改為 liquidated 旗標 + run() 選最佳前過濾後，「全部淘汰」的
正確行為是拋 RuntimeError（見 test_optimizer_disqualification_veto.py），
而不是勉強選一個其實也是強平的「贏家」。

因此以下 4 條測試改為 3 組參數：1 組真實災難（強平）、1 組 ValueError 淘汰、
1 組真實但保守（initial_quantity=0.0，不觸發強平、雖然虧損）的合格組，
用來驗證合格組永遠會被選中，淘汰/強平組永遠不會。
"""
import pandas as pd
import pytest

from backtest.config import Config
from backtest.optimizer import GridOptimizer


class _OptimizerWithSafeThirdCombo(GridOptimizer):
    """take_profit_spacing=0.003 這組改用 initial_quantity=0.0，
    讓它在同一條必爆 df 上不觸發強平（真實虧損但合格），
    作為淘汰組之外唯一的合格候選。"""

    def _create_config(self, params):
        config = super()._create_config(params)
        if params.get("take_profit_spacing") == 0.003:
            config.initial_quantity = 0.0
        return config


def _three_combo_optimizer(df, monkeypatch):
    """建立一個含「真實災難組（0.001）+ ValueError 淘汰組（0.002）+
    合格保守組（0.003）」的 optimizer，並用 config 內容（而非呼叫次數）
    決定要不要模擬 ValueError —— 與 run() 內部迭代順序無關，穩定可靠。"""
    from backtest import optimizer as optimizer_module

    original_run = optimizer_module.GridBacktester.run

    def _dispatch_run(self):
        if self.config.take_profit_spacing == 0.002:
            raise ValueError("模擬上游防禦破洞")
        return original_run(self)

    monkeypatch.setattr(optimizer_module.GridBacktester, "run", _dispatch_run)

    return _OptimizerWithSafeThirdCombo(
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
            "take_profit_spacing": [0.001, 0.002, 0.003],
            "grid_spacing": [0.005],
            "leverage": [20],
        },
    )


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
    """max_drawdown 排序（ascending=True），淘汰/強平組永遠不被選為最佳

    R3：淘汰不再靠哨兵值排序，而是 liquidated 旗標一票否決。
    """
    df = _disaster_case_df()
    opt = _three_combo_optimizer(df, monkeypatch)

    result = opt.run(metric="max_drawdown", ascending=True, n_jobs=1)

    assert result.best_result.liquidated is False, (
        "最佳解不得是強平/淘汰組"
    )
    assert result.best_config.take_profit_spacing == 0.003


def test_sentinel_final_equity_never_wins_ascending_false(monkeypatch):
    """final_equity 排序（ascending=False），淘汰/強平組永遠不被選為最佳

    R3：淘汰不再靠哨兵值排序，而是 liquidated 旗標一票否決。
    """
    df = _disaster_case_df()
    opt = _three_combo_optimizer(df, monkeypatch)

    result = opt.run(metric="final_equity", ascending=False, n_jobs=1)

    assert result.best_result.liquidated is False, (
        "最佳解不得是強平/淘汰組"
    )
    assert result.best_config.take_profit_spacing == 0.003


def test_sentinel_sharpe_ratio_never_wins_ascending_false(monkeypatch):
    """sharpe_ratio 排序（ascending=False），淘汰/強平組永遠不被選為最佳

    R3：淘汰不再靠哨兵值排序，而是 liquidated 旗標一票否決。
    """
    df = _disaster_case_df()
    opt = _three_combo_optimizer(df, monkeypatch)

    result = opt.run(metric="sharpe_ratio", ascending=False, n_jobs=1)

    assert result.best_result.liquidated is False, (
        "最佳解不得是強平/淘汰組"
    )
    assert result.best_config.take_profit_spacing == 0.003


def test_sentinel_return_pct_correctly_ranked(monkeypatch):
    """return_pct 排序（ascending=False），淘汰/強平組永遠不被選為最佳

    NOTE (Task 5b review R3)：原本認定 return_pct 下界是 -1.0（虧光本金），
    但控制端實測災難組可達 -1.0176（強平滑價侵蝕本金以外部位），-1.0 不是
    真下界。哨兵值改成 -inf；且淘汰機制已改為 liquidated 旗標 + run()
    選最佳前過濾，不再單靠排序表達淘汰。
    """
    df = _disaster_case_df()
    opt = _three_combo_optimizer(df, monkeypatch)

    result = opt.run(metric="return_pct", ascending=False, n_jobs=1)

    assert result.best_result.liquidated is False, (
        "最佳解不得是強平/淘汰組"
    )
    assert result.best_config.take_profit_spacing == 0.003


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
        assert result["return_pct"] == float("-inf"), (
            "return_pct 哨兵應改成 -inf（-1.0 不是真下界，實測可達 -1.0176）"
        )
        assert result["trades"] == 0
        assert result["win_rate"] == 0.0
        assert result["profit_factor"] == 0.0
    finally:
        optimizer_module.GridBacktester.run = original_run
