# Data Maintenance

所有維護程式使用獨立one-off container或工作資源，不在serve process執行。預設先
dry-run；修改staging前確認target、停止相關writers、建立backup並保存輸出。

## Partial dump與local seed

設定與工具：

```text
backend/configs/partial_dump.yaml
backend/scripts/partial_dump.py
backend/scripts/seed_upsert.py
```

```bash
uv --directory backend run python scripts/dev.py seed-upsert
uv --directory backend run python scripts/dev.py seed-upsert --truncate
```

- Seed package pin明確artifact/version，不使用會漂移的`latest`。
- 只匯出必要schema與抽樣資料；raw／敏感欄位需明確納入並匿名化。
- `--truncate`只可用於local development DB。
- Schema改變後重生package並驗證manifest、row counts與FK順序。
- 完整參數以script `--help`為準。

## Instrument名稱與routing

```text
backend/scripts/backfill_instrument_names.py
backend/scripts/backfill_world_names.py
backend/scripts/cleanup_stale_instruments.py
```

1. 確認candidate image包含預期script並建立RDS snapshot。
2. 以one-off container執行dry-run，保存candidate、unmatched、reclassified與deleted摘要。
3. 核對child rows與市場routing後才使用`--apply`。
4. 執行Serve與資料完整性檢查，再重生instrument cache。

TW舊Big5亂碼只有在候選集合已確認時才使用`--overwrite-existing`。Routing cleanup可能
搬移或刪除重複instrument，不得跳過FK與下游影響檢查。

## Generated cache

下列JSON是generated artifacts，不是source of truth：

```text
backend/app/static/data/instruments.json
```

Canonical data改變後使用`backend/scripts/generate_instrument_cache.py`重生。Generator以每頁100筆
讀取`/api/v1/serve/instruments`的nested coverage，不再逐商品補查EOD，也不再產生macro cache；每次
執行會先移除舊`macro-series.json`。Cache失敗時修正canonical data或generator，不直接編輯JSON。

## Staging data reset

Reset可移除mutable canonical、workflow與PostgreSQL raw，建立bounded空白基線；
它不是rollback。必須使用`backend/scripts/reset_staging_legacy_data.py`的兩階段安全流程：

1. 確認database／host是staging，記錄Alembic revision、image SHA與protected tables。
2. 將scheduler desired state切為`stopped`，停止`ingest`、`dispatcher`、`worker`、
   `raw-cleanup`，確認queue、DLQ與active tasks為空。
3. 以同一target connection執行唯讀盤點，保存counts與connection fingerprint。
4. 核對範圍後使用script要求的staging confirmation及writer-stopped assertion執行apply。
5. 驗證mutable targets為零、protected tables不變且lineage無orphan。
6. 同步重置provider ingestion SQLite checkpoint（含 Full frozen/prepared plan state），重生cache並執行bounded acceptance；保留獨立 provider-account governor 的 quota／usage，不能退款或歸零。

`dataset_registry`、source／API credentials及`alembic_version`屬設定／schema state，必須
保留。Full 的 `full_market_environment`、`full_market_enrollment` 安裝 authority 與
`full_market_dataset_state` 永久首次日期同樣保留；`full_market_admission`／`full_market_admission_feed`
和被引用的 baseline、calendar、plan 一起清除。重建 published baseline、取得有效
installed runtime ACK 後，Owner 才能重新手動啟動並建立新 admission；first date 不重設。
Provider queue 的 work／attempt／receipt 及綁定 work 的 Shioaji repair／ACK 稽核屬 mutable workflow，一起清除；owner slots 的 installed inode
與 monotonic fencing generation 保留。任何 active owner 都拒絕 reset，即使 heartbeat 已過期。
為保留 owner→attempt 的 FK，先明確刪除 repair ACK／repair／receipt／attempt／work，再在同一 transaction explicit
`TRUNCATE` 其餘 targets（包含 receipt 與 ingestion run）；禁止 `CASCADE`，未列出的 FK fail closed。
Reset 後 owner 留下 `mutable_reset_requires_verified_cutover` blocker；停止 consumers，備份並重置
provider-work journal、各 profile／historical mutable checkpoints，保留 shared lock inodes 和獨立
account governor quota／usage。重新執行完整 host preflight 與具名 Owner cutover 後才能 claim；
不能以舊 journal／prepared bytes 對新空白 DB 基線重播，也不能刪 governor 來退款用量。
`market_data_eod.run_id`、raw run ID與DQ run ID沒有完整DB FK／cascade，不能假設
只刪run即可清乾淨。

R2 raw不屬PostgreSQL reset，維持既有lifecycle與bucket lock。舊SQLite或prepared raw refs
不得跨bucket、environment或新空白基線重用。

## Raw retention

- R2 是 provider 原始 bytes 的主要保存處。Staging 與 production 的 raw bucket 由 Cloudflare
  lifecycle 在物件滿 90 天後刪除，由控制台管理。應用程式不主動刪除物件。
- Production raw 全桶的 bucket lock 保護前 7 天。Staging 的 7 天鎖定套用於
  `acceptance/raw_lock_probe/`。
- PostgreSQL `raw.market_payload` 保存 Source ingress payload 30 天，由
  `RAW_RETENTION_ENABLED`／`RAW_RETENTION_DAYS` 控制每日清理。Contract-only rerun
  依賴仍在 PostgreSQL 的 raw。R2 的 90 天窗口保留 provider bytes 供稽核及重新映射。
- Fetcher SQLite journal 與 checkpoint 是供工作重啟與恢復使用的暫存，清理規則見下方
  [已結案工作的本機保留](#已結案工作的本機保留)。
- 事故、queue recovery 或 migration 期間，先保留需要的 R2 證據，並暫停相關
  PostgreSQL cleanup，以保留 rerun 能力。Canonical 資料依獨立規則保存。

## Account governor保留

共用帳號governor每日由Full worker清理一次，同主機以`account_ledger_retention`記錄下次時間，
多個consumer不重複執行；失敗只輸出`account_ledger_retention`診斷，不阻擋抓取。

- 只刪`completed_at`超過7天的`finalized`／`cancelled` intent與其usage樣本。
- 只刪7天前、`blocked_until`已過的`full_quota` window；當日window與未到期cooldown保留。
- `armed`／`unknown` intent一律保留，用量不退款；Shioaji帳號全部保留，待退役流程處理。
- 熱路徑以`acquisition_intent_status(account,status,completed_at)`查armed recovery與unknown liability。
- Schema初始化（含既有大帳本補建index）以`temp_store=MEMORY`排序；consumer rootfs唯讀、`/tmp`
  僅16 MiB，排序溢出到`/tmp`會以`database or disk is full`讓每次受治理的抓取失敗。
- 刪除後不自動VACUUM，空間由SQLite重用。

## 已結案工作的本機保留

Full worker 每日對自己的 provider 清理一次已結案工作的本機狀態；同一 provider 的 consumers 以
`work_retention`（journal）與 `checkpoint_retention`（Full checkpoint）記錄下次時間，不重複執行。
清理失敗只輸出 `closed_work_retention` 診斷（只含例外類別名稱），不阻擋抓取，隔天重試。

- 是否結案由 Source 判定：Fetcher 以 `POST /api/v1/source/provider-work/closed` 查詢（唯讀，
  依 Source credential 的 provider／dataset scope 回答，一次最多 500 筆）；仍在進行、未知或越權的
  工作不會出現在回應中，一律保留。Source 尚未提供此 API 時查詢失敗，等同不清理。
- 只清 Source `completed_at` 已超過 7 天的工作。R2 保存 provider bytes 90 天，本機只保留恢復所需。
- Provider-work journal（主機共用的 `provider-work.sqlite3`）：刪除 `work_delivery`、`work_job`、
  `work_budget`、`work_cooldown`、`work_source_error`。Twelve Data 的回應已確認有 R2 副本，另刪
  `work_response`、`work_response_observation` 與 `work_manifest`；其他格式（FinLab dataset、官方
  名單、TAIFEX 報表、catalogue）的回應與 manifest 保留。任一 delivery 缺 receipt 的工作整筆保留。
- Full checkpoint：以 `daily:full_market:<dataset>:<trade_date>` 對應 plan，刪除該 plan 的
  `full_work` 及其 receipt、diagnostic、empty outcome、member repair，以及 plan、reconcile、outage、
  no-data trip／round 與 schedule 列。仍有 prepared body 但沒有 receipt 的 plan 整筆保留。
- 已結案的工作不會再被領取，因此沒有路徑會再讀這些列；`full_cursor` 與 account governor 不在此範圍。
- 刪除後不自動 VACUUM，空間由 SQLite 重用；要縮小檔案時，在 controls 停止後另行 VACUUM。

## PostgreSQL 暫存副本保留

以下三類 server-side 資料由常駐的 `raw-cleanup` 服務每日清理，在 raw TTL 之後執行。保存期限由 Owner 決定（#494），
設定在 `Settings`，每一項都必須至少 1 天。

| 資料 | 內容 | 恢復／稽核用途 | 保存期限（設定） |
| --- | --- | --- | --- |
| `provider_work_prepared.body` | Fetcher 交付的完整 body，和 raw TTL 無關 | Recovery、retry 與 drain 會重送原始 bytes。終態 work 不會再被領取，但 descriptor、digest 與 receipt 是永久證據 | 結案後 7 天（`PREPARED_BODY_RETENTION_DAYS`） |
| `calendar_import_batch` | 匯入預覽與 `candidate_rows` | `previewed` 在 30 分鐘後拒絕套用；`applied` 會在重放套用與列表中被讀取 | `previewed` 過期後 1 天（`CALENDAR_IMPORT_PREVIEW_RETENTION_DAYS`）；`applied` 永久保留 |
| `admin_session` | 已過期或已撤銷的 session | 讀取時已過濾；稽核需要時看 security event 與 audit，不依賴 session 列 | 結束後 30 天（`ADMIN_SESSION_RETENTION_DAYS`） |

`backend/scripts/retention.py`：

- `inspect`：唯讀。依 provider、work 狀態、結案年齡，以及是否仍可能重送，統計 prepared body 的
  bytes；另外統計 import batch 與 session 的狀態。
- `cleanup`：預設 dry run，加上 `--apply` 才會寫入。每一類都必須明確給天數
  （`--prepared-body-days`、`--import-batch-days`、`--session-days`）；CLI 不讀上表的設定，
  用於臨時盤點或手動清理。每日服務呼叫 `scheduled_cleanup`，以上表設定直接套用。
  - prepared：只把 body 設為 NULL，不刪除列，保留 descriptor、digest 與 receipt。條件是 work 已結案
    （completed、partial、failed、cancelled、calendar_closed）且結案超過 N 天、沒有未結算的 attempt，
    而且**每一筆** delivery 都有 terminal run 的 receipt。任何一筆未被接受或是人工 waiver 時，
    整個 work 的 body 都保留。`prepared_map` 已能處理 NULL body。
  - import batch：只刪除過期超過 N 天的 `previewed`，`applied` 保留。
  - session：只刪除過期或撤銷超過 N 天的列。
  - 可以重複執行；不做 VACUUM。body 清為 NULL 後空間由 autovacuum 回收再利用，資料檔不會縮小。

在 ingest container 內執行：`python scripts/retention.py inspect`。每日服務的結果記在
`findb-raw-cleanup` 的 log（`Retention cleanup completed: {...}`）。

2026-11-11 staging 以相同期限手動 `--apply`：prepared body 7,940 列（8.53 MiB）清為 NULL、刪除
1 筆 previewed batch 與 61 筆 session；再執行一次 dry run，三類皆為 0。Production 盤點共 343 MiB
prepared body，其中約 316 MiB 已超過 7 天；兩環境盤點數據見 #494。

## Production universe與historical backfill

Historical capability 現由 provider catalog 推導；Source execution-config 是 daily 設定發布，
不作 historical request／claim／acquisition 的額外 gate。四個 historical runners 與 production
planner 改讀 image 隨附的 `execution_descriptors.v1.json`，不讀 daily schedule／full-market config；
FinLab `tw_etf_eod` historical 已開放（`executable`，沒有靜態 universe descriptor）。PR4 已移除 image／build／release 的舊排程檔；daily 改讀 Backend 快照，historical 不依賴該 API。

Twelve Data 與 FinLab 新建的 historical request 不再使用靜態清單：每個日期凍結該日 Full plan 的 release
（沒有 plan 時為該日以前最新 published release）中已上市的 mapped 成員，Fetcher 以 Full daily adapter
逐成員抓取，結果可為 `partial` 並附逐成員 evidence；規則見
[以 historical 回補 Full 缺漏](full_market.md#以-historical-回補-full-缺漏twelve-datafinlab)。以下靜態 reviewed
artifact 只供本變更前建立、尚未排空的 request 與 planner 估算使用：

- Twelve Data historical descriptor 指向 `twelve_data_nasdaq_100_2026_09_14.v2.json`。快照保留公開QQQ持股中的全部101個
  securities（包括多股類），每次最多claim 5個symbol；scheduler poll及批次間隔不得短於60秒。
- FinLab使用`finlab_tw50_2026_09_21.v2.json`，固定50檔、每個交易日只讀五個SDK datasets，
  完整50-symbol grid成立後才可送出full snapshot。
- 已退役 Shioaji 的歷史證據曾使用`shioaji_tw50_2026_09_21.v2.json`，50檔股票與`0050`、`0056`、`006201`
  分成兩個sequence；每個provider request仍只處理一檔且`SHIOAJI_SIMULATION=true`。

三份v2 manifest均綁定來源URL、effective date、來源摘要與依序正規化symbol的SHA-256。
來源更新必須以PR替換整份快照並重跑loader tests；checksum不符、重複symbol、錯誤筆數、未知欄位或
production/staging target混用都會fail closed。`source_sha256`是該次review紀錄的
`source_url`、effective date、篩選規則／定審異動所形成之canonical evidence摘要；
`symbols_sha256`是依manifest順序、每行一個canonical symbol（Shioaji為
`dataset_key:symbol`）且結尾換行的摘要。

Production EOD historical control plane允許最近366個calendar days；Shioaji 已停止新申請，既有 minute request 的 31 天視窗語意保留。
仍依published market calendar與 provider policy 建立
open-day work items；舊 request 的 Twelve Data逐symbol、FinLab逐trade date保存lease/checkpoint並沿用raw-first、
immutable idempotency與Source terminal驗證。Twelve Data historical與daily同樣以`adjust=none`抓取
實際成交價；回補可依較新的`fetched_at`覆寫同來源舊列，但不會把拆股調整價寫入canonical。
本變更前以`adjust=splits`完成的回補列，若之後已有拆股，可能與實際成交價不同，須依原raw
與拆股日期個別核對後重跑，不得整批覆寫。切換前已保存回應或prepared body但尚未完成的
historical work會保留原bytes並以manual review結束，處理方式見[價格基準切換與舊回應快取](full_market.md#價格基準切換與舊回應快取)。Shioaji 舊 `tw_equity_minute` 與 `tw_etf_minute` 的 manifest／receipts 保留供查核，不再提供新回補。
建立正式
request前先使用`findb-fetch-backfill-plan --provider ... --dataset-key ... --start-date ...
--end-date ...`（預設dry-run）保存日期、預估calls／rows及無效日期；只有核對計畫後才加
`--deliver`，需要等待terminal state時再加`--wait`。CLI使用production Admin machine key呼叫既有
preview／create／list API，不持有provider secret；Fetcher worker只消費
已核准request。任何scheduler在backfill期間仍保持`stopped`，未驗證Raw R2、Source、outbox、DQ、
canonical與Serve前不可擴大下一批。

`APP_ENVIRONMENT=production` 是 planner 的環境前提；`--schedule-file` 已移除，
`FETCHER_HISTORICAL_SCHEDULE_FILE` 不再被 runner 或 planner 使用，沒有舊檔 fallback。
Twelve Data／FinLab 的 `--universe-file` 只替換本次 planner 的估算輸入，不修改 worker 或 Backend
request 的 universe；應使用與 worker 相同的 reviewed artifact。FinLab 原有
`FETCHER_FINLAB_UNIVERSE_FILE` 保留，runner 與 planner 預設共同使用；未設定時讀 descriptor 的同環境預設。
FactSet／TAIFEX 的成員由既有 Backend cohort／日期資料決定，不接受 planner universe override。
Twelve Data／FinLab 新 request 的實際成員是 Backend 凍結的 Full release，planner 的靜態 universe 只是估算；
FinLab `tw_etf_eod` 沒有靜態 universe，planner 以每日 5 次 SDK 呼叫估算，不估筆數。

移除排程檔依賴不會擴大日期、權限或歷史 scope。新 Twelve Data／FinLab historical payload 的
`delivery.scheduled_for` 為 request 交易日 UTC 00:00 的日期標記；request/job keys、target date、
重試／等待參數、frozen universe 與 attempt 不改算，已有 prepared bytes 保持原樣。
Daily API 失敗不阻擋 historical 啟動，但 Source claim／lease／acquisition／quota／calendar 與
原 cutoff 檢查仍須成功。Source unavailable 時依既有等待／復原流程保留 prepared delivery。

### Shioaji 股票／ETF 分鐘回補

Shioaji historical consumer 已移除，Backend 的新申請 capability 也已移除。即使 dataset 仍為
active、保留 scheduler／Source scope，scopes API 也不再列出 `tw_equity_minute` 或 `tw_etf_minute`
的 Shioaji 選項；有效日期的 preview 回報 `provider_dataset_unsupported`，新 request 回傳 HTTP 422。
Dashboard 使用同一 scopes API，因此不需另維護退役選項清單。TW 分 K 回補待替代 provider 上線。

既有 requests／items、frozen manifest、prepared bytes、receipts、repair／ACK、canonical 分 K 與
checkpoint 均保留；Admin 歷史列表／分頁、既有取消操作與 Serve 分 K 查詢維持原有權限與語意。
相同 request key、scope 與仍在原日期視窗內的重送只回傳原 request，不重開 failed work 或新增工作。
歷史 request 的台北日期與 31 天視窗仍用於原證據解讀及收尾，不代表恢復抓取能力。

此階段僅關閉新回補申請並調整 cutover inventory，未撤銷 Source credentials、刪除 scheduler／catalog
scope，亦未改寫既有 claim／交付收尾協定；無現行 Shioaji executor，不可將保留 API 當作重新啟用授權。
任何舊 queued／running work 或未交付 prepared bytes 都須先人工盤點；不藉此次發布自動取消、重試或刪除。
後續 scope／credential migration 與資源清理見 [backlog](../dev/backlog.md#full-market-external-acceptance)。

現行 providers 的 Historical CLI 收到`SIGTERM`／`SIGINT`後立即停止新日期claim，完成當前日期的raw-first交付與
terminal回報；idle時立即退出。部署停止期限為90秒，執行中的日期可能較久，應先等待request
terminal且沒有running item；scheduler停止狀態不代表historical worker已排空。若程序被強制終止，
原 provider owner 與 running item 保留；heartbeat 到期不釋放 owner，也不將 item 改為 pending。
只有重新取得相同 shared host-lock inode、確認原 child／descendants 已停止後，才可旋轉
generation／token 並恢復原 work。先排空原 prepared／accepted runs；需要重試時安全 close／
settle 至 FIFO 尾端，保留原日期 scope、immutable identity、checkpoint、receipts、budgets 與 cooldown。
日期僅保證首次由早到晚入列；延遲 retry 不阻擋同 request 的後續 runnable 日期，
因此完成日期可能不連續。請以每個 item／work 的 terminal evidence 判斷完成，不能只看最後日期。
不要手動刪除 raw／SQLite 或縮短 lease 來跳過此恢復邊界。僅已確認staging舊historical映像有exit `137`過渡例外，範圍與digest
見[部署流程](deployment.md)；新historical worker、日常scheduler、OOM或Docker Error仍fail closed。

維護 historical backfill API 與 service 測試時，涉及日期視窗或 claim／lease 的案例應明確使用
test-local、UTC-aware 固定 service clock，並由同一時鐘建立交易日期與 calendar fixtures；
claim 成功後須確認實際 item 與非空 lease token，再驗證 terminal state。固定時鐘僅限選用的
測試，不套用全域 autouse patch；日期邊界、provider 當地日期與過期案例仍須保留各自的時間設定。

## TAIFEX futures EOD 回補

`taifex / tw_futures_eod` 支援 TX、MTX、TMF、TE、TF 全部官方實際月／週／F 契約與
regular／after_hours；production 最近 366 個 calendar days，local／staging 最近 31 天。
日期邊界採 Asia/Taipei；日曆使用 TAIFEX published revision，缺少日期採 weekday default，
建立時凍結 snapshot；historical 已知休市日固定 skipped_calendar，避免把無報表誤報 no-data。當日必須已過 snapshot 收盤且至少 17:00 才能 claim／新抓取。
年度 ZIP、多年建庫、價差及 continuous 不在此路徑。

日常 Full runtime 持續依 first dates 與 frozen plans 補抓缺漏；指定歷史區間
使用既有 Admin／Dashboard「歷史回補」與 `findb-fetch-backfill-plan`，不建立或結清
daily plan。Historical 完成後，恢復的日常 runtime 仍自行交付／對帳。

1. 確認環境已完成 provider-work cutover、shared inode attestation 與 Source credential scope。
   Historical 與 daily 共用 owner，不要求停止 TAIFEX Full control。
2. 預覽 production CLI，檢查 scope、日期與 calendar source；預設不下載 provider。
   正常下載預估為每有效日期五次，rows 為 `null`（未知），實際重試可能增加 calls。
3. 加 `--deliver` 建立 request；可加 `--wait` 等待結果。Historical consumer 獨立於 daily control，
   仍需有效 Source credential、owner fence、已收盤日期與 account governor。
4. 驗證 raw manifest、全部 Source receipts／outbox、DQ、canonical 與 Serve。

Request 長時間 queued 時，先唯讀檢查 provider-work projection 的 FIFO head、scope、not_before、
owner heartbeat／recovery_reason 與 attested inode，並核對 consumer 存活。Historical item 的
lease 欄位只是 queue owner 投影，TTL 到期不能證明原 child 已死，也不會自動重派。

```bash
findb-fetch-backfill-plan --provider taifex --dataset-key tw_futures_eod \
  --start-date 2026-10-01 --end-date 2026-10-02
# 核對上述 dry-run 後，使用相同參數加 --deliver --wait。
```

CLI 與 runner 讀取生成的非排程 metadata；daily 與 historical 均已移除
`FETCHER_FULL_MARKET_CONFIG` 及舊 schedule manifest 的 runtime／build 依賴。Daily 每輪向
Backend 取得排程；historical 直接使用 request 日期、frozen scope 與原有 acquisition authorization，
不要求 daily schedule API 成功或 daily controls 為 running。
Staging／local 亦可直接使用 Admin preview／create 及
`findb-fetch-historical-backfill --provider taifex --run-forever`。

每日期按商品下載指定日 CSV，exact CP950 bytes以`.csv`及`text/csv; charset=big5`先存 R2 再解析；整日 manifest 凍結 item／request、
trade date、fetched_at、五商品 raw ref／SHA-256 與完整契約／時段 observations。
Historical completion 接受商品報表的 content-addressed `.csv` 引用，並相容既有 `.json`
報表 checkpoint；整日 manifest 仍只接受 `.json`。兩類引用都驗證 SHA-256 檔名、大小限制與
dataset 範圍，manifest 另核對完整證據的 SHA-256 與實際大小。
若 item 顯示 `COMPLETION_EVIDENCE_INVALID` 且沒有 run 關聯，應另查 checkpoint
receipt 與 Source run；最後結算失敗不代表抓取或 canonical 寫入失敗。修正後依下述規則另建
request key 重跑，保留原 failed item 與 checkpoint，不直接改寫完成狀態。
部署完成證據驗證的相容性修正不會自動重試 terminal failed request；新 request 應僅涵蓋
未完成區間，並再次驗證 data／no-data 證據與 Source terminal 結果。
有官方觀測值的列以單一 `futures_eod.v1` backfill batch 經 Source → raw → outbox → worker →
canonical；重新抓取沿用 source precedence／fetched_at upsert，可更新官方修正值。
所有 OHLC／settlement／open interest 皆空且 volume 明確為零才是 no-data，保存官方列與
報表 references，不建立零價格行情。空回應、缺商品、未知格式或未知成交量不得算 no-data。
完成驗證要求 expected = data + no_data；有 data 時核對 run identity、raw manifest、成功筆數
與 canonical keys，全日皆為已證明 no-data 時不建立空 Source run。
完成證據另存 PostgreSQL historical item，獨立於 Source ingress raw 的 30 天保存期。
官方 CSV 與 manifest 依 R2 物件的 90 天 lifecycle 保存。R2 物件到期刪除後，historical item
仍保留完成證據摘要，原 CSV 與 manifest 的 bytes 隨物件刪除。

`findb-historical-taifex` 使用 `/var/lib/findb-taifex-fetcher/historical/state.sqlite3`，與日常
checkpoint 隔離並共用 account governor。商品 bytes／references、manifest、prepared body、
receipt 與重試狀態跨重啟／fenced owner recovery 保留；不得刪檔重置工作。Source key／origin／environment
改變時 checkpoint fail closed，應在排空後另建 request／checkpoint，保留原檔供稽核。
429、transport、5xx 與暫時 R2／Source 故障最多三次，預設等待 30／120 秒且遵守較晚的
平台 cooldown。授權期限不足時停止新 I/O、保存原狀態，安全 close／drain／settle 後將
下一 attempt 排至 FIFO 尾端，依 not_before 與原 account cooldown 再領取；不等待 TTL 到期重派。
若 heartbeat／程序狀態無法確認，保留 owner，依相同 shared inode 的 fenced recovery 處理。
格式、契約、大小限制、DQ 錯誤或重試耗盡會停止 request，保留已完成日期並取消後續日期；
修復後另建 request key 重跑未完成區間。取消／失效 lease 禁止新抓取，舊 token 不得完成。

## Full-market universe maintenance

Full-market 清單與既有 reviewed bounded manifests 分開。官方 US、HKEX、TWSE／TPEx 與 TAIFEX
快照保留不可變原始來源 checksum、effective date、分類、provider symbol／contract mapping；
不得在 runtime 靜默改集合。第一份完整 baseline 先由 Owner 依官方證據發布；之後相對已
published release 異動超過 2% 時需明確 exception approval，未通過的 candidate 不改
既有 plan。

TW ordinary equity 與各種 ETF 分開 dataset，ETF 不限 0050／0056 等 pilot；US ADR 與多股類，
HK GEM，以及 TAIFEX 實際月／週合約都需列入官方 expected。Mapping 不支援時保存 gap，
不能濾除。Futures expected 以 product／expiry／session 計數，日盤和盤後不合併；settlement
與 open interest 缺失保持 null。

Rollback 停止新 full-market scope，恢復前一 accepted bounded runtime及其 versioned universe；保留 canonical、
raw、prepared checkpoint 與原 daily gaps。不可刪除 gap 或改寫舊 plan 達成 complete；已有
schema migration 仍採 forward fix。Full-market 只補 activation 後缺口，不建立 continuous
futures 或 historical backfill 工作；既有 bounded EOD historical workflow 維持獨立操作。

## EOD default partition recovery

`market_data_eod_default`正常必須為空；application在寫入前會確認目標年度的
migration-owned partition。Staging monitoring以一筆為告警門檻，因此不要等待累積到較大
數量才處理。告警後：

1. 保存alarm時間、row count、distinct年份、目前Alembic revision與image digest；不要輸出價格資料。
2. 停止`ingest`、`dispatcher`與`worker`，建立RDS snapshot，確認沒有進行中的normalization job。
3. 由Alembic建立缺少的年度partition；不得從application runtime執行DDL。
4. 在transaction內將該年份資料由default partition搬至年度partition。先在clone驗證row count、
   primary key、`run_id` lineage與query plan，正式執行時設定bounded lock/statement timeout。
5. 驗證default partition歸零、年度partition count與搬移前相同、lineage orphan為零，再恢復writers。

不要直接detach或drop非空default partition，也不要用手寫大範圍DELETE清除告警。

## 執行紀錄與回復

每次維護保存image SHA、target、參數、開始／結束時間、pre／post counts、驗證與operator。
不把`DATABASE_URL`或secret貼入文件、log或shell history。不使用未pin外部結果直接覆寫
資料。回復優先採RDS snapshot或script明確提供的可逆流程，不手寫大範圍DELETE。

## Provider work 模式的 historical／raw rerun

所有環境須完成 [provider-work cutover](deployment.md#provider-work-切換)。
TAIFEX stopped-controls 維護窗口與獨立 historical TTL lease 已退役；
可在 Full desired running 時提交所有既有支援 scope 的回補。
它們依同 provider FIFO 排隊；日期由早到晚入列，daily／request 決策與證據不互相覆蓋。
可用 scope 以後端 capability matrix 與 active scheduler scope 為準；現行為 Twelve Data US EOD、
FinLab TW equity EOD、TAIFEX futures EOD、FactSet HK EOD。Shioaji minute 已停止新申請，
TW ETF EOD 仍不支援 historical。

Twelve Data／FinLab historical 會保存 frozen universe、native response、immutable prepared jobs 與所有 receipts；
Shioaji 保存全部 minute sequences，TAIFEX 保存五報表與 whole-day manifest。Queue retry 重用原 scope／budget，
不重新抓取已持久化的 prepared work。TAIFEX completion 依 durable actual canonical-write evidence，後續 upsert
改寫 canonical 最新 run_id 不會抹除原證據；格式／DQ error 保留 failed／manual，不當作正常 no_data。

Source／Admin 單筆與 bulk retained-raw rerun 都建立新的 audited run／work，維持原 raw body 與失敗 receipt；
排到 provider owner 後才 dispatch normalization。不能以 receipt replay 重設 terminal failed run，或繞過同 provider
的 active daily work。Dashboard「交付監控」的 provider queue 可查看等待順位、active work、retry time 與 heartbeat。

清理舊 compatibility protocol 留下的 queued 投影時，先核對 request／item 終態與原 daily plan，
不可僅由工作日期判定失效。經 Owner 指定取消的舊工作，須在同 provider transaction lock 下重查
owner idle、controls stopped、沒有在途 run，且 queued attempt 未開始、沒有 prepared delivery／receipt。
符合條件時，已結束的 historical 投影同步原 item 終態；經授權不再執行的舊 daily 投影標記 cancelled，
attempt 標記 settled 並保存 outcome。逐筆記錄具名 operator、原因、變更前後狀態與 audit，保留
request／item、daily plan、checkpoint、raw 與 canonical 資料；有待交付證據的工作須走原 drain 流程。
