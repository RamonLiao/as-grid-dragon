# Bandit 狀態持久化 (#6) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 `UCBBanditOptimizer` 學到的狀態跨重啟存活，重啟不歸零重學。

**Architecture:** 新增純層模組 `grid_engine/bandit_persistence.py`（save/load，file-IO 隔離、可獨立測試）；`indicators/bandit.py` 只加一個純讀 `arm_signature()`；`grid_engine/bot.py` 只做接線（run 載入、每次評估後條件存、stop 收尾）。存檔用原子寫 + fsync，envelope 帶 `schema_version`/`arm_signature`/`saved_at`；任何載入失敗一律 cold-start，永不 crash。

**Tech Stack:** Python 3, stdlib（os/json/math/hashlib/datetime）、pytest、uv。

## Global Constraints

- **live config 是 `grid_engine/config.py::GlobalConfig`**（tests 從 `grid_engine.config` import）。`config/models.py` 是舊 core 系統，**勿改**。
- **live BanditConfig 是 `grid_engine/enhancements.BanditConfig`**（`grid_engine/config.py` 由此 import）。測試建 bandit 用 `from grid_engine.enhancements import BanditConfig`。
- bandit 本體在 `indicators/bandit.py`；`to_dict`(`:525`)/`load_state`(`:539`) 已存在，**不改其結構**（保持 roundtrip 對稱）。
- 存檔 default 路徑 `logs/bandit_state.json`，可由 `config.bandit_state_path` override。
- `config.bandit.enabled` 為 False 時 load/save 皆 short-circuit（gate 在 bot 層，persistence 函數本身不判 enabled）。
- 缺套件用 `uv`。git stage 只 `git add <明確檔案>`，禁止 `git add -A/.`。
- 專案規則：Unit/Integration 後必做 Monkey Testing（Task 6）。測試報數量不報形容詞。

---

## File Structure

- **Create** `grid_engine/bandit_persistence.py` — save/load bandit 狀態（純層，唯一碰檔案 IO 的地方）。
- **Create** `tests/test_bandit_persistence.py` — 純層 + bot 接線 + monkey + replay invariant 測試。
- **Create** `tests/test_bandit_state_config.py` — GlobalConfig 新欄位 roundtrip/正規化測試。
- **Modify** `indicators/bandit.py` — 加 `import hashlib` + `arm_signature()`。
- **Modify** `grid_engine/config.py` — GlobalConfig 加 `bandit_state_path` / `bandit_state_max_age_sec` + `_parse_bandit_state_max_age` + to_dict/from_dict 接線；`from typing import` 補 `Optional`。
- **Modify** `grid_engine/bot.py` — `__init__` 加兩個 instance 屬性；`run()` 載入；加 `_persist_bandit_state`/`_maybe_persist_bandit_state`；record_trade 段（`:909`）呼叫；`stop()`（`:1098`）best-effort save。

---

### Task 1: `arm_signature()` 純讀 helper

**Files:**
- Modify: `indicators/bandit.py`（top import + class `UCBBanditOptimizer` 內加方法，約 `:525` `to_dict` 前）
- Test: `tests/test_bandit_persistence.py`

**Interfaces:**
- Produces: `UCBBanditOptimizer.arm_signature() -> str`（sha1 hexdigest of `[(gamma, grid_spacing, take_profit_spacing) for arm in self.arms]`）。

- [ ] **Step 1: Write the failing test**

建立 `tests/test_bandit_persistence.py`：

```python
from indicators.bandit import UCBBanditOptimizer, ParameterArm
from grid_engine.enhancements import BanditConfig


def _bandit():
    return UCBBanditOptimizer(BanditConfig(enabled=True))


def test_arm_signature_stable_across_instances():
    assert _bandit().arm_signature() == _bandit().arm_signature()
    assert isinstance(_bandit().arm_signature(), str)


def test_arm_signature_changes_on_arm_count():
    a = _bandit()
    b = _bandit()
    b.arms = b.arms[:-1]
    assert a.arm_signature() != b.arm_signature()


def test_arm_signature_changes_on_arm_value():
    a = _bandit()
    b = _bandit()
    b.arms[0] = ParameterArm(gamma=0.999, grid_spacing=0.003, take_profit_spacing=0.003)
    assert a.arm_signature() != b.arm_signature()


def test_arm_signature_changes_on_order():
    a = _bandit()
    b = _bandit()
    b.arms[0], b.arms[1] = b.arms[1], b.arms[0]
    assert a.arm_signature() != b.arm_signature()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_bandit_persistence.py -v`
Expected: FAIL — `AttributeError: 'UCBBanditOptimizer' object has no attribute 'arm_signature'`

- [ ] **Step 3: Write minimal implementation**

在 `indicators/bandit.py` top imports 加 `import hashlib`（放在既有 `import time` / `import logging` 附近）。在 `UCBBanditOptimizer` class 內、`to_dict` 方法之前加：

```python
    def arm_signature(self) -> str:
        """arms 定義的穩定簽章；arms 數量/順序/參數值任一改變則簽章改變。
        用於持久化：載入時簽章不符即捨棄舊狀態冷啟動，避免 arm index 錯位學錯。"""
        payload = [(a.gamma, a.grid_spacing, a.take_profit_spacing) for a in self.arms]
        return hashlib.sha1(repr(payload).encode()).hexdigest()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_bandit_persistence.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: Commit**

```bash
git add indicators/bandit.py tests/test_bandit_persistence.py
git commit -m "feat: #6 UCBBanditOptimizer.arm_signature（arms 定義簽章，供持久化冷啟動守門）"
```

---

### Task 2: GlobalConfig 新增持久化設定欄位

**Files:**
- Modify: `grid_engine/config.py`（`class GlobalConfig` 欄位、`to_dict`、`from_dict`、加 `_parse_bandit_state_max_age`；`from typing import Dict` → `Dict, Optional`）
- Test: `tests/test_bandit_state_config.py`

**Interfaces:**
- Produces: `GlobalConfig.bandit_state_path: Optional[str] = None`、`GlobalConfig.bandit_state_max_age_sec: Optional[int] = None`；`GlobalConfig._parse_bandit_state_max_age(value) -> Optional[int]`（非正/非法 → None）。

- [ ] **Step 1: Write the failing test**

建立 `tests/test_bandit_state_config.py`：

```python
from grid_engine.config import GlobalConfig


def test_defaults_none():
    c = GlobalConfig()
    assert c.bandit_state_path is None
    assert c.bandit_state_max_age_sec is None


def test_roundtrip_preserves_fields():
    c = GlobalConfig()
    c.bandit_state_path = "logs/x.json"
    c.bandit_state_max_age_sec = 3600
    c2 = GlobalConfig.from_dict(c.to_dict())
    assert c2.bandit_state_path == "logs/x.json"
    assert c2.bandit_state_max_age_sec == 3600


def test_backward_compat_missing_keys():
    c = GlobalConfig.from_dict({})  # 舊 config 無這些欄
    assert c.bandit_state_path is None
    assert c.bandit_state_max_age_sec is None


def test_max_age_normalization():
    # 非正 / 非法型別 → None（永不過期）；空字串 path → None
    assert GlobalConfig.from_dict({"bandit_state_max_age_sec": 0}).bandit_state_max_age_sec is None
    assert GlobalConfig.from_dict({"bandit_state_max_age_sec": -5}).bandit_state_max_age_sec is None
    assert GlobalConfig.from_dict({"bandit_state_max_age_sec": "oops"}).bandit_state_max_age_sec is None
    assert GlobalConfig.from_dict({"bandit_state_max_age_sec": "600"}).bandit_state_max_age_sec == 600
    assert GlobalConfig.from_dict({"bandit_state_path": ""}).bandit_state_path is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_bandit_state_config.py -v`
Expected: FAIL — `AttributeError` 或 `TypeError`（欄位不存在）

- [ ] **Step 3: Write minimal implementation**

在 `grid_engine/config.py`：

1. import：`from typing import Dict` 改成 `from typing import Dict, Optional`。

2. `GlobalConfig` 欄位區（在 `telegram_daily_pnl_hour: int = 20` 之後）加：

```python
    # === Bandit 狀態持久化 ===
    bandit_state_path: Optional[str] = None       # None → bot 套 default logs/bandit_state.json
    bandit_state_max_age_sec: Optional[int] = None  # None = 永不過期
```

3. `to_dict()` return dict 末尾加：

```python
            "bandit_state_path": self.bandit_state_path,
            "bandit_state_max_age_sec": self.bandit_state_max_age_sec,
```

4. 在其他 `_parse_*` static method 附近加：

```python
    @staticmethod
    def _parse_bandit_state_max_age(value) -> Optional[int]:
        """正規化 bandit 狀態過期秒數；非正/非法/None → None（永不過期）。"""
        if value is None:
            return None
        try:
            secs = int(value)
        except (TypeError, ValueError):
            return None
        return secs if secs > 0 else None
```

5. `from_dict` 的 `config = cls(...)` 參數列末尾加（在 `telegram_daily_pnl_hour=...` 之後）：

```python
            bandit_state_path=data.get("bandit_state_path") or None,
            bandit_state_max_age_sec=cls._parse_bandit_state_max_age(
                data.get("bandit_state_max_age_sec")),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_bandit_state_config.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: Commit**

```bash
git add grid_engine/config.py tests/test_bandit_state_config.py
git commit -m "feat: #6 GlobalConfig 加 bandit_state_path/max_age_sec（from_dict 正規化，向後相容）"
```

---

### Task 3: `bandit_persistence` save + load 核心

**Files:**
- Create: `grid_engine/bandit_persistence.py`
- Test: `tests/test_bandit_persistence.py`

**Interfaces:**
- Consumes: `bandit.arm_signature()`（Task 1）、`bandit.to_dict()`/`bandit.load_state(dict)`（既有）。
- Produces:
  - `SCHEMA_VERSION = 1`
  - `save_bandit_state(bandit, path: str) -> None`（原子寫 envelope；失敗 raise，呼叫端負責 try/except）
  - `load_bandit_state(bandit, path: str, max_age_sec=None) -> bool`（成功套用回 True，任何失敗/不符回 False；永不 raise）

本 task 只做核心：happy-path roundtrip、arm_signature/schema 守門、缺檔/壞檔 cold-start、原子寫。C/D/max_age 在 Task 4。

- [ ] **Step 1: Write the failing test**

在 `tests/test_bandit_persistence.py` 追加（沿用 Task 1 的 `_bandit()`；補 imports）：

```python
import json
from grid_engine.bandit_persistence import save_bandit_state, load_bandit_state, SCHEMA_VERSION


def _trained_bandit(updates=3):
    b = _bandit()
    n = b.config.update_interval * updates
    for i in range(n):
        b.record_trade(1.0 if i % 2 else -0.5, "long" if i % 2 else "short")
    return b


def test_roundtrip_preserves_learned_stats(tmp_path):
    b = _trained_bandit()
    path = str(tmp_path / "bandit_state.json")
    save_bandit_state(b, path)

    b2 = _bandit()
    assert load_bandit_state(b2, path) is True
    assert b2.total_pulls == b.total_pulls
    assert b2.pull_counts == b.pull_counts
    assert {k: list(v) for k, v in b2.rewards.items()} == {k: list(v) for k, v in b.rewards.items()}
    assert b2.cumulative_reward == b.cumulative_reward
    assert dict(b2.thompson_alpha) == dict(b.thompson_alpha)
    assert dict(b2.thompson_beta) == dict(b.thompson_beta)


def test_missing_file_cold_starts(tmp_path):
    b = _bandit()
    assert load_bandit_state(b, str(tmp_path / "nope.json")) is False
    assert b.total_pulls == 0


def test_corrupt_json_cold_starts(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json")
    assert load_bandit_state(_bandit(), str(p)) is False


def test_non_dict_or_missing_state_cold_starts(tmp_path):
    p1 = tmp_path / "a.json"; p1.write_text("123")
    assert load_bandit_state(_bandit(), str(p1)) is False
    p2 = tmp_path / "b.json"; p2.write_text(json.dumps({"schema_version": SCHEMA_VERSION}))
    assert load_bandit_state(_bandit(), str(p2)) is False


def test_schema_version_mismatch_rejected(tmp_path):
    b = _trained_bandit(); path = str(tmp_path / "s.json")
    save_bandit_state(b, path)
    env = json.loads((tmp_path / "s.json").read_text())
    env["schema_version"] = 999
    (tmp_path / "s.json").write_text(json.dumps(env))
    b2 = _bandit()
    assert load_bandit_state(b2, path) is False
    assert b2.total_pulls == 0


def test_arm_signature_mismatch_rejected(tmp_path):
    b = _trained_bandit(); path = str(tmp_path / "s.json")
    save_bandit_state(b, path)
    env = json.loads((tmp_path / "s.json").read_text())
    env["arm_signature"] = "deadbeef"
    (tmp_path / "s.json").write_text(json.dumps(env))
    b2 = _bandit()
    assert load_bandit_state(b2, path) is False
    assert b2.total_pulls == 0


def test_save_atomic_makedirs_and_no_tmp(tmp_path):
    b = _trained_bandit()
    nested = tmp_path / "a" / "b" / "bandit_state.json"
    save_bandit_state(b, str(nested))
    assert nested.exists()
    assert not (tmp_path / "a" / "b" / "bandit_state.json.tmp").exists()
    env = json.loads(nested.read_text())
    assert env["schema_version"] == SCHEMA_VERSION
    assert "saved_at" in env and "state" in env
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_bandit_persistence.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'grid_engine.bandit_persistence'`

- [ ] **Step 3: Write minimal implementation**

建立 `grid_engine/bandit_persistence.py`：

```python
"""Bandit 狀態持久化（純層）。

save/load `UCBBanditOptimizer` 狀態，讓學習跨重啟存活。唯一碰檔案 IO 的地方。
本模組不判 config.enabled（gate 由 bot 決定是否呼叫），以保持函數可獨立測試。
"""

import os
import json
import math
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger("as_grid_max")

SCHEMA_VERSION = 1


def save_bandit_state(bandit, path: str) -> None:
    """原子寫 bandit 狀態到 path。失敗會 raise，呼叫端負責 try/except（best-effort）。"""
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "arm_signature": bandit.arm_signature(),
        "saved_at": datetime.utcnow().isoformat(),
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_bandit_persistence.py -v`
Expected: PASS（Task 1 4 + Task 3 7 = 11 passed）

- [ ] **Step 5: Commit**

```bash
git add grid_engine/bandit_persistence.py tests/test_bandit_persistence.py
git commit -m "feat: #6 bandit_persistence save/load 核心（原子寫+fsync、arm_signature/schema 守門、失敗一律冷啟動）"
```

---

### Task 4: load 加固 — 不復原瞬時選擇(C) / 非有限值 sanitize(D) / max_age 過期(B)

**Files:**
- Modify: `grid_engine/bandit_persistence.py`（`load_bandit_state` 內加邏輯 + 兩個私有 helper）
- Test: `tests/test_bandit_persistence.py`

**Interfaces:**
- Modifies: `load_bandit_state(bandit, path, max_age_sec=None)` 行為 —
  - 套用前剝掉 `state['current_arm_idx']`/`state['current_context']`（C）
  - 套用前 sanitize：`rewards` 過濾非有限、`thompson_alpha/beta` 非有限重置 1.0、`total_pulls`/`pull_counts` clamp 非負（D）
  - `max_age_sec` 有值且 `saved_at` 距今超過即回 False（B）
- Produces（module-private）：`_is_stale(saved_at, max_age_sec) -> bool`、`_sanitize_state(state) -> None`（就地）。

- [ ] **Step 1: Write the failing test**

在 `tests/test_bandit_persistence.py` 追加（補 import `from indicators.bandit import MarketContext`）：

```python
def test_load_does_not_restore_transient_selection(tmp_path):
    b = _trained_bandit()
    b.current_arm_idx = 7
    b.current_context = MarketContext.HIGH_VOLATILITY
    path = str(tmp_path / "s.json")
    save_bandit_state(b, path)

    b2 = _bandit()
    assert load_bandit_state(b2, path) is True
    assert b2.current_arm_idx == 0                      # 瞬時選擇不復原
    assert b2.current_context == MarketContext.RANGING  # context 不復原
    assert b2.total_pulls == b.total_pulls              # 學到的統計仍復原


def test_load_sanitizes_non_finite(tmp_path):
    b = _trained_bandit()
    path = str(tmp_path / "s.json")
    save_bandit_state(b, path)
    env = json.loads((tmp_path / "s.json").read_text())
    env["state"]["rewards"]["0"] = [float("nan"), 1.0, float("inf"), 2.0]
    env["state"]["thompson_alpha"]["0"] = float("inf")
    (tmp_path / "s.json").write_text(json.dumps(env))

    b2 = _bandit()
    assert load_bandit_state(b2, path) is True
    assert list(b2.rewards[0]) == [1.0, 2.0]
    assert b2.thompson_alpha[0] == 1.0
    assert 0 <= b2.select_arm() < len(b2.arms)  # 無 NaN 傳播、不丟例外


def test_load_clamps_negative_counts(tmp_path):
    b = _trained_bandit()
    path = str(tmp_path / "s.json")
    save_bandit_state(b, path)
    env = json.loads((tmp_path / "s.json").read_text())
    env["state"]["total_pulls"] = -5
    env["state"]["pull_counts"]["0"] = -3
    (tmp_path / "s.json").write_text(json.dumps(env))

    b2 = _bandit()
    assert load_bandit_state(b2, path) is True
    assert b2.total_pulls == 0
    assert b2.pull_counts[0] == 0
    assert 0 <= b2.select_arm() < len(b2.arms)


def test_max_age_expiry(tmp_path):
    b = _trained_bandit()
    path = str(tmp_path / "s.json")
    save_bandit_state(b, path)
    env = json.loads((tmp_path / "s.json").read_text())
    env["saved_at"] = "2000-01-01T00:00:00"
    (tmp_path / "s.json").write_text(json.dumps(env))

    assert load_bandit_state(_bandit(), path, max_age_sec=60) is False   # 過期 → 冷啟動
    assert load_bandit_state(_bandit(), path) is True                    # 未設 max_age → 不因舊而拒


def test_max_age_unparseable_saved_at_is_stale(tmp_path):
    b = _trained_bandit()
    path = str(tmp_path / "s.json")
    save_bandit_state(b, path)
    env = json.loads((tmp_path / "s.json").read_text())
    env["saved_at"] = "not-a-timestamp"
    (tmp_path / "s.json").write_text(json.dumps(env))
    assert load_bandit_state(_bandit(), path, max_age_sec=60) is False  # 無法解析視為過期
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_bandit_persistence.py -k "transient or sanitize or clamp or max_age" -v`
Expected: FAIL（`current_arm_idx == 7` 未被剝、NaN 未過濾、無 max_age 檢查）

- [ ] **Step 3: Write minimal implementation**

在 `grid_engine/bandit_persistence.py` 加兩個 helper（放 `load_bandit_state` 之上）：

```python
def _is_stale(saved_at, max_age_sec: float) -> bool:
    """saved_at 距今是否超過 max_age_sec；無法解析視為過期（保守冷啟動）。"""
    if not isinstance(saved_at, str):
        return True
    try:
        ts = datetime.fromisoformat(saved_at)
    except ValueError:
        return True
    return (datetime.utcnow() - ts).total_seconds() > max_age_sec


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
```

修改 `load_bandit_state` — 把「Task 4 會在此插入」註解那段換成：

```python
    if max_age_sec is not None and _is_stale(envelope.get("saved_at"), max_age_sec):
        logger.warning("[Bandit] 狀態過期（超過 %ss），冷啟動", max_age_sec)
        return False
    state = dict(envelope["state"])
    state.pop("current_arm_idx", None)   # 不復原瞬時選擇：讓 select_arm 在 live data 重選
    state.pop("current_context", None)   # price_history 未持久化，context 重新暖機
    _sanitize_state(state)
    bandit.load_state(state)
    logger.info("[Bandit] 載入狀態 total_pulls=%s", bandit.total_pulls)
    return True
```

（即：刪掉原本 Task 3 的 `state = dict(envelope["state"]); bandit.load_state(state); logger...; return True` 三行，換成上面這段。）

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_bandit_persistence.py -v`
Expected: PASS（Task 1+3+4 全綠）

- [ ] **Step 5: Commit**

```bash
git add grid_engine/bandit_persistence.py tests/test_bandit_persistence.py
git commit -m "feat: #6 load 加固：不復原瞬時選擇、非有限值/負計數 sanitize、max_age 過期冷啟動"
```

---

### Task 5: bot 接線（載入 / 條件存 / stop 收尾）

**Files:**
- Modify: `grid_engine/bot.py`（`__init__` `:108` 後；`run()` `:1059-1065` 區塊；record_trade 段 `:909`；`stop()` `:1098`；新增兩個方法）
- Test: `tests/test_bandit_persistence.py`

**Interfaces:**
- Consumes: `save_bandit_state`/`load_bandit_state`（Task 3/4）、`GlobalConfig.bandit_state_path`/`bandit_state_max_age_sec`（Task 2）。
- Produces（bot instance）：`self._bandit_state_path`、`self._bandit_last_saved_pulls`、`self._persist_bandit_state()`、`self._maybe_persist_bandit_state()`。

- [ ] **Step 1: Write the failing test**

在 `tests/test_bandit_persistence.py` 追加（補 import `import os`、`from grid_engine.bot import MaxGridBot`、`from grid_engine.config import GlobalConfig`）：

```python
def _bot(tmp_path, enabled=True):
    cfg = GlobalConfig()
    cfg.bandit.enabled = enabled
    bot = MaxGridBot(cfg)
    bot._bandit_state_path = str(tmp_path / "bandit_state.json")
    bot._bandit_last_saved_pulls = 0
    return bot


def test_maybe_persist_writes_on_pull_change(tmp_path):
    bot = _bot(tmp_path)
    for _ in range(bot.bandit_optimizer.config.update_interval):
        bot.bandit_optimizer.record_trade(1.0, "long")
    assert bot.bandit_optimizer.total_pulls == 1
    bot._maybe_persist_bandit_state()
    assert os.path.exists(bot._bandit_state_path)
    assert bot._bandit_last_saved_pulls == 1


def test_maybe_persist_noop_when_no_pull_change(tmp_path):
    bot = _bot(tmp_path)
    bot._maybe_persist_bandit_state()  # total_pulls 0 == last 0
    assert not os.path.exists(bot._bandit_state_path)


def test_disabled_bandit_never_writes(tmp_path):
    bot = _bot(tmp_path, enabled=False)
    bot._persist_bandit_state()
    bot._maybe_persist_bandit_state()
    assert not os.path.exists(bot._bandit_state_path)


def test_persist_swallows_errors(tmp_path):
    bot = _bot(tmp_path)
    for _ in range(bot.bandit_optimizer.config.update_interval):
        bot.bandit_optimizer.record_trade(1.0, "long")
    blocker = tmp_path / "blocker"
    blocker.write_text("x")                    # 用檔案當目錄 → makedirs 失敗
    bot._bandit_state_path = str(blocker / "sub" / "state.json")
    bot._persist_bandit_state()                # 不可 raise
    assert not (blocker / "sub").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_bandit_persistence.py -k "persist or disabled" -v`
Expected: FAIL — `AttributeError: 'MaxGridBot' object has no attribute '_maybe_persist_bandit_state'`

- [ ] **Step 3: Write minimal implementation**

在 `grid_engine/bot.py`：

1. `__init__`，`self.bandit_optimizer = UCBBanditOptimizer(config.bandit)`（`:108`）之後加：

```python
        self._bandit_state_path = None
        self._bandit_last_saved_pulls = 0
```

2. 新增兩個方法（放在 `record_trade` 呼叫處所在的方法附近，或緊接其他 helper）：

```python
    def _persist_bandit_state(self):
        """best-effort 存 bandit 狀態；僅 enabled 時存，失敗只 log 不炸主流程。"""
        if not self.config.bandit.enabled:
            return
        path = getattr(self, "_bandit_state_path", None) or "logs/bandit_state.json"
        try:
            from grid_engine.bandit_persistence import save_bandit_state
            save_bandit_state(self.bandit_optimizer, path)
        except Exception as e:
            logger.warning(f"[Bandit] 狀態存檔失敗: {e}")

    def _maybe_persist_bandit_state(self):
        """bandit 評估過（total_pulls 變化）才落地，寫檔次數 = 評估次數。"""
        if self.bandit_optimizer.total_pulls != self._bandit_last_saved_pulls:
            self._persist_bandit_state()
            self._bandit_last_saved_pulls = self.bandit_optimizer.total_pulls
```

3. record_trade 段（`:909`），`self.bandit_optimizer.record_trade(realized_pnl, trade_side)` 之後加一行：

```python
                    self._maybe_persist_bandit_state()
```

4. `run()`，decision log default 區塊（`:1059-1063`）之後、`await self.sync_all()`（`:1065`）之前加：

```python
            if getattr(self, "_bandit_state_path", None) is None:
                self._bandit_state_path = (
                    getattr(self.config, "bandit_state_path", None) or "logs/bandit_state.json"
                )
            if self.config.bandit.enabled:
                from grid_engine.bandit_persistence import load_bandit_state
                if load_bandit_state(
                    self.bandit_optimizer,
                    self._bandit_state_path,
                    getattr(self.config, "bandit_state_max_age_sec", None),
                ):
                    self._bandit_last_saved_pulls = self.bandit_optimizer.total_pulls
```

5. `stop()`（`:1098`），`notify_stop` 的 try/except 區塊之後加 best-effort 收尾：

```python
        self._persist_bandit_state()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_bandit_persistence.py -v`
Expected: PASS（全綠）

- [ ] **Step 5: Commit**

```bash
git add grid_engine/bot.py tests/test_bandit_persistence.py
git commit -m "feat: #6 bot 接線 bandit 持久化（run 載入、每次評估後條件存、stop best-effort 收尾）"
```

---

### Task 6: Monkey testing + replay invariant(F) + 全套回歸

**Files:**
- Test: `tests/test_bandit_persistence.py`（追加）
- Run: 全套測試

**Interfaces:**
- Consumes: 全部前置 task。

- [ ] **Step 1: Write the failing/guard tests**

在 `tests/test_bandit_persistence.py` 追加（補 import `import dataclasses`、`from grid_engine.decision import DecisionInputs`）：

```python
def test_empty_state_dict_loads_as_noop(tmp_path):
    b = _trained_bandit()
    path = str(tmp_path / "s.json")
    save_bandit_state(b, path)
    env = json.loads((tmp_path / "s.json").read_text())
    env["state"] = {}                          # 空 state（load_state 對空 dict 早退）
    (tmp_path / "s.json").write_text(json.dumps(env))
    b2 = _bandit()
    assert load_bandit_state(b2, path) is True  # 不 crash
    assert b2.total_pulls == 0


def test_truncated_json_cold_starts(tmp_path):
    b = _trained_bandit()
    path = str(tmp_path / "s.json")
    save_bandit_state(b, path)
    full = (tmp_path / "s.json").read_text()
    (tmp_path / "s.json").write_text(full[:len(full) // 2])  # 砍一半
    assert load_bandit_state(_bandit(), path) is False


def test_garbage_signature_type_cold_starts(tmp_path):
    b = _trained_bandit()
    path = str(tmp_path / "s.json")
    save_bandit_state(b, path)
    env = json.loads((tmp_path / "s.json").read_text())
    env["arm_signature"] = 12345               # 非字串亂填
    (tmp_path / "s.json").write_text(json.dumps(env))
    assert load_bandit_state(_bandit(), path) is False


def test_bandit_params_present_in_decision_inputs():
    # F: bandit 覆寫的三個參數必須是 DecisionInputs 欄位，才會落進 decisions.jsonl、replay 才吃得到。
    # 守住「#6 不回歸 #4 replay zero-diff」：bandit 只改未來選哪個 arm，每筆決策的參數已凍結在 log。
    fields = {f.name for f in dataclasses.fields(DecisionInputs)}
    assert {"gamma", "grid_spacing", "take_profit_spacing"} <= fields
```

- [ ] **Step 2: Run the new tests**

Run: `uv run pytest tests/test_bandit_persistence.py -k "empty or truncated or garbage or decision_inputs" -v`
Expected: PASS（實作已在 Task 3/4 完成，這些是加固驗證；若任一 FAIL 表示前面 sanitize/守門有洞，回頭補）

- [ ] **Step 3: 全套回歸 + #4 replay 未回歸**

Run: `uv run pytest tests/ -q`
Expected: 全綠（先前 187 passed + 本次新增；replay/decision 測試 `tests/test_replay.py` `tests/test_decision*.py` 必須仍綠 → 證 #6 未破 #4 zero-diff）。記錄實際數量（報數量不報形容詞）。

- [ ] **Step 4: 手動煙霧驗證持久化真的落地/復原**

Run:
```bash
uv run python -c "
from grid_engine.config import GlobalConfig
from grid_engine.bot import MaxGridBot
from grid_engine.bandit_persistence import load_bandit_state
import os, tempfile
d = tempfile.mkdtemp(); p = os.path.join(d, 'bandit_state.json')
cfg = GlobalConfig(); cfg.bandit.enabled = True
bot = MaxGridBot(cfg); bot._bandit_state_path = p; bot._bandit_last_saved_pulls = 0
for _ in range(bot.bandit_optimizer.config.update_interval * 3):
    bot.bandit_optimizer.record_trade(1.0, 'long'); bot._maybe_persist_bandit_state()
saved = bot.bandit_optimizer.total_pulls
assert os.path.exists(p), 'state 未落地'
b2 = MaxGridBot(cfg).bandit_optimizer
assert load_bandit_state(b2, p) is True and b2.total_pulls == saved
print('OK persisted+restored total_pulls=', saved)
"
```
Expected: `OK persisted+restored total_pulls= 3`

- [ ] **Step 5: Commit**

```bash
git add tests/test_bandit_persistence.py
git commit -m "test: #6 monkey（空/截斷/亂填 state）+ replay invariant 守門 + 全套回歸"
```

---

## Self-Review

**1. Spec coverage：**
- arm_signature → Task 1 ✓
- 存檔格式/原子寫+fsync/saved_at → Task 3 ✓
- 事件驅動存檔(total_pulls) + stop 收尾 + enabled gate → Task 5 ✓
- 載入決策樹（缺檔/壞檔/schema/arm_signature） → Task 3 ✓
- C 不復原瞬時選擇 / D 非有限值 sanitize / B max_age → Task 4 ✓
- Config 兩欄 + from_dict 正規化 → Task 2 ✓
- Monkey（唯讀/makedirs 失敗 swallow、空/截斷/亂填、負計數） → Task 4(負計數/唯讀)+Task 6(空/截斷/亂填) ✓
- F replay invariant → Task 6 ✓
- 已知限制（跨-symbol、gamma race、極小 PnL）→ 範圍外，spec 已記，無 task（正確）

**2. Placeholder scan：** 無 TBD/TODO；每個 code step 都有完整程式碼與確切指令。

**3. Type consistency：** `save_bandit_state(bandit, path)` / `load_bandit_state(bandit, path, max_age_sec=None) -> bool` / `arm_signature() -> str` / `_persist_bandit_state` / `_maybe_persist_bandit_state` / `_bandit_state_path` / `_bandit_last_saved_pulls` / `_parse_bandit_state_max_age` 跨 task 命名一致。`SCHEMA_VERSION` 常數一致。

**驗收（全 task 完成後）：** dual-review 兩輪 + verifier fresh-context read-back。
