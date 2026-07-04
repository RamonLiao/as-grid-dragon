from backtest.config import Config


def test_defaults_fidelity_first():
    c = Config()
    assert c.slippage_bps == 0.0001
    assert c.funding_enabled is True


def test_roundtrip_preserves_cost_fields():
    c = Config(slippage_bps=0.0003, funding_enabled=False)
    d = c.to_dict()
    assert d["slippage_bps"] == 0.0003
    assert d["funding_enabled"] is False
    c2 = Config.from_dict(d)
    assert c2.slippage_bps == 0.0003
    assert c2.funding_enabled is False


def test_backward_compat_missing_fields_use_defaults():
    # 舊 config 無這兩欄 → 套 fidelity-first 預設
    c = Config.from_dict({"symbol": "BTCUSDC"})
    assert c.slippage_bps == 0.0001
    assert c.funding_enabled is True


def test_from_dict_negative_slippage_fallback():
    assert Config.from_dict({"slippage_bps": -0.5}).slippage_bps == 0.0001


def test_from_dict_nan_slippage_fallback():
    assert Config.from_dict({"slippage_bps": float("nan")}).slippage_bps == 0.0001


def test_from_dict_non_bool_funding_enabled_fallback():
    assert Config.from_dict({"funding_enabled": "yes"}).funding_enabled is True
