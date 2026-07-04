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
