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
