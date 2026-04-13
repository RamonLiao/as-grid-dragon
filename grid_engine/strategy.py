"""
網格策略核心 (統一回測/實盤邏輯)
"""

from typing import Tuple


class GridStrategy:
    """
    網格策略核心邏輯 - 統一回測與實盤

    此類提取所有策略計算邏輯，確保回測與實盤行為一致。
    不包含任何 I/O 操作（下單、日誌等），只負責純計算。

    使用方式:
    - 回測: BacktestManager 調用靜態方法計算價格/數量
    - 實盤: MaxGridBot 調用相同方法，確保邏輯一致
    """

    # ═══════════════════════════════════════════════════════════════════════════
    # 常量定義 - 集中管理魔術數字
    # ═══════════════════════════════════════════════════════════════════════════
    DEAD_MODE_FALLBACK_LONG = 1.05    # 多頭裝死模式無對手倉時的止盈比例
    DEAD_MODE_FALLBACK_SHORT = 0.95   # 空頭裝死模式無對手倉時的止盈比例
    DEAD_MODE_DIVISOR = 100           # 裝死模式計算除數 (持倉比/100)

    @staticmethod
    def is_dead_mode(position: float, threshold: float) -> bool:
        """判斷是否進入裝死模式"""
        return position > threshold

    @staticmethod
    def calculate_dead_mode_price(
        base_price: float,
        my_position: float,
        opposite_position: float,
        side: str
    ) -> float:
        """計算裝死模式的特殊止盈價格"""
        if opposite_position > 0:
            r = (my_position / opposite_position) / GridStrategy.DEAD_MODE_DIVISOR + 1
            if side == 'long':
                return base_price * r
            else:
                return base_price / r
        else:
            if side == 'long':
                return base_price * GridStrategy.DEAD_MODE_FALLBACK_LONG
            else:
                return base_price * GridStrategy.DEAD_MODE_FALLBACK_SHORT

    @staticmethod
    def calculate_tp_quantity(
        base_qty: float,
        my_position: float,
        opposite_position: float,
        position_limit: float,
        position_threshold: float
    ) -> float:
        """計算止盈數量"""
        if my_position > position_limit or opposite_position >= position_threshold:
            return base_qty * 2
        return base_qty

    @staticmethod
    def calculate_grid_prices(
        base_price: float,
        take_profit_spacing: float,
        grid_spacing: float,
        side: str
    ) -> Tuple[float, float]:
        """計算正常模式的網格價格 → (止盈價格, 補倉價格)"""
        if side == 'long':
            tp_price = base_price * (1 + take_profit_spacing)
            entry_price = base_price * (1 - grid_spacing)
        else:
            tp_price = base_price * (1 - take_profit_spacing)
            entry_price = base_price * (1 + grid_spacing)

        return tp_price, entry_price

    @staticmethod
    def get_grid_decision(
        price: float,
        my_position: float,
        opposite_position: float,
        position_threshold: float,
        position_limit: float,
        base_qty: float,
        take_profit_spacing: float,
        grid_spacing: float,
        side: str
    ) -> dict:
        """獲取完整的網格決策 (主要入口方法)"""
        dead_mode = GridStrategy.is_dead_mode(my_position, position_threshold)

        tp_qty = GridStrategy.calculate_tp_quantity(
            base_qty, my_position, opposite_position,
            position_limit, position_threshold
        )

        if dead_mode:
            tp_price = GridStrategy.calculate_dead_mode_price(
                price, my_position, opposite_position, side
            )
            return {
                'dead_mode': True,
                'tp_price': tp_price,
                'entry_price': None,
                'tp_qty': tp_qty,
                'entry_qty': 0,
            }
        else:
            tp_price, entry_price = GridStrategy.calculate_grid_prices(
                price, take_profit_spacing, grid_spacing, side
            )
            return {
                'dead_mode': False,
                'tp_price': tp_price,
                'entry_price': entry_price,
                'tp_qty': tp_qty,
                'entry_qty': base_qty,
            }
