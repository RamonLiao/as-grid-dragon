"""新舊回測引擎對比（#9 遷移一次性驗證）。

成本歸零對齊（fee/滑價/funding/hard_stop 全關）後，
同一 symbol+日期跑舊 core.backtest 與新 backtest.GridBacktester，
比純網格邏輯的收益率/回撤量級。差一個數量級 = 參數映射 bug。

注意：Phase 2 刪 core/ 後本 script 的舊引擎路徑失效，僅留存歷史。
用法: uv run python scripts/compare_backtest_engines.py ETHUSDC 2026-01-25 2026-01-31
      uv run python scripts/compare_backtest_engines.py BNBUSDC 2025-11-17 2025-11-23
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main(symbol: str, start: str, end: str):
    from web.services import config_store, backtest_service
    from backtest.data_loader import DataLoader

    config = config_store.load_config()
    sym_config = next(
        (s for s in config.symbols.values() if s.symbol == symbol), None)
    if sym_config is None:
        print(f"config 中找不到 {symbol}")
        sys.exit(1)

    # 預設 data_dir 只有 funding 子目錄，K 線在 asBack/data/ 下（見任務踩坑記錄）
    loader = DataLoader(data_dir=str(Path(__file__).resolve().parent.parent / "asBack" / "data"))
    df = loader.load(symbol, start, end)
    if df is None or df.empty:
        print(f"無數據: {symbol} {start}~{end}（先在頁3 下載）")
        sys.exit(1)
    print(f"載入 {len(df):,} 條 K 線")

    # --- 成本對齊策略 ---
    # 舊引擎 fee 寫死每邊 0.0004（core/backtest.py:230，無法參數關閉）；
    # 新引擎實際執行路徑 _run_terminal_ui_mode（backtester.py:540,587,606,616）
    # 每邊直接收整個 fee_pct（無 /2；帶 /2 的 282/324 等行屬未執行的 _run_legacy_mode）。
    # → fee 對齊：新引擎 fee_pct=0.0004 ⇒ 每邊 0.0004 = 舊引擎（同構，非兩倍）。
    # 滑價/funding/hard_stop 兩邊皆可關 → 全關。

    # --- 新引擎 ---
    from backtest.backtester import GridBacktester
    new_cfg = backtest_service.to_backtest_config(sym_config, zero_costs=True)
    new_cfg.fee_pct = 0.0004  # 每邊 0.0004，對齊舊引擎寫死值（terminal_ui_mode 路徑無 /2）
    new_result = GridBacktester(df, new_cfg).run()

    # --- 舊引擎（成本經由 run_backtest kwargs 關閉，core/backtest.py:194-198） ---
    from core.backtest import BacktestManager
    old_manager = BacktestManager()
    old_result = old_manager.run_backtest(
        sym_config, df,
        hard_stop_pct=1e9,    # 永不觸發
        slippage_pct=0.0,     # 關隨機滑價
        funding_rate=0.0,     # 關資金費率
    )

    old_return_pct = old_result["return_pct"] * 100
    old_max_dd = old_result["max_drawdown"] * 100
    old_trades = old_result["trades_count"]

    new_return_pct = new_result.return_pct * 100
    new_max_dd = new_result.max_drawdown * 100
    new_trades = new_result.trades_count

    print("\n=== 對比（成本歸零，純網格邏輯） ===")
    print(f"{'指標':<16}{'舊引擎':>14}{'新引擎':>14}")
    print(f"{'收益率%':<16}{old_return_pct:>14.4f}{new_return_pct:>14.4f}")
    print(f"{'最大回撤%':<16}{old_max_dd:>14.4f}{new_max_dd:>14.4f}")
    print(f"{'成交筆數':<16}{old_trades:>14}{new_trades:>14}")
    print("\n判讀：方向一致、量級同階（比值 0.2x~5x 內）= PASS；"
          "差一個數量級以上 = 映射 bug，回頭查 to_backtest_config。")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1], sys.argv[2], sys.argv[3])
