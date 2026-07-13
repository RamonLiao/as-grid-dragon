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

## 可判定驗收（詳見 spec §6）
校準 gate PASS → 新語意零強平（兩資本場景全窗口）→ Δeq 三段窗口全 ≥ 舊語意
→ cost sens 排序不翻轉 → 拒單率 >30% 則結論綁入金 → 懸崖偵測（1.5 對照）。
交易 <30 的 cell 標樣本不足。組合總數揭露。
