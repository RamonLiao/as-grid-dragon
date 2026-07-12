"""
工具函數與常量定義
"""

import logging
import logging.handlers
import os
from pathlib import Path

from rich.console import Console

# 支援的交易對 (簡化格式 -> ccxt格式)
SYMBOL_MAP = {
    "XRPUSDC": "XRP/USDC:USDC",
    "BTCUSDC": "BTC/USDC:USDC",
    "ETHUSDC": "ETH/USDC:USDC",
    "SOLUSDC": "SOL/USDC:USDC",
    "DOGEUSDC": "DOGE/USDC:USDC",
    "XRPUSDT": "XRP/USDT:USDT",
    "BTCUSDT": "BTC/USDT:USDT",
    "ETHUSDT": "ETH/USDT:USDT",
    "SOLUSDT": "SOL/USDT:USDT",
    "DOGEUSDT": "DOGE/USDT:USDT",
    "BNBUSDT": "BNB/USDT:USDT",
    "ADAUSDT": "ADA/USDT:USDT",
}

# 配置文件路徑
CONFIG_DIR = Path(__file__).parent.parent / "config"
CONFIG_FILE = CONFIG_DIR / "trading_config_max.json"
DATA_DIR = Path(__file__).parent.parent / "asBack" / "data"

# 創建目錄
CONFIG_DIR.mkdir(exist_ok=True)
os.makedirs(Path(__file__).parent.parent / "log", exist_ok=True)

# Console
console = Console()

# 日誌配置
# 時間為機器本地時區（本機 Taipei = UTC+8；與 logs/decisions.jsonl 的 UTC 對時要換算）
LOG_FILE = Path(__file__).parent.parent / "log" / "as_terminal_max.log"
LOG_FORMAT = "%(asctime)s %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


def build_log_handler() -> logging.Handler:
    # delay=True：建構不開檔，首筆 log 才開（測試進程建 handler 不碰活檔）
    return logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=50 * 1024 * 1024, backupCount=3,
        encoding="utf-8", delay=True,
    )


def setup_file_logging() -> None:
    """由引擎入口（as_terminal_max.py）顯式呼叫，不做 import 副作用。

    RotatingFileHandler 的 rollover 會 rename 活檔，多進程（web/streamlit
    也 import 本模組）各自持 handler 對同一檔案輪替會互相抽走 fd —— 只允許
    引擎進程這一個 writer。
    """
    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        datefmt=LOG_DATEFMT,
        handlers=[build_log_handler()],
        force=True,  # root 已有 handler 時 basicConfig 預設無聲 no-op，會讓事件不落磁碟
    )


logger = logging.getLogger("as_grid_max")


def normalize_symbol(symbol_input: str) -> tuple:
    """標準化交易對符號"""
    s = symbol_input.upper().strip().replace("/", "").replace(":", "").replace("-", "")

    if s in SYMBOL_MAP:
        ccxt_sym = SYMBOL_MAP[s]
        parts = ccxt_sym.split("/")
        coin = parts[0]
        quote = parts[1].split(":")[0]
        return s, ccxt_sym, coin, quote

    for suffix in ["USDC", "USDT"]:
        if s.endswith(suffix):
            coin = s[:-len(suffix)]
            if coin:
                ccxt_sym = f"{coin}/{suffix}:{suffix}"
                return s, ccxt_sym, coin, suffix

    return None, None, None, None
