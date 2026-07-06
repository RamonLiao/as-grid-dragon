"""config_store merge-preserve 測試。

為什麼重要：config/trading_config_max.json 是生產引擎讀的檔。
grid_engine schema 缺 trading_mode/hard_stop 等欄位，naive to_dict 覆寫
會靜默抹掉它們（trading_mode 丟了 → 頁3 優化參數範圍錯）。
"""
import json
import pytest
from pathlib import Path

from web.services import config_store
from grid_engine.config import GlobalConfig

SAMPLE = {
    "api_key": "k", "api_secret": "s",
    "exchange_type": "binance",       # 舊 schema 欄位，engine 不認識
    "testnet": False,                  # 舊 schema 欄位
    "symbols": {
        "XRP/USDC:USDC": {
            "symbol": "XRPUSDC", "ccxt_symbol": "XRP/USDC:USDC",
            "enabled": True, "take_profit_spacing": 0.004,
            "grid_spacing": 0.006, "initial_quantity": 3.0,
            "leverage": 20, "limit_multiplier": 5.0,
            "threshold_multiplier": 20.0,
            "trading_mode": "swing",   # engine schema 沒有此欄位
        }
    },
    "risk": {
        "enabled": True, "margin_threshold": 0.5,
        "hard_stop_enabled": True,          # engine RiskConfig 沒有
        "max_loss_pct": 0.1,                # engine RiskConfig 沒有
        "max_position_loss_pct": 0.05,      # engine RiskConfig 沒有
    },
}


@pytest.fixture
def cfg_file(tmp_path):
    p = tmp_path / "trading_config_max.json"
    p.write_text(json.dumps(SAMPLE, indent=2))
    return p


def test_load_config_parses_engine_fields(cfg_file):
    config = config_store.load_config(path=cfg_file)
    assert isinstance(config, GlobalConfig)
    assert "XRP/USDC:USDC" in config.symbols
    assert config.symbols["XRP/USDC:USDC"].take_profit_spacing == 0.004


def test_get_symbol_extra_reads_trading_mode(cfg_file):
    assert config_store.get_symbol_extra(
        "XRP/USDC:USDC", "trading_mode", path=cfg_file) == "swing"
    assert config_store.get_symbol_extra(
        "XRP/USDC:USDC", "nonexistent", default="d", path=cfg_file) == "d"


def test_save_preserves_unknown_fields(cfg_file):
    """核心保證：engine schema 沒有的欄位，存檔後原樣保留。"""
    config = config_store.load_config(path=cfg_file)
    config.symbols["XRP/USDC:USDC"].leverage = 25  # 模擬頁2 編輯
    config_store.save_config(config, path=cfg_file)

    raw = json.loads(cfg_file.read_text())
    assert raw["symbols"]["XRP/USDC:USDC"]["leverage"] == 25          # 編輯生效
    assert raw["symbols"]["XRP/USDC:USDC"]["trading_mode"] == "swing"  # 未知欄位保留
    assert raw["exchange_type"] == "binance"                            # top-level 保留
    assert raw["testnet"] is False
    assert raw["risk"]["hard_stop_enabled"] is True                     # risk 未知欄位保留
    assert raw["risk"]["max_loss_pct"] == 0.1
    assert raw["risk"]["max_position_loss_pct"] == 0.05


def test_save_applies_symbol_extras(cfg_file):
    """頁2 編輯 trading_mode 走 extras 通道。"""
    config = config_store.load_config(path=cfg_file)
    config_store.save_config(
        config,
        symbol_extras={"XRP/USDC:USDC": {"trading_mode": "high_freq"}},
        path=cfg_file)
    raw = json.loads(cfg_file.read_text())
    assert raw["symbols"]["XRP/USDC:USDC"]["trading_mode"] == "high_freq"


def test_save_new_symbol_and_removed_symbol(cfg_file):
    """新增 symbol 進檔；config 移除的 symbol 從檔案消失（刪除是有意操作）。"""
    from grid_engine.config import SymbolConfig
    config = config_store.load_config(path=cfg_file)
    config.symbols["BNB/USDC:USDC"] = SymbolConfig(
        symbol="BNBUSDC", ccxt_symbol="BNB/USDC:USDC")
    del config.symbols["XRP/USDC:USDC"]
    config_store.save_config(config, path=cfg_file)
    raw = json.loads(cfg_file.read_text())
    assert "BNB/USDC:USDC" in raw["symbols"]
    assert "XRP/USDC:USDC" not in raw["symbols"]


def test_save_creates_one_time_backup(cfg_file):
    """首次存檔前建 .bak-pre-web-migration 備份，之後不再覆蓋。"""
    config = config_store.load_config(path=cfg_file)
    config_store.save_config(config, path=cfg_file)
    bak = cfg_file.with_name(cfg_file.name + ".bak-pre-web-migration")
    assert bak.exists()
    original = json.loads(bak.read_text())
    assert original["symbols"]["XRP/USDC:USDC"]["leverage"] == 20

    # 二次存檔改值，備份不變
    config.symbols["XRP/USDC:USDC"].leverage = 30
    config_store.save_config(config, path=cfg_file)
    assert json.loads(bak.read_text())["symbols"]["XRP/USDC:USDC"]["leverage"] == 20


def test_roundtrip_real_config_no_field_loss():
    """用 repo 現況 JSON 實測 round-trip 零欄位遺失（遞迴比對 key 集合）。"""
    real = Path(__file__).resolve().parents[2] / "config" / "trading_config_max.json"
    if not real.exists():
        pytest.skip("no real config")
    import shutil, tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "trading_config_max.json"
        shutil.copy(real, p)
        before = json.loads(p.read_text())
        config = config_store.load_config(path=p)
        config_store.save_config(config, path=p)
        after = json.loads(p.read_text())

        def keys_recursive(d, prefix=""):
            out = set()
            for k, v in d.items():
                out.add(f"{prefix}{k}")
                if isinstance(v, dict):
                    out |= keys_recursive(v, f"{prefix}{k}.")
            return out

        missing = keys_recursive(before) - keys_recursive(after)
        assert missing == set(), f"存檔遺失欄位: {missing}"
