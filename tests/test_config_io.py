"""config_io 共用底層測試（merge-preserve + 原子寫）。全走 tmp_path，不碰真實 config。"""
import json
import os
from pathlib import Path

import pytest

from grid_engine import config_io


def _write(p, data):
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def test_load_raw_missing_returns_empty(tmp_path):
    assert config_io.load_raw(tmp_path / "nope.json") == {}


def test_load_raw_corrupt_raises(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        config_io.load_raw(p)


def test_merge_preserves_unknown_top_level():
    raw = {"exchange_type": "binance", "api_key": "old"}
    new = {"api_key": "new"}
    merged = config_io.merge_preserve(raw, new)
    assert merged["exchange_type"] == "binance"  # 未知 top-level 保留
    assert merged["api_key"] == "new"            # 已知欄位覆寫


def test_merge_preserves_symbol_unknown_key():
    raw = {"symbols": {"X/USDC:USDC": {"leverage": 20, "trading_mode": "swing"}}}
    new = {"symbols": {"X/USDC:USDC": {"leverage": 25}}}
    merged = config_io.merge_preserve(raw, new)
    sym = merged["symbols"]["X/USDC:USDC"]
    assert sym["leverage"] == 25             # engine 欄位覆寫
    assert sym["trading_mode"] == "swing"    # 未知欄位保留


def test_merge_drops_removed_symbol():
    raw = {"symbols": {"A/USDC:USDC": {"leverage": 1}, "B/USDC:USDC": {"leverage": 2}}}
    new = {"symbols": {"A/USDC:USDC": {"leverage": 1}}}
    merged = config_io.merge_preserve(raw, new)
    assert "B/USDC:USDC" not in merged["symbols"]  # config 已刪的消失


def test_merge_nested_dict_field_level():
    raw = {"risk": {"enabled": True, "hard_stop_enabled": True, "max_loss_pct": 0.1}}
    new = {"risk": {"enabled": False}}
    merged = config_io.merge_preserve(raw, new)
    assert merged["risk"]["enabled"] is False        # 覆寫
    assert merged["risk"]["hard_stop_enabled"] is True  # 未知保留
    assert merged["risk"]["max_loss_pct"] == 0.1


def test_symbol_extras_overlay():
    raw = {"symbols": {"X/USDC:USDC": {"leverage": 20}}}
    new = {"symbols": {"X/USDC:USDC": {"leverage": 20}}}
    merged = config_io.merge_preserve(
        raw, new, symbol_extras={"X/USDC:USDC": {"trading_mode": "high_freq"}})
    assert merged["symbols"]["X/USDC:USDC"]["trading_mode"] == "high_freq"


def test_merge_preserve_pure_no_raw_mutation_with_extras():
    """new 不含 symbols + symbol_extras 命中 raw-only symbol 時，raw 不可被原地變異。"""
    raw = {"symbols": {"X/USDC:USDC": {"leverage": 20}}}
    new = {"api_key": "k"}  # 不含 symbols
    merged = config_io.merge_preserve(
        raw, new, symbol_extras={"X/USDC:USDC": {"trading_mode": "swing"}})
    assert merged["symbols"]["X/USDC:USDC"]["trading_mode"] == "swing"   # extras 生效
    assert "trading_mode" not in raw["symbols"]["X/USDC:USDC"]            # raw 未被污染


def test_atomic_write_no_tmp_residue(tmp_path):
    p = tmp_path / "trading_config_max.json"
    config_io._atomic_write_json(p, {"a": 1})
    assert json.loads(p.read_text()) == {"a": 1}
    residue = list(tmp_path.glob("trading_config_max.json.tmp*"))
    assert residue == [], f"tmp 殘留: {residue}"


def test_atomic_write_failure_keeps_original(tmp_path, monkeypatch):
    p = tmp_path / "trading_config_max.json"
    _write(p, {"orig": True})

    def boom(*a, **k):
        raise IOError("disk full")
    monkeypatch.setattr(config_io.json, "dump", boom)
    with pytest.raises(IOError):
        config_io._atomic_write_json(p, {"new": True})

    assert json.loads(p.read_text()) == {"orig": True}          # 原檔完好
    assert list(tmp_path.glob("*.tmp*")) == []                   # tmp 清乾淨


def test_merge_preserve_save_first_time_creates(tmp_path):
    p = tmp_path / "trading_config_max.json"
    config_io.merge_preserve_save(p, {"api_key": "k", "symbols": {}})
    assert json.loads(p.read_text())["api_key"] == "k"


def test_merge_preserve_save_preserves_and_backs_up(tmp_path):
    p = tmp_path / "trading_config_max.json"
    _write(p, {"exchange_type": "binance",
               "symbols": {"X/USDC:USDC": {"leverage": 20, "trading_mode": "swing"}}})
    config_io.merge_preserve_save(
        p, {"symbols": {"X/USDC:USDC": {"leverage": 25}}}, ensure_backup=True)
    raw = json.loads(p.read_text())
    assert raw["symbols"]["X/USDC:USDC"]["leverage"] == 25
    assert raw["symbols"]["X/USDC:USDC"]["trading_mode"] == "swing"
    assert raw["exchange_type"] == "binance"
    bak = p.with_name(p.name + config_io.BACKUP_SUFFIX)
    assert json.loads(bak.read_text())["symbols"]["X/USDC:USDC"]["leverage"] == 20


def test_ensure_backup_once(tmp_path):
    p = tmp_path / "trading_config_max.json"
    _write(p, {"v": 1})
    config_io.merge_preserve_save(p, {"v": 2}, ensure_backup=True)
    config_io.merge_preserve_save(p, {"v": 3}, ensure_backup=True)
    bak = p.with_name(p.name + config_io.BACKUP_SUFFIX)
    assert json.loads(bak.read_text())["v"] == 1  # 備份只建一次，維持首版
