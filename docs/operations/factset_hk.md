# FactSet 港股日行情

FactSet 負責切換後的新港股 EOD 抓取；Twelve Data 繼續負責美股。港股不自動 fallback。
程式更新不等於環境已切換：正式抓取仍需要 provider-work enforcement、已發布的
HKEX baseline、明確的 HK provider cutover，以及 Owner 啟動相應 Pilot／Full control。

## 清單與歷史範圍

清單以[港交所股本證券](https://www.hkex.com.hk/market-data/securities-prices/equities?sc_lang=zh-hk)
及官方 `ListOfSecurities.xlsx` 為主，每天由 FactSet maintenance work 更新一次。
只納入主板／GEM 普通股；分類不明、找不到 listing ID、幣別不符或 mapping 歧義均保留 gap，
不得從 expected 成員中刪除。[TradingView 現行股票清單](https://tw.tradingview.com/markets/stocks-hong-kong/market-movers-all-stocks/)
只作輔助核對。

Daily plan 凍結其 published release；歷史 request 在建立時凍結當下 published release、
member checksum 及完整成員 mapping，所有日期共用這份快照。清單刷新不會重寫原 request。
回補對象是申請當時的現行股票，不包含已不在清單中的下市商品，具有存活者偏差，
不得標示為歷史全市場。Production 最長 366 calendar days，local/staging 最長 31 天。

首次 baseline 尚未發布時，使用已部署 FactSet image 的正常
`findb-fetch-runtime --execution-profile pilot --provider factset --sync-universes`，並帶入該環境
原有 config、Pilot checkpoint 與 shared account／provider-work mounts。維護使用 Source 的
maintenance 授權，不必先啟動 daily control；不得以獨立無 owner 的 MySQL probe 代替。
CLI 回傳 `queued` 只表示排隊，須追到 maintenance terminal、原始證據與 release 才算成功。
工作 key 由 CLI 依 UTC 日期產生，同日重跑不另造 identity；HKEX 檔案日期則按香港日期驗證。
Future-dated 失敗保留原 journal 與用量，待正常日期建立新工作，不改時鐘或重置預算。

首份 release 由 Owner 呼叫 `POST /api/v1/admin/universes/{release_id}/publish`，提供
`first_baseline_approved=true` 與可追溯的 `evidence_note`。發布前核對官方檔案日期／SHA-256、
raw reference、成員總數／checksum、mapping gaps 與 maintenance 結算；gaps 不得刪除。
每日刷新驗收須保存不同正常日期的 work／release 與原始證據，並核對既有 daily plan 的
release ID、members、parts 及 checksum 未變。單日重跑或本機 fixture 通過不代表已驗證跨日更新。
自動 maintenance 由 running cycle 在 refresh due 時入列，controls stopped 時不保證每日刷新；
需明確執行上述維護入口。既有發布差異門檻與人工例外規則維持。

Daily 新 I/O 僅於香港時間 18:30（含）至 23:00（不含）進行；已準備好的交付仍須排空。
Pilot 按 canonical 識別排序取前兩檔。`FACTSET_SKIP_NON_TRADING_DAYS=true` 是預設值；
歷史回補略過休市日。缺少已發布日曆時沿用既有週一至週五規則。

## MySQL 與價格映射

Fetcher 使用獨立 FactSet image 與 Source credential，不連 FinDB DB。設定：

| 設定 | 意義 |
| --- | --- |
| `FACTSET_HOST`、`FACTSET_PORT` | RDS hostname 與 port，預設 3306 |
| `FACTSET_USER`、`FACTSET_PASSWORD` | 來源唯讀帳號，僅提供給 FactSet consumer |
| `FACTSET_SSL_CA_FILE` | 必須存在的 CA bundle；image 預設 `/app/certs/ap-southeast-1-bundle.pem` |
| `FACTSET_QUERY_TIMEOUT_SECONDS` | 1–60 秒，預設 30 |

部署 secret catalog 使用 `provider/factset` 與 `api/source/factset`，後者包含
`FETCHER_FACTSET_SOURCE_CLIENT_KEY`。密碼不進入 argv、錯誤訊息、工作紀錄或 account identity；
account identity 使用 host／port／user，密碼輪替不會重置用量紀錄。

連線驗證 TLS CA 及 hostname，採 read-only transaction、參數化 SELECT、server query timeout、
最多 10,000 個 listing／16 MiB 回應。每個 frozen cohort／日期只查詢一次。
Connect 與 SELECT 前分別重驗授權；阻塞呼叫在繼承 whole-work FD 的子程序執行，
timeout 必須 terminate／kill／reap 後才能釋放 owner。

來源表為 `factset.fgp_v1_fgp_global_prices`，主鍵 `fsym_id`／`price_date`：

| 原生欄位 | 契約欄位 |
| --- | --- |
| `price_open`／`price_high`／`price_low`／`price` | open／high／low／close |
| `volume` | 可無損轉為非負 bigint 的 volume |
| `currency` | 與 frozen HKEX member 一致的幣別 |

映射以 HKEX symbols 限定 `sym_v1_sym_ticker_exchange_hist` 的 ticker 主鍵查詢範圍，
再核對 `sym_v1_sym_ticker_exchange` 的相同 ticker／listing 當前關聯，以及
`sym_v1_sym_coverage` 的 HKG／SHARE／listing 記錄。固定 join 順序避免掃描整張 coverage；
缺少歷史索引候選或當前關聯時保留 mapping gap。
只接受 listing `*-L`，不以 regional ID 替代。`first_price_date` 與 ticker history 起訖
不作上市、下市或停復牌證據。Turnover 單位尚未核定，僅保留 native raw，不寫 canonical turnover。

## 資料錯誤、結算與修補

輸出 `market_eod.v2`：僅 OHLC 大小關係移至逐列 DQ，其餘入口限制保持。
Native bytes 先持久保存，再逐一處理成員；結構錯誤與缺列保留 raw 證據並繼續其他成員。
缺列是 missing，不是 no_data；目前來源沒有可用的每日停牌／無交易證據，adapter 不造 no_data。

EOD v2 daily/historical 結算核對完整 frozen member 集、所有永久 receipts、實際寫入證據及
DQ／raw 缺漏證據。所有 accepted runs 必須 terminal，原 prepared 必須已交付或有合法
終結證據。存在資料缺漏時結果為 `partial`；historical 繼續下一日期，最終 request 仍標示
partial。暫時來源／Source／raw storage 故障停止新 I/O，close、drain 後 FIFO tail retry，
不能用 partial 掩蓋。Native response、prepared bytes、receipts 與 operation budgets 跨 retry 保留。
窗口／control 停止也走安全 tail retry；只有該操作尚未 dispatch 時退還預留次數，已開始的
來源 I/O 仍計入原預算。已知永久來源、raw storage 或 Source 拒絕會 close／drain 後結算
failed，保留尚未交付 prepared 的人工處理證據；fence 或 ownership 不確定則仍停止並要求復原。
Production planner 的 `--deliver --wait` 遇到 partial／failed／cancelled 立即輸出終態並回傳
exit code 3；只有 completed 回傳 0。未指定 `--deliver` 仍只是 preview。

每日告警依 expected trading days 計數；`EOD_LONG_GAP_TRADING_DAYS` 預設 5，可設 1–366。
有效 data 或持久 no_data 中斷該成員的 streak；休市不計。沒有 frozen plan 時記錄 feed 缺漏，
不推測成員範圍。

Monitor 依 dataset 的 `allowed_sources` 掃描，Twelve Data 已不在 HK scope，不會建立新告警。
Migration `c7d2e9a4f1b3` 結清既有 open 告警，保留原 details 並加上
`resolution.code=FEED_RETIRED`；歷史紀錄上的 `PROVIDER_RETIRED` 不再新增。
切換前日期的資料改以 FactSet historical request 回補。

來源修正後，可建立新的歷史回補 request。相同 provider／dataset／日期、相同 frozen
上市識別碼的 historical 實際寫入，可解除 daily monitoring 的缺漏；原 daily work 的 partial
結算與原始證據仍保留。其他 provider、不同上市識別碼、單純成功筆數或既有價格不能補足證據。
任一 work 的 completed 結算仍只採認自己的 receipts，不能借用另一工作的寫入證據。

### `no_source_row` 缺漏排查

先依環境、execution profile、資料集與交易日定位 daily plan 及 provider work，再核對
`provider_work_attempt.proof.eod.members` 的逐成員 reason。`no_source_row` 表示該次成功
來源查詢沒有對應 listing 的價格列；不等同連線失敗、停牌或已證實的上游延遲。
若整批缺列，可能沒有任何 Source ingestion run；應先查 retained snapshot，不能只從
normalization queue 尋找原因。

以唯讀 transaction／SQLite `mode=ro` 比對 frozen listing scope、交易日、snapshot
`fetched_at`、列數與 settlement raw hash，並核對 operation budget、receipts 及 canonical
寫入數。診斷輸出只需日期、計數、reason、hash 與識別碼，避免匯出完整原始 response。

Daily work 在 immutable `work.created_at + completion_window_seconds` 截止前遇到
`no_source_row` 時，只將該次空缺保存為 observation，不保存可 replay 的成功 snapshot；work
安全 close／drain 後排到 provider FIFO 尾端，最長 15 分鐘後重新查詢來源。這類等待不消耗
local `FULL_WORK_MAX_ATTEMPTS`，但每次查詢及其 hash、計數與時間仍保留稽核。截止後仍缺列
才保存 authoritative snapshot，將 local member 標示 `manual`／`missing`，並把 daily work
結算為 terminal `partial`。Historical work 不使用 daily publication window，缺列仍直接結算
`partial`，後續由新的歷史回補 work 取得新證據。

Provider query 與 EOD cohort 兩層 durable response 都只會將涵蓋完整 frozen listing scope 的
結果保存為 replay authority；任一層都不得把 `no_source_row` observation 升級成可 replay
snapshot。後續 publication retry 因此一定重新進入來源查詢，而不是重播首次缺列結果。

每次 daily publication retry 都會在任何新 FactSet query 前重新讀取目前 published calendar，但不改寫建立 work 時凍結的
`planned` decision。若 frozen decision 之後的新 published revision 已將該日標為休市，work
以 `CALENDAR_DECISION_REPAIR_REQUIRED` failed，停止來源重試並等待下節人工休市更正流程。
不可藉重啟 consumer、重置 checkpoint 或清除 journal 規避截止時間或修復決策。

### 重試層級與來源更新時間

| 情境 | 現行處理 |
| --- | --- |
| Daily 來源成功回傳 `no_source_row`，且仍在 publication window | 保存 observation、不凍結空 snapshot；排至 FIFO 尾端，最長 15 分鐘後重新查詢，不消耗 local work attempt |
| Daily `no_source_row` 超過 publication window；historical `no_source_row` | 保存 authoritative snapshot 與逐成員缺漏證據，結算 `partial` |
| Retry 時 frozen decision 之後的新 published revision 已標示休市 | `CALENDAR_DECISION_REPAIR_REQUIRED` failed；不改寫 frozen decision，依人工休市更正流程處理 |
| mapping gap、永久資料結構／DQ 問題 | 逐成員保留錯誤證據，結算 `partial`；不當成來源尚未發布 |
| 暫時連線／MySQL unavailable、可重試 raw storage 或 Source 故障 | close／drain 後排至 provider FIFO 尾端，預設至少 60 秒，另遵守 cooldown 與抓取窗口 |
| 原始 snapshot、raw persistence、各成員 receipt 操作 | 各自最多 3 次總 attempts，跨 FIFO rounds 保留預算；未 dispatch 的合法等待可退還預留次數 |
| Source HTTP transport／408／429／502／503／504 | 預設每次呼叫最多 3 次總 attempts，退避 1、2 秒；有合法 `Retry-After` 時依其等待，預設上限 30 秒。其餘 5xx 由外層處理 FIFO retry |

成功操作的回應會存入 work journal，後續相同 work 使用原 snapshot、prepared bytes 與
receipts。唯一例外是 publication window 內的 daily `no_source_row`：只存 observation，retry
會取得較新的來源 snapshot。Source 已接受後的 normalization retry 是另一層，只重跑既有 raw，
不重新查詢 FactSet。

查詢上游更新時間時，先確認目標 listing／`price_date` 已存在，再檢查來源表是否有逐列
入庫時間欄位或 loader audit。`price_date` 是交易日，Fetcher `fetched_at` 是觀測時間。
MySQL `information_schema.tables.UPDATE_TIME` 僅為整表最後異動 metadata，不能當成特定
交易日／listing 的首次入庫時間；其值也不保證跨重啟持久保存，見
[MySQL TABLES 說明](https://dev.mysql.com/doc/mysql-infoschema-excerpt/8.0/en/information-schema-tables-table.html)。
缺少逐列時間、載入稽核或可用 binlog 時，只能以先前缺列與首次確認有資料的觀測建立時間
區間；確切更新時刻需來源端同步工作紀錄佐證。

## Backend-first 切換

1. 更新 backend、執行 Alembic head 與 seed。HK registry 同時保留 v1、v2 和
   `twelve_data`、`factset` acceptance，舊 prepared／raw rerun 按原 identity 路由。
2. 安裝 FactSet image／credentials；所有新 controls 預設 stopped。Fetcher bundle v2
   包含四個 images、五個 providers、十五個 Pilot／Full／Historical consumers。
   舊 bundle v1 只供既有安裝辨識；新 release 與 promotion 必須包含 FactSet digest。
3. 依 [provider-work 維運](../architecture/provider_work.md) 完成單 host／shared inode／所有 consumers
   停止及排空的 preflight attestation，再開啟 `PROVIDER_WORK_REQUIRED`。
4. 使用 FactSet Pilot maintenance 同步 HKEX 清單，由 Admin 確認並發布第一份 baseline。
   之後沿用既有差異門檻與人工例外核准規則。
5. 停止 Twelve Data 與 FactSet 的 Pilot／Full controls，排空 active owner。以香港當日日期
   執行 `uv --directory backend run python scripts/cutover_hk_factset.py --effective-date YYYY-MM-DD --actor OPERATOR`。
   預設驗證後 rollback；加入 `--apply` 才提交。切換決定不可改寫或回溯日期。
6. 切換 transaction 保留原 dataset/profile first dates 與 plans，記錄 audit，設定 FactSet
   priority -1、Twelve Data priority 0，並使舊 HK Twelve Data stored priority 一致，保留價格與 lineage。
   FactSet 新 daily 日期從切換日開始；更早日期使用 historical request。
7. 完成兩檔單日、異常歷史樣本、全市場 native acceptance 後，再由 Owner 啟動所需 control，
   依既有 staging acceptance／production promotion 流程交付。

不得在仍有 cohort、partial、v2 run 或切換紀錄時 downgrade 此 migration。Rollback 亦須保留
支援 v2 acceptance、FactSet scopes 與五 provider inventory 的版本；不以刪除證據方式降版。

### Cutover 與原生連線驗收證據

切換前保存當前 accepted Backend／Fetcher bundle、Alembic revision、DB／checkpoint 備份、
兩 provider 的 Pilot／Full controls revisions、first dates、plans／members fingerprints，
以及 shared lock identity。以 CAS 停止四個 controls，等待 desired／observed stopped，
核對兩 provider owner 已釋放及在途 prepared／receipts 已排空；心跳過期不代表 owner 已釋放。
先執行 cutover script 的預設 dry-run，再以同一香港日期與具名 actor 執行 `--apply`，
保存 decision／audit 與上述 fingerprints 的前後比對。跨香港午夜須重做日期核對。

HK provider decision 不可改寫，應用 rollback 不代表把 HK 新抓取切回 Twelve Data。
候選 rollback 版本須維持目前部署的 provider-work owner 到期證據契約，以及 EOD v2、
cohort／partial、FactSet scopes 與十五 consumers；accepted 身分、migration compatibility
及停止／排空要求依 [部署維運](deployment.md) 核對，不只比較 image tag。

FactSet maintenance 成功可提供 staging 到 RDS 的 TLS／原生 catalogue SELECT 證據，須核對
實際 image 的 CA／hostname 必驗、read-only transaction、timeout 及持久原生 response。
行情驗收另須在正常 owner／授權流程下取得 frozen listings／日期的 prices SELECT，保存 raw
SHA-256、查詢範圍與 terminal 結果。只看到 container running、Secrets Manager 配置完成、
mock TLS 測試或本機快照重播，均不足以完成 staging 實際連線驗收。
密碼與完整 connection string 不得進入輸出。

## Twelve Data 舊 HK work 結案

Twelve Data 不再擁有 HK 抓取路徑：Fetcher 已移除香港 country catalogue、HKEX 名單與 HK
日資料分支；Migration `c7d2e9a4f1b3` 移除 scheduler、未撤銷 credential 與 `allowed_sources`
上的 `twelve_data` × `hk_equity_eod` scope。Twelve Data 對 XHKG 的單次診斷
（凍結商品 `0001`，含 `exchange=HKEX`）均回 HTTP／provider code 400 `no_data_response`；
官方將 XHKG 列為需授權或未支援市場，因此港股不再列為 Twelve Data 待解項目。

方案沒有 HK 權限，此退役為永久決定：Source 的抓取閘門不論 HK cutover 是否已套用，一律拒絕
`twelve_data` × `hk_equity_eod` 的新 plan、manifest、prepare 與 claim 取得權限；新交付與舊 raw
rerun 也因不在 `allowed_sources` 而 fail closed。此閘門只封鎖這個組合，cutover 判斷也只適用
FactSet；未來新增的 HK 來源依 provider scope、`allowed_sources` 與 execution manifest 治理，
不受此處限制。

Migration 只處理 scope 與 open 告警，保留 raw、run、plan、work、universe release、canonical
列及已撤銷 credential。若此組合仍有非 terminal work／run、未結算 attempt、未交付 prepared 或
進行中的 historical request，migration 會中止，不代為結算。Downgrade 為 no-op。

2026-11-02 以唯讀 transaction 核對，兩環境此組合的 accepted runs 與 raw 皆為 0，canonical 也沒有
Twelve Data HK 列，關閉 rerun 沒有資料受影響。Staging 已套用 HK cutover；production 當時尚未
套用，也沒有 FactSet release、work 或 HK canonical 資料，需另依前述切換程序上線。

2026-10-30 06:10 UTC 以唯讀 transaction／SQLite `mode=ro` 核對兩環境，結案條件全部成立：

| 環境 | Source work | Plans | Owner／在途 | Fetcher checkpoint |
| --- | --- | --- | --- | --- |
| Production | 12 筆 Full daily（10/13–10/24）均 `failed / RETIRED_FEED_MANUAL_REVIEW`，prepared 0、receipts 0、未結 attempts 0 | 12 份，expected 33,852 全為 blocked | owner 無 attempt；非 terminal runs 0、未發布 outbox 0；Pilot／Full controls stopped | Full 12 份本機 plan 仍 `complete=0`：33,288 `pending/source_error`、564 `manual/mapping_gap`，prepared bodies 0、receipts 0；Pilot 0 |
| Staging | 2 筆 Pilot daily（10/15–10/16）均 `failed / RETIRED_FEED_MANUAL_REVIEW`，prepared 0、receipts 0、未結 attempts 0 | 2 份，expected 4 全為 blocked | owner 無 attempt；非 terminal runs 0、未發布 outbox 0；Pilot／Full controls stopped | Pilot 2 份本機 plan：4 筆 `manual/retry_exhausted`，prepared bodies 0、receipts 0；Full 0 |

本機 checkpoint 的未完成 HK plans 是舊執行的保留證據，會持續出現在 `--health` 的
`unresolved`；不得清檔、改成 complete／no_data 或重設預算。這些成員沒有 prepared bytes
或 receipts，不會再交付。切換日前的港股資料若需補齊，一律以 FactSet historical request 處理。

SSM 證據：production DB `408e1514-f6fb-4c24-bfa9-445a7675824f`、Fetcher SQLite
`876d9006-82f7-487e-aaf8-d603e0482d20`；staging DB `57ed7c13-70c4-4552-9614-96073634aed8`、
Fetcher SQLite `0c1d31f2-6738-4f8e-8ac8-d1d0c614c223`。核對未改動 controls、work、plans、
checkpoint、credentials 或預算。
