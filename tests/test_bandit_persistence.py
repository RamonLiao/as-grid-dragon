import json
import os
import dataclasses
from grid_engine.enhancements import UCBBanditOptimizer, ParameterArm, MarketContext, BanditConfig
from grid_engine.bandit_persistence import save_bandit_state, load_bandit_state, SCHEMA_VERSION
from grid_engine.bot import MaxGridBot
from grid_engine.config import GlobalConfig
from grid_engine.decision import DecisionInputs


def _bandit():
    return UCBBanditOptimizer(BanditConfig(enabled=True, cold_start_enabled=False))


def _trained_bandit(updates=3):
    b = _bandit()
    n = b.config.update_interval * updates
    for i in range(n):
        b.record_trade(1.0 if i % 2 else -0.5, "long" if i % 2 else "short")
    return b


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


def test_corrupted_pull_counts_non_numeric_key_cold_starts(tmp_path):
    """pull_counts 包含非數字 key 時，load_bandit_state 應回 False 不 raise。"""
    b = _trained_bandit()
    path = str(tmp_path / "s.json")
    save_bandit_state(b, path)
    env = json.loads((tmp_path / "s.json").read_text())
    # 竄改 pull_counts，加入非數字 key
    env["state"]["pull_counts"]["abc"] = 3
    (tmp_path / "s.json").write_text(json.dumps(env))

    b2 = _bandit()
    # 呼叫不應 raise，應回 False；且 bandit 仍可用（select_arm 不拋例外）
    result = load_bandit_state(b2, path)
    assert result is False  # 應回 False（資料損毀，冷啟動）
    assert 0 <= b2.select_arm() < len(b2.arms)  # 損毀後仍可安全使用


def _bot(tmp_path, enabled=True):
    cfg = GlobalConfig()
    cfg.bandit.enabled = enabled
    cfg.bandit.cold_start_enabled = False  # 冷啟動會預載 pulls，干擾 total_pulls 斷言
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


def test_corrupted_rewards_non_list_value_cold_starts(tmp_path):
    """rewards 包含非 list value 時，load_bandit_state 應回 False 不 raise。"""
    b = _trained_bandit()
    path = str(tmp_path / "s.json")
    save_bandit_state(b, path)
    env = json.loads((tmp_path / "s.json").read_text())
    # 竄改 rewards，某個 value 改成整數而非 list
    env["state"]["rewards"]["0"] = 5
    (tmp_path / "s.json").write_text(json.dumps(env))

    b2 = _bandit()
    # 呼叫不應 raise，應回 False；且 bandit 仍可用（select_arm 不拋例外）
    result = load_bandit_state(b2, path)
    assert result is False  # 應回 False（資料損毀，冷啟動）
    assert 0 <= b2.select_arm() < len(b2.arms)  # 損毀後仍可安全使用


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
    assert 0 <= b2.select_arm() < len(b2.arms)  # 空 state 載入後 select_arm 不得 raise（pull_counts 需 backfill）


def test_partial_pull_counts_no_keyerror(tmp_path):
    """pull_counts 只剩部分 arm key（部分寫入/損毀但仍合法 JSON）時，
    load_state 整表取代 pull_counts → 後續 select_arm() 對缺失 index KeyError 炸 async 迴圈。
    persistence 層須 backfill 缺失 index 為 0。"""
    b = _trained_bandit()
    path = str(tmp_path / "s.json")
    save_bandit_state(b, path)
    env = json.loads((tmp_path / "s.json").read_text())
    env["state"]["pull_counts"] = {"0": 3}     # 只保留單一 arm key
    (tmp_path / "s.json").write_text(json.dumps(env))

    b2 = _bandit()
    assert load_bandit_state(b2, path) is True
    assert 0 <= b2.select_arm() < len(b2.arms)          # 不得 KeyError
    assert set(b2.pull_counts) >= set(range(len(b2.arms)))  # 涵蓋所有 arm index


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


def test_load_resets_nonpositive_thompson_priors(tmp_path):
    """thompson_alpha/beta 有限但 <=0（竄改/部分寫壞）→ 重置為先驗 1.0。
    np.random.beta 要求 a>0 且 b>0，非正值會在 select_arm 走 Thompson 分支時 raise ValueError 炸 async 迴圈。"""
    b = _trained_bandit()
    path = str(tmp_path / "s.json")
    save_bandit_state(b, path)
    env = json.loads((tmp_path / "s.json").read_text())
    env["state"]["thompson_alpha"]["0"] = -5.0
    env["state"]["thompson_beta"]["1"] = 0.0
    (tmp_path / "s.json").write_text(json.dumps(env))

    b2 = _bandit()
    assert load_bandit_state(b2, path) is True
    assert b2.thompson_alpha[0] == 1.0   # 負值被重置為先驗
    assert b2.thompson_beta[1] == 1.0    # 0 被重置為先驗


def test_load_poisoned_priors_select_arm_no_crash(tmp_path):
    """載入非正 thompson 先驗後，反覆 select_arm() 走 Thompson 分支不得 raise（防 async 迴圈 crash 回歸）。"""
    b = _trained_bandit()
    path = str(tmp_path / "s.json")
    save_bandit_state(b, path)
    env = json.loads((tmp_path / "s.json").read_text())
    env["state"]["thompson_alpha"]["0"] = -5.0
    env["state"]["thompson_beta"]["1"] = 0.0
    (tmp_path / "s.json").write_text(json.dumps(env))

    b2 = _bandit()
    assert load_bandit_state(b2, path) is True
    for _ in range(30):
        idx = b2.select_arm()
        assert 0 <= idx < len(b2.arms)


def test_bandit_params_present_in_decision_inputs():
    # F: bandit 覆寫的三個參數必須是 DecisionInputs 欄位，才會落進 decisions.jsonl、replay 才吃得到。
    # 守住「#6 不回歸 #4 replay zero-diff」：bandit 只改未來選哪個 arm，每筆決策的參數已凍結在 log。
    fields = {f.name for f in dataclasses.fields(DecisionInputs)}
    assert {"gamma", "grid_spacing", "take_profit_spacing"} <= fields
