"""
回測管理器
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List

import ccxt
import pandas as pd

from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from .utils import DATA_DIR, console
from .config import SymbolConfig


class BacktestManager:
    """回測管理器 - 簡化版，直接輸入交易對符號"""

    def __init__(self):
        self.data_dir = DATA_DIR

    def get_data_path(self, symbol_raw: str) -> Path:
        """獲取數據路徑"""
        return self.data_dir / f"futures/um/daily/klines/{symbol_raw}/1m"

    def get_available_dates(self, symbol_raw: str) -> List[str]:
        """獲取可用日期"""
        path = self.get_data_path(symbol_raw)
        if not path.exists():
            return []

        dates = []
        for f in path.glob(f"{symbol_raw}-1m-*.csv"):
            try:
                parts = f.stem.split('-')
                if len(parts) >= 5:
                    date_str = f"{parts[2]}-{parts[3]}-{parts[4]}"
                    dates.append(date_str)
            except Exception:
                pass

        return sorted(dates)

    def load_data(self, symbol_raw: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """載入歷史數據"""
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")

        all_data = []
        current = start

        while current <= end:
            date_str = current.strftime("%Y-%m-%d")
            path = self.get_data_path(symbol_raw) / f"{symbol_raw}-1m-{date_str}.csv"

            if path.exists():
                try:
                    df = pd.read_csv(path)
                    if 'open_time' in df.columns:
                        df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
                    all_data.append(df)
                except Exception as e:
                    console.print(f"[yellow]載入 {date_str} 失敗: {e}[/]")

            current += timedelta(days=1)

        if not all_data:
            return None

        full_df = pd.concat(all_data, ignore_index=True)
        return full_df.sort_values('open_time').reset_index(drop=True)

    def download_data(self, symbol_raw: str, ccxt_symbol: str, start_date: str, end_date: str) -> bool:
        """下載歷史數據"""
        try:
            exchange = ccxt.binance({
                'enableRateLimit': True,
                'options': {'defaultType': 'future'}
            })

            fetch_symbol = ccxt_symbol.split(":")[0]

            start = datetime.strptime(start_date, "%Y-%m-%d")
            end = datetime.strptime(end_date, "%Y-%m-%d")

            total_bars = 0
            days = (end - start).days + 1

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                console=console
            ) as progress:
                task = progress.add_task(f"下載 {symbol_raw}...", total=days)
                current = start

                while current <= end:
                    date_str = current.strftime("%Y-%m-%d")
                    output_path = self.get_data_path(symbol_raw) / f"{symbol_raw}-1m-{date_str}.csv"

                    if not output_path.exists():
                        output_path.parent.mkdir(parents=True, exist_ok=True)

                        since = int(datetime(current.year, current.month, current.day).timestamp() * 1000)
                        until = since + 24 * 60 * 60 * 1000

                        try:
                            ohlcv = exchange.fetch_ohlcv(fetch_symbol, "1m", since=since, limit=1500)
                            if ohlcv:
                                ohlcv = [bar for bar in ohlcv if bar[0] < until]
                                df = pd.DataFrame(ohlcv, columns=['open_time', 'open', 'high', 'low', 'close', 'volume'])
                                df.to_csv(output_path, index=False)
                                total_bars += len(df)
                        except Exception as e:
                            console.print(f"[red]{date_str}: {e}[/]")

                    current += timedelta(days=1)
                    progress.update(task, advance=1)

            console.print(f"[green]下載完成: {total_bars:,} 條數據[/]")
            return True

        except Exception as e:
            console.print(f"[red]下載失敗: {e}[/]")
            return False

    def run_backtest(self, config: SymbolConfig, df: pd.DataFrame) -> dict:
        """執行回測 — 委派給 backtest.backtester.GridBacktester（決策同源 grid_engine.decision.decide()）。

        將 grid_engine.config.SymbolConfig 映射為 backtest.config.Config，走與實盤一致的
        terminal_ui_mode（initial_quantity 幣量下單），確保與 as_terminal_max.py 實盤路徑同源。
        """
        # 延遲 import：避免 grid_engine 套件層級對 backtest 套件產生循環依賴
        from backtest.config import Config as BacktestConfig
        from backtest.backtester import GridBacktester

        bt_config = BacktestConfig(
            symbol=config.symbol,
            take_profit_spacing=config.take_profit_spacing,
            grid_spacing=config.grid_spacing,
            initial_quantity=config.initial_quantity,
            leverage=config.leverage,
            limit_multiplier=config.limit_multiplier,
            threshold_multiplier=config.threshold_multiplier,
            direction=getattr(config, "direction", "both"),
            terminal_ui_mode=True,
        )

        result = GridBacktester(df, bt_config).run()
        return result.to_dict()

    def optimize_params(self, config: SymbolConfig, df: pd.DataFrame, progress_callback=None) -> List[dict]:
        """優化參數"""
        results = []

        take_profits = [0.002, 0.003, 0.004, 0.005, 0.006]
        grid_spacings = [0.004, 0.006, 0.008, 0.01, 0.012]

        valid_combos = [(tp, gs) for tp in take_profits for gs in grid_spacings if tp < gs]
        total = len(valid_combos)

        for i, (tp, gs) in enumerate(valid_combos):
            test_config = SymbolConfig(
                symbol=config.symbol,
                ccxt_symbol=config.ccxt_symbol,
                take_profit_spacing=tp,
                grid_spacing=gs,
                initial_quantity=config.initial_quantity,
                leverage=config.leverage
            )

            result = self.run_backtest(test_config, df)
            result["take_profit_spacing"] = tp
            result["grid_spacing"] = gs
            results.append(result)

            if progress_callback:
                progress_callback(i + 1, total)

        results.sort(key=lambda x: x["return_pct"], reverse=True)
        return results
