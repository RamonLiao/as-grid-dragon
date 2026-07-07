"""併發寫入正確性：多進程同時 merge_preserve_save，斷言無 lost-update、無撕裂讀。

為什麼重要：這是 F1(tmp 碰撞)/F2(lost-update) 唯一測得到的層級。移除 flock 或
改回固定 tmp 名，此測試即 fail；單進程 unit test 測不出併發缺陷。
"""
import json
import os
from pathlib import Path

import pytest

from grid_engine import config_io

N_PROCS = 5
ITERS = 30


def _worker(path_str, key):
    path = Path(path_str)
    for _ in range(ITERS):
        config_io.merge_preserve_save(path, {key: os.getpid()})
        # 每次寫後立即讀，撕裂讀/tmp 碰撞會讓 json.load raise → 進程非 0 退出
        with open(path, encoding="utf-8") as f:
            json.load(f)


def test_concurrent_writers_no_lost_update_no_torn(tmp_path):
    import multiprocessing as mp
    path = tmp_path / "trading_config_max.json"
    path.write_text(json.dumps({"base": 1}), encoding="utf-8")

    keys = [f"k{i}" for i in range(N_PROCS)]
    ctx = mp.get_context("spawn")  # 跨平台一致（macOS 預設即 spawn）
    procs = [ctx.Process(target=_worker, args=(str(path), k)) for k in keys]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)

    for p in procs:
        assert p.exitcode == 0, "worker 撞到撕裂讀/tmp 碰撞（json.load 失敗）"

    final = json.loads(path.read_text())
    assert final["base"] == 1                      # 原 key 不丟
    for k in keys:                                  # 每個 worker 的 key 都在 → 無 lost-update
        assert k in final, f"lost update: {k} 遺失"
