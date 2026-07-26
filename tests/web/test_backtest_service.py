"""backtest_service 黃金測試。

為什麼重要：SymbolConfig→backtest.Config 映射錯了不會炸，
只會默默給錯回測結論（量化系統最貴的一類 bug）。
已知輸入→已知輸出鎖死每個欄位。
"""
import pandas as pd
import numpy as np
import pytest

from web.services import backtest_service
from grid_engine.config import SymbolConfig
from backtest.backtester import BacktestResult
from backtest.optimizer import GridOptimizer


_FAKE_NOTES = "回測保真限制: (test fixture notes, 非真正 FIDELITY_NOTES)"


SYM = SymbolConfig(
    symbol="XRPUSDC", ccxt_symbol="XRP/USDC:USDC", enabled=True,
    take_profit_spacing=0.004, grid_spacing=0.006,
    initial_quantity=3.0, assumed_leverage=7,
    limit_multiplier=5.0, threshold_multiplier=20.0,
)


def test_to_backtest_config_golden():
    cfg = backtest_service.to_backtest_config(SYM)
    assert cfg.symbol == "XRPUSDC"
    assert cfg.initial_quantity == 3.0          # 預設 0.0=空回測，必須帶入
    # 7 是刻意選的非預設值（SymbolConfig.assumed_leverage 預設 20）：
    # 若映射被寫死或落回預設，本斷言會紅（見 mapping-guard-report.md）。
    assert cfg.leverage == 7
    assert cfg.take_profit_spacing == 0.004     # 兩邊皆小數比例，1:1
    assert cfg.grid_spacing == 0.006
    assert cfg.limit_multiplier == 5.0          # 不帶 → backtester 用預設 5.0（grid_engine/config.py:48）
    assert cfg.threshold_multiplier == 20.0     # 預設 20.0（grid_engine/config.py:49）
    assert cfg.initial_balance == 1000.0
    # 成本模型：單次回測用引擎預設（保真）
    assert cfg.fee_pct == 0.0002   # maker（網格全是限價單），見 spec G7
    assert cfg.funding_enabled is True
    assert cfg.position_threshold == 0.0
    assert cfg.position_limit == 0.0


def test_grid_optimizer_create_config_preserves_multiplier_fields():
    """回歸測試：Config.to_dict()→from_dict() round-trip 曾漏序列化
    initial_quantity/limit_multiplier/threshold_multiplier/terminal_ui_mode，
    導致 GridOptimizer._create_config 產出的 config 這四欄被打回預設值，
    initial_quantity 3.0→0.0 令 GridBacktester.run() 誤判走 legacy 引擎，
    使用者 multiplier 配置全丟。"""
    base_config = backtest_service.to_backtest_config(SYM)
    optimizer = GridOptimizer(_make_df(), base_config=base_config)
    config = optimizer._create_config({})
    assert config.initial_quantity == 3.0
    assert config.threshold_multiplier == 20.0
    assert config.limit_multiplier == 5.0
    assert config.terminal_ui_mode is True


def test_to_backtest_config_rejects_zero_quantity():
    """initial_quantity<=0 會落入 legacy 絕對值路徑（500/100 預設）→ 直接拒絕。"""
    bad = SymbolConfig(symbol="X", ccxt_symbol="X/USDC:USDC", initial_quantity=0)
    with pytest.raises(ValueError):
        backtest_service.to_backtest_config(bad)


def test_to_backtest_config_zero_costs():
    """新舊引擎對比模式：成本全歸零。"""
    cfg = backtest_service.to_backtest_config(SYM, zero_costs=True)
    assert cfg.fee_pct == 0.0
    assert cfg.slippage_bps == 0.0
    assert cfg.funding_enabled is False


def _make_df(n=300, price=1.0):
    """合成 1m K 線：正弦波動保證網格有成交。"""
    ts = pd.date_range("2026-01-01", periods=n, freq="1min")
    wave = price * (1 + 0.02 * np.sin(np.arange(n) / 20))
    return pd.DataFrame({
        "timestamp": ts, "open": wave, "high": wave * 1.001,
        "low": wave * 0.999, "close": wave, "volume": 100.0,
    })


def test_run_single_backtest_returns_view_dict():
    view = backtest_service.run_single_backtest(SYM, _make_df())
    for key in ("return_pct", "max_drawdown", "total_pnl", "trades_count",
                "win_rate", "profit_factor", "sharpe_ratio", "final_equity",
                "trade_history", "equity_curve"):
        assert key in view, f"view 缺 {key}"
    assert isinstance(view["trades_count"], int)


def test_backtest_result_to_view_full_keyset():
    """view dict 是頁面渲染契約：15 個 key 一個不能少。

    這條測試原本鎖的是「13 個 key」——那個數字本身就是缺陷：view dict 漏了
    liquidated 與 peak_margin_usage，導致單次回測若在期間觸發強平（spec §7
    一票否決），前端完全看不到任何信號，使用者會把爆倉組的結果當成「表現平平」
    而非「不可用」。任何鎖住現況的測試都有這個風險——它不知道現況是對的還是
    錯的，只是把當下的行為原封不動地凍結成規格。dual-review 外部獨立 review
    抓出這點後，keyset 補上 liquidated / peak_margin_usage，這條測試也跟著改寫，
    不再把缺陷寫成契約。
    """
    view = backtest_service.run_single_backtest(SYM, _make_df())
    assert set(view.keys()) == {
        "return_pct", "max_drawdown", "realized_pnl", "unrealized_pnl",
        "total_pnl", "trades_count", "win_rate", "profit_factor",
        "sharpe_ratio", "final_equity", "trade_history", "equity_curve",
        "notes", "liquidated", "peak_margin_usage",
    }


def test_view_dict_exposes_liquidated_flag_so_ui_cannot_silently_ignore_it():
    """單次回測若觸發強平，view dict 必須帶上明確信號，否則使用者會把爆倉組
    當成「表現平平」而非「一票否決」。

    失敗場景：使用者在 UI 對一組高槓桿 / 關掉裝死的參數跑單次回測，該組在
    K 線中途爆倉。修法前 view dict 只回 final_equity（爆倉後餘額）、
    max_drawdown、notes（通用保真限制文字），沒有任何「此組已強平、結果不可用」
    的信號——使用者據此調整實盤參數，等於拿一票否決的組當最佳解看。

    修法把警告寫進 notes（前端已經在渲染的欄位），零 UI 改動即生效：不變式由
    持有它的模組（backtest_service）保證，不外包給消費端（Streamlit 頁面）
    自行判讀 liquidated flag。
    """
    liquidated_result = BacktestResult(
        final_equity=10.0,
        return_pct=-0.99,
        max_drawdown=0.99,
        realized_pnl=-990.0,
        unrealized_pnl=0.0,
        total_pnl=-990.0,
        trades_count=5,
        win_rate=0.2,
        profit_factor=0.1,
        sharpe_ratio=-2.0,
        direction="long",
        config=backtest_service.to_backtest_config(SYM),
        trade_history=[],
        equity_curve=[(0, 1.0, 10.0)],
        notes=_FAKE_NOTES,
        funding_paid=0.0,
        peak_margin_usage=1.0,
        liquidated=True,
    )
    view = backtest_service.backtest_result_to_view(liquidated_result)

    assert view["liquidated"] is True
    assert view["notes"].startswith("⚠️")
    assert "強平" in view["notes"]
    assert "一票否決" in view["notes"]
    assert _FAKE_NOTES in view["notes"]  # 原本的 FIDELITY_NOTES 內容不能被吃掉，只是前面加警告


def test_view_dict_has_no_liquidation_warning_when_not_liquidated():
    """負向對照：未強平時，notes 就是原本的 FIDELITY_NOTES，開頭沒有警告。"""
    normal_result = BacktestResult(
        final_equity=1050.0,
        return_pct=0.05,
        max_drawdown=0.02,
        realized_pnl=50.0,
        unrealized_pnl=0.0,
        total_pnl=50.0,
        trades_count=10,
        win_rate=0.6,
        profit_factor=1.5,
        sharpe_ratio=1.2,
        direction="long",
        config=backtest_service.to_backtest_config(SYM),
        trade_history=[],
        equity_curve=[(0, 1.0, 1050.0)],
        notes=_FAKE_NOTES,
        funding_paid=0.0,
        peak_margin_usage=0.3,
        liquidated=False,
    )
    view = backtest_service.backtest_result_to_view(normal_result)

    assert view["liquidated"] is False
    assert view["notes"] == _FAKE_NOTES
    assert not view["notes"].startswith("⚠️")


def test_grid_optimization_returns_dataframe():
    param_ranges = {"take_profit_spacing": [0.003, 0.004],
                    "grid_spacing": [0.005, 0.006]}
    df = backtest_service.run_grid_optimization(
        SYM, _make_df(), param_ranges=param_ranges)
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 4  # 2x2 組合
    assert "take_profit_spacing" in df.columns
    assert "return_pct" in df.columns


def test_smart_optimization_returns_dataframe():
    pytest.importorskip("optuna")
    df = backtest_service.run_smart_optimization(
        SYM, _make_df(), n_trials=3, objective="sharpe", trading_mode="swing")
    assert isinstance(df, pd.DataFrame)
    assert len(df) >= 1
    assert "objective_value" in df.columns
