# 當前任務 Spec：TODO 4a —— `leverage` → `assumed_leverage` 改名與舊 key 清除

完整設計：`docs/superpowers/specs/2026-07-26-leverage-rename-design.md`（權威出處）

**任務已拆分**：原合併 spec（`2026-07-26-leverage-false-knob-design.md` v1/v2）連兩輪被 quant
reviewer 判 Reject，兩次 blocker 同形態——**斷言接線存在而未查證**（v1 斷在交易所邊界、
v2 斷在行程邊界）。依 judgment-rubrics R4「同一錯誤第二次 → 換路徑」，經使用者同意拆為：
- **4a（本 spec）**：純改名 + 舊 key 清除。零交易所互動、零新狀態、零併發。
- **4b（另立）**：讀交易所實測槓桿。範圍縮到引擎行程內（web 端明確承認無實測來源）。

## Goals
1. 欄位改名 `assumed_leverage`，名字自述「假設值，非控制項」。
2. **不留下第二個假旋鈕**：config 檔內舊 `leverage` key 必須實際移除，不得並存。
3. 任何遺漏的舊名存取（讀或寫）在測試期爆炸，不靜默降級。

## Non-goals
**不改任何行為**（回測仍收到同樣數值）；不修「回測 20x vs 實盤 5x」保真度缺陷（屬 4b）；
不讀交易所、不呼叫 `set_leverage`；不改 `backtest/config.py:Config.leverage`（真旋鈕）；
不改 `backtest/`、`scripts/` 純離線路徑；不改下單/決策/風控邏輯。

## Security constraints
零交易所互動。**會寫 `config/`**（僅舊 key 清除，走既有 flock + 原子寫，不需停機）；
不寫 `logs/`、`log/`；不下單、不重啟引擎；測試限 `$(mktemp -d)` 或 `tests/`。

## 可判定驗收（詳見 spec §6；(M) = 須附 mutation）
A1 `from_dict` 舊 key 相容、並存時新 key 勝、`to_dict` 不含舊 key
A2 (M) 讀 `cfg.leverage` 拋 AttributeError
A3 (M) 寫 `cfg.leverage = x` 拋 AttributeError（須先在「只有 `__getattr__`」版本下紅過）
A4 `SymbolConfig(leverage=5)` 拋 TypeError；`assumed_leverage=5` 正常
A5 其他屬性名維持原生行為；`asdict`/`deepcopy` 不拋非 AttributeError
A6 (M) `drop_symbol_keys` 後檔案不含舊 key
A7 (M) `new` 不含 `symbols` key 時 drop 仍生效
A8 (M) `symbol_extras` 含同名 key 時 drop 勝出
A9 **兩個** save 路徑各驗一次 A6
A10 未傳 `drop_symbol_keys` 時行為與改動前完全相同
A11 改動前後回測 result dict bit-identical（「純語意修繕」的直接證據）
A12 全套測試綠（報數量）
A13 `grep leverage` 逐行人工裁決 + 白名單（grep **不是**自動判準，見 spec §7.4）

**停止條件**：dual-review 產出 `Ship as-is` 前不得標記完成。

## 狀態
- 2026-07-26：brainstorming 完成；使用者核可 B+C 與拆分；4a spec 已寫；
  fresh-context quant reviewer 審查中（quant.md 硬觸發，未回 verdict 前不開工）。

---

## 存檔：前一任務 spec（追價語意驗證，已收官 2026-07-15）
設計：`docs/superpowers/specs/2026-07-13-requote-semantics-design.md`；
結果：`tasks/requote-experiment-results.md`（§6 判準 3 FAIL，數據否決 factor=1.0；
holdout 05-01~06-05 保持未開封）。
