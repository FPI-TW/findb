# Full market 執行流程

Daily acquisition 只有 `full_market` 一種 execution profile；Pilot profile 與 `FULL_MARKET_ENABLED`
旗標已退役，切換與 rollback 下限見 [Pilot 退役](deployment.md#pilot-profile-退役與-rollback-下限)。
Backend 與 Fetcher 必須使用相同 `APP_ENVIRONMENT=local|staging|production`；JSON `deployment_target`
仍是環境匹配契約。兩環境固定安裝相同 runtime；production 執行 Full market daily，staging 平時 controls
停止，只在測試改動功能時一次啟動一個 provider。
新抓取只受 scheduler control（Owner start／stop）與 Source 授權（credential scope、每日窗口、平台配額／
account governor、資料驗證）約束；部署不會啟動排程。啟動資料條件只包含已發布 baseline。
Owner 權限、revision CAS 與 Source 認證屬於既有操作／資料存取規則。
TAIFEX 官方 OpenAPI 不需 provider 帳號、API key 或 provider secret。部署設定中的
`FETCHER_TAIFEX_SOURCE_CLIENT_KEY` 是 **FinDB 自己簽發的 Source API key**，供 TAIFEX Fetcher
存取 control／authorization／universe／plan、回報 heartbeat 並交付資料；它不是期交所的 API key。
只有下載官方報表時無須此 key，走正常 FinDB Source API 時仍須認證。其 Secrets Manager 路徑為
`findb/{staging|production}/fetcher/api/source/taifex`，不宣告 `provider/taifex` secret。
Full API 的 Source credential 必須有明確、非空的 dataset list，且涵蓋 admitted feeds；
NULL、空值或 malformed scope 一律拒絕授權與 universe／plan 存取。
maintenance bootstrap 也必須通過有效 provider／dataset scope 與 credential 期限檢查，
但無須先建立 admission 或發布 baseline；授權回應的 blockers 僅反映 maintenance 條件，
並以 `authorized_dataset_keys` 限制官方名單刷新。Running cycle 刷新成功或失敗後，
universe、plan 與新資料抓取仍僅使用當次凍結的 admitted feeds。正常 credential create／rotate
會將省略的 scope 展開成 provider 的明確清單；本規則不放寬既有 API 邊界。

## Profile 與計畫範圍

`findb-fetch-runtime --execution-profile full_market` 是兩環境共同的 daily 入口；`pilot` 以 exit 2
回傳退役訊息，不載入 secrets 或建立 state。每條 feed 依 published universe 凍結完整 cohort；期貨以
product／contract 選擇並保留 sessions。Daily plan 凍結選取結果，不因重試、補抓或 universe 更新改寫。
Controls、first dates、plans、checkpoints、缺漏與告警只有 `full_market` 一組；Full daily、historical 與
maintenance 一律經同環境共用的 account governor。只追補首次日期後的缺口，prepared delivery 在停止後仍可完成。

### 停止後的低成本排空與完成確認

停止 controls 阻止新 provider acquisition；常駐 consumer 仍維持必要的 prepared delivery、
accepted normalization 完成確認、心跳及 graceful shutdown。不能直接略過整個 drain。

所有 consumers 只透過 queue claim／owner recovery 取得工作。停止後只排空原本已持久化的
immutable prepared bodies／receipts，accepted runs terminal 且原 proof 成立才 settle；
無 ownership 的舊 SQLite backlog scanner 不再作為 acquiring／drain 入口。

Additive `full_plan_reconcile` 保存是否有尚未完成 Source evaluation 的本機進度。
`finish()` 與 dirty marker 在同一 transaction 寫入；只有成功取得並保存 Source evaluation
才清除 marker。即使最後一筆 work 已在本機 terminal、程序卻在 evaluation 前中斷，重啟後仍
以原 plan identity 重試完成確認，無須重送已接受資料。首次開啟舊 checkpoint 時，既有未完成
plans 會建立一次 reconciliation marker；Source 確認後不再每輪重算。Marker 不將缺漏改成成功
或 no_data，完整性仍由 Source canonical evidence 判定。

Runtime 執行既有 claim／owner recovery、prepared drain 與
settlement；只有 idle drain 丟棄的完整 health 統計被省略。明確 `--health` 與正常 cycle 的輸出
保留，plan／provider 狀態索引降低摘要查詢成本。

Health 輸出不隨失敗成員數成長，避免把同一個 container 的本機 log 快取（10 MB × 3 檔）裡的
`delivery_timing` 與診斷輸出輪替掉：

- 每個未完成 plan 以 `failure_counts` 回報各原因的成員數；`failures` 只列範例，整份 health 合計最多 20 筆
  （`HEALTH_FAILURE_SAMPLES`，由最早的 plan 開始列）。完整的失敗成員由 Dashboard 交付計畫的缺口明細或
  Admin `GET /admin/delivery-plans/{plan_id}/summary` 查詢；settlement proof 仍帶該 plan 的完整清單。
- Cycle 印出的 health 單行上限 16 KB（`HEALTH_LINE_LIMIT_BYTES`）。超過時先拿掉 `failures` 範例，仍超過則只保留
  純量欄位與 `plans_count`／`unresolved_count`／`date_prerequisites_count`／`provider_outages_count`，兩種情況都帶
  `truncated: true`。`--health` 是一次性查詢，不套用單行上限。
- 內容沒有變化時（不計 `observed_at`），cycle 的 health 每 10 分鐘才重複一次（`HEALTH_LOG_INTERVAL_SECONDS`），
  有變化時當輪就印。Control 的 heartbeat 與輪詢不受影響；log 一段時間沒有 health 行不代表程序停住。

本次 SQLite 變更僅新增索引與 reconciliation table，啟動時在原 schema lock 下建立，不需
PostgreSQL migration，也不重建 checkpoint、清除 body／receipt 或重設 governor。
部署後應在 controls stopped、必要交付完成時，核對 runtime CPU、owner、prepared／receipt、
normalization terminal 與 SDK actual-start 計數；本機測試不替代 production CPU 改善驗收。

### Shioaji 歷史資料

Shioaji 已退出 acquisition catalog、Source credential 管理與新回補 scopes。兩條 TW 分 K
registry 保持 active，供 canonical／Serve 與 Admin 歷史查詢；work、attempt、prepared／receipt、
repair、archive 與 frozen plans 在 Backend 不改寫；Fetcher 本機副本依
[退役 provider 的本機資料](data_maintenance.md#退役-provider-的本機資料)清除。部署條件、資產保留與 rollback 下限見
[Shioaji 退役](deployment.md#shioaji-退役的-rollback-下限)。TW 分 K 改由 FinLab 盤中 feed 提供，舊 dataset 不再更新。
舊每日窗口與輪替規則僅存在於歷史 frozen evidence，不能當成重新啟用 Shioaji 的依據。

### Scheduler scope 與有效抓取範圍

Scheduler 的 `dataset_keys` 是控制／Source 認證的相容範圍，不代表 runtime 目前會抓取所有
列出的 datasets。判讀時分別核對：

| 範圍 | 權威來源 | 用途 |
| --- | --- | --- |
| 相容 scope | `scheduler_dataset`／scheduler `dataset_keys` | 驗證 control identity 與 Source credential；Twelve Data 僅 US，HK 已永久退役 |
| 安裝 feeds | `backend/configs/provider_catalog.v1.json` 及其生成的 Fetcher config | Backend catalog 與 Fetcher config 均為九條：五條日線與 FinLab 的四條盤中 feed（Shioaji 分 K 僅保留歷史查詢，見 [deployment](deployment.md)）；Twelve Data 僅 US EOD，HK EOD 配置給 FactSet |
| 凍結 admission | `admitted_dataset_keys`／`admission_id` | Owner 最近一次啟動時凍結的可用 datasets；停止後投影仍可能保留 |
| 即時抓取授權 | Source `full-market/authorization?consumer=full_market\|maintenance` 的 `acquisition_allowed`／`acquisition_blockers` | 驗證 control、Source credential、baseline 與 provider cutover；deprecated `full_market_enabled` 固定為 `true` |

Source `execution-config?execution_profile=full_market` 已發布 credential 範圍內的排程快照，
不包含 running／quota 授權；Admin 的逐 feed schedule 使用相同 resolver。Fetcher daily 每輪規劃前
讀取這份快照，image 只保存非排程的 execution descriptors 與 bootstrap；API 失敗時停止新日期的規劃，
既有 prepared recovery 與 historical 不受影響。已建立的 plan 沿用凍結的 `deadline_at`，不因快照更新而改寫
（見 [provider_metadata](../architecture/provider_metadata.md)）。

新 daily 入列使用安裝 feeds 與 admission 的交集；新抓取仍須通過即時授權、交易日期／窗口及平台配額。
FactSet 的安裝 feed 也不代表已獲准取得 HK 新資料，仍須發布 baseline 並明確 cutover。

`desired_state=running` 是 Owner 的持續意圖。統一 CLI 在 idle poll 與 cycle 完成後回報
`observed_state=stopped`，進入 cycle 時才回報 `running`。因此 running／stopped 組合須連同
`last_heartbeat_at`、`last_cycle_started_at`、`last_cycle_completed_at`、`last_error`、Source
授權及 provider-work owner／queue 核對；心跳新鮮且 cycle 持續完成可屬正常間歇。心跳過期、
cycle 沒有完成或授權拒絕時，保留相應證據再判定原因，不能只憑這一組狀態宣告故障或健康。

Dashboard「導入與排程」卡片把「Owner 設定」（desired：已啟用／已停止與 revision）和
「Runtime 實況」分開顯示，實況依下表由 Admin scheduler 回應推導，並附一句白話說明。
門檻取自回應的 `expected_cycle_interval_seconds`（Backend `SCHEDULER_EXPECTED_CYCLE_INTERVAL_SECONDS`，
預設 30，須與 Fetcher bootstrap `poll_seconds` 及 `FETCHER_SCHEDULER_CONTROL_POLL_SECONDS` 一致；
欄位缺少時 Dashboard 以 30 秒計），心跳過期與 cycle 停滯門檻皆為 3 × 間隔。

| 條件（依序判定） | Runtime 實況 |
| --- | --- |
| 設定錯誤、尚未啟用、已停用 | 沿用 freshness 判定 |
| 沒有心跳／心跳超過 3 × 間隔 | 尚未回報／心跳過期 |
| desired=stopped，observed=stopped／running | 已停止／停止中（顯示本輪已執行多久） |
| desired=running，`acquisition_allowed=false` | 已啟用但抓取被擋，列出 `acquisition_blockers` |
| observed=running 且最近 cycle 開始後尚未完成 | 抓取中（顯示本輪已執行多久） |
| 最近 cycle 在 3 × 間隔內開始且已完成 | 執行中：等待下一輪 |
| 最近 cycle 在 3 × 間隔內開始但未完成 | 已啟用但上一輪未完成（附 `last_error`） |
| `running_since`（目前 running revision 的 admission 時間）在 3 × 間隔內且之後尚無 cycle | 啟動中：等待第一輪 cycle |
| 其他 desired=running | 已啟用但沒有進入 cycle |

授權 re-check 會在 cycle 之間短暫回報 observed=running；只要最近 cycle 已完成，就不算抓取中。
卡片另以 market-freshness 的持久化 plan 摘要顯示各 feed 當期交易日的 data／expected、blocked
與狀態，用於辨識 cycle 照常完成但沒有產出的情況；完整成員核對仍在「交付監控」。

Backend／Source 暫時不可用時，尚無 active owner 的長駐 runtime 會印出固定的
`fetcher_source_wait reason=source_unavailable` 並退避重查，不因單次 521 退出。等待期間
不會取得新 provider 資料，Source 恢復後仍重新取得 control 與授權。若已有 active owner
且 fence／heartbeat／ACK 無法確認，程序仍可能退出並由 Docker 重啟；應核對 owner
recovery 與原 shared inode，不能把此安全退出改成繼續舊 SDK session。Governor 用量、
unknown debt、cooldown、checkpoint、prepared 與 receipts 均保留。詳細等待與本地 attempt
規則見 [provider work 架構](../architecture/provider_work.md#source-暫時中斷與恢復)。

Unified runtime 以工作 `dataset_key` 核對目前 `installed_feeds`，先於 closed-round retry 與
frozen `scope.feed` 查找。退役 daily／maintenance 不規劃新 plan、不刷新名單、不執行 provider
I/O，也不消耗 maintenance attempt；只以原 owner 排空 server-retained prepared bytes。
所有已接受 runs 達 terminal 後，結算 `failed / RETIRED_FEED_MANUAL_REVIEW`，保留原
manifest、plan、成員、receipt、manual／missing；不將缺漏改成成功或 no_data。Source settlement
不豁免未接受的 prepared deliveries，normalization 未完成時持續保有 owner 與 shared inode。

Migration 的 `legacy_id` 工作還須核對同 provider／dataset／日期的本機 checkpoint：每個已保存
sequence 必須與 server-retained 原 bytes 相符。無法核對、本機 checksum 損壞或原交付排空失敗
時保留 owner，要求明確復原，不能只憑 server prepared 清單為空就釋放。本機 body、逐 sequence
receipt 與 retry budget 不重寫。操作前先停止相關 control、保存 checkpoint，再以相同 shared
inode 復原；不得藉重新啟動 daemon、等待 TTL 或刪除相容 scope 排除舊工作。Staging 實際處置見
[FactSet 部署證據](deployment.md#2026-10-21-staging-hk-切換)。

`expected = data + no_data + missing + blocked`。Data 必須有該 plan 所屬 run 的持久化 canonical 寫入證據；
舊 Pilot 留下的 runs 已脫離 plan（`delivery_part_id` 為 NULL），不計入任何 plan。
Minute 必須有完整 sequenced snapshot；DQ error、空批次、quota failure 不得算成功或 no_data。
No_data 必須有 durable provider evidence。
清單只用來確認缺漏，不阻擋有資料的標的（#629）。Catalog `unlisted_symbols: deliver` 的 feed（FinLab
台股與 ETF 日 K）可以送出不在凍結清單上的標的，也可以送出清單上 mapping gap 成員的資料；兩者都照常寫入
canonical 並留下寫入證據。清單外的標的不計入 `expected`，summary 另回傳 `unlisted_data`（數量），單一
plan 的 summary 讀取另列出 `unlisted`（代碼，上限與缺口明細相同）。Backend 仍擋下三種清單外的列：
同 plan 其他 part 的成員、幣別不是 dataset 預設幣別，以及同 provider 另一個資產類別（例如 ETF）最新
published 名單上的代碼，避免以錯的資產類別建立 instrument。回補也一樣：清單外寫入不影響成員的完成
判定，完成時記在該日期的 `unlisted_symbols`。其他 feed 對清單外的列維持 409。完成判定時，prepare 登記的成員
裡沒有任何凍結成員的 run（清單外或補查列）失敗或出現任何 DQ error，都不影響成員；Source 在 prepare 之後拒絕
這類 body 時，settle 也不等它交付。成員自己的 run 與 body 維持原本的規則。

FinLab Fetcher 讀五張價格表時保留全部欄位（raw 也是完整的表），清單外的代碼用 FinLab 自己的資料分類：
`tw_etf_nav_daily:淨值` 有該欄的是 ETF；`company_basic_info` 中 `市場別` 為 `sii`／`otc`（興櫃 `rotc`
不算）且 `產業類別` 不是 `存託憑證` 的是股票。每個 dataset 只送自己資產類別的代碼，其他（特別股、ETN、
REIT、TDR、興櫃）不寫入，Fetcher log `finlab_unlisted` 記數量；當天沒有成交的代碼也不送。公司資料只有
現在的公司，回補過去日期時已下市的股票分類不到，所以不會寫入。兩張分類表同一個程序每天讀一次，回補的各個
日期共用（2026-11-22 production 實測合計約 0.6 MB 配額）。SDK 子程序回傳清單外欄位時，取到目標日為止最近 60
列（足以帶出較早的收盤價），所以回補舊日期也讀得到當天的值。Daily 在同一輪已有五張表時，於成員之後送出清單外的列，每個代碼在
同一個 work 只送一次，Source 拒絕某個代碼時記下拒絕並繼續其他代碼，不影響成員結果與 settle；回補在成員與
補查成員之後送出。官方名單上但公司名單沒有的普通股（`classification_gap`），只要 FinLab 目錄有該代碼就是
mapped，不再列為 mapping gap。

逐筆請求的 Twelve Data 只會回答被問到的代碼，所以美股名單刷新時另外記一份**目錄補查**（`universe_supplement`）：
`/stocks` 目錄中交易所掛牌（NYSE、Nasdaq、NYSE American、IEX、Arca、Cboe，不含 OTC）的股票型別（Common Stock、REIT、
ADR、DR、Limited Partnership、Trust），扣掉官方名單已有的代碼，再依官方名單同樣的名稱規則與代碼樣式排除權證（`.WT`、Nasdaq 第五碼 W）、
Unit（`.U`、U）、權利（`.R`、R）、特別股（`.PR`、`代碼-字母`、Nasdaq 第五碼 M／N／O／P）與測試代碼。補查與 release
分開保存，目錄變動不會產生新 release，也不影響 release digest 與變動門檻。Plan 建立時凍結交易日當天或之前最新的
一份（`daily_delivery_plan.supplement_id`），Source 的 plan 回應附在 `supplementary`；Fetcher 在 daily manifest 帶上
同一份（Source 核對），在沒有成員還要抓取時（已送出、仍在 normalization 的成員不算）、同一輪 settle 之前逐一
請求，有資料就寫入並計入 `unlisted_data`，沒有資料、Source 拒絕或暫時錯誤都只記在該 work 的 journal，不算缺漏、
不影響成員結果與 settle。目錄變成空的也會記下一份空補查，之後的 plan 就不再請求；同一天重複出現的內容以最後
一次觀測為準。2026-11-22 的 production 目錄依上述規則約
628 個代碼，每日請求約增加 11%。回補每個日期也凍結該日 plan 的補查（沒有 plan 時為當天以前最新的一份），與之後
release 的成員一起當補查成員。
Summary 另回傳 `mapping_gap`：blocked 中屬於 mapping gap 的成員數。`is_late` 只看抓取進度，
也就是超過 deadline 時，扣除 mapping gap 後仍有 missing 或 blocked；mapping gap 照樣計入 blocked
與缺漏告警，不會因此被隱藏。

## 啟動契約

每個 feed 只要求同 provider／dataset 有 `published` official baseline，且 effective date
不晚於交易所當地今天。至少一個 feed 符合即可啟動，並只凍結符合條件的 feeds。日曆不必先設定。
Enrollment、installation receipt、readiness ACK／expiry、account／capacity proof 與
registry 額外 metadata 不列為啟動資格 gate。舊 `full_market` metadata 若為字串或 list，
start 視為缺少 optional governance，重新建立 activation date；有效 contract declaration 與
既有 immutable first date 仍保留。未知 scheduler／scope 仍依 API definition validation
處理，Owner 操作權限與 CAS 保留。

Owner 透過 `PATCH /api/v1/admin/schedulers/{scheduler_key}`，傳入 `desired_state=running`
與 `expected_revision`。Backend 在同一 transaction 重查 baseline 與當時可用的 calendar metadata，凍結 ready datasets、
baseline IDs、可空的 calendar revision IDs 與首次日期，並寫入操作 audit；admission 不必綁定 enrollment。
新的 ready sibling 須停止後再啟動，重複 running PATCH 不改凍結範圍。
每 dataset 首次日期由交易所當地今天決定；migration 保留合法 legacy activation_date，malformed
legacy 日期在首次 start 時以 immutable first date 修正。停止、重啟與重新 enrollment 均不重設日期。舊 `/admin/feeds/{dataset}/activate`
與 `/deactivate` 在 Owner 認證後回傳 410；沒有 per-feed activation、manual readiness_approved 或五日門檻。

每次 provider call 前立即查 effective Source authorization。Owner 停止、Source credential 撤銷／到期、
超出每日窗口或平台配額不足時，禁止新抓取與 plan。已保存 prepared payload／frozen plan 的 delivery、
normalization、canonical 與稽核可繼續；不重抓已 prepared 資料。Ingest 啟動不再調整 controls，
GET 投影永遠唯讀；停止後仍需 Owner 手動啟動。

### 緊急停止

`FULL_MARKET_ENABLED` kill switch 已由 Backend CLI 取代；預設 dry-run，`--apply` 才寫入：

```bash
docker exec findb-ingest python scripts/emergency_stop_controls.py \
  --actor <operator> --reason <text>
docker exec findb-ingest python scripts/emergency_stop_controls.py \
  --actor <operator> --reason <text> --apply
```

工具依 providers → environment → controls 順序加鎖，將所有非 retired control 設為
`desired_state='stopped'` 並遞增 revision，每個 control 寫一筆 `admin_audit_event`
（`actor_type=deployment`、`details.reason=emergency_stop`）。Observed state 待各 runtime 回報後才改變，
prepared delivery 仍正常排空。任何 RDS snapshot restore 後，必須在 Fetcher consumers 啟動與 Owner
start 前執行；ingest 啟動不調整 controls，production 可先只啟動 ingest container（Fetcher 保持停止）
再於其中執行。本機改用
`uv --directory backend run python scripts/emergency_stop_controls.py`。

## 有效日曆與每日政策

Backend 是政策權威。`runtime/configuration` 每個 provider 一個獨立開關：
`TWELVE_DATA_SKIP_NON_TRADING_DAYS`、`FINLAB_SKIP_NON_TRADING_DAYS`、
`TAIFEX_SKIP_NON_TRADING_DAYS`、`FACTSET_SKIP_NON_TRADING_DAYS`、`HKEX_SKIP_NON_TRADING_DAYS`、
`FMP_SKIP_NON_TRADING_DAYS`。
每個開關皆為 `true`／`false` 字串，預設皆為 `true`，兩環境欄位相同，經部署生效；
`FACTSET`、`HKEX` 與 `FMP` 的開關在 runtime configuration 中可省略。
Fetcher 透過 Source 取得政策與每日決策，不保存另一份日曆或政策設定。預設略過非交易日；
明確設為 false 才在週末／休市照常執行。排程時間、交易所時區、Owner controls、平台配額、429 與既有重試規則仍有效。
不設連續十筆失敗中斷規則。

有效日曆逐日優先讀 published revision；缺年份或日期時依市場`weekend_days`補足，尚未建立market row的
內建市場採週一至週五開市／週六日休市，draft 不參與。
TW／US／HK／CN／TAIFEX 使用固定市場時區及正常收盤時間，published 提早收盤優先。
查詢不寫 DB、不自動發布；API 保留 `calendar_source=published|weekday_default` 與可空 revision。
缺日曆可預設，DB／認證／通訊故障則正常報錯。部署與 Owner start 不 seed 或檢查 calendar completeness。

過往年份已於 2026-11-23 經 Admin API（json preview → apply → publish，每個市場年度一份 full_year revision）發布，
兩環境相同：TW 1990–2026、TAIFEX 1998–2026、US 1953–2026（production 另有 2027–2028）、HK 1987–2026。依據：

- TW：證交所 FMTQIK 的實際成交日，含 2001 年前的星期六與補班日。`settlement_only` 只用於有官方休市日 CSV 的
  2023 年以後，更早年份春節前無交易的日子記為 closed。
- TAIFEX：期交所臺股期貨（TX）一般時段每日行情；1998-07-21 上市前的日期記為休市。
- US：NYSE 日曆（`pandas_market_calendars`），含特殊停市與提早收盤，以 FMP `^GSPC` 交叉核對。1952 年以前
  NYSE 星期六也有交易，但指數資料沒有星期六，因此不發布 1953 年以前。
- HK：XHKG（`exchange_calendars`），以 FMP 與 Yahoo `^HSI` 交叉核對。1999 年以前的佛誕依實際行情記為開市；
  1991-02-14、1993-10-01、1997-02-06 記為休市（Owner 確認）。FMP 在港股休市與颱風停市日會回重複前一日的值，
  不能單獨用來判斷開休市。
- 舊年份的 TW、TAIFEX、HK 沒有填交易時段，effective calendar 顯示目前的市場預設時間，只影響顯示。

catalog 設 `calendar_required: true` 的 feed 例外，日期只用 published revision，不用 weekday 預設：
- 建立 plan 時，該日沒有 published revision 就以 409 `{"code": "CALENDAR_UNPUBLISHED"}` 回絕，不凍結
  決策，所以週末也不會先被凍結成 `skipped_calendar`。
- Fetcher 把該日工作改成 `retry`，failure code 為 `DAILY_CALENDAR_UNPUBLISHED`，一小時後再試；同一天其他
  市場的 feed 照常規劃。發布日曆後下一次重試就會建立 plan。
- 過了完成期限仍沒有 plan 時，缺漏告警的細節是 `CALENDAR_UNPUBLISHED` 與 `calendar_market`。
- 歷史回補：範圍內有任何一天沒有 published revision，預覽回 `calendar_unpublished`，建立時整個範圍被拒絕。
- 歷史回補的「今天」與到期日依 feed 的 calendar market 計算，同一 provider 的 feed 可以在不同市場。

`daily_execution_decision` 以 provider／dataset／日期／profile 唯一識別，原子保存 planned 或 skipped_calendar，
凍結當日 calendar／session bounds／policy hash。既有 plan、成員、first dates、prepared bodies 不因後續更新改變。
`skipped_calendar` 是最終決策，政策變更不重開；可另建受當時政策約束的人工回補。
Source 仍拒絕未來日期、超前 fetched_at 與未結束 session；非交易日使用正常收盤時間。

人工回補在建立時凍結所有日期，略過項目標示 skipped_calendar；全略過的 request 完成但不產生成功 run。
Claim 不重算政策；可先建立當日請求，但必須到凍結快照的市場收盤時間才發給 lease，
每次新抓取授權也核對同一界線。Published 提早收盤及非交易日正常收盤皆使用建立時快照，
後續日曆修訂不改變既有工作的界線；已準備的交付可繼續完成。
既有失敗／過期與序列執行規則保留。略過不計入 expected、missing 或 no_data。
實際非交易日回應依原有 canonical evidence、完整 minute snapshot 與 DQ 判定，空回應不等於成功。
Migration `91a4c7e2d630` 保留既有計畫及歷史項目，用升級當時有效日曆建立 `legacy_migration_snapshot`，
不聲稱它是原始建立時的日曆；不補入舊回補範圍中原本省略的日期。這是新的 rollback 下限。

### 來源尚未發布的等待與休市判定

FinLab daily 回傳 target-not-ready，或 FactSet daily 成功查詢但出現 `no_source_row` 時，以
該交易日的排程時刻（work 凍結的 feed 的 `scheduled_time`、`source_timezone` 與 `target_date_lag_days`）加上
feed 的 `publication_retry_window_seconds`（catalog 與 execution-config 的欄位名稱；Fetcher runtime 內部的 feed
欄位是 `completion_window_seconds`）作為 immutable publication deadline。不從 work 建立時間起算：停止後補抓的
過去日期，排程窗口早已過去，缺列即為最終結果，不會再等一整個窗口。
截止前安全 close／drain 並排到 provider FIFO 尾端，單次最長等待 15 分鐘；這種等待不消耗
local `FULL_WORK_MAX_ATTEMPTS`。FactSet 只保存缺列 observation，不將空結果保存成可 replay 的
authoritative snapshot，因此下輪會重新查詢。FinLab 與 FactSet 的其他永久格式、mapping、DQ、
auth／scope 錯誤仍依原規則 failed／manual，不得誤分類成尚未發布。

每次 publication retry 都在任何新 provider I/O 前，透過持有 fence 的 Source operation 重新讀取有效 published calendar，
但只比較目前狀態與 frozen decision，不自動重算或改寫原計畫。若 frozen decision 是 `planned`，
且其後的新 published revision 已將該日標示休市，立即停止重試；這項偵測不以目前 skip flag
作為 gate。Provider work 保存 `CALENDAR_DECISION_REPAIR_REQUIRED`，local member 標示
`calendar_repair_required`。Operator 依下節流程核對並取得授權後，才可更正 frozen decision；
不得以新 revision 自動覆寫。若目前 skip flag 為 false，work 仍維持人工處理；須先明確決定是否
透過部署啟用政策，不能跳過下節對目前政策與證據的檢查直接修復。

四個現役 provider 的 Full daily canonical 回合，另在第一個需要新 provider I/O 的成員 claim
前查詢一次同一 calendar status；FactSet 則在第一次 snapshot 請求前查詢。已完成、manual、
deferred 或未 mapped 的成員、本地已有 prepared bytes 的排空與 historical 回合都不觸發查詢。
若結果為 `repair_required`，work 以 `CALENDAR_DECISION_REPAIR_REQUIRED` failed，不改寫任何
local member checkpoint（既有 no-trade 等證據保留），不授權、不呼叫 provider，也不消耗 member attempts；
查詢本身失敗時同樣在 claim 前停止，不扣額度。這保護舊 `planned` 決策在略過政策啟用後的休市日，
不會用滿一般失敗額度；frozen decision 仍只經下節人工流程更正。

到 publication deadline 後，FinLab target-not-ready 成為 terminal manual；FactSet
`no_source_row` 保存 authoritative snapshot 並以 terminal `partial` 結算。Provider work 的等待、
截止時間、failure code 與每次 observation 都須留在原 work 證據中；重啟、
清除 journal 或重置 checkpoint 不得延長窗口。若之後來源補齊，使用正常歷史回補建立新 work。

### Shioaji 空 Kbars 與凍結 mapping 修復

> Shioaji Fetcher runtime 已移除（見 [rollback 下限](deployment.md#shioaji-退役的-rollback-下限)）。本節 CLI 只存在於 `fetcher-v0.1.21` 以前，Backend 保留歷史 inventory／preview；controls retired 後 repair apply 拒絕。內容僅供稽核既有紀錄，不可重新啟動舊 CLI。Pilot 退役 migration `a7c3e1f9d2b4` 已刪除綁定 Pilot 的 closure／repair rows，下文的 Pilot 描述只說明退役前行為。

七個 Kbars 向量皆空記為 `empty_response`，原因維持 `unknown`；不能推定尚未發布、無交易或
entitlement 不足。缺欄、長度不一致、錯誤日期及其他永久格式錯誤仍走原本有界失敗流程。
正式 decoder 拒絕、尚未建立 snapshot 的已知格式錯誤亦記為 `invalid`，不占空回應／未知觀測
額度或觸發十五分鐘退避；僅保存 reviewed error code／reason，原 native 用量與一般失敗次數保留。
無明確格式證據的 transport／child-boundary 錯誤仍記為 `unknown`。
有效日期快取沿用原 bytes；無效舊快取保留原 row／checksum，負向 observation 不授權 replay。

Pilot／Full daily 每個原 work／provider symbol／日期最多三次空回應或未知派送觀測（包含首次），
重新觀測至少間隔十五分鐘，並受既有抓取窗口、owner、quota、unknown debt 與 cooldown 限制。
派送前的 journal reservation 在不確定邊界保守計數；可證明 Kbars 未派送的等待或 bootstrap
失敗記為 `predispatch`，不占三次觀測額度。SDK 建立、登入、contracts 載入失敗仍依 `FULL_WORK_MAX_ATTEMPTS` 一般
失敗政策處理，已發出的 bootstrap native／登入用量不退款。程序中斷未能保存未派送證據時仍為 unknown。
即使尚未保存任何空回應，未知派送也受三次上限與十五分鐘退避限制；不能因未取得回應而重新
開啟額度。未證實為空時，本地記為 `unknown_dispatch`、Source 維持 `source_error`，不偽造空回應。
舊快取中的空回應匯入為一次既有觀測，不把歷次本地重播算成新的 native 請求。
舊快取沒有原始觀測時間時，從首次辨識起保守等待十五分鐘；重啟不重新起算。
等待只退回本輪新增的 local claim，既有 failure attempts 與已派送平台用量不退款。
觀測等待、額度耗盡與派送前 calendar 授權錯誤會保留仍存活的 SDK session；gateway 自行清理
transport／child 故障，runtime 仍在偵測 child 死亡、pause 或 close 時回收，避免無效重登入。
三次耗盡為 manual；缺漏與告警保留。新 published calendar 與原計畫衝突時，在重新抓取前停止並
要求 calendar repair，不能自動改寫 frozen decision。no_data 仍須符合既有 durable evidence 契約。
Historical 共用快取驗證，保留原重試政策，不使用 daily 的觀測額度或人工恢復 grant。

| 狀態 | 解讀與處置 |
| --- | --- |
| `acquisition_window_closed` | 等待下次窗口，不代表 provider 失敗 |
| attempts=0／無 observation | 尚未嘗試，不能視為已被 SDK 拒絕 |
| `empty_response` | 有空回應證據，原因未知；檢查觀測額度與下一次時間 |
| `unknown_dispatch` | 曾保留未知派送責任，尚未證實空回應；同樣檢查觀測額度與退避 |
| `mapping_gap` | 凍結官方商品尚缺 provider mapping，須先取得 catalogue 證據。Plan 只缺這種成員時，daily work 以 `partial` 結束而不是 `failed`；plan 統計仍把它們算作 blocked（見 [provider_work](../architecture/provider_work.md)） |
| `retry_exhausted` | 本地一般失敗額度（`FULL_WORK_MAX_ATTEMPTS`，5 次，所有 provider 一致）耗盡，不等於五次 native 請求 |
| `no_data` | Twelve Data 回報該檔該日無資料，或單檔回 404（`not_found`，例如清單已列出但日線尚未提供的新代號），不停止其他成員；依額度重試，不算成功或 no_data。兩者以 Fetcher SQLite `full_work_diagnostic.category` 分辨，Admin API 與 Dashboard 都只顯示 `source_error` |
| `no_data_exhausted` | `no_data` 額度耗盡（共 6 次：plan 重試輪每輪結算後依序等待 30、60、60、120 分鐘，最後一輪固定在截止時間前 30 分鐘，也就是開始後 11 小時 30 分；進度見 Fetcher SQLite `full_no_data_round`），缺漏須另行回補；只影響該檔。Breaker 連續觸發 5 次且其間沒有資料才結束 plan，此時先查是否為未發布休市日 |
| `prepared/normalizing` | 應查 Backend run 與序列收據；可能已完成，不能據此宣稱 queue 卡住 |
| `source_proven_no_trade` | 已完成。FinLab 當日四個價格全空、成交量為空或 0，視為當日沒有成交，附整張表 bundle 的 SHA-256 回報 `no_trade` 證據；TAIFEX 依報表的無成交列。<br>FinLab 價格全空但成交量大於 0 不算無成交，而是照常交付一列日 K（成員狀態為一般的已完成）：開、高、低、收都填該成員最近一個有值的收盤價（不限前一個交易日，除權息日也一樣），成交量照 FinLab 的值。這種成員每個交易日都有，成交股數多半不足一張。共用的五表 bundle raw 保留原始空值，並在 `close.previous` 記下所用的收盤價與其日期 |
| `member_unavailable` | Provider 回應本身正常，但只有該成員不能使用。只擋下該檔，不重試、不重新下載共用表或報表、不結束這一輪；Backend 記為 blocked，細分原因看 `full_work_diagnostic.category` 的 `member:<code>`：<br>FinLab：部分價格欄位空值、價格全空且成交量大於 0 卻沒有任何較早的收盤價、OHLC 越界、成交量非整數（`source_error`），或表中還沒有這個代號，例如剛上市（`member:missing_symbol`，`empty_response`）。<br>Twelve Data：回應的幣別與凍結 release 成員不同（`member:identity_mismatch`，`source_error`）；該檔的日線值不能用：開盤或收盤落在最高、最低之外，或最高低於最低（`member:ohlc_bounds`）、價格不是數字（`member:non_numeric`）、不是有限值（`member:non_finite`）、為負（`member:negative`）、成交量非整數（`member:volume_not_integer`），都記 `source_error`。回應格式本身的錯誤（status、meta、日期格式等）可能影響所有成員，仍結束這一輪並重試。<br>TAIFEX：該列沒有任何觀測值（`member:no_observation`，`source_error`）；plan 凍結的是同一交易日的 dated universe，而當日報表沒有該 contract／session（`member:missing_contract`，`empty_response`）。#307 之前以較舊 universe 凍結的 plan 缺 contract 時，可能是 universe 落後於轉倉，仍以一般 `source_error` 結束這一輪並重試 |

FinLab 抓表失敗時，`full_work_diagnostic.category` 記為 `finlab_sdk:<code>`，Fetcher 另輸出一行
`{"finlab_sdk_failure": {"dataset", "code"}}`。`code` 是有界的失敗類別，不含 FinLab 的原始訊息、token 或憑證內容：

| code | 意思 | 來源 |
| --- | --- | --- |
| `auth` | FinLab API 回覆 token 無效或帳號不存在；或 SDK 選到的身分不是設定的 API token | 子程序判斷：SDK 的認證例外，或 SDK 試圖開啟登入（子程序一律拒絕） |
| `quota` | FinLab API 回覆當日用量已達上限 | 子程序依 SDK 的配額例外型別判斷 |
| `sdk_contract` | 安裝的 SDK 缺少子程序依賴的名稱，不是審核過的版本 | 子程序在要求授權與任何網路請求之前檢查；不重試，work 直接結束 |
| `network` | 連線失敗、被重設、TLS 或 DNS 錯誤 | 子程序依例外型別判斷 |
| `timeout` | 單一請求逾時 | 同上 |
| `bad_response` | 回應不是合法的 JSON | 同上 |
| `memory` | 子程序記憶體不足（Python `MemoryError`） | 同上 |
| `os_error` | 其他作業系統錯誤，例如寫入 SDK cache 失敗 | 同上 |
| `sdk_error` | 以上都不是 | 同上 |
| `killed` | 子程序被 signal 終止，例如 OOM kill | 父程序依結束狀態判斷 |
| `crashed` | 子程序沒有回覆就結束 | 同上 |
| `process_timeout` | 子程序在整體時限內沒有回覆 | 父程序 |
| `dispatch_expired` | 授權在實際呼叫 SDK 前已過期，沒有呼叫 | 父程序 |
| `account_quota`、`acquisition` | 本機帳號 governor 的額度等待，或取得授權時失敗 | 父程序 |

FactSet 的子程序也會回報同一套類別（沒有 `auth`、`quota` 的 API 判斷），但目前只有 FinLab 把它寫進 diagnostic 與 log。

### FinLab 日 K 只取最終版

FinLab 的價格表每天台北 15:32、16:32、18:32、21:32 各更新一次，目錄標示 18:32 那次之後才是最終值
（`final_after`）。較早的版本還沒有當天的鉅額交易成交量；2026-11-16 到 11-19 每天有 14 到 48 檔的成交量在 18:32
後變大，價格不變（#597）。

- Fetcher 每次取表後讀 SDK 快取檔的時間，也就是 FinLab 的發布時間（SDK 下載或確認版本時把檔案時間設成發布時間）。
  早於目標交易日台北 18:32 的表當成「尚未發布」，走上面的發布等待，15 分鐘後再試，並輸出一行
  `{"finlab_target_not_final": {"dataset", "target_date", "published_at"}}`。五張表依序取，第一張就不是最終版時不取其餘四張。
- FinLab 每次排程更新都會刷新發布時間，內容沒變也一樣，所以沒有任何改值的日子也會在 18:32 那次之後通過。
  2026-11-16 到 11-20 的版本都在 18:48–18:54 出現；11-13 的 18:32 那次沒有發布，到 21:49 才出現。
- 到台北 22:15 仍不是最終版（18:32 與 21:32 兩次都沒有發布）時照樣取表，並輸出一行
  `{"finlab_final_publication_missing": {...}}`，欄位相同。日 K 的發布等待窗口是排程後 4.5 小時（到 22:30），
  所以 22:15 之後還有一輪。這種日子的成交量可能偏小，隔天的改值偵測會列出來。
- 補抓過去的日期與 historical 回補讀到的表，發布時間都晚於該日 18:32，不受影響。
- 等待期間日 K 的重試排到佇列尾端；四條盤中 feed 排在 19:15，晚於一般日子的最終版時間，日 K 才不會被盤中工作插隊。

### FinLab 事後改值的偵測

FinLab 日 K 每天取表時，表裡本來就有前幾個交易日的資料。Fetcher 拿前一個交易日現在的值，和當初交付的結果比對，
每個 dataset 每天輸出一行 `{"finlab_revision_check": {...}}`。這只是偵測：不改交付規則，不多呼叫 FinLab，比對
失敗也不影響當天的交付。

| 欄位 | 意思 |
| --- | --- |
| `trade_date`、`previous_date` | 當天，與表中當天之前最新的交易日 |
| `target_valued`、`previous_valued_now`、`target_to_previous` | 當天與前一交易日收盤價有值的檔數（今天的成員），以及兩者的比例；日後判斷是否需要完整度門檻用 |
| `compared`、`previous_valued_delivered` | 比對的成員數，以及當初交付時有資料的檔數 |
| `delivered_changed` | 當初交付的成員，開高低收或成交量現在不同（價格全空、以較早收盤價補值交付的成員只比成交量） |
| `no_trade_now_valued` | 當初記為無成交的成員現在有值 |
| `delivered_now_empty` | 當初交付的成員現在四個價格全空、成交量為空或 0 |
| `delivered_now_missing` | 當初交付的成員現在不在表裡 |

- 只輸出檔數與日期，不含任何價格或成交量。
- 當初交付的結果取自 Fetcher 本機的 checkpoint，只保留到工作結束後 7 天。前一交易日的計劃不在本機時（例如從沒在這個環境跑過），比對欄位是 `null`，只剩檔數與比例。
- 取表若是從 journal 重播（重試已取過的表），就沒有可比對的資料，那天不輸出這一行。
- 後四個欄位任一大於 0 時，另外輸出一行 `{"finlab_revision_detected": {...}}`，內容相同，給 log 告警使用。CloudWatch 的告警規則尚未設定。

FinLab daily 的抓表失敗是整個 cohort 共用的失敗，不記在任何成員身上（不回報 `source_error`、不扣成員的嘗試次數）：

| 類別 | 處理 |
| --- | --- |
| `auth`、設定錯誤（缺 token 等） | 重試沒有用：work 立刻以 `failed`／`DAILY_PROVIDER_REJECTED` 結束 |
| `quota` | 整個 plan 等 30 分鐘後重試，結算為 `retry`／`DAILY_PROVIDER_UNAVAILABLE` |
| 其他（`network`、`timeout`、`killed`、表格無法通過驗證等） | 整個 plan 等 5 分鐘後重試，同上 |

抓表失敗會一直重試到 plan 凍結的完成期限（`deadline_at`，排程後 12 小時），到期仍未成功時，work 以
`failed`／`DAILY_PROVIDER_OUTAGE_EXHAUSTED` 結束。所有 provider 的 outage 都用這個期限，不再有各自的固定時數。Work 結束後成員仍是 pending，
缺漏以 historical 回補。「目標日期尚未發布」不屬於這裡，仍走上面的發布等待。Historical 的抓表失敗本來就以日期為單位重試
（每個日期最多 5 次），沒有改變。TAIFEX 的報表 outage 同樣到完成期限為止，但只延後當下那個成員，不讓整個 plan 一起等待。

診斷只讀取既有 checkpoint 與 Source API；不建立空替代檔、不呼叫 SDK。以環境既有的 Source
credential、config 與 `FETCHER_ACCOUNT_STATE_PATH` 執行，明確指定 profile／state path：

```bash
findb-fetch-runtime --provider shioaji --execution-profile full_market \
  --config /path/to/full-market.json --state-path /path/to/state.sqlite3 \
  --diagnose-shioaji --work-id WORK_UUID > /tmp/shioaji-diagnostic.json
```

輸出包含全部凍結成員、原始與有效 mapping、本地 attempts／狀態、prepared digest／序列收據、
快取 SHA256、原生觀測／向量長度及 SDK／用量資訊。先對帳完成收據，再選擇仍未交付的成員。
需要新 catalogue 時，透過正常 maintenance work 取得並保存證據；診斷工具不直接查 SDK。

建立 proposal JSON，`checkpoint_sha256` 填入診斷輸出的頂層 `sha256`，`request_id` 為本次核准的
唯一穩定識別；相同 request ID 不可換內容。下列識別與 URL／SHA256 應以實際證據取代：

```json
{
  "checkpoint_sha256": "<diagnostic sha256>",
  "request_id": "shioaji-reviewed-recovery-001",
  "members": [{
    "member_key": "<frozen member>",
    "evidence": {"url": "https://evidence.example/observation", "sha256": "<64 lowercase hex>"},
    "reason": "已核對原始回應與商品證據，核准受控恢復",
    "observation_limit": 3,
    "mapping": {
      "symbol": "<canonical symbol>", "provider_symbol": "<reviewed provider symbol>",
      "exchange": "<original exchange>", "currency": "TWD",
      "reference": {"url": "https://evidence.example/catalogue", "sha256": "<64 lowercase hex>"}
    }
  }]
}
```

已有正確 mapping 的空回應恢復省略 `mapping`。Mapping 只接受原 gap 的相同 canonical symbol、
exchange、currency；canonical 與 provider symbol 均須符合 Fetcher 規則：1–50 個 ASCII 字元，
只允許英數、`.`、`-` 且至少有一個英數，不接受空白、exchange prefix、`_` 或 `:`。
Owner 須核對 reference 指向的 catalogue row 與 checksum，不能只把 symbol
照抄成成功。既有 prepared／accepted 成員不接受更正；無法證明 legacy receipt 的 canonical
membership 時保守拒絕核准，先完成原收據對帳。

在安全環境載入具 Owner 權限的 `FINDB_ADMIN_API_KEY`，不要將 key 放在 CLI 參數或輸出。
使用同一組 config／provider／profile／state／work 參數，先加上
`--preview-shioaji-repair --repair-proposal /path/to/proposal.json`，核對 Backend inventory、proposal
及回傳的 `sha256`；再改為 `--apply --repair-proposal /path/to/proposal.json --expected-sha256 HASH`。
Admin API 為 `POST /api/v1/admin/provider-work/{work_id}/shioaji-repairs/preview` 與
`POST /api/v1/admin/provider-work/{work_id}/shioaji-repairs`；直接呼叫時省略 CLI 專用的
`checkpoint_sha256`，apply body 加上 `expected_sha256`。
兩個 Admin 回應均使用 OpenAPI typed schema：preview 必須包含完整的 `inventory`、`proposal`
與 `sha256`，inventory 的摘要欄位均限制為 64 碼小寫十六進位；序列化不改動既有摘要綁定內容。
apply 必須回傳非空 UUID `repair_ids` 與布林 `replayed`，且提交授權前先驗證回應。

核准須等待該 work 的 active round settle；stale preview、錯誤 scope、已完成或 cancelled work
均拒絕。Backend 附加 `shioaji_member_repair`，只對原 terminal failed／partial work 新增 queued
attempt；不改舊 attempts、manifest、universe 或 plan。核准不啟動 control、不立即抓取。
新 attempt 依當前窗口、control 與 Full flag 設定 `drain_only`，claim／派送時仍重查；Pilot 維持
不受 Full flag 控制。Mapping overlay 與 persisted plan summary 在同一交易更新，重播核准亦會
刷新摘要；mapping 修好後不再顯示 `mapping_gap`，獨立的 source error 仍保留，完成仍須正式證據。
空回應分類與本輪 claim 退還先原子保存，再查 calendar／回報 Source；即使 calendar API 失聯，
重啟仍先補報原 outcome、不新增 native 呼叫或重複扣除舊 attempts。
原 coordinator 以 fenced `shioaji-repairs`／`shioaji-repair-ack` 安裝核准；先補報原 outcome、
恢復 prepared／receipts，剩餘成員才可消耗最多三次額外觀測。重啟、ACK-loss 或重複 apply／ACK
不補回已消耗 grant，也不重設原本的失敗次數。Mapping 更正只有取得 canonical evidence 後才算 data。
本機已完成（包括 `source_proven_no_trade`）與原 prepared／receipts 成員保留進度，不重新安裝
grant。只有確實保存 local repair 與 journal grant 的 repair ID 才回傳安裝 ACK；略過的修復
不記為已安裝。兩個 checkpoint 間中斷時，以相同 repair ID 續作，不重複發放額度。
已 ACK 且本地已安裝的 repair 在後續回合不再查 diagnostics／restore／ACK，只套用 mapping；
若 ACK 遺失（`acknowledged=false`），下一回合會冪等補送。

先部署 Backend migration `2f79b1d0c4e3`／API，再更新 Fetcher；SQLite 只新增 tables。
使用 repair 後須排空相關 work 才能退回不支援修復協定的 runtime；只要有 approval 證據，Alembic
downgrade 即拒絕刪除這兩張 audit tables。不得清 cache、刪 queue、reset attempts 或退款用量。
本機離線驗證不能證實 production 空回應根因；部署、原生觀測及受影響工作恢復仍須另行驗收。

#### 2026-10-30 staging 受控驗收（已停止，10/23 舊工作已休市結清）

本次驗收於台北 20:15:05 因 `1102` 達三次空回應上限轉 manual，依授權停止 Pilot；
Pilot revision=41、Full revision=9，desired／observed 均 stopped，owner 已排空。
指定兩筆 10/23 work 在驗收停止時尚未結清，驗收後已確認當天因颱風休市，
沒有分鐘資料符合市場狀態；不能將當日沒有 bars 當作資料恢復失敗。
安全退避／額度／證據保留與正常 catch-up 的交付路徑取得原生證據，
mapping 更正及 production 其他日期的根因／恢復仍待驗收。兩筆舊 work 已於台北 22:18
經 Owner 核准以新協定結清，完整保留原始證據與帳務，詳見下方部署／套用紀錄。

使用者補充休市資訊後，已核對證交所 2026-10-22 發布的
[10/23 颱風休市公告](https://investoredu.twse.com.tw/FileSystem/FileUpload/c91c7dff-afb0-42cc-9949-9f072bece31d.pdf)，
確認 2026-10-23 因薔琵颱風全日休市，不應為取得該日 bars 追加額度或重啟。
首次後續只讀查核遇到 staging AWS session 過期；更新既有 session 後，已確認日曆及
兩筆決策早已更正，剩餘範圍是保留的舊工作／計畫，詳見下方對帳預覽。
休市略過不等於成員已取得 canonical 或 no_data；
原觀測的 `cause=unknown`、attempts、用量、快取、收據與證據封包均保留原值。

使用者已核准 Pilot 在本日台北 18:00 後合法窗口驗收，Full 維持停止；兩筆
2026-10-23 舊工作各最多三次額外觀測，原 attempts=8 保留。Owner apply 與相同申請
重播均已成功，重播回傳原 repair ID，未重複發放額度：

| Dataset／成員 | Work ID | Repair ID |
| --- | --- | --- |
| `tw_equity_minute`／`1101` | `01a16834-7992-75a7-b7b4-6615742cd5a5` | `01a18bf8-b669-7050-8cfa-a4cb9ac6bc42` |
| `tw_etf_minute`／`00400A` | `01a16834-79c1-7802-9076-163f774a9e9e` | `01a18bf8-b6fc-767b-bdd7-6e50d670f82f` |

08:44 UTC 前的後驗確認兩 work queued，各附加第 9 輪且 `drain_only=true`；
本地原 attempts、空快取 checksum、凍結 manifest 與 native 帳務保留。
Prepared／receipt、repair ACK 均為零，兩 plan 合計
`expected=4 = data 0 + no_data 0 + missing 4 + blocked 0`；Pilot revision=39、
Full revision=9，desired／observed 均 stopped。SSM apply 證據
`f02d6e9a-6f4b-4053-9ea3-27e9272d2426`，後驗正式診斷
`312126ae-2854-4758-ae39-9baf938445fa`，原 attempts／manifest／額度核對
`a7b13f08-c8dc-4fc2-975e-25c406b382a7`。

09:59 UTC 的窗口前檢查遇到官方 AWS MCP 連續回傳
`Unknown tool: aws___run_script`；本輪沒有成功派送 SSM command，也沒有送出
start control 或 native provider 請求。無法重新確認即時 controls、部署與工作狀態，
不得把先前 stopped 快照當成即時證據。已取消本聊天的
`issue-378-staging-pilot` heartbeat，避免工具恢復後自動啟動。
當時尚待工具恢復及重新預檢；後續依下述新授權改用 CLI，沿用 repair IDs，未重新發額度。

使用者隨後明確授權改用 AWS CLI 繼續。19:37:52（Asia/Taipei）經 staging
身分／窗口／controls／版本／原值核對後，以 Owner API 將 Pilot revision 39→40、
desired state 改為 running；Full revision=9、desired／observed 仍 stopped。
此時 Backend 已由其他部署更新為 `7db03d5d9633587eb9c559dc87f36f4873a68bc0`，
Fetcher 維持 `501498aefa1d6e33163d3908b4122c42a459c0da`，兩者均包含 #383；
migration 仍為 `2f79b1d0c4e3`，本次未部署或修改環境設定。
CLI 預檢 SSM：`08631641-6ebb-4aaa-8e8a-e860dfc1eacc`（Backend）、
`6a1b7f9c-42be-4ad0-a0ee-594a39d19e48`（Fetcher）；啟動：
`1c7d456d-cdd6-4040-aaf7-3d96fd54fcf3`。

19:38 後原 coordinator 已安裝兩筆 local repair／journal grant，Backend ACK=2；
兩筆 `original_attempts=8`、本地 attempts=8 保留。舊 cache 另存 observation，
保留原 bytes／checksum；匯入 legacy 空回應起十五分鐘內不重新觀測，約 19:53 後
才 eligible，等待本身未耗一般失敗預算或額外恢復額度。
正常 catch-up 已建立 10/29、10/30 的工作，符合使用者核准範圍；
19:43 核對 10/29、10/30 八成員的收據均有 raw，normalization completed、
failed_records=0 且已有 canonical evidence；摘要等式相符。指定兩筆舊 work 仍等待。
安裝／退避證據：`f6fd49f1-a20a-4528-af20-c5f4ea87ff49`（Backend）、
`475e4ff3-b5f5-442e-8480-e42f4168db69`（Fetcher）。
完整驗收仍須確認指定舊 work 的 canonical／Serve 結果，並於完成或新阻擋時停止 Pilot。
19:44 的 `1102`、`00401A` 首次 native 觀測也為七向量皆空，原因維持 unknown，
本地 attempts=0 保留，依原三次上限／十五分鐘退避續作；不宣稱 no_data。
這兩次原生空回應與前兩筆 legacy 空快取分開保留，不退款 native 用量。
截至該輪觀測，官方 SDK usage connections 為 1–3，最新 sample bytes=6,788,976、
limit_bytes=524,288,000；本次 intents 已 finalized，未見持久化未知派送責任。
Source→canonical 查核 SSM：`519f64cd-7009-41e5-80d7-ed9c3ce6c7ab`；
用量查核：`d06b3250-7f8d-4b79-ba23-41299c131ac9`。
原完整 usage 查詢超過 SSM 輸出上限，不作完整證據；後者改採 governor 完整快照
checksum、intents、sample 數量及有界 usage 摘要，原 governor 紀錄保留。
本聊天的 `issue-378-staging-pilot-cli` 曾每兩分鐘續驗，已於觸發停止條件後刪除，
不會自動 restart 或發放額度。

停止時成員狀態如下；所有 10/23 原生回應皆為七向量皆空，當時診斷原因記為 unknown，
Fetcher 未據既有休市決策結清舊工作，系統當時不計 no_data。後續休市對帳如上，
不回寫原觀測或將以下狀態直接改記成功：

| 成員 | 原本地 attempts／停止後 | 本次 native 觀測 | 停止後狀態／剩餘額度 |
| --- | --- | --- | --- |
| `1101` | 8／8 | 2 次額外觀測皆空 | pending；已核准的 3 次剩 1 次 |
| `1102` | 0／0 | 3 次原始觀測皆空 | manual；原三次上限耗盡，沒有額外授權 |
| `00400A` | 8／8 | 2 次額外觀測皆空 | pending；已核准的 3 次剩 1 次 |
| `00401A` | 0／0 | 2 次原始觀測皆空 | pending；原三次上限剩 1 次 |

觀測紀錄間隔最短 920.193 秒；原 cache bytes／checksum、兩 manifest digest、
原八輪 Backend attempts 與空 prepared 均逐項核對未變。兩 repair 的 approval／local
installation／ACK 各一份，generation=71／72。Backend queue round 已附加至 35／34，
這是輪替紀錄，不等於本地失敗 attempts 或 native 用量。兩 work 仍 queued、收據為零，
合計 `expected=4 = data 0 + no_data 0 + missing 0 + blocked 4`，缺漏及告警保留。

本次新增 native Kbars 17 次（8 次有效、9 次空），login 15 次；已發用量保留。
所有新增 intents 已 finalized，沒有遺留 armed／unknown debt。Official usage 共 109 筆
samples，connections=1–3，最後 bytes=8,244,828／limit=524,288,000；共享 pacing／cooldown
歷史保留。正常 catch-up 的四 work 均 completed，八個 run 的 raw、normalization、
canonical evidence 皆完整，failed_records=0。使用既有 Serve key 實際查詢完整分頁，
八組皆 HTTP 200 並通過 `MinuteListResponse`，2,031 根 bars 與 canonical 行數／身分／日期
相符，逐組保存結果 checksum；這是 10/29、10/30 的證據，不是 10/23 的完成證明。

停止 SSM：`cdd49b58-798d-421f-8c6e-00eb94038e0f`；停止後 Backend／owner／原值：
`0c766297-a186-4d5b-8f37-23601f17b00a`；Fetcher 正式診斷／checksum：
`245098b4-75f2-47ed-81eb-b2667449e0c9`；Source→canonical：
`c9a7f3e6-a749-4b61-a214-9c6dd85a2d4c`；native 用量：
`fae036c6-3d38-4aa8-b1c0-baf8496528c0`；Serve：
`e0f85d5e-3a1d-4446-abe4-5eaa8c8ff436`。累計正式 CLI 原文曾超過 SSM 輸出上限，
停止後改用同一正式診斷驗證後的有界摘要，完整原文不作未截斷證據宣稱。
本機證據封包 checksum 為
`050a9358da65fc5878d2f998d0de5465fe9cf8daf5edba95d32e42f2eadd3d1e`，
保存在 `/private/tmp/findb-378-staging-20261030/`；S3 備份遭自動核准審查拒絕，未上傳。
使用者隨後選擇僅保留本機封包與 SSM command history，不上傳此封包。

本次只變更營運文件，`pnpm check` 與 `git diff --check` 通過，未重跑程式／migration suites；
沒有變更程式或 migration。未執行人工 fault injection、停止後重啟、ACK-loss／Source outage
重現或 mapping gap 正向恢復（staging 沒有原 gap）。10/23 日曆／凍結決策已完成對帳，
舊工作已由下述保留證據的結清機制處理；production 其他日期的空回應根因、mapping 恢復及相關負向 native
驗收仍待後續處理，不將本次休市解釋外推至其他日期，不自行追加額度或重啟。

#### 2026-10-30 staging 10/23 休市對帳預覽（只讀，未套用）

台北 20:47–20:50 只讀核對帳號 `439622209937`／staging：TW published revision=6
已將 10/23 設為颱風休市，Shioaji `skip_non_trading_days=true`。兩筆 Pilot decision
早於台北 10/24 00:09:02 透過 `manual-pilot-typhoon-calendar-20261024-v1` 改為
`skipped_calendar`、`plan_id=NULL`；原本為 revision=5 的開市 planned 決策。
稽核 `01a16985-dd09-7fb5-a48c-6b70e746dcce`／`01a16985-dd0d-7aca-887f-069d6701221b`
的 before／after SHA 均相符，現 decision 與 audit.after 全文相符，對應 alerts 已 resolved。
因此日曆、決策、告警的預計更正均為零，不能再次重做休市更正。

當時剩餘範圍是上表兩個 Shioaji work／兩 plan／兩 parts／四 members：Backend work queued，
原 scope 保留 revision=5 開市快照及 canonical manifest，本地三 pending／一 manual。
Backend prepared、receipts、linked ingestion runs，本地 prepared、sequence receipts、有效 lease
及 journal deliveries 均為零；原 attempts、兩 repair／ACK、cache／observation checksum 保留。
合計仍為 `expected=4 = data 0 + no_data 0 + missing 0 + blocked 4`。
Pilot revision=41、Full revision=9，desired／observed stopped；owner generation=131、attempt=NULL，
無 armed／unknown intent。沒有此日 Full plan／decision 候選；FinLab 另兩個保留 plan
不納入本次 Shioaji 範圍，HK／US 的正常開市也不受 TW 休市影響。

預覽 `apply_ready=false`：兩 plan 各被一筆 `shioaji_member_repair` 以 `ON DELETE RESTRICT`
引用，不能使用下節舊的刪除式封存流程，更不能刪修復授權繞過。
`_plan_for_date()` 仍先沿用原 plan，`current_calendar_status()` 對已 skipped 的決策回傳
`repair_required=false`；這些檢查不能替舊 canonical work 結清。不得直接將其改為 completed、
改寫 manifest 為 calendar_skip，或偽造 no_data。後續需先確認／補齊保留原 manifest 與帳務的
休市工作結清協定，再產生可套用預覽；這是只讀對帳當時的限制，後續實作如下。

完整預覽保存在 `/private/tmp/findb-378-staging-20261030/calendar-reconciliation-preview.md`，
JSON SHA-256 為 `0deb444465eae99d60705b2b29c595b911be8d8e5b5b8a7bf56e9dddb50299f4`。
完整 SSM 證據：inventory `7548adc8-ef69-4fe4-9053-93d887c3e09f`、scope／delivery／FK
`7c1c083d-1df6-4844-a7b8-ff89f4283f7e`、既有 audit／deployments
`e462b8df-fc70-45ca-a2d7-4a9874d3a4d0`、checkpoint／journal
`9b1ffe10-e965-4afc-840b-d6c3b9f7be60`，均 Success／ResponseCode=0。
初版輸出遭 SSM 24 KB 截斷，不作完整證據；後者以有界／無損壓縮結果取代並驗證 checksum。
本次只讀，不修改 DB／checkpoint／帳務、不啟動 controls、不呼叫 SDK；證據僅留本地與 SSM。
文件更新後 `pnpm check` 與 `git diff --check` 通過，未重跑程式／migration suites。

### 已更正休市決策的舊工作結清

> Shioaji Fetcher runtime 已移除（見 [rollback 下限](deployment.md#shioaji-退役的-rollback-下限)）。本節 CLI 只存在於 `fetcher-v0.1.21` 以前，Backend 保留歷史 inventory／preview；controls retired 後 repair apply 拒絕。內容僅供稽核既有紀錄，不可重新啟動舊 CLI。Pilot 退役 migration `a7c3e1f9d2b4` 已刪除綁定 Pilot 的 closure／repair rows，下文的 Pilot 描述只說明退役前行為。

Shioaji Pilot／Full daily 的舊 canonical work 可經 Owner 明確核准改為 `calendar_closed`。
此狀態只表示休市工作已結清，不代表取得 canonical 或 no_data；原 plan／parts／members、
expected／blocked、manifest／scope、所有 attempts、修復授權／ACK、checkpoint、快取及帳務
均保留原值。活動 FIFO projection 排除已結清 work；相同 stable key enqueue 仍回傳原 work，
不能建立新 attempt 或再抓取。舊計畫查詢仍保留原不完整 summary，不能清空成員偽造成功。

Owner API 為 `POST /api/v1/admin/provider-work/{work_id}/calendar-closure/preview` 與
`POST /api/v1/admin/provider-work/{work_id}/calendar-closure`，使用 typed request／response。
Source 只可經自身 provider／dataset scope 讀取
`GET /api/v1/source/provider-work/{work_id}/calendar-closure`，Fetcher 不連 Backend DB。
每筆預覽綁定 environment、profile、plan、所有成員、原 manifest／attempts／修復與 ACK、
目前日曆及已存在的決策更正 audit。只有目前 published calendar 休市、略過政策開啟、
decision 已 skipped_calendar 且原 plan／audit 前後摘要吻合、對應告警 resolved，才可結清。
本流程不負責首次更正 frozen decision，也不覆寫原日曆證據。

Pilot／Full controls 必須 desired／observed stopped，provider owner 必須閒置；已有 Backend
prepared／receipt／任何對應 run，或本地 prepared／receipt／有效 lease／journal delivery／job、
未知 probe／SDK intent，一律拒絕。既有 prepared work 應走原交付對帳／排空流程。
已 skipped_calendar 的舊 canonical work 即使尚未結清，也不能取得 acquisition 授權；
獨立 calendar_skip manifest 及原 prepared drain 不受此保護誤擋。

正式工具沿用既有 Owner 操作模式，需合法 Source 與 `FINDB_ADMIN_API_KEY`；key 不輸出。
Owner 提供的本機證明是受信任操作端的 attestation，不是新的 Source 寫入權限。
工具鎖住既有共用 host inode，唯讀檢查 checkpoint／journal／governor，直到 Owner API 回應；
不初始化 SQLite、不改本地進度、不載入 SDK。初始 proposal JSON 僅含 request_id 與 reason：

```bash
findb-fetch-runtime --provider shioaji --execution-profile pilot \
  --config /app/configs/bootstrap.staging.v1.json \
  --state-path /var/lib/findb-pilot/staging/state.sqlite3 \
  --calendar-closure --work-id <work-id> --closure-proposal request.json > preview.json
```

核對 `apply_ready`、blockers、inventory、完整 request 與 sha256 後，才能另行核准套用：

```bash
findb-fetch-runtime --provider shioaji --execution-profile pilot \
  --config /app/configs/bootstrap.staging.v1.json \
  --state-path /var/lib/findb-pilot/staging/state.sqlite3 \
  --calendar-closure --work-id <work-id> --closure-proposal preview.json \
  --apply --expected-sha256 <preview-sha256>
```

本機證明有效十五分鐘，首次 apply 以 provider→environment→dataset→calendar→control 鎖序
重查停止狀態、owner generation 及所有 fingerprints。成功時同一 transaction 附加
`provider_work_calendar_closure` 稽核，再只改原 work 的 state／completed_at；既有 attempt
列不改寫、不退款、不重發 grant。若回應遺失，以同一 request／摘要重播，回傳原 closure ID，
不因超過證明效期或其後新增 governor 歷史而重複結清。相同 work 的不同請求 bytes 拒絕。

先部署 Backend migration `3a80c2e1d5f4`／API，再更新 Fetcher 工具；不得用未部署版本的
預覽宣稱線上已可套用。離線匯出預覽可用同一 evaluator 檢查 staging 快照，但須明列 schema
部署前置條件；部署後仍須重新取得正式預覽。只要存在 closure evidence，migration downgrade
即拒絕，以免舊版遺失結清語意。部署與預覽不啟動 Pilot／Full；實際結清仍須 Owner 明確核准。

Staging mutable-data reset 的 dry-run 可列出 closure 數量；只要已有 closure evidence，
reset apply 一律拒絕，保留其原 work／plan／attempts／修復 graph。空 closure table 才可隨
原本明確核准的 staging reset 一併清空；本次部署與預覽不執行任何 reset。

#### 2026-10-30 staging 結清流程部署、正式預覽與套用

實作分支 `fix/shioaji-calendar-work-closure`。Developer 驗證：362 項相關 Backend／Source→
canonical→Serve 測試、29 項結清／migration／schema drift 測試、完整 Fetcher suite 1,876 項
及全專案 `pnpm check`／`git diff --check` 通過。Fetcher 初次兩項 fork/socket 測試受 sandbox
限制，提升本機測試權限後重跑完整 suite 通過；沒有真實 provider 請求。
獨立 reviewer 沿用 Developer 模型／reasoning，implementation verdict=CLEAR，無實質 findings
或待修復項目；另跑兩端 mypy、113 項 Backend 及 8 項 CLI 測試均通過。Reviewer 未重跑完整
suite／migration，沿用前述 Developer 證據；本次沒有 production 驗收。
CI 初次發現 reset inventory 與 Source 路由清單遺漏，補齊後完整 Backend 非 migration suite
2,297 passed／3 skipped、34 項相關測試與 `pnpm check` 通過。獨立聚焦 re-review=CLEAR，
另驗證 12 項 reset／Source 測試、ruff、mypy 及 diff check；無待處理 findings。

[PR #385](https://github.com/FPI-TW/findb/pull/385) 必要 CI 全通過後合併，staging release 為
`0a7bb35c7a808c61f5b497ccade83007dc7e2b97`。
[FinDB CD 41066064486](https://github.com/FPI-TW/findb/actions/runs/41066064486) 先成功，
再由 [Fetcher CD 41066064471](https://github.com/FPI-TW/findb/actions/runs/41066064471) 完成同 SHA
部署。兩台主機 current manifest／containers 已核對；Backend migration=`3a80c2e1d5f4`，
Backend digest=`sha256:8f2e3fce76ad61af3b27b4c7f59563b1d9ce16f9fc2e58d9de3badba561bcfbd`，
Shioaji digest=`sha256:d8390d8439367886a72c8f0946f2978d8feb25a27a4c50cb57dda1f94cb77152`。
只部署 staging，未操作 production。

首次 14:06 UTC 正式預覽透過新 Fetcher 的 Source-authenticated GET 取得 inventory，使用已部署 CLI 的
`checkpoint_facts()`，持續鎖住原 inode `staging:66305:512068` 至 Backend Owner preview API
回應並確認後才釋放。Owner key 僅在 Backend 容器記憶體使用，未傳到 Fetcher。兩筆均為
實際 HTTP typed response，`schema_ready=true`、`apply_ready=true`、`blockers=[]`，
proposed state=`calendar_closed`；該次預覽未套用，後續依 Owner 新授權刷新後套用如下。

| Dataset | Work | 正式預覽 SHA-256 | 證明效期（UTC） |
| --- | --- | --- | --- |
| tw_equity_minute | 01a16834-7992-75a7-b7b4-6615742cd5a5 | 11bfc2398068b500b10d4e9edcd39762e2f5309469bf8956bfb365293a0529fb | 2026-10-30 14:06:12.855199–14:21:12.855199 |
| tw_etf_minute | 01a16834-79c1-7802-9076-163f774a9e9e | fd8c1b3b1a314a601ce48a0436b6c0ff6f6397ecbaab544a48e370e0a6d9a744 | 2026-10-30 14:06:13.432316–14:21:13.432316 |

完整 request／inventory 與摘要位於
`/private/tmp/findb-378-staging-20261030/formal-calendar-closure-preview.md` 及同目錄的
`<work-id>.calendar-closure.formal-preview.json`；過期或任一 fingerprint 改變後須重取預覽，
不能沿用先前離線預覽。部署前封包與離線證據均另存保留。

部署後 Backend／Fetcher 核對 SSM 為 `db124271-5f31-4b98-a07b-2760db92d910`、
`1f30761e-9905-41b5-8e4d-238901143d13`；正式 Owner 預覽為
`fb8bb5c2-22f1-4dcb-86e2-592adb41c92d`；預覽後 Backend／native 帳務確認為
`cdad8749-fe91-48cf-87be-7b180bae5a8a`、`12b3d337-e327-44fd-9284-62a7a531949a`，
均 Success／ResponseCode=0。完整 Backend snapshot 各欄與本機 facts 均與部署前相同；
當時兩 work 仍 queued、Backend attempts 35／34 列，原本機 attempts=8、manifest／cache bytes／checksum、
兩修復授權及兩 ACK、quota／cooldown 均保留。總計仍 expected=4、data=0、no_data=0、missing=0、blocked=4。
原 native accounting fingerprint
`9e87c508dca36c6eff6ff0c6b881c26ced1247d3df588322df37d49a7442881c` 未變，usage 仍 109 筆，
未新增 SDK login／Kbars dispatch、未退款或再發 grant。

該次預覽後 Pilot revision=41／Full revision=9 均 desired／observed stopped，owner generation=131、
attempt=NULL，closure table=0。首次預覽及所有部署前證據另存保留，不覆寫。

Owner 後續明確核准套用 staging 10/23 兩筆 work。14:17 UTC 重新產生正式預覽，核對 inventory
及本機 facts 與前次完全相同，兩筆均 apply_ready=true／blockers=[]；整段操作持續持有原
`staging:66305:512068` host lock，套用前再經 Source GET 重查 fingerprints 未變。
僅以 Backend 既有 Owner key 呼叫 typed apply API，沒有直接寫 DB／checkpoint，也未啟動 controls。

| Dataset | 套用預覽 SHA-256 | Closure ID | 結清時間（UTC） |
| --- | --- | --- | --- |
| tw_equity_minute | dd0cf73f17958c4e77c42cdfb67b867d81346a059fabde9a1c8b0db24e3e7e66 | 01a18d2c-e1cf-7f8a-a9bb-4ffa45764274 | 2026-10-30 14:18:11.279584 |
| tw_etf_minute | 9cceec3d46eb77c8e6c9b8b325ecd65ab7c0e8920e67c42b83fde1f13f2bbd1e | 01a18d2c-e225-77f6-80d9-f388a79dbb7e | 2026-10-30 14:18:11.365813 |

兩筆 work 均已 `calendar_closed`，同一 request bytes／expected SHA 重播回傳同一 closure ID／
closed_at 且 replayed=true；closure table 恰為 2 列。Source 套用後讀取與本機檢查亦在同把
host lock 下完成，證明每筆只改 work.state／completed_at 並附加 closure；新稽核中的原 inventory
與套用前全文相同。原 plan／parts／members／summary、所有 attempts、修復授權／ACK、
prepared／receipts／runs、日曆／決策／audit／alerts、checkpoint／cache bytes／checksum／
grant／probe／budget、native quota／usage／cooldown 均未變。兩 plan 合計仍
`expected=4 = data 0 + no_data 0 + missing 0 + blocked 4`，工作結清保留這份原統計。
正式 Shioaji 診斷的整體 SHA 因 Backend work.state 改變而更新，原成員 attempts／status 與
cache checksum 不變；不能將此報告摘要誤認為原快取 checksum。

重新預覽 SSM=`7a99de37-cf52-4a1d-8dcb-2a3a51173ce4`；apply／相同 bytes 重播
SSM=`0f4a2a19-904c-4828-ad03-c3a2371e6c7b`；最終 Backend／Fetcher／native 帳務核對
SSM 分別為 `fa890d1d-1442-48a8-a907-1b3cf02384d0`、`ff06d775-9944-4731-90b2-e10982ca8c26`、
`dae6dad9-2ead-47d0-bffa-f767abedf01d`，均 Success／ResponseCode=0。
首次 SendCommand 的註解過長，於派送前被拒絕；縮短註解後送出同一批 request／摘要，
沒有未知派送或另發授權。完整刷新預覽、請求、typed 收據、Source 套用後 inventory、
保留性比對與摘要位於 `/private/tmp/findb-378-staging-20261030/calendar-closure-applied.md`
及同目錄的 `closure-approved-*` 檔案；本次證據僅保留本地與 SSM。

最終 Pilot revision=41／Full revision=9 均 desired／observed stopped，owner generation=131、
attempt=NULL，原 native accounting fingerprint 與 109 筆 usage 完全相同；沒有新增 SDK login／
Kbars dispatch、退款或重發 grant。歷史空回應 cause=unknown 保持原值。這兩筆已結清，
不因預覽過期再建立新申請；ACK-loss 只重播原核准 bytes。未操作 production，其餘日期根因、
mapping 恢復及 production native／負向恢復仍待驗收。

### 凍結計畫的人工休市更正

現有版本沒有首次更正凍結決策的專用休市處置或告警 acknowledgement API；上述工作結清 API
只接受已存在且可驗證的決策更正 audit。只將 `missing_delivery_alert`
設為 resolved，monitor 仍會依既有 planned 決策重開。永久結清需要更正每日決策，屬於
凍結規則的人工例外，必須先取得對「修改原決策、原值完整另存稽核」的明確授權，不能因
日曆／政策更新便自動套用，也不能以偽造 no_data 或將 blocked 改成成功替代。

操作限定明確的 environment、provider、dataset、profile、日期、plan ID 與 alert ID，先 dry-run：
核對 AWS／DB 身分、原 plan／decision fingerprints、原與目前日曆皆休市、目前休市略過已啟用，
並確認沒有 accepted run、prepared body、receipt、有效 lease 或在途抓取。若已有交付證據，
停止套用此流程，另行判定。保留 checkpoint 的 manual／attempts，不刪除或重置 SQLite。

同一 DB transaction 為每筆新增 `admin_audit_event`，保存操作身分、理由、operation ID、
完整原 decision／plan／parts／members／alert／first date／control、目前日曆與 before／after
SHA-256，再將 decision 改為 `skipped_calendar`、`plan_id=NULL`，並結清對應 alert。
原 `created_at` 保留；新 snapshot 使用 `snapshot_origin=manual_calendar_repair`，標明
`corrected_at`、`original_plan_id`、`audit_event_id` 與 operation ID。原 planned 決策全文由
該 audit 保存，原 plan／part／member、expected、blocked、first dates 與 control revision 不變。
任何檢查失敗應整批 rollback；重跑同一 operation ID 只核對已完成結果，不能重複建立 audit。

Staging 的 `manual-pilot-weekend-calendar-20261019-v1` 於 2026-10-19 11:24（Asia/Taipei）
依明確授權處理六條非 TAIFEX feeds 的 10/17、10/18，共 12 筆 decision／alert，新增 12 筆
`action=calendar_decision_repair`、`actor_type=aws_operator` 稽核。每個原 plan 在首階段維持
`expected=2、data=0、no_data=0、missing=0、blocked=2`；這些原值亦隨下述封存保留，
不改記成功或 no_data。此作業未部署 application／schema，也未更換 image 或設定。
套用 SSM command：`1280065f-4683-43e7-be8d-c8c8614d2975`；完整原值與前後差異以 DB audit 為準。
11:24:28、11:25:28 自然 monitor scan 後均未重開；11:27 後驗確認 25 plans、25 parts、
52 member identities、14 first dates、12 controls 及日曆／flags fingerprints 不變，27 筆
decisions 中僅指定的 12 筆改變，其餘告警狀態不變。12 個原 plan 的完整成員紀錄亦逐筆與
audit before 相符，Backend／Fetcher images 和 SQLite checkpoints 不變。後驗 SSM commands：
`0d81ba54-4c29-4e13-8db1-d7191a9aae86`、`071c705c-0270-4a99-a132-7d79873874b6`。

後驗應跨至少兩次自然 monitor scan，核對告警不重開、audit hashes 與前後資料一致，並確認
其他日期／profiles 的告警仍正常。daily 回合在新抓取前會查詢 calendar status（見上節），但不會自動更正 decision；既有 checkpoint 的
manual 狀態仍保留，不應重置後直接重跑舊 plan。如需撤回，以新 operation ID 和新 audit
在單一交易恢復原 decision／alert，先核對目前狀態；不得刪除原 repair audit。

#### 同步交付計畫的活動資料

交付計畫沒有可手動改為 skipped 的 status 欄位；API 直接依成員計算完整性。保留舊計畫時，
畫面會同時出現 incomplete 舊計畫及 skipped_calendar 決策。若不部署新版本而要消除這項
重複，必須另取得「完整封存後刪除活動計畫／parts／members／checkpoint」的明確授權。
不得清空成員後把空計畫算成功，或只刪 Backend 而留下 Fetcher 的舊 plan 參照。

先核對原決策更正 audit、無 ingestion run／其他外鍵交付引用，以及 checkpoint 無 prepared
body／receipt／有效 lease。完整保存每個 plan、part、member、目前 decision／alert 及 SQLite
plan／work 原文，包括 BLOB bytes、前後 fingerprints、原修復 audit 連結與操作身分。
在 `calendar_plan_archive_prepare` audit 已 commit 後，才可移除指定活動資料。
PostgreSQL 依 members → parts → plans 在同一交易刪除；SQLite 依 work → plan 在同一交易
刪除。兩者不能共用 transaction，須以持久化階段紀錄協調；任何一側失敗，先核對實際 commit
狀態，再恢復 checkpoint 或完成已提交的一側。兩側一致後才能恢復 runtime，不刪除 prepare audit。

Staging 已依另次明確授權完成 `manual-pilot-weekend-plan-archive-20261019-v1`：12 plans、
12 parts、24 members 及對應的 12 SQLite plans／24 work rows 完整封存至 Admin audit 後移出
活動資料。活動計畫由 25 降為 13，交付頁保留 14 筆休市略過（含原有 2 筆 TAIFEX）；
原 12 筆決策更正 audit、27 筆 daily decisions、已結清 alerts、first dates、controls、日曆，
以及其餘 Backend／SQLite 資料的保護 fingerprints 均相符。原 expected／blocked 值保存在
封存 audit，沒有造出 complete 或 no_data。

本次短暫 pause／unpause 共用 Pilot checkpoint 的四個服務，未部署 application／schema、
未換 image 或設定。恢復時 Shioaji／Twelve Data 各以 exit code 2 自動重啟一次，後驗均維持
running，其他兩個沒有重啟；containers IDs 與 digests 均未變。僅 pause 仍會保留程序中的
舊 plan 參照，後續此類活動計畫移除須安排 runtime 靜止與恢復驗收，不能以 DB／SQLite
交易已完成便假設舊程序可無中斷繼續。Checkpoint bytes 還原及中斷交易 rollback 已先在本機演練。

Prepare SSM：`477ef221-ad3e-4086-9389-76f26bfe794b`；Backend 刪除：
`abe7d74f-67ea-4022-b7b4-a671ee781e3c`；完成 audit：
`01a1524d-5328-7cc1-9240-8f0ac2918799`。如需還原，須在 runtime 靜止時從 prepare audit
先恢復 Backend plans → parts → members，再恢復 SQLite plans → work，核對原文與 hashes，
並新增復原 audit；已更正的 skipped_calendar 決策不隨計畫還原自動改回 planned。

## 平台限制與用量

平台配額只採用以下已確認限制；未公布的維度不新增數值配額、預算或啟動門檻。

| Provider | 適用限制 | 用量核對與超限處理 |
| --- | --- | --- |
| Twelve Data | 1597 API credits／分鐘 | Durable governor 依 1597／分鐘平均 pacing，分鐘與日窗口以 credits（一次 batch 的 symbol 數）計；HTTP 429 與 HTTP 200 payload code 429 均寫入 60 秒帳號 cooldown 後等待／重試。實際帳號用量以 readiness probe 的 `/api_usage` 人工核對；每日路徑尚未讀取 response credit headers |
| FinLab | 5000 MB／日 | 以 SDK API 回傳的 `quota`／`limit_size` 核對帳號累計用量，達限後等待平台重置。API 隨每次取表回報的當日用量（`quota`／`limit_size`）會記錄下來：Fetcher log 的 `{"finlab_quota": {"dataset", "used_mb", "limit_mb"}}`（API 有回報這次取表計費的位元組數時另帶 `billed_bytes`，是該次更新與整表下載的合計），以及帳號 governor SQLite 的 `acquisition_usage`（`bytes`／`limit_bytes`／`remaining_bytes`，與該次取表的 `acquisition_intent` 同一個 `operation_id`）。SDK 直接讀本機 cache、沒有向 API 發出請求時不會有紀錄。盤中四條 feed 每個代號只下載分區，不取表，API 不回報用量；因此每一輪（第一輪與每次無資料重試）結算前另讀一次帳號用量，以同樣的 `finlab_quota` 與 `acquisition_usage` 記錄，`dataset` 是該 feed 的 dataset key，前後兩筆相減即是該輪用量。這次讀取是一個本機版本日期為現在的 metadata 請求（`price:收盤價.recent`），FinLab 不給下載網址、不扣配額（staging 2026-11-18 連續五次讀到相同用量）；讀取失敗只輸出 `{"finlab_quota_unavailable": {"dataset", "code"}}`，不影響這一輪（#601） |
| Shioaji | 500 MB／日、行情查詢 50 次／10 秒 | 以 `api.usage()` 的 bytes／limit_bytes／remaining_bytes 核對；API 回報的 524288000 bytes 為 500 MiB；依 10 秒視窗控制查詢速率 |
| TAIFEX | OpenAPI 不需帳號，未公布明確 requests／bytes 數值上限 | 以 HTTP 429 作為觸及平台上限的訊號，依 `Retry-After` 或既有 backoff 等待／重試，不自行設定 provider 配額數值 |

Twelve Data daily 以同一 frozen part 為單位共用 provider 請求：part 中第一個需要抓取的成員，把同 part
後續仍需抓取的成員（已 mapping、沒有 prepared body、eligible：無 lease、退避或未到的 no-data 重試輪）
一起以逗號 `symbol` 發一次 `/time_series`，最多 50 個、每 symbol 1 credit；mapping gap 與已完成成員不加入、
不計費。回應必須正好以要求的 symbol 為 key，缺少或多出任何 key 即整批失敗；一次只有一個 symbol 時沿用
單檔請求。整批回應只存一個 R2 raw object，作為它所服務每個成員的 lineage；每個成員再由自己的項目建出
request，照常逐檔 prepare／ingest／receipt。單一 symbol 的錯誤（例如 `no_data`、404）只讓該成員走原本的
member 失敗、no-data 重試輪與 breaker；整批錯誤（例如 429）由觸發的成員承擔。Journal 只有在每個 symbol
都已有目標日資料時才允許重播該 batch，重試輪因此會重新詢問；未取用的項目最多保留 300 秒。
實測（2026-11-11）：回應以 symbol 為 key，錯誤項為 `{code, message, meta, status:"error"}`，其 `meta.symbol`
為整串清單；無效代號也計費（`api-credits-request` 等於 symbol 數）。

Twelve Data credits 計算見[官方說明](https://support.twelvedata.com/en/articles/5615854-credits)；
Shioaji 限制見[官方使用限制](https://sinotrade.github.io/zh/tutor/limit/)；TAIFEX 平台與 endpoint
定義見[官方 OpenAPI](https://openapi.taifex.com.tw/)及[Swagger schema](https://openapi.taifex.com.tw/swagger.json)。
TAIFEX 429 的等待策略採用現行 runtime 的 rate-limit handling。

每個 provider 只安裝 `findb-full-market-*` 與 `findb-historical-*` 兩個 consumers，使用獨立 container／checkpoint names。
Installed consumers 的用量與 cooldown 由 durable governor 保存，process restart 不清除帳號已用量。
FinDB internal Source API 的 2400 RPM 為內部服務設定，與 provider 的平台配額分開。
Payload／R2 object／解壓縮／IPC bounds 屬於傳輸與資料處理保護，不列為額外 provider 配額證據。

Raw R2 以內容雜湊決定 immutable object key，使用 `If-None-Match: *` 建立，避免重試覆寫
已鎖定物件。409／412 不直接視為成功；必須在同一 bucket/key 讀回完整且有大小上限的 bytes，
核對長度、內容、content type 與 provider/dataset/symbol/SHA-256 metadata 後才可重用。
讀取失敗或內容不一致仍阻擋交付，查無物件的競態可重試；不移除 bucket lock 或刪除物件。
Twelve Data 的 catalogue 以 Nasdaq MIC 規則比對：`XNCM`、`XNGS`、`XNMS` 對應官方 `XNAS`。
目錄的交易所常落後於實際轉板，因此不要求一致：同交易所優先；沒有時，其他交易所只有一筆同幣別
商品且公司名稱相符才 mapped。同代號在其他交易所可能是無關的公司，名稱不符維持 mapping gap。
成員保留官方交易所；日資料回應僅核對幣別與 frozen universe 相符，原始回應不改寫。
美股 universe 只含普通股、REIT、ADR，以及以普通單位交易的上市合夥與礦權信託：目錄 `type` 為
`Common Stock`、`REIT`、`American Depositary Receipt`、`Depositary Receipt`、`Limited Partnership`、`Trust`
才可 mapped（2026-11-22 起，#629；官方名單本來就把 EPD、SPH、SBR 等列為普通股，當時有 12 檔因型別而成為
mapping gap，抽查 Twelve Data 日線的型別、交易所與價格皆正確），官方名稱未標明股類的成員由此
`type` 決定分類。範圍外不列入 expected：名稱為 right、unit、warrant、特別股或債券類的官方成員，
目錄 `type` 為 `Preferred Stock`、`Warrant`、`Right`、`Unit`、`Exchange-Traded Note` 的成員，
以及名稱標示 fund、closed-end、trust 或 beneficial interest 且不在 Twelve Data 美股目錄的封閉式
基金與信託。其餘無法對應的成員（目錄無此商品、公司名稱不符）維持 mapping gap；同代號在 OTC 或目錄
留著的舊公司不會用代號直接請求，避免把另一家公司的資料寫成官方成員的資料。
Twelve Data universe refresh 只抓取 Source 當次授權的美國 country catalogue，不下載全球
`/stocks` 目錄，也不再抓取香港 catalogue 或 HKEX 名單（港股屬 FactSet）。每個市場各自經過
dataset-bound fresh authorization 與 account governor；回應大小上限維持不變，Raw 與 evidence URL
按市場分開。API key 只以 `Authorization: apikey …` header 傳送，不出現在 request URL、evidence URL
或例外訊息。

### Twelve Data 日資料與失敗分類

日資料以 `/time_series`、`interval=1day`、`adjust=none` 抓取當日；canonical 為實際成交價，
拆股／股利不在行情中預先調整（見 [ingress contract](../architecture/ingress_contracts.md#market_eodv1)）。
Twelve Data 對查無資料、代號錯誤等情況回傳 HTTP 200 加 payload `status=error`，或直接回傳 HTTP
4xx／5xx。Fetcher 依 HTTP status、payload `code` 與固定訊息族群分類，只保存分類與數字代碼，
不保存 provider 原文：

| 分類 | 判定 | 處理 |
| --- | --- | --- |
| `rate_limited` | HTTP 429 或 payload code 429 | 已派送；寫入帳號 cooldown，回報 `rate_limited` |
| `transport`／`server_error` | 無 HTTP 回應、5xx | 回報 `source_error`，依原 failure budget 重試 |
| `auth_failed`／`plan_restricted` | 401／403 | 回報 `source_error`；需人工核對金鑰與方案權限 |
| `no_data` | 400 且訊息為 `No data is available…` | 回報 `source_error`；不視為 `no_data` 證據（供應商缺資料不等於交易所無成交） |
| `invalid_symbol`／`not_found`／`bad_request` | 其他 400、404 | 回報 `source_error`；需核對 mapping |
| `response_too_large`／`malformed_response` | 超出回應上限、非 JSON、非 `ok` | 回報 `source_error` |

#### 價格基準切換與舊回應快取

Provider-work journal 的 Twelve Data 日資料快取（`work_response` 的 `twelve_daily:*`）以價格基準
`adjust-none-v1` 作為識別的一部分。切換前保存的回應沒有此基準，可能是拆股調整價，Fetcher
不重播也不靜默重抓，改以保留原 bytes 的 failed／manual 結算處理，不會把 owner 留在
recovery blocked 而卡住同 provider 的後續工作：

| 情境 | 判定 | 處理 |
| --- | --- | --- |
| 同一 request identity 只有舊回應 | `durable_response` 找到無基準 key | 拋出一般錯誤 `ProviderWorkBasisMismatchError`；Full 成員標為 `manual / price_basis_manual_review` 並回報 `source_error`，同計畫其他成員照常處理；historical item 以 `PREPARED_DELIVERY_MANUAL_REVIEW` 結束 |
| Prepared body 由舊回應產生 | body 的 `payload.batch.source_raw_sha256` 等於某筆舊回應原始 bytes 的 SHA-256 | 該 body 不送出；Full 成員同上標為 manual；historical item 同上 |
| 排空 prepared ledger 時遇到上述 body | 同上，逐筆判定；只擋 Source 尚未接受的 body | 跳過該 body、其餘照常交付（已接受的舊 body 重播只取回原 run）；本 session 已記錄被擋 body 時，任何非 retry 結算（含 `completed`、`cancelled`、`failed`，以及
排空後重送的 `PREPARED_DRAINING` 結算）與 drain-only retry 都在送出前改為 `failed`，`failure_code=PRICE_BASIS_MANUAL_REVIEW`（已有 failure code 時保留），proof 帶 `manual_pending_prepared`、`price_basis_blocked` 與 `price_basis`；proof 仍有未回報的 `pending_source_errors` 時先維持 retry，回報後再轉換 |

判定逐筆進行：work 只是含有舊回應、但待送 body 不是由它產生（或待送集合為空）時，不阻擋。
Historical 的 scope stopped、drain-only 與 closed／draining 回合排空後若有被擋 body，一律以
`PREPARED_DELIVERY_MANUAL_REVIEW` 完成 item，completion 遇到 `PREPARED_DRAINING` 也改送同一
manual 結果，讓 item／request 與 work 一起進入終態。Full drain-only 回合不會清除
（Source 只在所有 prepared 都被接受後才解除 drain-only），因此被擋 body 會使該回合的 retry
轉為 manual failed，尚未抓取的成員改由 historical 回補。

判定只依該 work 自己 journal 中的 `twelve_daily` 回應：沒有 journal 回應可對照的舊 checkpoint
body（例如 provider-work 之前匯入、且已有 prepared 的工作）無法辨識基準。2026-10-30 核對時
production 匯入的 13 筆 US daily 均未開始、沒有 prepared，目前無此類 body。
原 `work_response` bytes、prepared bytes、receipts 與 R2 raw 全部保留；第一次偵測時於
`work_response_observation` 複製一列 `origin=incompatible_basis:adjust-none-v1` 作為證據，並在
Fetcher `full_work_diagnostic` 記為 `price_basis_mismatch`。

Owner 的後續處理：historical request 因 failed date 結束後，為相同日期建立新 request（新 work／
journal 以 `adjust=none` 抓取）；Full 計畫以 manual 成員結算 failed 後，以 historical request
回補該日期。取消 historical request 也會在原 owner 排空時走同一 manual 結算，不需人工改 DB。
不得刪除 journal 列、清除 prepared 或重設預算。

2026-10-30 唯讀核對：production journal 沒有 `twelve_daily` 快取，13 筆 queued US daily
尚未開始；staging 有 6 筆含舊快取的 Pilot daily 均為 completed 且 receipts 齊全，不會再被
claim。切換當下兩環境均無受影響工作。SSM：production `312dd583-1106-43a6-a026-95efdfbf3e60`／
`b76aca73-1d06-476d-bdeb-4b9c9ac341b3`，staging `ae01c182-e7ce-4157-9b95-9edcc361762b`／
`17c9ba56-09c5-42c5-954f-c3d4e904a37b`。

Full daily 成員迴圈（`_deliver_plan`）中以下失敗會附加一列到 Fetcher SQLite
`full_work_diagnostic`（work key、分類、HTTP status、provider code、是否已派送、時間）：
Twelve Data provider 錯誤（上表分類）、已派送的 quota 失敗（`quota_blocked`）、價格基準不符
（`price_basis_mismatch`），以及其他 provider 的未分類例外（`exception:<類別名稱>`）。
Source 4xx 拒收、mapping gap、normalization 失敗、acquisition 停止／時間窗關閉、FinLab 尚未發布、
FactSet 專用交付路徑與 historical 不寫診斷，仍以既有 `full_work.reason`、Source outcome 與
historical failure code 追溯。診斷在 Source 回報之後以 best effort 寫入，寫入失敗不影響回報。
`--health` 輸出的 `source_error_diagnostics` 為未完成成員最新分類的計數。歷史紀錄只會新增，
不改寫 `full_work`、prepared bytes、receipts 或 failure budget。

Source 的 `POST /api/v1/source/universes` 與 `POST /api/v1/source/provider-work/{id}/manifest`
使用 16 MiB nginx 與 Backend Content-Length 上限：前者容納完整市場成員，後者由 Fetcher 一次凍結
整份 cohort，沒有分頁或分塊協定。其餘 Source 端點維持 1 MiB nginx 上限及原有
`SOURCE_MAX_PAYLOAD_BYTES` Backend 限制（預設 1,000,000 bytes）。nginx 同時限制 chunked
body 的實際大小。Manifest location 必須巢狀放在 `^~ /api/v1/source/` 內，因為 `^~` 會停止
server 層的 regex 比對；兩個放寬端點都使用相同 IP allowlist、proxy 與 Backend Source 認證，
仍驗證 dataset scope、最多 30,000 個成員與 evidence；上限不是略過驗證的權限。
中途停機、撤銷某市場權限或任一回應失敗時，不提交部分 catalogue evidence。

這些程式保護不等於 native 驗收通過。驗收仍須包含真正 locked object 的重複寫入、五條現役 feeds
各自的非零 canonical／Serve 證據，以及 profile、plan、run 與 R2 bytes 的對應。
Shioaji 共用 runtime 明確套用既有 `SHIOAJI_SIMULATION=true` 驗證，再建立隔離 SDK；
Pilot／Full market 均使用資料擷取用 simulation，catalogue raw evidence 也記錄相同模式。
不合法設定在 child process 建立前拒絕，不能默默回退至 live-account mode。

啟動資格的程式修改位於 `refactor/full-market-baseline-calendar-start`；migration
`3553ed95cfbf` 允許 admission 的 legacy enrollment reference 為 null，保留既有歷史綁定。
所有環境的新抓取均不讀取 readiness／enrollment／expiry／capacity proof。Owner start 只依
published baseline；日曆缺少時依市場`weekend_days`預設，內建市場預設週六日休市；新抓取另遵守 running control、Source
及 provider API 認證與資料驗證。每次部署仍須確認 installed revision 與環境；最新 staging 資料查核見上方現況。
Nasdaq MIC 與 publication 排序修正已隨上述 c744a36 staging release 部署；production 狀態見 deployment runbook。

## 治理資料明確需求

啟動必要治理資料只包含 published baseline，其餘診斷／部署資料不列為啟動前提：

| 資料 | 必備內容 | 驗收方式／目前 staging 狀態 |
| --- | --- | --- |
| Published baseline | 每 provider／dataset 的 release ID、effective date、完整官方成員／provider mapping、來源 URL／SHA256 與不可變原始資料參考、member count／SHA256、Owner publication timestamp／audit | 同 provider／dataset、status=`published`、effective date 不晚於當地今天；staging 七個 feeds 已發布，release IDs 見上表。首份發布提供 `evidence_note` 與 `first_baseline_approved=true`；重大成員異動仍遵循既有 publish 規則 |

TAIFEX baseline 成員保留 TX／MTX／TMF／TE／TF、實際月／週到期 contract code／month 與
regular／after_hours sessions，排除價差與 continuous futures。官方成員與 mapping gaps 保留，
不能刪除缺口來宣稱完整。Owner 發布與 start audit 是既有操作紀錄，不新增 receipt／enrollment
等外部啟動核准步驟。

TAIFEX 新的 daily plan 必須使用與 trade date 相同日期的 published universe。
Source 先處理既有凍結計畫與依日曆政策略過的日期，再檢查目錄日期；需要更新時回傳
`409` 與固定 `taifex_dated_universe_required` code。Fetcher 此時才取得該日官方報表，
latest 日期不符時使用 TX／MTX／TMF／TE／TF 分產品歷史報表，透過原有 R2、governor
及 Source publication 流程建立目錄，再建立計畫。不能由過期週契約猜測 no_data。
官方歷史報表先發布盤後列、稍後才有一般時段列；沒有任何一般時段列的報表視同尚未發布，
與空報表同樣保留 date prerequisite 重試，不寫入 durable response。官方來源行為見
[TAIFEX 期貨日行情](taifex.md)。
日常 cycle 不先執行 TAIFEX latest 目錄更新；明確的 maintenance refresh 仍可使用。
報表尚未可用或目錄等待發布時，在 Full checkpoint 保存日期、原因與一小時後的
重試時間，provider-work `retry_at` 亦使用相同剩餘延遲，不再每 60 秒重新 claim；CLI health 的
`date_prerequisites` 可查詢，其他已凍結日期繼續補抓。
交付截止後若同日 candidate 仍未發布，missing-delivery alert 以
`DATED_UNIVERSE_PENDING` 區別一般資料缺漏；Dashboard「Universe 名單」或 Admin
`GET /admin/universes?status=candidate` 可列出候選，`GET /admin/universes/{release_id}/diff`
可唯讀核對相對最近 published release 的成員差異。這些入口不會自動發布或改寫 plan。
HTTP、認證、配額、cooldown 與 R2 故障仍中止本輪，不冒充日期略過。
既有計畫、成員與 terminal work 不受新日期限制影響。此變更須先部署 Backend，再部署 Fetcher，
部署期間維持 controls stopped；SQLite 僅新增 prerequisite table，保留原有 rows 與 quota debt。

舊版若已用前一交易日 release 凍結 TAIFEX 計畫，不得把已到期且未出現在當日官方報表的契約
改記 `no_data`，也不得改寫原 plan、成功 run 或 canonical evidence。一次性處置須先停止 TAIFEX control，取得當日官方歷史報表與 SHA-256，確認 provider owner 已釋放、
provider work 已 terminal 且沒有 prepared delivery，再以 Admin prepare audit 完整封存 Backend 與
checkpoint 原值。只有在 checkpoint snapshot digest 相符時，才能移除該 plan 的活動 SQLite
plan/work，將該 dataset/profile 的治理起始日移到 exact-date universe 保護開始的首日，並以
commit audit 結清舊告警。Backend 原 plan、parts、members、runs、canonical evidence 及 terminal
provider work 必須保留；恢復 control 後至少跨兩輪自然 heartbeat/monitor 驗證告警未重開、
checkpoint 無 unresolved plan、active provider work 為空。

Staging 的 `manual-pilot-taifex-1015-activation-boundary-20261027-v1` 依上述流程處理 Pilot
`tw_futures_eod`：10/15 plan `01a13ed6-ac22-757c-b152-ec5789a07804` 誤用 effective date
10/14 release，凍結已到期的 `MTX:202610W2`；10/15 官方 MTX 歷史報表 SHA-256 為
`e11e37614d41128349618522fb9d733b2b083c79b740e320d6356df4e2d9208a`，不含 W2、包含 W4。
Prepare audit `01a17c1f-0e5f-7014-894f-4dca381161ed` 保存 checkpoint snapshot digest
`7093d8560c349faf3f33b0e94aaa6999f324cbbaf762e34b0d05818fd12544bd`；活動 checkpoint 移除
1 plan／4 work 後 integrity check 為 `ok`。Commit audit
`01a17ca9-7a73-7ed3-b654-bf785a854129` 將 Pilot 起始日更正為 10/16，告警 resolution 設為
`STALE_UNIVERSE_PLAN_OUTSIDE_ACTIVATION`。原 1 plan、1 part、4 members、2 completed runs、
2 canonical evidence 與 failed provider work 均保留。恢復後兩輪驗證皆為 open alerts 0、active
TAIFEX provider work 0、checkpoint `unresolved=[]`／`manual_required=0`；Full Market TAIFEX
維持 stopped。

一般股票 universe 的自動發布使用 2% 變更門檻，不另設絕對筆數上限；異動比例超過時保留 candidate，
由 Owner 附 exception approval 發布。Owner 也可以在 Dashboard「Universe 名單」手動更新名單，手動更新
等同核准：

- Backend 排入一筆 maintenance work（stable key `maintenance:<dataset>:manual:<id>`，scope 帶 `manual_refresh`），
  Fetcher 依佇列順序執行。這筆 work 執行期間送出的名單直接發布，不受 2% 門檻限制；尚無 baseline 時成為
  第一份 baseline。當天已有內容相同、停在 candidate 的名單時，發布的就是那一份。
- `published_by` 記為提出要求的 Owner，`approval_note` 帶 work ID 與 Owner 填寫的原因；要求與發布各留一筆
  admin audit。
- 核准只在這筆 work 持有該 provider 的 owner 期間有效：排入但尚未執行、已結束，或排程自己排入的名單更新，
  都照原本的門檻判定。Source API 拒絕帶 `manual_refresh` 的 enqueue，Fetcher 無法自行取得核准。
- Control 停止時也會執行：maintenance 不要求 control 是 running，只需要該 provider 的 Fetcher 在運作。
- 同一個 dataset 同時只能有一筆未結束的手動更新。TAIFEX 的名單由每日報表產生，不提供手動更新。

TAIFEX 到期換倉另使用
fail-closed 結構規則：只接受 TX／MTX／TMF／TE／TF、合法月／週契約、regular／after_hours、
相同產品集合、每產品最多一個月契約及一個週契約向前替換，且當期契約不得早於 effective month；
共同契約可依官方報表增加 session，但不得移除既有 session。兩種規則的比較基準皆為 effective date
不晚於提交日期的最新 published release；較晚日期已發布時，重試的較早日期不以後者判定。
提交日期早於所有 published release 時，才以最新一份比較。產品、mapping、交易所、幣別、
分類、session 縮減或月份倒退均保留 candidate 等待人工審查。Release 同時保存
完整 `member_sha256` 與排除逐次 `raw_evidence_ref` 的 semantic digest；同日期、同語意的報表重抓
回傳既有 release，不再因 R2 evidence reference 不同產生重複 candidate。兩種 digest 的成員順序
一律使用 application 的 Python 字串排序，不依賴 PostgreSQL collation。

舊版已留下的 TAIFEX candidate 先以新版 Backend image 預覽，再明確套用；工具逐日只選最新
candidate，驗證完整與 semantic digest，並依前一份 published release 重新執行相同結構規則。
重複 candidate 保留為不可變 audit，已有同日 published release 時不再變更：

```bash
docker exec findb-ingest python scripts/reconcile_taifex_rollover.py \
  --deployment-target staging
docker exec findb-ingest python scripts/reconcile_taifex_rollover.py \
  --deployment-target staging --apply
```

預覽結果僅有 `would_publish` 才可執行 apply；`manual_review` 以 exit code 3 阻擋自動處理。
套用後維持原 daily plan 不變，queue 下次 claim 會重新取得該日 exact-date published release 並
建立 frozen plan。production 必須改用 `--deployment-target production`，且不得把 staging 輸出
當作 production 核准。

各 feed 的 baseline 對應如下，每個 dataset 都需要自己的 immutable release：

| Provider | Dataset | Calendar market |
| --- | --- | --- |
| Twelve Data | `us_equity_eod` | US |
| FactSet | `hk_equity_eod` | HK |
| FinLab | `tw_equity_eod`、`tw_etf_eod` | TW |
| FinLab（盤中） | `tw_equity_bar_1m`、`tw_etf_bar_1m`、`tw_equity_tick`、`tw_etf_tick` | TW |
| TAIFEX | `tw_futures_eod` | TAIFEX |

### FinLab 盤中 feed

四條盤中 feed 是 1 分 K 與逐筆成交，股票與 ETF 各一條，契約為 `intraday_partition.v1`：每個（標的、交易日）
一筆交付，資料存 Canonical R2（見 [盤中儲存](../architecture/intraday_storage.md)）。Staging 於 11/16、11/17
兩個交易日先行驗收；production 自 2026-11-18 起每日執行四條 feed，Serve 查詢見
[API 使用說明](../api/api_usage_guide.md#盤中資料)。剩餘工作見 [設計文件](../dev/finlab-intraday-batch1.md)。

- **啟用**：四個 dataset 由 migration 建立，和其他 dataset 一樣是啟用狀態（系統沒有「啟用 dataset」的操作；
  未啟用的 dataset 會讓排程卡片顯示設定錯誤，交付也會被拒收）。部署不會開始抓取：dataset 沒有 baseline 就不會
  被納入，而第一份 baseline 只能由 Owner 在 Dashboard 的名單頁手動更新產生，排程的名單更新只處理已納入的
  dataset。control 啟動時凍結當下可用的 dataset，所以在 control 執行中才有 baseline 的 dataset，要停止再啟動
  control 才會納入。
- **名單**：沿用台股日 K 的官方名單（TWSE／TPEx），不查 FinLab 的目錄。FinLab 沒有盤中標的清單，而且會提供
  日 K 價格表還沒有的標的（例如新上市），所以每個已分類的成員都會嘗試。
- **排程**：台北 19:15，完成期限為隔日 07:15（開始後 12 小時）。日 K 要等 FinLab 約 18:50 的最終版，等待時的重試排到
  佇列尾端，所以盤中排在最終版之後，日 K 才不會被插隊（見「FinLab 日 K 只取最終版」）。FinLab 的 control 顯示的是該 provider 最早的 feed 時間
  （18:00）；feeds 的時區不同時，以同一天換算成 UTC 的時刻比較。同一個交易日的工作依排程時間排入佇列，日 K 一定排在盤中之前；跨日補抓時仍是日期優先。
- **Historical**：不支援，回補不在第 1 批範圍。

FinLab 對單一成員的回答與結果：

| FinLab 的回答 | 結果 |
| --- | --- |
| 該日分區已發布 | `data`。FinLab 的原始檔與轉成契約欄位的檔都存 Raw R2，Worker 寫入日檔與索引後才計為有資料 |
| 該日沒有成交（`no_trades`） | `no_data`，證據是 FinLab manifest 的該筆紀錄 |
| FinLab 尚未驗證該交易日 | 沒有標的可取：整輪等待發布，不記在任何成員身上 |
| 沒有該標的的分區，同日日 K 無成交，或 FinLab 當天沒有成交價 | `no_data`（`no_trade`），證據狀態為 `no_record`，佐證記在成員的結果歷史 |
| 沒有該標的的分區，同日日 K 有成交價或尚未有結果 | 只影響該成員：回報 `blocked`（`empty_response`）並保留有限次重試，其他成員繼續 |

FinLab 對當天沒有整張成交的標的不產生分區，也不標示 `no_trades`；分區還在產生中時，回覆也一樣。兩者從
FinLab 的回覆分不出來，所以由 Source 對照同一個 provider 同一天的日 K 判定：

- 台股的開高低收只由整張成交產生，零股只增加成交股數。FinLab 當天沒有給成交價時，日 K 依上方 `source_proven_no_trade` 的規則以最近一個收盤價補值交付，所以 canonical 日 K 的開、高、低、收都等於前一筆日 K 的收盤價。
- 接受為無成交的條件：日 K 計劃中該成員為 `no_data`（`daily_no_data`），或同日 canonical 日 K 由同一個 provider 寫入，而且開高低收都等於前一筆日 K 的收盤價（`carried_close`，記下前一筆的日期）。
- 成交量不列入判定。2026-11-18、11-19 production 的實測：FinLab 日 K 有成交價的標的全部都有分區，沒有成交價的全部都沒有；被舊規則（成交量少於 1000 股）擋下的 4 檔次，都是零股合計超過 1000 股、FinLab 沒有給價格。反過來，有幾檔 ETF 成交不到 1000 股，FinLab 卻有價格，也有分區。
- 拒絕的情況（409，`no_data_not_corroborated`）：日 K 有成交價、價格與前一筆收盤不同、沒有更早的日 K、日 K 由其他來源寫入，或日 K 還沒有結果。Fetcher 收到這個拒絕就照舊回報 `blocked`，並在下一輪重試；日 K 之後有結果，下一輪會再判定一次。
- 已知的誤判風險：真的有整張成交、全天只有一個價格、又剛好等於前一日收盤的標的，如果它的分區在判定當下還沒產生，會被記為無成交。這種標的每天約 1–12 檔，但它們都有分區，兩天的實測中沒有遇到分區晚到。
- FinLab 的 manifest 把該日列為休市時不適用，照舊回報 `blocked`，這是日曆不一致，要人工處理。

**隔天回查**：無成交一旦記錄就不能修改，Fetcher 也不會再抓那個成員。為了發現「分區只是晚到、卻被記成無成交」，
每條盤中 feed 每天第一輪開始時，會對前一個交易日記為無成交的成員（不論依據是 FinLab 的 `no_trades`、
`daily_no_data` 或 `carried_close`）再問一次 FinLab 的 manifest：只問有沒有分區，不下載、不計配額，最多 100 檔。
結果輸出一行 `{"finlab_no_trade_recheck": {...}}`：

| 欄位 | 意思 |
| --- | --- |
| `trade_date` | 被回查的前一個交易日 |
| `no_trade_members`、`checked` | 前一交易日記為無成交的檔數，與實際回查的檔數 |
| `unanswered` | 分區還在產生中，或 FinLab 回查失敗（遇到失敗就停止當天的回查），沒有確定答案的檔數 |
| `now_published`、`symbols` | 現在已經有分區的檔數，與前 20 個代號 |

- `now_published` 大於 0 時另外輸出一行 `{"finlab_no_trade_partition_found": {...}}`，內容相同，給 log 告警使用（CloudWatch 告警規則尚未設定）。這些成員要人工回補。
- 只輸出 log，不改任何已交付的結果；回查失敗或 control 停止時就不輸出，不影響這一輪。
- 前一交易日的計劃取自 Fetcher 本機的 checkpoint（工作結束後保留 7 天），不在本機時不回查。

## 設定與啟動

Backend 先 migration、registry 初始化及 Source scope 校準。Owner 發布 baseline 後才人工啟動；部署本身不
start controls。Production 執行 Full daily；staging 平時保持停止，Owner 只在測試改動時一次啟動一個 provider，
驗收後即停止。Twelve Data 的 staging 與 production 共用同一帳號（來源無法發多組 API key），兩環境 governor
各自計算、沒有跨環境預算，staging 抓取會直接消耗 production 的 credits／分鐘配額。每個 consumer
逐檔串行，#504 之後 production 首輪約 140 檔／分鐘（見 [Performance](performance.md)），兩環境合計仍遠低於帳號
1597 credits／分鐘上限，
因此 staging 一次啟動一個 provider 驗收時不需事先協調 production 配額（#449）。若之後提高單一
consumer 的並行度或速率，須重新評估，必要時改以兩環境 allocation 切分配額。Daily 只抓當日，
回補天數由管理者建立回補時決定；完整範圍回補在 production 執行。Staging fetcher 主機記憶體小（見
[部署 OOM 紀錄](deployment.md#2026-10-22-staging-historical-finlab-oom-阻擋部署)），FinLab worker 一律載入
整張 dataset；staging 只做少量回補，啟動後須觀察記憶體與 OOM。

### Provider 變更的驗收清單

改到 provider adapter、成員交付、結算規則或排程的變更都照這份清單驗收。部署成功只代表 container 換版完成，
不代表抓取正確（見 [deployment](deployment.md)）；清單裡的數字要寫回對應的 issue 或 PR。

合併前：

- `pnpm check`、`pnpm test:fetcher`、`pnpm test:backend` 都通過。只改 `fetcher/` 也要跑 Backend 測試，因為
  Backend 的 provider work 測試會驅動真正的 Fetcher runtime。改到 catalog 另外執行 `pnpm contracts:check`。
- 行為改變對應的 `docs/` 內容在同一個 PR 更新。
- Staging 所有 controls 已停止（desired 與 observed 都是 stopped），沒有進行中的 historical 或 maintenance
  work。Fetcher 部署會重建所有 provider 的 container，有工作在執行時部署會失敗並還原。

Staging 驗收（一次只啟動一個 provider，結束後停止）：

1. 啟動前確認各 dataset 的首次日期與既有 plan：啟動後會從首次日期補抓到今天，驗收只留一個交易日。
2. Owner 啟動該 provider 的 control，等 daily 的 provider work 結束。
3. 核對 daily 的結果：
   - work 的結束狀態與 `failure_code` 符合預期。
   - Plan 統計符合 `expected = data + no_data + missing + blocked`，`missing` 為 0，每個 blocked 成員都有原因。
   - 這次的 `ingestion_run` 全部 completed，沒有新的 `dq_issue`。
   - Canonical 筆數等於 `data`；抽一檔從 Serve 查得到，數值與來源一致。
4. 建立一筆單日的 historical request，核對結束狀態、逐成員結果與耗時。
5. 核對資源：container 沒有 OOM、記憶體峰值（cgroup 的 `memory.peak`）、provider work journal 的檔案大小。
   FinLab 每張表是整段歷史，staging 主機記憶體小，這一項不可省略。
6. 核對 log：有 `delivery_timing`，沒有 traceback。Container 的輸出同時送到 CloudWatch 的
   `/findb/<環境>/fetcher/containers`（每個 container 名稱一個 stream），部署重建後仍查得到；
   `docker logs` 只看得到目前這個 container 的本機快取。
7. 停止 control，確認 observed 也是 stopped。

Production：

- 選所有 provider 的 daily 都不在執行的時段。停止所有 controls 並等 observed 也是 stopped，確認沒有進行中的
  historical 或 maintenance work，再 promote staging 已接受的 bundle。同一批有 Backend 變更時 Backend 先部署。
- 部署後重新啟動 controls。以部署後第一次 daily 重做上面第 3、5、6 項。
- 結果不符預期時，用 production 部署 workflow 的 rollback 模式回到上一個已接受的 bundle。

### 以 historical 回補 Full 缺漏（Twelve Data、FinLab）

Twelve Data `us_equity_eod`、FinLab `tw_equity_eod` 與 `tw_etf_eod` 的 historical request 建立時，每個要抓的
日期凍結一個 Full universe release：該日期已有 Full plan 時用 plan 的 release，沒有 plan 時用該日期以前
最新的 published release；成員為該 release 中已上市（`listed_date` 不晚於該日）的 mapped 成員全部，
不只 plan 的缺漏成員。Request 只保存每個日期的 release ID、release digest 與 cohort digest，claim 時
從不可變 release 重讀並核對；任何日期沒有可用 release 或沒有 mapped 成員時拒絕建立。

清單可能晚發布，所以每個日期另外凍結建立當下、在該 release 之後發布的同 feed release（#629，snapshot
version 3）：這些 release 中已對上、上市日不晚於該日（沒有上市日也算）、但不在該日 cohort 的成員是**補查**成員，
同一代碼以最新 release 的身分為準。補查成員也會抓取並做同樣的身分檢查，有資料就寫入並記在該日期的
`unlisted_symbols`，沒有資料不算缺漏；`expected` 與成員證據仍只算該日的 cohort。只有 FinLab 清單有上市日，
Twelve Data 與 FactSet 無法判斷後來的成員當天是否已存在，因此一律補查而不列入 expected。該日的目錄補查
（`catalogue_supplement_id`）也一起凍結，同一代碼以 release 成員的身分為準。Claim 時以凍結的
release ID 重算補查成員並核對 digest；manifest 的 `supplementary` 必須與它相同。Version 2 的舊 request 沒有補查成員。

Fetcher 逐成員使用 Full daily 的 adapter（`provider_symbol`、商品類型、幣別身分檢查；FinLab 每個日期只下載
一次五張欄位表；Twelve Data 與 daily 相同，把該日期接下來還要抓的成員以逗號合成一次 `/time_series`，最多 50 個、
每 symbol 1 credit，已有結果或 receipt 的成員不加入，一批只存一份 raw，該批成員的單檔失敗引用這份 raw），
每成員一個 `market_eod` v1 `backfill` request，`work_item_id=historical:<item>`：

| 情況 | 成員結果 | 日期 |
| --- | --- | --- |
| 寫入 canonical | `data` | 繼續 |
| FinLab 價格全空且無成交量 | `no_data`，附五表 bundle 的 raw | 繼續 |
| FinLab 價格全空但成交量大於 0 | `data`：開高低收填最近一個有值的收盤價，規則與 daily 相同 | 繼續 |
| Twelve Data no_data／404、FinLab 表中沒有該 symbol | `missing`（`no_source_row`） | 繼續 |
| FinLab OHLC 等單檔值不合法、Twelve Data 幣別不符或 mapper 拒絕、Source DQ 拒絕 | `invalid`（`row_structure_invalid`），附 raw | 繼續 |
| 限流、provider 暫時錯誤、FinLab 尚未發布該日期 | — | 整日稍後重試；已完成成員不重抓 |

成員送出後不逐檔等待 normalization（FactSet daily 與 historical 也一樣）：已接受的 run 先不讀取結果，繼續抓取與交付後面的成員，同時最多保留 16 筆
未讀取的 run，超過時才讀取最早的一筆；全部成員送出後再讀完剩下的。讀取時 run 仍在佇列才等待（0.25、0.5、1、2 秒
遞增），單筆最長 30 秒，逾時則整日稍後重試，已讀取的成員結果保留。交付期間每 60 秒與結束時輸出與 daily 相同格式的
`delivery_timing`，以 `item_id` 標示，另含 `step normalization_wait`。

確定性的成員錯誤不觸發整日重試。暫時錯誤每成員（FinLab 為每日期共用表）最多 5 次 native 嘗試，用盡後
該成員記 `missing`；FinLab 共用表用盡則該日期失敗。有任何非 data／no_data 成員時 item 為 `partial`，
Backend 逐成員驗證 evidence 與本 work 的實際寫入證據。本變更前建立的 request（沒有 cohort）仍沿用舊
排程器 executor 與靜態清單，至排空為止。

```bash
cd fetcher
export APP_ENVIRONMENT=local FETCHER_ACCOUNT_STATE_PATH=.state/account/governor.sqlite3
uv run --env-file .env findb-fetch-runtime --execution-profile full_market --provider finlab \
  --config configs/bootstrap.local.v1.json --state-path .state/full_market/state.sqlite3 --sync-universes
uv run --env-file .env findb-fetch-runtime --execution-profile full_market --provider finlab \
  --config configs/bootstrap.local.v1.json --state-path .state/full_market/state.sqlite3 --run-forever
```

Consumer 以 `FETCHER_CONSUMER_PROFILE=full_market|maintenance|historical` 標示角色（部署 consumers 只用
`full_market`／`historical`）；未設定時為 legacy bounded 行為。`FETCHER_EXECUTION_PROFILE` 已退役。Full runtime 一律使用共用 account governor，已無各
checkpoint 自帶的本機 limiter 路徑；各 consumer 共用同一 governor 絕對路徑，不能把 checkpoint 與
account DB 指向同一檔。既有 governor 的 pins、debt、unknown liability、cooldown 不得刪除或重設。
維護同步（universe sync）只依 Source credential 綁定授權，不需要 enrollment 或已發布 baseline，
因此可在首份 baseline 前執行；新抓取仍要求 published baseline 與 running controls。

AWS 路徑、首次切換、備份與回退下限見 [deployment.md](deployment.md)。CLI 的 `--check` 不呼叫 provider，
`--initialize-state` 只允許新 installation 建立 schema，既有 checkpoint 遺失必須阻塞。
`--require-stopped` 唯讀驗證 Full checkpoint 與 controls，不能藉部署修改 desired state。

## Dashboard 與 API

Overview／交付監控只呈現 Full market，沒有執行模式分頁；Dashboard 對 Admin API 固定傳入
`execution_profile=full_market`，舊連結的 `profile` 參數會被忽略。頁面呈現數量、缺口、告警、首次日期與啟動狀態。
API 的 `execution_profile` 參數保留但只接受 `full_market`，其他值回傳 422；未指定時即為 Full market。
細節見 [dashboard.md](dashboard.md)。

### Freshness 的持久化完整度摘要

`market-freshness` 的 Full market 完整度與最新 run metadata 讀自
`daily_delivery_plan_summary`，不在 GET 中載入歷史成員、canonical K 線或執行
`reconcile_plan`。每個 feed 的當期數量、最新完整日期與 run metadata 使用同一個 SQL
snapshot；交易日曆、首次日期與 deadline 仍依查詢時間計算，停止 scheduler 不會凍結
時間判斷。頁面更新不會觸發抓取或摘要寫入。

摘要以 immutable plan ID 區分，與原始資料／run／實際寫入證據在同一 transaction commit；
Plan row lock 串行化同 plan 的摘要寫入，失敗 rollback 不得先公布摘要。鎖順序固定為 plan → part：
摘要寫入者在 plan lock 下更新 part 計數；outcome 只鎖自己的 member 列（`FOR UPDATE OF`），不鎖
join 到的 part，再取 plan lock，避免與 worker 的 part 更新互相等待而死結。
`daily_delivery_part` 另存各 part 的 data／no_data／missing／blocked／mapping_gap 計數，
各 part 加總等於 plan 摘要。成員狀態只取決於自身的 canonical、證據、outcome 與 mapping，
因此：

- 建立 plan、分鐘資料（`sequenced_snapshot`）的 run、受控重建與缺少摘要的初始化，對整個
  cohort 完整重算，並重寫所有 part 計數。
- Daily run 的 terminal（完成、失敗、retry exhausted）與 no-data／blocked outcome，只在 plan
  lock 下重新分類該 part（約 50 個成員），把差值加到 plan 摘要；outcome 回應直接讀已更新的
  計數，`gaps` 為空，完整缺口由 GET plan 或 Admin reconcile 提供。
- Historical run 只重新分類它實際寫入成員所在的 part（同 provider／dataset／date 的 plan）。
- `resolve_missing_delivery_for_run` 以已存摘要加上該 part 的重新分類判斷 plan 是否完成，
  不再分類整個 cohort。

Part 計數為 NULL（既有 plan）或 `parts_synced_at` 不等於 `updated_at`（其他 release 寫過
摘要）時不可信，該次寫入改走完整重算並重新初始化。接受非 terminal、非 `sequenced_snapshot`
的新 run 不會改變計數，只以單調條件更新 latest run metadata，不取 plan lock、不重算；完整
重算寫回時也只在其 latest run 不舊於已記錄者時覆寫 metadata。Processing 狀態不刷新摘要；
queue worker 只在 terminal commit 前刷新一次，直接呼叫 normalizer 時由 terminal 狀態更新
負責。Missing-delivery 掃描本來就對逾期 plan 完整分類，順便與已存摘要比對：掃描期間摘要
未被寫入但計數不同時，記 `Plan summary drift corrected` warning 並完整重算修正。
Historical 實際寫入的修復會更新同 dataset／provider／date 的 plan 摘要：FactSet、Twelve Data 與
FinLab 的 historical work 只要以相同契約、相同 provider symbol 凍結該成員並有本 work 的 receipt 與
canonical 寫入證據，該成員即算 data，解除 gap 與 missing-delivery 告警；原 Full work 的結算不改寫。
Historical 的 no_data 證據同樣會讓同 dataset／provider／date 的 plan 成員算 no_data（#595），條件是：

- 回補項目（`historical_backfill_item`）以 completed 或 partial 結束；request 之後被取消不影響已完成的項目。
- 該成員在項目的完成證據（`result_evidence`）是 `no_data`，並附原始檔（R2 ref 與 SHA-256）。
  `missing`、`invalid` 與沒有原始檔的列都不算，供應商的空回應不會變成 no_data。
- 該項目的 work 以證據所列的契約、相同的 provider symbol 凍結該成員，與 data 的條件相同。
- 成員在 plan 上不是 mapping gap（`mapping_status = gap` 或 outcome reason 為 `mapping_gap`），gap 仍算 blocked。

這份證據優先於 daily 記下的 blocked（例如舊版的 `blocked / source_error`），但 canonical 寫入仍優先於 no_data。
成員的 `outcome`、原 Full work 的結算與 receipts 都不改寫，只影響 plan 的統計、lateness 與告警。
No_data 沒有 run，所以回補項目完成時，Backend 直接重新分類這些成員所在的 part。修正上線前已完成的回補，
用下面的 `rebuild_delivery_plan_summaries.py` 重建；期貨與分鐘資料不在這個範圍。
OHLCV 人工修正不改變 member 身份或完整度，無須重算摘要。

`expected = data + no_data + missing + blocked` 與原 canonical／durable evidence 規則相同。
分鐘資料仍要求所屬 snapshot/update 的所有最新 sequence attempts 都完成；新 queued
或 failed attempt 必須使該 group 失效，不能沿用之前完整的計數。No-data 仍需原本的
官方證據驗證，canonical 實際寫入可優先於既有 no-data。

Migration 只建立摘要表，保留 frozen plans、成員、outcomes 與 canonical 資料。
Dispatcher 以獨立維護 task、每批最多五個 plan 初始化既有資料，每批 transaction 上限
30 秒；已初始化後只檢查缺少摘要的 plan，不循環重建全部歷史。初始化未完成時 freshness
回報 `delivery_summary_pending`，不在 GET 回退至昂貴的重新計算，也不宣稱未知 plan 完整。
部署驗收須等下列查詢為零：

```sql
SELECT count(*) FROM daily_delivery_plan p
LEFT JOIN daily_delivery_plan_summary s USING (plan_id)
WHERE s.plan_id IS NULL;
```

受控 SQL 維修若直接改動 runs／member evidence／canonical lineage，須重建受影響 scope。
下列工具預設 dry-run；`--apply` 才寫入，只使用 Backend application DB 身份，在 Backend
host／container 執行，不能交給 Fetcher。每批獨立 commit，可安全重跑；不要與 schema
升降版同時執行。先 migration，再完成初始化／重建與驗收，不能只以 `/health` 判定完成。

```bash
uv --directory backend run python scripts/rebuild_delivery_plan_summaries.py --provider shioaji --profile full_market
uv --directory backend run python scripts/rebuild_delivery_plan_summaries.py --provider shioaji --profile full_market --apply
```

工具也接受 `--dataset` 與 `--batch-size`（1–100，預設 5）。Canonical／證據的直接維修應在
維護時段完成並重建，避免讓已維修但尚未重建的摘要對外顯示。一般 application 寫入不需要
人工重建，所有 GET 仍保持唯讀。Schema 為 additive；移除摘要表前須停止新版 application。
Migration `b4d6f8a1c3e5` 只新增 nullable 的 part 計數與 `parts_synced_at`，不回填；既有 plan
在下一次寫入時完整重算一次。回滾到前一版不需降版：舊版忽略新欄位，其摘要寫入讓
`parts_synced_at` 落後，新版回來後先完整重算；降版只刪除這些衍生計數。

## 用量與傳輸保護

正常抓取使用 provider catalog `platform_limits`（經 Fetcher 打包的 execution descriptor 由
`platform_limits.py` 讀取）中的已確認限制。Twelve Data 按 1597 credits／分鐘 pacing；
Shioaji 按 50 行情請求／10 秒 pacing，並在 Kbars 前查 `api.usage()`，每日達 500 MiB 就拒絕抓取。
FinLab 由 authenticated SDK API 執行每日 5000 MB 配額；detached dataframe bytes 只用於傳輸
驗證，不當作 API network usage。TAIFEX 不填造數值配額，429 保留 account cooldown。
FinLab SDK 的完整歷史 DataFrame 仍計入既有回應大小上限，但以每批 16 個欄位計算
與完整 `orient=columns` JSON 相同的 UTF-8 bytes，避免同時持有整份 JSON 字串與 bytes 副本。
全域重複欄位／索引仍在 accounting 前拒絕；完整大小只回報一次，之後才選取目標日期與商品。
此計算不改 provider 配額、下載請求、回應上限或凍結計畫，也不保證 SDK 本身載入 DataFrame 的記憶體足夠。
FinLab universe refresh 使用獨立的欄位存在性查詢，股票與 ETF 分別處理；官方名單中的缺欄商品
保留為 mapping gap，不因單一 ETF 缺欄而清除股票目錄的有效 mapping。目錄證據只表示
provider 欄位存在，沒有交易日期或價格成功語意；每日 OHLCV 仍要求指定日期、全部凍結成員
及完整欄位。每個成功的 provider catalogue 先保存 R2，再將 credential-free HTTPS 身分與
detached bytes checksum 加入 release evidence。SDK 資料錯誤只影響該 feed；授權、配額、
回應 accounting 或 R2 失敗中止 refresh，不發布假缺漏。TW 目錄的 effective date 使用台北日期，
universe request 的未來日期檢查亦依 source timezone 判定。

名單更新（universe refresh）的時間跟著 daily 的排程時刻，不跟著上一次完成的時間：

- 到期時間是該 provider 排程時刻的下一次（FinLab 為每天台北 18:00，取自 Backend 發布的排程）。名單更新完成後，
  不論是排程、啟動後補跑或手動觸發，下一次都回到排程時刻，所以不會每天往後漂移，也不會跟著 control 的啟動時間走。
- 排程時刻那一輪 cycle 先排入名單更新，再排入當天的 daily。佇列先進先出，當天的 plan 因此使用當天排程時刻取得的
  名單；已經在佇列裡的較早工作仍排在名單更新前面。
- 只有 control 是 running 時才會排入。停止期間不更新；啟動時若已過到期時間，第一輪就先補一次，下一次回到排程時刻。
  已經排入的名單更新不受之後的停止影響，會照常完成。
- 同一個排程時刻只有一筆名單更新（stable key 帶該時刻的 UTC 時間）。因傳輸問題重試的那一筆會排到佇列尾端，
  排在當天的 daily 後面。
- 傳輸失敗分兩種：官方網站或 provider API 是 `MAINTENANCE_PROVIDER_TRANSPORT`，Source API 是
  `MAINTENANCE_SOURCE_TRANSPORT`。attempt 的 proof 帶 `{"transport": {"origin": "provider"|"source", "url"}}`，
  `url` 只留 scheme、host、path 與 query，不留帳密與 fragment，`apikey`、`token` 等 query 值記為
  `<redacted>`（官方頁面同一路徑靠 query 區分）；錯誤沒有帶 request 時 `url` 為 null。Source governance
  的傳輸失敗本來就是 `MAINTENANCE_SOURCE_UNKNOWN`。
- FinLab 與 Twelve Data 的名單在排程時刻取得，生效日就是當天的交易日。當天的 daily 建立 plan 時，若查到的最新名單
  生效日早於交易日，表示當天的名單還沒發布：daily 不建立 plan，以 `DAILY_UNIVERSE_REFRESH_PENDING` 延後
  5 分鐘（不扣重試次數）重新排隊，讓名單的重試先跑完。最多等到排程時刻後 30 分鐘；之後照舊用最新的名單建立
  plan，並輸出一行 `{"universe_refresh_stale": {"dataset", "trade_date", "release_effective_date"}}`。
  只等同一個 feed 的名單，股票名單失敗不影響 ETF 的 daily；補抓較早的日期不等。FactSet 的 HKEX 清單帶自己的日期，
  不套用這條規則（#598）。
- 這個 feed 的名單更新若在排程時刻之後已經完成，daily 就不等：來源本身的日期仍是較早的那天，例如週末與美國假日
  Twelve Data 讀到的 Nasdaq 檔案是上一個交易日，或名單變動超過門檻而成為 candidate，再等也不會出現新名單。
  完成時間記在 Fetcher state 的 `full_universe_refreshed`（每個 provider 與 dataset 一列）。2026-11-22（週日）
  production 曾因此等滿 30 分鐘、多 6 筆 attempt，才以休市結算。
- Fetcher 取不到排程時（例如手動執行 `--sync-universes` 的程序），完成後的下一次到期時間退回 24 小時後。
- TAIFEX 不跑每日名單更新，不受這些規則影響。

官方名單下載的來源、時間限制與共用：

- TW 的商品名單來自 ISIN 網站的分類查詢（`class_main.jsp`，依市場與證券別各查一次）。股票的 universe refresh 讀
  TWSE／TPEx 公司清單與上市、上櫃股票兩個查詢；ETF 的 universe refresh 只讀上市、上櫃 ETF 兩個查詢。四個查詢
  合計約 1.1 MB，從 production 主機每個 1–26 秒。不下載 `C_public.jsp` 的完整頁面：兩頁合計 13.6 MB，其中 97% 是
  權證，從 Fetcher 主機下載要 7–9 分鐘。
- 分類查詢的參數沒有官方文件，所以每次都驗證回應：欄位名稱、每一列的連續編號、市場別與有價證券別都要符合
  查詢條件，頁面要完整結束。查無資料（HTTP 200 的轉址頁）、頁面被截斷或內容不符時整份名單 fail closed：該筆
  maintenance work 以 `MAINTENANCE_FORMAT_OR_SCOPE` 結束，不發布，daily 繼續使用上一份 release，下一次 refresh
  重新下載。完整但筆數異常減少的回應由 Backend 的 2% 變更門檻擋下。
- 單一官方名單的下載總時限是 20 分鐘（`OFFICIAL_SNAPSHOT_TIME_LIMIT_SECONDS`）。HTTP client 的 30 秒 timeout 只限制
  每次讀取的等待，對方持續慢速回應時原本沒有上限；超過總時限視為傳輸逾時，依 `MAINTENANCE_PROVIDER_TRANSPORT` 重試。
- 同一個 Fetcher 程序裡，maintenance work 在 30 分鐘內下載過的同一個網址，下一筆 maintenance work 直接沿用
  （內容與 R2 物件都沿用，不再送出請求）；每筆 work 仍各自在 journal 保存一份以供重播。股票與 ETF 的
  universe refresh 讀的網址不同，所以只有同一個 dataset 在 30 分鐘內再次 refresh 時才會沿用。Daily 與 historical
  讀的是會隨發布更新的報表，不沿用。
- 下載期間每 30 秒回報一次 scheduler control 的 heartbeat，長時間下載不再讓 heartbeat 超過告警門檻。
- FinLab 取一張表的子程序時限是 10 分鐘（`FINLAB_GET_TIMEOUT_SECONDS`，原本沿用 30 分鐘的預設值）；逾時記為
  `finlab_sdk` 的 `process_timeout`。

TW universe 成員帶有 ISIN 的上市日（`listed_date`，存於 `universe_member_listing`）。ISIN 可能在上市前就列出商品，所以上市日
欄位缺漏或格式錯誤時整份名單 fail closed，不視為已上市；parser 與 release 都不依上市日過濾，因為
release 會被之後的日期沿用。分類查詢不保證提前列出：2026-11-13 查核時，上市日在下一個交易日的權證已列出，
上市日在六天後的兩檔 ETF 尚未列出（完整頁面已列出）。新上市的商品要等分類查詢列出之後的下一次 universe refresh
才進入 release。建立 daily plan 時才以 trade date 判斷：`listed_date > trade_date` 的成員
不列入 expected，上市當日起列入；已上市但仍是 mapping gap 的成員維持 blocked 與告警。沒有上市日
的成員（其他市場、或上市日加入前建立的 release）視為已上市。Plan 有排除成員時，`member_sha256`
改為過濾後成員的 digest；沒有排除時與 release 相同。第一次帶入上市日的 release 不把「補上上市日」
算進 2% 變更比例，之後上市日改變才計入。

FinLab SDK 固定在 2.3.11（`fetcher/pyproject.toml` 與 `providers/finlab.py` 的 `FINLAB_SDK_VERSION`，
兩者由測試核對一致）。Adapter 拒絕其他版本；子程序另外檢查它依賴的 SDK 名稱都存在，缺少時以
`sdk_contract` 結束，不發出任何請求。升級 SDK 時依序做：改這兩處與 `uv.lock`、以
`uv --directory fetcher sync --extra finlab` 安裝後執行 `tests/test_finlab_sdk_contract.py`、
確認 CI 的 FinLab image 步驟通過 `verify_sdk_contract()`，再依
[Provider 變更的驗收清單](#provider-變更的驗收清單)在 staging 驗收。

- 身分只用 API token。子程序以 `login(token)` 選取 token 之後，就把 SDK 的登入入口換成拒絕：SDK 遇到認證
  錯誤或找不到 token 時會開啟瀏覽器登入，在無人值守的 container 裡只會等到逾時。SDK 若選到別的身分
  （例如 home 目錄下留有瀏覽器登入的憑證檔）也以 `auth` 結束，因為記帳的帳號就不是實際下載的帳號。
- SDK 把 API token 登入標為已棄用（原訂 2026-08-01 之後移除）。2026-11-16 以正式帳號實測，2.3.11 送出的
  API token 仍被接受。FinLab 停止接受時，症狀是所有取表都以 `auth` 結束；屆時要改用 SDK 的 refresh token
  登入，identity 與帳號 governor 的對應要一併重新設計。
- 本機 cache 在 host 的 `/var/lib/findb-finlab-fetcher/cache`（container 內 `~/finlab_db`，同環境的 FinLab consumers
  共用，SDK 以檔案鎖協調）。每張表一個 `.feather`（價格表約 18 MB、成交股數約 30 MB），第二次讀取起旁邊另有
  SDK 的解碼副本 `.decoded`（28–41 MB）；五張表合計約 260 MB。SDK 不寫 `/tmp`。
- 升級後第一次取表只下載差異，不會整張重新下載：SDK 沿用 1.5.7 留下的 `.pickle` 要求差異更新，合併後寫成
  `.feather`。2026-11-16 兩個環境的實際結果：staging 五張表合計計入 7.1 MB；production 每張 1.4–1.5 MB、合計
  7.4 MB，之後 cache 只剩 `.feather` 與 `.decoded`。離線以假的 API 重現時曾得到「整張下載」的結果，與真實 API
  不符，不可作為估算依據。
- 本機沒有 cache 時才整張下載，依壓縮後的檔案大小計費：2026-11-16 實測五張表合計約 103 MB 配額
  （`billed_bytes`）。回復到 1.5.7 的 image 時，舊 SDK 讀不到 `.feather`，預期會整張重新下載（未實測）。
- 記憶體：子程序設定 `ARROW_DEFAULT_MEMORY_POOL=system`（Arrow 預設的配置器不立即歸還已釋放的記憶體）。
  以價格表大小的資料（4,808 × 2,800）與假的 API 在本機 Linux container 量測子程序的 RSS 峰值：

  | 路徑 | 1.5.7 | 2.3.11 |
  | --- | --- | --- |
  | 整張下載 | 約 535 MB | 約 430 MB |
  | 每日差異更新 | 約 535 MB | 約 680–710 MB（未設定配置器時約 880 MB） |

  每日更新比 1.5.7 多約 150–175 MB：SDK 先載入本機的舊表，合併後再整張讀回。合成資料是不可壓縮的亂數，
  比真實的表重：2026-11-16 以真實 API、當時 staging 的 container 設定（`--read-only`、16 MB 的 `/tmp`、1 GiB
  記憶體上限）取五張表，整張下載與讀取本機 cache 的子程序峰值都是約 360–375 MB，沒有 OOM。
- 真實資料的每日差異更新（容器的 `memory.peak`，含檔案快取，都沒有 OOM）：staging 在 1 GiB 上限下是
  785–800 MiB（2026-11-13 的 daily）；production 沒有上限，是 940 MiB（2026-11-16 的 daily）。Staging 的上限
  因此已移除（見 `docs/operations/deployment.md`），之後在 staging 啟動 FinLab 時要留意主機的可用記憶體與 swap。

Gateway 對 FinLab 原生 FileStorage 設定 `use_cache=False`，只釋放記憶體中的
DataFrame references，保留既有磁碟檔案與修訂 metadata；不呼叫會刪檔的 `clear()`。
SDK scope 內同時處理呼叫前後的實際 storage 與 package re-export，避免切換 storage 後
舊物件仍保留整份歷史資料。純記憶體 CacheStorage 與自訂 storage 不變，避免清除唯一副本而增加下載。
有效且未變動的檔案仍由磁碟命中；外部更新／過期時遵循 SDK 的 recent merge、hash 檢查及完整下載 fallback，
所有網路使用仍受原有 acquisition／governor 管理。這項調整不保證單一大型 frame 或 CacheStorage 的記憶體上限。
Source API 依 DB-backed credential 的實際 rate limit 執行 429，舊 allocation 不另加 Source 預算。
Full governance client 遇到 429 時依 `Retry-After` 秒數或 HTTP 日期等待；無效值使用既有指數
backoff，等待受 `FETCHER_MAX_RETRY_AFTER_SECONDS` 限制，嘗試次數受 `FETCHER_MAX_ATTEMPTS` 限制。

已安裝的 account ID 可沿用以保留 ledger，provider／credential fingerprint 與 ledger ID 對應
會以 SQLite transaction 持久化；完成首次 pin 後，舊證據檔移除或修改不改用新 ledger，
既有 debt/cooldown 不重設，也不輸出 credential。Twelve Data fingerprint 去除前後空白；
FinLab gateway、identity 與 installed allocation 比對均使用 `credential.strip()[:64]`，
符合 SDK `login(token)`／`get_token` 的前 64 字元選取，長 token 的後綴不另建帳號 ledger。
同一有效 credential 的空白格式變動不另建 ledger。Full／Pilot／historical Shioaji 的 key／secret 與 SDK 輸入一致去除前後空白；
只合併可由同一有效 credential 證明關聯的 pins：FinLab 包含明確提供與 SDK 等值 installed
token 的 raw、舊去空白與新 SDK 等值 fingerprints；未知或不同實際 key 不合併。Request debt
加總、cooldown 取最大、alias merge 冪等，原 ledger 與 audit 保留；不合併不明身份的 orphan。
首次升級尚未建立對照時，若舊 allocation 遺失、損壞或沒有可用 account ID，舊 schema 沒有
provider／credential 關聯可供還原。此例外以 credential fingerprint 建立新 ledger，只依平台
實際配額；原 ledger 用量與 cooldown 保留供稽核，不猜測、改標籤或合併匿名債務。
此操作接受舊用量與 cooldown 無法接續的風險；應在首次升級前保留可辨識的 allocation 與
governor 以維持完整接續。不同環境不設分配或預算，平台累計用量與服務端限流仍包含外部用量。
Governor 保留用量、pacing、cooldown；舊 capacity violation/repair 資料保留稽核，正常抓取不再
以它們作前提，也不刪除既有 debt。HTTP／SDK／IPC／R2 大小、格式與 provenance 驗證仍保留；
過大或不合法 response 不得變成成功 payload，但不新增永久 account capacity 封鎖。

Account acquisition protocol v2 對同一 governor 路徑／account 使用跨 process OS advisory lock
及 process thread mutex，從 durable preparation 持有至 operation 完成／durable finalization。
SQLite transaction 只做短暫 ledger 更新，不跨 network／SDK 等待；其他 account 可以同時執行。
HTTP 在 transport send boundary、FinLab 在 `data.get`、Shioaji 在 child `api.kbars` 記錄 actual start。
Twelve Data HTTP boundary 使用該 client 實際 config credential（保留 raw／normalized pin 合併），
即使環境變數指向另一個 key，account lock、request debt 與 cooldown 都跟隨實際送出的 key。
FinLab SDK 的 `login(token)` 只選取 process-global token，不算上游 acquisition；
正式路徑的 login 與 `data.get` 在 exec'd provider worker 內執行，一次授權涵蓋兩者。
所有 `FinLabSdkGateway` 以 process-wide SDK lock 保護最後授權、login、`data.get`、finalization、
完整回應計量、目標投影與 FileStorage 記憶體快取清理；Acquisition 仍在 `data.get` 後完成，
SDK lock 先於 account lock，不同 explicit tokens 在同 process 依序使用，拒絕授權會釋放 lock。
Account identity 在 login 前建立；SDK 修改的 token environment 在離開 scope 時還原，避免
installed allocation 的環境身份被改寫，成功、拒絕或 SDK error 都遵守此規則。`from_env`
及 central account identity 使用同一 SDK scope 的 installed environment 快照，跨 thread 等待
還原完成；同 thread authorization／data／finalization callback 讀取 scope 前的 token，
不會把暫時 SDK token 當作 installed allocation，也不重入 SDK 鎖。
同 thread 的 recursive gateway dispatch 拒絕，避免授權 callback 改寫外層 token 或造成 deadlock。
持有 account lock 的 callback 僅可唯讀取得全部相關 fingerprints 已 pin 至同一 target 的 identity；
需要建立／合併 pins 時明確拒絕，保留債務，離開 operation 後可正常合併。唯讀路徑不再取得
identity mutex，避免其他 process 正在等待該 account 的 merge 形成反向等待。外部 account lock
內的巢狀 SDK dispatch 拒絕；installed snapshot 只嘗試立即取得 SDK lock，busy 時明確拒絕。
Known requests 原子且冪等計入 actual UTC minute／day；正常拒絕或過期 grant 不增加 known request debt。
下一次開始不得早於前次 completion 加平台 interval，可能比 grant-based pacing 慢。
Cached FinLab bundle／SDK catalogue 不算行情 acquisition；TAIFEX cached report 不另預留 orphan quota。

Durable armed intent 必須在最後 Source authorization 前保存。Current control／credential／scope
授權回應的 `checked_at` 與 HTTP header 相符，server check age／整次 RTT 均小於 5 秒；
immutable header reception origin 在 response hooks／body decode 前取得，local dispatch／child GO
期限最多 50ms，並受 actual minute boundary 與 historical lease expiry 限制。等待、準備延遲、rollover
或 token 過期須重新檢查 quota／Source，最多三次後 fail closed；不能把 child GO 發送時間當成新 origin。
這是有界 fresh check，遠端 stop 與 provider invocation 不構成跨服務 atomic transaction。
Historical 每 item 使用 protected lease／cancellation probe，不延長 lease。

HTTP 429 的 headers 一到就 durable MAX 保存 cooldown，先於 body 讀取。Provider `Retry-After`
秒數／日期採最少 60 秒，最多 `FETCHER_PROVIDER_MAX_RETRY_AFTER_SECONDS`（預設 300，允許 60–86400）；
此 cap 在 armed intent 預先保存。Crash 尚無 actual-start 證據時，下一個 lock owner 將 armed 轉 unknown，
從 recovery 起至少等待該 cap，並在 armed→recovered 可能區間重疊的 quota windows 各保留一筆 liability。
不偽造精確 start，不重設歷史債務；已知 finalization 不重複計數。Persistence 失敗或無法終止的 SDK
child 保留 lock，直到 process 結束與後續 recovery，避免其他 consumer 越過未確認操作。

Shioaji SDK child 由明確 allowlisted environment 啟動，provider keys 僅由 private pipe 傳入；
argv／environment／READY-GO 不包含 Source credential、cloud 或其他 provider keys。
Parent 作 Source check，child 收到共享 lock FD、key-free GO 與最多 5 秒前的 `api.usage()` sample，
在 actual `kbars` 前再檢查 freshness／lease／minute／500 MiB。Parent SIGKILL 後 child 仍持有相同 lock FD，
SDK completion 的 quota／usage／cooldown 先 durable finalize，再交付結果。

SDK scalar observations 保存 sample／observation timestamps、bytes／limit／remaining。官方 counter
按交易日台北 08:00 reset，SDK 沒有可證明的 epoch 欄位；跨該可能 reset boundary 重新取樣，
不推定 UTC quota day 為 counter epoch。當前 fresh authoritative counter 達 500 MiB 或 unavailable／
inconsistent 時拒絕新 Kbars。Crash 後重新查 current counter；歷史 reset／unknown bytes 與 legacy
cumulative floors 只保留稽核，不加上 counter delta 重複計費，也不用 response／IPC 大小捏造 API byte bound。
部署與 rollback 的全 consumer 升級要求見 [deployment.md](deployment.md)。

首次 Full checkpoint bootstrap 僅傳入與 config 一致的 `APP_ENVIRONMENT`，在 local／staging／production
以離線 `--initialize-state` 建立 SQLite；不載入 Source/provider secrets。已有 checkpoint 跳過初始化。
Full profile 的此步驟僅套用 `full_market` consumer 與 `findb-fetch-full-market` CLI；
部署 inventory、checkpoint 與 rollback 規則集中在 [deployment.md](deployment.md)。
目前只接受統一版 manifest，不執行舊 accepted replay。停止錯誤、查詢不明、遺失 checkpoint 都會阻塞，
不可把缺少觀察當成成功或不存在。

所有 HTTP status 使用 bounded raw-body read 與 provenance／大小驗證；headers 的已知 429
先 durable MAX 保存共享 account cooldown，再讀 body。實際 invocation journal 決定 request debt，
不使用 detached Permit 預先預留行情請求；body／encoding／observer failure 仍保留已開始的 request
及 cooldown。Twelve Data daily／catalogue、官方 universe、TAIFEX fallback report 的非 200 回應
與中途 transport failure 都不得解析為成功 data／universe。201／206 同樣拒絕；錯誤 JSON 只供
遮蔽 secrets 的診斷。HTTP／IPC body 長度是傳輸驗證 evidence，不能代替 Shioaji SDK network bytes，
無 counter 時記 unknown／unavailable，不虛構 0 bytes 或退款。Prepared Source drain 保持可完成。

SDK bootstrap 也屬受保護 operation：dormant child 在 account lock／armed intent／FD ownership
與最後 current Source GO 之前，不建立 SDK instance、不 login、不查 lazy contracts。每次 construction、
login、TSE／OTC group、Kbars 分開授權／finalize；cached detached catalogue 不新增抓取。
Kbars 在首次 durable finalization 記錄實際 SDK 呼叫的成功／錯誤；已開始但失敗的 Kbars
仍計一次 request，後續錯誤處理不覆寫已 finalized 的結果，logout cleanup 維持 zero-unit。
所有入口的 account identity 取實際 SDK key 正規化後的 fingerprint，顯式 key B 不套用 environment
key A 的 pins、allocation、cooldown 或 login budget。已知 raw／normalized pins 只按可證明關聯合併，
含 `_`／`%`／backslash 的合法 account IDs 以 literal prefix 保留 allocation rows，不匹配其他 account。

Shioaji 官方最多 1000 login／day；此 runtime 使用**本地保守 rolling 86400 秒最多 1000 actual login
starts**，不假設官方 login reset epoch。`operation_kind=sdk_login` 的已知 start（包括 SDK 登入失敗）
各占一筆；crashed armed login 以可能區間重疊保留一筆 unknown liability；cancel／stale GO 不算
已知 login，其他 bootstrap／Kbars／cleanup 不占 login 次數。Operation ID 與 alias reassignment
使 finalize／recovery 冪等；不可重設已知歷史或把它變成所有 Full new acquisition 的日預算。
首次尚未登入時 api.usage 不可用，保存 `not_authenticated`，只允許 fresh Source 授權的 bootstrap；
login 後必須取得 consistent、fresh current bytes／connections，才可查 contracts／Kbars。

SDK official usage observations 保留 `connections`（舊資料為 null，不造 0）、sample timestamps、
bytes／limit／remaining。Authenticated connections 須為 1–5；已知 fresh connections≥5 拒絕新的
login／reconnect，既有有效 session=5 可沿用。首次／無 fresh observation 的平台拒絕仍記一筆
known login 並 bounded cleanup；不建立 person_id admission ledger 或捏造不同 keys 的 person_id 關聯。
Native 驗收須保存 official connections、登入拒絕與 cleanup evidence；目前仍待正式 deployment／native acceptance。

Logout 是既有資源 cleanup，Source stopped／revoked 不阻止關閉；它不發起 login、contract 或 Kbars。
Same-account lock／zero-unit cleanup intent 保護至 durable completion；crash 保留 cleanup unknown
cooldown，不捏造行情 request 或 SDK bytes。Parent timeout／EOF／SIGKILL 期間 child 保留共享 FD，
先 finalize／cleanup 再交 ACK／釋放；無法終止的 child 留 lock 供後續 recovery。

## TAIFEX historical 與日常 catchup

TAIFEX 指定歷史日期回補沿用獨立 historical control plane，操作見
[資料維護](data_maintenance.md#taifex-futures-eod-回補)。日常與 historical 共用日期 CSV 的
raw-first／契約／時段解析；historical 不受 daily control、activation first date 或今日 universe
限制，也不改寫 Full frozen plans；不必停止 TAIFEX control。部署要求兩環境各 provider
安裝 Full 與 historical 兩個 consumers；TAIFEX 共用既有公開資料 governor、Source key 與 Twelve Data image，無新增 provider secret。

## Provider work FIFO

唯一 provider-work 路徑將 Full daily work 拆成 dataset/date，與同 provider
historical／maintenance／retained-raw rerun 共用 PostgreSQL FIFO；整批日期由早到晚入列，沒有 daily
優先權。相同 provider 從官方 universe／catalogue acquisition 前就取得 shared whole-work flock 與
fenced DB owner，一直到所有 accepted runs terminal、完整 proof 與 round settlement committed 才釋放。
其他 providers 可並行。原 profile controls、first dates、frozen expected、calendar decisions 與 checkpoints 保留。

Full control desired running 時仍可建立 historical request；不需以停止 TAIFEX controls 當排他。
停止／cancel 阻止新 acquisition，prepared／accepted work 由原 coordinator 安全排空；quota／FIFO 等待不改
controls，也不回報假 provider failure／no_data。Retryable round 排空後以原 work／scope／budgets 附加到隊尾。
心跳遺失保留原 owner，必須重取同一 shared inode、確認 child 已停止後恢復；不會依 TTL 自動跳下一工作。

兩環境都已切換到 provider work，`PROVIDER_WORK_REQUIRED` 固定為 true、不能關閉，沒有其他相容路徑。
切換紀錄、migration／import、rollback 下限與 preflight 見
[deployment](deployment.md#provider-work-切換)，完整契約與新 provider 必要條件見
[provider work](../architecture/provider_work.md)。交付監控頁會顯示共用 provider queue、active work、順位、
wait reason、retry／heartbeat；GET projection 不更動 controls、lease 或 audit。

## FactSet 港股 EOD

HK 新抓取使用 FactSet 與 `market_eod.v2`；Twelve Data 只負責 US，HK 已永久退役。每日 HKEX 清單、申請時現行 cohort 回補、partial 結算、五日長缺漏與明確 provider
切換規則見 [FactSet 港股日行情](factset_hk.md)。安裝新版 image 不會自動啟動 controls 或切換來源。

### Catalog 排程與 frozen deadline

新 plan deadline 與沒有 plan 的 missing-delivery 判定共用 Backend `feed_schedule` resolver。
所有 feed 同一條規則：排程開始後 12 小時，以排程的時區計算（catalog 載入時檢查，不符即失敗）。
換算成台北時間（皆為交易日的次日）：

| Feed | 排程 | 完成期限（台北） |
| --- | --- | --- |
| TAIFEX `tw_futures_eod` | 台北 17:00 | 05:00 |
| FinLab `tw_equity_eod`、`tw_etf_eod` | 台北 18:00 | 06:00 |
| FinLab 四條盤中 | 台北 19:15 | 07:15 |
| FactSet `hk_equity_eod`、FMP `hk_index_eod` | 香港 20:00 | 08:00 |
| HKEX `hk_equity_official_eod` | 香港 21:05 | 09:05 |
| Twelve Data `us_equity_eod` | 紐約 17:00 | 18:00（美國夏令時間 17:00） |
| FMP `us_index_eod` | 台北 08:30（抓前一個紐約交易日） | 20:30 |

既有 plan 以儲存的 `deadline_at` 判定，不因後續
catalog 修改提早或延後告警。Bounded US 09:15 是另一政策，使用自己的 timezone／day offset。
Dashboard 依 market 分組，逐 feed 顯示交易所當地觸發時間；slot 僅保留交付契約相容用途。
DB 舊值修正與發布下限見 [PR5 遷移](deployment.md#provider-catalog-pr5-發布與遷移)。

## FMP 市場指數

FMP 提供 10 檔市場指數的日線，依發布時間分兩條 feed，各一個 dataset，`asset_class` 為 `index`，代號沿用
FMP（例如 `^GSPC`）。設計與實測見 [FMP 導入規劃](../dev/fmp-provider.md)。

| Dataset | 指數 | 排程 | 完成期限 | 交易日曆 |
| --- | --- | --- | --- | --- |
| `hk_index_eod` | `^HSI`、`^HSCE` | 香港時間 20:00，當天 | 隔天 08:00 HKT | HK |
| `us_index_eod` | `^GSPC`、`^NDX`、`^DJI`、`^IXIC`、`^RUT`、`^VIX`、`^SOX`、`^NYA` | 台北時間 08:30，前一個紐約日 | 同日 20:30（台北） | US |

- 兩條 feed 共用一個 control `full_market_fmp_v1`，控制列顯示最早的 feed（台北 08:30）。
- **必須有已發布的交易所日曆**（`calendar_required`）：缺日曆的日期以 `CALENDAR_UNPUBLISHED` 等待重試，
  不以週一至週五代替；同一天另一條 feed 照常規劃。兩環境的港股日曆涵蓋 1987–2026 年，2027 年起要先發布該年日曆。
- **名單**固定，由 Fetcher 以 FMP 的指數清單核對每檔都在、幣別相符後發布；任一檔不符就保留上一份名單。
  讀清單遇到 429 或格式錯的回應時，名單工作結算為重試（429 另寫入帳號冷卻），受每件工作的重試次數限制；
  401 等不可重試的 HTTP 錯誤結算為失敗。
- **每日抓取**：每檔指數呼叫一次 `historical-price-eod/full`。回應沒有目標日那一列時視為尚未發布，在發布
  重試窗口（4 小時）內重試；FMP 沒有「定案」標記，所以排程放在實測的定案時間之後，且不重抓覆寫。
- **成交量**照 FMP 原值寫入，0 就是 0。FMP 的指數成交量不是一致的數列（例如 `^RUT`、`^NYA` 多數日子與
  `^GSPC` 相同，`^HSCE` 2024-10-08 起單位改為千股），使用前請看導入規劃的說明。
- **歷史回補**以建立請求時的名單服務每一天（`current_release`），每天的請求數為港股 2、美股 8；本輪不執行。
  交易所日曆已補到 HK 1987、US 1953 年起（見「有效日曆與每日政策」），回補範圍仍受 production 最近 366 天、
  staging 31 天的限制。
- staging 驗收：只啟動 `fmp`，兩個 dataset 的首日都先設成單一日期，確認 Source → raw → queue → canonical →
  Serve 與 `expected = data + no_data + missing + blocked`，完成後停止 control。
