"""replay 向後相容：舊 decisions.jsonl 記錄（inputs 無 requote_threshold_factor 欄位）
必須照樣 replay 出乾淨結果——_rebuild_inputs 用 **fields 展開，缺 key 走 dataclass 預設 0.5。
OLD_RECORD_JSON 取自 logs/decisions.jsonl 第一筆真實舊記錄（head -1，唯讀）。
"""
import json

from grid_engine.replay import diff_record

OLD_RECORD_JSON = r'''{"ts": 1783265790.812226, "symbol": "BNB/USDC:USDC", "inputs": {"price": 588.395, "long_position": 0.58, "short_position": 0.06, "buy_long_orders": 0.0, "sell_long_orders": 0.02, "buy_short_orders": 0.04, "sell_short_orders": 0.02, "last_grid_price_long": 588.405, "last_grid_price_short": 588.405, "long_dead_mode": true, "short_dead_mode": false, "grid_spacing": 0.003, "take_profit_spacing": 0.003, "initial_quantity": 0.02, "position_threshold": 0.4, "position_limit": 0.1, "glft_enabled": false, "gamma": 0.1, "enh": {"dynamic_take_profit": 0.003, "dynamic_grid_spacing": 0.003, "funding_long_bias": 1.0, "funding_short_bias": 1.0, "leading_ofi": 0.0, "leading_volume_ratio": 1.0, "leading_spread_ratio": 0.6122487203034663, "leading_signals": []}}, "decision": {"long": {"should_adjust": true, "enter_dead_mode": false, "exit_dead_mode": false, "cancel_side": false, "orders": [], "new_anchor_price": 588.395, "dynamic_tp": 0.003, "dynamic_gs": 0.003, "display": {"leading_ofi": 0.0, "leading_volume_ratio": 1.0, "leading_spread_ratio": 0.6122487203034663, "leading_signals": [], "inventory_ratio": 0.8125000000000001, "dynamic_take_profit": 0.003, "dynamic_grid_spacing": 0.003}}, "short": {"should_adjust": false, "enter_dead_mode": false, "exit_dead_mode": false, "cancel_side": false, "orders": [], "new_anchor_price": null, "dynamic_tp": 0.003, "dynamic_gs": 0.003, "display": {"leading_ofi": 0.0, "leading_volume_ratio": 1.0, "leading_spread_ratio": 0.6122487203034663, "leading_signals": [], "inventory_ratio": 0.8125000000000001, "dynamic_take_profit": 0.003, "dynamic_grid_spacing": 0.003}}}}'''


def test_old_record_without_factor_replays_clean():
    rec = json.loads(OLD_RECORD_JSON)
    assert "requote_threshold_factor" not in rec["inputs"]
    assert diff_record(rec) is None      # 預設 0.5 補上 → 決策不變


def test_new_record_with_factor_roundtrips():
    rec = json.loads(OLD_RECORD_JSON)
    rec["inputs"]["requote_threshold_factor"] = 0.5
    assert diff_record(rec) is None
