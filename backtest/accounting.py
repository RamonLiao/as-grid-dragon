"""帳務層 `PositionBook`：從 backtester `_run_terminal_ui_mode` 的 run() 閉包
（`_open`/`_close`/`_equity_at` + seed + funding）**逐行抽出**，行為零變。

兩套平行帳：
- **FIFO（per-lot）帳**：`balance` + `long_positions`/`short_positions`（list of
  {price, qty, margin}）。open 扣 margin+fee、close 走 per-lot FIFO 認列 realized。
  這是 backtester 主路徑唯一委派的帳，語意逐行搬自 backtester.py 閉包，故既有
  測試斷言一條不改即全綠。
- **netted 平行帳**（Binance 生產 margin 語意，spec §6.2）：獨立維護 `netted_balance`
  與每側加權均價/名目。open/seed 更新加權均價、按 qty*price/lev 扣 margin；close
  以 (price-avg)*qty 認 realized、按 qty*avg/lev 釋放 margin，avg 不變。
  已數值驗算：兩帳 equity 逐點相等（equity_at == netted_equity_at 寫成回歸釘），
  但可用餘額（balance vs netted_available）真實分歧。

`conservative_reject`（tick sim 用，Task 7）：True → open() 拒單看 FIFO/netted
兩口徑，任一 margin 不足即回 False；False（backtester 委派）→ 維持原 FIFO-only
判定以保既有行為零變。
"""
from backtest.costs import apply_slippage


class PositionBook:
    def __init__(self, balance: float, leverage: float, fee_pct: float,
                 slippage_bps: float, conservative_reject: bool = False):
        self.leverage = leverage
        self.fee_pct = fee_pct
        self.slippage_bps = slippage_bps
        self.conservative_reject = conservative_reject

        # FIFO（per-lot）帳
        self.balance = balance
        self.long_positions: list = []
        self.short_positions: list = []
        self.trades: list = []
        self.rejected_entries = 0

        # netted 平行帳（獨立 cash + 每側加權均價/名目）
        self.netted_balance = balance
        self._net_qty = {"long": 0.0, "short": 0.0}
        self._net_avg = {"long": 0.0, "short": 0.0}

    # ---- FIFO 帳讀取 ----
    def _bucket(self, side: str) -> list:
        return self.long_positions if side == "long" else self.short_positions

    def qty(self, side: str) -> float:
        return sum(p["qty"] for p in self._bucket(side))

    def open_margin(self) -> float:
        return (sum(p["margin"] for p in self.long_positions)
                + sum(p["margin"] for p in self.short_positions))

    def unrealized_at(self, price: float) -> float:
        u = sum((price - x["price"]) * x["qty"] for x in self.long_positions)
        u += sum((x["price"] - price) * x["qty"] for x in self.short_positions)
        return u

    def equity_at(self, price: float) -> float:
        """權益 at 假設價格 price（balance 已扣未平倉 margin，故加回 open_margin）。"""
        return self.balance + self.open_margin() + self.unrealized_at(price)

    # ---- netted 平行帳 ----
    def _net_open(self, side: str, fill_price: float, qty: float, fee: float) -> None:
        """netted：加權均價更新 + 按新增名目 qty*fill/lev 扣 margin（含 fee）。"""
        nqty = self._net_qty[side]
        navg = self._net_avg[side]
        new_qty = nqty + qty
        # new_qty*new_avg = nqty*navg + qty*fill → 新增 margin = qty*fill/lev（自洽）
        self._net_avg[side] = (nqty * navg + qty * fill_price) / new_qty if new_qty > 0 else 0.0
        self._net_qty[side] = new_qty
        self.netted_balance -= (qty * fill_price) / self.leverage + fee

    def _net_close(self, side: str, fill_price: float, qty: float, fee: float) -> None:
        """netted：等比例縮名目——realized (fill-avg)*qty、釋放 qty*avg/lev margin，avg 不變。"""
        navg = self._net_avg[side]
        gross = ((fill_price - navg) if side == "long" else (navg - fill_price)) * qty
        net = gross - fee
        release = (qty * navg) / self.leverage
        self.netted_balance += release + net
        self._net_qty[side] -= qty
        if self._net_qty[side] <= 0:
            self._net_qty[side] = 0.0
            self._net_avg[side] = 0.0

    def netted_avg(self, side: str) -> float:
        return self._net_avg[side]

    def netted_available(self) -> float:
        return self.netted_balance

    def netted_equity_at(self, price: float) -> float:
        u = (price - self._net_avg["long"]) * self._net_qty["long"]
        u += (self._net_avg["short"] - price) * self._net_qty["short"]
        om = (self._net_qty["long"] * self._net_avg["long"] / self.leverage
              + self._net_qty["short"] * self._net_avg["short"] / self.leverage)
        return self.netted_balance + om + u

    # ---- 委派入口（逐行搬自 backtester 閉包）----
    def seed(self, side: str, qty: float, price: float) -> None:
        """初始持倉注入：margin 從 balance 扣、不扣 fee（既存倉位非本回測新成交，#14 語意）。
        無 slippage（seed 用注入價，非成交）。FIFO 與 netted 兩帳同步注入。"""
        margin = (qty * price) / self.leverage
        self.balance -= margin
        self._bucket(side).append({"price": price, "qty": qty, "margin": margin})
        # netted：無 fee → 均價更新 + 扣 qty*price/lev
        self._net_open(side, price, qty, 0.0)

    def open(self, side: str, price: float, qty: float) -> bool:
        """開倉。回 False = 保證金不足拒單（-2019 等價）。

        搬自 _open 閉包：apply_slippage(entry) → margin=qty*fill/lev、fee=qty*fill*fee_pct、
        判定 margin+fee < balance（嚴格 <，與原閉包一致）。conservative_reject=True 時
        再加驗 netted 口徑（margin+fee < netted_balance），任一不足即拒。"""
        fill_price = apply_slippage(price, side, "entry", self.slippage_bps)
        margin = (qty * fill_price) / self.leverage
        fee = qty * fill_price * self.fee_pct

        fifo_ok = (margin + fee) < self.balance
        if self.conservative_reject:
            netted_ok = (margin + fee) < self.netted_balance
            accepted = fifo_ok and netted_ok
        else:
            accepted = fifo_ok

        if not accepted:
            self.rejected_entries += 1
            return False

        self.balance -= (margin + fee)
        self._bucket(side).append({"price": fill_price, "qty": qty, "margin": margin})
        self._net_open(side, fill_price, qty, fee)
        return True

    def close(self, side: str, price: float, qty: float, ts) -> float:
        """平倉（per-lot FIFO）。回傳本次 realized（net）總額。

        搬自 _close 閉包：apply_slippage(tp) → FIFO 逐 lot gross/fee/net、balance 加回
        margin+net、記 trade。netted 帳等量平倉（實際成交量 = qty - remaining）保 equity 等式。"""
        fill_price = apply_slippage(price, side, "tp", self.slippage_bps)
        positions = self._bucket(side)
        remaining = qty
        total_net = 0.0
        while positions and remaining > 0:
            pos = positions[0]
            if pos["qty"] <= remaining:
                positions.pop(0)
                gross = ((fill_price - pos["price"]) if side == "long"
                         else (pos["price"] - fill_price)) * pos["qty"]
                fee = pos["qty"] * fill_price * self.fee_pct
                net = gross - fee
                self.balance += pos["margin"] + net
                self.trades.append({"pnl": net, "type": side, "timestamp": ts})
                total_net += net
                remaining -= pos["qty"]
            else:
                ratio = remaining / pos["qty"]
                close_margin = pos["margin"] * ratio
                gross = ((fill_price - pos["price"]) if side == "long"
                         else (pos["price"] - fill_price)) * remaining
                fee = remaining * fill_price * self.fee_pct
                net = gross - fee
                self.balance += close_margin + net
                self.trades.append({"pnl": net, "type": side, "timestamp": ts})
                total_net += net
                pos["qty"] -= remaining
                pos["margin"] -= close_margin
                remaining = 0

        actual_closed = qty - remaining
        if actual_closed > 0:
            fee_net = actual_closed * fill_price * self.fee_pct
            self._net_close(side, fill_price, actual_closed, fee_net)
        return total_net

    def apply_funding(self, charge: float) -> None:
        """funding 現金流結算：兩帳同步扣（保 equity 等式；搬自 balance -= charge 觸點）。"""
        self.balance -= charge
        self.netted_balance -= charge
