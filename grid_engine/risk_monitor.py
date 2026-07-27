"""風控組件：保證金追蹤止盈 / 雙向減倉 / 保證金警報（含冷卻）。行為原樣搬移。"""
import time

from .utils import logger
from .config import SymbolConfig
from .state import SymbolState

# 風控警報冷卻秒數預設值（可由 config telegram_risk_alert_cooldown 覆寫）
RISK_ALERT_COOLDOWN = 300


class RiskMonitor:
    def __init__(self, config, state, order_executor, notifier):
        self.config = config
        self.state = state
        self.order_executor = order_executor
        self.notifier = notifier
        self.last_risk_alert_time = 0.0

    async def check_trailing_stop(self):
        """保證金追蹤止盈邏輯"""
        risk = self.config.risk

        if not risk.enabled:
            return

        if self.state.margin_usage < risk.margin_threshold:
            self.state.trailing_active.clear()
            self.state.peak_pnl.clear()
            return

        for sym_config in self.config.symbols.values():
            if not sym_config.enabled:
                continue

            ccxt_symbol = sym_config.ccxt_symbol
            sym_state = self.state.symbols.get(ccxt_symbol)
            if not sym_state:
                continue

            current_pnl = sym_state.unrealized_pnl

            if self.state.trailing_active.get(ccxt_symbol, False):
                peak = self.state.peak_pnl.get(ccxt_symbol, 0)
                if current_pnl > peak:
                    self.state.peak_pnl[ccxt_symbol] = current_pnl
                    logger.info(f"[追蹤止盈] {sym_config.symbol} 新高: {current_pnl:.2f}U")

                peak = self.state.peak_pnl.get(ccxt_symbol, 0)
                drawdown = peak - current_pnl

                trigger = max(risk.trailing_min_drawdown, peak * risk.trailing_drawdown_pct)

                if drawdown >= trigger and peak > 0:
                    logger.info(f"[追蹤止盈] {sym_config.symbol} 觸發! 最高:{peak:.2f}, 當前:{current_pnl:.2f}, 回撤:{drawdown:.2f}")
                    await self.order_executor.close_symbol_positions(ccxt_symbol, sym_config)
                    self.state.trailing_active[ccxt_symbol] = False
                    self.state.peak_pnl[ccxt_symbol] = 0

            else:
                if current_pnl >= risk.trailing_start_profit:
                    self.state.trailing_active[ccxt_symbol] = True
                    self.state.peak_pnl[ccxt_symbol] = current_pnl
                    logger.info(f"[追蹤止盈] {sym_config.symbol} 開始追蹤! 浮盈: {current_pnl:.2f}U")

    async def check_and_reduce_positions(self, sym_config: SymbolConfig, sym_state: SymbolState):
        """檢查並減倉"""
        REDUCE_COOLDOWN = 60

        ccxt_symbol = sym_config.ccxt_symbol
        local_threshold = sym_config.position_threshold * 0.8
        reduce_qty = sym_config.position_threshold * 0.1

        last_reduce = self.state.last_reduce_time.get(ccxt_symbol, 0)
        if time.time() - last_reduce < REDUCE_COOLDOWN:
            return

        if sym_state.long_position >= local_threshold and sym_state.short_position >= local_threshold:
            # 大側多減 min(reduce_qty, gap)：讓「|delta| 永不增加、永不變號」成為
            # 不變式而非案例分析（spec §3.2）。夾到 gap 也順帶消掉浮點相等比較——
            # gap == 0 時 extra == 0，自動退回雙側等量減倉（= 舊行為）。
            gap = abs(sym_state.long_position - sym_state.short_position)
            extra = min(reduce_qty, gap)
            long_qty = reduce_qty + (extra if sym_state.long_position > sym_state.short_position else 0.0)
            short_qty = reduce_qty + (extra if sym_state.short_position > sym_state.long_position else 0.0)

            logger.info(
                f"[風控] {sym_config.symbol} 多空持倉均超過 {local_threshold}，開始雙向減倉"
                f"（多 {long_qty} / 空 {short_qty}，gap={gap}）"
            )

            if sym_state.long_position > 0:
                await self.order_executor.place_order(ccxt_symbol, 'sell', 0, long_qty, True, 'long', 'market')
                logger.info(f"[風控] {sym_config.symbol} 市價平多 {long_qty}")

            if sym_state.short_position > 0:
                await self.order_executor.place_order(ccxt_symbol, 'buy', 0, short_qty, True, 'short', 'market')
                logger.info(f"[風控] {sym_config.symbol} 市價平空 {short_qty}")

            self.state.last_reduce_time[ccxt_symbol] = time.time()

    async def check_risk_and_notify(self):
        """檢查風控狀態並通知"""
        if not self.notifier.enabled or not self.config.risk.enabled:
            return
        if not getattr(self.config, "telegram_risk_alert_enabled", True):
            return
        if self.state.margin_usage > self.config.risk.margin_threshold:
            # 冷卻：避免每個 ticker tick 重複轟炸 Telegram
            cooldown = getattr(self.config, "telegram_risk_alert_cooldown", RISK_ALERT_COOLDOWN)
            if time.time() - self.last_risk_alert_time < cooldown:
                return
            self.last_risk_alert_time = time.time()
            alert = f"保證金使用率過高: {self.state.margin_usage:.1%} (閾值: {self.config.risk.margin_threshold:.1%})"
            await self.notifier.notify_risk_alert(alert)
