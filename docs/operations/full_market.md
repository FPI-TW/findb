# Pilot／Full market 共用執行流程

`FULL_MARKET_ENABLED=false` 是 local、staging、production 的預設值。Backend 與 Fetcher 必須使用
相同 `APP_ENVIRONMENT=local|staging|production` 與旗標。JSON `deployment_target` 仍是環境匹配契約。`FULL_MARKET_ENABLED` 從 AWS 部署控制設定載入，
只控制 Full market 新抓取資格；兩環境固定安裝相同 runtime，不以旗標選擇程式。
旗標為 true 不會啟動排程。Full market 的啟動資料條件只包含已發布 baseline。
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

## Profile 與計畫隔離

`findb-fetch-runtime --execution-profile pilot|full_market` 是兩環境共同入口。
Pilot 每條 feed 取 published universe 穩定排序的前兩個 canonical 商品；期貨以 product／contract 選擇，
保留入選商品 sessions。Daily plan 凍結選取結果，不因重試、補抓或 universe 更新改寫。
兩種 profile 各有 controls、first dates、plans、checkpoints、缺漏與告警；同環境共用 account governor。
只追補各自首次日期後的缺口，prepared delivery 在停止後仍可完成。

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
保留，plan／provider 狀態索引降低摘要查詢成本。兩種 profile 都適用，不自動切換 deployment flag。

本次 SQLite 變更僅新增索引與 reconciliation table，啟動時在原 schema lock 下建立，不需
PostgreSQL migration，也不重建 checkpoint、清除 body／receipt 或重設 governor。
部署後應在 controls stopped、必要交付完成時，核對 runtime CPU、owner、prepared／receipt、
normalization terminal 與 SDK actual-start 計數；本機測試不替代 production CPU 改善驗收。

### Shioaji 每日窗口與公平輪替

Pilot／Full daily 的股票與 ETF 共用 `daily_runtime_policies.shioaji`，唯一設定來源是
`backend/configs/execution_feeds.v1.json`，透過 execution-feeds exporter 產生三環境 Fetcher config。
新抓取窗口為 **Asia/Taipei 每日 18:00（含）至隔日 06:00（不含）**。這是 Owner 指定的
12 小時運行政策，與 feed 的 `completion_window_seconds`、每日 **23:00 交付期限**及完整性分開。
關窗不停止 control、不延後 deadline、不把 missing／blocked 改成成功，也不撤銷逾期告警。
人工 historical 與獨立 maintenance 不受此 daily 窗口限制，仍須遵守原授權、配額與 ownership。

每個 Shioaji daily turn 最多領取 **25 個成員**，或執行滿 **60 秒**後在下一個成員邊界讓出。
單一在途 SDK／Source 呼叫仍依既有 timeout 完成，因此 60 秒不是硬性完成 SLA；accepted
normalization 排空也可能延長交接。成功、實際失敗與 prepared replay 都算本輪成員，尚未 eligible
或租約未到的成員不消耗重試。直接模式先建立 sibling plans，再按日期與 feed 輪替；SQLite
`full_schedule` 保存成員游標及下一 feed，claim 與成員游標前移在同一 transaction，當機後會繞回
尚未完成、租約已到的成員。既有凍結成員、日期、mapping 與 plan identity 不改寫。

Queue 模式仍使用 provider FIFO，每輪先停止並回收 SDK child、close acquisition，排空原 prepared
交付及等待 accepted runs terminal，再將未完成工作以新 attempt 排到尾端。已確認接受的 body
不在每個批次重新送出；永久失敗的原 prepared bytes 保留為 manual evidence。單一 manual
成員不提前終結仍有 pending 成員的工作。一般 `batch_yield` 是正常輪替，attempt round 數不是
成員失敗次數；重試上限、逐 sequence receipts、已耗盡的 retries、帳號 pacing／cooldown 與
shared host inode 契約保留，不能藉換批次或重啟取得額外配額或無協調 SDK session。

`batch_yield` 的 queue retry 只延後 1 秒；`retry_at` 是最早 eligibility 時間，並非授權到期時間。
Source 在 fence、close、prepared／accepted terminal drain 與 proof 驗證後，以
`max(retry_at, Source 結算時間 + 1 秒)` 建立尾端 attempt。網路、provider lock 或 receipt 查詢
超過 1 秒時，合法的已過期 retry floor 不再產生 409；較晚的 cooldown／下次開窗時間仍保留。
這不允許新的窗口外 acquisition，也不清除 checkpoint、manual、失敗預算或帳號 cooldown。

結算拒絕的 HTTP 409 透過 `X-FinDB-Error-Code` 提供固定 reason，例如
`PROVIDER_WORK_FENCE_MISMATCH`、`PROVIDER_WORK_RECOVERY_REQUIRED`、
`PROVIDER_WORK_NOT_CLOSED`、`PROVIDER_WORK_INVALID_PROOF`、`PROVIDER_WORK_INVALID_RETRY`、
`PROVIDER_WORK_REPLAY_MISMATCH`；未分類者為 `PROVIDER_WORK_CONFLICT`。
Fetcher 只將允許清單中的 code 寫入 exception，不記錄任意 response detail。
只有既有 `PREPARED_DRAINING`／`DRAINING` 排空路徑會等待重試；其他 409 仍保留 owner recovery
blocker，不可為了讓 runtime 存活而放過 fence 或 proof 錯誤。
舊 Backend 沒有 reason header 時仍只顯示 HTTP status，故部署依原順序先 Backend 再 Fetcher。
此修正不需 migration、queue 重建或 retry reset；正式驗收仍須核對完整運行窗口的結算與重啟數。
[#367](https://github.com/FPI-TW/findb/issues/367) 的既有 production 日誌未保留拒絕細節，
已重現的 retry floor 競態不能直接當作所有歷史退出的已確認原因。

06:00 後不再啟動 daily SDK construction／login／Kbars；關窗前取得的 Source grant 也以
06:00 封頂。已在途回應可持久化，prepared bytes 可繼續交付與 normalization。剩餘成員於
下次 18:00 原位續跑，無須重新啟動 control，實際抓取仍取決於有效認證及共享配額。
尚未 dispatch 的配額等待與窗口等待不記為 `source_error`，也不增加失敗預算。

`--health`／cycle JSON 提供 `daily_runtime_window`，包含 `open`、`closes_at`、`next_open_at`；
未完成 plan 的 `plans` 包含本機 counts、failures 與 last_batch。這些是本機執行進度，完整性仍
以 Source canonical evidence 為準。Admin `/provider-work` 提供最近已結算批次的 processed、
elapsed_seconds、counts、failures 及 last_failure_code；執行中的批次資料尚未結算時不宣稱即時成員進度。
Dashboard 可區分等待同 provider、正常輪替、窗口外、共用配額等待、排空及最近實際失敗。

部署順序為 Backend（含 additive prepared lookup）後 Fetcher／Dashboard；先依既有程序 stop／drain，
保留 SQLite checkpoint 與 governor，不需 PostgreSQL migration。舊 frozen work 適用新窗口，
不重建其 scope。回退前應安全 stop／drain，保留新 SQLite table；舊程式不讀取該 table，
回退將失去有限批次及窗口行為。

本機 regression 使用可控時鐘／假 provider 驗證邊界與公平性，**尚未取得 staging 的真實吞吐量證據**。
部署驗收應保存每 feed 的 frozen member 數、批次 processed／elapsed_seconds、SDK login 次數、
`api.usage()` 前後 bytes、quota／cooldown 等待時間、最早 ETF turn、跨日 backlog 與原 deadline／alert。
由 actual-start／completion evidence 計算登入成本及有效抓取吞吐量，對照 12 小時窗口與每日配額。
若無法完成全市場，記錄不足與缺漏，不自動延長窗口、不增加平台配額，也不宣稱完整交付成功。

### Scheduler scope 與有效抓取範圍

Scheduler 的 `dataset_keys` 是控制／Source 認證的相容範圍，不代表 runtime 目前會抓取所有
列出的 datasets。判讀時分別核對：

| 範圍 | 權威來源 | 用途 |
| --- | --- | --- |
| 相容 scope | `scheduler_dataset`／scheduler `dataset_keys` | 驗證 control identity 與 Source credential；Twelve Data 僅 US，HK 已永久退役 |
| 安裝 feeds | `backend/configs/execution_feeds.v1.json` 及實際部署的 Fetcher config | 目前七條 feeds；Twelve Data 僅 US EOD，HK EOD 配置給 FactSet |
| 凍結 admission | `admitted_dataset_keys`／`admission_id` | Owner 最近一次啟動時凍結的可用 datasets；停止後投影仍可能保留 |
| 即時抓取授權 | Source `full-market/authorization?consumer=pilot\|full_market` 的 `acquisition_allowed`／`acquisition_blockers` | 驗證 control、profile flag、Source credential、baseline 與 provider cutover |

新 daily 入列使用安裝 feeds 與 admission 的交集；新抓取仍須通過即時授權、交易日期／窗口及平台配額。
FactSet 的安裝 feed 也不代表已獲准取得 HK 新資料，仍須發布 baseline 並明確 cutover。

`desired_state=running` 是 Owner 的持續意圖。統一 CLI 在 idle poll 與 cycle 完成後回報
`observed_state=stopped`，進入 cycle 時才回報 `running`。因此 running／stopped 組合須連同
`last_heartbeat_at`、`last_cycle_started_at`、`last_cycle_completed_at`、`last_error`、Source
授權及 provider-work owner／queue 核對；心跳新鮮且 cycle 持續完成可屬正常間歇。心跳過期、
cycle 沒有完成或授權拒絕時，保留相應證據再判定原因，不能只憑這一組狀態宣告故障或健康。

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

`expected = data + no_data + missing + blocked`。Data 必須有該 plan 所屬 run 的持久化 canonical 寫入證據，
Pilot 成功不能直接完成 Full plan。Minute 必須有完整 sequenced snapshot；DQ error、空批次、quota failure
不得算成功或 no_data。No_data 必須有 durable provider evidence。

## 啟動契約

Pilot／Full market 每個 feed 只要求同 provider／dataset 有 `published` official baseline，且 effective date
不晚於交易所當地今天。至少一個 feed 符合即可啟動，並只凍結符合條件的 feeds。日曆不必先設定。
Enrollment、installation receipt、readiness ACK／expiry、account／capacity proof、環境旗標與
registry 額外 metadata 不列為啟動資格 gate。舊 `full_market` metadata 若為字串或 list，
start 視為缺少 optional governance，重新建立 activation date；有效 contract declaration 與
既有 immutable first date 仍保留。未知 scheduler／scope 仍依 API definition validation
處理，Owner 操作權限與 CAS 保留。

Owner 透過 `PATCH /api/v1/admin/schedulers/{scheduler_key}`，傳入 `desired_state=running`
與 `expected_revision`。Backend 在同一 transaction 重查 baseline 與當時可用的 calendar metadata，凍結 ready datasets、
baseline IDs、可空的 calendar revision IDs 與首次日期，並寫入操作 audit；admission 不必綁定 enrollment。
新的 ready sibling 須停止後再啟動，重複 running PATCH 不改凍結範圍。
每 dataset＋profile 首次日期由交易所當地今天決定；migration 保留合法 legacy activation_date，malformed
legacy 日期在首次 start 時以 immutable first date 修正。停止、重啟與重新 enrollment 均不重設日期。舊 `/admin/feeds/{dataset}/activate`
與 `/deactivate` 在 Owner 認證後回傳 410；沒有 per-feed activation、manual readiness_approved 或五日門檻。

每次 provider call 前立即查 effective Source authorization。Full market 旗標關閉（不影響 Pilot）、Owner 停止、Source credential 撤銷／到期時，禁止新抓取與 plan。已保存 prepared payload／frozen plan 的 delivery、
normalization、canonical 與稽核可繼續；不重抓已 prepared 資料。關閉旗標由 ingest/all lifespan 或
下述管理命令停止 Full controls 並寫 audit；GET 投影永遠唯讀。重新開旗標後仍需 Owner 手動啟動。

## 有效日曆與每日政策

Backend 是政策權威。`runtime/configuration` 使用五個獨立開關：
`TWELVE_DATA_SKIP_NON_TRADING_DAYS`、`FINLAB_SKIP_NON_TRADING_DAYS`、
`SHIOAJI_SKIP_NON_TRADING_DAYS`、`TAIFEX_SKIP_NON_TRADING_DAYS`、`FACTSET_SKIP_NON_TRADING_DAYS`。
每個開關皆為 `true`／`false` 字串，五個 provider 預設皆為 `true`，兩環境欄位相同，經部署生效。
Fetcher 透過 Source 取得政策與每日決策，不保存另一份日曆或政策設定。預設略過非交易日；
明確設為 false 才在週末／休市照常執行。排程時間、交易所時區、Owner controls、平台配額、429 與既有重試規則仍有效。
不設連續十筆失敗中斷規則。

有效日曆逐日優先讀 published revision；缺年份或日期時依市場`weekend_days`補足，尚未建立market row的
內建市場採週一至週五開市／週六日休市，draft 不參與。
TW／US／HK／CN／TAIFEX 使用固定市場時區及正常收盤時間，published 提早收盤優先。
查詢不寫 DB、不自動發布；API 保留 `calendar_source=published|weekday_default` 與可空 revision。
缺日曆可預設，DB／認證／通訊故障則正常報錯。部署與 Owner start 不 seed 或檢查 calendar completeness。

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
provider work 建立時間加上 feed `completion_window_seconds` 作為 immutable publication deadline。
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

五個 provider 的 Pilot／Full daily canonical 回合，另在第一個需要新 provider I/O 的成員 claim
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

七個 Kbars 向量皆空記為 `empty_response`，原因維持 `unknown`；不能推定尚未發布、無交易或
entitlement 不足。缺欄、長度不一致、錯誤日期及其他永久格式錯誤仍走原本有界失敗流程。
正式 decoder 拒絕、尚未建立 snapshot 的已知格式錯誤亦記為 `invalid`，不占空回應／未知觀測
額度或觸發十五分鐘退避；僅保存 reviewed error code／reason，原 native 用量與一般失敗次數保留。
無明確格式證據的 transport／child-boundary 錯誤仍記為 `unknown`。
有效日期快取沿用原 bytes；無效舊快取保留原 row／checksum，負向 observation 不授權 replay。

Pilot／Full daily 每個原 work／provider symbol／日期最多三次空回應或未知派送觀測（包含首次），
重新觀測至少間隔十五分鐘，並受既有抓取窗口、owner、quota、unknown debt 與 cooldown 限制。
派送前的 journal reservation 在不確定邊界保守計數；可證明 Kbars 未派送的等待或 bootstrap
失敗記為 `predispatch`，不占三次觀測額度。SDK 建立、登入、contracts 載入失敗仍依八次一般
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
| `mapping_gap` | 凍結官方商品尚缺 provider mapping，須先取得 catalogue 證據 |
| `retry_exhausted` | 本地一般失敗額度耗盡，不等於八次 native 請求 |
| `prepared/normalizing` | 應查 Backend run 與序列收據；可能已完成，不能據此宣稱 queue 卡住 |

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
不補回已消耗 grant，也不重設原本八次失敗。Mapping 更正只有取得 canonical evidence 後才算 data。
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
  --config /app/configs/full_market.staging.v1.json \
  --state-path /var/lib/findb-pilot/staging/state.sqlite3 \
  --calendar-closure --work-id <work-id> --closure-proposal request.json > preview.json
```

核對 `apply_ready`、blockers、inventory、完整 request 與 sha256 後，才能另行核准套用：

```bash
findb-fetch-runtime --provider shioaji --execution-profile pilot \
  --config /app/configs/full_market.staging.v1.json \
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
| Twelve Data | 1597 API credits／分鐘 | Durable governor 依 1597／分鐘平均 pacing；HTTP 429 與 HTTP 200 payload code 429 均寫入 60 秒帳號 cooldown 後等待／重試。實際帳號用量以 readiness probe 的 `/api_usage` 人工核對；每日路徑尚未讀取 response credit headers |
| FinLab | 5000 MB／日 | 以 SDK API 回傳的 `quota`／`limit_size` 核對帳號累計用量，達限後等待平台重置 |
| Shioaji | 500 MB／日、行情查詢 50 次／10 秒 | 以 `api.usage()` 的 bytes／limit_bytes／remaining_bytes 核對；API 回報的 524288000 bytes 為 500 MiB；依 10 秒視窗控制查詢速率 |
| TAIFEX | OpenAPI 不需帳號，未公布明確 requests／bytes 數值上限 | 以 HTTP 429 作為觸及平台上限的訊號，依 `Retry-After` 或既有 backoff 等待／重試，不自行設定 provider 配額數值 |

Twelve Data credits 計算見[官方說明](https://support.twelvedata.com/en/articles/5615854-credits)；
Shioaji 限制見[官方使用限制](https://sinotrade.github.io/zh/tutor/limit/)；TAIFEX 平台與 endpoint
定義見[官方 OpenAPI](https://openapi.taifex.com.tw/)及[Swagger schema](https://openapi.taifex.com.tw/swagger.json)。
TAIFEX 429 的等待策略採用現行 runtime 的 rate-limit handling。

安裝 Full profile 時，保留 Pilot 與已有 historical workers，使用獨立 container／checkpoint names。
Installed consumers 的用量與 cooldown 由 durable governor 保存，process restart 不清除帳號已用量。
FinDB internal Source API 的 2400 RPM 為內部服務設定，與 provider 的平台配額分開。
Payload／R2 object／解壓縮／IPC bounds 屬於傳輸與資料處理保護，不列為額外 provider 配額證據。

Raw R2 以內容雜湊決定 immutable object key，使用 `If-None-Match: *` 建立，避免重試覆寫
已鎖定物件。409／412 不直接視為成功；必須在同一 bucket/key 讀回完整且有大小上限的 bytes，
核對長度、內容、content type 與 provider/dataset/symbol/SHA-256 metadata 後才可重用。
讀取失敗或內容不一致仍阻擋交付，查無物件的競態可重試；不移除 bucket lock 或刪除物件。
Twelve Data 的 catalogue 與實際日資料回應共用 Nasdaq MIC 規則：僅 `XNCM`、`XNGS`、
`XNMS` 對應官方 `XNAS`，其餘交易所與幣別仍須完全符合 frozen universe；原始回應不改寫。
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
| 同一 request identity 只有舊回應 | `durable_response` 找到無基準 key | 拋出一般錯誤 `ProviderWorkBasisMismatchError`；Full／Pilot 成員標為 `manual / price_basis_manual_review` 並回報 `source_error`，同計畫其他成員照常處理；historical item 以 `PREPARED_DELIVERY_MANUAL_REVIEW` 結束 |
| Prepared body 由舊回應產生 | body 的 `payload.batch.source_raw_sha256` 等於某筆舊回應原始 bytes 的 SHA-256 | 該 body 不送出；Full／Pilot 成員同上標為 manual；historical item 同上 |
| 排空 prepared ledger 時遇到上述 body | 同上，逐筆判定；只擋 Source 尚未接受的 body | 跳過該 body、其餘照常交付（已接受的舊 body 重播只取回原 run）；本 session 已記錄被擋 body 時，任何非 retry 結算（含 `completed`、`cancelled`、`failed`，以及
排空後重送的 `PREPARED_DRAINING` 結算）與 drain-only retry 都在送出前改為 `failed`，`failure_code=PRICE_BASIS_MANUAL_REVIEW`（已有 failure code 時保留），proof 帶 `manual_pending_prepared`、`price_basis_blocked` 與 `price_basis`；proof 仍有未回報的 `pending_source_errors` 時先維持 retry，回報後再轉換 |

判定逐筆進行：work 只是含有舊回應、但待送 body 不是由它產生（或待送集合為空）時，不阻擋。
Historical 的 scope stopped、drain-only 與 closed／draining 回合排空後若有被擋 body，一律以
`PREPARED_DELIVERY_MANUAL_REVIEW` 完成 item，completion 遇到 `PREPARED_DRAINING` 也改送同一
manual 結果，讓 item／request 與 work 一起進入終態。Full／Pilot drain-only 回合不會清除
（Source 只在所有 prepared 都被接受後才解除 drain-only），因此被擋 body 會使該回合的 retry
轉為 manual failed，尚未抓取的成員改由 historical 回補。

判定只依該 work 自己 journal 中的 `twelve_daily` 回應：沒有 journal 回應可對照的舊 checkpoint
body（例如 provider-work 之前匯入、且已有 prepared 的工作）無法辨識基準。2026-10-30 核對時
production 匯入的 13 筆 US daily 均未開始、沒有 prepared，目前無此類 body。
原 `work_response` bytes、prepared bytes、receipts 與 R2 raw 全部保留；第一次偵測時於
`work_response_observation` 複製一列 `origin=incompatible_basis:adjust-none-v1` 作為證據，並在
Fetcher `full_work_diagnostic` 記為 `price_basis_mismatch`。

Owner 的後續處理：historical request 因 failed date 結束後，為相同日期建立新 request（新 work／
journal 以 `adjust=none` 抓取）；Full／Pilot 計畫以 manual 成員結算 failed 後，以 historical request
回補該日期。取消 historical request 也會在原 owner 排空時走同一 manual 結算，不需人工改 DB。
不得刪除 journal 列、清除 prepared 或重設預算。

2026-10-30 唯讀核對：production journal 沒有 `twelve_daily` 快取，13 筆 queued US daily
尚未開始；staging 有 6 筆含舊快取的 Pilot daily 均為 completed 且 receipts 齊全，不會再被
claim。切換當下兩環境均無受影響工作。SSM：production `312dd583-1106-43a6-a026-95efdfbf3e60`／
`b76aca73-1d06-476d-bdeb-4b9c9ac341b3`，staging `ae01c182-e7ce-4157-9b95-9edcc361762b`／
`17c9ba56-09c5-42c5-954f-c3d4e904a37b`。

Full／Pilot daily 成員迴圈（`_deliver_plan`）中以下失敗會附加一列到 Fetcher SQLite
`full_work_diagnostic`（work key、分類、HTTP status、provider code、是否已派送、時間）：
Twelve Data provider 錯誤（上表分類）、已派送的 quota 失敗（`quota_blocked`）、價格基準不符
（`price_basis_mismatch`），以及其他 provider 的未分類例外（`exception:<類別名稱>`）。
Source 4xx 拒收、mapping gap、normalization 失敗、acquisition 停止／時間窗關閉、FinLab 尚未發布、
FactSet 專用交付路徑與 historical 不寫診斷，仍以既有 `full_work.reason`、Source outcome 與
historical failure code 追溯。診斷在 Source 回報之後以 best effort 寫入，寫入失敗不影響回報。
`--health` 輸出的 `source_error_diagnostics` 為未完成成員最新分類的計數。歷史紀錄只會新增，
不改寫 `full_work`、prepared bytes、receipts 或 failure budget。

Source 的 `POST /api/v1/source/universes` 使用 16 MiB nginx 與 Backend Content-Length 上限，
容納完整市場成員；其餘 Source 端點維持 1 MiB nginx 上限及原有
`SOURCE_MAX_PAYLOAD_BYTES` Backend 限制（預設 1,000,000 bytes）。nginx 同時限制 chunked
body 的實際大小。Universe 端點使用相同 IP allowlist、proxy 與 Backend
Source 認證，仍驗證 dataset scope、最多 30,000 個成員與 evidence；上限不是略過驗證的權限。
中途停機、撤銷某市場權限或任一回應失敗時，不提交部分 catalogue evidence。

這些程式保護不等於 native 驗收通過。驗收仍須包含真正 locked object 的重複寫入、七條 feeds
各自的非零 canonical／Serve 證據，以及 profile、plan、run 與 R2 bytes 的對應。
Shioaji 共用 runtime 明確套用既有 `SHIOAJI_SIMULATION=true` 驗證，再建立隔離 SDK；
Pilot／Full market 均使用資料擷取用 simulation，catalogue raw evidence 也記錄相同模式。
不合法設定在 child process 建立前拒絕，不能默默回退至 live-account mode。

啟動資格的程式修改位於 `refactor/full-market-baseline-calendar-start`；migration
`3553ed95cfbf` 允許 admission 的 legacy enrollment reference 為 null，保留既有歷史綁定。
所有環境的新抓取均不讀取 readiness／enrollment／expiry／capacity proof。Owner start 只依
published baseline；日曆缺少時依市場`weekend_days`預設，內建市場預設週六日休市；新抓取另遵守 running control、有效旗標、Source
及 provider API 認證與資料驗證。每次部署仍須確認 installed revision、環境與有效旗標；最新 staging 資料查核見上方現況。
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
報表尚未可用或目錄等待發布時，在各 profile checkpoint 保存日期、原因與一小時後的
重試時間，provider-work `retry_at` 亦使用相同剩餘延遲，不再每 60 秒重新 claim；CLI health 的
`date_prerequisites` 可查詢，其他已凍結日期繼續補抓。
交付截止後若同日 candidate 仍未發布，missing-delivery alert 以
`DATED_UNIVERSE_PENDING` 區別一般資料缺漏；Dashboard「Universe 候選」或 Admin
`GET /admin/universes?status=candidate` 可列出候選，`GET /admin/universes/{release_id}/diff`
可唯讀核對相對最近 published release 的成員差異。這些入口不會自動發布或改寫 plan。
HTTP、認證、配額、cooldown 與 R2 故障仍中止本輪，不冒充日期略過。
既有計畫、成員與 terminal work 不受新日期限制影響。此變更須先部署 Backend，再部署 Fetcher，
部署期間維持 controls stopped；SQLite 僅新增 prerequisite table，保留原有 rows 與 quota debt。

舊版若已用前一交易日 release 凍結 TAIFEX 計畫，不得把已到期且未出現在當日官方報表的契約
改記 `no_data`，也不得改寫原 plan、成功 run 或 canonical evidence。一次性處置須先停止對應
execution profile 的 TAIFEX control，取得當日官方歷史報表與 SHA-256，確認 provider owner 已釋放、
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

一般股票 universe 的自動發布仍使用絕對 20 筆及 2% 變更門檻。TAIFEX 到期換倉另使用
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

七個 feed 的 baseline 對應如下，每個 dataset 都需要自己的 immutable release：

| Provider | Dataset | Calendar market |
| --- | --- | --- |
| Twelve Data | `us_equity_eod` | US |
| FactSet | `hk_equity_eod` | HK |
| FinLab | `tw_equity_eod`、`tw_etf_eod` | TW |
| Shioaji | `tw_equity_minute`、`tw_etf_minute` | TW |
| TAIFEX | `tw_futures_eod` | TAIFEX |

## 設定與啟動

Backend 先 migration、registry 初始化及 Source scope 校準。Owner 發布 baseline 後才人工啟動。
Staging 只開 Pilot 驗證功能，確認正常後部署到 production 只開 Full；部署本身不 start controls。
Pilot 與 Full 的 daily 都只抓當日，回補天數由管理者建立回補時決定：Pilot 只補少量日期確認流程，
Full 才補完整範圍。Staging fetcher 主機記憶體小（見
[部署 OOM 紀錄](deployment.md#2026-10-22-staging-historical-finlab-oom-阻擋部署)），
不在 staging 執行 Full 或完整回補；FinLab 即使只取 Pilot 兩檔，worker 仍會載入整張 dataset。

```bash
cd fetcher
export APP_ENVIRONMENT=local FETCHER_ACCOUNT_STATE_PATH=.state/account/governor.sqlite3
uv run --env-file .env findb-fetch-runtime --execution-profile pilot --provider finlab \
  --config configs/full_market.local.v1.json --state-path .state/pilot/state.sqlite3 --sync-universes
uv run --env-file .env findb-fetch-runtime --execution-profile pilot --provider finlab \
  --config configs/full_market.local.v1.json --state-path .state/pilot/state.sqlite3 --run-forever
```

Full 使用 `--execution-profile full_market` 與獨立 checkpoint，並將 Backend／Fetcher 的
`FULL_MARKET_ENABLED` 同步設為 true。各 consumer 共用同一 governor 絕對路徑；不能把 checkpoint 與
account DB 指向同一檔。既有 governor 的 pins、debt、unknown liability、cooldown 不得刪除或重設。
維護同步以 Source maintenance 授權執行，不需要 enrollment；新抓取仍要求 published baseline 與 running controls。

AWS 路徑、首次切換、備份與回退下限見 [deployment.md](deployment.md)。CLI 的 `--check` 不呼叫 provider，
`--initialize-state` 只允許新 installation 建立 schema，既有 checkpoint 遺失必須阻塞。
`--require-stopped` 唯讀驗證正確 profile checkpoint 與 controls，不能藉部署修改 desired state。

## Dashboard 與 API

Overview／交付監控以 `profile=pilot|full_market` 分頁。Staging 預設 Pilot，production 預設 Full market。
各自呈現數量、缺口、告警、首次日期與啟動狀態；URL、篩選、刷新及 cache key 均帶 profile。
未指定 profile 的既有 plan API 維持 Full market 語意，新 runtime／Dashboard 必須明確傳入。
細節見 [dashboard.md](dashboard.md)。

### Freshness 的持久化完整度摘要

`market-freshness` 的 Pilot／Full market 完整度與最新 run metadata 讀自
`daily_delivery_plan_summary`，不在 GET 中載入歷史成員、canonical K 線或執行
`reconcile_plan`。每個 feed 的當期數量、最新完整日期與 run metadata 使用同一個 SQL
snapshot；交易日曆、首次日期與 deadline 仍依查詢時間計算，停止 scheduler 不會凍結
時間判斷。頁面更新不會觸發抓取或摘要寫入。

摘要以 immutable plan ID 隔離兩種 profile。建立 plan、接受新 run／分鐘重試、
normalization 完成或失敗、retry／retry exhausted、no-data／blocked outcome 變更時，
只刷新受影響的 plan，與原始資料／run／實際寫入證據在同一 transaction commit。
Plan row lock 串行化同 plan 的競爭寫入；失敗 rollback 不得先公布摘要。
FactSet 具合法 historical receipt 的修復會更新同 dataset／provider／date 的兩種 profile。
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

## 用量與傳輸保護

正常抓取使用 `platform_limits.py` 中的已確認限制。Twelve Data 按 1597 credits／分鐘 pacing；
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

Gateway 對 FinLab 1.5.7 原生 FileStorage 設定 `use_cache=False`，只釋放記憶體中的
DataFrame references，保留既有磁碟檔案與到期 metadata；不呼叫會刪檔的 `clear()`。
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
FinLab 1.5.7 gateway、identity 與 installed allocation 比對均使用 `credential.strip()[:64]`，
符合 native `LoginPanel.login`／`get_token` 的前 64 字元選取，長 token 的後綴不另建帳號 ledger。
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
FinLab 1.5.7 的 headless `login(token)` 只選取 process-global token，不算上游 acquisition；
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
raw-first／契約／時段解析；historical 不受 Full flag、activation first date 或今日 universe
限制，也不改寫 Pilot／Full frozen plans。執行前停止 TAIFEX 兩個 controls 並排空在途工作，
完成驗證後恢復日常 catchup。新版部署要求兩環境安裝十個 recurring 與五個 historical consumers；
TAIFEX 共用既有公開資料 governor、Source key 與 Twelve Data image，無新增 provider secret。

## Provider work FIFO（待環境啟用）

唯一 provider-work 路徑將 Pilot／Full daily work 拆成 dataset/date，與同 provider
historical／maintenance／retained-raw rerun 共用 PostgreSQL FIFO；整批日期由早到晚入列，沒有 daily
優先權。相同 provider 從官方 universe／catalogue acquisition 前就取得 shared whole-work flock 與
fenced DB owner，一直到所有 accepted runs terminal、完整 proof 與 round settlement committed 才釋放。
其他 providers 可並行。原 profile controls、first dates、frozen expected、calendar decisions 與 checkpoints 保留。

切換後可讓兩種 controls desired running 同時建立 historical request；不需以停止 TAIFEX controls 當排他。
停止／cancel 阻止新 acquisition，prepared／accepted work 由原 coordinator 安全排空；quota／FIFO 等待不改
controls，也不回報假 provider failure／no_data。Retryable round 排空後以原 work／scope／budgets 附加到隊尾。
心跳遺失保留原 owner，必須重取同一 shared inode、確認 child 已停止後恢復；不會依 TTL 自動跳下一工作。

目前已提交設定預設仍為 false，未執行 staging／production deployment 或 cutover；原 compatibility 操作規則
僅適用尚未啟用的環境。啟用、migration／import、rollback 與 preflight 見
[deployment](deployment.md#provider-work-切換)，完整契約與新 provider 必要條件見
[provider work](../architecture/provider_work.md)。交付監控頁會顯示共用 provider queue、active work、順位、
wait reason、retry／heartbeat；GET projection 不更動 controls、lease 或 audit。

## FactSet 港股 EOD

HK 新抓取使用 FactSet 與 `market_eod.v2`；Twelve Data 只負責 US，HK 已永久退役。每日 HKEX 清單、申請時現行 cohort 回補、partial 結算、五日長缺漏與明確 provider
切換規則見 [FactSet 港股日行情](factset_hk.md)。安裝新版 image 不會自動啟動 controls 或切換來源。
