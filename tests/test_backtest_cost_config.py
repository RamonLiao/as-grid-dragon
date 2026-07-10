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


def test_default_fee_is_maker_not_taker():
    """網格全是限價 maker 單。taker 費率會對高換手選項系統性多罰一倍。

    Binance USDⓈ-M VIP0：maker 0.02% = 0.0002，taker 0.05%。
    見 spec 缺口 G7。
    """
    from backtest.config import Config
    assert Config(symbol="BNBUSDC").fee_pct == 0.0002


def test_default_maintenance_margin_rate():
    from backtest.config import Config
    assert Config(symbol="BNBUSDC").maintenance_margin_rate == 0.005


def test_from_dict_defaults_match_constructor_defaults():
    """from_dict 的每個缺鍵 fallback 必須等於對應 dataclass field default。

    預設值被寫了兩次：dataclass field default 一次、from_dict 裡
    data.get(key, <literal>) 的字面值一次。兩者之間沒有任何機制保持同步，
    改一個忘另一個是必然（Task 6 實測：fee_pct field default 改成 maker
    0.0002 後，from_dict 的字面值仍停在 taker 0.0004，Config.load 任何缺
    fee_pct 鍵的舊 JSON 都會拿到錯的 taker 費率）。這條測試就是那個機制。
    """
    import dataclasses
    from backtest.config import Config

    ctor = Config(symbol="X")
    from_json = Config.from_dict({"symbol": "X"})

    mismatches = []
    for f in dataclasses.fields(Config):
        cv = getattr(ctor, f.name)
        jv = getattr(from_json, f.name)
        if cv != jv:
            mismatches.append(f"{f.name}: ctor={cv!r} from_dict={jv!r}")

    assert not mismatches, (
        "from_dict fallback 與 constructor default 漂移:\n  "
        + "\n  ".join(mismatches)
    )
