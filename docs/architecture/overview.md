# FinDB 現行架構

## 系統與資料流

FinDB接收provider金融資料，保存可追溯的raw delivery，經DQ與normalization產生
canonical data，再由唯讀Serve API提供下游使用。

```text
Fetcher
  -> Source API
  -> ingestion_attempt
  -> raw payload + ingestion_run + normalization_job + outbox
  -> Dispatcher -> RabbitMQ -> Celery Worker
  -> Normalize + DQ + canonical tables
  -> Serve API / Admin API / Dashboard
```

Source只有在raw、run、job與outbox於同一PostgreSQL transaction commit後才回
`202 Accepted`。RabbitMQ是可重建的delivery layer；已接受工作的durable truth在
PostgreSQL。

## Runtime與release units

| Unit | Runtime | 責任 |
| --- | --- | --- |
| FinDB backend | `serve`、`ingest`、`dispatcher`、`worker`、`raw-cleanup` | API、durable queue orchestration、normalization、DQ、canonical與migration |
| Dashboard | FinDB EC2上的獨立image | 透過Serve/Admin API提供營運介面，不直接連DB |
| Fetcher | 獨立EC2上的Twelve Data、FinLab、Shioaji images | Provider抓取、contract mapping、Raw R2、retry、checkpoint與delivery status |
| Infrastructure | PostgreSQL RDS、RabbitMQ、nginx | Durable data、可重建delivery與TLS／routing |

FinDB backend與Dashboard同屬一個deployment unit；Fetcher為另一個deployment unit。
四個GitHub workflows分離FinDB CI/CD與Fetcher CI/CD。Contract變更採
backend-first expand/migrate/contract，不能假設兩個EC2同步更新。

## Active feeds

文件層級的active feed清單只在本節維護；最終真相是
`backend/scripts/seed_data.py`及對應Fetcher configs。

| Provider | Dataset | Staging範圍 |
| --- | --- | --- |
| `twelve_data` | `us_equity_eod` | Reviewed bounded US equity universe |
| `finlab` | `tw_equity_eod` | Reviewed bounded TW equity universe |
| `shioaji` | `tw_equity_minute` | `2330` pilot |
| `shioaji` | `tw_etf_minute` | `0050`、`0056`、`006201` pilot |

Canonical tables與Serve endpoints可保留歷史或預留read model；這不代表目前有對應
provider feed。新增資料域必須另案完成contract、registry、normalizer、DQ與Serve驗收。

## 資料責任

| 層級 | 儲存位置 | 規則 |
| --- | --- | --- |
| Provider raw object | Fetcher Raw R2 | Credential-free reference與checksum傳給FinDB；依retention policy保存 |
| Raw ingress | `raw.market_payload` | 預設保存30天，支援audit與rerun |
| Workflow | attempt、run、job、outbox | Durable acceptance、terminal state與recovery truth |
| Registry | dataset、scheduler、credential metadata、DQ | 長期治理狀態 |
| Canonical | instrument、calendar、EOD、minute及其他read models | 長期保存，Serve只讀 |

Raw與Canonical R2使用不同private buckets及credentials。Fetcher不得取得Canonical
credential；FinDB不得取得provider credential或Fetcher state。RDS不保存R2 secret或
presigned URL。

## 服務邊界

FinDB負責Source、Serve、Admin、idempotency、raw audit、queue orchestration、DQ、
canonical schema及migration；不負責provider登入、抓取排程或provider-specific mapping。

Fetcher負責provider adapter、限流、versioned contract、stable identity、Raw R2、
SQLite retry／lease／checkpoint及terminal status追蹤；只能透過HTTPS呼叫FinDB，不得
import backend ORM／normalizer、連FinDB DB或取得RabbitMQ／Admin credentials。

Dashboard只能透過API運作。公開lookup使用canonical Serve API；generated cache僅供
backend static／Admin cache maintenance。RAG、embedding、vector index與模型runtime屬
下游，不進入FinDB。

允許跨unit共享的內容只有published JSON Schema、contract manifest、無secret fixtures及
純validation工具；runtime config、DB code、provider SDK與credentials不得共享。

## Scheduler與市場日曆

PostgreSQL的`scheduler_control`與`scheduler_dataset`是排程definition、dataset mapping
及desired state的唯一權威。Dashboard Owner可修改desired state；Fetcher以
provider-scoped Source key輪詢
`POST /api/v1/source/scheduler-controls/{scheduler_key}/poll`並回報heartbeat。
控制面失聯、scope不符或definition與reviewed workload不一致時，Fetcher fail closed，不
建立新cycle；停止要求會讓目前cycle完成後不再啟動下一輪。

市場日曆以不可變的年度revision管理。Dashboard建立draft，只有Owner可publish或rollback。
Scheduler只使用
`GET /api/v1/serve/calendar/years/{market}/{year}`；未發布、不完整或不一致時回`404`
並fail closed。`settlement_only`不可觸發抓取，沒有static weekday fallback。

## 不可破壞的規則

- Serve API與serve role不得寫DB；人工修正只能經Admin API並留下audit。
- 所有資料來源只走versioned provider-neutral Source contract，不建立direct寫入路徑。
- DQ `severity=error`阻擋canonical write；warning可寫入但保留issue。
- 時間使用UTC-aware datetime，主鍵預設UUIDv7，DB access採async SQLAlchemy。
- Schema只能經Alembic改變；runtime只驗證revision，不執行`create_all()`。
- `dataset_key`是治理單位，`schema_id + version`是wire contract，`source`是provider。
- Idempotency key必須能由producer穩定重建。

## 故障模型與source of truth

- RabbitMQ中斷：Source仍可commit outbox，broker恢復後補送。
- Worker中斷：lease與DB reconciliation重建delivery。
- 重複delivery：相同key與內容回既有run；相同key不同內容回`409`。
- Schema不相容：留下attempt並拒絕，不建立raw／run／job。
- Migration不相容：舊image拒絕啟動，採forward fix。

精確來源：

- Routes：`backend/app/api/v1/`
- Contracts：`backend/app/schemas/ingress.py`、`contracts/`
- Workflow：`backend/app/services/ingestion.py`與queue services
- ORM／schema：`backend/app/models/`、`backend/migrations/`
- Topology／delivery：`docker-compose.prod.yml`、`.github/workflows/`
