"""純函數網格決策層：無 I/O、不寫任何物件。實盤與回測共用。
搬移自 grid_engine/bot.py 的 _get_dynamic_spacing / _get_adjusted_quantity /
_should_adjust_grid / _place_grid 決策半段 + strategy.py 純計算（含 bug-for-bug）。"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class EnhancementSnapshot:
    """manager 在本 tick 的『已解析』輸出快照，由 snapshot.build_snapshot() 產生。
    只放需要 manager 狀態的值；純計算（GLFT 數量/裝死價/加倍）不進此處。"""
    dynamic_take_profit: float          # leading+ATR 解析後的最終止盈間距
    dynamic_grid_spacing: float         # leading+ATR 解析後的最終補倉間距
    funding_long_bias: float            # FundingRateManager.get_position_bias()[0]
    funding_short_bias: float           # [1]
    # 顯示欄位（面板用；不影響決策，供 bot 寫回 sym_state）：
    leading_ofi: float = 0.0
    leading_volume_ratio: float = 1.0
    leading_spread_ratio: float = 1.0
    leading_signals: tuple = ()


@dataclass(frozen=True)
class DecisionInputs:
    price: float
    long_position: float
    short_position: float
    buy_long_orders: float
    sell_long_orders: float
    buy_short_orders: float
    sell_short_orders: float
    last_grid_price_long: float
    last_grid_price_short: float
    long_dead_mode: bool
    short_dead_mode: bool
    grid_spacing: float                 # base（bandit 覆寫後），供 should_adjust 偏離門檻
    take_profit_spacing: float
    initial_quantity: float
    position_threshold: float
    position_limit: float
    glft_enabled: bool                  # = max_enhancement.is_feature_enabled('glft')
    gamma: float
    enh: EnhancementSnapshot
    requote_threshold_factor: float = 0.5   # 追價門檻 = grid_spacing * factor；0.5 為歷史 hardcode


@dataclass(frozen=True)
class OrderIntent:
    side: str                # 'buy' | 'sell'
    position_side: str       # 'long' | 'short'
    price: float
    quantity: float
    reduce_only: bool


@dataclass(frozen=True)
class SideDecision:
    should_adjust: bool
    enter_dead_mode: bool
    exit_dead_mode: bool
    cancel_side: bool
    orders: tuple
    new_anchor_price: Optional[float]
    dynamic_tp: float
    dynamic_gs: float
    display: dict = field(default_factory=dict)


@dataclass(frozen=True)
class GridDecision:
    long: SideDecision
    short: SideDecision


_FALLBACK_LONG = 1.05
_FALLBACK_SHORT = 0.95
_DEAD_DIVISOR = 100


def is_dead_mode(position: float, threshold: float) -> bool:
    return position > threshold


def dead_mode_price(base_price, my_position, opposite_position, side):
    if opposite_position > 0:
        r = (my_position / opposite_position) / _DEAD_DIVISOR + 1
        return base_price * r if side == "long" else base_price / r
    return base_price * (_FALLBACK_LONG if side == "long" else _FALLBACK_SHORT)


def grid_prices(base_price, take_profit_spacing, grid_spacing, side):
    if side == "long":
        return base_price * (1 + take_profit_spacing), base_price * (1 - grid_spacing)
    return base_price * (1 - take_profit_spacing), base_price * (1 + grid_spacing)


def tp_quantity(base_qty, my_position, opposite_position, position_limit, position_threshold):
    if my_position > position_limit or opposite_position >= position_threshold:
        return base_qty * 2
    return base_qty


def inventory_ratio(long_pos, short_pos):
    total = long_pos + short_pos
    return 0.0 if total <= 0 else (long_pos - short_pos) / total


def glft_quantity(base_qty, side, long_pos, short_pos, glft_enabled, gamma):
    if not glft_enabled:
        return base_qty
    inv = inventory_ratio(long_pos, short_pos)
    adjust = 1.0 - inv * gamma if side == "long" else 1.0 + inv * gamma
    adjust = max(0.5, min(1.5, adjust))
    return base_qty * adjust


def should_adjust(inputs, side):
    if side == "long":
        buy_o, sell_o, anchor = inputs.buy_long_orders, inputs.sell_long_orders, inputs.last_grid_price_long
    else:
        buy_o, sell_o, anchor = inputs.buy_short_orders, inputs.sell_short_orders, inputs.last_grid_price_short
    if buy_o <= 0 or sell_o <= 0:
        return True
    if anchor > 0:
        deviation = abs(inputs.price - anchor) / anchor
        return deviation >= inputs.grid_spacing * inputs.requote_threshold_factor
    return True


def compute_quantity(inputs, side, is_take_profit):
    my_pos = inputs.long_position if side == "long" else inputs.short_position
    opp_pos = inputs.short_position if side == "long" else inputs.long_position
    q = inputs.initial_quantity
    if is_take_profit:
        q = tp_quantity(q, my_pos, opp_pos, inputs.position_limit, inputs.position_threshold)
    else:
        q = glft_quantity(q, side, inputs.long_position, inputs.short_position,
                          inputs.glft_enabled, inputs.gamma)
    bias = inputs.enh.funding_long_bias if side == "long" else inputs.enh.funding_short_bias
    q *= bias
    return max(inputs.initial_quantity * 0.5, q)


def _decide_side(inputs, side):
    tp_sp, gs_sp = inputs.enh.dynamic_take_profit, inputs.enh.dynamic_grid_spacing
    display = {
        "leading_ofi": inputs.enh.leading_ofi,
        "leading_volume_ratio": inputs.enh.leading_volume_ratio,
        "leading_spread_ratio": inputs.enh.leading_spread_ratio,
        "leading_signals": list(inputs.enh.leading_signals),
        "inventory_ratio": inventory_ratio(inputs.long_position, inputs.short_position),
        "dynamic_take_profit": tp_sp,
        "dynamic_grid_spacing": gs_sp,
    }
    if not should_adjust(inputs, side):
        return SideDecision(False, False, False, False, (), None, tp_sp, gs_sp, display)

    if side == "long":
        my_pos, opp_pos = inputs.long_position, inputs.short_position
        dead_flag, pending_tp = inputs.long_dead_mode, inputs.sell_long_orders
    else:
        my_pos, opp_pos = inputs.short_position, inputs.long_position
        dead_flag, pending_tp = inputs.short_dead_mode, inputs.buy_short_orders

    price = inputs.price
    orders = []
    enter_dead = exit_dead = cancel = False

    if is_dead_mode(my_pos, inputs.position_threshold):
        entering = not dead_flag
        if entering:
            # 接管止盈單所有權：正常模式的殘留單價格由 grid_prices 算出，與裝死
            # 模式的失衡比例無關。留著它會讓 pending_tp 恆 > 0，下面那張特殊
            # 止盈永遠掛不出去，倉位永遠降不回 threshold 以下 → 該側永久停擺。
            enter_dead = True
            cancel = True
        if entering or pending_tp <= 0:
            special = dead_mode_price(price, my_pos, opp_pos, side)
            tp_qty = compute_quantity(inputs, side, True)
            o_side = "sell" if side == "long" else "buy"
            orders.append(OrderIntent(o_side, side, special, tp_qty, True))
    else:
        if dead_flag:
            exit_dead = True
        cancel = True
        tp_price, entry_price = grid_prices(price, tp_sp, gs_sp, side)
        tp_qty = compute_quantity(inputs, side, True)
        base_qty = compute_quantity(inputs, side, False)
        if my_pos > 0:
            o_side = "sell" if side == "long" else "buy"
            orders.append(OrderIntent(o_side, side, tp_price, tp_qty, True))
        e_side = "buy" if side == "long" else "sell"
        orders.append(OrderIntent(e_side, side, entry_price, base_qty, False))

    return SideDecision(True, enter_dead, exit_dead, cancel, tuple(orders),
                        price, tp_sp, gs_sp, display)


def decide(inputs):
    return GridDecision(long=_decide_side(inputs, "long"),
                        short=_decide_side(inputs, "short"))
