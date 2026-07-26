"""Config 讀寫（merge-preserve）。

為什麼不用 grid_engine.GlobalConfig.save()：engine 的 to_dict() 只 emit
engine schema 認識的欄位，而 config/trading_config_max.json 還有
trading_mode（頁3 優化器用）、risk hard_stop 三欄、exchange_type/testnet
等欄位。直接覆寫會把它們永久抹掉。這裡以 raw JSON 為底做欄位級 merge。

merge/原子寫/tmp 邏輯已下沉到 grid_engine.config_io（單一真相，engine
與 web 共用），本模組僅 delegate。
"""
from pathlib import Path
from typing import Optional

from grid_engine.config import GlobalConfig
from grid_engine.utils import CONFIG_FILE
from grid_engine import config_io
from grid_engine.config_io import BACKUP_SUFFIX  # 對外相容 re-export


def _resolve(path: Optional[Path]) -> Path:
    return Path(path) if path is not None else CONFIG_FILE


def load_raw(path: Optional[Path] = None) -> dict:
    return config_io.load_raw(_resolve(path))


def load_config(path: Optional[Path] = None) -> GlobalConfig:
    return GlobalConfig.from_dict(load_raw(path))


def get_mtime(path: Optional[Path] = None) -> int:
    """回傳設定檔目前的 mtime（ns），檔案不存在回 0。

    用於跨頁配置同步判斷（見 web/state.py check_config_updated）：
    比對檔案是否真的被寫入過，而非逐欄位比對 session 內的 config 物件
    （後者會被頁內 widget 對 session config 的暫時性寫入誤判為外部更新）。
    """
    p = _resolve(path)
    if not p.exists():
        return 0
    return p.stat().st_mtime_ns


def get_symbol_extra(ccxt_symbol: str, key: str, default=None,
                     path: Optional[Path] = None):
    raw = load_raw(path)
    return raw.get("symbols", {}).get(ccxt_symbol, {}).get(key, default)


def save_config(config: GlobalConfig,
                symbol_extras: Optional[dict] = None,
                path: Optional[Path] = None) -> None:
    """merge-preserve + 原子寫 + 跨進程鎖 存檔（delegate config_io，單一真相）。

    首次存檔前建一次性 .bak-pre-web-migration 備份。symbol_extras
    {ccxt_symbol: {key: value}} 顯式覆寫（頁2 編輯 trading_mode 用）。
    """
    config_io.merge_preserve_save(
        _resolve(path), config.to_dict(),
        symbol_extras=symbol_extras, ensure_backup=True,
        drop_symbol_keys={"leverage"})   # 一次性遷移，見 GlobalConfig.save 註記
