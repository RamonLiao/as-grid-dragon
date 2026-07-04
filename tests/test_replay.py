import json
from grid_engine import replay
from grid_engine.decision import DecisionInputs, EnhancementSnapshot, decide
import dataclasses


def _make_record():
    enh = EnhancementSnapshot(0.004, 0.006, 1.0, 1.0)
    inp = DecisionInputs(
        price=2.5, long_position=10, short_position=0,
        buy_long_orders=0, sell_long_orders=0, buy_short_orders=0, sell_short_orders=0,
        last_grid_price_long=2.5, last_grid_price_short=2.5,
        long_dead_mode=False, short_dead_mode=False,
        grid_spacing=0.006, take_profit_spacing=0.004,
        initial_quantity=3, position_threshold=60, position_limit=15,
        glft_enabled=False, gamma=0.1, enh=enh)
    dec = decide(inp)
    return {"symbol": "XRP/USDC:USDC",
            "inputs": dataclasses.asdict(inp),
            "decision": dataclasses.asdict(dec)}


def test_replay_zero_diff_on_faithful_record(tmp_path):
    rec = _make_record()
    p = tmp_path / "d.jsonl"
    p.write_text(json.dumps(rec, ensure_ascii=False) + "\n")
    total, diffs = replay.replay_file(str(p))
    assert total == 1 and diffs == []


def test_replay_detects_tampered_decision(tmp_path):
    rec = _make_record()
    rec["decision"]["long"]["should_adjust"] = not rec["decision"]["long"]["should_adjust"]
    p = tmp_path / "d.jsonl"
    p.write_text(json.dumps(rec, ensure_ascii=False) + "\n")
    total, diffs = replay.replay_file(str(p))
    assert total == 1 and len(diffs) == 1
