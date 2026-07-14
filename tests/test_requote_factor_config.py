from grid_engine.config import GlobalConfig


def test_default():
    assert GlobalConfig().requote_threshold_factor == 0.5


def test_roundtrip():
    cfg = GlobalConfig(requote_threshold_factor=1.0)
    assert GlobalConfig.from_dict(cfg.to_dict()).requote_threshold_factor == 1.0


def test_missing_key_falls_back():
    d = GlobalConfig().to_dict(); d.pop("requote_threshold_factor", None)
    assert GlobalConfig.from_dict(d).requote_threshold_factor == 0.5


def test_garbage_falls_back():
    for garbage in ("abc", float("nan"), float("inf"), -1.0, 0.0, 11.0, None):
        d = GlobalConfig().to_dict(); d["requote_threshold_factor"] = garbage
        assert GlobalConfig.from_dict(d).requote_threshold_factor == 0.5, garbage
