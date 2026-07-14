# Progress

## Current Task
**觀察期（2026-07-12 起）：#14 全線收工，網格帶新參數運行中，監控恢復狀況。**

生產現況（2026-07-12 收盤時點）：
- 引擎跑新 config：純網格 0.3%/0.3%、thr=0.8（mult=40）、bandit/leading/dgt 全關、增強全關
- 倉位：多 0.58@690.29 / 空 0.34@571.75，**delta +0.24**（使用者裁決 B：維持 5x 不再入金，未補滿中性）
- 權益 ~116、可用 ~6.1、強平價 90.76（尾部風險已解除）、uPnL ~-68.5 待網格磨回
- **氧氣限制**：可用 6.1 在 5x 只夠網格再加 ~2 層，空頭側連續進場會撞 -2019（引擎斷路器會擋）

## TODO（優先序）
1. ~~觀察期複檢（07-13 之後）~~ **完成（2026-07-13）：replay PASS，但發現重大 fidelity 落差 → 衍生 TODO 1a**。
   - **Replay PASS**：全量 98,546 筆重放，9 筆 diff 與首檢完全相同（全部 ≤07-09 19:12 Taipei，舊 code 產物）；修復後窗口 32,630 筆零 diff；新 config 窗口 26.5h、147 筆零 diff。
   - **健檢過的項目**：倉位多 0.58@690.29 / 空 0.34@571.75 不變、權益 115.3、可用 5.95、強平 90.84；雙側 4 張掛單持續在 ±0.3% 刷新（最後刷新 = 最後決策 17:19 Taipei，一致）；新窗口 log 零 -2019、Telegram 修復後零 403；funding 26h 僅 -0.02；07-13 14:16 一筆瞬時同步失敗（REST 錯誤，之後決策照跑，非阻礙）。
   - **⚠️ 警訊（健檢主發現）：觀察期 26.5h 網格零成交、零 REALIZED_PNL**。`fetch_my_trades` 確認窗口內僅 4 筆 = 07-12 手動補空；期間價格走 588→568（~3.4%）網格一張未成交。**不是新故障**：income 按日聚合顯示過去一個月成交本來就 0~3 筆/天、REALIZED_PNL 月合計 ≈ **-0.14**。
   - **機制（code 證據）**：`decision.py:125` `should_adjust` 在偏離 anchor ≥ `grid_spacing*0.5`（=0.15%）就撤單重掛，掛單在 ±0.3% → **掛單被追價永遠搬走**，只有「引擎反應間隙內暴走 >0.3%」才成交。backtester 撮合是「上一根掛單吃整根 1m bar 的 high/low」（`backtester.py:712` `_settle` 先結算後 decide()），掛單存活整整 1 分鐘 → **回測成交率 ~17 筆/天（513 trades/月）vs 實盤 ~1 筆/天，高估一個數量級以上**。FIDELITY_NOTES (5) 有揭露「追價以偏離門檻近似」但未量化幅度。
   - **對現行計畫的衝擊**：「網格慢慢磨回 uPnL -68」在實盤成交率下不成立（月已實現 ≈ -0.14，磨回遙遙無期）。thr=0.8 的方向結論不受影響（mult 40/60/100 恆同 = 只是關裝死），但 #14 回測的 Δeq 絕對值全部高估。
1a. ~~成交率斷層的處置~~ **驗證完成（2026-07-15，branch `feat/requote-semantics`，verifier ACCEPT 7/7，dual-review Ship as-is）：數據否決方向 (i)，剩 (ii) 待使用者裁決**。
   - tick 級實驗（aggTrades 06-06~07-13、校準 gate 三道全 PASS、N=166 組合全揭露）：factor=1.0（掛到成交）§6 判準 3 FAIL——W1 上漲段 -21.1 / W3 震盪 -4.5（只贏 W2 下跌 +27.7，逆選擇主導）；成本 2/2bps 下全程排序翻成 0.5 最優（成交 20 倍但費用吃光 grinding）；factor 0.8 的 +14.9 是 threshold=limit 邊界懸崖（成交驟降 9 倍），依預註冊規則不採納。**「磨回 -68」路線被數據關死；現行 0.5 語意在成本現實下可辯護。**
   - 已上線的中性產物：`requote_threshold_factor` 參數化（預設 0.5 bit-identical，replay 9/9 不變）、tick 模擬器 + PositionBook + aggTrades 管線（未來 requote 類實驗基礎設施）、FIDELITY_NOTES (13)。holdout 05-01~06-05 保持未開封（§6 未全過，依鎖不跑）。
   - **剩餘裁決（使用者）**：(ii) 接受倉位近似凍結 → 另議 -68 出場計畫（等價回 690 平多 / 定點認賠 / 入金補中性後長期持有）；或深究 factor 0.8 regime（需新一輪預註冊驗證，不急）。branch merge 決定見對話。
2. **入金 ~25 補滿 delta +0.24 → 完全中性**（使用者決定時機；5x 下補滿需錢包 ≥207）
3. **symbols-set 併發 race**（#10-A 衍生）：修法傾向砍終端 config 選單（單一 writer 根治）
4. lessons 通則落地檢查：`config.leverage` 假旋鈕要不要接線（啟動時 set_leverage 推到交易所）或改名揭露——現況 config 寫 20 交易所 5x 的分歧會再咬人
5. trading_mode 收編 engine schema（等 #4 驗收後）；頁3 clamp 寫回 session 全站排查
6. GCE 部署三件套（VM/setup script/IP 白名單）——部署後 replay 驗收要在 GCE 重跑一次
7. ~~file logger 修繕~~ **全部完成並 commit（`f64ae2f`，2026-07-12 20:05 重啟驗收過）**：新 log 每行帶時間戳、引擎雙側掛單正常；202MB 舊檔已歸檔為 `log/as_terminal_max.log.archive-20260712`（gitignored，含觀察期首日與歷史 -2019/斷路記錄）。main 已與 origin 同步。
8. ~~Telegram 通知接通~~ **完成（2026-07-12 21:43）**：根因兩個——chat_id 誤填 bot 自身 ID（log 三筆 `403 the bot can't send messages to the bot` 佐證）+ 引擎啟動時憑證為空致 reporter 未建。使用者修正 chat_id=1054193397 後 21:43 重啟，之後零失敗記錄。**07-13 20:00 Taipei 首封每日摘要使用者確認收到，端到端驗收完成。**

## Blockers
無硬阻礙。

## Recently Completed（2026-07-12）
- **TODO 7 重定向後完成（未 commit：grid_engine/utils.py、as_terminal_max.py 尾兩行、tests/test_logger_file_config.py 新檔）**：原前提「-2019/斷路器只噴終端磁碟無痕」是**誤記**——`log/as_terminal_max.log` 一直在收（歷史 1M+ 筆下單失敗、8 筆斷路，多為舊 config 時代產物）。真缺陷三個：(a) format 只有 `%(message)s`，`datefmt` 是死參數 → 事件無時間戳無法定位；(b) 202MB 單檔無 rotation；(c) `basicConfig` 是 import 副作用，web/streamlit 進程也會裝 handler → 換 RotatingFileHandler 後多 writer rollover 會互抽 fd。修法：`%(asctime)s` + RotatingFileHandler(50MB×3, delay=True) + 抽成 `setup_file_logging(force=True)` 只由 `as_terminal_max.py` `__main__` 呼叫（單一 writer）。439 passed（+5 新測試，全部 mutation red-once，含「pytest logging plugin 讓 basicConfig no-op」的假陰性教訓：先綠再紅順序不能省）。dual-review：外部輪 4 should-fix + 3 nit 全修（force=True/subprocess cwd/註解歸因/部署 checklist），Ship as-is；verifier ACCEPT 5/5（獨立 mutation 2/2，mktemp 隔離零污染）。**生效需重啟**，checklist 見 TODO 7。
- **#4 Task 10 replay 驗收 PASS**：全量 98,402 筆重放，9 筆 diff **全部**落在 07-09 19:12 之前、模式一致（long 進 dead mode 未接管止盈單）——正是 `60917cc`（07-10 10:57）修掉的 bug，屬舊 code 產物。現行 code 窗口（07-10 10:57 起，跨 ~2.2 天）**32,481 筆零 diff**，滿足「≥24h 零 diff」驗收準則（GCE 部署後仍需在 GCE 重跑一次，見 TODO 6）。
- **觀察期首檢（新 config 窗口 14:51~15:40，~1h）**：決策 20-30 分/筆是純網格 0.3% 的預期頻率（舊 config 增強全開才會 ~6s/筆，勿誤判為故障）；交易所 4 張掛單與最後一筆決策 orders 逐張匹配（價格/數量/reduceOnly），活體 decide→execute 一致 ✅；倉位多 0.58/空 0.34、權益 116.07、可用 6.12、強平 90.76 與收工快照一致 ✅；income since 14:51 僅 COMMISSION -0.06 + TRANSFER +35，尚無 REALIZED_PNL（未觸及止盈，窗口太短）；無 -2019 跡象（空頭 0.04→0.34 成交成長證明下單通道暢通）。健檢腳本（read-only）：scratchpad `health_check.py`——fetch positions/balance/open_orders + `fapiprivate_get_income({'startTime': ...})` 聚合 incomeType。
- **#14 全線收工並 merge 進 main**（`a49f6b0`，434 passed）：分段窗口驗證（漲/跌/震盪 × 3 場景 × cost sens）確認 mult=40 跨路徑穩健 → config 6 處變更上線 → 入金 35 → 補空 0.30（分批限價，中途撞出 leverage 假旋鈕：config 20 vs 交易所實際 5x）→ 使用者裁決維持 5x、選 B 部分對沖 → delta +0.50→+0.24，強平價 359→90.8
- lessons.md 整併 202→61 行（六條「靜態成立執行期不成立」同族併通則）；UI 持倉顯示兩位小數
- branch `feat/backtest-engine-fidelity`（37+5 commits）merge + push，main == origin/main

---

## 存檔：#14（2026-07-11 重新定向）：先回測定 threshold，再談改 code。原「修 `dead_mode_price`」的前提被實測推翻——見下。

### ★★★ 議倉裁決（真錢，2026-07-11 實測交易所）
`fetch_positions` / `fetch_balance` 實測（read-only，未下單）：
| | |
|---|---|
| 錢包 / 權益 / 可用 | 150.05 / **82.19** / **4.15**（保證金使用率 95%） |
| 多頭 | **0.58 @ 690.29**，現價 573.49，**uPnL -67.74**（水下 20.4%） |
| 空頭 | 0.08 @ 570.17，-0.27 |
| 強平價 | **412.12**（距現價 -28.1%）；**marginMode = cross**（非 isolated！） |
| 掛單 | `sell LONG @603.05 x0.04 RO`（= 日誌 03:57:29 那張假出場單，還掛著）、`buy SHORT @572.08 x0.04 RO`、`sell SHORT @575.53 x0.02` |

**關鍵推翻**：
1. **均價 690，不是貼近現價** → 方案 A（掛均價×1.003 = 692）要漲 **20.7%**，比現在那張 603 還遠。**A 不解凍、讓它更凍。**（教訓：推薦 A 前沒先量均價，被實測打臉。）
2. **cross margin，非 isolated** → #10-B 判死 hard_stop 的前提（isolated 結構性封頂）**事實層面不成立**。412 全帳戶爆是真尾部。
3. **裝死停擺 104h 不是 bug**，是裝死正確地阻止對水下 20% 倉位加碼。真正壞的是 threshold 以「幣數量」計價（0.4），從 690 一路買到 573 從不看保證金。

### 使用者裁決：看強/中性 + **願意入金** → 走 C（對沖到 delta-neutral，網格慢慢補，不實現虧損）
**但發現致命矛盾**：補空到 0.58 → 雙邊都 > threshold 0.4 → **兩側同時進裝死** → 網格停擺 → 「慢慢補」不發生，只鎖定 -68。「對沖 + 慢慢補」與 `threshold=0.4` **數學不相容**（任何值得對沖的倉位都已超 threshold）。
- C 需配 threshold 提高（讓對沖後雙邊 0.58 回正常網格）。
- 補空到中性數字：補空 **0.50**（0.08→0.58），需保證金 ≈14.3，可用 4.15 → **需入金**（建議 30-40 留 buffer）。delta-neutral 後 uPnL 幾乎不隨價變，鎖 -68，網格賺 0.3% 間距補回，價回 690 平對沖出場。

### ★ 使用者最終裁決：**先回測看 threshold 調多高**（不急著入金/改 config）

**seed 注入工具完成**（3 commits `e5ad948`→`0218871`→`b9821ef`，全套 434 passed，14 seed 測試）：
Config 加 `seed_long/short_qty/price`，`_run_terminal_ui_mode` 持倉初始化後 pre-populate seed lot（margin 扣 balance 不扣 fee），seed=0 bit-identical。
- **review 全走完，verdict = `Ship as-is`**：內部 reviewer（I1 legacy 靜默空倉/I2 FIFO 分歧/M1 inf/M2 套套邏輯）→ 修（`_validate_seed` 前置 raise）→ dual-review 外部輪（**no Critical**；I1 fee_pct=0 假綠/I2 揭露搬進 FIDELITY_NOTES (12)/M3 名不副實/M4 defense-in-depth/M5 NaN）→ 全修 + 3 mutation 驗證（fee/NaN/balance 扣減都 red-once）→ Round 2 專案規則 conform → **verifier ACCEPT 6/6**（fresh-context read-back + 實跑 + 獨立 mutation 3/3）。3 commits e5ad948→0218871→b9821ef，全套 434 passed。
- **統一原則**：seed qty>0 但任何原因無法如實注入（負/inf/NaN/price≤0/方向矛盾/走 legacy）→ 大聲 raise，不得靜默空倉（數字定實盤參數）。
- **關鍵保真限制（FIDELITY_NOTES 12）**：per-lot FIFO 先平 index-0 seed lot、與生產 Binance netted 均價分歧 ⇒ 涉及 seed 部分平倉的 threshold 掃描 realized/final_equity 系統性偏離生產，**只可看方向不可當精確預測**。

**threshold 掃描結果**（seed @ 生產均價，資料 06-06~07-10 單一路徑含 -14.8% 下跌，fee 2bps/slip 1bp 基準；起始 equity ≈ 82 吻合生產）：

| 場景 | mult=20(thr0.4) | mult=29(0.58) | mult≥40(≥0.8) | cost sens 最佳 |
|---|---|---|---|---|
| **現狀 0.58/0.08** | eq88.1 dd0.56 dead100% | eq86.3 | eq86.9 dd0.49 dead0% | **穩定 mult20** |
| **對沖後 0.58/0.58** | eq95.7 dd0.50 dead6.5% | **eq107.2** dead6.3% | eq89.7 dd0.47 dead0% | 穩定 mult29 |

**判讀（誠實）**：
- **`mult=29` 的 107 是過擬合陷阱**：threshold=0.58 恰好卡 seed 持倉量邊界，倉位在邊界反覆進出裝死。lessons「尾部參數最佳點永遠在懸崖邊」典型，**不可信**。
- **穩健訊號 = `mult≥40`**：40/60/100 **完全無差異**（對這段數據不敏感）→ 對沖後雙邊 0.58 < 0.8 回正常網格，兩側掛單，eq89.7、max_dd 最低 0.469。從起始 82 補回 ~8。
- **對沖後（雙邊活）比現狀補得多**：mult≥40 對沖後補 7.7 vs 現狀維持補 5.7。支持 C 路線。
- **局限**：單一歷史路徑、只含一段下跌，未測上漲/震盪。threshold 的真正代價（單邊大趨勢無限加倉）在對沖後被 delta-neutral 吸收，這段數據看不到 → **不能外推到不對沖的情況**。cost sensitivity 內排序穩定但絕對差距小（成本擾動 ±1.3 vs 場景間差距 ~18）。

**⚠️ 這些數字的可信度取決於 seed 注入工具正確性 → 需 dual-review 才能讓使用者據此入金**。
**初步建議**：走 C（入金補空到中性）時，threshold_multiplier 提到 **40**（thr=0.8，讓對沖後雙邊 0.58 回正常網格）。不要信 mult=29。

**分段窗口驗證（2026-07-12，補「單一路徑只測下跌」局限；script 在 session scratchpad `segment_scan.py`）**：
W1 上漲 06-06~06-16（574→618, +7.6%）/ W2 下跌 06-15~07-02（617→550, -10.8%）/ W3 震盪 06-25~07-10（564→576, ±5%）× 3 場景（現狀 150 / 對沖後 150 / **對沖後+入金35=185**，上次掃描沒建模入金）× mult {20,29,40,60,100}，60 回測 + 對沖場景 cost sens 54 回測：
- **對沖後 mult≥40 三段全正**：Δeq 上漲 +0.38 / 下跌 +2.89 / 震盪 +7.68，零強平，40/60/100 恆完全相同（= 對現倉規模等效關裝死，僅留 0.8 防暴衝上限）。上漲段 max_dd 三場景最低。
- **mult=20（現行）對沖後在上漲 -6.17、下跌 -10.81**，三段兩負 → 對沖 + 現行 threshold 確定是壞組合，數學矛盾被分段實測坐實。
- **mult=29 再次確認是懸崖**：震盪段 +21.66 貌似大勝，但 thr=0.58 恰等於 seed 量、贏在裝死邊界反覆進出的 artifact；上漲段輸給 40。跨窗口 best 在 29/40 間搖擺（W1=40、W2/W3=29）→ 依 lessons「排序不跨窗口穩定 + 最佳點在邊界」雙重理由棄 29。
- cost sens：每個窗口內排序對 fee{2,4}bps×slip{0,1,2}bps 全穩定不翻轉。
- 入金 35 變體：Δeq 與 150 版完全相同（網格行為不變），只墊高權益基數、max_dd 比例下降（0.47→0.39），符合預期 = 入金純粹買安全邊際。
- **現狀場景警訊**：W1 上漲段 mult=20 Δeq +24.8 遠勝其他 —— 那是凍結的 0.58 淨多頭在漲勢的方向性收益，不是網格能力；同一凍結在 W2 下跌段 -33.6。**「不對沖、維持現狀」= 押方向**，兩段對照是最直接證據。
- 保真警語不變（FIDELITY_NOTES 12）：涉 seed 部分平倉，數字只看方向不當精確預測。
- **結論維持並強化：走 C → threshold_multiplier=40**；三種路徑型態下皆正、不靠方向、不踩邊界。

### ★ C 路線執行中（2026-07-12）：config 已改，等使用者重啟 + 入金 + 補空
使用者裁決走 C。**config 6 處已改完**（停機後直接編輯 JSON 不走終端選單、原子寫、備份 `config/trading_config_max.json.bak-20260712` 已 gitignore、`GlobalConfig.load()` 實載驗證通過）：
1-3. BNBUSDC `grid_spacing` 0.008→**0.003**、`take_profit_spacing` 0.004→**0.003**（把 bandit arm 0 覆寫出來的實盤實際值寫死成契約）、`threshold_multiplier` 20→**40**（有效 thr=0.8）
4-6. `bandit.enabled`→**false**（#13 BD1-3 不學習+靜默切 arm 尾部風險）、`leading_indicator.enabled`→**false**、`dgt.enabled`→**false**（回測驗證的是零增強純網格；`bot.py:342`/`:488` 單一 gate 確認關 master 即全關）
- `all_enhancements_enabled` 維持 false；動態網格判定**不開**：回測器強制增強中性（FIDELITY_NOTES 3）測不了、delta-neutral 後接刀風險已被對沖吸收、min_spacing 0.002 可能把間距收窄到沒測過的值。要開得先給 backtester 接 ATR 增強線再用 seed 工具驗。
- **副作用已知悉**：mult=40 同時把止盈加倍門檻推到 0.8（`decision.py:97` 一參數兩用，回測同一 decide() 已含此耦合）。

**剩餘步驟（使用者端）**：① 重啟引擎，核對面板 GS/TP=0.30%/0.30%、學習模組停用；② **驗收關鍵**：多頭側應重新出現網格掛單（0.58<0.8 解除裝死），殘留那張 sell LONG @603.05 RO 應被 cancel=True 接管重掛——若多頭側仍零掛單，停下來查，**先不要入金**；③ 入金 ~35 USDC；④ 補空 0.50（0.08→0.58）。

### ★ C 路線執行結果（2026-07-12 完成，最終狀態與計畫的偏差都有使用者裁決）
1. **重啟驗收全過**：14:51 重啟後 `decisions.jsonl` thr=0.8 生效、多頭進場單重現（104h 裝死解除）、舊 @603.05 凍結單被接管換成貼價單。
2. **入金 35 到帳**（錢包 184.6）。停機期間網格自己動過：空頭 0.08→0.04（那張 buy SHORT RO 成交）。
3. **補空執行**（我直接下單，marketable limit 貼買一分批）：0.18 @571.78 + 0.12 @572.22 成交，空頭 0.04→**0.34**。
4. **執行中發現新假旋鈕：config `leverage: 20` 從未推到交易所**（grep 證實引擎無 set_leverage 呼叫），交易所實際 **5x** → 保證金 4 倍於估算，第二批撞 -2019。改槓桿被權限系統擋（正確），使用者裁決**維持 5x**。
5. 5x 下補滿 0.58/0.58 需錢包 ≥207（現 184.6），使用者選 **B：不再入金，補到保證金剩 ~5 buffer 為止** → 最終 delta **+0.24**（原 +0.50 減半），非完全中性。
6. **最終**：多 0.58@690.29 / 空 0.34@571.75，權益 116.0，可用 6.1，**強平價 359→90.8**（尾部風險基本解除），雙側網格掛單正常。
7. **已知氧氣限制**：可用 6.1 在 5x 只夠網格再加 ~2 層（每層 0.02 押 2.29），空頭側連續進場會撞 -2019 斷續熄火（引擎斷路器會擋，不失控）。日後入金 ~25 可補滿剩餘 0.24 到完全中性。
8. UI 順手修：`ui.py:153-154` 持倉顯示 `.1f`→`.2f`（trivial，未 commit）。

---

## 舊任務定義存檔（已作廢）：#14 修 `dead_mode_price`（Plan track）
brainstorming 中發現 `dead_mode_price` 公式 `price×((long/short)/100+1)` 使失衡越大目標越遠（反向風控），且 `if entering or pending_tp<=0` 讓特殊止盈單只掛一次凍結失衡比例。**但議倉實測顯示這公式不是主因**（主因是 threshold 計價 + cross margin + 淨曝險），改它救不了現場。留待 threshold 重做後一併處理。

### ★★ 補資料完成，但**原三選項對照計畫已作廢** —— 前提被實證推翻

**資料已補齊**（本 session 完成，未 commit）：
- K 線 `2026-06-06` ~ **`2026-07-10`**（50199 根，`07-10` 檔 1239 根為當日部分資料）
- funding `data/funding/BNBUSDC.csv` 重抓，107 筆，涵蓋到 `2026-07-10 08:00 UTC`
- ⚠️ 下載前刪掉兩個**毒快取**：`BNBUSDC-1m-2026-07-06.csv` 只有 907 根（當日未過完就存檔，`download()` 的 `if output_path.exists(): continue` 永不回補）；`load_funding()` 的 `if path.exists(): return` 同樣不看區間。備份在 scratchpad。
- 事實：這些 kline 檔是 **Taipei 日界**（每檔 UTC 16:00 → 次日 15:59），因為 `download()` 用 `datetime(y,m,d).timestamp()`（本地時區）。與 `decisions.jsonl`（UTC）對時要差 8 小時。
- ⚠️ `07-10` 檔是部分資料，`download()` 的 skip-if-exists 會讓它**永遠停在 1239 根**。下次要延伸資料前必須先刪它。

### 真因（全部來自 `logs/decisions.jsonl` 73123 筆，非推理）
生產多頭 `long_position` **恆為 0.58**，`long_dead_mode=100%`，`buy_long_orders=0`，跨 104 小時零變動。

1. **裝死死鎖已修**（`60917cc`，`cancel=True` 接管殘留止盈單）。生產引擎 pid 28845 於 `07-10 11:57 Taipei` 重啟，跑的是修好的碼。
2. 重啟後 **12 秒**（`07-10 03:57:29 UTC`）掛出**全程唯一一張**多頭單：`sell long @ 603.05, qty 0.04, reduce_only`。`orders` 張數分佈 `{0: 73122, 1: 1}`。
3. 當時 `price=575.245` → 那張單要求 **+4.83%**。之後最高價 **578.25**，未成交。
4. **根因是 `dead_mode_price` 的公式**：`price × ((long/short)/100 + 1)`。`0.58/0.12=4.83` → +4.83%。日誌裡 `short` 曾低到 `0.02` → 公式要求 **+29%**。**失衡越嚴重，要求的出場漲幅越大** —— 反向風控。
5. 公式本有自癒設計（價漲 → 空頭加倉 → 失衡降 → 止盈價下移），但 `_decide_side` 的 `if entering or pending_tp <= 0:` 讓那張單**只掛一次**、凍結在掛出當下的失衡比例，自癒從未生效。

### 回測的獨立障礙：空倉起跑**到不了** threshold
`position_limit = 0.02×5 = 0.1` 之上止盈量加倍（出 0.04 / 進 0.02）→ 持倉被壓在 **0.28** 平衡點，`threshold=0.4` 永遠碰不到。實測（06-06~07-10，價格最大回撤 **14.83%**，632.23→538.45）：
```
mult=20   → final_equity 105.2423  trades 513  liquidated=False  max_dd 0.1009
mult=1e9  → 完全相同
max_long_pos 0.2800   max_short_pos 0.2800   dead_mode_pct 0.00%
```
**補再多資料都一樣。** 要行使裝死路徑，回測必須支援**注入初始持倉**（seed `long=0.58`）。

### 附帶發現（非阻擋）
- 回測**每根 K 線最多成交一張補倉單**（`_settle` 只有一個 `pend[side]["entry"]`）。實測漏掉 **6.4%** 的層數（219 次成交 / 本可 234 層；5.9% 的 bar 本可吃 ≥2 層）。實盤 tick 級追價會連吃多層。
- `Config.position_threshold=500.0` / `Config.position_limit=100.0` 在主路徑（`_run_terminal_ui_mode`）**從未被讀** —— `backtester.py:565-566` 一律由 multiplier 重算。又兩個假旋鈕（只有 legacy helper `:72-74` 讀它們）。

### 已作廢的舊計畫（保留理由說明，別再撿回來）
「補資料 → 跑 (a) 調高 threshold / (b) 關掉裝死 / (c) 開 GLFT 三選項對照」：
- (a) 只是讓倉位凍結在**更高**的位置，不碰出場問題
- (b) 關掉裝死 = 無上限補倉
- (c) 已由分析定案：`clamp(0.5,1.5)` + `max(iq*0.5, q)` 地板 ⇒ 生產 `gamma=0.1` 下只減 **8.7%**
- **三者都沒碰到真正的缺陷**（出場價公式 + 一次性掛單）

---

## 舊計畫存檔（已作廢，見上）：補資料 → 跑真正的三選項對照

**為什麼是這個，不是寫 Phase A-C 計畫**：
- **(c) 開 GLFT 已被分析回答** —— `glft_quantity()` 的 `clamp(0.5, 1.5)` + `compute_quantity` 的 `max(initial_quantity*0.5, q)` 地板 ⇒ 多頭開倉量**最多砍到一半、永不停止買入、永不賣出**。生產 `gamma=0.1`、`inventory_ratio` 中位數 0.871 ⇒ 實際只減 **8.7%**。回測會證實，但數學已先說了。且開它需要 `all_enhancements_enabled=true`，那會讓 bandit 開始覆寫 `gamma`（`bot.py:359-360`）。
- **(a) 調高 threshold 與 (b) 關掉裝死，現在就能測** —— `threshold_multiplier=1e9` 在功能上等價於關掉裝死（實測 `final_equity`/`trades` 與 `mult=20` **完全相同**，因為根本沒觸發）。**不需要 Phase A。**
- **唯一卡住的是資料。**

### 資料缺口（精確）
| | 範圍 |
|---|---|
| 現有 K 線 | `data/futures/um/daily/klines/BNBUSDC/1m/` 共 31 檔，`2026-06-06` ~ **`2026-07-06`** |
| 生產決策日誌 | `logs/decisions.jsonl`：**`2026-07-05 23:36`** ~ **`2026-07-10 20:34`** ← 多頭 `in_dead=100%` 的那 4 天 |
| **缺口** | **`2026-07-06` ~ `2026-07-11`**（含單邊趨勢段） |

這 31 天內單側持倉從未超過 `0.02 × 20 = 0.4`，**裝死模式一次都沒觸發** ⇒ `mult=20` / `mult=40` / 關掉裝死三者數字**完全相同**。問題出在那 4 天，而那 4 天不在資料裡。

### 具體步驟
1. **補抓 K 線**（會連網、寫 `data/`。生產引擎不讀 `data/`，安全）：
   ```python
   from backtest.data_loader import DataLoader
   DataLoader().download("BNBUSDC", "2026-07-06", "2026-07-11", interval="1m")
   ```
   簽名見 `backtest/data_loader.py:367`。抓完確認 `get_date_range("BNBUSDC","1m")`（`:304`）涵蓋到 07-10。
   ⚠️ funding 快取（`data/funding/BNBUSDC.csv`）也要跟著延伸，否則尾段 `rate=0`（FIDELITY_NOTES 第 (7) 條已揭露）。

2. **確認裝死模式在新資料裡真的會觸發**（否則白做）：
   跑一次 `mult=20` vs `mult=1e9`，斷言 `trades_count` / `final_equity` **不再相同**。若仍相同 → 停下來查為什麼（可能 `initial_quantity` 或 `direction` 設定與生產不符）。

3. **跑對照**。生產有效參數（**注意不是 config 裡的值**）：
   ```
   grid_spacing = 0.003, take_profit_spacing = 0.003   ← bandit arm 0，實盤實際值
   initial_quantity = 0.02, leverage = 20, direction = "both"
   limit_multiplier = 5.0, initial_balance = 100
   fee_pct = 0.0002 (maker), funding_enabled = True
   ```
   `scripts/cost_sensitivity.py` 已支援 `--threshold-multiplier 5,10,20,40,1e9`。

4. **驗收指標（spec §7 分層，不得混用）**：
   - **主**：`liquidated`（布林一票否決）、`final_equity`、`max_drawdown`
   - **次**：`funding_paid`、`dead_mode_pct_long/short`（**尚未實作，屬 Phase A**）、裝死 TP 成交率
   - **禁止作為優化目標**：`trades_count` / `realized_pnl`（martingale 假象，攤平策略的已實現獲利恆為正）、`sharpe_ratio`（1m 報酬 ×√525600，自相關嚴重膨脹；強平的回測實測 `-486.97`）

5. **判讀規則**（spec §8 Phase D）：
   - 任一選項 `liquidated=True` → **該選項淘汰**，不論其他數字
   - 排序若在 fee ∈ {2,4} bps × slippage ∈ {0,1,2} bps 範圍內**翻轉** → **不得下結論**
   - 排序未翻轉也要看**差距 vs 成本擾動**。舊資料實測：最佳/次佳差距 `0.120 → 3.219`（放大 **26.9 倍**），低成本端只差 `0.26` ⇒ **落在雜訊裡，結論脆弱**
   - `threshold_multiplier` 響應曲面**非單調**（`mult=10` 劣於 5 與 20）⇒ 輸出 sensitivity curve，**不要只報單點最佳值**

### 這一步的前置事實（別重新發現一次）
- **實盤間距不是 config 的值**。`bot.py:355-358` 在 `bandit.enabled=true` 時**無條件覆寫** `grid_spacing`/`take_profit_spacing`。生產 60001 筆決策日誌實測恆為 `0.003/0.003`（arm 0），而 config 寫 `0.006/0.004`。已有測試釘死（`tests/test_bandit_overwrites_config.py`）。
- **實驗前置條件**：若結論要套用到實盤，必須 `bandit.enabled=false` + config 顯式設定受測間距，否則 live 與 backtest 跑的不是同一個策略。
- **`threshold_multiplier` 一參數兩用**：`decision.py:97` 的止盈加倍條件也讀 `position_threshold`（`opposite_position >= position_threshold`）。調高它會同時延後裝死觸發**並且**改變對手側止盈加倍時機。optimizer 無法歸因 ⇒ 需要 ablation（把加倍門檻解耦成獨立參數）。

### 未 commit 的東西
- `tasks/progress.md`（本檔）、`tasks/lessons.md`（untracked，新增 3 條通用教訓，共 27 條 189 行）
- branch `feat/backtest-engine-fidelity` **未 merge、未 push**（27 commits，dual-review `Ship as-is`）
- `lessons.md` 已超過 workflow 的 ~50 行門檻。六條同族（假旋鈕 / 死路徑 / 被覆寫 / 重複 class / 未接線欄位 / 隱含不變式，皆為「靜態結構看起來成立、執行期不成立」）可合併成一條通則。

---

### #12 起因：blocker 是假的，但挖出六個真缺陷
progress.md 原記載「backtester 不共用 decision.py」→ **錯**。主路徑 `_run_terminal_ui_mode` 早已完整呼叫 `decide()`（`backtester.py:696-715`）。那句話描述的是 `_legacy_grid_decision`（`initial_quantity<=0` 才走的死路徑）。

真正的問題是**回測引擎本身不可用**。六個缺陷，每個都有實證：

| 缺口 | 修法 | 實證 |
|---|---|---|
| **G4** 撮合兩個錯 | `high`/`low` 判穿越、成交於**掛單價** | 44107 根真實 K 線：漏掉 **48.5%** 成交、每筆送出 **10.38 bps** 幻覺價格改善（= 所建模 slippage 1bp 的 10 倍） |
| — 止盈越權平倉 | clamp 到本根 entry 結算前的持倉 | `trades_count` 4 → 2 |
| **G8** 權益漏算 margin | `equity = balance + open_margin + unrealized` | 恆等式缺口 `0.00e+00`（原 988.2 vs 正確 1007.5） |
| **G6** 無強平建模 | `should_liquidate` + `liquidated` 一票否決 | 必爆組 `final_equity` **-1853 → 強平於價格跌 19%** |
| — 安全檢查靜默失效 | 無效輸入 `raise` 而非回 `False` | `price=0` 曾讓強平檢查恆回「安全」 |
| **G7** 成本非方向中性 | `fee_pct` taker→maker + `FIDELITY_NOTES` 誠實化 | 三個 grep 驗收 + 自動化守門測試 |
| **G5** bandit 覆寫間距 | 釘死為測試（不修 bandit） | 生產 60001 筆：實盤恆 `0.003/0.003`，config 的 `0.006/0.004` **從未生效** |
| — 強平只看收盤價 | 改用盤中最不利價 | `(low=60, close=93)` 修前 `liquidated=False`、修後 `True` |
| — `max_drawdown` 只看收盤價 | 谷底取盤中最不利權益 | wick 使其由 `0.000700 → 0.010406` |

### spec §7「一票否決」的六個現場（不變式橫跨模組）
`optimizer.py` ✅（`eligible` 過濾 + `liquidated` 主排序鍵）、`cost_sensitivity.py` ✅、`smart_optimizer.py` ✅（`TrialPruned`）、`optimizer._calculate_param_importance` ✅、`web/services/backtest_service.py` ✅（警告前置進 `notes`）、`web/pages/` 免改（`iloc[0]` 天然安全）。

### dual-review 戰果（dev-rules 強制）
- 內部：4 輪 task review + 1 輪 opus whole-branch → **0 個 Important**
- 外部：4 輪 fresh-context → **1 Critical + 4 Important**，全部實測重現
- Critical 是**我們自己的 fix 引入的**：`TrialPruned` 讓 prune 從罕見變常態，打破 `self._trials[i].trial_number == i` 這個沒寫下來的不變式 → `IndexError` 殺死 `run_smart_optimization`
- verifier（fresh-context）：ACCEPT 7/7

### ★ Phase D 的前置阻礙（Phase 0 中途發現）
真實 K 線只到 **2026-07-06**，而生產出問題的期間是 **07-06 ~ 07-10**。實測 `threshold_multiplier` 掃描：
```
mult=5  → final_eq 104.458   trades 153
mult=10 → final_eq  98.912   trades 257
mult=20 → final_eq 103.428   trades 490   ← 生產值
mult=40 → 與 mult=20 完全相同
關掉裝死 → 與 mult=20 完全相同
```
**這段資料裡單側持倉從未超過 0.4，裝死模式從未觸發。** `threshold_multiplier` 在生產值附近是**惰性參數** —— 直接跑 optimizer 會得到「它對績效無影響」，可能被誤讀成「裝死模式關掉也行」。且響應曲面**非單調**（`mult=10` 劣於 5 與 20）。

**Phase D 前置**：用 `backtest/data_loader.py` 補抓 BNBUSDC 1m 至 >= 2026-07-10（含單邊趨勢段）。

### 成本敏感度（`scripts/cost_sensitivity.py` 實跑）
排序未翻轉（`mult5` 全程最佳），但最佳/次佳差距被成本擾動放大 **26.9 倍**（0.120 → 3.219）。**「排序沒翻轉」不等於「結論穩健」** —— 差距（0.26）小於成本擾動造成的變化（3.1）。

### #12 Follow-up（非阻擋）
1. `smart_optimizer.py:743` `study.best_value if hasattr(...)` —— `hasattr` 對 property 恆回 `True`（只吞 `AttributeError`），multi-objective study 存取 `best_value` → `RuntimeError`。**既有 bug，與 prune 無關。** ⇒ `web/pages` 的 NSGA-II 多目標選項是**死路徑**。正確寫法 `try/except RuntimeError`。
2. `margin_usage` 對 `equity<=0` 回 `inf` 即使零倉位（純觀測欄位）。
3. `optimizer` 三個 sweep 方法死碼、未加 `ValueError` 保護（docstring 已警告）。
4. `smart_optimizer` 的 `except Exception` 範圍過寬（既有）。
5. **#13 bandit 三缺陷**：BD1 `trade_count_since_update`/`pending_trades` 未持久化（126 次啟動只有 6 個 run 累積到 `update_interval=10` → **#6 的持久化實務上只存了一個永不改變的種子**）；BD2 `_cold_start_init` 注入捏造的 reward `0.5`；BD3 `load_state:521` 的 `.get('current_arm_idx', 0)` 靜默重置 arm。
6. Phase A-C 的實作計畫尚未撰寫（spec 已定案，見 §8）。

### 前次任務存檔
### #10-B 已判死（不做 hard_stop）
使用者裁決：對沖 + **isolated margin**（每 symbol 最大虧損被交易所結構性封頂），不設 PnL 硬止損，要讓網格掛著慢慢補。頁4 那三欄唯讀揭露（`hard_stop_enabled`/`max_loss_pct`/`max_position_loss_pct`）應移除或改成明確的設計聲明，避免未來又被當成「未完成功能」撿回來做。
- 若日後要做風險監控，正確方向是**強平距離監控**（`fetch_positions()` 回傳的 `liquidationPrice` 目前在 `sync_service.py:60-73` 被丟棄），純唯讀 + 通知，不碰下單路徑，也不需 backtest 驗證（backtest 不共用 RiskMonitor）。
- 另一個既有缺口：`check_and_reduce_positions()` 觸發條件是多空**同時**超標（AND），**單邊崩盤不會觸發減倉**。

### Recently Completed
**#10-A config 寫入原子/merge/跨進程鎖（2026-07-08，已 merged+push）**：`main` 1b2dd59..310f091（11 commits），全套 310 passed（294 基線+16 新）。抽 `grid_engine/config_io.py` 共用底層（merge-preserve + pid tmp 原子寫 + fcntl.flock sidecar 鎖），`GlobalConfig.save()` 與 `web/services/config_store.py` 皆 delegate，兩份邏輯合一。修三缺陷：撕裂讀（os.replace）、抹 extras（merge-preserve）、lost-update（flock 序列化 RMW）。順修 config_store 固定-tmp + web 側無跨進程鎖潛伏 bug。
- SDD 全程：4 實作 task 全 spec ✅ review clean → opus whole-branch（Ready to merge）→ dual-review（Ship as-is）。
- 併發守衛實證：flock-off 245/300 key 遺失、flock-on 0。Monkey：真實 config round-trip 零遺失、損毀檔 raise 不截斷。
- **dual-review R1（獨立）抓到 opus final review 漏掉的 Critical**：`config/.gitignore` 漏 backup 副檔名 → 含真實 api_key/secret 的 `.bak` 未被擋（root gitignore 只擋 `*.json`），已補 `*.bak*`（check-ignore exit 0 驗證）。
- **重要 accepted risk**：flock 只保護 top-level/symbol 內未知欄位；**symbols 集合的併發新增/刪除仍 last-writer-wins**（呼叫端持鎖外過期快照時丟失/復活 symbol）。已於 config_io docstring 誠實化。

### TODO（下一步候選，優先序）
1. ~~**#10-B hard_stop**~~ — 已判死，見上方「#10-B 已判死」。
2. **symbols-set 併發 race**（#10-A 衍生）：終端選單持鎖外過期快照 → 併發新增/刪除 symbol 丟失/復活。修法：save 前 reload-and-remerge，或**砍終端 config 選單**（單一 writer 徹底消除 dual-writer 前提，與既有「砍終端 config 選單」backlog 合流，較根治）。
3. trading_mode 收編 engine schema（等 #4 驗收後）。
4. 頁3「clamp 後寫回 session」模式未全站排查。

### Blockers
- **#4 人工 Task 10**：GCE 部署 ≥24h replay zero-diff，卡人工前置（非我可推進）。

---

**#9 web/ 遷移完成（2026-07-06）**：23 commits c924947..1b2dd59，全套 294 passed（270 基線+24 新）。SDD 11 task + final whole-branch review（Ready to merge）+ verifier（ACCEPT 8/8）+ dual-review（Ship as-is）全收斂。**已 push**（2026-07-06 本 session）。剩 #4 人工 Task 10（GCE 部署 ≥24h replay zero-diff）照舊卡人工前置。

### #9 成果摘要
- web 全遷新系統：新增 `web/services/`（config_store merge-preserve+原子寫 / history_reader 容錯讀 / backtest_service 黃金映射+雙優化器歸一）+ tests/web/ 24 測試；砍 bot 生命週期；頁1 降級讀 logs/decisions.jsonl；頁2-4 改接；刪 core/ ui/ exchanges/ main.py（~3千行）；config/models.py 瘦身留 4 indicator config。
- 過程抓到的真 bug（部分現存生產）：Config.to_dict 漏序列化 4 欄致網格優化掉 legacy 引擎（同源 bug 曾影響 as_terminal_max optimize_params，已修）；頁3 exchange_type AttributeError；每單數量 min_value=1.0 擋掉所有真實配置；頁3 clamp 寫回 session 致 check_config_updated rerun 風暴（整頁互動失效，改 mtime 比對修）；頁4 風控 tab hard_stop 回歸（唯讀揭露）；頁4 缺同步防護（lost-update 窗口）。
- Task 8 對比裁決：新舊引擎成本對齊後 return 方向仍相反 → 歸因 #4 刻意撮合重設計（新=實盤等價），使用者裁決接受新引擎為基準（tasks/notes.md）。
- **重要事實**：生產 JSON 已是純 engine schema——trading_mode/hard_stop/exchange_type 在 #9 前就被引擎終端選單 save() 抹掉。merge-preserve 是正確防禦碼但只守 web 側。

### #9 Follow-up backlog（非阻擋）
1. **#10 候選（實盤安全，優先）**：as_terminal_max 終端選單 `GlobalConfig.save()` 非原子+抹 extras → 改走 merge-preserve+原子寫或砍終端 config 選單；同捆「生產 grid_engine 補 hard_stop 實作」（現況硬止損無效，spec 揭露）。
2. trading_mode 收編 engine schema（#4 驗收後）。
3. 頁3「clamp 後寫回 session」模式未全站排查；scripts/compare_backtest_engines.py 的 core import 已死（plan 明定保留歷史）；各 task Minor 累積見 .superpowers/sdd/progress.md。


### #9 brainstorming 補充事實（2026-07-06，修正前置調查）
- 前置調查兩點已過時：core/ 現只剩 bot/backtest/strategy 三檔（無 path_resolver/logging_setup）；coin_selection 的 core import 是 try/except 死分支（模組不存在，恆走 fallback），Phase 2 清死碼即可。
- tests/ 零依賴舊系統（270 tests 全綁新系統），刪舊碼無測試連坐。
- 兩套系統共用 `config/trading_config_max.json`（grid_engine/utils.py:29）；grid_engine 落地檔：logs/decisions.jsonl、logs/bandit_state.json、log/*.log（snapshot 僅記憶體）。
- grid_engine 是 ccxt 直連 Binance（ws_client.py:79 fapiPrivatePutListenKey），exchanges/ adapter 層只剩頁4 在用。

### #9 前置調查 — web/舊系統依賴盤點（2026-07-05，scout 完成，未動碼）
- **web/ 對舊系統的依賴只有 5 個 import 點**：`web/state.py:21`(config.models.GlobalConfig) + `:148`(core.bot.MaxGridBot)、`web/pages/2_⚙️_交易對管理.py:26`(config.models.SymbolConfig)、`web/pages/3_🔬_回測優化.py:29,31`(SymbolConfig + core.backtest.BacktestManager)、`web/pages/4_🛠️_設定.py:37`(exchanges list/display_name)。app.py/theme.py/sidebar.py 乾淨；頁1 只間接經 state.py。
- **回測頁遷移缺口（≤5 條初判）**：① BacktestManager 統一抽象消失，新系統要分別接 `backtest/data_loader.py:DataLoader`(download:376/load:158) + `backtest/backtester.py:GridBacktester.run():504`，需包裝層或改寫（★★★）；② `get_available_dates()` 回傳 List[str] vs 新 `get_date_range():313` 回 (start,end) 元組，頁內邏輯要改（★★）；③ `optimize_params()` 拆成 optimizer.py/smart_optimizer.py 兩套，接口不同（★★★）；④ SymbolConfig → backtest/config.py:Config 參數映射需驗證（★★）；⑤ Monte Carlo 段（line 929-1013）**已經在用新系統 GridBacktester**，只需驗證（★）。
- **重要：刪 core/ 不只是 web 的事** — 新系統自身也踩著 core/：`backtest/data_loader.py` import `core.path_resolver`、`coin_selection/ws_provider.py` import core.logging_setup/error_handler/constants、`indicators/*.py`(dgt/funding/leading/bandit) import config.models、`ui/menu.py` import core.bot+core.backtest（舊終端 UI 整個要一起淘汰或遷移）。#9 範圍應含這些工具模組的去留（path_resolver/logging_setup 等宜先搬出 core/ 成獨立 utils）。
- web 啟動入口 `streamlit run web/app.py`（README 方式2）；健檢 `scripts/check_web_system.py`（45 項，前次 37 pass/4 fail 非阻塞）。scout 未實跑 web，可用性數字來自 WEB_TEST_REPORT.md（2026-01-13，偏舊）。

### #8 清理 — 完成（2026-07-05）
- 全套 **270 passed**（268+2 新測試）。reviewer(opus) LGTM 無 must-fix + verifier ACCEPT 5/5（含 revert 驗證新測試會紅、還原乾淨）。
- **asyncio task 生命週期修復（實質改動）**：`order_executor.py` 斷路通知 task 加 done-callback 完成自移除（修長跑累積洩漏）；`sync_service.py` 風控通知 fire-and-forget 原本裸 create_task 無參照（GC 可能在執行前回收），改掛共享 tasks list + 自移除，`SyncService.__init__` 新增必要參數 `tasks`；`bot.py` stop() 迭代改 `list(self.tasks)` 快照（callback 會在 await 期間變動 list，直接迭代會跳元素）。組裝斷言補 `sync_service.tasks is bot.tasks`。
- **記錄修正（#7 follow-up 三項是誤判，未改碼）**：`grid_engine/backtest.py` 並非「無人引用」——`as_terminal_max.py:11`（live 入口）與 `tests/test_backtest_manager_delegation.py` 都在用（#4 Task 8b 才修過它），**保留不刪**；`web/state.py:54` `bot.reload_config` 合法——web 用的是 `core.bot.MaxGridBot`（state.py:148），core/bot.py:405 有此方法；check_web_system 的 required_methods 檢查的也是 core bot，同樣合法。
- **頂層清理**：`test_web_system.py`/`test_symbol_conversion.py` 是 print 式手動診斷 script（測舊 core/web 系統），git mv 到 `scripts/check_web_system.py`/`scripts/check_symbol_conversion.py`（去 test_ 前綴防 pytest 收集；path 修正後實跑通）；刪生成物 `web_test_report.json` + gitignore。刪 `_handle_order_update` 的 sym_config dead assignment。
- Follow-up（非阻擋，歸 #9 或不修）：stop() 快照後 in-flight 下單失敗理論上可再 append 通知 task 逃出 cancel（best-effort 通知，影響極小）；check_web_system.py 的報告仍寫 repo 頂層（已 gitignore）；scripts 診斷依賴的 streamlit 不在 uv 環境、exchanges adapter 缺 create_order（舊系統既有，#9 一併處理）。

### #7 MaxGridBot god class 拆分 — 完成（2026-07-05）
- 8 commits cf3e10d..51def8c，全套 **268 passed**（基數修正：本機 uv 環境 clean HEAD 實測 257 非 ledger 舊記 267；+11 新測試=268）。SDD 7 task 全 Approved + final whole-branch review(opus, Ready to merge) + dual-review（R1 外部 fresh LGTM、R2 專案規則 conform，無衝突免 tie-breaker）+ verifier ACCEPT 6/6。
- 架構：bot.py 1153→767 行（組合根+生命週期+網格鏈+WS handlers），拆出 7 組件：`context.py`(ExchangeContext 兩階段容器)/`locks.py`(SymbolLocks)/`rest_gateway.py`(單 worker REST)/`order_executor.py`(下單/斷路器/is_blocked)/`sync_service.py`(同步/原子區/maybe_sync)/`ws_client.py`(純傳輸，callback 不包 try)/`risk_monitor.py`/`reporting.py`。bot 的 exchange/precisions/funding_manager 是轉發 ctx 的 property（兩階段初始化：組件呼叫當下讀 ctx，絕不 __init__ 快照）。
- 等價驗證：既有測試斷言全數未改（只遷 patch 路徑）、characterization 74 passed 斷言逐行核對、WS 例外語意 characterization（ticker 例外→重連）、組裝斷言（gateway/locks/ctx/stop_event/tasks 全組件單例）、monkey 跨組件鎖競態（canary 經 no-op 驗證會紅）。
- 關鍵修法：`run()` 的 `self.tasks = [...]` 改 `extend`——OrderExecutor 持共享 list 參照，重新賦值會讓斷路通知 task 逃出 stop() 的 cancel。
- **測試指令注意：`uv run python -m pytest tests/ -q`（系統 python3 無 pytest）。**
- Follow-up（非阻擋，歸 #8）：web/state.py:54 呼叫不存在的 `bot.reload_config`（有 try/except 包）、test_web_system.py required_methods 含 reload_config、`_handle_order_update` sym_config dead assignment、sync 的 fire-and-forget risk task 未納管（原版既有）、tasks list 永不移除累積（原版既有）。
- spec `docs/superpowers/specs/2026-07-05-maxgridbot-split-design.md`、plan `docs/superpowers/plans/2026-07-05-maxgridbot-split.md`、SDD ledger `.superpowers/sdd/progress.md` #7 段。

### 前次任務存檔：#4 回測/實盤策略脫鉤 — 程式碼完成 (Task 1-9 + 8b)，雙輪 review 收斂 (Ship as-is)。Subagent-Driven 執行，10 commits 800fd98..7186203，全套 187 passed。
- 純層 `grid_engine/decision.py`(decide()) + `clock.py`(sim-clock) + `snapshot.py`(共享快照) + `replay.py`(決策日誌重放驗收)；`bot.py` `_grid_step` 接線純層 + 決策日誌落地 `logs/decisions.jsonl`；刪 `strategy.py`→shim；`backtest/backtester.py` 吃 decide()+追價語意；`grid_engine/backtest.py::BacktestManager` 委派 GridBacktester(Task 8b，修 plan 誤判「死碼可刪」引入的 NotImplementedError regression)。
- 每 task 獨立 SDD review(Approved) + final whole-branch(opus, Ready to merge) + dual-review(R1 外部 1 Important→tie-breaker 判 INERT→文件揭露 7186203；R2 專案規則全 conform)。實盤等價逐行驗過 bug-for-bug，log↔replay 契約實跑 0 diff。
- **剩 Task 10（人工）**：部署後 ≥24h 跑 `replay.replay_file('logs/decisions.jsonl')` 期望 diff=0 才算 #4 真正完成。
- **Follow-up(非阻擋)**：決策日誌 rotation/停用開關、makedirs hoist、backtester price==decide() 等價鎖測試、FIDELITY_NOTES 補 crossing 只看 close、observability log 補回。
- SDD ledger 詳情：`.superpowers/sdd/progress.md`。#1-#3 先前完成(86acd3e..800fd98)。全部已 push（2026-07-04, HEAD=80a77bc）。

## TODO — 架構審查修復清單（2026-07-03，詳見 tasks/notes.md，依序修）
- [x] **#1 (P0) 下單路徑加固** — commit 86acd3e：clientOrderId + 指數 backoff + 斷路器（僅開倉單成功重置，防 TP 交錯失效）+ 封鎖期不白撤 + `position_adjust_cooldown`（預設 5s）；35 新測試，全套 109 passed；dual-review 兩輪收斂 + verifier PASS（已 push）
- [x] **#2 (P0) 同步 ccxt 阻塞 event loop** — commits b197fd9..800fd98：所有 ccxt REST（下單/撤單/sync/啟動/keepalive/funding）卸載至單 worker `ThreadPoolExecutor`（`_rest` helper；不用 to_thread — 預設 pool 多 worker 會並發打非 thread-safe 的 ccxt Session）；停機檢查 + `shutdown(cancel_futures=True)`（含 init 失敗路徑）
- [x] **#3 (P0) 無鎖並發** — 同批 commits：`adjust_grid` per-symbol lock（skip-if-locked 不排隊）+ `sync_all` 防重入 + REST apply「fetch 鎖外、寫回鎖內無 await」原子區塊 + `_close_symbol_positions` 全程持鎖；鎖序單向 `_sync_lock → symbol lock`。17 新測試（含 monkey：50 並發風暴/全 REST 例外風暴/停機競態），全套 126 passed；SDD 逐 task review + final whole-branch review + dual-review R1 LGTM + verifier ACCEPT
- [~] **#4 (P0) 回測/實盤策略脫鉤**：程式碼完成 (Task 1-9+8b, 800fd98..7186203, 187 passed)，雙輪 review Ship as-is，已 push。**剩 Task 10 人工 24h replay zero-diff 上線驗收**。詳見上方 Current Task。
- [x] **#5 (P1) 回測成本模型** — 完成，10 commits a00d313..80a77bc，全套 **267 passed**，SDD 7 task + final whole-branch review(opus) + dual-review + verifier ACCEPT(6/6 實測) 全收斂 Ship as-is，已 push。純層 `backtest/costs.py`(apply_slippage 四方向不利偏移/funding_charge 帶號現金流) + `Config.slippage_bps`(0.0001 fraction, fidelity-first 預設開)/`funding_enabled`(預設開) + `DataLoader.load_funding`(真實 funding 歷史按需分頁下載快取 `data/funding/<symbol>.csv`) + backtester 主路徑接線(滑價只在 _open/_close、crossing 不動；settlement data-driven 掃真實時點、funding 走獨立 `funding_paid` 不進 trades 防污染 win_rate/PF/count) + FIDELITY_NOTES 9 項重寫(haircut 誠實命名/保守堆疊/mark=close 代理/快取非 range-aware 揭露/legacy 無成本)。**review 抓修 2 個真 bug**：partial-fetch 例外仍寫快取→永久毒化(task review)、抓取窗口本地時區 vs UTC 偏移 8h→尾端 settlement 系統性漏扣(final review I1，Taipei 下必中)。等價守門實測：零成本 bit-identical、funding 不動交易指標。spec `docs/superpowers/specs/2026-07-04-backtest-cost-model-design.md`、plan `docs/superpowers/plans/2026-07-04-backtest-cost-model.md`。Follow-up(非阻擋)見 `.superpowers/sdd/progress.md` #5 段(range-aware 快取/空 fetch 標記/ISO 讀取/optimizer perf)。
- [x] **#6 (P1) Bandit 狀態持久化** — 完成，11 commits 65c0c71..e6c9849，全套 **220 passed**，SDD 六 task + final whole-branch review(opus) + dual-review 全收斂 Ship as-is，已 push。純層 `grid_engine/bandit_persistence.py`(save 原子寫+fsync／load 永不 raise 冷啟動兜底) + `enhancements.py`(live class)/`indicators/bandit.py` 加 `arm_signature` + bot 接線(run 載入／每評估後 total_pulls 變才存／stop 收尾) + `grid_engine/config.py` 加 `bandit_state_path`/`bandit_state_max_age_sec`。**review 抓修 3 個 async-loop crash 洞**：pull_counts 整表取代→select_arm KeyError(final-review)、thompson 有限≤0→np.random.beta ValueError(dual R1 reproduced)、load_state 竄改例外穿透違反永不 raise(task4 review)。**重大：計畫全程誤指 `indicators/bandit.py`(舊 core)，live bot 用 `grid_engine/enhancements.py` 重複 class — 同 GlobalConfig 兩份陷阱，實作者抓到修正**。Follow-up(非阻擋)見 `.superpowers/sdd/progress.md` #6 段(save fsync 阻塞 event loop 宜 offload／load 尾端未 select_arm／context_rewards 未持久化／重複 class 收斂屬#8/#9)。原始問題(bot 從未呼叫 to_dict/load_state→重啟歸零)已解。spec `docs/superpowers/specs/2026-07-04-bandit-state-persistence-design.md`、plan `docs/superpowers/plans/2026-07-04-bandit-state-persistence.md`（6 tasks TDD）。設計：純層 `grid_engine/bandit_persistence.py`(save/load 原子寫+fsync) + bandit.py 加 `arm_signature` + bot 接線 3 處（run 載入/每 10 筆評估後條件存/stop 收尾）；`grid_engine/config.py` 加 `bandit_state_path`/`bandit_state_max_age_sec`。量化 review 折入：arm_signature 不簽 sizing（reward 已驗 scale-invariant，砍 reward_signature）、只復原學到統計不復原瞬時選擇、非有限值 sanitize、max_age 過期冷啟動、replay-invariant 守門。**注意 live config 是 `grid_engine/config.py` 非 `config/models.py`（舊 core）**
- [x] **#7 (P1) MaxGridBot god class 拆分** — 完成，8 commits cf3e10d..51def8c，全套 **268 passed**，SDD 7 task + final review + dual-review + verifier ACCEPT 全收斂 Ship as-is（未 push）。詳見上方 Current Task。
- [x] **#8 (P2) 清理** — 完成（2026-07-05），270 passed，reviewer LGTM + verifier ACCEPT。範圍修正：grid_engine/backtest.py **保留**（live 入口 as_terminal_max.py 在用，「無人引用」是誤記）；頂層診斷 script 移 scripts/check_*.py；task 生命週期修復（GC 風險 + 累積洩漏）。詳見上方 Current Task。
- [~] **#9 (長期) 淘汰舊系統**：前置調查完成（2026-07-05，見 Current Task）——web 依賴面 5 個 import 點 + 回測頁 5 缺口初判；**範圍擴大：backtest/coin_selection/indicators/ui 也依賴 core/，需一併處理**。下一步：brainstorming 定遷移方案（Plan track，需使用者確認）

## TODO — 部署（先前遺留）
- [ ] 建立 GCE VM (e2-small, Ubuntu 22.04, 固定外部 IP)
- [ ] 在 GCE 上執行 `scripts/gce-setup.sh` 部署
- [ ] 交易所 API 綁定 GCE VM 的固定 IP 白名單
- [x] 考慮 BNB 間距加大到 1%+ 或加下單 cooldown 以降低交易頻率 → 併入 #1 的 position_adjust_cooldown

## Recently Completed (2026-07-04b)
- [x] **#5 回測成本模型全流程完成並 push**：brainstorming（4 決策：真實 funding 歷史/固定 bps/按需快取/fidelity-first 預設開）→ 量化工程師視角 spec review（funding 不進 trades、data-driven settlement、分頁、haircut 誠實命名、保守堆疊揭露）→ writing-plans 7 tasks → SDD 執行（每 task fresh implementer + reviewer）→ final whole-branch review 抓修 I1 時區 bug → dual-review Ship as-is → verifier ACCEPT 6/6
- [x] push origin/main：e6c9849..80a77bc（13 commits = #5 code 10 + spec/plan docs 3）

## Recently Completed (2026-07-04)
- [x] #4 相容性 audit（scout）：`dead_mode_enabled`/`fallback_long/short` 全 repo 無非預設值，backtester 用 getattr 讀且 fallback 值 = core 常數 → 純層直接遷移 grid_engine 硬編 1.05/0.95，不加開關
- [x] #4 實作計畫（writing-plans）：`docs/superpowers/plans/2026-07-04-strategy-decoupling.md`，10 tasks 全 TDD（先 red 再 green）+ 每 task commit
- [x] Self-review 對 spec：覆蓋率、placeholder、type consistency 三項 pass

## Recently Completed (2026-06-10d)
- [x] 風控警報頻率可設定 `telegram_risk_alert_cooldown`（秒，預設 300）：from_dict 正規化（非法/非正值 fallback 300）、bot 冷卻改讀 config、選單「7 風控警報頻率」分鐘輸入（清除設定移至 8）
- [x] 補 roundtrip/monkey + bot 冷卻測試，74 passed；dual-review LGTM；commit 28ff2ef 已 push

## Recently Completed (2026-06-10c)
- [x] 風控警報獨立開關 `telegram_risk_alert_enabled`（預設開）：config 三處 + `_check_risk_and_notify` 入口 gate（關閉時不消耗冷卻計時，重開後立即可發）+ Telegram 選單新增「6 開關風控警報」（清除設定改為 7）
- [x] 補測試：config roundtrip/monkey/向後相容 + bot gating 三測，全套 66 passed
- [x] Dual-review 通過（codex LGTM）；commit cdafcbd，連同 9531e97 已 push

## Recently Completed (2026-06-10b)
- [x] Telegram 功能整合 as-grid-auto：通知總開關 `telegram_enabled`、每日摘要時間 `telegram_daily_pnl_hour` 可設定（Asia/Taipei 整點，預設 20:00，非法值 fallback 20）、啟動通知列交易對、每日摘要升級（權益/保證金使用率/未實現/累計已實現/逐幣 L/S+PnL）、選單對齊（狀態顯示+開關+時間設定）
- [x] Dual-review 完成：codex 抓到 hour 無驗證 + .DS_Store 入 diff，已修（`_parse_daily_pnl_hour` + monkey tests；`git rm --cached .DS_Store`）
- [x] 57 passed；commit 9531e97（含移除誤入版控的 .DS_Store；尚未 push）
- [x] 使用者已完成 Telegram token/chat_id 設定並測試成功（Chat ID 曾誤填 bot 自身 ID，已更正為使用者 ID）

## Recently Completed (2026-06-10)
- [x] 查明 Telegram 沒通知/沒日報的根因：`config/trading_config_max.json` 缺 `telegram_bot_token`/`telegram_chat_id`，notifier 靜默停用（非程式 bug；Docker 與 `as_terminal_max.py` 走同一條 MaxGridBot+config 路徑，本來就不依賴 Docker）
- [x] bot 啟動：Telegram 未設定 → log warning；已設定 → 發 `notify_start` 啟動通知（grid_engine/bot.py）
- [x] notifier 新增 `notify_start()` + 測試，43 passed
- [ ] 待使用者在主選單「連線設定 → Telegram 通知」填入 token/chat_id 並發測試訊息

## Recently Completed (2026-06-03b)
- [x] 修復權益/保證金仍失真（浮盈雙算）— ccxt 合約 `total`=marginBalance(已含浮盈)，舊碼 `wallet_balance=total` 後 `equity=wallet+upnl` 又加一次 → 94.49 顯示成 64。`_sync_account` 改從 `balance['info']['assets']` 取 `walletBalance`/`unrealizedProfit`/`availableBalance`/`initialMargin`，equity 自動正確（`grid_engine/bot.py:218`）
- [x] 補 regression + monkey：tests/test_account_update.py +8（重現截圖 94.49、不雙算、fallback、極端值），21 passed；全套 52 passed
- [x] 對照原版 `as_terminal_max.py`：掛單頻率(每 bookTicker tick 跑 adjust_grid + 成交後 stale order-count 致 <1s 洗單)與原版完全一致 → 使用者決定不動
- [x] 面板「保證金%」定義 = 倉位保證金/權益(使用率)，非幣安 2.09%(維持保證金/保證金餘額,爆倉指標) → 使用者選擇維持現狀

## Recently Completed (2026-06-03)
- [x] 修復面板餘額/保證金顯示失真 — WS `_handle_account_update` 誤把 `cw`(全倉錢包) 當可用餘額、且從不更新 margin_used；移除錯誤賦值，available/margin 改由 REST `_sync_account` 獨佔維護（`grid_engine/bot.py:696`）
- [x] `sync_interval` 30s → 10s（grid_engine/config.py + trading_config_max.json），補償 B 方案延遲
- [x] Dual-review 通過：codex 卡死 fallback general-purpose subagent，判定 clean fix（無 must-fix）
- [x] 確認 API key 未洩漏（trading_config_max.json 從未被 git 追蹤，.gitignore 已正確排除）
- [x] 補 monkey test：tests/test_account_update.py 13 測試（核心 regression: WS 不覆寫 REST 真值 + 極端輸入），全測試 34 passed
- [x] 提交 commit 72de3e1（bot.py + config.py + 新測試；尚未 push）

## Recently Completed (2026-06-02)
- [x] 風控警報加 5 分鐘冷卻（`RISK_ALERT_COOLDOWN=300`），避免高頻 ticker 轟炸 Telegram — commit b247c78, pushed
- [x] notifier 測試 21/21 通過

## Recently Completed (2026-04-14)
- [x] 重建 Docker image（--no-cache）
- [x] 本地 `docker compose run --rm as-grid` 驗證 TUI 互動正常
- [x] 診斷交易面板 TP/GS 顯示舊值問題（`sym_state.dynamic_take_profit` 無倉位時不刷新）
- [x] 修復 bot.py：ticker handler 中每次更新 `dynamic_take_profit/grid_spacing` 為 base 值
- [x] 確認 config 保存邏輯正確（回測優化後 0.50%/0.60% 已寫入 config 檔）
- [x] 分析交易 log：BTC Margin insufficient 431K 次、BNB 成交 775 次

### 先前已完成
- [x] 修正交易面板 Ctrl+C 會觸發 Docker 退出：暫存/恢復 SIGINT handler
- [x] 主選單重構：選項 7 改為「連線設定」子選單（交易所 API + Telegram 通知）
- [x] TelegramNotifier 模組 + 21 個測試全部通過
- [x] GlobalConfig 加 telegram 欄位（向後相容舊 config）
- [x] Bot 接入 notifier：崩潰/停止/每日摘要/風控警報
- [x] Dockerfile.terminal + docker-compose.terminal.yml + .dockerignore
- [x] GCE 一鍵部署腳本 scripts/gce-setup.sh
- [x] Monkey testing（極端輸入、並發、邊界值）
- [x] 修正 Docker 互動模式：`run --rm` 取代 `up`
- [x] 全部 push 到 github.com/RamonLiao/as-grid-dragon

## Blockers
無

## Notes

### #4 計畫關鍵設計決定（2026-07-04，spec「plan 階段定案」授權內）
- **多開 `snapshot.py`（共享，不純）**：spec 說 bot/backtester「各自逐字複刻 manager 呼叫序列」→ 改成共享單一 `build_snapshot()`。理由：兩邊各寫一份 = 把 #4 要解的發散重新引入。`decision.py` 維持純函數；snapshot.py 是唯一「不純但共享」邊界（呼叫 manager、`get_signals` 有 append 副作用）。
- **`EnhancementSnapshot` 欄位收斂**：spec 草稿把 leading_reason/atr_tp/atr_gs 分開讓 decide() 重跑分支；改成 snapshot 直接存「已解析的 dynamic_tp/gs」。理由：`get_dynamic_spacing`（含 ATR 60s 快取副作用）必須**條件呼叫**，只能在 snapshot 層做，decide() 重跑分支會破壞 manager 呼叫序列。Task 4 序列等價測試守住。
- **`ofi_history` 是唯寫遙測**（推測，Task 4 測試驗證）：`get_signals` 每 tick append ofi_history(deque maxlen 100)，spec 擔心呼叫次數變動漂移狀態；但追碼發現 ofi_history 只寫不讀（決策讀 current_ofi）。若序列等價測試確認只有它不同 → 呼叫次數變動安全。
- **實盤零改變手段**：Task 1 characterization 先鎖死現行 `_place_grid`/`_should_adjust_grid` 行為 → Task 5 把 `_place_grid` 改**薄封裝**走 `decide()`，同一 place_order 序列 → characterization 斷言不改而綠 = 等價證明。
- **Task 8 是最高風險**：backtester 從靜態階梯（錨在成交價）改追價（should_adjust + 錨在觸發價），回測數字會變——這是 intended（P0 動機本身），舊數字不作回歸基準。
- **決策日誌重放（Task 9）= 強驗收**：實盤每次 decide() 落地 inputs+decision JSON 一行，離線用同一 decide() 重放比對；上線 ≥24h 零 diff 為最終驗收（唯一能驗「快照捕捉完整性」的手段，函數級一致性測試是套套邏輯防不了兩邊吃同一殘缺快照）。

### 風控警報通知設計（2026-06-10c/d）
- 獨立開關 `telegram_risk_alert_enabled` + 頻率 `telegram_risk_alert_cooldown`（秒，預設 300，UI 以分鐘輸入）
- gate 放在 `_check_risk_and_notify` 入口、冷卻檢查之前 → 關閉期間不消耗冷卻計時，重開後若仍超標立即發
- 選單編號變動：6=開關風控警報、7=風控警報頻率、8=清除設定
- 改 config 後需重啟 bot 才生效

### 風控警報無節流 bug（已修，2026-06-02）
- **問題**: `_check_risk_and_notify` 掛在 `_handle_ticker`，ticker 是 ws 高頻推送；保證金超標時每個 tick 都 `create_task` 發 Telegram，`notify_risk_alert` 內無 throttle，會被洗版到 Telegram API 限流
- **修法**: 加模組常數 `RISK_ALERT_COOLDOWN=300`，bot 加 `self.last_risk_alert_time`，超標時先檢查冷卻，未過 300s 直接 return；回到閾值以下後再超標會立即重發
- **改動檔案**: `grid_engine/bot.py`（3 處）

### 交易面板 TP/GS 顯示 bug（已修）
- **問題**: `sym_state.dynamic_take_profit` 只在 `_place_grid` 裡更新，無倉位時不會刷新，導致面板顯示舊值
- **修法**: 在 `_handle_ticker` 的 Bandit 之後、掛單分支之前，用 base 值更新 `dynamic_take_profit/grid_spacing`
- **改動檔案**: `grid_engine/bot.py`

### BTC 保證金佔比問題
- 64 USDC 帳戶跑 BTC 合約，一筆就佔滿保證金
- BTC 已停用，目前只跑 BNB，保證金佔比回到 19.3%

### 開關單頻繁分析
- BNB 0.50%/0.60% 間距對日內波動來說仍然很窄，交易頻率高是正常行為
- 要顯著降頻需加大到 1%+ 或加 cooldown 機制
- BTC 下單失敗 43 萬次（Margin insufficient），無 backoff 機制，浪費 API quota

### Docker TUI 互動方式（重要！）
- **啟動**: `docker compose -f docker-compose.terminal.yml run --rm as-grid`
- **斷開**: `Ctrl+P, Ctrl+Q`（container 繼續跑）
- **接回**: `docker attach as-grid`
- **停止**: `docker compose -f docker-compose.terminal.yml down`
