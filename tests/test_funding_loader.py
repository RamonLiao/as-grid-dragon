from datetime import datetime, timezone
from backtest.data_loader import DataLoader


class _FakeExchange:
    """回傳兩頁 funding history，第二頁後為空。ccxt timestamp 為毫秒。"""
    def __init__(self):
        self.calls = 0

    def fetch_funding_rate_history(self, symbol, since=None, limit=None, params=None):
        self.calls += 1
        if self.calls == 1:
            return [
                {"timestamp": 1_000_000_000_000, "fundingRate": 0.0001},
                {"timestamp": 1_000_028_800_000, "fundingRate": -0.0002},  # +8h
            ]
        return []  # 第二頁空 → 停


def test_load_funding_paginates_and_stops_on_empty(tmp_path):
    loader = DataLoader(data_dir=str(tmp_path))
    ex = _FakeExchange()
    fmap = loader.load_funding("BTCUSDC", datetime(2001, 9, 9), datetime(2001, 9, 10), exchange=ex)
    assert fmap == {1_000_000_000: 0.0001, 1_000_028_800: -0.0002}
    assert ex.calls == 2  # 一頁資料 + 一頁空


def test_load_funding_uses_cache_second_time(tmp_path):
    loader = DataLoader(data_dir=str(tmp_path))
    ex = _FakeExchange()
    loader.load_funding("BTCUSDC", datetime(2001, 9, 9), datetime(2001, 9, 10), exchange=ex)
    # 快取檔已寫：第二次不呼 exchange
    ex2 = _FakeExchange()
    fmap = loader.load_funding("BTCUSDC", datetime(2001, 9, 9), datetime(2001, 9, 10), exchange=ex2)
    assert fmap == {1_000_000_000: 0.0001, 1_000_028_800: -0.0002}
    assert ex2.calls == 0


def test_load_funding_fetch_failure_returns_empty(tmp_path):
    class _BoomExchange:
        def fetch_funding_rate_history(self, *a, **k):
            raise RuntimeError("network down")
    loader = DataLoader(data_dir=str(tmp_path))
    fmap = loader.load_funding("BTCUSDC", datetime(2001, 9, 9), datetime(2001, 9, 10), exchange=_BoomExchange())
    assert fmap == {}


class _MidPaginationBoomExchange:
    """第一頁回傳正常資料，第二頁（分頁中）直接炸掉，模擬瞬斷網路。"""
    def __init__(self):
        self.calls = 0

    def fetch_funding_rate_history(self, symbol, since=None, limit=None, params=None):
        self.calls += 1
        if self.calls == 1:
            return [
                {"timestamp": 1_000_000_000_000, "fundingRate": 0.0001},
                {"timestamp": 1_000_028_800_000, "fundingRate": -0.0002},  # +8h
            ]
        raise RuntimeError("network down mid-pagination")


def test_load_funding_mid_pagination_failure_does_not_poison_cache(tmp_path):
    loader = DataLoader(data_dir=str(tmp_path))
    ex = _MidPaginationBoomExchange()
    fmap = loader.load_funding("BTCUSDC", datetime(2001, 9, 9), datetime(2001, 9, 10), exchange=ex)

    # (a) 已知的第一頁資料仍回傳，不 raise
    assert fmap == {1_000_000_000: 0.0001, 1_000_028_800: -0.0002}

    # (b) 快取檔不應存在：partial 資料不可永久毒化快取
    assert not loader.get_funding_path("BTCUSDC").exists()

    # (c) 之後再呼叫（換一個正常運作的 exchange）應該重新抓取，而非誤讀空快取
    ex2 = _FakeExchange()
    fmap2 = loader.load_funding("BTCUSDC", datetime(2001, 9, 9), datetime(2001, 9, 10), exchange=ex2)
    assert ex2.calls == 2
    assert fmap2 == {1_000_000_000: 0.0001, 1_000_028_800: -0.0002}


class _CapturingExchange:
    """記錄呼叫時收到的 since，並依 end 邊界回傳一筆邊界內/邊界外的 item 驗證 end_ms 排除邏輯。"""
    def __init__(self, end_ms):
        self.calls = 0
        self.captured_since = None
        self._end_ms = end_ms

    def fetch_funding_rate_history(self, symbol, since=None, limit=None, params=None):
        self.calls += 1
        if self.calls == 1:
            self.captured_since = since
            return [
                {"timestamp": self._end_ms, "fundingRate": 0.0001},          # 應被納入（等於 since 起點日）
                {"timestamp": self._end_ms + 86400_000, "fundingRate": 0.9},  # >= end_ms 排除
            ]
        return []


def test_load_funding_window_is_utc_not_local_tz(tmp_path):
    """回歸測試（final-review I1）：since/end_ms 必須以 UTC 計算，不受本地時區影響。

    datetime(2001, 9, 9, tzinfo=timezone.utc).timestamp() == 999993600.0
    → since 應精確為 999993600000（ms）。在非 UTC 本地時區（例如 Asia/Taipei，UTC+8）
    若用 naive datetime().timestamp()（本地時區解讀），會得到 999964800000，與此不符。
    """
    expected_since = 999_993_600_000
    assert int(datetime(2001, 9, 9, tzinfo=timezone.utc).timestamp() * 1000) == expected_since

    loader = DataLoader(data_dir=str(tmp_path))
    ex = _CapturingExchange(end_ms=expected_since)
    fmap = loader.load_funding("BTCUSDC", datetime(2001, 9, 9), datetime(2001, 9, 9), exchange=ex)

    assert ex.captured_since == expected_since

    # end_ms = expected_since + 86400*1000（單日窗口）；邊界內納入，邊界外（>= end_ms）排除
    assert (expected_since // 1000) in fmap
    assert fmap[expected_since // 1000] == 0.0001
    assert ((expected_since + 86400_000) // 1000) not in fmap
