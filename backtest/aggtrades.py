"""Binance Vision UM futures aggTrades 日檔下載/驗證/快取。

設計約束（spec §4.1）：
- UTC 日界（datetime 一律帶 tzinfo=UTC，禁止 naive→本地時區——上次 kline 偏移 8h 教訓）。
- 逐日完整性驗證後才落快取；驗證失敗的日檔不落地（skip-if-exists 毒快取教訓）。
- 未過完的當日直接跳過。
"""
import datetime as dt
import io, zipfile
from pathlib import Path
import pandas as pd
import requests

_UTC = dt.timezone.utc
_COLS = ["agg_id", "price", "qty", "first_id", "last_id", "ts_ms", "is_buyer_maker"]
_URL = "https://data.binance.vision/data/futures/um/daily/aggTrades/{sym}/{sym}-aggTrades-{d}.zip"
_HEAD_MS, _TAIL_MS = 5 * 60_000, 5 * 60_000       # 首筆 <00:05、末筆 >23:55

class AggTradesLoader:
    def __init__(self, data_dir: str | None = None):
        if data_dir is None:
            from backtest.data_loader import DataLoader
            data_dir = str(Path(DataLoader()._get_default_data_dir()) / "futures/um/daily/aggTrades")
        self.data_dir = Path(data_dir)

    def _day_path(self, symbol: str, date_str: str) -> Path:
        return self.data_dir / symbol / f"{symbol}-aggTrades-{date_str}.csv"

    def _fetch_zip(self, symbol: str, date_str: str) -> bytes:
        r = requests.get(_URL.format(sym=symbol, d=date_str), timeout=60)
        r.raise_for_status()
        return r.content

    def validate_day(self, df: pd.DataFrame, date_str: str) -> None:
        if df is None or len(df) == 0:
            raise ValueError(f"{date_str}: 空檔")
        day0 = int(dt.datetime.strptime(date_str, "%Y-%m-%d")
                   .replace(tzinfo=_UTC).timestamp() * 1000)
        day1 = day0 + 86_400_000
        ts = df["ts_ms"]
        if not ((ts >= day0) & (ts < day1)).all():
            raise ValueError(f"{date_str}: 時間戳越日界")
        if ts.iloc[0] > day0 + _HEAD_MS:
            raise ValueError(f"{date_str}: 首筆 {ts.iloc[0]} 距日始 >5min（疑缺頭）")
        if ts.iloc[-1] < day1 - _TAIL_MS:
            raise ValueError(f"{date_str}: 末筆 {ts.iloc[-1]} 距日終 >5min（疑部分日）")
        if not ts.is_monotonic_increasing:
            raise ValueError(f"{date_str}: 時間戳非單調")

    def load_day(self, symbol: str, date_str: str) -> pd.DataFrame:
        df = pd.read_csv(self._day_path(symbol, date_str), header=None, names=_COLS)
        # Binance 部分月份日檔首行帶 header：容錯丟棄非數值首行
        if not str(df.iloc[0]["ts_ms"]).isdigit():
            df = df.iloc[1:].reset_index(drop=True)
        for c in ("price", "qty"):
            df[c] = df[c].astype(float)
        df["ts_ms"] = df["ts_ms"].astype("int64")
        df["is_buyer_maker"] = df["is_buyer_maker"].astype(bool)
        return df

    def download(self, symbol: str, start: str, end: str) -> list[Path]:
        today = dt.datetime.now(_UTC).strftime("%Y-%m-%d")
        out, d = [], dt.datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=_UTC)
        end_d = dt.datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=_UTC)
        while d <= end_d:
            ds = d.strftime("%Y-%m-%d")
            d += dt.timedelta(days=1)
            if ds >= today:          # 未過完的當日不抓
                continue
            path = self._day_path(symbol, ds)
            if path.exists():
                out.append(path); continue
            raw = self._fetch_zip(symbol, ds)
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                csv_bytes = zf.read(zf.namelist()[0])
            df = pd.read_csv(io.BytesIO(csv_bytes), header=None, names=_COLS)
            if not str(df.iloc[0]["ts_ms"]).isdigit():
                df = df.iloc[1:].reset_index(drop=True)
            df["ts_ms"] = df["ts_ms"].astype("int64")
            self.validate_day(df, ds)          # 驗證過才落地
            path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(path, index=False, header=False)
            out.append(path)
        return out


def compress_events(df: pd.DataFrame) -> pd.DataFrame:
    """連續同價 tick 合併：qty 加總、ts 取段首。決策只依賴價格穿越門檻，同價段不觸發狀態改變。"""
    run_id = (df["price"] != df["price"].shift()).cumsum()
    g = df.groupby(run_id, sort=False)
    return pd.DataFrame({"ts_ms": g["ts_ms"].first().values,
                         "price": g["price"].first().values,
                         "qty": g["qty"].sum().values})


def estimate_spread(df: pd.DataFrame, max_gap_ms: int = 1000) -> dict:
    """用 is_buyer_maker 側別重建 spread：True 打在 bid、False 打在 ask，
    僅取時間差 <max_gap_ms 的相鄰異側對，避免跨行情比較（spec F6）。"""
    px, side, ts = df["price"].values, df["is_buyer_maker"].values, df["ts_ms"].values
    spreads = []
    for i in range(1, len(px)):
        if side[i] != side[i - 1] and ts[i] - ts[i - 1] < max_gap_ms:
            ask = px[i] if not side[i] else px[i - 1]
            bid = px[i] if side[i] else px[i - 1]
            if ask >= bid > 0:
                spreads.append((ask - bid) / bid * 10_000)
    if not spreads:
        return {"median_bps": float("nan"), "p90_bps": float("nan"), "n_pairs": 0}
    s = pd.Series(spreads)
    return {"median_bps": float(s.median()), "p90_bps": float(s.quantile(0.9)),
            "n_pairs": len(spreads)}
