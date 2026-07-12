"""用真實 1m K 線釘死「close-only 撮合」漏掉的成交量級（spec G4 / 守門 G-0a1）。

資料為外部產物（data/futures/...），缺檔或 bar 數變動即 skip —— 不讓外部
資料使 CI 變紅，但只要資料還在，這個數字就必須成立。
"""
import csv
import glob

import pytest

from backtest.matching import entry_crossed

KLINE_GLOB = "data/futures/um/daily/klines/BNBUSDC/1m/*.csv"
EXPECTED_BARS = 44107
GRID_SPACING = 0.003          # 實盤有效間距（bandit arm 0），見 spec G5
EXPECTED_TOUCH = 167          # low <= limit（真實限價單成交）
EXPECTED_CLOSE_CROSS = 86     # close <= limit（舊實作的成交）


def _load_bars():
    """載入 K 線資料：明確跳過 header，資料損毀一律 raise。

    解析失敗（float() 轉換失敗或欄位缺失）一律向上冒泡 —— 資料損毀必須大聲失敗，
    不得偽裝成「資料變動」而被 skip。skip 只保留給「資料不存在」與「bar 數合法變動」。
    """
    rows = []
    for fp in sorted(glob.glob(KLINE_GLOB)):
        with open(fp) as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if header is None or header[0] != "open_time":
                raise ValueError(
                    f"{fp} 缺少預期的 header（第一欄應為 open_time，實得 "
                    f"{header[0] if header else None}）"
                )
            for r in reader:
                rows.append((float(r[2]), float(r[3]), float(r[4])))  # high, low, close
    return rows


@pytest.mark.skipif(not glob.glob(KLINE_GLOB), reason="真實 K 線資料不存在")
def test_close_only_crossing_misses_about_half_of_real_long_entry_fills():
    """舊實作（close 判穿越）漏掉約 48.5% 的真實多頭進場成交。

    limit 取上一根收盤價下方一格（= 回測掛單邏輯的簡化）。
    entry_crossed 用 low 判定 → 應得 167 次；用 close 判定 → 只有 86 次。
    """
    bars = _load_bars()
    if len(bars) != EXPECTED_BARS:
        pytest.skip(f"K 線資料已變動（{len(bars)} bars，期望 {EXPECTED_BARS}）")

    touch = close_cross = 0
    for i in range(1, len(bars)):
        _, low, close = bars[i]
        prev_close = bars[i - 1][2]
        limit = prev_close * (1 - GRID_SPACING)
        if entry_crossed("long", bar_low=low, bar_high=bars[i][0], limit=limit):
            touch += 1
        if close <= limit:
            close_cross += 1

    assert touch == EXPECTED_TOUCH
    assert close_cross == EXPECTED_CLOSE_CROSS
    missed_ratio = (touch - close_cross) / touch
    assert missed_ratio == pytest.approx(0.485, abs=0.005), (
        f"close-only 撮合漏掉 {missed_ratio:.1%} 的真實成交"
    )
