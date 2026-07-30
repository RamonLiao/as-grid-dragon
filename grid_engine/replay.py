"""決策日誌重放：用同一 decide() 逐筆重放實盤落地的 inputs，比對 decision。
零 diff = 快照捕捉完整（實盤 execute 與純層一致）。上線 ≥24h 零 diff 為 #4 最終驗收。

⚠️ `logs/decisions.jsonl` 是 append-only 且**跨越多個 code 版本**，所以「全檔零 diff」
在任何策略邏輯改動之後都必然為假——舊紀錄是舊規則的產物，用新 `decide()` 重放當然不同。
實測：2026-07-27 上線「止盈加倍只給淨曝險側」後，全檔 99,552 筆中 12,349 筆有 diff。
⇒ **任何零 diff 驗收都必須限定「部署時間點之後」的區段**，並在報告裡寫明起始 ts。
跨版本區段要驗的不是「零 diff」，而是「diff 全部符合預期的單向模式」（見該次 spec §5.1）。"""
import json
import dataclasses

from .decision import DecisionInputs, EnhancementSnapshot, decide


def load_records(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _rebuild_inputs(inp: dict) -> DecisionInputs:
    enh = EnhancementSnapshot(**{**inp["enh"],
                                 "leading_signals": tuple(inp["enh"].get("leading_signals", ()))})
    fields = {k: v for k, v in inp.items() if k != "enh"}
    return DecisionInputs(enh=enh, **fields)


def _normalize_tuples_to_lists(obj):
    """遞迴轉換 tuple → list，以匹配 JSON 序列化後的格式。"""
    if isinstance(obj, dict):
        return {k: _normalize_tuples_to_lists(v) for k, v in obj.items()}
    elif isinstance(obj, tuple):
        return [_normalize_tuples_to_lists(item) for item in obj]
    elif isinstance(obj, list):
        return [_normalize_tuples_to_lists(item) for item in obj]
    else:
        return obj


def replay_record(rec: dict) -> dict:
    result = dataclasses.asdict(decide(_rebuild_inputs(rec["inputs"])))
    return _normalize_tuples_to_lists(result)


def diff_record(rec: dict):
    replayed = replay_record(rec)
    return None if replayed == rec["decision"] else {
        "symbol": rec.get("symbol"), "expected": rec["decision"], "replayed": replayed}


def replay_file(path):
    recs = load_records(path)
    diffs = [d for d in (diff_record(r) for r in recs) if d is not None]
    return len(recs), diffs
