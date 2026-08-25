"""G-0c3：bandit.enabled=true 時，config 的 grid_spacing/take_profit_spacing 不生效。

bot.py:355-358 在每個 tick 無條件用 bandit arm 覆寫這兩個欄位（不需要
all_enhancements_enabled）。生產 decisions.jsonl 60001 筆實測：實盤間距恆為
0.003/0.003（arm 0），而 config 寫的是 0.006/0.004 —— 從未生效。

⇒ 任何「照 config 建 Config 跑回測」的做法，測的都不是實盤策略。
   實驗期間必須 bandit.enabled=false 並顯式設定受測間距。
見 spec G5 / G5-bis。

注意：fixture 的 CONFIG_GS/CONFIG_TP 刻意選 0.009/0.007，
不等於 UCBBanditOptimizer.DEFAULT_ARMS（grid_engine/enhancements.py:173-184）
裡任何一組值。若用 0.006/0.004，會恰好撞上 cold-start arm 4
（gamma=0.10, grid_spacing=0.006, take_profit_spacing=0.004），
覆寫後 sc.grid_spacing 仍是 0.006，`!= CONFIG_GS` 斷言會假綠失去鑑別力。

鑑別力論證：test_bandit_enabled_overwrites_config_spacing 與
test_bandit_disabled_preserves_config_spacing 是一組負向對照 ——
同一個 _grid_step 呼叫，唯一差異是 bandit.enabled，結果卻不同
（True 時 config 值被覆寫成 arm 值；False 時 config 值原封不動）。
這證明覆寫行為確實由 bandit.enabled 這個旗標驅動，而非測試 fixture
或 _grid_step 其他邏輯的副作用。若日後有人刪掉 bot.py:355-358，
前者的 `sc.grid_spacing != CONFIG_GS` 斷言會立刻轉紅。

不 mutate 產品碼驗證鑑別力的理由：grid_engine/bot.py 是正在跑的
生產交易引擎原始碼（操作真實資金），mutate 期間若引擎崩潰重啟，
會載入被改寫、失去 bandit 覆寫的版本。上述負向對照本身已構成完整
鑑別力證明，不需要承擔這個風險。
"""
import pytest
from unittest.mock import AsyncMock

from grid_engine.bot import MaxGridBot
from grid_engine.config import GlobalConfig, SymbolConfig
from grid_engine.enhancements import MaxEnhancement
from grid_engine import clock

SYMBOL = "XRP/USDC:USDC"

CONFIG_GS = 0.009
CONFIG_TP = 0.007


def _make_bot(bandit_enabled: bool):
    cfg = GlobalConfig()
    cfg.symbols = {SYMBOL: SymbolConfig(
        symbol="XRPUSDC", ccxt_symbol=SYMBOL, enabled=True,
        take_profit_spacing=CONFIG_TP, grid_spacing=CONFIG_GS, initial_quantity=3,
        limit_multiplier=5.0, threshold_multiplier=20.0,
    )}
    cfg.max_enhancement = MaxEnhancement()
    cfg.bandit.enabled = bandit_enabled
    bot = MaxGridBot(cfg)
    bot.order_executor.place_order = AsyncMock()
    bot.order_executor.cancel_orders_for_side = AsyncMock()
    # 封鎖下單路徑：本測試只關心 config 欄位有沒有被覆寫
    bot.order_executor.is_blocked = lambda _s: True
    st = bot.state.symbols[SYMBOL]
    st.latest_price = 2.5
    st.best_bid = 2.5
    st.best_ask = 2.5
    st.quote_at = clock.guard_now()  # 價格時效守衛：模擬「剛收到 ticker」
    st.long_position = 0
    st.short_position = 0
    return bot


@pytest.mark.asyncio
async def test_bandit_enabled_overwrites_config_spacing():
    """若此測試最後一條斷言紅了，代表 bot.py:355-358 的行為變了，
    spec G5 的「config 值即實際值」假設是否仍不成立需重新檢視。"""
    bot = _make_bot(bandit_enabled=True)
    sc = bot.config.symbols[SYMBOL]
    assert sc.grid_spacing == CONFIG_GS   # 前置：config 值

    await bot._grid_step(SYMBOL, sc)

    arm = bot.bandit_optimizer.get_current_params()
    assert sc.grid_spacing == arm.grid_spacing
    assert sc.take_profit_spacing == arm.take_profit_spacing
    assert sc.grid_spacing != CONFIG_GS, (
        "bandit 沒有覆寫 config 的 grid_spacing —— 若此斷言失敗，"
        "表示 bot.py:355-358 的行為變了，spec G5 的結論需重新檢視"
    )


@pytest.mark.asyncio
async def test_bandit_disabled_preserves_config_spacing():
    """實驗前置條件：關掉 bandit，config 值才真的是實盤跑的值。"""
    bot = _make_bot(bandit_enabled=False)
    sc = bot.config.symbols[SYMBOL]

    await bot._grid_step(SYMBOL, sc)

    assert sc.grid_spacing == CONFIG_GS
    assert sc.take_profit_spacing == CONFIG_TP


@pytest.mark.asyncio
async def test_bandit_arm_value_is_what_lands_in_sym_config():
    """把「落地的值來自 bandit arm」與「cold-start arm 是 4」兩件事都釘住。

    生產 decisions.jsonl 60001 筆實測：實盤間距恆為 0.003/0.003（arm 0），
    但本測試釘的是 cold-start（首次啟動、尚無歷史 pulls）情境下
    UCBBanditOptimizer 選中的 arm —— 目前實測是 arm_idx=4
    （DEFAULT_ARMS[4] = gamma=0.10, grid_spacing=0.006, take_profit_spacing=0.004）。
    若 cold_start_arm_idx 的預設值改了，這條會紅，提醒 spec G5-bis 的
    分析需要重做（cold-start arm 選擇邏輯變了，不代表 G-0c3 的覆寫結論變了）。
    """
    bot = _make_bot(bandit_enabled=True)
    sc = bot.config.symbols[SYMBOL]

    await bot._grid_step(SYMBOL, sc)

    arm = bot.bandit_optimizer.get_current_params()
    assert sc.grid_spacing == arm.grid_spacing
    assert sc.grid_spacing == 0.006
    assert arm.grid_spacing == 0.006
