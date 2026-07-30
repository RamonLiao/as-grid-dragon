"""`_legacy_grid_decision` 傳給 `tp_quantity` 的引數必須語意正確（verifier M10b）。

這條路徑是 deprecated（僅 `initial_quantity <= 0` 觸發），主線走 `decide()`，
所以它的 tp 加倍行為**原本零測試覆蓋**：verifier 把 `opposite_position` 換成
`my_position`（讓「我是淨曝險側」恆真、加倍條件恆假或恆真）之後，571 條測試零反應。

無守衛的行為 = 會執行的註解。這裡用「多空不對稱」把兩個引數釘死：只有在
`my > limit AND my > opposite` 時才加倍，交換或誤傳任一引數都會讓某一格變號。
"""
import pytest

from backtest.backtester import _legacy_grid_decision
from backtest.config import Config


def _cfg():
    # position_limit / position_threshold 在 legacy 路徑是絕對值（非 multiplier 重算）。
    # 取 limit=0.5、threshold=100（裝死關掉，避免 dead 分支吃掉 tp_qty 的斷言）。
    cfg = Config(symbol="BNBUSDC", initial_quantity=0.0)
    cfg.position_limit = 0.5
    cfg.position_threshold = 100.0
    assert cfg.position_limit != cfg.position_threshold, "兩個門檻不得相等，否則引數互換測不出來"
    return cfg


@pytest.mark.parametrize("my_pos, opp_pos, expect_doubled, why", [
    (1.0, 0.2, True,  "我 > limit 且我 > 對手 ⇒ 淨曝險側，加倍"),
    (0.2, 1.0, False, "我 > 對手不成立（我是對沖側）⇒ 不加倍。誤把 opposite 傳成 my 會在這格變成加倍"),
    (1.0, 1.0, False, "平手不加倍（嚴格大於）"),
    (0.3, 0.1, False, "我 <= limit(0.5) ⇒ 不加倍，即使我是較大側"),
])
def test_legacy_path_passes_opposite_position_to_tp_quantity(my_pos, opp_pos, expect_doubled, why):
    base_qty = 3.0
    out = _legacy_grid_decision(price=570.0, my_position=my_pos, opposite_position=opp_pos,
                                cfg=_cfg(), side="long", base_qty=base_qty)
    assert out["dead_mode"] is False, "threshold=100 應該關掉裝死，否則測到的是別的分支"
    expected = base_qty * 2 if expect_doubled else base_qty
    assert out["tp_qty"] == pytest.approx(expected), why


def test_legacy_path_uses_position_limit_not_position_threshold():
    """第四個引數必須是 position_limit（0.5），不是 position_threshold（100）。

    誤傳 threshold 會讓 my=1.0 這格從加倍變成不加倍——上面的 parametrize 已涵蓋，
    這條把「為什麼是 limit 不是 threshold」寫成獨立敘述，避免未來有人以為可以互換。
    """
    cfg = _cfg()
    out = _legacy_grid_decision(price=570.0, my_position=1.0, opposite_position=0.2,
                                cfg=cfg, side="long", base_qty=3.0)
    assert out["tp_qty"] == pytest.approx(6.0), (
        f"my=1.0 > position_limit={cfg.position_limit} 且 > opposite=0.2 ⇒ 應加倍；"
        f"若誤用 position_threshold={cfg.position_threshold} 則 1.0 不超過它、不會加倍")
