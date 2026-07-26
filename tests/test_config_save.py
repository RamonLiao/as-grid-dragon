"""GlobalConfig.save() delegate 正確性。monkeypatch CONFIG_FILE，不碰真實 config。"""
import json

from grid_engine.config import GlobalConfig, SymbolConfig


def _seed(path):
    path.write_text(json.dumps({
        "api_key": "k",
        "exchange_type": "binance",              # engine 不認識的舊欄位
        "symbols": {"XRP/USDC:USDC": {
            "symbol": "XRPUSDC", "ccxt_symbol": "XRP/USDC:USDC",
            "leverage": 20, "trading_mode": "swing",  # engine 不認識
        }},
    }, indent=2), encoding="utf-8")


def test_save_preserves_unknown_and_atomic(tmp_path, monkeypatch):
    cfg = tmp_path / "trading_config_max.json"
    _seed(cfg)
    monkeypatch.setattr("grid_engine.config.CONFIG_FILE", cfg)

    config = GlobalConfig.load()               # 讀 CONFIG_FILE(=tmp)
    config.symbols["XRP/USDC:USDC"].assumed_leverage = 25
    config.save()

    raw = json.loads(cfg.read_text())
    assert raw["symbols"]["XRP/USDC:USDC"]["assumed_leverage"] == 25   # 編輯生效
    assert raw["symbols"]["XRP/USDC:USDC"]["trading_mode"] == "swing"   # 未知欄位保留
    assert raw["exchange_type"] == "binance"                            # top-level 保留
    assert list(tmp_path.glob("trading_config_max.json.tmp*")) == []    # 無 tmp 殘留


def test_save_drops_removed_symbol(tmp_path, monkeypatch):
    cfg = tmp_path / "trading_config_max.json"
    _seed(cfg)
    monkeypatch.setattr("grid_engine.config.CONFIG_FILE", cfg)
    config = GlobalConfig.load()
    config.symbols["BNB/USDC:USDC"] = SymbolConfig(
        symbol="BNBUSDC", ccxt_symbol="BNB/USDC:USDC")
    del config.symbols["XRP/USDC:USDC"]
    config.save()
    raw = json.loads(cfg.read_text())
    assert "BNB/USDC:USDC" in raw["symbols"]
    assert "XRP/USDC:USDC" not in raw["symbols"]
