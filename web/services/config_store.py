"""Config 讀寫（merge-preserve）。

為什麼不用 grid_engine.GlobalConfig.save()：engine 的 to_dict() 只 emit
engine schema 認識的欄位，而 config/trading_config_max.json 還有
trading_mode（頁3 優化器用）、risk hard_stop 三欄、exchange_type/testnet
等欄位。直接覆寫會把它們永久抹掉。這裡以 raw JSON 為底做欄位級 merge。
"""
import json
import shutil
from pathlib import Path
from typing import Optional

from grid_engine.config import GlobalConfig
from grid_engine.utils import CONFIG_FILE

BACKUP_SUFFIX = ".bak-pre-web-migration"


def _resolve(path: Optional[Path]) -> Path:
    return Path(path) if path is not None else CONFIG_FILE


def load_raw(path: Optional[Path] = None) -> dict:
    p = _resolve(path)
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def load_config(path: Optional[Path] = None) -> GlobalConfig:
    return GlobalConfig.from_dict(load_raw(path))


def get_symbol_extra(ccxt_symbol: str, key: str, default=None,
                     path: Optional[Path] = None):
    raw = load_raw(path)
    return raw.get("symbols", {}).get(ccxt_symbol, {}).get(key, default)


def _ensure_backup(p: Path) -> None:
    if not p.exists():
        return
    bak = p.with_name(p.name + BACKUP_SUFFIX)
    if not bak.exists():
        shutil.copy(p, bak)


def save_config(config: GlobalConfig,
                symbol_extras: Optional[dict] = None,
                path: Optional[Path] = None) -> None:
    """merge-preserve 存檔。

    - top-level：raw 有、engine to_dict 沒有的 key 原樣保留。
    - symbols：以 config.symbols 為準（新增進檔、刪除移除），
      每個 symbol 內 raw 有、engine 沒有的 key（如 trading_mode）保留。
    - risk 等巢狀 dict：同樣欄位級 merge。
    - symbol_extras：{ccxt_symbol: {key: value}} 顯式覆寫（頁2 編輯 trading_mode 用）。
    """
    p = _resolve(path)
    raw = load_raw(p)
    new = config.to_dict()

    merged = dict(raw)  # raw 為底，保留未知 top-level key
    for k, v in new.items():
        if k == "symbols":
            merged_symbols = {}
            raw_symbols = raw.get("symbols", {})
            for sym_key, sym_new in v.items():
                sym_merged = dict(raw_symbols.get(sym_key, {}))
                sym_merged.update(sym_new)
                merged_symbols[sym_key] = sym_merged
            merged[k] = merged_symbols  # config 已刪的 symbol 不進 merged
        elif isinstance(v, dict) and isinstance(raw.get(k), dict):
            sub = dict(raw[k])
            sub.update(v)
            merged[k] = sub
        else:
            merged[k] = v

    for sym_key, extras in (symbol_extras or {}).items():
        if sym_key in merged.get("symbols", {}):
            merged["symbols"][sym_key].update(extras)

    _ensure_backup(p)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
