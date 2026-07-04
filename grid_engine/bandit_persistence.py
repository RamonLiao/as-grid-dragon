"""Bandit 狀態持久化（純層）。

save/load `UCBBanditOptimizer` 狀態，讓學習跨重啟存活。唯一碰檔案 IO 的地方。
本模組不判 config.enabled（gate 由 bot 決定是否呼叫），以保持函數可獨立測試。
"""

import os
import json
import math
import logging
from datetime import datetime, UTC
from typing import Optional

logger = logging.getLogger("as_grid_max")

SCHEMA_VERSION = 1


def save_bandit_state(bandit, path: str) -> None:
    """原子寫 bandit 狀態到 path。失敗會 raise，呼叫端負責 try/except（best-effort）。"""
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "arm_signature": bandit.arm_signature(),
        "saved_at": datetime.now(UTC).isoformat(),
        "state": bandit.to_dict(),
    }
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(envelope, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    # fsync 目錄，確保 rename 落地（GCE VM 被 kill / 斷電時不留 0-byte 檔）
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def load_bandit_state(bandit, path: str, max_age_sec: Optional[float] = None) -> bool:
    """讀取並套用 bandit 狀態。成功回 True；任何失敗/不符/過期回 False（冷啟動）。永不 raise。"""
    if not os.path.exists(path):
        logger.info("[Bandit] 無歷史狀態，冷啟動")
        return False
    try:
        with open(path) as f:
            envelope = json.load(f)
    except (OSError, ValueError):
        logger.warning("[Bandit] 狀態檔讀取/解析失敗，冷啟動")
        return False
    if not isinstance(envelope, dict) or not isinstance(envelope.get("state"), dict):
        logger.warning("[Bandit] 狀態檔格式無效，冷啟動")
        return False
    if envelope.get("schema_version") != SCHEMA_VERSION:
        logger.warning("[Bandit] 狀態 schema 版本不符，冷啟動")
        return False
    if envelope.get("arm_signature") != bandit.arm_signature():
        logger.warning("[Bandit] arms 定義已變，捨棄舊狀態，冷啟動")
        return False
    # Task 4 會在此插入 max_age 過期檢查與 state sanitize；本 task 直接套用
    state = dict(envelope["state"])
    bandit.load_state(state)
    logger.info("[Bandit] 載入狀態 total_pulls=%s", bandit.total_pulls)
    return True
