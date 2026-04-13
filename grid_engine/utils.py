"""
工具函數與常量定義
"""

import logging
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
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[logging.FileHandler(Path(__file__).parent.parent / "log" / "as_terminal_max.log")]
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
