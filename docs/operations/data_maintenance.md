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
backend/app/static/data/macro-series.json
```

Canonical data改變後使用`backend/scripts/generate_instrument_cache.py`重生。Cache失敗時
修正canonical data或generator，不直接編輯JSON。

## Staging data reset

Reset可移除mutable canonical、workflow與PostgreSQL raw，建立bounded pilot空白基線；
它不是rollback。必須使用`backend/scripts/reset_staging_legacy_data.py`的兩階段安全流程：

1. 確認database／host是staging，記錄Alembic revision、image SHA與protected tables。
2. 將scheduler desired state切為`stopped`，停止`ingest`、`dispatcher`、`worker`、
   `raw-cleanup`，確認queue、DLQ與active tasks為空。
3. 以同一target connection執行唯讀盤點，保存counts與connection fingerprint。
4. 核對範圍後使用script要求的staging confirmation及writer-stopped assertion執行apply。
5. 驗證mutable targets為零、protected tables不變且lineage無orphan。
6. 同步重置provider SQLite checkpoint，重生cache並執行bounded acceptance。

`dataset_registry`、source／API credentials及`alembic_version`屬設定／schema state，必須
保留。`market_data_eod.run_id`、raw run ID與DQ run ID沒有完整DB FK／cascade，不能假設
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
