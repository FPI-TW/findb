# Alembic Migration Workflow

## 原則

- ORM model與Alembic migration必須在同一個PR更新。
- Runtime `init_db()`只驗證revision與required tables，不執行 `create_all()`。
- Production schema是forward-only operation；downgrade只用於本機migration round-trip測試。
- Migration前停止所有DB writers，不能只停止HTTP ingest。
- 禁止手動修改production schema後不補migration。

## 本機流程

```bash
uv --directory backend run alembic current
uv --directory backend run alembic upgrade head
uv --directory backend run alembic revision --autogenerate -m "describe change"
uv --directory backend run alembic downgrade -1
uv --directory backend run alembic upgrade head
```

Autogenerate後必須人工審查：

- schema與table名稱
- enum、constraint與index
- PostgreSQL-specific options
- data backfill順序
- lock duration與大表rewrite
- upgrade/downgrade是否可逆
- default partition與existing rows

## 測試

至少執行：

```bash
make check
make test
uv --directory backend run alembic upgrade head
uv --directory backend run alembic current
```

涉及既有資料或partition時，在production-like clone或partial dump演練。驗證舊資料、
新寫入、Serve查詢與downtime估算，不只驗證空DB。

## Existing database baseline

只有schema已人工確認與某個revision完全一致時，才能 `alembic stamp`。Stamp不會執行
DDL；它不能用來跳過未知schema drift。

若remote DB不是head：

1. 備份並建立clone。
2. 比對revision與實際schema。
3. 在clone執行upgrade與application smoke tests。
4. 排定writer pause。
5. 執行production preflight後upgrade。

## Production rollout

1. 確認RDS snapshot/PITR與restore演練。
2. 暫停provider或確認安全retry。
3. 執行read-only DB preflight。
4. 停止ingest、dispatcher、worker、raw-cleanup及所有one-off writers。
5. 執行 `alembic upgrade head` 與 `alembic current`。
6. 啟動queue、worker、ingest，再啟動/確認Serve。
7. 執行health、queue與fixed-idempotency smoke test。

若preflight失敗，不停止既有服務。

## 失敗處置

- 不刪除raw/job/outbox。
- 不以 `stamp head`掩蓋失敗migration。
- 不直接啟動revision較舊的image；startup會拒絕且舊程式可能不懂新schema。
- 能在目前schema上運作的修正版image是首選。
- 只有已演練且不會遺失資料的downgrade才可在維護窗口考慮。
