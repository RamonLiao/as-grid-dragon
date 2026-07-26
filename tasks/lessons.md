# Lessons Learned

（2026-07-12 整併：202 行 → 精簡。原檔備份 `~/.claude/backups/2026-07-12-lessons-consolidation/`。）

## 通則 1：靜態結構看起來成立 ≠ 執行期成立（本 repo 最大坑族，已咬五次）
- **已知形態**：假旋鈕（config 有欄位沒接線：`position_threshold`/`position_limit`/`hard_stop`）、被覆寫（gate 通過但下游蓋掉：bandit 無條件覆寫間距，config 間距從未生效）、死路徑（函數存在沒人走：`_run_legacy_mode`、NSGA-II 選項）、重複 class（core/ vs grid_engine/ 同名兩份：GlobalConfig、UCBBanditOptimizer）、**接線斷在 repo 外**（config `leverage: 20` 從未推到交易所，實際 5x，repo 內 grep 永遠看不到）。
- **Rule**（判斷「這個欄位/類別/行號生效嗎」的完整檢查）：
  1. 往上游 `grep -rn "\.<field>\s*="` 追**所有 writer**（誰覆蓋它），不只往下游追 gate（誰擋它）。共用檔的「保護性」設計要盤點**全部**寫入者，否則明寫「僅防 X 側」。
  2. 動核心類別前先 grep live 入口（`grid_engine/bot.py`）的 import，確認用的是哪份定義。
  3. 引用行號當語意證據前，先答「這行在哪個函數、誰呼叫、本場景走不走得到」。
  4. 生效端在**外部系統**（交易所 leverage/marginMode、API 權限）→ 唯一合格證據是查外部系統本身（`positionRisk`/accountConfig），不是讀 config 也不是 grep code。真金下單前的保證金估算，槓桿一律取 positionRisk 實查值（2026-07-12 -2019 事故）。
  5. 有生產遙測 → 直接統計該欄位實際取值分佈當 ground truth（2 分鐘，比讀 code 可靠）。

## 通則 2：給方案 / 下結論前，關鍵數字逐一標「量過（附值）vs 假設」
- 凡是假設的、且會改變結論的，先量再開口；真金決策（開倉、入金、定實盤參數）不得省。
- 實證：方案 A 假設「均價貼近現價」，實測 690 vs 573，整個方案反向（2026-07-11）；「補空需 14.3」假設 leverage=20，實際 5x 差 4 倍，污染入金決策（2026-07-12，寫完隔天重犯）。
- 有結構化生產日誌就**先跑聚合看分佈，再讀 code 解釋分佈**。順序反過來，code 讀越細越容易把「機制上可能」當「實際發生」（ATR 嫌疑犯 vs 裝死停擺 104h 實證）。聚合前先確認日誌取樣偏誤。

## 通則 3：測試的鑑別力比斷言的嚴格度重要
- **假綠有四種**：斷言太弱（舊實作也回 0）；fixture 沒鑑別力（新舊實作數值恰好相同）；**測試資料把待測維度壓成常數**（`high=low=close` 下 close/high-low 判穿越數學等價；`fee_pct=0` 測不出誤扣 fee——這條寫過照犯，self-review 要固定檢查「fixture 有沒有把待測維度設成 0/常數/退化值」，不能靠記憶）；fixture 讓所有分支走同一條路。
- **斷言錯了會紅，資料退化只會一直綠。** 判準：列出「這 task 最可能出錯的方式」，逐一問「這條斷言會紅嗎」。門檻值要先跑舊碼、新碼各量一次取中間，憑直覺挑的門檻等於沒挑。
- TDD 的「先看它紅」是驗證鑑別力的唯一手段；每個新守衛必須先在真實缺陷前紅一次（mutation test）。
- **凡斷言「現況 == 期望」的測試（characterization、contract、keyset、golden file）都不知道現況對不對**：liquidated 從 view dict 漏掉，contract test 反而把缺陷寫成規格；裝死死鎖被 characterization test 忠實搬進新實作。重構落地後要逐條問每個斷言「這行為為什麼是對的」，名字含 `does_nothing`/`no_cancel`/`returns_empty` 的是待質疑訊號。
- 不變式最好讓它**無法被繞過**（過濾/拋例外/唯一入口），次佳才是「每處記得寫 + 補測試」；罕見觸發的不變式必須有刻意構造觸發條件的測試，否則全綠證明不了任何事。

## 通則 4：spec 強動詞會在翻譯中磨損成弱動詞，磨損不被測試抓到
- 「否決/禁止/絕不」經 dispatch prompt 到 code 會退化成「扣分/排序/提醒」，兩份文件各自看都沒錯，損耗在邊界。派工前對照 spec 原文的**動詞**。
- 哨兵值是對值域的未驗證斷言（`-1e6` 連錯三次）；極值會傳染聚合（一個 `-inf` 毀掉 `mean()`）。「淘汰」用顯式排除表達，不用「排最後」。
- **可操作停止訊號：同一個地方修第三次 → 停手回讀 spec 原文**，是抽象層錯了不是值挑錯了。
- 先數清楚不變式有幾個現場（grep 該欄位所有 reader）再逐一補——上次到第三輪才數清有四個（含一行印出來就自稱完成的）。

## 2026-07-10: 外部 review 找到的，內部 review 全找不到——失效機制可精確描述
- 內部 4+1 輪全 Approved、0 Important；外部 4 輪 fresh-context 找到 1 Critical + 4 Important 全實測重現（與 dev-rules 引用的前次數據幾乎逐字重演）。
- 機制：內部 reviewer 審視的是 **diff**，而 findings 藏在「沒改的行」「白名單之外」「觸發頻率改變的下游」——都不在「這次改了什麼」的視野裡，而 review prompt 定義了視野。
- **Rule**: whole-branch review prompt 給「這條 branch 建立了什麼原則」問「哪裡沒被貫徹」，不只給 diff；外部輪不給 spec/自述/前述結論；「已經 review 很多輪」不是跳過 dual-review 的理由——那個判斷本身是內部視角。

## 2026-07-10: 改變路徑的「觸發頻率」會弄壞所有隱含押注該頻率的程式碼，diff 看不到
- `TrialPruned` 讓 prune 從罕見變常態，`self._trials[i].trial_number == i` 這個沒寫下的不變式塌掉——改的是 line 454，壞的是 line 705，內部 5 輪 review（審 diff）全漏。
- **Rule**: 每次改動列「哪些路徑觸發頻率變了」（例外變常態？分支變常走？集合會缺元素？），對每個問「誰隱含假設它很少發生」。grep 位置索引/`len`/`enumerate`/`zip` 逐一驗。隱含不變式比顯式契約危險：它只存在於「當前頻率」裡。

## 2026-07-10: 安全檢查函數不能用 False 表達「我不知道」
- `price=0`/`equity=NaN` 讓 `should_liquidate` 恆回「安全」。無效輸入一律 raise。
- 但 raise 要問兩件事：(a) 正常路徑會不會觸發（餵髒資料實測）；(b) **爆炸半徑**——optimizer 批次呼叫無保護，一組壞參數炸整個 grid search。批次語意 = 淘汰該項 + 大聲記錄 + 繼續；單次 = 大聲失敗。並區分「例外淘汰」（碼有 bug）與「真實觸發」（參數不好）。
- 同族：「永不 raise」的承諾守門在 load 不夠，要一路守到 consumer——sanitize 涵蓋**值域**不只型別（beta 要 >0），驗收要真的 exercise consumer，不能只斷言 load 回 True。

## 2026-07-10: 未 commit 工作區 + subagent 跑 `git checkout --` = 靜默毀掉當輪成果
- mutate-and-restore 型驗證二擇一：先 commit 工作區讓 git 語意成立；或還原走 `$(mktemp -d)` cp 且 prompt 明文禁止**一切** git 寫入指令（checkout/reset/stash/restore/clean）。白名單開例外 = 黑名單。派出會寫檔的 subagent 前先備份要保護的檔。
- subagent 的關鍵驗證宣稱（尤其資料完整性類）控制端要一行指令親驗，不能只看 report 說 PASS（Task 11「trading_mode intact」實際欄位根本不存在）。

## 量化域規則（回測/優化，已 encode 進 spec §7 + FIDELITY_NOTES，此處留判準）
- **攤平策略 `realized_pnl`/`win_rate` 恆為正**（虧損全躺未實現）→ 只能用吃未實現的 equity 指標（`final_equity`、`max_drawdown`）當優化目標，且必先建模爆倉，否則 martingale 是算術必勝。`sharpe` 1m 年化自相關膨脹，禁用。
- **尾部風險參數單路徑 in-sample 必被推到上界**，最佳點永遠在懸崖邊（mult=29 卡 seed 邊界的 107 假勝兩次重演）→ 輸出 sensitivity curve、分段窗口驗證、只看方向不定點。
- **限價回測**：`low<=limit`（買）/`high>=limit`（賣）判穿越、成交於掛單價；close 判 + close 成交 = 兩個反向錯（漏 48.5% 成交 + 10.38bps 幻覺改善），淨偏誤不可預測比單向偏誤更糟。成交價 == 掛單價寫成零成本斷言。
- **成本非方向中性**：按次收費系統性偏袒低換手方案，比較高低換手選項時成本誤差直接決定排序 → 必做 cost sensitivity，排序在合理成本範圍翻轉則不得下結論；排序沒翻轉也要看差距 vs 擾動量級。

## 環境/API 事實（參考）
- ccxt 合約 `fetch_balance` 的 `total`=marginBalance（**已含浮盈**），equity 要從 `info.assets` 取 `walletBalance`+`unrealizedProfit`，否則浮盈雙算。驗算式：marginBalance = walletBalance + uPnL。
- Binance WS `ACCOUNT_UPDATE` 不推可用餘額/保證金（協定設計），WS 只更新它真有的欄位，available/margin 交給週期 REST 當唯一真值。
- config 數值欄位在 `from_dict` 就正規化（型別+範圍+fallback），垃圾值不得流進 runtime loop；功能靜默降級（如 Telegram 未設定）啟動時必須給訊號。
- 安全示警前先 `git ls-files`/`log -S`/`check-ignore` 三查實際暴露面，別看到明文 key 就喊洩漏。
- Docker TUI：互動用 `compose run --rm`（`up` 不轉發 stdin）；自定義 SIGINT handler 會吃掉 KeyboardInterrupt，需要處暫時換回 `default_int_handler`。

## 2026-07-26: 寫「外部系統會回傳什麼欄位」時，SDK 預設路由是必查項——不是可推測項
- Context：TODO 4 spec §1「問題陳述（**事實，非推測**）」裡寫「`fetch_positions` 回傳本來就含 `leverage`，所以零額外 REST 呼叫」。實測後為假：ccxt 4.5.32 預設路由到 V3 positionRisk，該端點根本不回這欄位（`params={'useV2': True}` 才有）。
- Error：我看到「ccxt Position 結構有 leverage 欄位」就當成「這個帳戶這條路徑會拿到值」。**統一資料結構的欄位存在 ≠ 該欄位在你走的那條路由上有值**——SDK 的 unified schema 是欄位的聯集，缺的填 `None`。而這份 spec 的全部意義正是在修「假旋鈕」，我在修它的文件裡犯了同一種病（通則 1 第 4 條的第六次現場）。
- 連帶損害：錯誤前提污染了下游的成本權衡（誤算成「換 endpoint 要新增一次 REST 呼叫」，實際只是同一次呼叫加參數）與放棄條件，整個 §7/§8 都要重寫。**錯誤的事實不只錯在那一行，它會沿著推論鏈擴散。**
- Rule：spec 裡凡是「外部 API/SDK 會回傳 X」的句子，寫下之前先跑一次 read-only 實測並把**原始輸出**貼進 spec；貼不出輸出就不准標成事實，只能寫「待實測」。查 SDK 時要追到**實際被呼叫的那個 endpoint**（`handle_option_and_params` / 預設 method / options 覆寫），不是停在統一介面的 docstring。

## 2026-07-15: 驗證器與被驗物共用判準/資料 = 回歸守衛，不是獨立證據
- Context：校準 gate 用「每筆 fill 有嚴格穿越事件」驗模擬器沒偷跑，745/745 零違規被我當成「強於 ratio 代理的證據」呈給使用者。
- Error：fill 引擎記錄成交用的就是同一判準同一事件流，驗證對現行引擎**必然**回 0——套套邏輯。它防的是未來回歸（改 touch-fill、記錯 ts），不證明現在沒高估。
- Rule：設計驗證前先問「這個檢查有沒有可能在現行實作下失敗？」不可能失敗的檢查是回歸守衛，呈報時不得包裝成獨立證據；獨立證據必須來自不同資料源或不同判準（live ground truth、對照模型、人工抽樣）。
