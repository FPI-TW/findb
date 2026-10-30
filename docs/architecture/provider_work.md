# Provider work FIFO

所有五個 providers（Twelve Data、FinLab、Shioaji、TAIFEX、FactSet）只使用 provider-work-v1。
Pilot／Full daily、historical、maintenance 與 retained-raw rerun 共用同 provider 的 FIFO、owner fence
與 whole-work host lock。`PROVIDER_WORK_REQUIRED` 保留為部署相容性斷言：缺省為 `true`，
Backend／Fetcher 接受一般布林 true 寫法（不分大小寫的 `true`、`1`、`yes`、`on`、`t`、`y`）；
明確 false 拒絕停用協定，空字串／無效值則回報布林解析錯誤。部署檔案仍統一使用 `true`，
不再選擇另一套執行協定。

程式碼退役不代表環境已完成切換；環境升級仍須依
[deployment runbook](../operations/deployment.md#provider-work-切換) 完成 drain、備份、單 host／
shared inode preflight 與 Owner attestation。未 attested 的 Source claim 一律拒絕。
Production 已於 2026-10-29 完成 Owner attestation、兩端 enforcement 與 v0.1.12 部署，十五個
consumers 均正常且原 checkpoint／prepared／receipts／budgets 保留；切換驗收時 controls 全部停止。
後續 control 啟停與不可重試堆積的保留證據結案紀錄，見下述 production 維運文件。
此為部署與保留性驗證，native canonical／recovery 驗收仍未完成。原唯讀快照、未結束工作與本次
發布證據見 [production 維運](../operations/production.md)。

舊 acquiring CLI（provider-specific scheduler、Twelve Data one-shot、FinLab／Shioaji smoke、
Shioaji staging 與 TAIFEX pilot）只回傳 exit 2 的安全退役訊息，不載入 secrets、不建立 state、不抓取。
八個入口模組只保留 tombstone `main()` 與 `python -m` guard；舊 parser、CLI worker、排程與
獨立 TAIFEX pilot executor 已刪除，不以成功的私人 helper 測試保留已退役的執行路徑。
使用 `findb-fetch-runtime`、`findb-fetch-historical-backfill` 與 runtime 的 queued universe refresh。
共用 mapper／executor、舊 checkpoint parser、prepared bytes、receipts、raw lineage、operation budgets、
account pins／debt／cooldown 全部保留；不執行清檔、重新入列全部 backlog 或資料 migration。
`legacy_checkpoints.read_taifex_pilot_checkpoint()` 保留舊 TAIFEX meta／pilot_day 的 schema／identity
驗證與逐列唯讀檢查，使用既有檔案的 SQLite `mode=ro`；原 prepared、receipts、attempts 與 request
用量照原值讀取，沒有建立、修復、抓取或交付方法。其餘仍使用的 shared mapper／executor／
checkpoint parser 繼續放在各自模組，退役入口不再匯入它們。
原 TAIFEX 格式使用 rollback journal。Reader 在 SQLite connect 前拒絕 WAL 格式，避免
`mode=ro` 仍建立 sidecars；即使 sidecars 不存在也不忽略 WAL。WAL 檔須先經 SQLite
一致性備份取得 rollback 格式的離線副本，不以 `immutable=1` 跳過尚未 checkpoint 的證據。

Member executor 一開始取得 `require_owner()` 回傳的 fenced session，restore、authorize 與 reserve
直接使用同一 owner。Queue claim 無 runnable work 時仍可回傳 `work: null`，cycle／drain／
maintenance 保持正常 idle；它與已進入 member executor 後的必要 ownership 是不同邊界。
Historical `_run_once()` 同樣保存 `require_owner()` 回傳的 session，停止、drain-only、closed／
draining round 與例外處理全部使用該 owner，不重讀可為空的 `active()` 或採用無 owner fallback。
同一 owner 取得的 historical item 仍可能為空，此時正常返回 idle，不能跳過最初的 fence 驗證。

退役移除以下已重現的分歧：跨 request 的獨立 historical lease 可並行且 TTL 自動重派；
FinLab publication wait 存取不存在的 owner；TD／FinLab 無 durable historical retry authority；
TD／FinLab／Shioaji 只憑最後單筆 run 即完成；Source 暫時錯誤後繼續抓取；domain 與 FIFO 終態分離；
以及 FactSet false 模式僅健康回報卻無抓取。現在 completion 一律須通過原 work 的完整 manifest、
receipts／canonical proof；lease 欄位僅為同一 queue owner 的歷史投影，不能獨立授權或重派。

## Source 暫時中斷與恢復

現行 Pilot／Full／historical 的 `--run-forever` 在尚未取得工作時，對 Source transport、
HTTP 408／429／5xx（含部署期間觀察到的 521）採可中止的指數退避，從一秒增加至
poll interval 上限（historical 為 30 秒）。重試從完整 idle iteration 開始，重新 drain、poll
與取得即時授權，不使用中斷前快取的 desired state 或 grant。兩個 control clients 的單次
HTTP request 仍受 `max_attempts` 與 `max_retry_after_seconds` 限制；idle 的內層 backoff
與外層等待均可由 SIGTERM／SIGINT 中止。401／403、格式／identity／設定錯誤不進入
此暫時故障退避；one-shot／部署 probes 不啟用外層持續重試。

持有工作後，Source acquisition check 暫時失敗以 `ProviderWorkSourceUnavailableError`
穿過 SDK sanitizer；它仍繼承 `ProviderWorkRecoveryBlockedError(BaseException)`。
同一 session 不能繼續取得 provider 資料，仍由原流程停止／reap、隔離 shared host FD、
保留 DB owner，並以相同 inode 的 recovery 證據取得新 generation／token。一般 heartbeat、
錯誤 fence、prepared／settlement ACK 不確定的 recovery error 亦不會被 idle retry 吞掉。
停止訊號不取消 active work 的 bounded acceptance／terminal drain。

Canonical daily 成員在 Source check 前已 claim 本地 attempt。若此次 reservation 的
`dispatch_observations` 未增加、沒有 prepared body，且錯誤明確來自上述暫時 Source
acquisition check，僅以 `defer_wait(reason=source_unavailable)` 退還這次新增的 attempt、
清除本次 lease 並延後 60 秒；隨即重新拋出 recovery error，不繼續舊 owner。保護範圍包含
initial owner authorize、governed acquisition gate／Source authorization 及實際派送前的
owner recheck。一旦任一步已 started／possibly-started，原 failure budget、quota、unknown
liability、cooldown 均保留；先前 attempts、其他成員 prepared／receipts 不重設。這不改變
Shioaji 空回應採獨立持久觀測額度與人工恢復授權，見 [修復程序](../operations/full_market.md#shioaji-空-kbars-與凍結-mapping-修復)。

同一個明確的 Source authorization wait 也適用於 TD／FinLab historical operation、
universe maintenance、FactSet snapshot、TAIFEX historical operation，以及 Shioaji historical
sequence 的獨立帳本。各層以本次 dispatch observation 差值決定是否退還當次預留，再原樣
拋出 recovery error。Shioaji 追加 `waiting / SOURCE_WAIT` audit，與既有 `QUOTA_WAIT`
一樣排除本地 failure budget／rolling request allowance；不重分類舊 started／unknown／failed
紀錄，不修改既有 snapshot 或日曆／空回應決策。TAIFEX operation 亦保留 `source_wait`
audit 與至少 60 秒的下一次時間。任一前置 SDK login／catalogue 已派送即保留整次預留。

## 權威、範圍與排序

PostgreSQL `provider_work` 保存穩定工作，`provider_work_attempt` 以 sequence 保存每輪入列順序，
`provider_work_owner` 的 `(environment, provider)` primary key 是唯一 owner slot。
`provider_work_receipt` 將所有 Source runs 永久綁定原 work，包含同日多商品與 minute 各 sequence。
重複 enqueue 不改寫原 scope／順序；不同 profile、historical item 與 retained-raw rerun 決策分開保存。

Daily 的粒度是 dataset／trade_date／execution profile；historical 是原 request 中的單日 item。
同批日期由早到晚 enqueue；較早日期進入延遲 tail retry 後，後續 runnable 日期可以先執行，
不保證同 request 依日期完成。Historical claim 只投影已 fenced 的 queue owner，不另選日期或 lease。
FIFO 只比較 runnable attempts，不給 daily deadline 額外優先權。
未收盤、`not_before`、已停止且沒有待排空交付的 daily controls，或未開始且沒有待排空交付的
cancelled historical work 不阻擋其他 runnable work。先在 provider transaction 中選全域 runnable head，
再檢查 consumer 的 dataset scope、kind 與 profile；較窄的 Source key 不能跳過無權處理的 head，
也不能取得該工作的識別或內容。其他 provider 仍可獨立前進。
Daily 首次 enqueue 凍結 published calendar close；重複 enqueue 不因後續 calendar 編輯提早 acquisition。
不同 providers 可以並行；同 provider 的 universe／catalogue、抓取、raw persistence、prepared delivery、
normalization 與 completion proof 共用整個 owner，不能以一次 Source `202` 作為釋放條件。

Retryable round 必須先關閉 acquisition／acceptance、排空所有 accepted runs，再把下一 attempt
附加到 FIFO 尾端並設定 `not_before`。原 frozen manifest、prepared bytes、receipts 與 operation budgets
跨 queue retry 保留。等待 quota／queue 不是 provider failure；永久格式／DQ failure 保留 failed／manual。
Daily 七條 feeds 與 historical adapters 在觀察到第一個 retryable provider／Source 錯誤、transport
unknown 或 terminal polling timeout 後，即停止該輪的新 provider I/O 與新 prepared intent；不繼續後續
member／sequence。原 bytes／accepted receipts 先 reconcile／terminal drain，再安全 tail retry。
原 controls／request 仍允許抓取時，下一 attempt 才可接續同一 frozen scope 的剩餘 members。
Daily member 的一般例外會先持久保存 `source_error_pending`，再回報 Source `source_error`；
回報成功才套用原 local attempt ceiling。回報失敗／重啟時，先補報原 outcome，該輪不再抓取
其他 members，不增加或退還 acquisition attempts，也不改寫 prepared bytes／receipts。
此 continuation 即使在最後一次 acquisition attempt 或 drain-only round 仍須先完成。
Tail retry 的 settlement proof 同時保存 canonical `pending_source_errors` member keys，Source
驗證其屬於原 daily frozen manifest，將待補報資訊帶入下一 attempt。FIFO／唯讀 projection
將它視為待排空工作：即使 control 停止、daily window 關閉或帳號 cooldown 中，仍可取得
drain-only owner 補報，但 acquisition fence 繼續拒絕新抓取／intent。補報使用一般短 backoff，
不延至下一抓取窗口或 cooldown 結束；原帳號 cooldown 不清除。ACK 後下一次 settlement
不再攜帶此 continuation，恢復一般 FIFO gate，不能永久保留補報例外。
Maintenance explicit refresh 與 daily cycle 自動領取 refresh 共用同一 round executor：已知 catalogue／
official HTTP 503、transport 與 quota wait 會先停止 SDK／證明 reap、close、排空 accepted runs，再 tail
retry；格式、auth／scope 永久拒絕安全結束 failed/manual。只有 owner fence、失聯或無法證明 child
停止等不確定邊界才 recovery blocked。Maintenance 使用既有 Full work 的
`FULL_WORK_MAX_ATTEMPTS` 上限，budget 依 stable work 保存，跨 tail／重啟不重設；正常失敗不隔離
已安全停止的 host FD。已入列 maintenance 依 maintenance scope 授權，不以 daily desired running 作條件。
FinLab catalogue 的 typed operational failure 同樣安全 tail retry，reviewed config 錯誤 failed/manual；
daily 取得 owner 後的 Source plan／release／universe 查詢與規劃暫時失敗也安全 close／drain／retry，
不產生新 provider I/O、missing/no_data 或 control failure。
TAIFEX 首次日期在 plan／manifest 尚不存在時的 latest official 或 dated-product HTTP 暫時錯誤，
也先停止 I/O、安全 close／tail retry；predispatch quota 不耗 failure budget，not-published 仍保留原
`DATED_UNIVERSE_PENDING`。永久 HTTP auth／scope 或明確格式拒絕 failed/manual；未知 child／fence
保持 recovery blocked。dated universe 的 Admin publication／threshold exception 不因 retry 自動批准。
FinLab daily target-not-ready 與 FactSet daily `no_source_row` 在 immutable
`work.created_at + completion_window_seconds` 前屬 publication wait：安全 close／tail retry，最長
15 分鐘後重新取得來源，並退還 local work attempt reservation。FactSet 此時只保存 observation，
不保存可 replay 的空 snapshot；historical 及超過 deadline 的缺列仍依 terminal partial／manual
處理。每次 retry 在任何新 provider I/O 前，以 owner fence 重新讀取目前 published calendar；若 frozen planned decision
之後的新 published revision 將該日改為休市，保存 `CALENDAR_DECISION_REPAIR_REQUIRED` 並停止
重試，等待人工修復，絕不自動改寫 frozen decision。詳見 [Full market runbook](../operations/full_market.md#來源尚未發布的等待與休市判定)。
TAIFEX maintenance 官方 JSON 須為非空 row list，每列具有效且一致的 `Date` 字串，才可推導報表日期並
進行完整 report validation。空／錯誤形狀、缺欄位、錯誤日期或永久 body 格式錯誤會保留 safe failure
code 並安全結束 failed/manual、釋放 owner；空報表不構成 no-trade／no_data 證據，也不外洩原 exception details。
Twelve Data 實際 HTTP 429 與 HTTP 200／payload code 429 都是已派送的失敗；保留原 failure budget
與 account cooldown，跨 queue retry／重啟不能以 `rate_limited` outcome 字串退款。
已觀察的 native Retry-After／account cooldown 同步保存至原 work journal 的 `work_cooldown` checkpoint，
持有 owner fence 的 heartbeat 將剩餘期限提交到 PostgreSQL provider owner slot 的
`acquisition_not_before`，只可延長，釋放 owner 不清除；安全 settlement 仍保存原 attempt 的
`not_before`。目前單 host／provider account 採保守 environment/provider floor，涵蓋全部日期與 profile，
journal 不是另一套 lease。PG 在全域 FIFO 的 caller scope／kind 檢查前排除冷卻中的新 acquisition，
不先 claim 再持有 flock 等待 account governor。尚待交付原 prepared、accepted nonterminal 與 retained-raw
rerun 仍可依原 not_before 領取排空；冷卻 drain-only round 不新增 intent，未抓取的 frozen scope 安全 tail
retry，到期且原 request／control 仍合法時由新 attempt 續作，不永久鎖住 drain-only。
Crash 復原先驗證 owner fence，再以原 cooldown 阻止新 native I/O；prepared／retained-raw normalization
仍可排空。原 work 未到期時，其他已 runnable 的 prepared work 可領取 owner，不能繞過 account-wide
cooldown 發起新 native request。Executor 分別保存
`provider_dispatched`、`predispatch_quota_wait` 與 `prepared_delivery_wait`：已完成 provider acquisition 的
immutable prepared 重送只等待 Source，不增加 acquisition failure；純 governor predispatch wait 才退還
本輪未派送的 reservation。FinLab 共用相同 result metadata；Shioaji／TAIFEX 的明確 predispatch 證據規則不變。
Quota wait 必須有明確的 predispatch quota 證據才不扣 failure budget；SDK／HTTP 已派送或結果未知，
不退款。Shioaji public／legacy SDK boundary 保持安全的 `ShioajiSdkError`；只有已持有 queue work 的
consumer 才可取得明確 predispatch quota 的 typed wait，scope／auth／unknown 不會藉此退款。
TAIFEX 保存 operation reservation、wait 與 dispatch outcome history；Shioaji 的 audit numbering
與 failure budget 分開，明確 `QUOTA_WAIT` 不占 reviewed rolling request allowance，原 limiter／attempt
rows 仍保留。真正派送仍扣 allowance，真正 failure／unknown 仍受原 budget 限制。
尚未 accepted 的 original intent 可在 delivery／SDK 已停止、accepted runs 全 terminal 後隨安全 round
排到尾端，保留原 bytes 與 `not_before`，不因 Source 429／503 長時間獨占 provider。
Stopped daily 的既有 prepared 不會被 claim filter 擋住；stop／cancel 後的 attempt 保存
`drain_only=true`，只能交付原 body，不能藉重新 claim 建立 intent 或恢復新抓取。
原 prepared 全部已有 receipts 且 accepted runs 全 terminal 後，仍 stopped 的 daily work 保留 queued／
suspended 與原 descriptors，owner 已釋放且不再是 runnable drain head。合法 start 同 profile 後，
只有新的 daily attempt 可恢復原 frozen 未抓取 scope；已在執行的 drain round 不重新開啟 acquisition。
Historical cancellation 不適用此恢復路徑。
取消已在尾端等待的 retry 時，同樣保留原 prepared／accepted receipts 的 continuation、入列順序與
`not_before`；重做 cutover reconciliation 也不會把此 continuation 終結。`BACKFILL_EXPIRED` 的 failed 首項同樣保留
原 prepared／receipts、queued attempt、`not_before` 與 drain-only；這個例外不適用一般 manual failure。
Domain request／item 的 failed code、完成時間與 audit 不因 cutover 或排空改寫。尚無任何 retained 交付的
queued work 才可直接取消。
Historical request 失敗時同樣以 provider transaction 同步取消後續 domain items 與空 queued attempts；
較早 tail retry 已保存的交付維持 drain-only。下一個 daily／maintenance consumer 不需要歷史 consumer
先逐日清空 cancelled heads；claim 的防禦 reconciliation 仍保留所有有 prepared／receipts 的 continuation。
Controls／request 仍允許 acquisition 的正常 retry 可接續原 frozen scope 的未抓取 members，原 budgets 不重設。
未交付的可恢復 intent 阻擋 completed／cancelled 等 terminal settlement；永久拒絕或需要人工複查時，
明確保存 failed／manual failure code、原 bytes 與 `manual_pending_prepared` proof，不能默默丟棄。
`expected = data + no_data + missing + blocked`、first dates、calendar decisions、profile checkpoints 與
原 controls／admission audits 不變。Historical 可在 Pilot 與 Full desired running 時執行。
FactSet HK historical 在建立 request 時凍結現行 HKEX cohort；EOD v2 daily／historical 可依完整成員證據結算 partial，historical 繼續下一日期。原 work 的 completed 只採認自己的 receipts。Production Shioaji historical 支援股票／ETF minute，依 reviewed production manifest 凍結 50／3 成員及 sequences；完整成功 receipts 才可完成。TW ETF EOD historical 仍不支援。詳見 [FactSet runbook](../operations/factset_hk.md) 與 [資料維護](../operations/data_maintenance.md#shioaji-股票etf-分鐘回補)。

## 雙層排他、fencing 與復原

每個 consumer 先在同一環境 shared account bind mount 取得
`provider-work-{environment}-{provider}.lock` 的 whole-work OS flock，才可 claim PostgreSQL owner。
所有 provider-I/O subprocess 必須繼承同一 FD；Shioaji IPC child 的 `pass_fds` 包含此 FD。
FactSet MySQL、FinLab blocking SDK 與 Shioaji isolated session 在有 timeout／terminate／kill／reap 的獨立 process group 執行。
無法證明 child／descendant group 已停止時，保留 flock 並標示 recovery blocked；timeout 不是死亡證據。
FD 只 close，不對與 child 共用的 open file description 呼叫 `LOCK_UN`，因此 orphan child 仍會阻擋下一 owner。

`--initialize-state` 只建立／開啟 shared inode，再關閉此次獨立取得的 descriptor，
不取得 flock、不宣告 ownership，也不解除另一 profile／child 正持有的鎖。部署補建另一 profile 的
checkpoint 不必等待該 provider 空閒；原 inode、原工作鎖與帳號用量不變。真正 claim 與 cutover
preflight 仍必須成功取得同一 inode 的排他鎖；初始化成功不是 drain／attestation 證據。

Owner 每 20 秒 heartbeat，兩分鐘沒有心跳後需復原。TTL **不釋放 owner，也不自動選下一工作**。
重啟先取得相同 shared lock inode，旋轉 fencing generation／token 並恢復原 work。
Closed／draining round 可重送完全相同的 frozen manifest、確認／交付原 prepared、replay receipt 與
重試 proof settlement；這些操作不重新開啟 acquisition。Closed round 的未抓取 scope 先安全 tail retry，
再依原 scope 授權接續，不能在 closed recovery 直接呼叫 provider。
Source acquisition authorization、acceptance、completion 都檢查 owner fence；token 不出現在 Admin／Dashboard。
停止或取消不發起新 provider acquisition；停止／取消前已保存但尚未接受的 prepared 交付仍可排空，
所有已接受的 normalization 必須 terminal 後才能 settle。Historical request／item 保持 cancelled，
排空不會改寫成 completed 或捏造 no_data。

Prepared 使用兩階段邊界：在任何 adapter／profile 保存本地 immutable bytes **之前**，先以
`/prepare` 的 `intent` 階段持久化原始 body、SHA-256、固定 job／member 與 frozen scope；本地保存後
以 `confirm` 階段確認同一 descriptor。Intent、停止／取消與 owner close 共用 provider transaction lock。
因此 local save 後、confirm 前發生取消仍可確認並交付原 bytes；intent 已 commit 但 ACK 遺失或
local save crash 時，復原先從受保護 `/prepared` 讀回 server 原 body，不能重建 timestamp／idempotency
或重新抓取。Generation 旋轉仍綁定原 work；closed／draining 不得新增 intent，停止／取消後不能以
新 body 或新 member 冒充 prepared。既有 migration 前 prepared 可由原 work 的已接受 receipt 與
相同 payload digest 導入 descriptor，保留原 bytes、raw 與 receipt identity。

Source transport 使用 `X-FinDB-Work-Id`、`X-FinDB-Work-Generation`、`X-FinDB-Work-Token`、
`X-FinDB-Work-Member`。它們與 immutable contract body／payload hash／idempotency 分開。
新建 request 的 identity 可包含 immutable work ID；已保存的 prepared body 不會因 lease renewal 或
queue retry 被重寫。Fetcher shared journal 以全域唯一 work UUID 隔離 environment／provider 工作。
原 provider／profile SQLite checkpoint identity 仍保留；native response 在 raw upload／preparation 前先持久化，
Twelve Data／FinLab historical 也保存 frozen universe、prepared job 與全成員 receipts。

Acceptance 與 close／settle 取得同一 provider transaction advisory lock；validate fence、run association、
raw／run／job／outbox 的 commit 都在該鎖內，duplicate fast path 也不例外。
固定 lock order：provider FIFO → full-market environment／dataset／calendar／control → ingestion idempotency
→ delivery policy → run／job。Owner 的 open → closed → draining → settled 狀態限制新 acceptance，
所有已接受的 runs terminal 且完整 proof 成立後才能釋放。Exact receipt replay 只能回原 work，
不重設 terminal failed run。
等待 terminal drain 回傳 `PROVIDER_WORK_DRAINING`；consumer 保留 host FD 與 heartbeat，不能將其他
`409` 當作一般等待。未取得 durable settlement acknowledgement 就退出會保留 recovery blocker。
Source／Admin／bulk retained-raw rerun 會建立新的 audited run／work，
輪到 owner 後才建立 normalization job，不繞過 FIFO。
若 real normalization 因較新 canonical precedence 完成但未實際寫入全部 records，rerun work 明確
settle 為 failed／`NORMALIZATION_FAILED`，保留原 raw、新 run 的實際 counts、receipt 與 audit。
完全相同的 settlement ACK replay 可重送，不會留下 owner 或偽造 actual-write proof。

Source 明確拒絕的 HTTP 4xx（408／429 除外）屬永久交付錯誤；original body、delivery identity、SHA-256、
實際 HTTP status 與安全 error code 寫入 durable journal，manual proof 保留相同錯誤與
`manual_pending_prepared`。429／5xx 仍走安全 tail retry；HTTP 408 視同 transport timeout，
不作永久拒絕證據；deadline／transport unknown 先以原 identity reconcile，普通
RuntimeError 不會被一律視為永久拒絕。Owner fence 錯誤則停止並要求復原，不當作 payload manual failure。

Expand migration 匯入未完成 daily plans 時，以每個 minute snapshot group 的最新各 sequence 判定
完整性；只有全部 sequence 成功且連號完整才排除該 member。部分 group 的原 plan、raw、receipts、
checkpoint 與 sequence identity 均保留，復原 replay 原 receipts 後繼續原未交付 sequence。

Canonical `delivery_member_evidence` 與實際 canonical writes 同 transaction 保存，不依賴會被後續 upsert
替換的最新 canonical `run_id`。TAIFEX 仍需五份分產品 report、whole-day raw manifest 與 no-trade 驗證；
generic settle 不能代替 historical completion validator。

同日重疊範圍存在可達的 manual outcome：Full 原 prepared 因 503 tail retry 延後，期間 Pilot 已寫入
較新的 canonical 資料時，舊 Full payload 依既有 precedence 可能得到 `success_records=0`，因而無法成立
該 member 的 actual-write proof。此 work 保存 `MANUAL_INTERVENTION_REQUIRED`／round proof、原 bytes、
receipts 與 truthful expected／data／missing，所有 accepted runs terminal 後結束 failed round 並釋放 provider；
不無限 retry，也不以 superseded-success 或 no_data 偽造完成。Daily overlap regression 的兩商品案例為
expected=2、data=0、missing=2、no_data=blocked=0：首個 503 已停止第二 member 的新抓取，
該 member 沒有 prepared；第一 member 的原 bytes 被 precedence 跳過，保持明確 manual failure。

## API 與營運觀測

Admin queue projection 在 SQL 先排除 settled attempts，僅讀取未結案工作與 owner 的必要欄位。
不將完整 prepared body、manifest、scope、owner token 或每輪 proof 載入監控程序；
prepared／receipt 是否仍需排空由 SQL 計算布林摘要，最近 settled batch 與最近實際失敗保留原語意。
一般列表固定三次 SELECT，需要 historical／HK eligibility 時最多五次；查詢次數不隨工作數增加。
歷史 request／item 的 eligibility 批次查詢，daily window／controls、HK cutover、原 FIFO 順位、
cooldown、drain-only、report continuation 與 recovery 顯示不改變。此為唯讀投影，不取得 provider
lock、不結案或修改任何 work／attempt，不授予新 acquisition；正式執行仍由原 owner fence 判定。

Source `/provider-work` 提供 enqueue、claim、authorize、calendar-status、heartbeat、manifest、
prepare／prepared、close、settle 與 execute-rerun；historical claim 只是取得同一 owner 中的 item，
不再有獨立 lease authority。
Source queue claim 須已有 Owner cutover 保存的相同 shared inode identity，
單獨改 flag 無法開始 claim。所有 ingest／historical claim 缺少 work protocol 一律 fail closed。
Source `/provider-work/{work_id}/authorize` 在既有 provider transaction lock 下回傳原 work ID、provider、
generation／token、UTC-aware checked_at 與目前 owner expires_at；此 projection 不續約 owner。
Fetcher 嚴格驗證身份及時間欄位，缺少或 malformed 的證據 fail closed；heartbeat 仍以既有流程續約，
下個授權讀取最新 expiry。部署須先更新 Backend，舊 Fetcher 可忽略新增欄位；新 Fetcher 的 Backend
rollback 下限是提供此完整授權契約的版本。若須回復更早 Backend，先停止並回復 Fetcher 至相容版本，
保存原 owner、journal、prepared bytes、budget 與 cooldown；不得用 claim 時的舊 expiry 代替。
Admin `GET /provider-work` 是唯讀 projection，包含 active work、FIFO 順位、wait reason、retry time、round
與 heartbeat，並公開 `drain_only`／`prepared_drain` 等排空狀態。Dashboard「交付監控」共用同一 projection；初次 loading、背景 refresh 與頁首刷新規則相同。
Dashboard 的「僅排空原交付」獨立於等待原因顯示；未到 `not_before` 時仍同時顯示 cooldown／重試時間。

Shioaji 的實際 parent READY／GO handshake 在送出 SDK command 及 GO 前都檢查 whole-work fence、
shared FD 與 heartbeat failure；child 在 usage／SDK I/O 前驗證繼承 FD identity。一般 account governor
與 acquisition locks 繼續保留。legacy smoke CLI 一律 fail closed，不提供 FIFO 外的 SDK 入口。

## 新增 provider 的必要條件

新增 provider／scope 必須同時完成 capability／registry／contract／normalizer／Serve 驗收，且：

1. 接入 shared DB FIFO 與該 environment/provider 的 shared flock；不得另建 historical lease 或繞過 owner 的 catalogue path。
2. 所有 SDK、HTTP、subprocess 與 descendants 在實際 I/O 前重查 acquisition fence，並繼承 FD／提供可證明的 stop-reap 邊界。
3. 在抓取前凍結 expected scope；保存 native response、raw reference、immutable prepared bytes、全部 member／sequence receipts 與原 operation budgets。所有本地 prepared save 前必須完成上述 intent，並支援原 body 的失聯復原與停止／取消排空。
4. 使用相同 acceptance transaction lock／fence、durable actual-write evidence 與完整 scope settlement；不能以最後一張 receipt、空批次或 quota miss 當成功。
5. 補上 provider matrix、multiprocess orphan／kill-reap、auth-pause／acceptance-close race、partial sequence、stop/cancel、raw rerun 與 migration/cutover 測試。

Enabled historical claim 與 legacy claim 使用同一 provider-specific backfill expiry window；provider
transaction 先處理過期 request，最早未完成日期保留 `BACKFILL_EXPIRED` failed，其餘未完成日期 cancelled。
沒有交付的 queued work 立即終結，已保存 original descriptors／receipts 的日期只排空原 body，不恢復
request／item 狀態；TAIFEX 仍驗證 descriptor digest、原固定日期、五報表與 canonical evidence。GET projection
保持唯讀，不負責清理或延期。

Native replay cache 只接受適用原固定 target 的觀察。Twelve Data bounded daily response 須包含原日期窗，
TAIFEX dated product report 須通過原日期／product parser；不適用的 HTTP200 bytes 以
`work_response_observation` 保存原 encoded body、hash、origin 與觀察時間，仍由原 mapper 判定
not-published 或永久格式錯誤，不能轉成 no_data。下一合法 attempt 可對原 operation 重新 native fetch，
原 failure budget／account cooldown／scope 不重設。target validation hook 是純函式，不能隱含 provider I/O。
既有 valid legacy cache 驗證後 byte-for-byte 沿用；invalid legacy cache 原 row 不刪、不覆寫，後續有效
response 使用 validated namespace。已有 prepared／receipt 優先恢復，其 body／idempotency／raw 不改寫。
FinLab 缺少 target 時先保存原日期軸、reviewed symbols 與安全 scalar cells 的 table snapshot／digest，
再回報 target-not-ready；不保存 DataFrame attrs、SDK config 或任意物件字串。負向觀測不授權 replay，
重啟後可依原 budget 重抓，valid legacy table 仍可沿用；已有 prepared bytes 不改寫。
Shioaji invalid/date payload 保持既有永久 `KBARS_PAYLOAD` 規則，
七向量皆空則獨立分類 `empty_response / unknown`，daily 每 work／symbol／date 最多三次原生觀測、
間隔至少十五分鐘；原始 bytes、checksum 與未知派送 debt 保留，不構成 no_data。
`work_shioaji_probe` 保存派送前 reservation 與安全 SDK／向量／用量診斷，`work_shioaji_grant` 保存
initial 及核准額度，均與既有 local failure／account governor 分離。只有明確 predispatch wait
可釋放本輪 reservation；cache replay、重啟、換 attempt 不重置。未知派送從首次即套用上限與
退避；尚無空回應證據時不回報 empty_response。本地 continuation 先於 calendar／outcome RPC
保存，避免 Source calendar 失聯遺失續報或耗掉本輪 local claim。
Backend `shioaji_member_repair` 以 environment／profile／work／plan／member 綁定 Owner 核准、
原值摘要及證據；effective mapping 是附加覆蓋，不更新 universe／plan／manifest。Ingress、對帳與
Fetcher 使用同一有效 mapping；有任何 prepared／accepted canonical membership 的成員拒絕更正。
`shioaji_repair_ack` 保存 fenced 安裝證據；本地 `full_member_repair` 以 repair ID 冪等安裝，
`full_empty_outcome` 保存 Source outcome 的 ACK-loss continuation。Original terminal work 用新
attempt 恢復，舊 attempts、prepared bytes、receipts、quota、cooldown 不改寫。
不把其他格式錯誤變成無限 not-published retry。新 provider 必須在 replay authority 前驗證 target，並保留
negative observation 的 audit 與有界重試驗收。

所有 native dispatch 在 account governor／pacing／account authorization 等阻塞返回後，必須再確認
whole-work heartbeat、generation、scope；HTTP／FinLab 的 `confirm_start` 與 Shioaji child command／
READY→GO 都有此 gate，失效直接 recovery blocked，不能轉普通 retry/manual 或執行 SDK。
正常 request pacing 是完整 work 內的節流，仍可短暫等待，但等待後不得沿用舊 work grant。
派送順序是 account prepare／pacing 返回、fresh Source work-fence RPC、最後取得全新 Source account
grant，再以純本機檢查驗證 heartbeat、原 provider／work ID／generation／token 與 shared FD inode，
最後確認 account grant deadline 並 confirm／GO。最後 grant 返回後不再讀 SQLite 或呼叫 owner RPC，
避免一般網路延遲耗盡 50ms 本機派送窗口；HTTP、FactSet connect／SELECT、FinLab get 與
Shioaji READY→GO 共用此順序。Owner 遠端證據從 RPC 發送前的 monotonic 時點起最多五秒，
同時受 server checked_at 與 expires_at 限制，monotonic expiry cap 不因 wall clock 後退而延長。
遠端 stop／generation 撤銷以此有界證據生效；兩次 RPC 與 I/O 間仍有競態，不宣稱瞬時撤銷。
觀察到本機 heartbeat／fence／FD 失效則立即拒絕。Account grant 仍保留原 50ms、本機 minute rollover、
五秒 checked_at 與 lease expiry 下限。任何本機 fence／descriptor 失效立即停止；僅過期證據可在
原最多三次授權嘗試內重取（原設定一回仍為一回），不延長 deadline、不重新收費或重設 budget。

Governor 的 `full_account_cooldown` 與 `full_pacing` 在同一 SQLite transaction 保存 deadline/reason。
`unknown_recovery`、`unknown_dispatch`、native `rate_limited`、quota block 及只有 deadline 的 legacy
floor 都須在新 native 前同步原 journal／fenced Source heartbeat／PG eligibility，安全結束 round 排尾，
不能持 owner／flock 睡到冷卻到期。新舊 floor 只取更長者，owner release 不清掉 PG floor；若舊 writer
只延長 full_pacing 而缺 reason，reopen 保守記成 legacy，不猜成一般 pacing 或清除 debt。
正常 pacing 的明確 reason 不導致 SDK bootstrap 每一步 tail/restart。Account alias merge 也保留較長
floor/reason。這是 account governor 的持久證據，沒有第二個 queue 或 lease。

armed→unknown 與 unknown dispatch 保留原 intent、request liability、原 failure budget／audit。
復原冷卻中，本輪可證明未派送的新 reservation 仍依原 predispatch wait 規則處理，不退款原 unknown。
PG 已保存 floor 但 ACK 遺失時，恢復的 open owner 也只可 drain／close／tail；journal／Source publication
失聯保留 recovery blocked 與 host descriptor，不能假設已交棒。prepared siblings／retained-raw 仍可排空，
原未抓取 scope 到期且原 controls／request 合法時由後續 attempt 續作。

FinLab 與 FactSet 的 blocking 呼叫由 `supervised_call` 以 `subprocess` exec
`findb_fetcher.provider_worker`，不在多執行緒 parent 內 fork。Worker 只接受白名單操作
`finlab.get`／`factset.query`；credential 只經 pipe 傳遞，`pass_fds` 只含 pipe 與 whole-work FD，
environment 為明確最小集合。Worker 在每個 native I/O 前送出 READY；parent 取得 fresh Source
work-fence 授權、再取得 fresh Source account grant，純本機重驗 fence 後才回覆 monotonic deadline。
交給 child 的 deadline 取 account grant 與 owner proof／expiry 的最早期限；Child 在實際 I/O 前檢查
此期限，過期直接停止，不在 child 增設重試。Parent 每個 boundary 沿用原最多三次
授權限制；owner 或 account 證據持續過期時有界停止。

FinLab 1.5.7 的 `login(token)` 只在 worker 內設定 token、沒有網路 I/O，因此只在 login 與
`data.get` 前送出一次 READY；worker 另停用 SDK 在每個新 process 都會發出、沒有 timeout 的
PyPI 版本檢查。`data.get` 一律載入整張 dataset；worker 只回傳 reviewed symbols 欄位，
需要用量核對時另回傳完整表的 response bytes，父行程不再持有完整表。欄位或日期不合法時原表整份回傳，
由父行程產生與 in-process 路徑相同的型別化驗證錯誤。FactSet `connect` 是真實網路 I/O，connect 與 SELECT 前各送出一次 READY。
SELECT 前重新授權不重建 acquisition intent、不重設 started_at、不重複計費；已派送 connect 的原責任與
failure budget 不因 SELECT 被 stop／heartbeat／generation 拒絕而退款。拒絕時先完成整個 process group
terminate／kill／reap，再傳遞原 stop／recovery 邊界；無法證明停止仍保留 host FD。

歷史 expiry 的 provider-local cutoff 由 claim、work authorize、account authorize 與
new manifest／intent 共用；owner 持續 heartbeat 不會延長支援日期窗。授權只投影 owner expiry，
不續寫 item lease 或改變 domain status。到期後 authenticated completion／failure 在 provider
transaction 與 owner scope 驗證後套用原 expiry 決策，再排空原 prepared，保留 `BACKFILL_EXPIRED`。

Stop／quota wait 的 reservation 退款依整輪觀察判定，涵蓋該 member／job／maintenance 中先前
已完成的 SDK 步驟及 possibly-started dispatch，不只看最後一次拒絕。已派送的部分 acquisition
保留原 attempt budget；daily stop/start 仍會到達原 retry ceiling。純 predispatch wait、成功 historical
job 與已完整保存 immutable prepared 的 Source delivery wait 沿用既有規則。程序內 dispatch
observation 只用於當前 reservation，不取代 durable account intent／budget，也不清除跨重啟債務。

SDK process group 的 signal syscall 失敗不等於已停止，也不跳過 reap。Supervisor 仍嘗試
SIGTERM／join／SIGKILL／join，之後確認 leader 已回收且 group 不存在；僅 group probe 的
`ProcessLookupError` 可作不存在證據。若 child 仍活著或無法檢查 group，維持 recovery blocked
與 host FD，不能把未證明停止包成可重試 SDK 錯誤。這也涵蓋 macOS child 退出時的 signal 競態。

Daily control 在 claim 後停止時，也拒絕首次 manifest 寫入；已存在且完全相同的 frozen manifest
仍可重播，以支援原 prepared 排空。這個 gate 位於既有 provider transaction／owner fence 內，
Pilot 與 Full 都適用，不改寫已凍結的 scope 或 calendar-skip 決策。
