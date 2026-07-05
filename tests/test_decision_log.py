"""決策日誌 characterization：_grid_step 有倉位分支落地一行 JSON（inputs+decision）。
日誌 I/O 失敗不得中斷交易。"""
import json


def test_decision_log_writes_one_json_line(tmp_path):
    """decide() 每次落地一行 JSON，含 inputs 關鍵欄位 + 每側 should_adjust。"""
    from tests.test_characterization_grid import _make_bot, _state
    bot = _make_bot()
    logf = tmp_path / "decisions.jsonl"
    bot._decision_log_path = str(logf)
    sc = bot.config.symbols["XRP/USDC:USDC"]
    _state(bot, latest_price=2.5, long_position=10, short_position=0,
           buy_long_orders=0, sell_long_orders=0)
    import asyncio
    asyncio.run(bot._grid_step("XRP/USDC:USDC", sc))
    lines = logf.read_text().strip().splitlines()
    assert len(lines) >= 1
    rec = json.loads(lines[-1])
    assert rec["symbol"] == "XRP/USDC:USDC"
    assert "inputs" in rec and "decision" in rec
    assert rec["inputs"]["price"] == 2.5
    # 每側 should_adjust 落地
    assert "should_adjust" in rec["decision"]["long"]
    assert "should_adjust" in rec["decision"]["short"]


def test_decision_log_disabled_when_no_path(tmp_path):
    """未設 _decision_log_path → 不寫檔（getattr 預設 None）。"""
    from tests.test_characterization_grid import _make_bot, _state
    bot = _make_bot()
    sc = bot.config.symbols["XRP/USDC:USDC"]
    _state(bot, latest_price=2.5, long_position=10, short_position=0,
           buy_long_orders=0, sell_long_orders=0)
    import asyncio
    # 無 _decision_log_path 屬性 → 不應拋
    asyncio.run(bot._grid_step("XRP/USDC:USDC", sc))


def test_decision_log_io_failure_does_not_break_trading(tmp_path):
    """日誌路徑無效（指向目錄）→ 寫入吞例外，交易不中斷（place_order 仍被呼叫）。"""
    from tests.test_characterization_grid import _make_bot, _state
    bot = _make_bot()
    # 指向一個「目錄」路徑當 log file → open(..., 'a') 失敗
    bad = tmp_path / "adir"
    bad.mkdir()
    bot._decision_log_path = str(bad)
    sc = bot.config.symbols["XRP/USDC:USDC"]
    _state(bot, latest_price=2.5, long_position=10, short_position=0,
           buy_long_orders=0, sell_long_orders=0)
    import asyncio
    asyncio.run(bot._grid_step("XRP/USDC:USDC", sc))  # 不應拋
    assert bot.order_executor.place_order.await_count >= 1
