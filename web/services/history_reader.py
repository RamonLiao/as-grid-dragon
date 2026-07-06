"""grid_engine 落地檔讀取（頁1 歷史檢視資料層）。

引擎在 GCE/本地持續 append decisions.jsonl，行可能截斷——
逐行 json.loads、壞行跳過，永不 raise。
"""
import json
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DECISIONS_PATH = _PROJECT_ROOT / "logs" / "decisions.jsonl"
BANDIT_STATE_PATH = _PROJECT_ROOT / "logs" / "bandit_state.json"


def load_decisions(path: Optional[Path] = None,
                   max_lines: int = 5000) -> pd.DataFrame:
    p = Path(path) if path is not None else DECISIONS_PATH
    if not p.exists():
        return pd.DataFrame()

    rows = []
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            tail = deque(f, maxlen=max_lines)
    except OSError:
        return pd.DataFrame()
    for line in tail:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        inputs = rec.get("inputs", {}) or {}
        rows.append({
            "ts": rec.get("ts"),
            "symbol": rec.get("symbol", ""),
            "price": inputs.get("price"),
            "long_position": inputs.get("long_position"),
            "short_position": inputs.get("short_position"),
            "grid_spacing": inputs.get("grid_spacing"),
            "take_profit_spacing": inputs.get("take_profit_spacing"),
            "long_dead_mode": inputs.get("long_dead_mode"),
            "short_dead_mode": inputs.get("short_dead_mode"),
        })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df[pd.to_numeric(df["ts"], errors="coerce").notna()]
    df["ts"] = pd.to_datetime(df["ts"], unit="s")
    return df


def load_bandit_state(path: Optional[Path] = None) -> dict:
    p = Path(path) if path is not None else BANDIT_STATE_PATH
    if not p.exists():
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError, ValueError):
        return {}


def last_activity(df: pd.DataFrame) -> Optional[datetime]:
    if df is None or df.empty or "ts" not in df.columns:
        return None
    return df["ts"].max().to_pydatetime()
