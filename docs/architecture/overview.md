# FinDB 現行架構

> 核對日期：2026-07-24

## 系統目標

FinDB 接收多個 provider 的金融資料，保存可追溯的 raw delivery，經過 DQ 與
normalization 後產生 canonical data，並透過唯讀 Serve API 提供下游使用。

核心資料流：

```text
Fetcher
  -> Source API
  -> ingestion_attempt
  -> raw payload + ingestion_run + normalization_job + outbox
  -> Outbox Dispatcher
  -> RabbitMQ
  -> Celery Worker
  -> Normalize + DQ + canonical tables
  -> Serve API / Admin API
```

Source API 只有在 raw、run、job 與 outbox 同一個 PostgreSQL transaction commit
後才回 `202 Accepted`。RabbitMQ 是可重建的 delivery layer；已接受工作的 durable
truth 位於 PostgreSQL。

## Production 角色

Production 使用同一個 backend image，依 `APP_ROLE` 與 Compose service 分離 workload：

| 角色 | 職責 | 寫入 DB |
| --- | --- | --- |
| `serve` | Serve API、公開靜態資料與健康檢查 | 否 |
| `ingest` | Source API、Admin API、接受 delivery | 是 |
| `dispatcher` | 將 transactional outbox 發布到 RabbitMQ | 是 |
| `worker` | 執行 normalization、DQ 與 canonical upsert | 是 |
| `rabbitmq` | 保存待處理 delivery；不是真相來源 | 不適用 |
| `dashboard` | 營運查詢與管理介面 | 只透過 API |
| `nginx` | TLS、路由、Source allowlist、proxy headers | 否 |
| `raw-cleanup` | 依 retention policy 清理 raw payload | 是 |

`serve` 與 `ingest` 使用獨立 DB pool，避免大量 ingest 搶占 read path 資源。Serve
handler 不得新增寫入行為，未來才能安全指向 read replica。

## 資料層

| 層級 | 主要資料 | 保存策略 |
| --- | --- | --- |
| Raw | `raw.market_payload` | 預設啟用清理並保存30天，由 `RAW_RETENTION_ENABLED` 與 `RAW_RETENTION_DAYS` 控制 |
| Workflow | `ingestion_attempt`、`ingestion_run`、`normalization_job`、`normalization_outbox` | durable audit 與 recovery truth |
| Registry | `dataset_registry`、source clients、API keys、`dq_issue` | 長期保存 |
| Canonical | instruments、calendar、EOD、corporate actions、macro、futures、bonds、stats | 長期保存 |

Provider原始檔由Fetcher寫入R2；R2 lifecycle保存30天、bucket lock保護前7天。
FinDB contract只帶不含credentials的`source_raw_ref`與checksum。

## 寫入路徑

1. Fetcher 把 provider-specific payload 轉成 provider-neutral ingress contract。
2. Source API 驗證 client identity、dataset scope、rate limit、schema 與 delivery policy。
3. Canonical endpoint 先建立 `ingestion_attempt`，使被拒絕的 delivery 也可追蹤。
4. 接受後原子寫入 raw、run、job、outbox並回 `202`。
5. Dispatcher 發布 delivery；worker 使用 late ack 執行。
6. Worker 以 dataset advisory lock 控制同 dataset 的執行順序。
7. Normalizer 執行 DQ、canonical upsert，並在同一 transaction 更新 terminal state。

所有資料來源必須經 Source API 入庫。腳本、Fetcher、Admin 或 worker 都不得建立第二條
直接寫 canonical tables 的 ingestion path。

## 讀取與修正路徑

- Serve API 只讀 canonical tables。
- Dashboard 與其他消費者透過 Serve API 讀資料。
- 人工修正透過 Admin API，必須留下 correction audit。
- DQ `severity="error"` 阻擋 canonical write；warning 允許寫入但留下 issue。
- RAG、embedding、vector index 與模型執行屬下游服務，不進入 FinDB。

## Schema 與識別規則

- 時間一律為 UTC-aware datetime。
- 主鍵預設使用 UUIDv7。
- DB access 使用 async SQLAlchemy。
- Schema 變更只能透過 Alembic；runtime 不執行 `create_all()`。
- `dataset_key` 表示治理單位，不能包含 provider 名稱。
- `schema_id + schema_version` 表示 payload 契約。
- `source` 表示實際 provider。
- Idempotency key 必須可由 Fetcher 穩定重建。

## 已發布、尚未啟用的 minute 契約

`market_minute.v1` 與 `market_minute_archive.v1` 已作為 machine-readable
contract 發布，minute workflow/canonical DB 骨架亦已建立，但目前沒有 minute
normalizer、runtime persistence、Serve query 或 Export API。RDS hot 61 monthly
partitions automation、永久 Canonical R2 與 archive publication barrier 均屬後續實作；
Serve 保持唯讀，未來 Export API 必須與 Serve 分離。細節見
[台灣一分鐘資料契約](tw-minute-data.md)。

## 故障模型

- RabbitMQ 中斷：Source API 仍可接受並寫入 outbox，恢復後補送。
- Worker 中斷：job lease 與 DB reconciliation 重新建立 delivery。
- 重複 delivery：相同 key 與內容回既有 run；相同 key、不同內容回 `409`。
- Schema 不相容：先拒絕並留下 attempt，不建立 raw/run/job。
- DB migration 不相容：舊 image 的 revision check 會拒絕啟動，採 forward fix。

## Source of truth

- API routes：`backend/app/api/v1/`
- Ingress contracts：`backend/app/schemas/ingress.py`
- Contract registry：`backend/app/services/ingress_contracts.py`
- Workflow：`backend/app/services/ingestion.py`、task queue 與 dispatcher modules
- ORM：`backend/app/models/`
- DB schema：`backend/migrations/`
- Production topology：`docker-compose.prod.yml`
- CI/CD：`.github/workflows/findb-ci.yml`、`.github/workflows/findb-cd.yml`、
  `.github/workflows/fetcher-ci.yml`、`.github/workflows/fetcher-cd.yml`
