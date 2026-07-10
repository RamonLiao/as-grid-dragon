"""權益 / 保證金 / 強平的純判定。

回測前身完全沒有強平建模：_open() 在保證金不足時只是 return False，
倉位永遠不會被強平，equity 可以變成負數而回測照跑到底。
⇒ 「無限加倉 + 不爆倉」在算術上是必勝策略（martingale 恆等式），
   任何優化器都會選它。見 spec 缺口 G6。
"""


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
