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


def test_engine_save_drops_legacy_leverage_key(tmp_path, monkeypatch):
    """GlobalConfig.save() 路徑：舊 key 必須被實際移除。

    config.py:255 硬寫 CONFIG_FILE（→ config/trading_config_max.json），
    而實盤引擎正在讀寫該檔 —— 必須 monkeypatch 隔離，禁止寫生產檔。
    """
    import json
    from grid_engine.config import GlobalConfig
    from grid_engine.config_io import load_raw

    p = tmp_path / "trading_config_max.json"
    p.write_text(json.dumps({
        "symbols": {"X/USDC:USDC": {"leverage": 20, "initial_quantity": 1}}}))
    # GlobalConfig.load() 無 path 參數，直接讀模組層 CONFIG_FILE
    # （沿用 tests/test_config_save.py:20 既有 pattern）
    monkeypatch.setattr("grid_engine.config.CONFIG_FILE", p)

    config = GlobalConfig.load()
    config.save()

    sym = load_raw(p)["symbols"]["X/USDC:USDC"]
    assert "leverage" not in sym
    assert "assumed_leverage" in sym
