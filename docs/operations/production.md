# Production 環境維運

Production 固定使用 AWS account `289112218471`、region `ap-southeast-1`，state、KMS、ECR、S3、IAM、DB、host 與 runtime secrets 均與 staging 隔離。
兩環境共用部署契約，以 [部署文件](deployment.md) 為準；本文件保留 production 資源與待處理資料問題。
兩環境的統一版部署基準見 [部署文件](deployment.md)；native 資料與實際 rollback 演練仍見
[待完成項目](../dev/production-foundation-cutover-plan.md)，不得以部署健康驗收代替。

## Twelve Data 供應商工作佇列積壓（2026-10-29）

2026-10-29 唯讀查核 production DB、Fetcher checkpoint 與實際 Dashboard
projection，確認 Twelve Data 有 25 筆 Full market daily work，並非人工 historical backfill：

| Dataset | 筆數 | 凍結資料日期 |
| --- | --- | --- |
| `us_equity_eod` | 13 | 2026-10-12 至 2026-10-24 |
| `hk_equity_eod` | 12 | 2026-10-13 至 2026-10-24 |

全部 work 都帶 `scope.legacy_id`，符合 migration `b53da90e7f21` 匯入未完成 daily plan 的規則；
各自只有第一輪 queued attempt，`started_at` 與 `failure_code` 均為空。Dashboard projection 的
25 筆 `wait_reason` 全為 `suspended`，沒有 active owner；`recovery_reason`、帳號 cooldown 亦為空，
查核當下 daily window 為 open。因此目前等待原因是排程停止，並非正在重試或等待失聯 owner。

Backend 與 Twelve Data 的 Pilot／Full／historical 三個 consumers 實際均設定
`PROVIDER_WORK_REQUIRED=false`，仍走 compatibility runtime，不會消費新 FIFO；三個容器雖有
protocol v1 label 且正常 running，不能據此認定已完成 cutover。Pilot／Full controls 的
desired／observed 均 stopped，DB 未查到 cutover audit。Expand migration 建立佇列不等於啟用 consumer，
單純重啟容器不會讓這 25 筆工作執行。

原 Full checkpoint 另保存 98,374 個 `pending / source_error`、12,810 個
`manual / mapping_gap` 與 980 個 `manual / retry_exhausted` 成員。這是舊執行結果，不能與 FIFO
尚未開始的 attempt 混為一談；`source_error` 是多類例外共用分類，未保留足以確認特定 HTTP code、
provider entitlement 或連線故障的診斷，不能直接歸因為 429、金鑰失效或無資料。

後續處理須先核對原 prepared／receipts／terminal 證據，再依
[provider-work 切換程序](deployment.md#provider-work-切換) 完成 production cutover 與明確的 control 啟用。
現行 HK feed 已由 FactSet 負責，舊 Twelve Data HK work 須走退役 feed 的保留證據／人工處理流程，
不能整批重試、刪除 checkpoint 或改成 completed／no_data。本次僅診斷，未變更 production
旗標、controls、工作狀態或觸發 provider I/O。

SSM 證據：DB／旗標 `7c8bc889-fa9c-43d9-bacb-b8d8b288d847`、SQLite／consumers
`b398ef09-5a3d-4f54-b529-0dfade9069e6`、日期與 Dashboard projection
`088522b4-2b34-4e5b-aff0-e1f54ee78628`。

## 退役後不可重試堆積的結案查核（2026-10-29）

v0.1.12 部署後再次以 Admin projection、Source DB read-only transaction 與原 SQLite
`mode=ro` 查核全部五個 providers。15 個實際 consumers 均使用統一 daily／historical 入口、
`PROVIDER_WORK_REQUIRED=true` 與本次 accepted image；舊 acquiring CLI 已無執行實作。
Shioaji 與 TAIFEX Full controls 已另行啟動，這不是切換驗收時的停止快照。

| 分類 | 數量 | 處理界線 |
| --- | --- | --- |
| FinLab 舊 daily 全成員 complete／manual | 22 | 原成員已無 pending／prepared；可依原預算與交付證據結案 failed |
| Twelve Data 舊 HK daily | 12 | 現行 HK feed 屬 FactSet；只結案退役 work，不重新抓取、不改成 no_data |
| FinLab 仍可重試 daily | 8 | 保留 queued／suspended 與剩餘預算 |
| Twelve Data US 仍可重試 daily | 13 | 保留 queued／suspended 與剩餘預算 |
| Shioaji、TAIFEX、FactSet | 0 筆符合本批清理 | 保留 active／retryable work 與既有 terminal history |

本批 34 筆候選的完整 frozen member 集合及 15,497 筆原始 sequence receipts 已逐筆比對。
Local prepared bytes 的原 SHA 保留；Source raw payload 的 SHA 必須與原 receipt 一致。
跨兩端 payload 比對只將契約預設的 nullable 欄位省略與 `null` 視為等價，避免將 Source
`model_dump` 補上的 null 誤判為內容不同；所有非 null 值、run ID、idempotency key 都須一致。
此查核不更改原 bytes、raw 或 receipts。

清理僅允許將上述 imported daily 的未開始第一輪 queued projection 標記 failed／settled，
逐筆追加具名 AWS IAM operator audit。執行前須持有原 FinLab／Twelve Data flock inodes，
在同一 transaction 依 provider 排序取得 advisory locks，確認兩個 provider 的 controls 仍 stopped、
owner idle、無未回報 source error、未交付 prepared 或 nonterminal run。任一不一致整批 rollback。
Checkpoint、prepared、receipts、raw、frozen plans／members、原預算及其它工作均保留；
stable-key enqueue 再執行不能復活已結案 work。這不是刪除錯誤歷史或重設失敗額度。

Production 已於 2026-10-29 13:09 UTC 完成結案與複查：34 筆 work 為 failed、attempt 為
settled，同一 transaction 追加 34 筆 `reconcile_queue` operator audits。FinLab 使用
`LEGACY_RETRY_EXHAUSTED`，退役 TD HK 使用 `RETIRED_FEED_MANUAL_REVIEW`。
交易前後 13 組 Source DB 保留性雜湊一致；持鎖期間再次讀取原 SQLite，34 筆候選的
plan／member／body／receipt／attempt rows 與原 journal 均未變。兩個原 inode 的 FD 已正常釋放。

執行後 Admin API 顯示 27 筆待處理工作：FinLab 8、Twelve Data US 13、Shioaji 6。
前兩者仍為 queued／suspended；Shioaji 的可執行與 batch-yield 工作照常保留。已結案的 34 個
IDs 全部離開 projection，未清除可重試項目。TAIFEX／FactSet 沒有本批清理項目。

證據保存在兩個 hosts 的 root-only
`/var/backups/findb-provider-work-cleanup/production-20261029T130718Z`：Fetcher 的 fresh lock proof
與保留性結果、Backend 的原 projection 備份、實際程式及 transaction 結果。
SSM：持鎖 `f1b8c5d9-ff4f-4548-a9bd-4aed7d7f2287`、鎖驗證
`d0222e46-2f19-46cc-afd5-d0e0581a5098`、結案
`a54c93a5-c9da-453b-a6ba-0f05c919ff64`、釋放
`b69c8a19-6113-4276-b064-f3cf2a6c0202`，均 Success。
驗證包含 15 項隔離 PostgreSQL 維護 transaction 測試、125 項退役／原預算／錯誤回報相關測試，
以及全 repo `pnpm check`；獨立實作審查 CLEAR，另重跑 15＋46＋39 項測試通過。

歷史 `source_error` 未留下精確 HTTP／SDK 例外，仍不能聲稱已找出每筆來源錯誤的根因，
也不能保證供應商、網路或 mapping 錯誤不再發生；已封住的是退役的雙執行路徑與無 owner
的抓取／接受入口。Mapping gaps 保留人工處理，不因清理改為成功。

## 供應商工作佇列讀取逾時（2026-10-30，修正待部署）

台北時間 2026-10-30 00:32 起的唯讀調查，已重現 Dashboard 的
`供應商佇列更新失敗：FinDB API request timed out`。直接以合法 Admin credential 呼叫
`GET /api/v1/admin/provider-work` 可回傳 HTTP 200、27 筆工作，實測耗時 10.612 秒；
相同回應通過 Dashboard 的 `providerWorkSchema`。Dashboard `fetchTarget` 的
`UPSTREAM_TIMEOUT_MS=10_000` 先取消請求，因此即使 Backend 最後完成，面板仍顯示失敗。

已部署 v0.1.12 的根因在 `backend/app/services/provider_work.py:projection()`：初始 ORM query 將
`ProviderWork` 全欄位與**所有** `ProviderWorkAttempt` join，取得結果後才在 Python 跳過
settled attempts。每輪 join 都重新傳輸／解碼 work 的完整 `prepared_deliveries`；它包含原始
prepared body，而監控列表只需要狀態摘要。當時 Shioaji 已有 183 輪 settled attempts，
current attempts 為第 26～27 輪；六筆工作約 40,436,313 bytes 的 prepared JSON 在該 join
中被放大到約 1,077,959,230 bytes。頁面只回傳約 18.7 KB，並不代表查詢本身很小。

獨立唯讀程序執行 production 原函式，得到 27 筆、耗時 13.655 秒、89 個 SQL calls、
peak RSS 1,106,884 KiB。其中初始 work／attempt join 耗時 13.039 秒；27 次 receipts 查詢
合計 0.124 秒，54 次 previous-attempt 查詢合計 0.061 秒。相同 read-only snapshot 的
查詢對照：在 SQL 先排除 settled、仍選取完整 ORM 的初始查詢為 0.509 秒；只選取未結案
work／attempt metadata 的初始查詢為 0.006 秒。這些是初始查詢對照，尚非修正後完整 endpoint
的效能驗收。

Backend／Dashboard／nginx 均 healthy、零重啟、無 OOM；DB 基本彙總查詢為 2～7 ms，
Shioaji owner heartbeat 正常。前次結案的 22 筆 FinLab、12 筆 TD HK 仍為 failed，
其餘佇列／prepared／receipts 持續保留；本次未清理資料、停止 runtime、調整 timeout 或部署。
修正方向為 SQL 先篩掉 settled、監控只讀摘要／必要欄位，並批次讀取 receipts 與最近 attempt；
驗證需包含大量歷史 rounds、minute prepared bytes 與十秒以內的完整 Dashboard 讀取。
不得以刪除歷史 attempts／prepared 或單純提高 timeout 取代查詢修正。

Repository 已將 projection 改為在 SQL 排除 settled，僅選取必要 metadata；prepared 有無、
未接受交付與 nonterminal receipt 由 SQL 計算布林摘要，最近批次與失敗代碼以同一查詢的
相關子查詢取得。Historical eligibility 批次讀取 request／item，HK cutover 沿用正式 eligibility
判定。一般 projection 為固定三次 SELECT；同時需要 historical／HK eligibility 時最多五次，
不隨工作數或歷史輪次新增 SQL round trips。原工作、receipts、prepared bytes 與 budgets 不改寫。

候選函式於 production host 的**獨立程序**、READ ONLY transaction 中量測，未替換 module、
未更新服務或部署：當下 26 筆未結案工作全部回傳，完整 projection 0.240 秒、三次 SELECT、
peak RSS 81,504 KiB、回應約 19.3 KB。當時原服務仍使用舊版，這不是已部署 endpoint 的驗收。
本機六筆 minute fixture 保存 42,062,340 bytes prepared 與各 28 輪歷史，projection 為三次 SELECT、
0.028 秒；測試以禁止選取完整 body／manifest／proof／token 與固定 SQL 次數防回歸，
不以依賴機器負載的毫秒門檻判定正確性。合併後仍須按部署流程驗證正式 Dashboard 十秒內讀取。

另曾觀察公網 `/health` 單次 Cloudflare 525、耗時 43.787 秒。Origin certificate 有效至
2041-09-13，當下沒有足夠證據定位該次 TLS handshake failure；不能將它與已獨立重現的
佇列查詢膨脹混為同一根因。

SSM：健康與規模 `7fc31a56-dea4-4283-b622-d2d78ec663f1`、實際 profiler 與對照
`88bf68d2-c39a-4095-b0e4-98d0ef906009`。所有診斷為 read-only，沒有改寫 production 程式或資料。
候選 projection 唯讀量測：`475de304-2002-4a81-843e-8effec56bb30`。

## 五 provider 舊流程退役評估（2026-10-29）

同日 03:53 UTC 全 provider 唯讀查核：Backend 與全部 15 個 consumers 的
`PROVIDER_WORK_REQUIRED` 仍為 false；Twelve Data 25 筆與 FinLab 30 筆 daily work 均 suspended。
Shioaji Full 的 desired／observed 仍 running，其餘九個現行 Pilot／Full controls 均 stopped。
所有既有 provider owner slots 均未 attested，未查到 cutover audit；零 nonterminal ingestion run
只代表當下狀態，不能當作停止新 acquisition 後的 drain 證據。

全 provider 程式碼、production 函式雜湊與隔離重現結果支持退役舊 acquiring／無 work fence
acceptance，但必須先完成五 provider cutover、原交付保留與環境驗收。各 provider 差異與已實作的
退役邊界見 [provider-work 架構](../architecture/provider_work.md)。Repository 已移除舊執行入口，
當時 production cutover、native 驗收與部署尚未執行；沒有停止 Shioaji、改 flag 或清理 queue。
後續已授權的切換紀錄見下節；native 資料驗收仍獨立列管。

## Provider-work 切換與舊流程退役發布（2026-10-29）

本次目標版本為 staging 已 accepted 的 `f514cf32185d8d3b76784c062ec171a37665a381`
（PR #375），使用 annotated immutable tags `findb-v0.1.12`、`fetcher-v0.1.12`。
兩個 tags 的物件分別為 `0e4ab79fa02a178e9a04ce9f11ee108d9fa74678`、
`ce10157602bd0bcf0d153f1501ad57293cc750bf`；發布依既有 Backend-first promotion，沿用
staging digests，不重建 images。Backend／Fetcher 的 CI、acceptance、activation 均已成功。

停機前再次確認十個 Pilot／Full controls 的 desired／observed 全為 stopped、25 個 historical
requests 全為 terminal、沒有 running historical item 或 nonterminal ingestion run。Full／Pilot／
governor 沒有 body 已保存但 receipt 缺失的 work；13 份 Shioaji historical checkpoint 的
prepared-without-receipt、accepted-nonterminal 均為零。TAIFEX historical 兩筆 work 均保留原
prepared／receipt。原 failed Shioaji request 的一筆 `raw_uploading` 仍原樣保留，不重試或猜測交付。

全部十五個 consumers graceful stop，均 exit 0、無 OOM／Docker error。Fetcher host
`i-0e94c09b2af831808` 的 `/var/backups/findb-provider-work-cutover/production-20261029T1134Z`
保存完整原 container inspect、59 個持久化檔案、17 個 SQLite 的逐表 rows／SHA256 與
`state.tar`（486,963,200 bytes，SHA256
`a8223f4674605c1777aba769a30aa3426308be5ebb9d58063fce39b0b72c29c8`）。原 Full、Pilot、
governor、五 provider historical 目錄及 sidecars、prepared、receipts、account pins／budget／
cooldown 全部保留。RDS snapshot `findb-provider-work-production-20261029-1134` 已 available；
加密 EBS snapshot `snap-0e76651e38bfe7101` 已 completed。

離線以原 accepted protocol-v1 images 初始化五個共享鎖，再按原 image／argv／HostConfig／bind
mounts 重建 enforced consumers，僅將 `PROVIDER_WORK_REQUIRED` 設為 true；此階段不啟動。
原 17 個 SQLite 的所有既有 tables／rows 與備份指紋完全一致。原版 preflight 對真實 container
inspection、唯一 Fetcher host 與五個 flock 驗證成功；11:44:03 UTC 產生 proof，隨即以具名
Owner machine key 經正式 cutover API 提交，回覆成功。具名 audit actor 為
`Production_admin_owner_key`（machine），event ID `01a18779-b6aa-7b61-be03-6c5148015bfc`，
保存時間 11:44:23 UTC；Inventory SHA256 為
`ec58b2d89525ec41d2634a1a5e320a3c66b5a12c488620f6bb37676336679f79`。

| Provider | Attested shared lock identity |
| --- | --- |
| Twelve Data | `production:66305:302843` |
| FinLab | `production:66305:302860` |
| Shioaji | `production:66305:302872` |
| TAIFEX | `production:66305:302880` |
| FactSet | `production:66305:302881` |

兩組 deployment SSM parameter 均由 version 1 更新為 version 2，唯一變更為
`PROVIDER_WORK_REQUIRED=true`。兩組 runtime configuration 同步新增此值，其餘欄位不變；
Backend／Fetcher 新 versions 分別為 `8da4ea8d-8891-4934-8bbc-ec2e99105453`、
`b752d41b-d3d0-44be-b587-a961fca19c57`。

新建但未啟動的容器實際為 `created`，cutover helper 接受此準備狀態，installed-host guard
則要求 `running` 或 graceful `exited`。因此須在 Backend promotion 成功後、controls 仍停止且沒有
可 acquisition work／maintenance／rerun 時，啟動這批 enforced accepted consumers，確認真實
running／零重啟並執行完整 installed-host guard，再 dispatch Fetcher promotion；不偽造 Docker
State、不跳過 guard。本次這項啟動檢查已通過：15 個 consumers 均真實 running、零重啟，完整
guard 回報 `fetcher_host_eligibility=running`，五組 inode 與 Owner attestation 相同。

Backend 的 schema-v3 production accepted bundle 為
`findb/production/accepted/f514cf32185d8d3b76784c062ec171a37665a381/28244fc878a64439f1c879d27bf7107b019d11f228f8ad69897833467381526f.tar`。
Fetcher accepted bundle 為
`fetcher/production/accepted/f514cf32185d8d3b76784c062ec171a37665a381/3971df592c38b2465f7abed0679442c5f80cf5bb63520b10da81a2c722db3cd8.tar`；
兩份 accepted records 皆為 schema-v3／production／accepted，綁定同一 commit 與各自的 tag，
Fetcher 為 `runtime_profile=unified`。六個 digests 與 staging 完全相同：

| Image | SHA256 digest |
| --- | --- |
| Backend | `2db1da26a45542e0fb255d6f53c41ec879ff9751892693cddec5ca9454bb35d5` |
| Dashboard | `397166c8926c160fc6caed75d8c42bc7b8d309f63877b43a9ea0ea9b8436cbc3` |
| Twelve Data／TAIFEX | `30bd93485ff4d210aa9f264ae3476f0878ec4eb299af4bcfa92f7f48a761d99e` |
| FinLab | `05b8da4c683a12de3d9c2dd53dd9c87111800b78aa9a1764a76ebb870e14a1d0` |
| Shioaji | `705717720905898479aa5e49c70ec769d6f4331f5f6d5eac679b0a504217ec3b` |
| FactSet | `63ef339a2803732246ca8b8d0ee7d25e406fe0f8390bb7ea6b8c8f2619623ec2` |

DB revision 由 `0d57f9b8a2c1` 升至 `1e68a0c9b3d2`；此 migration 僅調整 constraint 名稱／日曆
CHECK／時間預設值，沒有改寫既有 frozen rows。部署後八個表的 rows 與完整內容指紋一致：
7 first-date states、101 plans、101 decisions、3,699 parts、182,413 members、25 historical
requests、32 historical items、17,772 provider-work receipts。舊 revision 的 accepted bundle
仍須通過現行 DB compatibility gate，不能直接啟動舊 image 或 downgrade 交付證據。

Enforced runtime 的既有 pending-plan projection 為 Shioaji 2026-10-27 的 equity／ETF minute
各補上一筆 FIFO work；兩筆原 plan 均於 10/28 建立，原 plan／members／checkpoint 未改寫。
原 Twelve Data 25 筆、FinLab 30 筆 work 保留，目前 57 筆 projection 全為 suspended、沒有 active
owner 或 acquisition／待 drain 工作；不是重新執行全部 backlog。原 Shioaji 28 筆、TAIFEX 15 筆
cancelled work 及兩筆 TAIFEX historical 終態也保留。公開 health 正常，normalization queue
沒有 queued／processing、unpublished outbox、expired lease 或 retry exhausted。部署不啟動既有
停止的 controls、不觸發 provider 資料抓取，也不宣稱 native 驗收完成。

最後一輪唯讀檢查確認六個 Backend／Dashboard services 與十五個 Fetcher consumers 均 running、
exit 0、零重啟、無 OOM／Docker error，實際 images／argv 與 installed release manifest 一致；
五個 Backend roles 與全部 Fetcher consumers 的 `PROVIDER_WORK_REQUIRED=true`。
完整 installed-host guard 再次通過，五組 lock inode 未變。十七個原 SQLite 的所有既有 tables／rows
與備份 SHA256 完全一致，包含原 prepared bytes、20,102 個 Full checkpoint receipts、帳號 pins、
acquisition intents、operation attempts、quota／pacing／cooldown；八個 DB frozen tables 指紋也再次
一致。`pnpm check` 與 `git diff --check` 通過。本次沒有執行 native canonical／recovery 或實際 rollback
演練，相關待辦繼續列於 [native 與回復驗收](../dev/production-foundation-cutover-plan.md)。

證據：停機／state 備份 `1a791b83-37d5-4dd5-910a-f300aaac5777`、DB frozen scope 基準
`57427f8b-9d67-41de-86b0-0ac128cf38d7`、enforced containers／原 rows 比對
`4a18c241-691c-4967-947f-777446c2e6dd`、host proof
`9279b1c1-4cee-43f6-a114-040a452987db`；Backend production workflow
[40867773717](https://github.com/FPI-TW/findb/actions/runs/40867773717) 已成功，Fetcher workflow
[40869478807](https://github.com/FPI-TW/findb/actions/runs/40869478807) 亦已成功。
Backend 後 frozen rows gate `795d241e-1e77-491b-b242-496f0b3ea6f8`、受控啟動／完整 host guard
`04ea1e7a-6992-4497-8e29-e3eea7d5b38b`、新增投影來源／具名 audit／原 frozen rows 比對
`7e36eada-2a8c-4b2b-aace-df902f522728`。
最終 Fetcher 保留性／images／完整 guard `79293c41-5839-4c31-82e0-0f7754d072da`、
Backend／Dashboard images／服務健康 `945729ee-678b-4517-a9c2-b78e80461ed9`、
DB frozen rows／controls／queue／Owner audit `ef962abb-4b81-4ed5-8e70-ac0d892539d8`。

## Shioaji 缺口與 DQ 調查（2026-10-05）

Production 2026-10-01、10-02、10-05 共六筆未結案 missing alerts，分別對應每日的
`tw_equity_minute` 與 `tw_etf_minute`。三日的 SQLite checkpoint 均顯示 equity sequence 1
（`2330`）成功，sequence 2（`2454`）在第一次 attempt 以 `KBARS_PAYLOAD` /
`contract_access` 終止；其餘 48 檔股票與 3 檔 ETF 尚未嘗試，排程窗口已到 cutoff。
這是 provider contract access 階段失敗，不能把未嘗試序列判定為商品無資料。

調查當時 runtime 固定使用 Shioaji `1.7.1`。隔離的 bounded simulation 驗證確認 login 成功後，
`api.contracts` 實際為 `ContractsApi`；讀取 `.stocks` 立即拋出 `AttributeError`。
直接呼叫 `api.contracts.get("2454")` 則回傳 `BaseContract`，identity 為
`code=2454`、`exchange=TSE`、`security_type=STK`、`region=TW`，並成功 logout。
失敗根因是 runtime binding 的公開介面與已安裝 `_core.pyi` 中的 legacy `stocks`
宣告不一致，不是商品無資料或已確認的 catalog timeout。唯讀 direct lookup 另確認 frozen
53 商品全部可取得：code 與 manifest 一致、security type 均為 STK、region 均為 TW，
其中 52 檔為 TSE、1 檔為 OTC，無需推測 exchange。

Provider fallback 已改為 [官方合約文件](https://sinotrade.github.io/tutor/contract/)
公開的 `api.contracts.get(code)`；維持 missing contract 分類、credential-free error 封裝，
以及 `2330`、`0050`、`0056`、`006201` 四個已驗證 `BaseContract` identity shortcut。
Production 53 商品 manifest、sequence 順序與 cutoff／terminal 政策維持原設定。

修復 provider 不會自動重設既有 terminal checkpoint 或補送上述缺口。Production 現可透過
[Shioaji 歷史回補](data_maintenance.md#shioaji-股票etf-分鐘回補) 分別申請股票／ETF minute，
以 reviewed production 50／3 成員 manifest 建立新 request，保留 daily terminal checkpoint。
上述六筆 alerts 是當次調查紀錄；實際缺口須重新查證並完成回補驗收後，再人工處理對應 alerts／DQ。
不可刪除 SQLite state、鬆綁 daily cutoff 或把 terminal error 自動轉為 retry。
部署驗證與歷史缺口復原須各自留下驗收證據。

同次唯讀調查的 26 筆未解決 DQ issues 均為 `INGRESS_DELIVERY_POLICY_WARNING`，severity
為 `warning`。其中 16 筆為 Shioaji 09-21 至 09-24 的 historical payload 在 09-30 送達，
觸發 `LATEST_DATE_MISSING`（expected latest date 為 09-29）；另 10 筆為 FinLab snapshot
低於 delivery policy 最低 2,100 筆，五次各 50 筆、五次各 2 筆，其中四次同時缺 latest date。
Bounded FinLab production manifest `fetcher/configs/finlab_tw50_2026_09_21.v2.json`
只選 50 檔，與 full snapshot 的 2,100 筆 threshold
不相符，須獨立檢視政策與執行範圍。這 26 個 ingestion runs 均為 completed，合計成功
寫入 3,668 筆，`failed_records` 合計為 0；warning 不阻擋 canonical 寫入。
DQ 問題需逐筆經管理流程人工結案；provider 修復或部署不可自動 resolve，也未授權修改
DQ／delivery policy。

## Foundation apply

1. 使用 `findb-production` profile 驗證 caller identity，先在
   `infra/tofu/production-bootstrap` 執行 `fmt`、`validate`、refresh plan。Bootstrap plan 只能
   建立 state KMS 與 `findb-production-tofu-state-289112218471`，不可出現 delete。
2. 經核准後 apply bootstrap，將其 local state 遷移至獨立的
   `production/bootstrap.tfstate`；確認 versioning、public access block、SSE-KMS 與 lockfile。
   遷移前先將 `production-bootstrap/backend.s3.tf.example` 複製為 ignored `backend.tf`，再使用
   `bootstrap_backend_migrate_command` output；確認 remote state 完整後才安全刪除 local state。
3. 以 bootstrap output 的 KMS ARN初始化 `infra/tofu/production`。保存 refresh plan，確認
   account output 為 `289112218471` 且沒有 delete，再由受信任 operator apply。
4. 同一 PR 對 `infra/tofu/staging` 執行 refresh plan，新增的兩個 promotion-reader role 之外
   必須 zero-delete。Reader 只可讀自己 unit 的 `*/accepted/`、acceptance record 與 ECR layers。

禁止從 PR runner apply；禁止在 state bucket 存 application secret；禁止把 bootstrap saved plan
拿來 apply foundation。任何 account、region、resource prefix 或 delete 不符即停止。

## Live foundation acceptance

- VPC CIDR 必須是 `10.20.0.0/16`，無 NAT；RDS subnets 無 internet route。
- FinDB `t3.large`、Fetcher `t3.medium`，IMDSv2 required、hop limit 2、無 key pair、無 TCP 22。
  FinDB 443 只接受 Cloudflare published CIDRs；Fetcher security group 無 ingress。兩台host都必須安裝
  Docker Engine與Compose v2；`docker compose version`是部署前必要檢查。
- RDS 必須為 private PostgreSQL 16、`db.t4g.medium`、100 GiB gp3、KMS、Single-AZ、14 日
  backup、deletion protection，5432 僅接受 FinDB security group。
- 五個 ECR repository 必須 immutable 且 scan-on-push；instance role 只 pull 自己 unit，deploy
  role不能讀 Secrets Manager values，promotion role 只寫自己 unit 的 ECR 與 bundle metadata。
- 兩台 host 必須出現在 SSM managed nodes；使用 bounded SSM command 驗證 instance role、unit
  ECR pull、unit bundle read、cross-unit denial、runtime secret isolation與 RDS connectivity。
- 驗證 CloudTrail multi-region/log validation、GuardDuty、DLM recovery point、RDS automated backup、
  CloudWatch alarms 與寄至 `tyler.ho@fpitw.com` 的 SNS subscription。Custom metrics 在 collector
  尚未健康發布時必須因 missing data 告警，不得把 `INSUFFICIENT_DATA` 當通過。
- 所有資源必須有 `Project=findb`、`Environment=production`、`DeploymentUnit`、`Owner`、
  `BackupOwner`，並在 Cost Explorer 啟用這些 cost allocation tags；不建立固定 Budget。

## External resources 與 secrets

Cloudflare 帳號中建立 private `findb-production-raw` 與 `findb-production-canonical` R2 buckets。
Raw lifecycle 30 日並保護前 7 日；Fetcher raw credential只可讀寫 raw。Canonical publisher只可
讀寫 canonical，Serve reader只可讀 canonical，三組 access key/secret不得相同。管理 credential
不得注入 runtime。

建立 proxied `findb.tingfong.com` A record 指向 FinDB EIP，origin 使用獨立 Cloudflare origin
certificate。Fetcher EIP 的 `/32` 必須成為 `SOURCE_ALLOWLIST_CIDRS`。憑證、R2、DB、RabbitMQ、
四類 API key 與三個 provider credential 全部重新產生，分別寫入 OpenTofu 已建立的
`findb/production/findb/*` 或 `findb/production/fetcher/*` metadata；不得重用 staging 值。
Origin certificate secret固定使用單行 base64 欄位
`FINDB_ORIGIN_CERTIFICATE_PEM_B64`與`FINDB_ORIGIN_PRIVATE_KEY_PEM_B64`；部署只在instance上解碼、
驗證hostname、至少30日效期與keypair一致性，再原子寫入`/run/findb-runtime-secrets/nginx/`。
憑證與私鑰不得持久化到`/home/ubuntu`或進入GitHub Environment。部署產生的三個非秘密nginx
設定檔必須在驗證前正規化為root-owned `0644`；tmpfs中的憑證、私鑰與lookup key仍維持`0600`。

## 共用 credentials 與初始化

五個 provider Source scopes 在 staging／production 相同，只有 credentials values 不同：

| Source | Dataset allowlist |
| --- | --- |
| `twelve_data` | `us_equity_eod`、`hk_equity_eod` |
| `finlab` | `tw_equity_eod`、`tw_etf_eod` |
| `shioaji` | `tw_equity_minute`、`tw_etf_minute` |
| `taifex` | `tw_futures_eod` |
| `factset` | `hk_equity_eod` |

`reconcile_fetcher_credentials.py --runtime-profile unified` 只校準 allowlist／hash 並留下 audit，
不啟動 scheduler。五把 Source keys 相互隔離；現行execution manifest的HK feed使用FactSet，
Twelve Data保留HK allowlist僅供既有或已凍結工作的相容交付。有效日曆與執行政策由Source提供，
不再注入calendar Serve key。
跨 unit bridge 只傳 SHA-256，
instance roles 不放寬跨 unit Secrets Manager 讀取。
TAIFEX 公開 API 不需要 provider credential，但需要獨立 Source key；IaC 固定宣告此 metadata。
S3 state backend 必須使用既有 KMS key ARN，不能任意替換 encryption 設定。

## DB 與 calendar

兩環境共用 migration、application role 最小權限校準、registry 與 credential 初始化；
部署不執行 calendar seed，也不要求日曆完整。Published 日期優先，缺少日期採 weekday default。
新 controls 均 stopped；不得複製 staging canonical／raw／workflow data 至 production。
Candidate 必須先通過 migration 與內部健康驗證，才保存 acceptance；失敗保持停止，不讓舊程式操作新 schema。
詳見 [統一切換與回退程序](deployment.md)。

`backend/configs/calendars/` 的 2026 US v2、HK v1、CN v1、TAIFEX v1 JSON 各有
365 個明確日期、session 時間、來源 URL／原始檔 SHA-256 與 2026-10-02 review 註記。
TW 2026 沿用既有 immutable `holiday_schedule_115.csv`，2026-10-02 核對
[TWSE 官方開休市頁](https://www.twse.com.tw/zh/trading/holiday.html)及其
[2026 年度 JSON](https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?date=20260101&response=json)
的 27 筆日期、名稱與開休市分類完全一致（換行格式不同）；原始 JSON SHA-256 為
`7fefe785ea7155a5004a2eb74486ad865ea5c4b5f02ee0cffbbdacb1ca2ea390`。
保留 2/12、2/13 無交易但可結算交割的 `settlement_only`，不以 TAIFEX 的休市分類取代。
HK 使用 HKEX 年度通告校正國票 HTML 遺漏的 6/19，2/16、12/24、12/31 為半日開市，
採 closing auction 最晚界線 12:10，正常日 16:10。US 依 NYSE 保留 10/12、11/11 開市，
11/27、12/24 於 New York 13:00 提前收市；其餘 open rows 為 09:30–16:00。
CN 取國票 PDF「上海及深圳」本地市場列並核對上交所年度安排，不以北向／南向通休市
代替本地股市；4/3、4/7、7/1、10/19 維持開市，例假補班週末仍休市，收盤 15:00。
TAIFEX 僅取官方年度 PDF 的 regular 台灣指數期貨交易日期，2/12、2/13 是休市，
不能複用 TWSE settlement-only 狀態；regular 收盤 13:45，彩色到期／掛牌標記不視為休市。
個別契約最後交易日、國外商品與臨時停市不由此 fixture 推測；臨時異動需 Owner 新 revision。
After-hours 仍保留交易所 attributed trade_date，不能轉成民曆日期或建立 continuous futures。

`seed_production_calendars.py --deployment-target production` 預設只驗證檔案；
`--apply` 才使用既有 preview → apply → publish 流程發布缺少的完整年度。
任何未知來源、operator override、draft 或不同 rows 均拒絕覆寫。
已存在且 source metadata、checksum、365 筆 rows 全部符合舊版 NYSE bootstrap 的 US 2026，
一般 seed 保留 revision 並回報 `action=pending_reviewed_update, upgrade_required=true`，
不阻塞一般部署，也不宣稱新 session 已套用。Owner 審核後才以 `--apply --upgrade-reviewed-2026`
發布 successor，透過 expected revision 的 CAS 防止競爭，原 revision 保留為 superseded。
HK／CN／TAIFEX 初次套用與 US successor 均不改動 scheduler admission、首次日期、
readiness 或 scheduler desired state；本 repo fixture 的更新不等於 live DB 已調整。
