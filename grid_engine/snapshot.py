"""共享快照收集：呼叫 manager 實例組出 EnhancementSnapshot。
不純（讀 manager 狀態、get_signals 有 append 副作用），但 bot 與 backtester 共用同一份，
保證 manager 呼叫序列一致——這是回測/實盤等價的關鍵，別在兩邊各寫一份。

呼叫序列逐字對照現行 grid_engine/bot.py:_get_dynamic_spacing（bot.py:450-515）：
1. leading.enabled -> get_signals(sym)                              (bot.py:464)
2. should_pause_trading(sym)                                        (bot.py:471)
   pause -> base_tp*=2, base_gs*=2, reason=f"暫停:{pause_reason}"    (bot.py:472-476)
   elif signals -> get_spacing_adjustment(sym, base_gs)             (bot.py:477-484)
3. not reason or reason=="正常" -> dynamic_grid_manager.get_dynamic_spacing(...)
   （條件呼叫，維持 calculate_atr 快取副作用時機）                    (bot.py:487-496)
   else -> tp/gs = base_tp/base_gs

funding.get_position_bias 不在 bot.py:_get_dynamic_spacing 序列內（現行是在
_get_adjusted_quantity，bot.py:547-550，每次算數量時各呼叫一次）。這裡併入單一
快照是 Task 3 decision.py 既定設計（EnhancementSnapshot 攜帶 funding bias），
Task 5 接線時 bot 側只會呼叫一次而非現行的多次——此為刻意行為變更，非序列對照範圍。
"""
from dataclasses import dataclass
from typing import Optional

from .decision import EnhancementSnapshot, inventory_ratio  # noqa: F401  (re-export 供下游使用)


@dataclass
class ManagerBundle:
    leading_indicator: object
    dynamic_grid_manager: object
    glft_controller: object
    funding_manager: Optional[object]
    max_enhancement: object
    leading_enabled: bool


def build_snapshot(bundle, ccxt_symbol, base_tp, base_gs):
    max_cfg = bundle.max_enhancement
    ofi = vol_ratio = spread_ratio = None
    signals = []
    leading_reason = ""

    if bundle.leading_enabled:
        signals, values = bundle.leading_indicator.get_signals(ccxt_symbol)  # bot.py:464
        ofi = values.get("ofi", 0)
        vol_ratio = values.get("volume_ratio", 1.0)
        spread_ratio = values.get("spread_ratio", 1.0)

        should_pause, pause_reason = bundle.leading_indicator.should_pause_trading(ccxt_symbol)  # 471
        if should_pause:
            base_tp *= 2.0
            base_gs *= 2.0
            leading_reason = f"暫停:{pause_reason}"
        elif signals:
            adjusted, leading_reason = bundle.leading_indicator.get_spacing_adjustment(  # 478
                ccxt_symbol, base_gs)
            if adjusted != base_gs:
                ratio = adjusted / base_gs
                base_gs = adjusted
                base_tp *= ratio

    if not leading_reason or leading_reason == "正常":
        tp, gs = bundle.dynamic_grid_manager.get_dynamic_spacing(  # 488（條件呼叫，維持 ATR 快取時機）
            ccxt_symbol, base_tp, base_gs, max_cfg)
    else:
        tp, gs = base_tp, base_gs

    if bundle.funding_manager is not None:
        long_bias, short_bias = bundle.funding_manager.get_position_bias(ccxt_symbol, max_cfg)
    else:
        long_bias, short_bias = 1.0, 1.0

    return EnhancementSnapshot(
        dynamic_take_profit=tp,
        dynamic_grid_spacing=gs,
        funding_long_bias=long_bias,
        funding_short_bias=short_bias,
        leading_ofi=ofi if ofi is not None else 0.0,
        leading_volume_ratio=vol_ratio if vol_ratio is not None else 1.0,
        leading_spread_ratio=spread_ratio if spread_ratio is not None else 1.0,
        leading_signals=tuple(signals),
    )
