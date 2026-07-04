# Bandit 狀態持久化 (#6) — Design

**Date:** 2026-07-04
**Status:** Approved (brainstorming)
**Scope:** 讓 `UCBBanditOptimizer` 學到的狀態跨重啟存活，重啟不歸零重學。

## 問題

`indicators/bandit.py::UCBBanditOptimizer` 已有 `to_dict()` / `load_state()`，但 `grid_engine/bot.py` 從未呼叫。bot 每次重啟 `self.bandit_optimizer = UCBBanditOptimizer(config.bandit)`（`bot.py:108`）都從零開始，先前累積的 arm 統計（pull_counts / rewards / Thompson α,β / contextual 統計）全丟，白燒探索成本重學。

此外 `load_state()`（`bandit.py:539`）**無任何防護**：盲信存檔中的 arm index。若 `DEFAULT_ARMS`（`bandit.py:82-98`，硬編 10 組）數量或順序變動，舊 index 會對應到錯的參數 → 靜默學錯，是隱藏地雷。

## 目標 / 非目標

**目標**
- bot 啟動載入、運行中定期落地、停機收尾 bandit 狀態。
- arms 定義變動時安全 fallback 到冷啟動（絕不學錯 index）。
- 任何載入失敗（缺檔 / 壞 JSON / schema 或 arm 簽章不符）都冷啟動、不 crash。
- 存檔對 crash 原子安全（不留半個 JSON）。

**非目標**
- 不做 per-symbol bandit（現況全域單一實例，維持）。
- 不做 arm 變動的統計遷移（YAGNI，簽章不符直接冷啟動）。
- 不改 bandit 學習演算法本身。

## 架構

新增純層模組 **`grid_engine/bandit_persistence.py`**（file-IO 隔離、可獨立 unit test，延續 #4 落地的「純層 + 薄接線」pattern）。bot.py 只做接線；bandit.py 只加一個純讀 helper。

```
bot.run()      ──load──▶ bandit_persistence.load_bandit_state(bandit, path)
bot.record_trade 段 ──save(條件)──▶ bandit_persistence.save_bandit_state(bandit, path)
bot.stop()     ──save(best-effort)──▶ 同上
```

### 元件邊界

| 元件 | 職責 | 依賴 |
|------|------|------|
| `bandit_persistence.save_bandit_state(bandit, path)` | envelope 打包 + 原子寫檔 | os, json, hashlib（透過 bandit.arm_signature） |
| `bandit_persistence.load_bandit_state(bandit, path)` | 讀檔 + 驗證 + 套用 or 冷啟動；回傳 bool（是否成功載入） | 同上 |
| `bandit.arm_signature() -> str` | 純讀 `self.arms` 算 sha1 簽章 | hashlib |
| bot 接線（3 處） | 決定何時 load/save、gate、記 last_saved_pulls | 上述模組 |

## 存檔格式

`logs/bandit_state.json`（default，可由 `config.bandit_state_path` override）：

```json
{
  "schema_version": 1,
  "arm_signature": "<sha1 hexdigest of [(gamma, grid_spacing, take_profit_spacing), ...]>",
  "state": { "...bandit.to_dict() 原樣..." }
}
```

- `state` 直接嵌 `bandit.to_dict()`（`bandit.py:525`）的回傳，不改其結構。
- **原子寫入**：寫 `<path>.tmp` → `os.replace(tmp, path)`（同檔系統 rename 原子）；先 `os.makedirs(os.path.dirname(path), exist_ok=True)`。

## arm_signature

`indicators/bandit.py` 新增：

```python
def arm_signature(self) -> str:
    """arms 定義的穩定簽章；arms 數量/順序/參數值任一改變則簽章改變。"""
    payload = [(a.gamma, a.grid_spacing, a.take_profit_spacing) for a in self.arms]
    return hashlib.sha1(repr(payload).encode()).hexdigest()
```

- 純函數、無副作用、只讀 `self.arms`。是唯一動到 bandit.py 的地方。
- **不**納入 `window_size` — window_size 變動不影響 index↔參數對應，`load_state` 已用 `deque(v, maxlen=window_size)` 重建（`bandit.py:554`），改窗大小仍可安全載入。

## 存檔時機（事件驅動 + stop）

**精確觸發依據**：`_update_and_select()`（`bandit.py:433`）每 `update_interval`（預設 10）筆交易才跑一次，且其中 `self.total_pulls += 1`（`bandit.py:447`）是唯一遞增點。所以 `total_pulls` 變化 ⟺ 發生了一次評估、狀態實質改變。

- bot 加 `self._bandit_last_saved_pulls`（init 0）。在呼叫 `record_trade(...)` 之後（`bot.py:909` 那段），若 `bandit.total_pulls != self._bandit_last_saved_pulls` → `save_bandit_state(...)` 並更新 `_bandit_last_saved_pulls`。
  - 寫檔次數 = 評估次數（每 10 筆一次），零浪費、永遠是最新學習狀態。
  - 不改 `record_trade` 簽名（維持回傳 None）。
- `stop()`（`bot.py:1098`）內 best-effort save 一次，`try/except` 包住，**絕不擋停機流程**（收 `_update_and_select` 剛跑完但主迴圈已退的殘留）。
- save 失敗（磁碟滿 / 目錄唯讀）只 log warning，不可炸 bot 交易主流程。

## 載入時機

`run()`（`bot.py:1049`）開頭，`config.bandit.enabled` 為真時（純讀檔，不依賴 exchange，放最前面即可）。

`load_bandit_state` 決策樹（全部走 cold-start fallback、永不 raise 到 bot）：

| 情況 | 行為 | log |
|------|------|-----|
| 檔案不存在 | 冷啟動 | info「無歷史 bandit 狀態，冷啟動」 |
| JSON parse 失敗 / 非 dict / 缺 `state` | 冷啟動 | warning |
| `schema_version` 不符 | 冷啟動 | warning |
| `arm_signature` 不符 | 冷啟動 | warning「arms 定義已變，捨棄舊 bandit 狀態」 |
| 全部通過 | `bandit.load_state(payload['state'])` | info「載入 bandit 狀態 total_pulls=N」 |

載入成功時同步把 `self._bandit_last_saved_pulls = bandit.total_pulls`（避免啟動後第一次評估前就多寫一次）。

## Gate

`config.bandit.enabled` 為 False 時，load 與 save 皆 short-circuit return，完全不碰檔案系統。

## Config

`config/models.py` 加 `bandit_state_path: Optional[str] = None`（GlobalConfig 層，非 BanditConfig，比照 `decision_log_path`）。None → bot 在 run() 套 default `logs/bandit_state.json`。from_dict 向後相容（舊 config 無此欄 → None → default）。

## 錯誤處理總覽

- **載入**：任何異常 → 冷啟動，bot 照常起。
- **存檔**：任何異常 → log warning，交易主流程不受影響。
- **原子性**：`os.replace` 保證讀者看到的永遠是完整舊檔或完整新檔，無中間態。
- **並發**：bot 單 event loop，save/load 為 async 內同步呼叫，無跨執行緒競態；REST executor 不碰此檔。

## 測試計畫（TDD，先 red 再 green）

**純層 `tests/test_bandit_persistence.py`**
- `arm_signature`：相同 arms 穩定、改任一 arm 參數 / 增刪 arm / 換順序 → 簽章變。
- roundtrip：train 一個 bandit（跑數十筆 record_trade 觸發多次評估）→ save → 新 bandit load → 狀態等價（`current_arm_idx` / `total_pulls` / `pull_counts` / `rewards` / `thompson_alpha,beta` / `context_pulls` / `cumulative_reward`）。
- 相容拒絕：手改存檔 `arm_signature` → load 回 False、bandit 維持冷啟動預設。
- `schema_version` 不符 → 拒絕。
- 缺檔 → 回 False、不 raise。
- 壞 JSON / 截斷檔 / 非 dict / 缺 `state` 欄 → 回 False、不 raise。
- 原子寫：save 後無殘留 `.tmp`；目錄不存在會自動建。

**bot 接線（擴充現有 bot 測試或新檔）**
- `total_pulls` 因足量 record_trade 變化後 → 落地一次（用 `tmp_path` 指定 `bandit_state_path`）。
- 未達 `update_interval`（total_pulls 不變）→ 不寫檔。
- `stop()` → best-effort save 有觸發。
- `config.bandit.enabled = False` → run/record/stop 全程不碰檔（斷言檔案不存在）。
- 載入成功後 `_bandit_last_saved_pulls == bandit.total_pulls`。

**Monkey（極端 / 破壞）**
- 空 `state` dict、`arm_signature` 亂填字串、`total_pulls` 負值、`rewards` 含 NaN/inf。
- 存檔目錄唯讀（模擬磁碟不可寫）→ save 吞例外、bot 不炸。
- 檔案中途截斷（寫一半的 JSON）→ load 冷啟動。
- 巨大 `pull_counts`（index 遠超 arm 數）→ `load_state` 現有 `if idx in self.rewards` 已擋 rewards；驗證 pull_counts/thompson 載入不引入越界 index 造成後續 `select_arm` KeyError（若有風險，在 load_bandit_state 或 load_state 補 index 範圍過濾）。

> 注：最後一項 monkey 可能揭露 `bandit.load_state` 對 `pull_counts` / `thompson_alpha,beta` 未做 index 範圍過濾（只有 rewards 有 `if idx in self.rewards`）。若測試證實會導致後續 KeyError，於 load 路徑補範圍過濾（min-fix，不重寫 load_state）。

## 動到的檔

- **新增** `grid_engine/bandit_persistence.py`
- **新增** `tests/test_bandit_persistence.py`
- **改** `indicators/bandit.py`：加 `arm_signature()` + `import hashlib`
- **改** `grid_engine/bot.py`：`run()` 載入、record_trade 段條件 save、`stop()` best-effort save、`__init__` 加 `_bandit_last_saved_pulls` / `_bandit_state_path`
- **改** `config/models.py`：GlobalConfig 加 `bandit_state_path`

## 驗收

- 全套測試綠（回報數量）。
- dual-review 兩輪收斂。
- verifier fresh-context read-back + 實跑測試。
- 手動：跑一次 bot（或整合測試）→ 產生 `logs/bandit_state.json` → 重啟 → log 顯示「載入 bandit 狀態 total_pulls=N」且 N > 0。
