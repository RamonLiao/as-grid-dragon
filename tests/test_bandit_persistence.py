import json
from indicators.bandit import UCBBanditOptimizer, ParameterArm
from grid_engine.enhancements import BanditConfig
from grid_engine.bandit_persistence import save_bandit_state, load_bandit_state, SCHEMA_VERSION


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
