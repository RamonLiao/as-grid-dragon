"""接線測試：bot 的前置 gate 與 DecisionInputs 都吃 config.requote_threshold_factor。
防假旋鈕：config 寫 1.0 而 gate 仍用 0.5 → 這裡必紅。"""
from unittest.mock import AsyncMock, patch

import pytest

from grid_engine.bot import MaxGridBot
from grid_engine.config import GlobalConfig, SymbolConfig
from grid_engine.decision import decide as real_decide

SYMBOL = "XRP/USDC:USDC"


def _make_bot():
    """沿用 tests/test_characterization_grid.py 的最小 bot fixture 模式：
    真實 GlobalConfig + 單一 SymbolConfig，bandit 關閉避免 _grid_step 覆寫
    grid_spacing（bandit 預設 enabled=True 會蓋掉本測試指定的 0.003）。
    order_executor 的 place_order/cancel_orders_for_side mock 掉，避免真的打交易所。
    """
    cfg = GlobalConfig()
    cfg.symbols = {SYMBOL: SymbolConfig(
        symbol="XRPUSDC", ccxt_symbol=SYMBOL, enabled=True,
        take_profit_spacing=0.003, grid_spacing=0.003, initial_quantity=0.02,
        limit_multiplier=5.0, threshold_multiplier=20.0,
    )}
    cfg.bandit.enabled = False
    bot = MaxGridBot(cfg)
    bot.order_executor.place_order = AsyncMock()
    bot.order_executor.cancel_orders_for_side = AsyncMock()
    return bot


@pytest.fixture
def minimal_bot():
    return _make_bot()


def test_gate_threshold_follows_config(minimal_bot):
    minimal_bot.config.requote_threshold_factor = 1.0
    sym_cfg = next(iter(minimal_bot.config.symbols.values()))   # grid_spacing=0.003
    state = minimal_bot.state.symbols[sym_cfg.ccxt_symbol]
    state.buy_long_orders = state.sell_long_orders = 0.02
    state.last_grid_price_long = 100.0
    state.latest_price = 100.2          # 偏離 0.2%：>0.15%（舊）但 <0.3%（新）
    assert minimal_bot._should_adjust_grid(sym_cfg, state, "long") is False
    minimal_bot.config.requote_threshold_factor = 0.5
    assert minimal_bot._should_adjust_grid(sym_cfg, state, "long") is True


@pytest.mark.asyncio
async def test_decision_inputs_carry_factor(minimal_bot):
    """_grid_step 建構的 DecisionInputs.requote_threshold_factor == config 值。
    做法：patch grid_engine.bot.decide 捕獲 inputs，跑一次 _grid_step。"""
    minimal_bot.config.requote_threshold_factor = 0.8
    sym_cfg = next(iter(minimal_bot.config.symbols.values()))
    state = minimal_bot.state.symbols[sym_cfg.ccxt_symbol]
    state.latest_price = 100.0
    state.long_position = 1.0
    state.short_position = 0.0
    # buy_long_orders<=0 → _should_adjust_grid 無條件回 True（不靠 deviation 湊出 need_long）
    state.buy_long_orders = 0.0
    state.sell_long_orders = 0.02
    state.last_grid_price_long = 100.0

    captured = {}

    def fake_decide(inputs):
        captured["inputs"] = inputs
        return real_decide(inputs)

    with patch("grid_engine.bot.decide", side_effect=fake_decide):
        await minimal_bot._grid_step(sym_cfg.ccxt_symbol, sym_cfg)

    assert "inputs" in captured
    assert captured["inputs"].requote_threshold_factor == 0.8
