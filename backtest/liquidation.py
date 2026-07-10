"""權益 / 保證金 / 強平的純判定。

回測前身完全沒有強平建模：_open() 在保證金不足時只是 return False，
倉位永遠不會被強平，equity 可以變成負數而回測照跑到底。
⇒ 「無限加倉 + 不爆倉」在算術上是必勝策略（martingale 恆等式），
   任何優化器都會選它。見 spec 缺口 G6。
"""
import math


def margin_usage(long_pos: float, short_pos: float, price: float,
                 leverage: float, equity: float) -> float:
    """保證金使用率 = 倉位名目 / 槓桿 / 權益。

    hedge mode 下多空兩側各自佔用保證金，故名目相加。
    equity <= 0 → inf（下游一律視為已強平），避免除零。

    註：live 的 state.margin_usage 是帳戶層（跨 symbol，state.py:115），
    此處是單 symbol。單 symbol 回測的結論不得外推至多 symbol 實盤。
    """
    if equity <= 0:
        return float("inf")
    notional = (long_pos + short_pos) * price
    return (notional / leverage) / equity


def should_liquidate(equity: float, long_pos: float, short_pos: float,
                     price: float, maintenance_margin_rate: float) -> bool:
    """權益是否已跌破維持保證金 → 觸發強平。

    採 isolated margin 的簡化模型：維持保證金 = 倉位名目 × maintenance_margin_rate。
    真實幣安是分層階梯（tiered），此處用單一費率代理並在 FIDELITY_NOTES 揭露。

    無倉位（long_pos == short_pos == 0）→ 永不強平（沒有維持保證金需求）。

    定義域（違反 → raise ValueError，不回傳 False）：
      - price：必須是有限值且 > 0
      - equity：必須是有限值（可為負，代表已穿倉）
      - maintenance_margin_rate：必須是有限值且 >= 0
      - long_pos / short_pos：必須是有限值且 >= 0

    為什麼不用 False 表達無效輸入：這是強平安全檢查函數，函數內 False 的唯一
    合法語意是「倉位是安全的」。若對 NaN/inf/負值等髒資料回傳 False，等同於
    在資料已經崩壞的情況下宣稱「沒事」——equity=NaN 時 `NaN <= x` 在 Python
    恆為 False，price<=0 時 notional<=0 會被誤判為「無倉位」而永不強平——
    兩者都會靜默關掉整個強平檢查，讓回測在帳戶實際已爆倉的路徑上照跑到底。
    呼叫端（backtest/backtester.py 主迴圈，price/OHLC 髒資料 guard）已在餵入
    此函數前擋掉非有限值，所以正常路徑不會觸發這裡的 raise；此處是
    defense-in-depth，一旦上游防禦失效，要讓它變成一個會炸掉的 bug，
    而不是一個靜默錯誤的回測結果。
    """
    if not (isinstance(price, (int, float)) and math.isfinite(price) and price > 0):
        raise ValueError(f"price 必須是有限正值，收到 {price!r}")
    if not (isinstance(equity, (int, float)) and math.isfinite(equity)):
        raise ValueError(f"equity 必須是有限值，收到 {equity!r}")
    if not (isinstance(maintenance_margin_rate, (int, float))
            and math.isfinite(maintenance_margin_rate) and maintenance_margin_rate >= 0):
        raise ValueError(
            f"maintenance_margin_rate 必須是有限非負值，收到 {maintenance_margin_rate!r}")
    if not (isinstance(long_pos, (int, float)) and math.isfinite(long_pos) and long_pos >= 0):
        raise ValueError(f"long_pos 必須是有限非負值，收到 {long_pos!r}")
    if not (isinstance(short_pos, (int, float)) and math.isfinite(short_pos) and short_pos >= 0):
        raise ValueError(f"short_pos 必須是有限非負值，收到 {short_pos!r}")

    notional = (long_pos + short_pos) * price
    if notional <= 0:
        return False
    return equity <= notional * maintenance_margin_rate
