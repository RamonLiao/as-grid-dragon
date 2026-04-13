"""
AS 網格交易系統 - MAX 版本
==============================
基於 as_terminal_pro.py 增強版本

新增功能:
1. Funding Rate 偏向機制 - 根據資金費率調整多空偏好
2. GLFT 風險係數 γ - 更精細的庫存控制
3. 動態網格範圍 - ATR/波動率自適應

依賴:
-----
pip install rich ccxt websockets pandas numpy

使用:
-----
python as_terminal_max.py
"""

import asyncio
import time
import threading
from datetime import datetime, timedelta
from typing import Optional, List

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.live import Live
from rich import box
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.prompt import Prompt, Confirm, IntPrompt, FloatPrompt

# 從 grid_engine 導入所有模組
from grid_engine import (
    SYMBOL_MAP, normalize_symbol, console, logger,
    GridStrategy,
    MaxEnhancement, BanditConfig, DGTConfig, LeadingIndicatorConfig,
    SymbolConfig, RiskConfig, GlobalConfig,
    SymbolState, AccountBalance, GlobalState,
    BacktestManager, MaxGridBot, TerminalUI,
)

# 選幣模組
try:
    from coin_selection import (
        CoinScorer, CoinRanker, SymbolScanner,
        scan_grid_candidates, SymbolInfo, AmplitudeStats,
    )
    COIN_SELECTION_AVAILABLE = True
except ImportError:
    COIN_SELECTION_AVAILABLE = False


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                              主菜單                                            ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

class MainMenu:
    def __init__(self):
        self.config = GlobalConfig.load()
        self.backtest_manager = BacktestManager()

        # 背景交易相關
        self.bot: Optional[MaxGridBot] = None
        self.bot_thread: Optional[threading.Thread] = None
        self.bot_loop: Optional[asyncio.AbstractEventLoop] = None
        self._trading_active = False

    def show_banner(self):
        console.clear()
        console.print(Panel.fit(
            "[bold cyan]AS 網格交易系統[/] [bold yellow]MAX[/]\n"
            "[dim]Funding Rate · GLFT · 動態網格[/]",
            border_style="yellow"
        ))
        console.print()

    def main_menu(self):
        while True:
            self.show_banner()

            if self._trading_active and self.bot:
                console.print("[bold green]● 交易運行中[/]", end="  ")
                if self.bot.state.start_time:
                    duration = datetime.now() - self.bot.state.start_time
                    hours, remainder = divmod(int(duration.total_seconds()), 3600)
                    minutes, seconds = divmod(remainder, 60)
                    console.print(f"[dim]運行時間: {hours:02d}:{minutes:02d}:{seconds:02d}[/]", end="  ")
                console.print(f"[dim]浮盈: {self.bot.state.total_unrealized_pnl:+.2f}[/]\n")

            if self.config.symbols:
                enabled = [s for s in self.config.symbols.values() if s.enabled]
                console.print(f"[dim]已配置 {len(self.config.symbols)} 個交易對，{len(enabled)} 個啟用[/]\n")

            console.print("[bold]請選擇操作:[/]\n")

            if self._trading_active:
                console.print("  [cyan]1[/] 查看交易面板")
                console.print("  [cyan]s[/] [red]停止交易[/]")
            else:
                console.print("  [cyan]1[/] 開始交易")

            console.print("  [cyan]2[/] 管理交易對")
            console.print("  [cyan]3[/] 回測/優化")
            console.print("  [cyan]4[/] MAX 增強設定")
            console.print("  [cyan]5[/] 學習模組 (Bandit/DGT)")
            console.print("  [cyan]6[/] 風控設定")
            console.print("  [cyan]7[/] API 設定")
            if COIN_SELECTION_AVAILABLE:
                console.print("  [cyan]8[/] 選幣分析")
            console.print("  [cyan]0[/] 退出")
            console.print()

            valid_choices = ["0", "1", "2", "3", "4", "5", "6", "7"]
            if COIN_SELECTION_AVAILABLE:
                valid_choices.append("8")
            if self._trading_active:
                valid_choices.append("s")

            choice = Prompt.ask("選擇", choices=valid_choices, default="1")

            if choice == "0":
                if self._trading_active:
                    if Confirm.ask("[yellow]交易運行中，確定要退出嗎？[/]"):
                        self.stop_trading()
                        break
                else:
                    break
            elif choice == "1":
                if self._trading_active:
                    self.view_trading_panel()
                else:
                    self.start_trading()
            elif choice == "s" and self._trading_active:
                self.stop_trading()
            elif choice == "2":
                self.manage_symbols()
            elif choice == "3":
                self.quick_backtest()
            elif choice == "4":
                self.setup_max_enhancement()
            elif choice == "5":
                self.setup_learning()
            elif choice == "6":
                self.setup_risk()
            elif choice == "7":
                self.setup_api()
            elif choice == "8" and COIN_SELECTION_AVAILABLE:
                self.coin_selection_menu()

    def quick_backtest(self):
        """快速回測"""
        self.show_banner()
        console.print("[bold]回測/優化[/]\n")
        console.print("[dim]直接輸入交易對符號，如: XRPUSDC, BTCUSDT[/]\n")

        symbol_input = Prompt.ask("交易對").strip()
        raw, ccxt_sym, coin, quote = normalize_symbol(symbol_input)

        if not raw:
            console.print(f"[red]無法識別交易對: {symbol_input}[/]")
            console.print("[dim]支援格式: XRPUSDC, BTCUSDT, ETH/USDC 等[/]")
            Prompt.ask("按 Enter 繼續")
            return

        console.print(f"\n[green]識別為: {coin}/{quote} ({raw})[/]\n")

        available_dates = self.backtest_manager.get_available_dates(raw)

        if available_dates:
            console.print(f"[dim]已有數據: {available_dates[0]} 至 {available_dates[-1]}[/]\n")

        today = datetime.now()
        console.print("[bold]選擇回測時間範圍:[/]\n")
        console.print("  [cyan]1[/] 最近 7 天  (1W)")
        console.print("  [cyan]2[/] 最近 14 天 (2W)")
        console.print("  [cyan]3[/] 最近 30 天 (1M)")
        console.print("  [cyan]4[/] 最近 90 天 (3M)")
        console.print("  [cyan]5[/] 最近 180 天 (6M)")
        console.print("  [cyan]6[/] 最近 365 天 (1Y)")
        console.print("  [cyan]7[/] 自定義日期範圍")
        console.print()

        date_choice = Prompt.ask("選擇", choices=["1", "2", "3", "4", "5", "6", "7"], default="3")

        date_ranges = {
            "1": 7, "2": 14, "3": 30, "4": 90, "5": 180, "6": 365,
        }

        if date_choice == "7":
            default_start = available_dates[0] if available_dates else (today - timedelta(days=30)).strftime("%Y-%m-%d")
            default_end = (today - timedelta(days=1)).strftime("%Y-%m-%d")
            start_date = Prompt.ask("開始日期 (YYYY-MM-DD)", default=default_start)
            end_date = Prompt.ask("結束日期 (YYYY-MM-DD)", default=default_end)
        else:
            days = date_ranges[date_choice]
            end_date = (today - timedelta(days=1)).strftime("%Y-%m-%d")
            start_date = (today - timedelta(days=days)).strftime("%Y-%m-%d")
            console.print(f"\n[dim]時間範圍: {start_date} 至 {end_date}[/]")

        need_download = False
        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")

            current = start_dt
            while current <= end_dt:
                date_str = current.strftime("%Y-%m-%d")
                if date_str not in available_dates:
                    need_download = True
                    break
                current += timedelta(days=1)
        except ValueError:
            console.print("[red]日期格式錯誤[/]")
            Prompt.ask("按 Enter 繼續")
            return

        if need_download:
            console.print("\n[yellow]部分數據缺失，開始下載...[/]\n")
            if not self.backtest_manager.download_data(raw, ccxt_sym, start_date, end_date):
                console.print("[red]下載失敗[/]")
                Prompt.ask("按 Enter 繼續")
                return

        console.print("\n載入數據...")
        df = self.backtest_manager.load_data(raw, start_date, end_date)

        if df is None or df.empty:
            console.print("[red]載入數據失敗[/]")
            Prompt.ask("按 Enter 繼續")
            return

        console.print(f"[green]載入 {len(df):,} 條 K 線[/]\n")

        console.print("  [cyan]1[/] 執行回測 (使用當前/默認參數)")
        console.print("  [cyan]2[/] 參數優化 (搜索最佳參數)")
        console.print()

        mode = Prompt.ask("選擇", choices=["1", "2"], default="2")

        if raw in self.config.symbols:
            sym_config = self.config.symbols[raw]
        else:
            sym_config = SymbolConfig(symbol=raw, ccxt_symbol=ccxt_sym)

        if mode == "1":
            console.print("\n執行回測...\n")
            result = self.backtest_manager.run_backtest(sym_config, df)
            self._show_backtest_result(result)

        else:
            console.print("\n執行參數優化...\n")

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                console=console
            ) as progress:
                task = progress.add_task("優化中...", total=100)

                def update_progress(current, total):
                    progress.update(task, completed=current * 100 // total)

                results = self.backtest_manager.optimize_params(sym_config, df, update_progress)

            console.print("\n" + "="*60)
            console.print("[bold cyan]優化結果 (Top 5)[/]")
            console.print("="*60 + "\n")

            table = Table(box=box.ROUNDED)
            table.add_column("#", style="dim")
            table.add_column("止盈", justify="right")
            table.add_column("補倉", justify="right")
            table.add_column("收益率", justify="right")
            table.add_column("回撤", justify="right")
            table.add_column("交易數", justify="right")
            table.add_column("勝率", justify="right")

            for i, r in enumerate(results[:5], 1):
                return_color = "green" if r["return_pct"] >= 0 else "red"
                table.add_row(
                    str(i),
                    f"{r['take_profit_spacing']*100:.2f}%",
                    f"{r['grid_spacing']*100:.2f}%",
                    f"[{return_color}]{r['return_pct']*100:.2f}%[/]",
                    f"{r['max_drawdown']*100:.2f}%",
                    str(r['trades_count']),
                    f"{r['win_rate']*100:.1f}%"
                )

            console.print(table)

            if results:
                console.print()
                console.print(f"[dim]當前參數: 止盈 {sym_config.take_profit_spacing*100:.2f}%, 補倉 {sym_config.grid_spacing*100:.2f}%[/]")
                console.print(f"[bold]最佳參數: 止盈 {results[0]['take_profit_spacing']*100:.2f}%, 補倉 {results[0]['grid_spacing']*100:.2f}%[/]")
                console.print()

                if Confirm.ask("是否應用最佳參數?"):
                    best = results[0]

                    if raw not in self.config.symbols:
                        self.config.symbols[raw] = sym_config

                    self.config.symbols[raw].take_profit_spacing = best["take_profit_spacing"]
                    self.config.symbols[raw].grid_spacing = best["grid_spacing"]
                    self.config.save()

                    console.print(f"\n[green]已應用並保存: 止盈 {best['take_profit_spacing']*100:.2f}%, 補倉 {best['grid_spacing']*100:.2f}%[/]")

        Prompt.ask("\n按 Enter 繼續")

    def _show_backtest_result(self, result: dict):
        """顯示回測結果"""
        console.print("="*50)
        console.print("[bold cyan]回測結果[/]")
        console.print("="*50 + "\n")

        return_color = "green" if result["return_pct"] >= 0 else "red"

        table = Table(box=box.ROUNDED)
        table.add_column("指標", style="dim")
        table.add_column("值", justify="right")

        table.add_row("最終淨值", f"${result['final_equity']:.2f}")
        table.add_row("收益率", f"[{return_color}]{result['return_pct']*100:.2f}%[/]")
        table.add_row("最大回撤", f"[red]{result['max_drawdown']*100:.2f}%[/]")
        table.add_row("交易次數", str(result['trades_count']))
        table.add_row("勝率", f"{result['win_rate']*100:.1f}%")
        pf = result['profit_factor']
        table.add_row("盈虧比", f"{pf:.2f}" if pf != float('inf') else "∞")
        table.add_row("已實現盈虧", f"${result['realized_pnl']:.2f}")
        table.add_row("未實現盈虧", f"${result['unrealized_pnl']:.2f}")

        console.print(table)

    def setup_max_enhancement(self):
        """MAX 增強功能設定"""
        self.show_banner()
        console.print("[bold yellow]MAX 增強功能設定[/]\n")

        max_cfg = self.config.max_enhancement

        if max_cfg.all_enhancements_enabled:
            mode_status = "[bold green]增強模式[/] (學術模型啟用)"
        else:
            mode_status = "[bold cyan]純淨模式[/] (與 Pro 版相同)"
        console.print(f"[bold]當前模式:[/] {mode_status}\n")

        console.print("[bold]1. Funding Rate 偏向[/]")
        fr_active = max_cfg.is_feature_enabled('funding_rate')
        fr_status = "[green]啟用[/]" if fr_active else "[dim]停用[/]"
        console.print(f"   狀態: {fr_status}")
        console.print(f"   閾值: {max_cfg.funding_rate_threshold*100:.3f}% (超過才調整)")
        console.print(f"   偏向強度: {max_cfg.funding_rate_position_bias*100:.0f}%")
        console.print()

        console.print("[bold]2. GLFT 庫存控制[/]")
        glft_active = max_cfg.is_feature_enabled('glft')
        glft_status = "[green]啟用[/]" if glft_active else "[dim]停用[/]"
        console.print(f"   狀態: {glft_status}")
        console.print(f"   γ (風險厭惡): {max_cfg.gamma}")
        console.print(f"   目標庫存比: {max_cfg.inventory_target}")
        console.print()

        console.print("[bold]3. 動態網格範圍[/]")
        dg_active = max_cfg.is_feature_enabled('dynamic_grid')
        dg_status = "[green]啟用[/]" if dg_active else "[dim]停用[/]"
        console.print(f"   狀態: {dg_status}")
        console.print(f"   ATR 週期: {max_cfg.atr_period}")
        console.print(f"   ATR 乘數: {max_cfg.atr_multiplier}")
        console.print(f"   間距範圍: {max_cfg.min_spacing*100:.2f}% ~ {max_cfg.max_spacing*100:.2f}%")
        console.print()

        if not Confirm.ask("是否修改設定?"):
            return

        console.print("\n[bold yellow]── 模式選擇 ──[/]")
        console.print("[dim]純淨模式: 與 Pro 版完全相同，固定間距和數量[/]")
        console.print("[dim]增強模式: 啟用學術模型，動態調整間距和數量[/]")
        max_cfg.all_enhancements_enabled = Confirm.ask(
            "啟用增強模式?",
            default=max_cfg.all_enhancements_enabled
        )

        if not max_cfg.all_enhancements_enabled:
            self.config.save()
            console.print("\n[cyan]已切換到純淨模式，與 Pro 版行為相同[/]")
            Prompt.ask("按 Enter 繼續")
            return

        console.print("\n[bold cyan]── Funding Rate 偏向 ──[/]")
        max_cfg.funding_rate_enabled = Confirm.ask("啟用 Funding Rate 偏向?", default=max_cfg.funding_rate_enabled)
        if max_cfg.funding_rate_enabled:
            max_cfg.funding_rate_threshold = FloatPrompt.ask(
                f"閾值 (%) [當前: {max_cfg.funding_rate_threshold*100:.3f}]",
                default=max_cfg.funding_rate_threshold * 100
            ) / 100
            max_cfg.funding_rate_position_bias = FloatPrompt.ask(
                f"偏向強度 (%) [當前: {max_cfg.funding_rate_position_bias*100:.0f}]",
                default=max_cfg.funding_rate_position_bias * 100
            ) / 100

        console.print("\n[bold cyan]── GLFT 庫存控制 ──[/]")
        max_cfg.glft_enabled = Confirm.ask("啟用 GLFT 庫存控制?", default=max_cfg.glft_enabled)
        if max_cfg.glft_enabled:
            max_cfg.gamma = FloatPrompt.ask(
                f"γ 風險厭惡係數 (0.01-1.0) [當前: {max_cfg.gamma}]",
                default=max_cfg.gamma
            )

        console.print("\n[bold cyan]── 動態網格範圍 ──[/]")
        max_cfg.dynamic_grid_enabled = Confirm.ask("啟用動態網格?", default=max_cfg.dynamic_grid_enabled)
        if max_cfg.dynamic_grid_enabled:
            max_cfg.atr_period = IntPrompt.ask(
                f"ATR 週期 [當前: {max_cfg.atr_period}]",
                default=max_cfg.atr_period
            )
            max_cfg.atr_multiplier = FloatPrompt.ask(
                f"ATR 乘數 [當前: {max_cfg.atr_multiplier}]",
                default=max_cfg.atr_multiplier
            )
            max_cfg.min_spacing = FloatPrompt.ask(
                f"最小間距 (%) [當前: {max_cfg.min_spacing*100:.2f}]",
                default=max_cfg.min_spacing * 100
            ) / 100
            max_cfg.max_spacing = FloatPrompt.ask(
                f"最大間距 (%) [當前: {max_cfg.max_spacing*100:.2f}]",
                default=max_cfg.max_spacing * 100
            ) / 100

        self.config.save()
        console.print("[green]MAX 增強設定已保存[/]")

        if self._trading_active and self.bot:
            self.bot.config = self.config
            console.print("[cyan]✓ 配置已即時套用[/]")

        Prompt.ask("按 Enter 繼續")

    def setup_api(self):
        self.show_banner()
        console.print("[bold]API 設定[/]\n")

        if self.config.api_key:
            console.print(f"[dim]當前 API Key: {self.config.api_key[:8]}...{self.config.api_key[-4:]}[/]")
            if not Confirm.ask("是否重新設定?"):
                return

        self.config.api_key = Prompt.ask("API Key")
        self.config.api_secret = Prompt.ask("API Secret")
        self.config.save()

        console.print("[green]API 設定完成[/]")
        Prompt.ask("按 Enter 繼續")

    def setup_learning(self):
        """學習模組設定"""
        self.show_banner()
        console.print("[bold yellow]學習模組設定[/]\n")

        bandit = self.config.bandit
        leading = self.config.leading_indicator
        dgt = self.config.dgt

        console.print("[bold]1. UCB Bandit 參數優化器[/]")
        console.print("[dim]   基於 TradeBot 論文，自動學習最佳參數組合[/]")
        bandit_status = "[green]啟用[/]" if bandit.enabled else "[red]停用[/]"
        console.print(f"   狀態: {bandit_status}")
        console.print(f"   滑動窗口: {bandit.window_size} 筆交易")
        console.print(f"   探索係數: {bandit.exploration_factor}")
        console.print(f"   更新間隔: 每 {bandit.update_interval} 筆交易")
        console.print()

        console.print("[bold]2. 領先指標系統 (取代滯後 ATR)[/]")
        console.print("[dim]   OFI (訂單流) + Volume (成交量) + Spread (價差)[/]")
        leading_status = "[green]啟用[/]" if leading.enabled else "[red]停用[/]"
        console.print(f"   狀態: {leading_status}")
        console.print(f"   OFI 閾值: ±{leading.ofi_threshold}")
        console.print(f"   放量倍數: {leading.volume_surge_threshold}x")
        console.print(f"   價差倍數: {leading.spread_surge_threshold}x")
        console.print()

        console.print("[bold]3. DGT 動態邊界重置[/]")
        console.print("[dim]   基於 arXiv:2506.11921，價格突破邊界時自動重置網格[/]")
        dgt_status = "[green]啟用[/]" if dgt.enabled else "[red]停用[/]"
        console.print(f"   狀態: {dgt_status}")
        console.print(f"   邊界緩衝: {dgt.boundary_buffer*100:.1f}%")
        console.print(f"   利潤再投資: {dgt.profit_reinvest_ratio*100:.0f}%")
        console.print()

        if not Confirm.ask("是否修改設定?"):
            return

        console.print("\n[bold cyan]── UCB Bandit 設定 ──[/]")
        bandit.enabled = Confirm.ask("啟用 Bandit 參數學習?", default=bandit.enabled)
        if bandit.enabled:
            bandit.window_size = IntPrompt.ask(
                f"滑動窗口大小 [當前: {bandit.window_size}]",
                default=bandit.window_size
            )
            bandit.exploration_factor = FloatPrompt.ask(
                f"探索係數 (1.0-3.0) [當前: {bandit.exploration_factor}]",
                default=bandit.exploration_factor
            )
            bandit.update_interval = IntPrompt.ask(
                f"更新間隔 (筆交易) [當前: {bandit.update_interval}]",
                default=bandit.update_interval
            )

        console.print("\n[bold cyan]── 領先指標設定 ──[/]")
        console.print("[dim]領先指標預測波動，優先於滯後的 ATR[/]")
        leading.enabled = Confirm.ask("啟用領先指標?", default=leading.enabled)
        if leading.enabled:
            leading.ofi_enabled = Confirm.ask("  啟用 OFI (訂單流失衡)?", default=leading.ofi_enabled)
            leading.volume_enabled = Confirm.ask("  啟用成交量分析?", default=leading.volume_enabled)
            leading.spread_enabled = Confirm.ask("  啟用價差分析?", default=leading.spread_enabled)

            leading.ofi_threshold = FloatPrompt.ask(
                f"  OFI 觸發閾值 (0-1) [當前: {leading.ofi_threshold}]",
                default=leading.ofi_threshold
            )
            leading.volume_surge_threshold = FloatPrompt.ask(
                f"  放量倍數閾值 [當前: {leading.volume_surge_threshold}]",
                default=leading.volume_surge_threshold
            )
            leading.spread_surge_threshold = FloatPrompt.ask(
                f"  價差倍數閾值 [當前: {leading.spread_surge_threshold}]",
                default=leading.spread_surge_threshold
            )

        console.print("\n[bold cyan]── DGT 動態邊界 ──[/]")
        dgt.enabled = Confirm.ask("啟用 DGT 邊界重置?", default=dgt.enabled)
        if dgt.enabled:
            dgt.boundary_buffer = FloatPrompt.ask(
                f"邊界緩衝 (%) [當前: {dgt.boundary_buffer*100:.1f}]",
                default=dgt.boundary_buffer * 100
            ) / 100
            dgt.profit_reinvest_ratio = FloatPrompt.ask(
                f"利潤再投資比例 (%) [當前: {dgt.profit_reinvest_ratio*100:.0f}]",
                default=dgt.profit_reinvest_ratio * 100
            ) / 100

        self.config.save()
        console.print("\n[green]學習模組設定已保存[/]")

        if self._trading_active and self.bot:
            self.bot.config = self.config
            console.print("[cyan]✓ 配置已即時套用[/]")

        Prompt.ask("按 Enter 繼續")

    def setup_risk(self):
        """風控設定"""
        self.show_banner()
        console.print("[bold]風控設定 - 保證金追蹤止盈[/]\n")

        risk = self.config.risk

        console.print("[dim]當前設定:[/]")
        status = "[green]啟用[/]" if risk.enabled else "[red]停用[/]"
        console.print(f"  狀態: {status}")
        console.print(f"  保證金閾值: {risk.margin_threshold*100:.0f}%")
        console.print(f"  啟動追蹤: 浮盈 >= {risk.trailing_start_profit:.1f}U")
        console.print(f"  回撤觸發: max({risk.trailing_min_drawdown:.1f}U, 最高浮盈 × {risk.trailing_drawdown_pct*100:.0f}%)")
        console.print()

        if Confirm.ask("是否修改設定?"):
            risk.enabled = Confirm.ask("啟用追蹤止盈?", default=risk.enabled)

            if risk.enabled:
                risk.margin_threshold = FloatPrompt.ask(
                    f"保證金閾值 (%) [當前: {risk.margin_threshold*100:.0f}]",
                    default=risk.margin_threshold * 100
                ) / 100

                risk.trailing_start_profit = FloatPrompt.ask(
                    f"啟動追蹤閾值 (U) [當前: {risk.trailing_start_profit:.1f}]",
                    default=risk.trailing_start_profit
                )

                risk.trailing_drawdown_pct = FloatPrompt.ask(
                    f"回撤比例 (%) [當前: {risk.trailing_drawdown_pct*100:.0f}]",
                    default=risk.trailing_drawdown_pct * 100
                ) / 100

                risk.trailing_min_drawdown = FloatPrompt.ask(
                    f"最小回撤 (U) [當前: {risk.trailing_min_drawdown:.1f}]",
                    default=risk.trailing_min_drawdown
                )

            self.config.save()
            console.print("[green]風控設定已保存[/]")

            if self._trading_active and self.bot:
                self.bot.config = self.config
                console.print("[cyan]✓ 配置已即時套用[/]")

        Prompt.ask("按 Enter 繼續")

    def manage_symbols(self):
        while True:
            self.show_banner()
            console.print("[bold]交易對管理[/]\n")

            if self._trading_active:
                console.print("[dim yellow]● 交易運行中 - 修改參數會即時套用[/]\n")

            if self.config.symbols:
                table = Table(box=box.ROUNDED)
                table.add_column("#", style="dim")
                table.add_column("交易對", style="cyan")
                table.add_column("狀態")
                table.add_column("止盈", justify="right")
                table.add_column("補倉", justify="right")
                table.add_column("數量", justify="right")
                table.add_column("槓桿", justify="right")
                table.add_column("加倍", justify="right", style="yellow")
                table.add_column("裝死", justify="right", style="red")

                for i, cfg in enumerate(self.config.symbols.values(), 1):
                    status = "[green]啟用[/]" if cfg.enabled else "[dim]停用[/]"
                    table.add_row(
                        str(i),
                        cfg.symbol,
                        status,
                        f"{cfg.take_profit_spacing*100:.2f}%",
                        f"{cfg.grid_spacing*100:.2f}%",
                        str(cfg.initial_quantity),
                        f"{cfg.leverage}x",
                        f"×{cfg.limit_multiplier:.0f} ({cfg.position_limit:.0f})",
                        f"×{cfg.threshold_multiplier:.0f} ({cfg.position_threshold:.0f})"
                    )

                console.print(table)
                console.print()

            console.print("  [cyan]a[/] 新增交易對")
            console.print("  [cyan]e[/] 編輯交易對")
            console.print("  [cyan]d[/] 刪除交易對")
            console.print("  [cyan]t[/] 切換啟用/停用")
            console.print("  [cyan]0[/] 返回")
            console.print()

            choice = Prompt.ask("選擇", choices=["0", "a", "e", "d", "t"], default="0")

            if choice == "0":
                break
            elif choice == "a":
                self.add_symbol()
            elif choice == "e":
                self.edit_symbol()
            elif choice == "d":
                self.delete_symbol()
            elif choice == "t":
                self.toggle_symbol()

    def add_symbol(self):
        self.show_banner()
        console.print("[bold]新增交易對[/]\n")

        symbol_input = Prompt.ask("輸入交易對 (如 XRPUSDC)")
        raw, ccxt, coin, quote = normalize_symbol(symbol_input)

        if not raw:
            console.print("[red]無法識別的交易對格式[/]")
            Prompt.ask("按 Enter 繼續")
            return

        if raw in self.config.symbols:
            console.print(f"[yellow]{raw} 已存在[/]")
            Prompt.ask("按 Enter 繼續")
            return

        take_profit = FloatPrompt.ask("止盈間距 (%)", default=0.4) / 100
        grid_spacing = FloatPrompt.ask("補倉間距 (%)", default=0.6) / 100
        quantity = FloatPrompt.ask("每單數量", default=3.0)
        leverage = IntPrompt.ask("槓桿", default=20)

        console.print(f"\n[dim]持倉控制 (基於每單數量 {quantity} 自動計算)[/]")
        limit_mult = FloatPrompt.ask("加倍倍數 (幾單後止盈加倍)", default=5.0)
        threshold_mult = FloatPrompt.ask("裝死倍數 (幾單後停止補倉)", default=20.0)
        console.print(f"[dim]→ 止盈加倍閾值: {quantity * limit_mult:.1f}, 裝死閾值: {quantity * threshold_mult:.1f}[/]")

        self.config.symbols[raw] = SymbolConfig(
            symbol=raw,
            ccxt_symbol=ccxt,
            enabled=True,
            take_profit_spacing=take_profit,
            grid_spacing=grid_spacing,
            initial_quantity=quantity,
            leverage=leverage,
            limit_multiplier=limit_mult,
            threshold_multiplier=threshold_mult
        )

        self.config.save()
        console.print(f"[green]已新增 {raw}[/]")
        Prompt.ask("按 Enter 繼續")

    def edit_symbol(self):
        if not self.config.symbols:
            console.print("[yellow]沒有可編輯的交易對[/]")
            Prompt.ask("按 Enter 繼續")
            return

        symbols = list(self.config.symbols.keys())
        console.print("輸入序號編輯:")
        idx = IntPrompt.ask("序號", default=1) - 1

        if idx < 0 or idx >= len(symbols):
            console.print("[red]無效序號[/]")
            Prompt.ask("按 Enter 繼續")
            return

        key = symbols[idx]
        cfg = self.config.symbols[key]

        console.print(f"\n編輯 [cyan]{cfg.symbol}[/]")
        cfg.take_profit_spacing = FloatPrompt.ask(
            f"止盈間距 (%) [當前: {cfg.take_profit_spacing*100:.2f}]",
            default=cfg.take_profit_spacing * 100
        ) / 100
        cfg.grid_spacing = FloatPrompt.ask(
            f"補倉間距 (%) [當前: {cfg.grid_spacing*100:.2f}]",
            default=cfg.grid_spacing * 100
        ) / 100
        cfg.initial_quantity = FloatPrompt.ask(
            f"每單數量 [當前: {cfg.initial_quantity}]",
            default=cfg.initial_quantity
        )
        cfg.leverage = IntPrompt.ask(
            f"槓桿 [當前: {cfg.leverage}]",
            default=cfg.leverage
        )

        console.print(f"\n[dim]持倉控制 (基於每單數量 {cfg.initial_quantity} 自動計算)[/]")
        cfg.limit_multiplier = FloatPrompt.ask(
            f"加倍倍數 (幾單後止盈加倍) [當前: {cfg.limit_multiplier}]",
            default=cfg.limit_multiplier
        )
        cfg.threshold_multiplier = FloatPrompt.ask(
            f"裝死倍數 (幾單後停止補倉) [當前: {cfg.threshold_multiplier}]",
            default=cfg.threshold_multiplier
        )
        console.print(f"[dim]→ 止盈加倍閾值: {cfg.position_limit:.1f}, 裝死閾值: {cfg.position_threshold:.1f}[/]")

        self.config.save()
        console.print("[green]已更新[/]")

        if self._trading_active and self.bot:
            self.bot.config = self.config
            console.print("[cyan]✓ 配置已即時套用到運行中的交易[/]")

        Prompt.ask("按 Enter 繼續")

    def delete_symbol(self):
        if not self.config.symbols:
            console.print("[yellow]沒有可刪除的交易對[/]")
            Prompt.ask("按 Enter 繼續")
            return

        symbols = list(self.config.symbols.keys())
        console.print("輸入序號刪除:")
        idx = IntPrompt.ask("序號", default=1) - 1

        if idx < 0 or idx >= len(symbols):
            console.print("[red]無效序號[/]")
            Prompt.ask("按 Enter 繼續")
            return

        key = symbols[idx]
        if Confirm.ask(f"確定刪除 {key}?"):
            del self.config.symbols[key]
            self.config.save()
            console.print("[green]已刪除[/]")

        Prompt.ask("按 Enter 繼續")

    def toggle_symbol(self):
        if not self.config.symbols:
            console.print("[yellow]沒有交易對[/]")
            Prompt.ask("按 Enter 繼續")
            return

        symbols = list(self.config.symbols.keys())
        console.print("輸入序號切換:")
        idx = IntPrompt.ask("序號", default=1) - 1

        if idx < 0 or idx >= len(symbols):
            console.print("[red]無效序號[/]")
            Prompt.ask("按 Enter 繼續")
            return

        key = symbols[idx]
        cfg = self.config.symbols[key]
        cfg.enabled = not cfg.enabled
        self.config.save()

        status = "啟用" if cfg.enabled else "停用"
        console.print(f"[green]{key} 已{status}[/]")

        if self._trading_active:
            console.print("[yellow]注意: 交易對啟用/停用需要重啟交易才能生效[/]")

        Prompt.ask("按 Enter 繼續")

    def coin_selection_menu(self):
        """選幣分析菜單"""
        self.show_banner()
        console.print("[bold yellow]選幣分析[/]\n")

        if not self.config.api_key:
            console.print("[red]請先設定 API (選項 7)[/]")
            Prompt.ask("按 Enter 繼續")
            return

        # 選擇報價幣
        console.print("[bold]選擇報價幣:[/]\n")
        console.print("  [cyan]1[/] USDC")
        console.print("  [cyan]2[/] USDT")
        console.print()
        quote_choice = Prompt.ask("選擇", choices=["1", "2"], default="1")
        quote_currency = "USDC" if quote_choice == "1" else "USDT"

        # 選擇模式
        console.print(f"\n[bold]掃描模式 ({quote_currency}):[/]\n")
        console.print("  [cyan]1[/] 快速掃描 (振幅篩選，~2 分鐘)")
        console.print("  [cyan]2[/] 深度分析 (振幅 + 多維評分，~5 分鐘)")
        console.print()
        mode = Prompt.ask("選擇", choices=["1", "2"], default="1")

        top_n = IntPrompt.ask("顯示前 N 名", default=20)

        console.print(f"\n[bold cyan]掃描 {quote_currency} 永續合約中...[/]\n")

        # 在 asyncio 中執行掃描
        try:
            results = asyncio.run(self._run_coin_scan(quote_currency, top_n, mode == "2"))
        except Exception as e:
            console.print(f"[red]掃描失敗: {e}[/]")
            logger.error(f"選幣掃描錯誤: {e}", exc_info=True)
            Prompt.ask("按 Enter 繼續")
            return

        if not results:
            console.print("[yellow]沒有找到符合條件的交易對[/]")
            Prompt.ask("按 Enter 繼續")
            return

        # 顯示結果表格
        self._show_coin_selection_results(results, mode == "2")

        # 選擇加入交易
        console.print(f"\n[bold]加入交易列表[/]")
        console.print("[dim]輸入序號 (多個用逗號分隔，如 1,3,5)，直接 Enter 跳過[/]\n")

        selection = Prompt.ask("選擇序號", default="").strip()
        if not selection:
            Prompt.ask("按 Enter 繼續")
            return

        # 解析選擇
        try:
            indices = [int(x.strip()) - 1 for x in selection.split(",")]
        except ValueError:
            console.print("[red]格式錯誤，請輸入數字序號[/]")
            Prompt.ask("按 Enter 繼續")
            return

        added = []
        skipped = []
        for idx in indices:
            if idx < 0 or idx >= len(results):
                console.print(f"[yellow]序號 {idx + 1} 超出範圍，跳過[/]")
                continue

            sym_info = results[idx][0]  # SymbolInfo
            raw = sym_info.symbol
            ccxt_sym = sym_info.ccxt_symbol

            if raw in self.config.symbols:
                skipped.append(raw)
                continue

            # 用預設參數加入
            self.config.symbols[raw] = SymbolConfig(
                symbol=raw,
                ccxt_symbol=ccxt_sym,
                enabled=True,
                take_profit_spacing=0.004,
                grid_spacing=0.006,
                initial_quantity=3.0,
                leverage=20,
            )
            added.append(raw)

        if added:
            self.config.save()
            console.print(f"\n[green]已加入 {len(added)} 個交易對: {', '.join(added)}[/]")
            console.print("[dim]可到「管理交易對」調整參數[/]")

            if self._trading_active and self.bot:
                self.bot.config = self.config
                console.print("[cyan]✓ 配置已即時套用[/]")
                console.print("[yellow]注意: 新交易對需要重啟交易才能生效[/]")

        if skipped:
            console.print(f"[yellow]已跳過 (已存在): {', '.join(skipped)}[/]")

        Prompt.ask("\n按 Enter 繼續")

    async def _run_coin_scan(self, quote_currency: str, top_n: int, deep: bool):
        """執行選幣掃描 (async)"""
        import ccxt.async_support as ccxt_async

        exchange_id = self.config.exchange_id or "binance"
        exchange_cls = getattr(ccxt_async, exchange_id, ccxt_async.binance)

        exchange = exchange_cls({
            'apiKey': self.config.api_key,
            'secret': self.config.api_secret,
            'options': {'defaultType': 'swap'},
            'enableRateLimit': True,
        })
        if self.config.api_password:
            exchange.password = self.config.api_password

        try:
            # Step 1: 振幅掃描
            scanner = SymbolScanner()
            candidates = await scanner.scan_with_amplitude(
                exchange, quote_currency, top_n=top_n
            )

            if not candidates:
                return []

            # Step 2: 深度分析 (多維度評分)
            if deep and candidates:
                scorer = CoinScorer()
                symbols = [sym.ccxt_symbol for sym, _ in candidates]
                scores = await scorer.score_all(symbols, exchange)

                # 合併結果: [(SymbolInfo, AmplitudeStats, CoinScore), ...]
                score_map = {s.symbol: s for s in scores}
                results = []
                for sym, stats in candidates:
                    coin_score = score_map.get(sym.ccxt_symbol)
                    results.append((sym, stats, coin_score))

                # 按多維度評分排序 (有評分的)
                results.sort(
                    key=lambda x: x[2].final_score if x[2] else 0,
                    reverse=True
                )
                return results

            # 淺層: [(SymbolInfo, AmplitudeStats, None), ...]
            return [(sym, stats, None) for sym, stats in candidates]

        finally:
            await exchange.close()

    def _show_coin_selection_results(self, results, deep: bool):
        """顯示選幣結果表格"""
        table = Table(title="選幣結果", box=box.ROUNDED)
        table.add_column("#", style="dim", width=4)
        table.add_column("交易對", style="cyan")
        table.add_column("價格", justify="right")
        table.add_column("振幅", justify="right")
        table.add_column("趨勢", justify="right")
        table.add_column("24h量", justify="right")
        table.add_column("適合度", justify="right")

        if deep:
            table.add_column("總評分", justify="right", style="bold")
            table.add_column("均回", justify="right")
            table.add_column("Hurst", justify="right")

        # 標記已在交易列表的
        existing = set(self.config.symbols.keys())

        for i, item in enumerate(results, 1):
            sym_info = item[0]   # SymbolInfo
            stats = item[1]      # AmplitudeStats
            coin_score = item[2] if len(item) > 2 else None  # CoinScore or None

            # 基礎欄位
            name = sym_info.symbol
            if name in existing:
                name = f"[dim]{name} ✓[/]"

            price_str = f"${stats.last_price:.4f}" if stats.last_price < 1 else f"${stats.last_price:.2f}"
            vol_str = f"${stats.volume_24h/1e6:.0f}M"

            trend_val = stats.total_change
            trend_color = "green" if abs(trend_val) < 10 else ("yellow" if abs(trend_val) < 30 else "red")
            trend_str = f"[{trend_color}]{trend_val:+.1f}%[/]"

            suit_val = stats.grid_suitability
            suit_color = "green" if suit_val >= 70 else ("yellow" if suit_val >= 50 else "red")
            suit_str = f"[{suit_color}]{suit_val:.0f}[/]"

            row = [
                str(i), name, price_str,
                f"{stats.avg_amplitude:.1f}%",
                trend_str, vol_str, suit_str,
            ]

            if deep and coin_score:
                score_color = "green" if coin_score.final_score >= 70 else ("yellow" if coin_score.final_score >= 50 else "red")
                row.append(f"[{score_color}]{coin_score.final_score:.1f}[/]")
                row.append(f"{coin_score.mean_revert_score:.0f}")

                h = coin_score.hurst_exponent
                h_color = "green" if h < 0.45 else ("yellow" if h < 0.5 else "red")
                row.append(f"[{h_color}]{h:.3f}[/]")
            elif deep:
                row.extend(["—", "—", "—"])

            table.add_row(*row)

        console.print(table)

        # 圖例
        console.print("\n[dim]振幅: 30日平均日波動 | 趨勢: 30日累計漲跌幅 | 適合度: 振幅+趨勢綜合分[/]")
        if deep:
            console.print("[dim]總評分: 多維度加權 (均回40% 流動20% 波動15% 動量15% 穩定10%)[/]")
            console.print("[dim]Hurst: <0.45 均值回歸(適合網格) | 0.5 隨機 | >0.5 趨勢(不適合)[/]")
        console.print("[dim]✓ = 已在交易列表中[/]")

    def start_trading(self):
        """啟動背景交易"""
        if not self.config.api_key:
            console.print("[red]請先設定 API[/]")
            Prompt.ask("按 Enter 繼續")
            return

        enabled = [s for s in self.config.symbols.values() if s.enabled]
        if not enabled:
            console.print("[red]沒有啟用的交易對[/]")
            Prompt.ask("按 Enter 繼續")
            return

        if self._trading_active:
            console.print("[yellow]交易已在運行中[/]")
            Prompt.ask("按 Enter 繼續")
            return

        console.print("[bold]啟動 MAX 網格交易...[/]\n")

        self.bot = MaxGridBot(self.config)

        def run_bot_thread():
            self.bot_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.bot_loop)
            try:
                self.bot_loop.run_until_complete(self.bot.run())
            except Exception as e:
                logger.error(f"Bot 運行錯誤: {e}")
            finally:
                self.bot_loop.close()
                self._trading_active = False

        self.bot_thread = threading.Thread(target=run_bot_thread, daemon=True)
        self.bot_thread.start()

        with console.status("[bold cyan]連接交易所...[/]"):
            for _ in range(100):
                if self.bot.state.running:
                    break
                time.sleep(0.1)

        if self.bot.state.running:
            self._trading_active = True
            console.print("[bold green]✓ 交易已在背景啟動！[/]\n")
            console.print("[dim]可以返回主選單管理設定，交易會持續運行[/]")
            console.print("[dim]選擇「1」查看交易面板，「s」停止交易[/]\n")
        else:
            if self.bot_thread and self.bot_thread.is_alive():
                console.print("[yellow]初始化較慢，請稍等...[/]")
                for _ in range(100):
                    if self.bot.state.running:
                        break
                    time.sleep(0.1)

                if self.bot.state.running:
                    self._trading_active = True
                    console.print("[bold green]✓ 交易已在背景啟動！[/]\n")
                else:
                    console.print("[red]Bot 啟動超時，請檢查網絡連接[/]")
                    self.bot = None
            else:
                console.print("[red]Bot 啟動失敗，請檢查日誌[/]")
                self.bot = None

        Prompt.ask("按 Enter 繼續")

    def stop_trading(self):
        """停止背景交易"""
        if not self._trading_active or not self.bot:
            console.print("[yellow]沒有運行中的交易[/]")
            return

        console.print("[bold yellow]正在停止交易...[/]")

        if self.bot_loop and self.bot_loop.is_running():
            asyncio.run_coroutine_threadsafe(self.bot.stop(), self.bot_loop)

        if self.bot_thread and self.bot_thread.is_alive():
            self.bot_thread.join(timeout=5)

        self._trading_active = False
        self.bot = None
        self.bot_thread = None
        self.bot_loop = None

        console.print("[green]✓ 交易已停止[/]")
        Prompt.ask("按 Enter 繼續")

    def view_trading_panel(self):
        """查看交易面板"""
        if not self._trading_active or not self.bot:
            console.print("[yellow]沒有運行中的交易[/]")
            Prompt.ask("按 Enter 繼續")
            return

        ui = TerminalUI(self.config, self.bot.state, self.bot)

        console.print("[dim]按 Ctrl+C 返回主選單 (交易會繼續運行)[/]\n")

        try:
            with Live(ui.create_layout(), console=console, refresh_per_second=2) as live:
                while self._trading_active and self.bot.state.running:
                    live.update(ui.create_layout())
                    time.sleep(0.5)
        except KeyboardInterrupt:
            pass

        console.print("\n[dim]返回主選單...[/]")

    def reload_config(self):
        """重新載入配置並套用到運行中的 bot"""
        old_api_key = self.config.api_key if self.config else ""
        old_api_secret = self.config.api_secret if self.config else ""

        self.config = GlobalConfig.load()

        if not self.config.api_key and old_api_key:
            self.config.api_key = old_api_key
        if not self.config.api_secret and old_api_secret:
            self.config.api_secret = old_api_secret

        if self._trading_active and self.bot:
            self.bot.config = self.config
            console.print("[green]✓ 配置已重新載入[/]")


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║                              程式入口                                          ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

if __name__ == "__main__":
    menu = MainMenu()
    menu.main_menu()
