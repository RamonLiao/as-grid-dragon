"""週期同步的 monkey testing：想辦法把它玩壞。

專案規則要求 unit + integration 之後做極端測試。這裡的每一條都對應一個
「這東西掛了會怎樣」的問題，不是為了覆蓋率。

前 5 條對應 task brief（`test_concurrent_run_and_manual_sync_all` 依主 session
裁定改寫：brief 原斷言 `any(skipped) or all(not skipped)` 對任何輸入恆真，
換成真正守 lock 語意的斷言；其中一條裝飾性斷言已於最終 review 的 fix wave 刪除）。後 3 條是本輪額外想的極端情境。
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

from grid_engine import clock
from grid_engine.bot import MaxGridBot
from grid_engine.config import GlobalConfig, SymbolConfig
from grid_engine.sync_service import SYNC_FAILURE_THRESHOLD, SyncOutcome

SYMBOL = "XRP/USDC:USDC"


def _make_bot():
    cfg = GlobalConfig()
    cfg.symbols = {SYMBOL: SymbolConfig(
        symbol="XRPUSDC", ccxt_symbol=SYMBOL, enabled=True,
        take_profit_spacing=0.003, grid_spacing=0.003, initial_quantity=0.02,
        limit_multiplier=5.0, threshold_multiplier=20.0,
    )}
    cfg.bandit.enabled = False
    bot = MaxGridBot(cfg)
    bot.order_executor.place_order = AsyncMock()
    bot.order_executor.cancel_orders_for_side = AsyncMock()
    return bot


@pytest.fixture
def sync():
    bot = _make_bot()
    s = bot.sync_service
    s._sync_positions = AsyncMock()
    s._sync_orders = AsyncMock()
    s._sync_account = AsyncMock()
    s._sync_funding_rates = AsyncMock()
    s._sync_trade_stats = AsyncMock()
    yield s
    clock.reset_clock()
    clock.reset_guard_clock()


@pytest.mark.asyncio
async def test_notifier_send_raising_does_not_propagate_into_evaluate(sync):
    """`_notify()` 用 `asyncio.create_task` 把送信丟成背景 task，不 `await`
    它——告警失敗必須是那個 detached task 自己的事，不能沿著呼叫鏈冒泡回
    `_evaluate()`／`run()`。

    這條測試原本（改版前）把 `sync_all` mock 成正常回傳一個失敗的
    `SyncOutcome`（不拋例外），透過 `run()` 跑幾輪後只斷言
    `sync._degraded is True`——但 `send()` 的例外從頭到尾發生在 `_notify`
    `create_task` 出去的那個 task 裡，從未出現在 `run()` 的呼叫堆疊上；就算
    把 `run()` 的 `except Exception`（`sync_service.py:212`）整段刪掉，這條
    測試依然綠燈——它驗證的是「這裡不需要守衛」，不是「守衛有效」，等於一條
    會執行的註解（review Important，2026-08-26）。

    改版：直接呼叫 `_evaluate()`（略過 `run()`），逼它跨過門檻觸發
    `_notify()` → `notifier.send()` 拋例外。真正的守衛對象是 `_notify` 內的
    `create_task`（`sync_service.py:172`），不是 `run()` 的 `except Exception`
    ——後者只是剛好也會接住任何例外，蓋掉這條測試真正該驗的訊號，所以改版
    刻意繞過它。鑑別力：若把 `create_task` 換成直接 `await
    self.notifier.send(message)`（連帶把 `_notify`/`_evaluate` 改成 async），
    `send()` 的 `RuntimeError` 會在下面 for 迴圈內的 `_evaluate()` 呼叫處直接
    冒出，測試會在該行掛掉（不是斷言失敗，是例外讓測試本身出錯）——已用這個
    mutation 實際跑過一次驗證為紅，跑完已還原（見 task-7-report.md 附錄）。
    """
    sync.notifier.bot_token = "t"
    sync.notifier.chat_id = "c"
    sync.notifier.send = AsyncMock(side_effect=RuntimeError("telegram down"))

    for _ in range(SYNC_FAILURE_THRESHOLD):
        sync._evaluate(SyncOutcome(positions_ok=False))   # 不應該冒出例外

    assert sync._degraded is True
    assert sync._degraded_total == 1

    await asyncio.sleep(0.01)   # 讓 create_task 出去的背景 task 真的跑到、真的丟出例外
    sync.notifier.send.assert_awaited_once()   # 證明例外真的發生過，不是斷言了個沒發生的事


@pytest.mark.asyncio
async def test_stop_then_run_exits_immediately(sync):
    """先 stop 再 run：不得卡住，不得跑任何同步。"""
    sync.stop()
    await asyncio.wait_for(sync.run(), timeout=2.0)
    sync._sync_positions.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_run_and_manual_sync_all(sync):
    """loop 與啟動時的 sync_all()（bot.py:788）撞在一起：靠 `_sync_lock` 的
    early-return 化解，不得死鎖、不得重入、skipped 不得被算成失敗。

    原 brief 版本的收尾斷言 `any(skipped) or all(not skipped)` 涵蓋所有可能
    輸出、對任何結果恆真，等於一條會執行的註解——不符合「測試要 encode 為
    什麼這行為重要」的規則。這裡改成三件真正重要的事：(1) 五次並發呼叫全部
    正常回傳 SyncOutcome，不拋例外也不回 None；(2) 收尾時鎖未被持有，證明
    early-return 路徑沒有忘記釋放或死鎖。

    原本還有第三條 `all(r.critical_ok for r in skipped)`（主 session 的
    pre-flight Ruling 3 要求加的），已於最終 review 的 fix wave 刪除：
    `SyncOutcome(skipped=True)` 的關鍵欄位預設就是 True，那條對任何輸入恆真，
    是裝飾性斷言。該語意由 test_periodic_sync.py 的
    test_none_and_skipped_do_not_move_counter 守住（它驗的是 _evaluate 對
    skipped 的處理，不是 dataclass 的預設值）。
    """
    sync.config.sync_interval = 0.01
    task = asyncio.create_task(sync.run())
    results = await asyncio.gather(*[sync.sync_all() for _ in range(5)])
    sync.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert not sync._sync_lock.locked()
    assert all(isinstance(r, SyncOutcome) for r in results)


@pytest.mark.asyncio
async def test_flapping_does_not_spam_alerts(sync):
    """失敗↔成功來回抖動：每次真正進降級才發一封，不得洗版。"""
    sent = []
    sync._notify = lambda msg: sent.append(msg)
    for _ in range(5):
        for _ in range(SYNC_FAILURE_THRESHOLD):
            sync._evaluate(SyncOutcome(positions_ok=False))
        sync._evaluate(SyncOutcome())
    assert len(sent) == 10          # 5 次降級 + 5 次恢復，不多不少
    assert sync._degraded_total == 5


@pytest.mark.asyncio
async def test_config_interval_changed_at_runtime(sync):
    """執行中改 sync_interval：下一輪就該生效，不得要重啟。"""
    sync.config.sync_interval = 0.01
    task = asyncio.create_task(sync.run())
    await asyncio.sleep(0.05)
    sync.config.sync_interval = 999
    before = sync._sync_positions.call_count
    await asyncio.sleep(0.1)
    sync.stop()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert sync._sync_positions.call_count - before <= 1


# --- 以下 3 條是本輪額外想的極端情境，brief 未覆蓋 ---

@pytest.mark.asyncio
async def test_base_exception_in_sync_all_kills_loop(sync):
    """`run()` 只接 `CancelledError` 與 `Exception`（docstring：「例外一律吞掉
    續跑」指的是 Exception），非 Exception 的 BaseException 不在傘下。這條
    釘死現況行為：BaseException 會讓 loop task 帶著例外收工，不是被吞掉續跑。

    為什麼值得測：這是目前程式碼的實際行為，但沒有任何既有測試明確斷言過
    ——如果之後有人「順手」把 `except Exception` 擴大成 `except BaseException`
    （例如為了「更保險」），這條測試會先紅，逼寫的人正視這個改動改變了什麼
    語意（例如會連 KeyboardInterrupt/SystemExit 都吞掉續跑）。
    """
    class Boom(BaseException):
        pass

    sync.config.sync_interval = 0.01
    sync.sync_all = AsyncMock(side_effect=Boom("not a subclass of Exception"))

    task = asyncio.create_task(sync.run())
    with pytest.raises(Boom):
        await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_stop_during_inflight_sync_does_not_strand_lock(sync):
    """`stop()` 在 loop 正跑到 `sync_all()` 中途（`_sync_positions` 還沒
    return）時被呼叫時會怎樣。

    這條是 **characterization（存檔現況）**，不是在守某個既有守衛——沒有任何
    一行程式碼是為了「stop 撞上 in-flight sync」而寫的，現況是
    `_stop_event` 只擋「下一輪要不要開始」、`async with self._sync_lock` 自己
    保證釋放，兩者疊起來剛好得到這個行為。存檔的內容：in-flight 的同步會跑完、
    `_sync_lock` 會釋放、loop 會乾淨收尾（無例外退出）。日後若有人改成「stop
    要中斷 in-flight 那一輪」，這條會紅——那時該做的是重新裁決語意並改這條
    測試，不是把它當成違反了什麼不變式。
    """
    started = asyncio.Event()

    async def slow_positions():
        started.set()
        await asyncio.sleep(0.2)
        return True

    sync._sync_positions = slow_positions
    sync.config.sync_interval = 0.01

    task = asyncio.create_task(sync.run())
    await started.wait()          # 確保 stop() 落在 _sync_positions 執行期間
    sync.stop()
    await asyncio.wait_for(task, timeout=2.0)

    assert not sync._sync_lock.locked()
    assert task.done() and task.exception() is None


@pytest.mark.asyncio
async def test_notifier_enabled_toggled_mid_run_is_read_live(sync):
    """`notifier.enabled` 是算出來的 property（`bot_token` 與 `chat_id`
    是否都設好），`_notify()` 每次呼叫才檢查一次，不是建構時快照。這條守：
    降級發生時 enabled=False（沒設 token）不該送出任何訊息；使用者接著在
    執行中設好 token/chat_id 後，下一次狀態轉換（這裡是恢復）必須送得出去
    ——不需要重啟 SyncService，因為 `_notify` 沒有快取 enabled 的舊值。
    """
    sent = []
    sync.notifier.send = AsyncMock(side_effect=lambda msg: sent.append(msg))

    assert sync.notifier.enabled is False   # 預設 bot_token/chat_id 皆空
    for _ in range(SYNC_FAILURE_THRESHOLD):
        sync._evaluate(SyncOutcome(positions_ok=False))
    assert sync._degraded is True
    assert sent == []                       # enabled=False，降級當下不該送

    sync.notifier.bot_token = "t"
    sync.notifier.chat_id = "c"
    assert sync.notifier.enabled is True

    sync._evaluate(SyncOutcome())           # 觸發恢復
    await asyncio.sleep(0.01)               # 讓 _notify 建立的 fire-and-forget task 跑完
    assert sync._degraded is False
    assert len(sent) == 1
