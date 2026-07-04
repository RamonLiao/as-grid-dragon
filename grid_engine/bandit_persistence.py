"""Bandit 狀態持久化（純層）。

save/load `UCBBanditOptimizer` 狀態，讓學習跨重啟存活。唯一碰檔案 IO 的地方。
本模組不判 config.enabled（gate 由 bot 決定是否呼叫），以保持函數可獨立測試。
"""

import os
import json
import math
import logging
from datetime import datetime, UTC, timezone
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


def _is_stale(saved_at, max_age_sec: float) -> bool:
    """saved_at 距今是否超過 max_age_sec；無法解析/型別錯視為過期（保守冷啟動），永不 raise。"""
    if not isinstance(saved_at, str):
        return True
    try:
        ts = datetime.fromisoformat(saved_at)
        if ts.tzinfo is None:                      # naive 視為 UTC
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
    except (ValueError, TypeError):
        return True
    return age > max_age_sec


def _sanitize_state(state: dict) -> None:
    """就地清掉會毒害選擇邏輯的值。"""
    rewards = state.get("rewards")
    if isinstance(rewards, dict):
        for k, seq in list(rewards.items()):
            if isinstance(seq, list):
                rewards[k] = [x for x in seq
                              if isinstance(x, (int, float)) and math.isfinite(x)]
    for key in ("thompson_alpha", "thompson_beta"):
        d = state.get(key)
        if isinstance(d, dict):
            for k, v in list(d.items()):
                if not (isinstance(v, (int, float)) and math.isfinite(v)):
                    d[k] = 1.0
    cr = state.get("cumulative_reward")
    if not (isinstance(cr, (int, float)) and math.isfinite(cr)):
        state["cumulative_reward"] = 0
    tp = state.get("total_pulls")
    try:
        state["total_pulls"] = max(0, int(tp))
    except (TypeError, ValueError):
        state["total_pulls"] = 0
    pc = state.get("pull_counts")
    if isinstance(pc, dict):
        for k, v in list(pc.items()):
            try:
                pc[k] = max(0, int(v))
            except (TypeError, ValueError):
                pc[k] = 0


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
    if max_age_sec is not None and _is_stale(envelope.get("saved_at"), max_age_sec):
        logger.warning("[Bandit] 狀態過期（超過 %ss），冷啟動", max_age_sec)
        return False
    state = dict(envelope["state"])
    state.pop("current_arm_idx", None)   # 不復原瞬時選擇：讓 select_arm 在 live data 重選
    state.pop("current_context", None)   # price_history 未持久化，context 重新暖機
    _sanitize_state(state)
    try:
        bandit.load_state(state)
    except Exception as e:
        logger.warning("[Bandit] 套用狀態失敗（資料損毀），冷啟動: %s", e)
        return False
    # load_state 對 pull_counts 是整表取代（非 merge），部分/空 pull_counts
    # 會讓後續 select_arm() 對缺失 index 做 self.pull_counts[i] → KeyError 炸 async 交易迴圈。
    # 補齊為 0（select_arm 的 cold-start 分支會優先探索 pull=0 的 arm），確保永不 KeyError。
    for i in range(len(bandit.arms)):
        bandit.pull_counts.setdefault(i, 0)
    logger.info("[Bandit] 載入狀態 total_pulls=%s", bandit.total_pulls)
    return True
