"""回測成本模型純函數層（無 I/O、無副作用）。

與 grid_engine.decision 並列為回測第二個純層邊界：
成本語意集中一處、可 bug-for-bug 單測、不污染回測 loop。
"""
import math


def apply_slippage(price: float, side: str, action: str, bps: float) -> float:
    """成交價往不利方向偏移（執行成本 haircut，非訂單簿滑價）。

    action ∈ {"entry", "tp"}；side ∈ {"long", "short"}。
    - long  entry: price*(1+bps)  買貴
    - long  tp:    price*(1-bps)  賣便宜
    - short entry: price*(1-bps)  賣便宜
    - short tp:    price*(1+bps)  買回貴
    bps<=0 或非有限 → 不偏移。
    """
    if not (isinstance(bps, (int, float)) and math.isfinite(bps)) or bps <= 0:
        return price
    # 買方（成交價升）: long entry / short tp；賣方（成交價降）: long tp / short entry
    buy_side = (side == "long" and action == "entry") or (side == "short" and action == "tp")
    return price * (1 + bps) if buy_side else price * (1 - bps)


def funding_charge(positions: list, rate: float, side: str, mark_price: float) -> float:
    """該側 funding 現金流（正=付出，呼叫端 balance -= charge）。

    notional = Σ(pos["qty"]) * mark_price（mark_price 用結算時點 bar close 代理）。
    - long:  +notional*rate  正 rate 多頭付、負 rate 收
    - short: -notional*rate  相反
    rate 非有限、positions 空 → 0。
    """
    if not (isinstance(rate, (int, float)) and math.isfinite(rate)):
        return 0.0
    if not (isinstance(mark_price, (int, float)) and math.isfinite(mark_price)):
        return 0.0
    notional = sum(p["qty"] for p in positions) * mark_price
    charge = notional * rate
    return charge if side == "long" else -charge
