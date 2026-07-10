"""限價單撮合的純判定：用 bar 的 high/low 判穿越，成交於掛單價。

回測前身（backtester.py:_settle）用該根 close 判穿越、且以 close 成交，
造成兩個方向相反的誤差：
  1. 只看 close → 盤中觸及即成交的限價單被漏掉（真實 1m K 線實測漏 48.5%）
  2. 以 close 成交 → 送出不存在的免費價格改善（實測 mean 10.38 bps）
見 docs/superpowers/specs/2026-07-10-backtest-decision-parity-design.md 缺口 G4。

本模組只回答「有沒有穿越」。成交價一律是掛單價（limit），由呼叫端負責。
"""


def entry_crossed(side: str, bar_low: float, bar_high: float, limit: float) -> bool:
    """進場限價單是否在本根 K 線成交。

    多頭進場 = 買單掛在現價下方 → bar 最低價觸及即成交。
    空頭進場 = 賣單掛在現價上方 → bar 最高價觸及即成交。
    """
    if side == "long":
        return bar_low <= limit
    return bar_high >= limit


def tp_crossed(side: str, bar_low: float, bar_high: float, limit: float) -> bool:
    """止盈限價單是否在本根 K 線成交（方向與進場相反）。

    多頭止盈 = 賣單掛在現價上方 → 看 bar 最高價。
    空頭止盈 = 買單掛在現價下方 → 看 bar 最低價。
    """
    if side == "long":
        return bar_high >= limit
    return bar_low <= limit
