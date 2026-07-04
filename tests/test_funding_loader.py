from datetime import datetime
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
