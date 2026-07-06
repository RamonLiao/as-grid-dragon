"""history_reader 測試。

為什麼重要：頁1 監控降級後唯一資料源是 decisions.jsonl。
引擎隨時在 append、行可能截斷，reader 必須對損毀輸入免疫。
"""
import json
import pandas as pd
from datetime import datetime

from web.services import history_reader

GOOD = {"ts": 1783265786.63, "symbol": "BNB/USDC:USDC",
        "inputs": {"price": 588.405, "long_position": 0.58,
                   "short_position": 0.06}}


def _write_jsonl(tmp_path, lines):
    p = tmp_path / "decisions.jsonl"
    p.write_text("\n".join(lines))
    return p


def test_load_decisions_parses_fields(tmp_path):
    p = _write_jsonl(tmp_path, [json.dumps(GOOD)])
    df = history_reader.load_decisions(path=p)
    assert len(df) == 1
    row = df.iloc[0]
    assert row["symbol"] == "BNB/USDC:USDC"
    assert row["price"] == 588.405
    assert row["long_position"] == 0.58
    assert isinstance(row["ts"], pd.Timestamp)


def test_load_decisions_skips_corrupt_lines(tmp_path):
    p = _write_jsonl(tmp_path, [
        json.dumps(GOOD),
        '{"ts": 178326, "symbol": "X", "inputs"',  # 截斷行（引擎寫入中）
        "",                                          # 空行
        "not json at all",
        json.dumps(GOOD),
    ])
    df = history_reader.load_decisions(path=p)
    assert len(df) == 2


def test_load_decisions_missing_file(tmp_path):
    df = history_reader.load_decisions(path=tmp_path / "nope.jsonl")
    assert df.empty


def test_load_decisions_tail_limit(tmp_path):
    lines = [json.dumps({**GOOD, "ts": GOOD["ts"] + i}) for i in range(100)]
    p = _write_jsonl(tmp_path, lines)
    df = history_reader.load_decisions(path=p, max_lines=10)
    assert len(df) == 10
    # tail 語意：拿最後 10 筆（最新的）
    assert df["ts"].max() == pd.to_datetime(GOOD["ts"] + 99, unit="s")


def test_load_bandit_state(tmp_path):
    p = tmp_path / "bandit_state.json"
    p.write_text(json.dumps({"arms": {"a": 1}}))
    assert history_reader.load_bandit_state(path=p) == {"arms": {"a": 1}}
    assert history_reader.load_bandit_state(path=tmp_path / "no.json") == {}
    p.write_text("{corrupt")
    assert history_reader.load_bandit_state(path=p) == {}


def test_load_decisions_skips_non_dict_json(tmp_path):
    """JSON parse 成功但非 dict（如引擎並發寫入的 scalar/array）必須跳過，不能 AttributeError。"""
    p = _write_jsonl(tmp_path, [
        json.dumps(GOOD),
        "5",                  # 合法 JSON scalar，但無 .get() 方法
        "null",               # 合法 JSON null
        "[1,2]",              # 合法 JSON array
        json.dumps(GOOD),
    ])
    df = history_reader.load_decisions(path=p)
    # 只有兩筆正常記錄，其他三行跳過
    assert len(df) == 2
    assert not df.empty


def test_load_decisions_directory_path(tmp_path):
    """path 傳入目錄而非檔案時，應返回空 DataFrame，不 raise OSError。"""
    df = history_reader.load_decisions(path=tmp_path)
    assert df.empty


def test_last_activity(tmp_path):
    p = _write_jsonl(tmp_path, [json.dumps(GOOD)])
    df = history_reader.load_decisions(path=p)
    assert history_reader.last_activity(df) is not None
    assert history_reader.last_activity(pd.DataFrame()) is None
