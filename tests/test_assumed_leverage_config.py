"""`assumed_leverage` 值域守衛。

為什麼這行為重要：此值不推送交易所，只餵回測的保證金/強平計算。
垃圾值不報錯而是流進 runtime 的後果是**回測靜默算錯風險**——實測 `-5`
會跑完 113 筆交易吐出 +0.155% 正報酬（保證金數學全錯但不報錯），
`0` 觸發 divide-by-zero 後靜默回 0 筆。

守衛掛在 `SymbolConfig.__setattr__` 而非 `from_dict`：web 有三個寫入點
（`2_⚙️_交易對管理.py:194,306`、`3_🔬_回測優化.py:214`）直接建構/賦值，
只靠 UI clamp 撐著；dataclass `__init__` 也走 setattr ⇒ `__setattr__`
是唯一涵蓋全部路徑的咽喉點。
"""
import json

from grid_engine.config import SymbolConfig


def _cfg(value):
    d = SymbolConfig().to_dict()
    d["assumed_leverage"] = value
    return SymbolConfig.from_dict(d)


def test_valid_values_pass_through():
    for v in (1, 5, 20, 125):
        assert _cfg(v).assumed_leverage == v, v


def test_numeric_strings_and_integral_floats_normalize_to_int():
    for v in ("20", 20.0):
        got = _cfg(v).assumed_leverage
        assert got == 20 and isinstance(got, int), (v, type(got))


def test_out_of_range_and_garbage_falls_back_to_default():
    garbage = (
        -5,            # 負槓桿：實測會吐出假的正報酬
        0,             # divide-by-zero → 靜默 0 筆
        126,           # 超過交易所上限
        float("nan"),  # 非嚴格 JSON 寫得出來
        float("inf"),
        "abc",
        None,
        True,          # bool 是 int 子類，float(True)==1.0 會矇混成 1x
        [5],
    )
    for v in garbage:
        assert _cfg(v).assumed_leverage == 5, v


def test_non_integer_is_rejected_not_truncated():
    """截斷值必須 ≠ fallback 5，否則「拿掉 is_integer() 檢查」這個回歸抓不到。

    5.7 當測資是不夠的：int(5.7)==5 恰好等於 fallback，靜默截斷與正確拒絕
    在結果上無法區分（verifier 實測此 mutation 存活）。
    """
    for v, truncated in ((7.3, 7), (20.9, 20), (-0.5, 0)):
        assert _cfg(v).assumed_leverage == 5, v
        assert _cfg(v).assumed_leverage != truncated, v


def test_missing_key_uses_dataclass_default():
    d = SymbolConfig().to_dict()
    d.pop("assumed_leverage", None)
    assert SymbolConfig.from_dict(d).assumed_leverage == 5


def test_normalized_value_survives_json_roundtrip():
    """回退值必須是嚴格 JSON 寫得出來的——nan 進來不能 nan 出去。"""
    cfg = _cfg(float("nan"))
    text = json.dumps(cfg.to_dict(), allow_nan=False)
    assert json.loads(text)["assumed_leverage"] == 5


def test_legacy_leverage_key_carrying_garbage_is_also_guarded():
    """舊 key `leverage` 遷移過來的非法值一樣要擋。

    生產 config 四個 symbol 都經歷過 leverage → assumed_leverage 遷移，
    手動編輯舊 key 是真實可及的路徑。
    """
    for v in (-5, 0, 999, float("nan"), "abc"):
        d = SymbolConfig().to_dict()
        d.pop("assumed_leverage", None)
        d["leverage"] = v
        assert SymbolConfig.from_dict(d).assumed_leverage == 5, v


def test_direct_construction_is_guarded():
    """web/pages/2 是直接建構 dataclass，不走 from_dict。"""
    assert SymbolConfig(assumed_leverage=-5).assumed_leverage == 5
    assert SymbolConfig(assumed_leverage=20).assumed_leverage == 20


def test_direct_assignment_is_guarded():
    """web/pages/2:306 與 web/pages/3:214 是直接賦值，不走 from_dict。"""
    cfg = SymbolConfig()
    cfg.assumed_leverage = 0
    assert cfg.assumed_leverage == 5
    cfg.assumed_leverage = 25
    assert cfg.assumed_leverage == 25


def test_float_raising_arbitrary_exception_falls_back():
    """`__float__` 可以拋任何東西，不只 TypeError/ValueError。

    沒有這條，`except Exception` 收窄回 `except (TypeError, ValueError)`
    的重構會全綠通過（verifier 第二輪實測此 mutation 存活）——
    讓「配置有垃圾值」變成一個看起來無關的 crash。
    """
    class Evil:
        def __float__(self):
            raise KeyError("x")

    assert _cfg(Evil()).assumed_leverage == 5


def test_from_dict_does_not_mutate_caller_dict():
    d = SymbolConfig().to_dict()
    d["assumed_leverage"] = -5
    SymbolConfig.from_dict(d)
    assert d["assumed_leverage"] == -5
