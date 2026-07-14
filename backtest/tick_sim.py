"""tick 事件模擬器：在 aggTrades 事件流（ts_ms, price, qty）上模擬 live 網格迴路。

用途：在 tick 粒度重放 live `_handle_ticker`→`adjust_grid`→`_grid_step` 的追價/掛單
語意，對比 `requote_threshold_factor` 對「掛單被搬走 vs 掛單靜置到被穿越」的影響
（spec §4.2）。與 1m `backtester.py` 的差異：

- 撮合吃單一 tick price（非 bar high/low），**嚴格穿越**才成交（V1/V2 防禦，
  buy 掛單 `price < limit`、sell 掛單 `price > limit`），all-or-nothing。
- 掛單有 `effective_ms`/`expire_ms` 生命週期：requote 時舊單延後 `ev+delay` 才撤
  （cancel 未落地前仍可成交，lookahead 防禦），新單 `ev+delay` 才生效。
- per-side cooldown、決策延遲、dead mode 旗標維護。

純層（`grid_engine.decision.decide`、`PositionBook`、`should_liquidate`、
`funding_charge`）全 import 不重寫。event loop 為 tick 級 event-driven，允許 Python
迴圈（quant rules：迴圈只允許在 event-driven 回測引擎內）。

模擬語意逐條對照見 .superpowers/sdd/task-7-brief.md §4.2 (1)-(9)。

注意（gate 語意，brief §5 (i) 落地，2026-07-14 review 修正分側）：觸發條件 (i)
「該側掛單缺失」依持倉分兩個 regime：

- **有倉側**（`book.qty(side) > 0`）：live OR 語意——缺 entry 或缺 tp（各自無任何
  未過期掛單）任一成立即觸發，鏡射 `decision.should_adjust` 的
  `buy_o<=0 OR sell_o<=0`。這修正了 `requote_threshold_factor>1` 時的邊界洞：
  TP 成交後 deviation 尚未達門檻，但 live 會在下個 10s timer tick 立即補掛 tp
  （§9 已知差異，非 event-driven），舊版 AND-of-absence 會漏掉這個補掛時點，
  系統性低估 factor>1 的成交率。
- **flat 側**（`qty == 0`）：維持 AND-of-absence（該側**無任何未過期掛單**，
  entry 與 tp 皆無才觸發）。flat 側若改用 OR，會因「結構性無 tp 單」（無倉可平）
  而每個事件都觸發，把靜置的 entry 一路搬走，令 test_strict_crossing /
  test_resting 的「掛單靜置到被穿越」核心主張失效。

`requote_threshold_factor<=1` 時（deviation 門檻 <= grid_spacing）兩個 regime
行為與 live 等價（deviation 門檻本身已夠緊，OR/AND 差異不顯現）；factor>1 時只有
有倉側的 OR 修正會生效。gate 由本模組自行計算（不複用 should_adjust）；decide()
只在 gate 放行後被呼叫來產生掛單，其內部 should_adjust 在 gate 放行時恆為 True
（空側 → buy_o=0；有倉側缺單 → 對應 o<=0；偏離 → deviation），兩者一致。
dead 側/flat 側完全平光後的重掛節奏與 live 的差異見 brief §9 FIDELITY_NOTES。
"""
from dataclasses import dataclass, field

import pandas as pd

from backtest.accounting import PositionBook
from backtest.costs import funding_charge
from backtest.liquidation import should_liquidate
from grid_engine.decision import (DecisionInputs, EnhancementSnapshot, decide)


@dataclass
class TickSimConfig:
    grid_spacing: float = 0.003
    take_profit_spacing: float = 0.003
    initial_quantity: float = 0.02
    leverage: float = 5.0
    initial_balance: float = 184.6
    fee_pct: float = 0.0002
    slippage_bps: float = 0.0001
    threshold_multiplier: float = 40.0
    limit_multiplier: float = 5.0
    requote_threshold_factor: float = 0.5
    cooldown_sec: float = 5.0
    decision_delay_ms: int = 500
    maintenance_margin_rate: float = 0.005    # 對齊 backtest/config.py 現值
    seed_long_qty: float = 0.0
    seed_long_price: float = 0.0
    seed_short_qty: float = 0.0
    seed_short_price: float = 0.0
    funding_events: list = field(default_factory=list)   # [(epoch_sec, rate)]


@dataclass
class TickSimResult:
    final_equity: float
    max_drawdown: float
    liquidated: bool
    fills: list              # [{ts_ms, side, kind: 'entry'|'tp', price, qty}]
    round_trips: int         # 完成的 entry→TP 往返數（獨立事件數，spec §5）
    rejected_entries: int
    requote_count: int
    realized_pnl: float


def _neutral_snapshot(base_tp: float, base_gs: float) -> EnhancementSnapshot:
    """中性快照：dynamic=base、funding bias=1.0（無 leading/ATR/funding 增強）。"""
    return EnhancementSnapshot(
        dynamic_take_profit=base_tp,
        dynamic_grid_spacing=base_gs,
        funding_long_bias=1.0,
        funding_short_bias=1.0,
    )


def run_tick_sim(events: pd.DataFrame, cfg: TickSimConfig) -> TickSimResult:
    book = PositionBook(cfg.initial_balance, cfg.leverage, cfg.fee_pct,
                        cfg.slippage_bps, conservative_reject=True)

    # seed 既有持倉（margin 扣 balance 不扣 fee，#14 語意）
    if cfg.seed_long_qty > 0:
        book.seed("long", cfg.seed_long_qty, cfg.seed_long_price)
    if cfg.seed_short_qty > 0:
        book.seed("short", cfg.seed_short_qty, cfg.seed_short_price)

    position_threshold = cfg.initial_quantity * cfg.threshold_multiplier
    position_limit = cfg.initial_quantity * cfg.limit_multiplier
    delay = cfg.decision_delay_ms

    # 掛單集合：每張 {pos_side, kind('entry'|'tp'), is_buy, price, qty, effective_ms, expire_ms}
    # expire_ms is None → 永不過期（直到被 requote 標記撤單）。
    orders: list = []
    anchor = {"long": 0.0, "short": 0.0}
    dead = {"long": False, "short": False}
    last_requote = {"long": None, "short": None}   # epoch_sec，None=未曾 requote

    fills: list = []
    round_trips = 0
    requote_count = 0
    liquidated = False
    _prune_counter = 0
    _PRUNE_EVERY = 1000

    funding_sorted = sorted(cfg.funding_events, key=lambda x: x[0])
    fund_i = 0

    max_equity = book.equity_at(cfg.seed_long_price or cfg.seed_short_price or 1.0)
    min_equity = max_equity
    last_price = None

    def _present(o, ts) -> bool:
        """未過期（含尚未 effective 的 pending 單）——供 gate 與 order-count。"""
        return o["expire_ms"] is None or ts < o["expire_ms"]

    def _fill_eligible(o, ts) -> bool:
        """本 tick 可成交：已 effective 且未過期。"""
        return o["effective_ms"] <= ts and (o["expire_ms"] is None or ts < o["expire_ms"])

    def _side_qty(side: str, kind: str, ts: float) -> float:
        return sum(o["qty"] for o in orders
                   if o["pos_side"] == side and o["kind"] == kind and _present(o, ts))

    for row in events.itertuples(index=False):
        ts = float(row.ts_ms)
        price = float(row.price)
        epoch = ts / 1000.0
        last_price = price

        # ---- (a) 成交判定：嚴格穿越，entry 先於 tp（與 1m _settle 同序）----
        for side in ("long", "short"):
            prior_qty = book.qty(side)   # entry 結算前快照，供 tp clamp
            side_orders = [o for o in orders if o["pos_side"] == side and _fill_eligible(o, ts)]
            entries = [o for o in side_orders if o["kind"] == "entry"]
            tps = [o for o in side_orders if o["kind"] == "tp"]
            for o in entries:
                crossed = (price < o["price"]) if o["is_buy"] else (price > o["price"])
                if not crossed:
                    continue
                if book.open(side, o["price"], o["qty"]):
                    fills.append({"ts_ms": ts, "side": side, "kind": "entry",
                                  "price": o["price"], "qty": o["qty"]})
                    orders.remove(o)   # 成交才撤（拒單則保留，與 _settle 一致）
            for o in tps:
                crossed = (price < o["price"]) if o["is_buy"] else (price > o["price"])
                if not crossed:
                    continue   # 未穿越 → tp 靜置（與 _settle 一致，不撤）
                closable = min(o["qty"], prior_qty)
                if closable > 0:
                    book.close(side, o["price"], closable, ts)
                    fills.append({"ts_ms": ts, "side": side, "kind": "tp",
                                  "price": o["price"], "qty": closable})
                    round_trips += 1
                orders.remove(o)   # 穿越判定後撤（與 _settle 同：只在觸發時清）

        # ---- (b) 強平檢查 ----
        if should_liquidate(book.equity_at(price), book.qty("long"), book.qty("short"),
                            price, cfg.maintenance_margin_rate):
            if book.qty("long") > 0:
                book.close("long", price, book.qty("long"), ts)
            if book.qty("short") > 0:
                book.close("short", price, book.qty("short"), ts)
            liquidated = True
            eq = book.equity_at(price)
            max_equity = max(max_equity, eq)
            min_equity = min(min_equity, eq)
            break

        # ---- (c) funding 現金流結算（data-driven；掃過所有 epoch <= 本事件）----
        while fund_i < len(funding_sorted) and funding_sorted[fund_i][0] <= epoch:
            rate = funding_sorted[fund_i][1]
            book.apply_funding(funding_charge(book.long_positions, rate, "long", price))
            book.apply_funding(funding_charge(book.short_positions, rate, "short", price))
            fund_i += 1

        # ---- (d) 決策 gate（鏡射 live _handle_ticker→adjust_grid→_grid_step）----
        long_pos = book.qty("long")
        short_pos = book.qty("short")
        gates = {}
        for side in ("long", "short"):
            side_pos = long_pos if side == "long" else short_pos
            if side_pos > 0:
                # 有倉側：live OR 語意（缺 entry 或缺 tp 任一即觸發）
                has_entry = any(o["pos_side"] == side and o["kind"] == "entry"
                                and _present(o, ts) for o in orders)
                has_tp = any(o["pos_side"] == side and o["kind"] == "tp"
                             and _present(o, ts) for o in orders)
                trigger = not has_entry or not has_tp
            else:
                # flat 側：AND-of-absence（完全無掛單才觸發，避免每事件 chasing）
                has_order = any(o["pos_side"] == side and _present(o, ts) for o in orders)
                trigger = not has_order
            if not trigger and anchor[side] > 0:
                trigger = abs(price - anchor[side]) / anchor[side] >= \
                    cfg.grid_spacing * cfg.requote_threshold_factor
            if not trigger:
                continue
            # per-side cooldown：距上次該側 requote >= cooldown_sec
            lr = last_requote[side]
            if lr is not None and (epoch - lr) < cfg.cooldown_sec:
                continue
            gates[side] = True

        if gates:
            inputs = DecisionInputs(
                price=price,
                long_position=long_pos,
                short_position=short_pos,
                buy_long_orders=_side_qty("long", "entry", ts),
                sell_long_orders=_side_qty("long", "tp", ts),
                buy_short_orders=_side_qty("short", "tp", ts),      # 空頭 TP = 買回
                sell_short_orders=_side_qty("short", "entry", ts),  # 空頭進場 = 賣
                last_grid_price_long=anchor["long"],
                last_grid_price_short=anchor["short"],
                long_dead_mode=dead["long"],
                short_dead_mode=dead["short"],
                grid_spacing=cfg.grid_spacing,
                take_profit_spacing=cfg.take_profit_spacing,
                initial_quantity=cfg.initial_quantity,
                position_threshold=position_threshold,
                position_limit=position_limit,
                glft_enabled=False,
                gamma=0.0,
                enh=_neutral_snapshot(cfg.take_profit_spacing, cfg.grid_spacing),
                requote_threshold_factor=cfg.requote_threshold_factor,
            )
            decision = decide(inputs)
            for side in ("long", "short"):
                if side not in gates:
                    continue
                sd = decision.long if side == "long" else decision.short
                if not sd.should_adjust:
                    continue
                if sd.enter_dead_mode:
                    dead[side] = True
                if sd.exit_dead_mode:
                    dead[side] = False
                if sd.cancel_side:
                    # 舊單延後 ev+delay 撤（cancel 落地前仍可成交）
                    for o in orders:
                        if o["pos_side"] == side and _present(o, ts):
                            exp = ts + delay
                            o["expire_ms"] = exp if o["expire_ms"] is None else min(o["expire_ms"], exp)
                for oi in sd.orders:
                    orders.append({
                        "pos_side": side,
                        "kind": "tp" if oi.reduce_only else "entry",
                        "is_buy": oi.side == "buy",
                        "price": oi.price,
                        "qty": oi.quantity,
                        "effective_ms": ts + delay,   # 新單延遲後生效
                        "expire_ms": None,
                    })
                if sd.new_anchor_price is not None:
                    anchor[side] = sd.new_anchor_price
                last_requote[side] = epoch
            requote_count += 1   # 每事件計一次（初始佈網=1；brief §5 test_cooldown 註）

        # ---- equity 曲線（逐事件 tick 級，天然含 wick）----
        eq = book.equity_at(price)
        max_equity = max(max_equity, eq)
        min_equity = min(min_equity, eq)

        # ---- 定期清除已過期掛單（ts 單調不減 → 一旦 not _present 永遠 not _present，
        # 清除不改變任何 _present/_fill_eligible 讀取路徑的行為，純 perf）----
        _prune_counter += 1
        if _prune_counter >= _PRUNE_EVERY:
            _prune_counter = 0
            orders[:] = [o for o in orders if _present(o, ts)]

    final_price = last_price if last_price is not None else cfg.initial_balance
    final_equity = book.equity_at(final_price)
    realized_pnl = sum(t["pnl"] for t in book.trades)
    max_drawdown = 1 - (min_equity / max_equity) if max_equity > 0 else 0.0

    return TickSimResult(
        final_equity=final_equity,
        max_drawdown=max_drawdown,
        liquidated=liquidated,
        fills=fills,
        round_trips=round_trips,
        rejected_entries=book.rejected_entries,
        requote_count=requote_count,
        realized_pnl=realized_pnl,
    )
