"""SymbolLocks：per-symbol asyncio.Lock 註冊表（全 bot 共享同一實例）。

鎖序不變式：_sync_lock → symbol lock 單向（spec 不變式 2）。
"""
import asyncio
from typing import Dict


class SymbolLocks:
    def __init__(self):
        self._locks: Dict[str, asyncio.Lock] = {}

    def get(self, ccxt_symbol: str) -> asyncio.Lock:
        return self._locks.setdefault(ccxt_symbol, asyncio.Lock())
