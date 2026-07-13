# 當前任務 Spec：追價語意驗證（tick 級回測）+ 門檻參數化

完整設計：`docs/superpowers/specs/2026-07-13-requote-semantics-design.md`（權威出處）

## Goals
1. 同一 tick 路徑（aggTrades）量化追價門檻 0.5×spacing vs ≥1.0×spacing 的
   Δeq / max_dd / 強平 / 成交率 / 保證金拒單率 → 產出改不改 live 語意的裁決依據。
2. `requote_threshold_factor` 參數化（預設 0.5，bit-identical），live 與純層單一來源。
3. 校準 gate：舊語意模擬必須重現 live ~0 成交/26.5h，FAIL 即停。

## Non-goals
不改 spacing、不開增強、本任務不翻 live 行為（翻值是使用者獨立決定）、
不做多 symbol / post-only、不動 GridBacktester 1m 撮合語意。

## Security constraints
交易所全程 read-only；寫檔限 data/、docs/、tasks/、tests；
不碰 config/、logs/、log/；不下單不重啟引擎。

## 可判定驗收（詳見 spec §6；2026-07-13 quant reviewer 修訂後）
① 校準 gate 雙向（低端 live≈0 + 高端 vs 1m backtester [0.2×,1.0×]）→
② 零強平（強平判定 per-lot 與 netted 雙模型保守取或）→
③ Δeq 三段全 ≥ 舊（只計獨立事件 ≥30 的 cell，達標段 <2 = inconclusive）→
④ cost sens 排序不翻轉 → ⑤ 拒單率 >30% 結論綁入金 →
⑥ 優勝者 factor ±20% + cooldown {2.5,5,10}s 無孤峰 →
⑦ holdout 05-01~06-05（未開封）最終 OOS，翻車即 inconclusive 不回頭調參。
窗口不相交；組合總數揭露；spread 假觸發敏感度報告。
