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
backend/scripts/fix_misrouted_tw_futures.py
backend/scripts/cleanup_stale_instruments.py
```

1. 確認candidate image包含預期script並建立RDS snapshot。
2. 以one-off container執行dry-run，保存candidate、unmatched、reclassified與deleted摘要。
3. 核對child rows與市場routing後才使用`--apply`。
4. 執行Serve與資料完整性檢查，再重生instrument cache。

TW舊Big5亂碼只有在候選集合已確認時才使用`--overwrite-existing`。Routing cleanup可能
搬移或刪除重複instrument，不得跳過FK與下游影響檢查。

`fix_misrouted_tw_futures.py`是deprecated的歷史殘留修復工具，只用於檢查或修正舊資料的
TW futures誤分類；預設必須dry-run，確認候選集合後才能使用`--apply`。後續需先確認staging
與production都沒有候選資料，再以獨立程式清理工作移除，不能在本次文件整理中直接刪除。

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

## 執行紀錄與回復

每次維護保存image SHA、target、參數、開始／結束時間、pre／post counts、驗證與operator。
不把`DATABASE_URL`或secret貼入文件、log或shell history。不使用未pin外部結果直接覆寫
資料。回復優先採RDS snapshot或script明確提供的可逆流程，不手寫大範圍DELETE。
