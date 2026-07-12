"""Task 5b: param_importance 必須排除淘汰列

背景：_calculate_param_importance() 對全部列做 groupby().mean()，
當其中有淘汰列（liquidated=True）時，其指標值會是 inf/-inf 的哨兵值，
導致整個 group 的平均變成 inf/nan，污染 param_importance。

修法：param_importance 的計算必須基於 eligible 子集（liquidated != True）。
"""
import math
import pandas as pd
import pytest

from backtest.config import Config
from backtest.optimizer import GridOptimizer


def _disaster_case_df():
    """必爆場景"""
    prices = [100.0] + [100.0 * (0.99 ** i) for i in range(1, 400)]
    return pd.DataFrame({
        "open_time": pd.date_range("2024-01-01", periods=len(prices), freq="1min"),
        "open": prices,
        "high": prices,
        "low": prices,
        "close": prices,
        "volume": [100.0] * len(prices),
    })


class _OptimizerWithSafeEligibleCombo(GridOptimizer):
    """某個 leverage 值設為 initial_quantity=0.0，
    讓它在必爆 df 上不觸發強平（虧損但合格）。"""

    def _create_config(self, params):
        config = super()._create_config(params)
        # leverage=20 這組用保守初始倉位，確保合格
        if params.get("leverage") == 20:
            config.initial_quantity = 0.0
        return config


def test_param_importance_excludes_liquidated_rows():
    """param_importance 的每個值都必須是有限數（不能是 inf/nan）

    場景：三個 leverage 值，其中 leverage=15 被 ValueError 淘汰，
          leverage=20 是合格的保守組，leverage=10 會真實強平。
    驗證：param_importance 中所有值都是有限數，未被淘汰列污染。
    """
    df = _disaster_case_df()

    # 建立優化器，設定會讓某個參數值組被淘汰
    from backtest import optimizer as optimizer_module

    original_run = optimizer_module.GridBacktester.run

    def _dispatch_run(self):
        # 讓 leverage=15 這組拋 ValueError 淘汰
        if self.config.leverage == 15:
            raise ValueError("模擬上游防禦破洞")
        return original_run(self)

    try:
        optimizer_module.GridBacktester.run = _dispatch_run

        opt = _OptimizerWithSafeEligibleCombo(
            df,
            base_config=Config(
                symbol="BNBUSDC",
                initial_balance=100.0,
                initial_quantity=0.5,
                leverage=10,
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
                "leverage": [10, 15, 20],  # 15 被 ValueError 淘汰，20 合格，10 強平
            },
        )

        result = opt.run(metric="return_pct", ascending=False, n_jobs=1)

        # 核心驗證：param_importance 中沒有 inf/nan 值，說明淘汰列未污染聚合
        for param_name, importance_value in result.param_importance.items():
            assert math.isfinite(importance_value), (
                f"param_importance[{param_name}] = {importance_value} 不是有限數，"
                f"說明淘汰列污染了聚合計算"
            )
            assert importance_value >= 0.0, (
                f"param_importance[{param_name}] = {importance_value} 為負數"
            )

    finally:
        optimizer_module.GridBacktester.run = original_run
