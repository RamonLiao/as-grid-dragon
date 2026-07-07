"""併發寫入正確性：多進程同時 merge_preserve_save，斷言無 lost-update、無撕裂讀。

為什麼重要：這是 F1(tmp 碰撞)/F2(lost-update) 唯一測得到的層級。移除 flock 或
改回固定 tmp 名，此測試即 fail；單進程 unit test 測不出併發缺陷。

設計要點：每個 worker 每輪寫「全域唯一、只寫一次」的 key（wid_iter），不重寫同一
key，藉此移除自我修復性——任一次 lost-update 都讓某 key 永久遺失，最終斷言即可穩定
偵測。若改成反覆寫同一 key，暫時性 lost-update 會被同 worker 下輪覆寫修復，成為
false-negative 弱守衛。
"""
import json
import multiprocessing as mp
from pathlib import Path

from grid_engine import config_io

N_PROCS = 6
ITERS = 50


def _worker(path_str, wid):
    path = Path(path_str)
    for i in range(ITERS):
        config_io.merge_preserve_save(path, {f"{wid}_{i}": 1})
        # 每次寫後立即讀，撕裂讀/tmp 碰撞會讓 json.load raise → 進程非 0 退出
        with open(path, encoding="utf-8") as f:
            json.load(f)


def test_concurrent_writers_no_lost_update_no_torn(tmp_path):
    path = tmp_path / "trading_config_max.json"
    path.write_text(json.dumps({"base": 1}), encoding="utf-8")

    ctx = mp.get_context("spawn")  # 跨平台一致（macOS 預設即 spawn）
    procs = [ctx.Process(target=_worker, args=(str(path), w)) for w in range(N_PROCS)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=120)

    for p in procs:
        assert p.exitcode == 0, "worker 撞到撕裂讀/tmp 碰撞（json.load 失敗）"

    final = json.loads(path.read_text())
    assert final["base"] == 1  # 原 key 不丟
    expected = {f"{w}_{i}" for w in range(N_PROCS) for i in range(ITERS)}
    missing = expected - (set(final) - {"base"})
    assert not missing, f"lost update: {len(missing)} 個 key 遺失（例 {sorted(missing)[:5]}）"
