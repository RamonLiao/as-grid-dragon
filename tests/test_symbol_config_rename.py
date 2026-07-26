"""assumed_leverage 改名與舊名攔截的驗收。

背景：leverage 是假旋鈕——實盤路徑不讀、從未推送交易所（實測 5x，config 寫 20），
唯一實效是餵回測。改名讓它在 grep 當下就自曝語意。
"""
import copy
import dataclasses

import pytest

from grid_engine.config import SymbolConfig


def test_from_dict_accepts_old_key():
    cfg = SymbolConfig.from_dict({"leverage": 5})
    assert cfg.assumed_leverage == 5


def test_from_dict_new_key_wins_over_old():
    cfg = SymbolConfig.from_dict({"assumed_leverage": 7, "leverage": 5})
    assert cfg.assumed_leverage == 7


def test_from_dict_default_when_absent():
    assert SymbolConfig.from_dict({}).assumed_leverage == 20


def test_to_dict_emits_new_key_only():
    d = SymbolConfig(assumed_leverage=5).to_dict()
    assert d["assumed_leverage"] == 5
    assert "leverage" not in d


def test_reading_old_name_raises():
    cfg = SymbolConfig()
    with pytest.raises(AttributeError, match="assumed_leverage"):
        _ = cfg.leverage


def test_writing_old_name_raises_and_does_not_pollute():
    """__getattr__ 只攔讀不攔寫。少了 __setattr__ 時 cfg.leverage = 20 會靜默
    建立實例屬性：之後讀取成功、to_dict() 忽略它 ⇒ UI 顯示成功、存檔沒有、
    回測沒用到 —— 正是本任務要消滅的假旋鈕的複刻。"""
    cfg = SymbolConfig(assumed_leverage=5)
    with pytest.raises(AttributeError, match="assumed_leverage"):
        cfg.leverage = 20
    assert "leverage" not in cfg.to_dict()
    assert cfg.assumed_leverage == 5


def test_constructor_rejects_old_kwarg():
    SymbolConfig(assumed_leverage=5)          # 不拋
    with pytest.raises(TypeError):
        SymbolConfig(leverage=5)


def test_other_missing_attributes_keep_native_behaviour():
    cfg = SymbolConfig()
    with pytest.raises(AttributeError, match="nonexistent_field"):
        _ = cfg.nonexistent_field


def test_legal_field_assignment_still_works():
    cfg = SymbolConfig()
    cfg.assumed_leverage = 9
    cfg.grid_spacing = 0.005
    assert cfg.assumed_leverage == 9 and cfg.grid_spacing == 0.005


def test_asdict_and_deepcopy_do_not_raise_non_attribute_errors():
    cfg = SymbolConfig(assumed_leverage=5)
    assert dataclasses.asdict(cfg)["assumed_leverage"] == 5
    assert copy.deepcopy(cfg).assumed_leverage == 5


def test_property_internal_error_is_not_rewritten_as_rename_message():
    """SymbolConfig 有 5 個 @property。若 property 內部拋 AttributeError，
    Python 會改而呼叫 __getattr__ —— fallback 若寫死改名訊息，會把
    「coin_name 出錯」誤報成「leverage 已改名」，指向完全錯誤的方向。

    已實測的 Python 語意（不要期待更多）：原始例外**完全丟棄**——
    外層只剩 fallback 訊息，`__context__` 與 `__cause__` 皆為 None
    （__getattr__ 由 slot 機制在 except 區塊之外呼叫，無隱式串接）。
    所以本測試只能斷言「沒有誤報成改名」+「有指出正確的屬性名」，
    **無法**斷言原始的 .split 錯誤還在。見 plan §誠實揭露第 4 點。
    """
    cfg = SymbolConfig()
    cfg.ccxt_symbol = 12345          # 非字串 → coin_name 的 .split 會炸
    with pytest.raises(AttributeError) as ei:
        _ = cfg.coin_name
    assert "assumed_leverage" not in str(ei.value)
    assert "coin_name" in str(ei.value)
