# Lessons Learned

（整併史：2026-07-12 202→101 行、2026-07-30 108→115 行、**2026-08-16 第三次 115→本檔**。
三層制：活躍條目留完整判準；已內化進 `~/.claude/rules/general/dev-rules.md` 的降為一行錨點；
完整敘事在 `tasks/lessons-archive.md`（不注入）。備份 `~/.claude/backups/2026-08-16-lessons-consolidation-3/`。）

## 通則 1：靜態結構看起來成立 ≠ 執行期成立（本 repo 最大坑族，已咬七次）
- **已知形態**：假旋鈕（config 有欄位沒接線：`position_threshold`/`position_limit`/`hard_stop`/`trading_mode`）、被覆寫（bandit 無條件覆寫間距，config 間距從未生效）、死路徑（`_run_legacy_mode`、NSGA-II 選項）、重複 class（core/ vs grid_engine/ 同名兩份）、**接線斷在 repo 外**（config `leverage: 20` 從未推到交易所，實際 5x；`fee_pct` 註解寫 maker 2bps，實查為 0）。
- **Rule**（判斷「這個欄位/類別/行號生效嗎」）：
  1. 往上游 `grep -rn "\.<field>\s*="` 追**所有 writer**，不只往下游追 gate。共用檔的「保護性」設計要盤點全部寫入者，否則明寫「僅防 X 側」。
  2. 動核心類別前先 grep live 入口（`grid_engine/bot.py`）的 import，確認用的是哪份定義。
  3. 引用行號當語意證據前，先答「這行在哪個函數、誰呼叫、本場景走不走得到」。
  4. 生效端在**外部系統**（交易所 leverage/marginMode/費率/API 權限）→ 唯一合格證據是查外部系統本身。保證金估算的槓桿一律取 `positionRisk` 實查值（2026-07-12 -2019 事故）。
  5. **SDK 統一資料結構有某欄位 ≠ 你走的那條路由會有值**。要追到實際被呼叫的 endpoint。實證：ccxt 4.5.32 `fetch_positions` 預設走 V3 positionRisk、**不回 `leverage`**，須 `params={'useV2': True}`。
  6. 有生產遙測 → 直接統計該欄位實際取值分佈當 ground truth（2 分鐘，比讀 code 可靠）。
- **spec 寫作規則**：凡「外部 API/SDK 會回傳 X」的句子，寫下之前先跑 read-only 實測並把原始輸出貼進 spec；貼不出輸出就只能寫「待實測」。錯誤事實會沿推論鏈擴散。

## 通則 2：給方案 / 下結論前，關鍵數字逐一標「量過（附值）vs 假設」
- 假設的、且會改變結論的，先量再開口；真金決策（開倉、入金、定實盤參數）不得省。
- 實證：方案 A 假設「均價貼近現價」，實測 690 vs 573，整個方案反向；「補空需 14.3」假設 leverage=20，實際 5x 差 4 倍（寫完隔天重犯）。
- 有結構化生產日誌就**先跑聚合看分佈，再讀 code 解釋分佈**。順序反過來，code 讀越細越容易把「機制上可能」當「實際發生」。聚合前先確認日誌取樣偏誤。
- **送使用者裁決前，把裁決依賴的每個事實斷言標「已驗算 / 未驗算」，未驗算的不得進選項描述。**

## 通則 3：假守衛形態總表（原通則 3 + 通則 6 合併；本 repo 最貴的一族，每條都在真實 mutation 下實測會漏）
1. 斷言太弱（舊實作也回 0）。
2. fixture 沒鑑別力（新舊實作數值恰好相同）。
3. **測資把待測維度壓成常數/退化值**：`high=low=close` 讓 close/high-low 判穿越數學等價；`fee_pct=0` 測不出誤扣 fee。self-review 要固定檢查「fixture 有沒有把待測維度設成 0/常數/退化值」。
4. **測試值 == 欄位預設值 ⇒ 套套邏輯**：`assumed_leverage=20` 的 fixture 讓 `assert cfg.leverage == 20` 恆真，三個映射點改成硬編碼常數、543 條全綠。任何「驗證欄位 X 有被正確讀取/傳遞」的斷言，測試值必須 ≠ 預設值。
5. **驗證器與被驗物共用判準/資料**：校準 gate 用「每筆 fill 有嚴格穿越事件」驗模擬器，而 fill 引擎記錄成交用的就是同一判準同一事件流 ⇒ 必然回 0。不可能失敗的檢查 = 回歸守衛，**呈報時不得包裝成獨立證據**。
6. **期望值從被測 module import**：`assert x == start + 10_000 - MARGIN` 改 `MARGIN` 時期望值同步位移；`BACKOFF_SECONDS` 驅動自己的時間軸。期望值與測資一律寫死字面值。
7. **測資從被測常數推導**：`total_ids = 1000 * (MAX_PAGES + 3)`，常數改錯測資跟著放大。
8. **子字串掃描當行為守衛**（最貴的一條，它守的是使用者看到的金額）：`assert "total_trades += 1" not in body` 改寫成 `= x + 1` 就繞過。行為不變式用行為測試（建真實物件、餵事件、斷言狀態），字串掃描只能當第二道防線。
9. **測資混入其他因素繞過缺陷**：測「畸形筆的游標推進順序」卻在同批混了 id 更高的正常筆。
10. **斷言抓共通後果而非差異**：`assert connect_calls == 2` 分不出「break 重連」與「raise → outer except 重連」，有效的是 `assert errors == []`。
11. **截斷值恰好等於 fallback**：`int(5.7)==5` 而 fallback 也是 5 ⇒ 靜默截斷與正確拒絕不可分辨。改用 `7.3`/`20.9`。
12. **註解宣稱的不變式沒有測試** = 會執行的註解。
13. **接線完全沒被守**：刪掉 `sync_all()` 裡的 `await self._sync_trade_stats()`，625 條全綠——元件對、測試對、就是沒接上，正是 userData 死一個月的同型缺陷。每個「元件 → 排程/呼叫端」的接線都要有一條刪掉會紅的守衛。

- **Rule**：每條新守衛都要**實跑** mutation 看它紅，且說出**紅在哪一行斷言**（「應該會紅」不算）。宣稱「一條測試守住多個缺陷」時逐條指出紅在哪行。
- **斷言錯了會紅，資料退化只會一直綠。** 門檻值要先跑舊碼、新碼各量一次取中間，憑直覺挑的門檻等於沒挑。
- **刪除或收緊任何條件前，先問「有沒有測試是刻意靠它觸發的」**：讀 fixture 的註解與刻意設定的極端值（拉到 100、壓到剛好等於門檻的 1.0）。實證：刪掉 `tp_quantity` 的 `or opposite >= threshold` 後 clamp 測試三條斷言值恰好完全不變 ⇒ 繼續綠、鑑別力歸零。
- **凡斷言「現況 == 期望」的測試（characterization、contract、keyset、golden file）都不知道現況對不對**：liquidated 從 view dict 漏掉，contract test 反而把缺陷寫成規格。名字含 `does_nothing`/`no_cancel`/`returns_empty` 的是待質疑訊號。
- 不變式最好讓它**無法被繞過**（過濾/拋例外/唯一入口），次佳才是「每處記得寫 + 補測試」。
- **判準只套在自己剛寫的東西上，是最容易的自我豁免**——每條判準同時掃既有測試。
- **測 loop 時終止條件不得掛在被測行為上**（2026-07-30 實踩）：mutation 讓迴圈永不終止 ⇒ 測試 hang 兩分鐘被 timeout 殺掉而不是紅，還連帶 kill 掉 restore 那步。終止條件掛在每輪必經、與被測行為無關的點（`sleep`、輪次計數器）。**hang 在 CI 上是 timeout 不是 red，鑑別力等於零。**
- **派 verifier / 外部 reviewer 時不要給自己列的 mutation 清單**，要它自選。2026-08-15 三個獨立輪次自選 18/5/17 條，抓到 3+1+3 條存活，**全部不在我列的清單裡**。

## 通則 4：spec 強動詞會在翻譯中磨損成弱動詞，磨損不被測試抓到
- 「否決/禁止/絕不」經 dispatch prompt 到 code 會退化成「扣分/排序/提醒」，兩份文件各自看都沒錯，損耗在邊界。派工前對照 spec 原文的**動詞**。
- 哨兵值是對值域的未驗證斷言（`-1e6` 連錯三次）；極值會傳染聚合（一個 `-inf` 毀掉 `mean()`）。「淘汰」用顯式排除表達，不用「排最後」。
- **同一個地方修第三次 → 停手回讀 spec 原文**，是抽象層錯了不是值挑錯了。
- 先數清楚不變式有幾個現場（grep 該欄位所有 reader）再逐一補。

## 通則 5：差值 / 比率 / 軌跡型指標，語言直覺一律不可信（已咬四次，全在同一個 delta 上）
- **有定義式就把式子寫出來代數字。**「兩邊都變大/都變小」對差值的影響經常是零：`risk_monitor` 雙側等量減倉 ⇒ `delta = long − short` 完全不變，它降的是 gross 不是中性度。**同一份文件裡，量化嚴謹度不會自動傳染到下一節。**
- **單點標量描述不了一條軌跡，帶號與絕對值會各錯一次**：帶號下降 ≠ 收斂；`abs` 在穿越 0 的路徑上又反向誤判。
- **Rule**：路徑型指標（delta、曝險、庫存）判準至少兩項——**峰值**（`max abs`）+ **逐點軌跡不劣化**，終點值只能當補充。
- **任何「風險變小」的宣稱要配一個存量指標**（`min(long, short)`、gross），否則「把部位清光」會被判成最安全：舊規則窗口末 `min(L,S)`=0.08 vs 新規則 0.28。
- **事後改判準是回測最經典的自欺** ⇒ 若診斷後確實該改：留在 spec 內、標明是看過結果後才改、寫清修訂前後差異、由使用者核可。改成更嚴比放寬容易辯護。

## 通則 6：「不對稱的量」會單調摧毀人工建立的結構（本專案核心教訓，2026-07-26）
- 使用者手動補空建 delta 中性（0.58/0.36），11 天後空頭 −44%、多頭不變。這是**確定性機制**不是隨機：`tp_quantity` 在持倉 > `position_limit` 後兩側都把止盈量加倍（進 0.02/出 0.04），每完整往返雙側各淨減 0.02；等量減對**基數小的那側**成比例更快（3 倍）。逐筆對帳零殘差。
- 為什麼會漏：加倍規則早就讀過，但只當成「回測倉位長不大」的解釋。**同一機制在不同劇本下的後果要各自推一次。**
- Rule：任何「人工建立 / 一次性注入」的結構（對沖倉、seed、手動補倉）都要問「策略的常態行為會不會把它拆掉？」拆解速率用「絕對量 vs 該側基數」算相對衰減。涉及入金/開倉的建議必附這條檢查。

## 通則 7：觀測工具沒有自我監控，觀測結果就不可信（2026-08-15）
- 查 userData 為何不推寫了四輪探針：round 1 漏 listenKey keepalive（6.6 小時「零事件」證明不了任何事）；round 2 的 WS task 拋例外後靜默死亡而 heartbeat 照跑，「bookTicker 凍住」被誤讀成資料停了；round 3/4 的觀察窗口內交易所端零事件，一樣作廢。
- Rule：長時間被動觀察必須自帶 (a) 目標資源續期（listenKey 25 分鐘 PUT）、(b) 連線代數/例外落 log、(c) **同窗對照組與獨立交叉驗證**（REST `allOrders` 印出同窗事件數）。**窗口內沒有真事件的「零觀測」不是證據。**

## 量化域規則（已 encode 進 spec §7 + FIDELITY_NOTES，此處留判準）
- **攤平策略 `realized_pnl`/`win_rate` 恆為正**（虧損全躺未實現）→ 只能用吃未實現的 equity 指標（`final_equity`、`max_drawdown`）當優化目標，且必先建模爆倉，否則 martingale 是算術必勝。`sharpe` 1m 年化自相關膨脹，禁用。
- **尾部風險參數單路徑 in-sample 必被推到上界**，最佳點永遠在懸崖邊（mult=29 卡 seed 邊界的 107 假勝兩次重演）→ 輸出 sensitivity curve、分段窗口驗證、只看方向不定點。
- **限價回測**：`low<=limit`（買）/`high>=limit`（賣）判穿越、成交於掛單價；close 判 + close 成交 = 兩個反向錯（漏 48.5% 成交 + 10.38bps 幻覺改善），淨偏誤不可預測比單向偏誤更糟。
- **成本非方向中性**：按次收費系統性偏袒低換手方案 → 必做 cost sensitivity，排序在合理成本範圍翻轉則不得下結論。
- **做 sensitivity 之前先問「真值在網格裡嗎」**：fee ∈ {2,4}bps 掃得再完整，真值 maker=0 不在網格內就只是「在錯誤區間內的完整」。促銷性質的外部參數另記「非永久」與監控條件。
- **結論對但理由錯，要跟結論錯一樣認真修**——理由是下一輪的路標。（fee=0 補跑後結論方向不變，但「成本吃光 grinding」被推翻，真障礙是逆選擇。）

## 通則 8：斷言字串要挑「只有被測那條路徑會印」的（2026-08-24）
- Context：測「孤兒 bot 橫幅有沒有顯示」，寫 `assert "bot 仍在運行" in blob`。mutation（把整段橫幅改成 `if False:`）**存活**——選單選項那行也印「停止交易（bot 仍在運行）」，斷言被別的輸出滿足。改成橫幅獨有的 `"啟動未確認"` 才紅。
- Error：斷言挑了一個「畫面上不只一處會出現」的字串，測到的是整個畫面而不是那條路徑。
- Rule：對輸出做字串斷言時，先問「這個字串在同一份輸出裡還有誰會印？」。挑被測分支獨有的字面值；挑不出來就先改被測程式碼讓它有獨有標記。**每個字串斷言都要跑一次「刪掉被測分支」的 mutation**，沒紅就是假斷言（這條是通則 3「未紅過的測試等於註解」在字串斷言上的具體形態）。

## 通則 9：換守衛條件時，要盤的是「這個條件在別處還 gate 著什麼」（2026-08-24）
- Context：修 TUI 孤兒 bot，改了 `stop_trading` / `start_trading` / `_handle_shutdown` 三處守衛就以為修完了。L5 檢查才發現 `main_menu` 的 `valid_choices` 也 gate 在同一個 `_trading_active` 上——孤兒狀態下「s 停止交易」根本不在選項裡，**使用者按不到我剛修好的那條路徑**。verifier 又再帶出六處「設定即時套用」同樣 gate 在它上面（靜默不套用）。
- Error：把「修好那個函式」當成「修好那個行為」。守衛條件是跨函式的隱性協定，改一處的語意等於改全部讀它的地方。
- Rule：改任何 `if <flag>` 的守衛前，先 `grep -n "<flag>"` 全檔列出所有讀取點，逐一問「這個點在新語意下對不對」。旗標語意變更 = 全檔級改動，不是單函式改動。UI 可達性（選項出不出現在 choices 裡）算功能的一部分，不是裝飾。

## 已內化進 rules 的條目（一行錨點；完整敘事見 `tasks/lessons-archive.md`）
- **外部 review 抓到內部 review 全漏**（內部 4+1 輪 0 Important vs 外部 4 輪 1 Critical + 4 Important，全實測重現）。機制：內部 reviewer 審的是 diff，findings 藏在沒改的行。→ dev-rules「Code Review」兩輪制、whole-branch prompt 給原則問「哪裡沒被貫徹」。
- **改變路徑的「觸發頻率」會弄壞所有隱含押注該頻率的程式碼，diff 看不到**（`TrialPruned` 讓 prune 從罕見變常態，改 line 454 壞 line 705，內部 5 輪全漏）。→ dev-rules「Code Modification」最後一條。
- **未 commit 工作區 + subagent 跑 `git checkout --` = 靜默毀掉當輪成果**。→ dev-rules「Subagent 派工」硬規則 2（白名單邊界）。subagent 的關鍵驗證宣稱控制端要一行親驗。
- **安全檢查函數不能用 False 表達「我不知道」**（`price=0`/`equity=NaN` 讓 `should_liquidate` 恆回安全）：無效輸入一律 raise，但要先問 (a) 正常路徑會不會觸發（餵髒資料實測）、(b) 爆炸半徑（批次 = 淘汰該項 + 大聲記錄 + 繼續；單次 = 大聲失敗）。sanitize 要涵蓋值域不只型別。

## 環境/API 事實（參考）
- ccxt 合約 `fetch_balance` 的 `total`=marginBalance（**已含浮盈**），equity 要從 `info.assets` 取 `walletBalance`+`unrealizedProfit`，否則浮盈雙算。驗算式：marginBalance = walletBalance + uPnL。
- Binance WS `ACCOUNT_UPDATE` 不推可用餘額/保證金（協定設計），available/margin 交給週期 REST 當唯一真值。
- config 數值欄位在 `from_dict` 就正規化（型別+範圍+fallback），垃圾值不得流進 runtime loop；功能靜默降級（如 Telegram 未設定）啟動時必須給訊號。
- 安全示警前先 `git ls-files`/`log -S`/`check-ignore` 三查實際暴露面。
- Docker TUI：互動用 `compose run --rm`（`up` 不轉發 stdin）；自定義 SIGINT handler 會吃掉 KeyboardInterrupt。
