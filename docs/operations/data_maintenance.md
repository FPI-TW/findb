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

Reset可移除mutable canonical、workflow與PostgreSQL raw，建立bounded pilot空白基線；
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
Provider queue 的 work／attempt／receipt 屬 mutable workflow，一起清除；owner slots 的 installed inode
與 monotonic fencing generation 保留。任何 active owner 都拒絕 reset，即使 heartbeat 已過期。
為保留 owner→attempt 的 FK，先明確刪除 receipt／attempt／work，再在同一 transaction explicit
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

- PostgreSQL `raw.market_payload`與Raw R2正式保存期為30天。
- PostgreSQL由`RAW_RETENTION_ENABLED`／`RAW_RETENTION_DAYS`每日清理。
- R2由Cloudflare lifecycle刪除，bucket lock保護前7天；application不主動刪object。
- 事故、queue recovery或migration期間先暫停cleanup，避免失去rerun與audit evidence。
- Retention必須長於正常延遲、事故調查及contract migration窗口。
- Raw刪除不影響canonical，但會失去rerun與provider payload audit能力。

## Production universe與EOD backfill

Production universe是reviewed、versioned artifact，不在runtime自動換股：

- Twelve Data使用`daily_scheduler.production.v3.json`與
  `twelve_data_nasdaq_100_2026_09_14.v2.json`。快照保留公開QQQ持股中的全部101個
  securities（包括多股類），每次最多claim 5個symbol；scheduler poll及批次間隔不得短於60秒。
- FinLab使用`finlab_tw50_2026_09_21.v2.json`，固定50檔、每個交易日只讀五個SDK datasets，
  完整50-symbol grid成立後才可送出full snapshot。
- Shioaji使用`shioaji_tw50_2026_09_21.v2.json`，50檔股票與`0050`、`0056`、`006201`
  分成兩個sequence；每個provider request仍只處理一檔且`SHIOAJI_SIMULATION=true`。

三份v2 manifest均綁定來源URL、effective date、來源摘要與依序正規化symbol的SHA-256。
來源更新必須以PR替換整份快照並重跑loader tests；checksum不符、重複symbol、錯誤筆數、未知欄位或
production/staging target混用都會fail closed。`source_sha256`是該次review紀錄的
`source_url`、effective date、篩選規則／定審異動所形成之canonical evidence摘要；
`symbols_sha256`是依manifest順序、每行一個canonical symbol（Shioaji為
`dataset_key:symbol`）且結尾換行的摘要。

Production EOD control plane允許最近366個calendar days，仍依published market calendar只建立
open-day work items；Twelve Data逐symbol、FinLab逐trade date保存lease/checkpoint並沿用raw-first、
immutable idempotency與Source terminal驗證。Production不接受Shioaji historical scope。建立正式
request前先使用`findb-fetch-backfill-plan --provider ... --dataset-key ... --start-date ...
--end-date ...`（預設dry-run）保存日期、預估calls／rows及無效日期；只有核對計畫後才加
`--deliver`，需要等待terminal state時再加`--wait`。CLI使用production Admin machine key呼叫既有
preview／create／list API，不持有provider secret；Fetcher worker只消費
已核准request。任何scheduler在backfill期間仍保持`stopped`，未驗證Raw R2、Source、outbox、DQ、
canonical與Serve前不可擴大下一批。

Historical CLI收到`SIGTERM`／`SIGINT`後立即停止新日期claim，完成當前日期的raw-first交付與
terminal回報；idle時立即退出。部署停止期限仍為30秒，執行中的日期可能較久，應先等待request
terminal且沒有running item；scheduler停止狀態不代表historical worker已排空。若程序被強制終止，
running item保留到15分鐘lease到期，後續claim將其改為pending並核發新token／增加attempt；
同一日期交付保持immutable identity，舊token不能覆寫重試結果。不要手動刪除raw、SQLite或
縮短lease以跳過此恢復邊界。僅已確認staging舊historical映像有exit `137`過渡例外，範圍與digest
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

日常 Pilot／Full runtime 持續依自己的 first dates 與 frozen plans 補抓缺漏；指定歷史區間
使用既有 Admin／Dashboard「歷史回補」與 `findb-fetch-backfill-plan`，不建立或結清另一
profile 的 daily plan。Historical 完成後，恢復的日常 runtime 仍自行交付／對帳。

1. 停止 **TAIFEX** Pilot／Full controls，等待 observed stopped 並排空在途 normalization。
   Backend 在 claim／每次新 acquisition authorization 重查 controls；不要求停止其他 providers。
2. 使用下列 production CLI 預覽，檢查 scope、日期與 calendar source；預設不下載 provider。
   正常下載預估為每有效日期五次，rows 為 `null`（未知），實際重試可能增加 calls。
3. 加 `--deliver` 建立 request；可加 `--wait` 等待結果。歷史 consumer 獨立於 Full flag，
   仍需有效 Source credential／dataset scope、lease 與 account governor。
4. 驗證 raw manifest、Source run／outbox、DQ、canonical 與 Serve，才恢復 TAIFEX controls。

若 request 長時間維持 `queued`，先唯讀檢查日期 item 的 `lease_attempt`、`started_at` 與
`run_id`；`lease_attempt=0` 且其餘兩者為空表示尚未領取。建立 request 成功不代表已進入
維護窗口：所有未 retired 的 TAIFEX controls 都須同時滿足 `desired_state=stopped` 與
`observed_state=stopped`，且沒有屬於其他 work item 的 queued／pending／processing／retrying
TAIFEX `tw_futures_eod` ingestion run，才可領取已收盤日期。Pilot 在兩次 cycle 間暫時回報
observed stopped，若 desired 仍為 running，仍不符合條件。Historical worker 空閒時每
15 秒輪詢一次；應另核對 consumer 存活與最近 claim 存取紀錄，不能只由 queued 推定 worker 故障。

```bash
findb-fetch-backfill-plan --provider taifex --dataset-key tw_futures_eod \
  --start-date 2026-10-01 --end-date 2026-10-02
# 核對上述 dry-run 後，使用相同參數加 --deliver --wait。
```

CLI 與 runner 讀取共用 `full_market.<environment>.v1.json`；可用
`FETCHER_FULL_MARKET_CONFIG` 指定同環境檔案，不依賴舊 bounded scheduler manifest。
Staging／local 亦可直接使用 Admin preview／create 及
`findb-fetch-historical-backfill --provider taifex --run-forever`。

每日期按商品下載指定日 CSV，exact bytes 先存 R2 再解析；整日 manifest 凍結 item／request、
trade date、fetched_at、五商品 raw ref／SHA-256 與完整契約／時段 observations。
有官方觀測值的列以單一 `futures_eod.v1` backfill batch 經 Source → raw → outbox → worker →
canonical；重新抓取沿用 source precedence／fetched_at upsert，可更新官方修正值。
所有 OHLC／settlement／open interest 皆空且 volume 明確為零才是 no-data，保存官方列與
報表 references，不建立零價格行情。空回應、缺商品、未知格式或未知成交量不得算 no-data。
完成驗證要求 expected = data + no_data；有 data 時核對 run identity、raw manifest、成功筆數
與 canonical keys，全日皆為已證明 no-data 時不建立空 Source run。
完成證據另存 PostgreSQL historical item，不受 30 天 raw retention 影響；R2 到期後仍無法讀回
原 CSV，證據摘要不取代原始檔。

`findb-historical-taifex` 使用 `/var/lib/findb-taifex-fetcher/historical/state.sqlite3`，與日常
checkpoint 隔離並共用 account governor。商品 bytes／references、manifest、prepared body、
receipt 與重試狀態跨重啟／lease reclaim 保留；不得刪檔重置工作。Source key／origin／environment
改變時 checkpoint fail closed，應在排空後另建 request／checkpoint，保留原檔供稽核。
429、transport、5xx 與暫時 R2／Source 故障最多三次，預設等待 30／120 秒且遵守較晚的
平台 cooldown；不足剩餘 lease 時保存狀態等待 15 分鐘 lease 到期重新 claim。
格式、契約、大小限制、DQ 錯誤或重試耗盡會停止 request，保留已完成日期並取消後續日期；
修復後另建 request key 重跑未完成區間。取消／失效 lease 禁止新抓取，舊 token 不得完成。

## Full-market universe maintenance

Full-market 清單與既有 reviewed bounded manifests 分開。官方 US、HKEX、TWSE／TPEx 與 TAIFEX
快照保留不可變原始來源 checksum、effective date、分類、provider symbol／contract mapping；
不得在 runtime 靜默改集合。第一份完整 baseline 先由 Owner 依官方證據發布；之後相對已
published release 異動超過 20 或 2% 時需明確 exception approval，未通過的 candidate 不改
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

當環境完成 [provider-work cutover](deployment.md#provider-work-切換) 並同步啟用
`PROVIDER_WORK_REQUIRED=true` 後，本文前述 TAIFEX stopped-controls 維護窗口與獨立 historical TTL lease
只屬舊 compatibility protocol。新模式可在 Pilot／Full desired running 時提交所有既有支援 scope 的回補。
它們依同 provider FIFO 排隊；日期由早到晚入列，profile／request 決策與證據不互相覆蓋。
本分支未新增 HK／TW ETF EOD historical、未開放 production Shioaji，也尚未啟用任何遠端環境。

Twelve Data／FinLab historical 會保存 frozen universe、native response、immutable prepared jobs 與所有 receipts；
Shioaji 保存全部 minute sequences，TAIFEX 保存五報表與 whole-day manifest。Queue retry 重用原 scope／budget，
不重新抓取已持久化的 prepared work。TAIFEX completion 依 durable actual canonical-write evidence，後續 upsert
改寫 canonical 最新 run_id 不會抹除原證據；格式／DQ error 保留 failed／manual，不當作正常 no_data。

Source／Admin 單筆與 bulk retained-raw rerun 都建立新的 audited run／work，維持原 raw body 與失敗 receipt；
排到 provider owner 後才 dispatch normalization。不能以 receipt replay 重設 terminal failed run，或繞過同 provider
的 active daily work。Dashboard「交付監控」的 provider queue 可查看等待順位、active work、retry time 與 heartbeat。
