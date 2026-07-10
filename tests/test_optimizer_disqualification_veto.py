"""Task 5b review R3：liquidated 一票否決取代哨兵值排序。

spec §7 早已明訂 liquidated 是「一票否決，不進優化目標」（見
backtest/backtester.py::BacktestResult.liquidated 欄位註解）。但前兩輪的實作
用「哨兵值排最後」表達淘汰，而不是真正過濾 —— 連錯三次：max_drawdown 1.0
vs 實測 1.1726、final_equity 0.0 vs -17.2579、return_pct -1.0 vs -1.0176。
根因：用排序表達淘汰，語意承載不了；真實災難組的指標可以比任何事先猜測的
哨兵值更差，且在打平時（如 profit_factor 兩組皆為 0.0），排序結果依賴
DataFrame 內部排序穩定性等實作細節，不是設計保證。

而且選錯不只是排序難看。run() 選最佳後會用 best_row 的參數重新建立 config
並再跑一次 `bt.run()`。若淘汰組因打平而排第一，這次重跑可能重新觸發同一個
ValueError，把我們原本要擋住的爆炸從後門帶回來（見
test_all_combos_disqualified_raises_clear_error_instead_of_rerunning）。

本檔驗證修正後的設計：
1. run() 選最佳時，先排除 liquidated == True 的列，再排序取 iloc[0]。
2. 若排除後一列都不剩，拋出語意清楚的 RuntimeError（不重跑、不吞）。
3. all_results（df_results）仍保留淘汰/強平列供使用者檢視。
4. 對多種 metric，最佳解永遠不是 liquidated=True 的列。
"""
import pandas as pd
import pytest

from backtest.config import Config
from backtest.optimizer import GridOptimizer
from backtest.backtester import BacktestResult


def _tiny_df(n=5):
    return pd.DataFrame({
        "open_time": pd.date_range("2024-01-01", periods=n, freq="1min"),
        "open": [100.0] * n, "high": [100.2] * n, "low": [99.8] * n,
        "close": [100.0] * n, "volume": [100.0] * n,
    })


def _br(**overrides):
    """建構一個可控的 BacktestResult，預設是「安全正常」的結果。"""
    base = dict(
        final_equity=105.0, return_pct=0.05, max_drawdown=0.02,
        realized_pnl=5.0, unrealized_pnl=0.0, total_pnl=5.0,
        trades_count=10, win_rate=0.6, profit_factor=0.0, sharpe_ratio=1.2,
        direction="long", config=Config(), liquidated=False,
    )
    base.update(overrides)
    return BacktestResult(**base)


def _base_opt(param_ranges, dispatch_run_single, dispatch_bt_run, monkeypatch, df=None):
    """建立一個 GridOptimizer，並將 _run_single_backtest（grid loop）與
    GridBacktester.run（run() 最後選最佳後的重跑）都換成完全可控的假實作，
    使測試不依賴真實回測數值、確定且可重現。"""
    from backtest import optimizer as optimizer_module

    monkeypatch.setattr(
        GridOptimizer, "_run_single_backtest",
        lambda self, params: dispatch_run_single(params)
    )
    monkeypatch.setattr(
        optimizer_module.GridBacktester, "run",
        lambda self: dispatch_bt_run(self.config)
    )

    opt = GridOptimizer(
        df if df is not None else _tiny_df(),
        base_config=Config(symbol="BNBUSDC", initial_balance=100.0, leverage=10,
                           direction="long", terminal_ui_mode=True),
        param_ranges=param_ranges,
    )
    return opt


def test_disqualified_combo_never_selected_as_best_even_when_tied(monkeypatch):
    """三組：leverage=20 是真實災難組（liquidated=True, profit_factor=0.0,
    插入順序最前）、leverage=21 是 ValueError 淘汰組（sentinel, profit_factor
    =0.0）、leverage=22 是正常組（liquidated=False, profit_factor=0.0，零成交
    但未觸發強平 —— 真實世界中止盈間距過寬會出現這種合法的 0.0）。

    三組在 metric=profit_factor 上完全打平。改動前必紅：現行碼（排序表達
    淘汰）在打平時直接取 sort 後的 iloc[0]，無視 liquidated 旗標；經驗證
    pandas.sort_values 對這組資料的 tie-break 是插入順序，第一筆（真實災難
    組 leverage=20）會被選為最佳 —— 一個 liquidated=True 的結果被當成優化
    結果回傳給使用者，且完全沒有任何錯誤或警告。
    """
    disaster_result = _br(final_equity=-1.7633, return_pct=-1.0176,
                           max_drawdown=1.0176, profit_factor=0.0,
                           sharpe_ratio=-2.0, liquidated=True)
    normal_result = _br(final_equity=100.0, return_pct=0.0, max_drawdown=0.0,
                         profit_factor=0.0, trades_count=0, win_rate=0.0,
                         sharpe_ratio=0.0, liquidated=False)

    def dispatch_run_single(params):
        lev = params["leverage"]
        if lev == 20:
            r = disaster_result
        elif lev == 21:
            raise ValueError("模擬上游防禦破洞：price 非有限值")
        else:
            r = normal_result
        return {
            **params,
            "return_pct": r.return_pct, "max_drawdown": r.max_drawdown,
            "trades": r.trades_count, "win_rate": r.win_rate,
            "profit_factor": r.profit_factor, "sharpe_ratio": r.sharpe_ratio,
            "final_equity": r.final_equity, "realized_pnl": r.realized_pnl,
            "unrealized_pnl": r.unrealized_pnl, "liquidated": r.liquidated,
            "value_error_eliminated": False,
        }

    def real_dispatch_run_single(params):
        # 手動複製 optimizer.py 對 ValueError 的淘汰 dict 邏輯（不 import 私有
        # 方法，避免測試與實作耦合過緊；數值需與 optimizer.py 現行實作一致）。
        try:
            return dispatch_run_single(params)
        except ValueError:
            return {
                **params,
                "return_pct": float("-inf"), "max_drawdown": float("inf"),
                "trades": 0, "win_rate": 0.0, "profit_factor": 0.0,
                "sharpe_ratio": float("-inf"), "final_equity": float("-inf"),
                "realized_pnl": 0.0, "unrealized_pnl": 0.0,
                "liquidated": True, "value_error_eliminated": True,
            }

    def dispatch_bt_run(config):
        if config.leverage == 20:
            return disaster_result
        elif config.leverage == 21:
            raise ValueError("模擬上游防禦破洞：price 非有限值")
        return normal_result

    opt = _base_opt(
        param_ranges={
            "take_profit_spacing": [0.001], "grid_spacing": [0.002],
            "leverage": [20, 21, 22],
        },
        dispatch_run_single=real_dispatch_run_single,
        dispatch_bt_run=dispatch_bt_run,
        monkeypatch=monkeypatch,
    )

    result = opt.run(metric="profit_factor", ascending=False, n_jobs=1)

    assert result.best_result.liquidated is False, (
        "最佳解不得是 liquidated=True 的列（無論是真實強平還是 ValueError 淘汰）"
    )
    assert result.best_config.leverage == 22


def test_all_combos_disqualified_raises_clear_error_instead_of_rerunning(monkeypatch):
    """所有組合皆拋 ValueError -> run() 必須拋 RuntimeError（附組數），
    不得走到 best_row 重跑那一行去踩 ValueError 後門。

    改動前必紅：現行碼會在 run() 內 `best_result = bt.run()` 那行拋
    ValueError（因為 df_results.iloc[0] 選到的是唯一存在、但 liquidated=True
    的哨兵組，重跑同一組參數會再次觸發下方 monkeypatch 的 ValueError）。
    """
    from backtest import optimizer as optimizer_module

    def _always_raise(self):
        raise ValueError("模擬全滅：所有參數組合皆觸發防線")

    monkeypatch.setattr(optimizer_module.GridBacktester, "run", _always_raise)

    opt = GridOptimizer(
        _tiny_df(),
        base_config=Config(symbol="BNBUSDC", initial_balance=100.0, leverage=10,
                           direction="long", terminal_ui_mode=True),
        param_ranges={
            "take_profit_spacing": [0.001, 0.002],
            "grid_spacing": [0.005],
            "leverage": [10],
        },
    )

    with pytest.raises(RuntimeError) as exc_info:
        opt.run(metric="return_pct", ascending=False, n_jobs=1)

    # 必須是純 RuntimeError，不是 ValueError 從後門冒出來
    assert not isinstance(exc_info.value, ValueError)
    msg = str(exc_info.value)
    assert "2" in msg  # 兩組全被淘汰，訊息需附組數


def test_liquidated_rows_still_present_in_results_dataframe(monkeypatch):
    """淘汰/強平的列仍在 df_results 裡（使用者要看得到），只是不被選為最佳。"""
    from backtest import optimizer as optimizer_module

    original_run = optimizer_module.GridBacktester.run
    call_count = {"n": 0}

    def _sometimes_raise(self):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise ValueError("模擬上游防禦破洞")
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

    assert len(result.all_results) == 3
    assert result.all_results["liquidated"].sum() == 1


@pytest.mark.parametrize("metric,ascending", [
    ("return_pct", False),
    ("sharpe_ratio", False),
    ("profit_factor", False),
    ("max_drawdown", True),
    ("final_equity", False),
])
def test_best_never_liquidated_across_metrics(monkeypatch, metric, ascending):
    """跨 metric：災難組（真實強平）+ ValueError 淘汰組 + 正常組混合時，
    無論用哪個 metric 排序，最佳解永遠不是 liquidated=True 的列。
    """
    disaster_result = _br(final_equity=-1.7633, return_pct=-1.0176,
                           max_drawdown=1.0176, profit_factor=0.0,
                           sharpe_ratio=-2.0, liquidated=True)
    normal_result = _br(final_equity=110.0, return_pct=0.10, max_drawdown=0.03,
                         profit_factor=1.8, trades_count=8, win_rate=0.7,
                         sharpe_ratio=1.5, liquidated=False)

    def dispatch(params):
        lev = params["leverage"]
        if lev == 20:
            r = disaster_result
        elif lev == 21:
            raise ValueError("模擬上游防禦破洞")
        else:
            r = normal_result
        return {
            **params,
            "return_pct": r.return_pct, "max_drawdown": r.max_drawdown,
            "trades": r.trades_count, "win_rate": r.win_rate,
            "profit_factor": r.profit_factor, "sharpe_ratio": r.sharpe_ratio,
            "final_equity": r.final_equity, "realized_pnl": r.realized_pnl,
            "unrealized_pnl": r.unrealized_pnl, "liquidated": r.liquidated,
            "value_error_eliminated": False,
        }

    def real_dispatch(params):
        try:
            return dispatch(params)
        except ValueError:
            return {
                **params,
                "return_pct": float("-inf"), "max_drawdown": float("inf"),
                "trades": 0, "win_rate": 0.0, "profit_factor": 0.0,
                "sharpe_ratio": float("-inf"), "final_equity": float("-inf"),
                "realized_pnl": 0.0, "unrealized_pnl": 0.0,
                "liquidated": True, "value_error_eliminated": True,
            }

    def dispatch_bt_run(config):
        if config.leverage == 20:
            return disaster_result
        elif config.leverage == 21:
            raise ValueError("模擬上游防禦破洞")
        return normal_result

    opt = _base_opt(
        param_ranges={
            "take_profit_spacing": [0.001], "grid_spacing": [0.002],
            "leverage": [20, 21, 22],
        },
        dispatch_run_single=real_dispatch,
        dispatch_bt_run=dispatch_bt_run,
        monkeypatch=monkeypatch,
    )

    result = opt.run(metric=metric, ascending=ascending, n_jobs=1)

    assert result.best_result.liquidated is False
    assert result.best_config.leverage == 22
