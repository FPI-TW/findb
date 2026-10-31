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
| Fetcher | 獨立EC2上的Twelve Data／TAIFEX、FinLab、Shioaji、FactSet images | Provider抓取、contract mapping、Raw R2、retry、checkpoint與delivery status |
| Infrastructure | PostgreSQL RDS、RabbitMQ、nginx | Durable data、可重建delivery與TLS／routing |

FinDB backend與Dashboard同屬一個deployment unit；Fetcher為另一個deployment unit。
四個GitHub workflows分離FinDB CI/CD與Fetcher CI/CD。Contract變更採
backend-first expand/migrate/contract，不能假設兩個EC2同步更新。

## Active feeds

七條 feeds 的唯一宣告在 `backend/configs/execution_feeds.v1.json`，由 exporter 確定性產生
Fetcher 的三環境 manifest；兩個 execution profiles 共用宣告。

| Provider | Datasets | Contract |
| --- | --- | --- |
| `twelve_data` | `us_equity_eod`、`hk_equity_eod` | `market_eod.v1` |
| `finlab` | `tw_equity_eod`、`tw_etf_eod` | `market_eod.v1` |
| `shioaji` | `tw_equity_minute`、`tw_etf_minute` | `market_minute.v1` |
| `taifex` | `tw_futures_eod` | `futures_eod.v1` |

Pilot 與 Full market 使用同一 CLI、provider adapter、published universe／有效日曆、交易所時區、
收盤判定、重試、Raw R2、Source、normalization 與 Serve 流程。Pilot 只在建立 daily plan 時依 canonical
識別排序取前兩個商品，不足兩個取實際數量；期貨按 product／contract 選擇，保留各自所有 sessions。
空集合阻塞；plan 建立後 universe 更新與重試不得改寫成員。

Serve catalog 由 active registry 動態解析，canonical 資料可跨 profile 共用；交付完成證據不共用。
`delivery_member_evidence` 與 canonical writes 在同一 normalization transaction 保存，另一 profile
覆寫 canonical 最新 run_id 不會撤銷已完成計畫。Minute 仍須完整 sequenced snapshot。

## Full-market governance

Full market 在 local/staging/production 皆為 opt-in，預設 false。Registry acceptance 與 acquisition
admission 分離：Source 仍驗證 dataset existence/active/provider/scope，Full-only feeds 普通 ingress
不接受，需 `fp1` frozen plan。Owner 手動 scheduler start 各自凍結當時 ready feeds，首次日期以 dataset＋profile 隔離，首日為 exchange-local
今天且永不重設；合法 legacy cutoff 保留。啟動 eligibility 只檢查 published baseline；日曆缺日期依市場`weekend_days`預設，內建市場預設週六日休市。所有環境的新抓取不依賴 enrollment、readiness ACK／expiry 或 capacity proof；API 認證、
平台配額、running control 與資料驗證保留。
操作與部署/rollback contracts 見 [runbook](../operations/full_market.md)。

| Provider | 目標範圍 | Contract |
| --- | --- | --- |
| `twelve_data` | US 主要交易所普通股與 ADR；保留舊 HK delivery／rerun acceptance | `market_eod.v1` |
| `factset` | HKEX 現行主板／GEM 普通股；切換後的新 HK 抓取 | `market_eod.v2` |
| `finlab` | TW 上市／上櫃普通股與各類 ETF 日線 | `market_eod.v1` |
| `shioaji` | 相同 TW 股票／ETF 的一分鐘 bars | `market_minute.v1` |
| `taifex` | TX、MTX、TMF、TE、TF 實際月／週到期合約，日盤與盤後分開 | `futures_eod.v1` |

Universe 由官方清單建立 immutable release，保留來源 URL、checksum、觀測時間、effective
date、classification 與 provider mapping。第一份 baseline 需要 Owner 明確 audited approval；
相對上一份 published release 異動超過 20 個成員或 2% 時保留 candidate，須明確 exception
approval 才可發布。Mapping gap 仍列為 expected，不能刪除成員來降低分母。

每個 profile／已到排程時間的日期建立 frozen daily delivery plan，以 release 固定成員、日期與 work item；
只有 provider 明確設定跳過非交易日時才記錄 durable skip。TAIFEX 新計畫使用同日期官方報表的 published release。
完整度恆等式是 `expected = data + no_data + missing + blocked`；data 只計入成功 canonical
lineage，no_data 只接受停牌／無成交的 durable source evidence。成功部分立即可查，partial
coverage 不等同全日 complete；mapping、quota、permission 或 source error 均保留 gap。

啟用從 activation date 向前運作，只追補啟用後缺口；full-market 不建立歷史回補或連續期貨。
TAIFEX trade date 使用交易所歸屬日，盤後不由 timestamp 推算成隔日；無 settlement／open
interest 保留 null。Published exchange calendar 是開市日權威，TAIFEX 使用獨立市場日曆。

治理資料只要求 Owner published official baseline；日曆缺日期依市場`weekend_days`預設，內建市場預設週六日休市。平台限制為 Twelve Data
1597 credits／分鐘、FinLab API 5000 MB／日、Shioaji 500 MiB／日與 50 行情查詢／10 秒；
TAIFEX OpenAPI 無帳號及明確數值配額，收到 429 時等待／重試。帳號 ledger 保留用量與 cooldown，
不設跨環境分配或额外 capacity 前提；API 權限、payload 與資料驗證仍保留。
操作與治理交付資料見 [runbook](../operations/full_market.md#治理資料明確需求)。
停止或失效只阻擋新 acquisition，已有 prepared frozen delivery 可完成。

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
Backend 共用有效日曆解析：published 日期優先，缺日期依市場`weekend_days`預設，內建市場預設週六日休市；draft 不影響抓取。
Serve 年度／區間查詢及 Admin 顯示日期來源與可空 revision，不寫入或自動發布。
Fetcher 每日經 Source 建立凍結決策，不依賴 Serve 日曆 credentials。
五個 provider 的 `*_SKIP_NON_TRADING_DAYS` 預設皆為 true。對應開關為 true
時略過非交易日並保存 durable skip。
部署與啟動不要求日曆完整；其他認證、時區、收盤、Owner controls、配額與交付證據規則保留。


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

## Provider work 協調

[Provider work FIFO](provider_work.md) 將同環境／provider 的 Pilot、Full daily、historical、maintenance 與
retained-raw rerun 協調為 PostgreSQL stable work＋append-only attempts＋single owner slot；Fetcher 的 shared
whole-work OS flock 另約束外部 provider I/O 與 descendants。Source acceptance／receipt association 與 close／settle
共用 transaction lock／generation fence，owner 保留到 accepted runs 全部 terminal 與完整 durable proof committed。
不同 providers 仍可並行，profile 的 controls／first dates／frozen plans／checkpoints 不合併。
`PROVIDER_WORK_REQUIRED` 缺省 true；false 會拒絕啟動，無 legacy acquiring／acceptance fallback。遠端切換仍須完成 deployment runbook 的 audited preflight。
新增 provider 的 acquisition、universe/catalogue、historical 與 rerun 都必須接入此 protocol。
