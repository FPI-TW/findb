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
target-aware registry provisioning及`fetcher/configs/staging_provider_pilots.v1.json`。

| Provider | Dataset | Staging範圍 |
| --- | --- | --- |
| `twelve_data` | `us_equity_eod` | `AAPL`、`MSFT` daily pilot |
| `finlab` | `tw_equity_eod` | `2330`、`2317` daily pilot |
| `shioaji` | `tw_equity_minute` | `2330` pilot |
| `shioaji` | `tw_etf_minute` | `0050` pilot |
| `taifex` | `tw_futures_eod` | `TX`、`MTX` 各一個實際近月合約、兩個 sessions |

五個 feed 只驗證最新已完成交易日，不建立 historical backfill。TAIFEX staging override 使用
`full_market.required=false`、`enabled=false`；Full market 由環境旗標與 admission 獨立授權。
Serve catalog由active `dataset_registry`動態解析，公開US/TW equity EOD、TW
equity/ETF minute及實際 futures EOD。只有至少符合一個active scope的商品會出現在Serve instruments。Canonical
tables可保留inactive歷史資料，但不因此產生公開route或catalog項目。新增資料域必須另案完成
contract、registry、normalizer、DQ與Serve驗收。

## Full-market governance

Full market 在 local/staging/production 皆為 opt-in，預設 false。Registry acceptance 與 acquisition
admission 分離：Source 仍驗證 dataset existence/active/provider/scope，Full-only feeds 普通 ingress
不接受，需 `fp1` frozen plan。Owner 手動 scheduler start 凍結當時 ready feeds，首日為 exchange-local
今天且永不重設；合法 legacy cutoff 保留。Trusted enrollment 綁定實際 runtime、artifact/config/account
quota、Source identity、evidence digest 與 expiry，Source report 只能 acknowledgement。
操作與部署/rollback contracts 見 [runbook](../operations/full_market.md)。

| Provider | 目標範圍 | Contract |
| --- | --- | --- |
| `twelve_data` | US 主要交易所普通股與 ADR；HK 主板與 GEM | `market_eod.v1` |
| `finlab` | TW 上市／上櫃普通股與各類 ETF 日線 | `market_eod.v1` |
| `shioaji` | 相同 TW 股票／ETF 的一分鐘 bars | `market_minute.v1` |
| `taifex` | TX、MTX、TMF、TE、TF 實際月／週到期合約，日盤與盤後分開 | `futures_eod.v1` |

Universe 由官方清單建立 immutable release，保留來源 URL、checksum、觀測時間、effective
date、classification 與 provider mapping。第一份 baseline 需要 Owner 明確 audited approval；
相對上一份 published release 異動超過 20 個成員或 2% 時保留 candidate，須明確 exception
approval 才可發布。Mapping gap 仍列為 expected，不能刪除成員來降低分母。

每個實際開市日建立 frozen daily delivery plan，以 release 固定成員、交易日與 work item。
完整度恆等式是 `expected = data + no_data + missing + blocked`；data 只計入成功 canonical
lineage，no_data 只接受停牌／無成交的 durable source evidence。成功部分立即可查，partial
coverage 不等同全日 complete；mapping、quota、permission 或 source error 均保留 gap。

啟用從 activation date 向前運作，只追補啟用後缺口；full-market 不建立歷史回補或連續期貨。
TAIFEX trade date 使用交易所歸屬日，盤後不由 timestamp 推算成隔日；無 settlement／open
interest 保留 null。Published exchange calendar 是開市日權威，TAIFEX 使用獨立市場日曆。

帳號 entitlement、完整 universe mapping、provider quota 與 deadline capacity 必須有實際
readiness 證據。受限時標示 blocked，不自動升級付費方案，也不以縮小 subset 宣稱全市場完成。
所有 opt-in consumers 共用 durable account governor；Pilot/historical 的 installed allocation 與 Full
readiness expiry 分離。Capacity 合計全部 ready feeds 與 retry/byte/deadline/Source-check overhead，
拒絕不足的 provider。停止或失效只阻擋新 acquisition，已有 prepared frozen delivery 可完成。

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

Dashboard只能透過API運作。公開lookup使用單一`/serve/instruments`及新版EOD／minute endpoints；
generated instrument cache僅供backend static／Admin cache maintenance。RAG、embedding、vector index與模型runtime屬
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
