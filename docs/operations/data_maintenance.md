# Data Maintenance

所有維護程式必須使用獨立one-off container或工作資源，不在serve process內執行。
預設先dry-run；會改production資料的命令需先備份並保留輸出。

## Partial dump與local seed

設定：

```text
backend/configs/partial_dump.yaml
```

工具：

```text
backend/scripts/partial_dump.py
backend/scripts/seed_upsert.py
```

常用命令：

```bash
uv --directory backend run python scripts/dev.py seed-upsert
uv --directory backend run python scripts/dev.py seed-upsert --truncate
```

- Seed package必須pin明確artifact/version，避免「latest」隨時間改變。
- 預設只匯出必要schema與抽樣資料；raw或敏感欄位需明確納入並匿名化。
- `--truncate` 會移除/清空seed範圍資料，只能對local development DB使用。
- Schema變更後重生package並驗證manifest、row counts與FK依賴順序。
- CSV與大型seed artifacts依repo規則使用Git LFS；manifest/schema/load script保留文字。

## Instrument名稱與routing修正

工具：

```text
backend/scripts/backfill_instrument_names.py
backend/scripts/backfill_world_names.py
backend/scripts/fix_misrouted_tw_futures.py
backend/scripts/cleanup_stale_instruments.py
```

執行規則：

1. 確認image SHA包含預期script。
2. 建立RDS snapshot。
3. 以one-off container先跑dry-run。
4. 保存候選筆數、unmatched、reclassified與deleted摘要。
5. 人工核對後才使用 `--apply`。
6. 執行Serve查詢與資料完整性SQL。
7. 重生instrument cache。

TW舊Big5亂碼只有在已確認候選集合時才使用 `--overwrite-existing`。Routing cleanup
可能搬移或刪除重複instrument，必須先核對child row影響。

各script的完整參數以 `--help` 與程式碼為準，不在文件複製可能過期的預估筆數。

## Static cache

Instrument與macro lookup JSON是generated artifacts，不是source of truth：

```text
backend/app/static/data/instruments.json
backend/app/static/data/macro-series.json
```

Canonical data變更或backfill後執行：

```text
backend/scripts/generate_instrument_cache.py
```

Production deploy會在服務健康後重生instrument cache。Cache失敗不能以直接編輯JSON
取代；應修正canonical data或generator。

## Raw retention

- 正式政策為PostgreSQL `raw.market_payload`與R2 raw object都保存30天。
- PostgreSQL以`RAW_RETENTION_ENABLED=true`、`RAW_RETENTION_DAYS=30`執行每日清理。
- R2由Cloudflare lifecycle在object滿30天後刪除，bucket lock保護前7天。
- R2 lifecycle依provider prefix套用；Fetcher與Backend application不主動刪除R2 object。
- `RAW_RETENTION_ENABLED=false`只供事故處理時暫停PostgreSQL自動刪除。
- 啟用前確認queue reconciliation、rerun保留期與稽核需求。
- `RAW_RETENTION_DAYS`必須長於正常延遲、事故調查與contract migration窗口。
- Queue backlog、expired leases或migration期間不要清理相關raw payload。
- Raw刪除不影響canonical，但會失去rerun與provider payload audit能力。

## Production執行安全

- 不在serve container內跑長時間writer。
- 不把production `DATABASE_URL`貼進shell history或文件。
- 不用未pin的外部資料來源結果直接覆寫既有值。
- 每次操作記錄image SHA、參數、開始/結束時間、影響筆數與驗證結果。
- 回滾優先使用DB snapshot或script明確提供的可逆流程，不手寫大範圍DELETE。
