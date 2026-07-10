"""回測初始持倉注入（seed position）：讓回測從既有倉位起跑，
重現生產裝死狀態（多空各 0.58 @ 生產均價 ~690），而非只能空倉起跑。

空倉起跑因 position_limit 止盈加倍把持倉壓在 0.28，永遠碰不到 threshold 0.4
（實測 06-06~07-10 全程 dead_mode 觸發率 0%），故任何裝死路徑相關的實驗
（threshold 掃描、對沖後雙邊裝死）都需要能注入初始持倉。

語義：seed lot pre-populate 進 long_positions/short_positions，margin 從
balance 扣（qty×price/leverage，與 _open 一致），但**不扣 fee**（既存倉位
不是本回測的新成交）。seed 全 0 → 與現狀 bit-identical。
"""
import pandas as pd
import pytest

from backtest.backtester import GridBacktester
from backtest.config import Config


def _flat_df(prices):
    return pd.DataFrame({
        "open_time": pd.date_range("2024-01-01", periods=len(prices), freq="1min"),
        "open": prices, "high": prices, "low": prices, "close": prices,
        "volume": [100.0] * len(prices),
    })


def _seed_cfg(**kw):
    """零成本、大間距（注入後不立刻開新倉/止盈，隔離 seed 的效果）。"""
    base = dict(symbol="BNBUSDC", initial_balance=1000.0, initial_quantity=0.02,
                leverage=20, grid_spacing=1.0, take_profit_spacing=1.0,
                direction="both", terminal_ui_mode=True,
                fee_pct=0.0, slippage_bps=0.0, funding_enabled=False,
                threshold_multiplier=1e9)  # 不觸發裝死，純觀測注入的倉位
    base.update(kw)
    return Config(**base)


def test_seed_long_position_reflected_in_unrealized_pnl():
    """核心：注入多頭 0.58 @ 690，現價 573 → 未實現 = (573-690)×0.58。

    這條同時守住「seed 有接線」與「均價用對了」。若 seed 沒生效，
    unrealized_pnl=0（空倉）；若均價取錯（例如用現價），unrealized 也不對。
    大間距 + 高 threshold_multiplier 確保注入的 0.58 全程不被止盈/加倉動到。
    """
    res = GridBacktester(
        _flat_df([573.0] * 5),
        _seed_cfg(seed_long_qty=0.58, seed_long_price=690.0, direction="long"),
    ).run()
    assert res.unrealized_pnl == pytest.approx((573.0 - 690.0) * 0.58, abs=1e-6), (
        f"注入多頭 0.58@690、現價 573 → 未實現應為 {(573.0-690.0)*0.58}，"
        f"實得 {res.unrealized_pnl}（0 = seed 沒接線；其他 = 均價取錯）"
    )


def test_seed_short_position_reflected_in_unrealized_pnl():
    """對稱：注入空頭 0.58 @ 690，現價 573 → 未實現 = (690-573)×0.58（空頭獲利）。

    守住「空頭側也接了線」，不是只接多頭。
    """
    res = GridBacktester(
        _flat_df([573.0] * 5),
        _seed_cfg(seed_short_qty=0.58, seed_short_price=690.0, direction="short"),
    ).run()
    assert res.unrealized_pnl == pytest.approx((690.0 - 573.0) * 0.58, abs=1e-6), (
        f"注入空頭 0.58@690、現價 573 → 未實現應為 {(690.0-573.0)*0.58}，實得 {res.unrealized_pnl}"
    )


def test_seed_at_current_price_no_fee_preserves_balance():
    """均價=現價（unrealized=0）、零成本 → final_equity 必須 == 本金。

    seed 扣的 margin 會在 equity 裡加回（equity=balance+open_margin+unrealized）。
    若實作誤扣 fee，final_equity < 本金 → 紅。這條專門守「不扣 fee」。
    """
    res = GridBacktester(
        _flat_df([573.0] * 5),
        _seed_cfg(seed_long_qty=0.58, seed_long_price=573.0, direction="long"),
    ).run()
    assert res.final_equity == pytest.approx(1000.0, abs=1e-6), (
        f"均價=現價、零成本 → final_equity 應 == 本金 1000，實得 {res.final_equity}"
        f"（< 1000 = 誤扣了 fee 或重複扣 margin）"
    )


def test_seed_zero_is_bit_identical_to_no_seed():
    """等價守門：seed 全 0 與不設 seed，結果逐位元相同。

    單調下跌讓多頭一路開倉，放大任何差異。seed 注入若污染了 seed=0 路徑
    （例如無條件 append 空 lot、或改了 balance），這條會紅。
    """
    prices = [100.0] * 3 + [99.0, 98.0, 97.0, 96.0, 95.0] + [100.0]
    common = dict(symbol="BNBUSDC", initial_balance=1000.0, initial_quantity=0.5,
                  leverage=10, grid_spacing=0.006, take_profit_spacing=0.004,
                  direction="long", terminal_ui_mode=True,
                  fee_pct=0.0002, slippage_bps=0.0001, funding_enabled=False)
    res_default = GridBacktester(_flat_df(prices), Config(**common)).run()
    res_zero = GridBacktester(
        _flat_df(prices),
        Config(**common, seed_long_qty=0.0, seed_long_price=0.0,
               seed_short_qty=0.0, seed_short_price=0.0),
    ).run()
    assert res_default.final_equity == res_zero.final_equity
    assert res_default.trades_count == res_zero.trades_count
    assert res_default.max_drawdown == res_zero.max_drawdown
    assert res_default.unrealized_pnl == res_zero.unrealized_pnl


def test_seed_margin_deducted_from_balance():
    """seed 佔用的 margin 必須從 balance 扣（否則等於憑空多了保證金）。

    注入 0.58@573（20x）→ margin = 0.58×573/20 = 16.617。此時 balance 應 = 1000-16.617。
    間接驗證：再注入一個對手單無法開倉時的可用資金一致性——這裡直接用
    peak_margin_usage > 0 確認倉位真的佔了保證金額度（空倉時為 0）。
    """
    res = GridBacktester(
        _flat_df([573.0] * 5),
        _seed_cfg(seed_long_qty=0.58, seed_long_price=573.0, direction="long"),
    ).run()
    assert res.peak_margin_usage > 0.0, (
        "注入的倉位必須反映在 margin_usage 上（空倉為 0）；"
        f"實得 {res.peak_margin_usage} → seed 倉位沒佔保證金"
    )


# ── Monkey：極端輸入不得注入垃圾倉位 ──────────────────────────────────

def test_seed_negative_price_not_injected():
    """seed_price <= 0 → 不注入（垃圾價會污染 unrealized/margin）。guard: _px > 0。"""
    res = GridBacktester(
        _flat_df([573.0] * 5),
        _seed_cfg(seed_long_qty=0.58, seed_long_price=-100.0, direction="long"),
    ).run()
    assert res.unrealized_pnl == pytest.approx(0.0, abs=1e-9), (
        f"負 seed_price 應被拒絕注入，unrealized 應為 0，實得 {res.unrealized_pnl}"
    )
    assert res.peak_margin_usage == 0.0


def test_seed_negative_qty_not_injected():
    """seed_qty <= 0 → 不注入（負量 = 反向倉位，語義未定義）。guard: _qty > 0。"""
    res = GridBacktester(
        _flat_df([573.0] * 5),
        _seed_cfg(seed_long_qty=-0.5, seed_long_price=690.0, direction="long"),
    ).run()
    assert res.unrealized_pnl == pytest.approx(0.0, abs=1e-9)
    assert res.peak_margin_usage == 0.0


def test_seed_respects_direction_filter():
    """direction=long 時，seed_short 不得注入（回測方向與注入方向必須一致）。

    guard: cfg.direction in (_side, "both")。否則會憑空造出方向外的倉位。
    """
    res = GridBacktester(
        _flat_df([573.0] * 5),
        _seed_cfg(seed_short_qty=0.58, seed_short_price=690.0, direction="long"),
    ).run()
    assert res.unrealized_pnl == pytest.approx(0.0, abs=1e-9), (
        f"direction=long 應忽略 seed_short，unrealized 應為 0，實得 {res.unrealized_pnl}"
    )


def test_seed_margin_exceeding_balance_does_not_crash():
    """seed margin > balance → balance 變負，回測不得崩潰（照常跑，通常立刻強平）。

    極端注入：0.58 @ 690（20x）margin=20.01，但 initial_balance 只有 5。
    balance 變負是合法的『資不抵債』狀態，回測應照常判定強平，不是 raise。
    """
    res = GridBacktester(
        _flat_df([573.0] * 5),
        _seed_cfg(seed_long_qty=0.58, seed_long_price=690.0, direction="long",
                  initial_balance=5.0),
    ).run()
    # 不崩潰即通過；資不抵債下應被判強平（equity = 5 - 20.01 margin + margin - 67.86 unrealized < 0）
    assert isinstance(res.final_equity, float)
    assert res.liquidated is True, "資不抵債的注入倉位應立刻觸發強平"
