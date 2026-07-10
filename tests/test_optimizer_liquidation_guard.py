"""Task 5b：批次優化（GridOptimizer）遇 should_liquidate() 的 ValueError 防線時，
淘汰該組參數繼續跑，而不是炸掉整個 grid search / Optuna study。

背景：backtest/liquidation.py::should_liquidate() 對無效輸入（price<=0、非有限
price/equity、負的 long_pos/short_pos/maintenance_margin_rate）raise ValueError，
不再靜默回傳 False（見 tests/test_backtest_liquidation.py）。正常路徑不會觸發
（backtester.py 主迴圈已擋掉髒 K 線），但若上游防禦有洞，這個 raise 一路往上
炸，炸掉的是整批優化，不是這一組壞參數。

修法：GridOptimizer._run_single_backtest() 是 optimizer.py 五個 bt.run() 呼叫點
的唯一共同瓶頸（generate_param_combinations → run() 的單線程/多進程分支都經過
它），只在這裡 catch ValueError，記錄完整參數與例外訊息，回傳一個績效指標必然
排最後、且 liquidated=True 的字典，讓該組被自然淘汰、其餘組合繼續跑。
"""
import pandas as pd
import pytest

from backtest.config import Config
from backtest.optimizer import GridOptimizer


def _tiny_df():
    prices = [100.0, 100.1, 99.9, 100.2, 99.8]
    return pd.DataFrame({
        "open_time": pd.date_range("2024-01-01", periods=len(prices), freq="1min"),
        "open": prices, "high": prices, "low": prices, "close": prices,
        "volume": [100.0] * len(prices),
    })


def _optimizer():
    return GridOptimizer(_tiny_df(), base_config=Config(
        symbol="BNBUSDC", initial_balance=100.0, leverage=10,
        grid_spacing=0.002, take_profit_spacing=0.001,
        direction="long", terminal_ui_mode=True,
    ))


def test_value_error_from_run_is_caught_and_param_set_eliminated(monkeypatch):
    """核心行為：should_liquidate() 的 ValueError 防線觸發時，
    _run_single_backtest 不得往上炸 —— 必須淘汰該組並回傳明確排最後的結果。

    （本測試在補丁前会失敗：修補前 bt.run() 內的 ValueError 會原樣往上拋出，
    monkeypatch.setattr(GridBacktester, "run", ...) 直接讓 run() raise，
    驗證呼叫點是否吞下它。）
    """
    from backtest import optimizer as optimizer_module

    def _raise(self):
        raise ValueError("price 必須是有限正值，收到 0.0")

    monkeypatch.setattr(optimizer_module.GridBacktester, "run", _raise)

    opt = _optimizer()
    result = opt._run_single_backtest({"take_profit_spacing": 0.001, "grid_spacing": 0.002})

    assert result["liquidated"] is True
    assert result["return_pct"] == -1.0
    assert result["final_equity"] == float("-inf")
    # 目標函數常見排序方向（return_pct/sharpe/profit_factor 越大越好）下必排最後
    assert result["sharpe_ratio"] == float("-inf")
    assert result["profit_factor"] == 0.0


def test_non_value_error_still_propagates(monkeypatch):
    """只 catch ValueError：其他例外類型（如 RuntimeError）必須照常往上炸，
    不能被過度捕捉吞掉，否則真正的程式錯誤會被靜默藏起來。"""
    from backtest import optimizer as optimizer_module

    def _raise(self):
        raise RuntimeError("非預期的程式錯誤，不該被吞")

    monkeypatch.setattr(optimizer_module.GridBacktester, "run", _raise)

    opt = _optimizer()
    with pytest.raises(RuntimeError):
        opt._run_single_backtest({"take_profit_spacing": 0.001, "grid_spacing": 0.002})


def test_normal_backtest_still_reports_liquidated_flag():
    """正常路徑（無 raise）：liquidated 欄位應直接反映 BacktestResult.liquidated，
    確保新增的 catch 分支沒有動到成功路徑的回傳結構。"""
    opt = _optimizer()
    result = opt._run_single_backtest({"take_profit_spacing": 0.001, "grid_spacing": 0.002})
    assert "liquidated" in result
    assert result["liquidated"] is False


def test_grid_search_run_survives_one_bad_combo_among_many(monkeypatch):
    """整合：run() 跑一批組合，其中一組觸發 ValueError，其餘組合仍完成，
    且整個 optimize 呼叫不炸掉。"""
    from backtest import optimizer as optimizer_module

    original_run = optimizer_module.GridBacktester.run
    call_count = {"n": 0}

    def _sometimes_raise(self):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise ValueError("模擬上游防禦破洞：price 非有限值")
        return original_run(self)

    monkeypatch.setattr(optimizer_module.GridBacktester, "run", _sometimes_raise)

    opt = GridOptimizer(
        _tiny_df(),
        base_config=Config(symbol="BNBUSDC", initial_balance=100.0, leverage=10,
                           direction="long", terminal_ui_mode=True),
        param_ranges={
            "take_profit_spacing": [0.001, 0.002, 0.003],
            "grid_spacing": [0.005],
            "leverage": [10],
        },
    )

    result = opt.run(metric="return_pct", ascending=False, n_jobs=1)

    assert call_count["n"] >= 2
    assert len(result.all_results) == 3
    assert result.all_results["liquidated"].sum() == 1


# ── mutation coverage 補強（review Minor）：+inf 的 raise 測試 ─────────────
# 既有測試只測了 long_pos/short_pos/maintenance_margin_rate 的負值、
# price 的 0/負值/nan，缺 +inf。若日後有人誤把 isfinite 檢查降級成只剩
# `>= 0`，這些 mutation 不會被上面既有測試抓到，但會被下面這四條抓到。

from backtest.liquidation import should_liquidate  # noqa: E402


def test_price_positive_infinity_raises():
    with pytest.raises(ValueError):
        should_liquidate(equity=4.0, long_pos=10.0, short_pos=0.0,
                         price=float("inf"), maintenance_margin_rate=0.005)


def test_long_pos_infinity_raises():
    with pytest.raises(ValueError):
        should_liquidate(equity=4.0, long_pos=float("inf"), short_pos=0.0,
                         price=100.0, maintenance_margin_rate=0.005)


def test_short_pos_infinity_raises():
    with pytest.raises(ValueError):
        should_liquidate(equity=4.0, long_pos=0.0, short_pos=float("inf"),
                         price=100.0, maintenance_margin_rate=0.005)


def test_maintenance_margin_rate_infinity_raises():
    with pytest.raises(ValueError):
        should_liquidate(equity=4.0, long_pos=10.0, short_pos=0.0,
                         price=100.0, maintenance_margin_rate=float("inf"))
