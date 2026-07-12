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

    注意：此測試僅驗「字面值漂移」（data.get 的 fallback 字面值與 field default 差異），
    不驗「欄位是否實際被 to_dict()／from_dict() 接線」。後者由
    test_every_dataclass_field_survives_to_dict_from_dict_roundtrip 負責。
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


# Phase A 待刪欄位（spec §5.3）：dead_mode_fallback_long / dead_mode_fallback_short
# 純層硬編 1.05 / 0.95，全 repo 無人讀取，後續版本會移除。
# 本允許清單刪除後必須同步移除 _PENDING_DELETION_SPEC_5_3 常數。
_PENDING_DELETION_SPEC_5_3 = frozenset([
    "dead_mode_fallback_long",
    "dead_mode_fallback_short",
])


def test_every_dataclass_field_survives_to_dict_from_dict_roundtrip():
    """每個 dataclass 欄位必須存活 to_dict()→from_dict() roundtrip。

    舊 parity 測試（test_from_dict_defaults_match_constructor_defaults）
    只驗「字面值漂移」，結構上盲於「某欄位根本沒被 to_dict() 寫出」的 bug。
    未接線欄位在兩邊都回落到 dataclass default，因此恆等、測試恆綠。

    本測試對每個欄位塞一個「明確不同於 default」的值，測試 roundtrip 後
    是否還原成原值。允許清單 _PENDING_DELETION_SPEC_5_3 豁免檢查
    （Phase A 刪除這些欄位後必須同步移除此豁免清單）。
    """
    import dataclasses
    import math
    from backtest.config import Config

    for field in dataclasses.fields(Config):
        fname = field.name

        # 略過豁免清單
        if fname in _PENDING_DELETION_SPEC_5_3:
            continue

        # 為每個欄位生成「與 default 不同」的值
        if field.type == bool:
            test_val = not field.default  # 反轉布爾值
        elif field.type in (int,):
            test_val = field.default + 7
        elif field.type in (float,):
            test_val = field.default * 2.0 + 0.5
        elif field.type == str:
            test_val = str(field.default) + "_test_suffix"
        else:
            # 跳過無法生成測試值的欄位
            continue

        # 建立 Config 並塞入測試值
        kwargs = {"symbol": "TEST"}
        kwargs[fname] = test_val
        cfg = Config(**kwargs)

        # roundtrip：to_dict() → from_dict()
        d = cfg.to_dict()
        cfg2 = Config.from_dict(d)

        # 驗證還原
        result_val = getattr(cfg2, fname)
        if isinstance(test_val, float) and isinstance(result_val, float):
            # 浮點比較允許微小誤差（normalization/rounding）
            assert math.isclose(result_val, test_val, rel_tol=1e-9), (
                f"欄位 {fname} roundtrip 遺失：原={test_val!r} → 還原={result_val!r}"
            )
        else:
            assert result_val == test_val, (
                f"欄位 {fname} roundtrip 遺失：原={test_val!r} → 還原={result_val!r}"
            )
