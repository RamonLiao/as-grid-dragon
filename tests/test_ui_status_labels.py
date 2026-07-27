"""面板狀態標籤：`×2` 必須與 decision.tp_quantity 的實際行為一致。

若標籤說「空×2」而實際止盈量是 1×，操作者會誤判新規則沒生效
（spec §7 回退表第 4 條的誤觸來源）。
"""
from grid_engine.ui import position_status_labels

# 生產參數：initial_quantity 0.02 × limit_multiplier 5 = 0.1
#           initial_quantity 0.02 × threshold_multiplier 40 = 0.8
LIMIT, THRESHOLD = 0.1, 0.8


def test_only_the_net_exposure_side_is_labelled_doubled():
    # 生產現況：多 0.60 / 空 0.20，兩側都 > limit 0.1，但只有多頭是淨曝險側
    assert position_status_labels(0.60, 0.20, LIMIT, THRESHOLD) == ["[yellow]多×2[/]"]
    # 鏡像
    assert position_status_labels(0.20, 0.60, LIMIT, THRESHOLD) == ["[yellow]空×2[/]"]


def test_equal_sides_get_no_doubling_label():
    # 嚴格大於：相等時兩側都不加倍，故都不該標
    assert position_status_labels(0.60, 0.60, LIMIT, THRESHOLD) == []


def test_below_limit_gets_no_label():
    assert position_status_labels(0.05, 0.0, LIMIT, THRESHOLD) == []
    assert position_status_labels(LIMIT, 0.0, LIMIT, THRESHOLD) == [], "limit 是嚴格大於"


def test_dead_mode_label_takes_precedence_and_ignores_opposite():
    """裝死判定只看自己這側，與對手側大小無關。

    long=0.9 是**較小**側（short=1.0 更大），但它 > threshold 0.8 ⇒ 仍須標裝死。
    若有人錯誤地把裝死判定也加上淨曝險條件，多頭的標籤會消失 ⇒ 本測試會紅。
    （註：以 LIMIT=0.1 / THRESHOLD=0.8 而言，「對手側較大但未裝死」數學上不可能——
    short > long = 0.9 > 0.8 ⇒ short 必然也超過 threshold，故兩側皆裝死是正解。）
    """
    assert position_status_labels(0.9, 1.0, LIMIT, THRESHOLD) == [
        "[red bold]多裝死[/]", "[red bold]空裝死[/]",
    ]


def test_both_sides_dead_mode():
    assert position_status_labels(0.9, 0.95, LIMIT, THRESHOLD) == [
        "[red bold]多裝死[/]", "[red bold]空裝死[/]",
    ]
