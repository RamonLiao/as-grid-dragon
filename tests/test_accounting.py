import pytest
from backtest.accounting import PositionBook


def _book(balance=1000.0, lev=5.0, fee=0.0002, slip=0.0):
    return PositionBook(balance=balance, leverage=lev, fee_pct=fee, slippage_bps=slip)


def test_open_deducts_margin_and_fee():
    b = _book()
    assert b.open("long", 100.0, 1.0) is True
    # margin = 100/5 = 20, fee = 100*0.0002 = 0.02
    assert b.balance == pytest.approx(1000 - 20 - 0.02)


def test_open_rejects_when_margin_insufficient():
    b = _book(balance=10.0)
    assert b.open("long", 100.0, 1.0) is False      # 需 20 > 10
    assert b.rejected_entries == 1
    assert b.balance == 10.0                        # 拒單不動帳


def test_close_fifo_and_realized():
    b = _book(fee=0.0)
    b.open("long", 100.0, 1.0); b.open("long", 110.0, 1.0)
    realized = b.close("long", 120.0, 1.0, ts=None)
    assert realized == pytest.approx(20.0)          # FIFO 先平 100 那口
    assert b.qty("long") == pytest.approx(1.0)


def test_equity_identity():
    b = _book(fee=0.0)
    b.open("long", 100.0, 2.0)
    # equity = balance + margin + uPnL = (1000-40) + 40 + (110-100)*2
    assert b.equity_at(110.0) == pytest.approx(1020.0)


def test_netted_equals_perlot_equity_after_partial_close():
    """spec §6.2 修訂版回歸釘：兩套獨立平倉帳（FIFO vs netted）equity 逐點相等。
    已數值驗算（2026-07-13）：lots [1@100,1@120] close 0.5@130 → FIFO 帳
    balance=981/margin=34、netted 帳 balance=977/margin=33，uPnL 差恆抵銷。
    若未來改動讓兩者分歧，這裡炸。"""
    b = _book(fee=0.0)
    b.open("long", 100.0, 1.0); b.open("long", 120.0, 1.0)
    b.close("long", 130.0, 0.5, ts=None)            # 部分平倉後兩帳 balance 已分歧
    for p in (90.0, 110.0, 140.0):
        assert b.equity_at(p) == pytest.approx(b.netted_equity_at(p))


def test_available_balance_diverges_after_partial_close():
    """反向釘：兩帳「可用餘額」必須分歧（FIFO 981 vs netted 977）——
    若有人把 netted 帳實作成 per-lot 換皮（從 lot 重算 avg），這裡炸。"""
    b = _book(fee=0.0)
    b.open("long", 100.0, 1.0); b.open("long", 120.0, 1.0)
    b.close("long", 130.0, 0.5, ts=None)
    assert b.balance == pytest.approx(981.0)
    assert b.netted_available() == pytest.approx(977.0)


def test_conservative_reject_uses_worse_margin():
    """保守取或：netted 口徑不足即拒單，即使 FIFO 口徑足夠"""
    b = PositionBook(balance=1000.0, leverage=5.0, fee_pct=0.0, slippage_bps=0.0,
                     conservative_reject=True)
    b.open("long", 100.0, 1.0); b.open("long", 120.0, 1.0)
    b.close("long", 130.0, 0.5, ts=None)            # FIFO 可用 981 / netted 977
    # 構造需 margin 介於 977 與 981 之間的開倉：qty*price/5 = 979 → qty=48.95@100
    assert b.open("long", 100.0, 48.95) is False
    assert b.rejected_entries == 1


def test_seed_no_fee():
    b = _book()
    b.seed("long", 1.0, 100.0)
    assert b.balance == pytest.approx(1000 - 20)    # 只扣 margin 不扣 fee
