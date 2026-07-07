"""Config 讀寫共用底層：merge-preserve + 原子寫 + 跨進程鎖。

單一真相：grid_engine.GlobalConfig.save() 與 web.services.config_store 皆 delegate。
為什麼下沉到 grid_engine：web/services 匯入 grid_engine（下層），反向依賴不允許。

原子性與併發：
- os.replace 給「可見性原子」→ 防撕裂讀。
- fcntl.flock(LOCK_EX) 包住整個 read-modify-write → 防跨進程 lost-update。
- tmp 檔 pid 唯一化 → crash 殘留不互撞、不被誤 replace（flock 之外的 defense-in-depth）。
"""
import fcntl
import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

BACKUP_SUFFIX = ".bak-pre-web-migration"
LOCK_SUFFIX = ".lock"


def load_raw(path) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)  # invalid JSON → 故意 raise（fail loud，不丟未知 key）


def merge_preserve(raw: dict, new: dict,
                   symbol_extras: Optional[dict] = None) -> dict:
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
    return merged


@contextmanager
def _config_lock(path):
    """sidecar .lock 檔上的跨進程獨佔鎖。不鎖 config 本體：os.replace 換 inode 會使鎖失效。"""
    p = Path(path)
    lock_path = p.with_name(p.name + LOCK_SUFFIX)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # 阻塞式，寫檔期間持鎖
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _atomic_write_json(path, data: dict) -> None:
    p = Path(path)
    tmp = p.with_name(f"{p.name}.tmp.{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _ensure_backup(path) -> None:
    p = Path(path)
    if not p.exists():
        return
    bak = p.with_name(p.name + BACKUP_SUFFIX)
    if not bak.exists():
        shutil.copy(p, bak)


def merge_preserve_save(path, new: dict,
                        symbol_extras: Optional[dict] = None,
                        ensure_backup: bool = False) -> None:
    """鎖內 RMW 主入口：flock → 讀 raw → merge → (backup) → 原子寫。"""
    p = Path(path)
    with _config_lock(p):
        merged = merge_preserve(load_raw(p), new, symbol_extras)
        if ensure_backup:
            _ensure_backup(p)
        _atomic_write_json(p, merged)
