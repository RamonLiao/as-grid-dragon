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


def test_seed_charges_no_fee_even_when_fee_pct_positive():
    """核心（外部 review I1）：seed 是既存倉位，即使 fee_pct>0 也不得扣 fee。

    前一條用 fee_pct=0 → 把待測的 fee 維度壓成常數，誤扣 fee 的真實 bug
    （抄 _open 的 fee=qty×price×fee_pct）在 fee_pct=0 時算出 0、仍綠（假綠）。
    本條 fee_pct=0.0002、seed@現價 → 若 seed 扣了 fee，final_equity =
    1000 - 0.58×573×0.0002 = 999.9335 ≠ 1000 → 紅。大間距確保無其他成交產生 fee。
    """
    res = GridBacktester(
        _flat_df([573.0] * 5),
        _seed_cfg(seed_long_qty=0.58, seed_long_price=573.0, direction="long",
                  fee_pct=0.0002),
    ).run()
    assert res.final_equity == pytest.approx(1000.0, abs=1e-6), (
        f"seed 不得扣 fee（即使 fee_pct>0）；final_equity 應 == 1000，實得 {res.final_equity}"
        f"（≈999.93 = 誤把既存倉位當新成交收了 fee）"
    )


def test_seed_zero_is_bit_identical_to_no_seed():
    """守門：顯式設 seed_*=0 與完全不設（用 dataclass 預設），結果逐位元相同。

    誠實範圍（內部 review M2）：因 Config 預設本就是 0.0，兩個 arm 走的是
    同一條 `_qty==0 → continue` 分支，這條測試無法區分「注入邏輯對非零輸入
    是否污染共享狀態」——那由 `test_seed_*` 系列 + guard `_qty>0` 短路保證。
    本測試真正守的是：若日後有人把某個 seed 欄位的預設值從 0.0 改掉，
    explicit-zero 與 default 會分歧 → 這條會紅。屬預設值回歸守門，非污染守門。
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


def test_seed_lot_occupies_margin_and_deducts_balance():
    """seed lot 必須同時：(a) 佔保證金額度、(b) margin 從 balance 扣（外部 review M3）。

    (a) peak_margin_usage > 0：空倉為 0，注入後 lot 佔額度。
    (b) seed@現價 → unrealized=0 → final_equity 應 == 本金。若漏扣 balance，
        open_margin 仍含 seed margin（lot 帶 margin 欄位）→ equity 多算一個 margin
        → final_equity = 1016.617 ≠ 1000 → 紅。這才真的守住「balance -= margin」，
        單獨 peak_margin_usage>0 刪掉扣減也仍綠（margin_usage 只看 lot 存在）。
    """
    res = GridBacktester(
        _flat_df([573.0] * 5),
        _seed_cfg(seed_long_qty=0.58, seed_long_price=573.0, direction="long"),
    ).run()
    assert res.peak_margin_usage > 0.0, (  # (a)
        f"注入的倉位必須反映在 margin_usage 上（空倉為 0）；實得 {res.peak_margin_usage}"
    )
    assert res.final_equity == pytest.approx(1000.0, abs=1e-6), (  # (b)
        f"seed margin 必須從 balance 扣；漏扣則 final_equity≈1016.6，實得 {res.final_equity}"
    )


# ── Monkey：qty>0 但無法如實注入 → 大聲 raise（不得靜默丟棄）──────────
# 統一原則（內部 review I1/M1）：seed 數字會定實盤 threshold_multiplier 並影響
# 入金決策。qty==0 = 合法不注入；qty>0 但任何原因無法如實注入（負/inf/price<=0/
# 方向矛盾/路由走 legacy）→ 靜默空倉起跑是最危險的失效模式，一律 raise ValueError。

def test_seed_negative_price_raises():
    """seed_qty>0 但 price<=0 → raise（不得靜默丟棄成空倉）。"""
    with pytest.raises(ValueError, match="seed_long_price"):
        GridBacktester(
            _flat_df([573.0] * 5),
            _seed_cfg(seed_long_qty=0.58, seed_long_price=-100.0, direction="long"),
        ).run()


def test_seed_negative_qty_raises():
    """seed_qty < 0（負量 = 反向倉位，語義未定義）→ raise。"""
    with pytest.raises(ValueError, match="seed_long_qty"):
        GridBacktester(
            _flat_df([573.0] * 5),
            _seed_cfg(seed_long_qty=-0.5, seed_long_price=690.0, direction="long"),
        ).run()


def test_seed_infinite_price_raises():
    """seed_price = inf → raise（inf 通過 `>0` 但會污染 balance/equity 成 nan）。"""
    with pytest.raises(ValueError, match="seed_long_price"):
        GridBacktester(
            _flat_df([573.0] * 5),
            _seed_cfg(seed_long_qty=0.58, seed_long_price=float("inf"), direction="long"),
        ).run()


def test_seed_nan_raises():
    """seed_qty 或 seed_price = NaN → raise（外部 review M5：inf 有 red-once，NaN 也要）。

    NaN==0 為 False（不會被當成「不注入」），落到 not isfinite → raise。
    """
    with pytest.raises(ValueError, match="seed_long_qty"):
        GridBacktester(
            _flat_df([573.0] * 5),
            _seed_cfg(seed_long_qty=float("nan"), seed_long_price=690.0, direction="long"),
        ).run()
    with pytest.raises(ValueError, match="seed_long_price"):
        GridBacktester(
            _flat_df([573.0] * 5),
            _seed_cfg(seed_long_qty=0.58, seed_long_price=float("nan"), direction="long"),
        ).run()


def test_seed_direction_mismatch_raises():
    """direction=long 但設了 seed_short>0 → 矛盾配置，raise（不得靜默忽略）。"""
    with pytest.raises(ValueError, match="direction"):
        GridBacktester(
            _flat_df([573.0] * 5),
            _seed_cfg(seed_short_qty=0.58, seed_short_price=690.0, direction="long"),
        ).run()


def test_seed_with_legacy_routing_raises():
    """I1：seed 設了但路由走 _run_legacy_mode（terminal_ui_mode=False 或
    initial_quantity<=0）→ seed 會被靜默丟棄、回傳空倉結果。這是定實盤參數時
    最危險的失效，必須 raise 而非靜默空倉。"""
    with pytest.raises(ValueError, match="terminal_ui_mode|legacy"):
        GridBacktester(
            _flat_df([573.0] * 5),
            _seed_cfg(seed_long_qty=0.58, seed_long_price=690.0, direction="long",
                      terminal_ui_mode=False),
        ).run()
    with pytest.raises(ValueError, match="terminal_ui_mode|legacy"):
        GridBacktester(
            _flat_df([573.0] * 5),
            _seed_cfg(seed_long_qty=0.58, seed_long_price=690.0, direction="long",
                      initial_quantity=0.0),
        ).run()


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
