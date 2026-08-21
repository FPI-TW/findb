# FinDB API 使用指南

> 精確request/response model以部署版本的`/docs`、`/openapi.json`與Source contract
> endpoint為準。本文件只維護穩定的使用規則。

## API families與認證

```text
/api/v1/source   Contract ingest、delivery status與scheduler control
/api/v1/serve    Canonical唯讀查詢
/api/v1/admin    營運、credential、修正與治理
/health          Process liveness
```

Machine API使用：

```http
X-API-Key: <key>
```

Dashboard human session使用：

```http
Authorization: Bearer <admin-session>
```

| API | 認證與權限 |
| --- | --- |
| Source | 必須使用DB-backed Source client key，受`source_name`、datasets與rate limit限制 |
| Serve | 由`SERVE_REQUIRE_AUTH`控制；即使開放仍保持唯讀 |
| Admin | 永遠需要具名session、DB-backed machine key或break-glass recovery key |

每個provider/client使用獨立Source key。Fetcher的calendar Serve key只讀published
calendar且不得與Source key共用。`ADMIN_BREAK_GLASS_API_KEY`只供bootstrap與緊急復原。

## Canonical ingest

```http
POST /api/v1/source/ingest
Content-Type: application/json
X-API-Key: <source-client-key>
```

最小範例：

```json
{
  "dataset_key": "tw_equity_eod",
  "schema_id": "market_eod",
  "schema_version": 1,
  "source": "finlab",
  "request_key": "finlab_tw_equity_eod_20260724_01",
  "idempotency_key": "finlab_tw_equity_eod_20260724",
  "fetched_at": "2026-07-24T08:00:00Z",
  "payload": {
    "batch": {
      "data_date": "2026-07-24",
      "delivery_mode": "full_snapshot",
      "declared_record_count": 1
    },
    "data": [{"symbol": "2330", "trade_date": "2026-07-24", "close": "1140.00"}]
  }
}
```

成功回`202`及`attempt_id`、`run_id`、schema version與初始status。`202`只代表durable
accept，不代表canonical完成；producer必須保存兩個ID並等待run進入terminal state。

完整contract見[Versioned Ingress Contract](../architecture/ingress_contracts.md)，或讀取：

```http
GET /api/v1/source/contracts/{schema_id}/versions/{schema_version}
```

## Delivery status、重試與rerun

```http
GET  /api/v1/source/attempts/{attempt_id}
GET  /api/v1/source/runs/{run_id}
POST /api/v1/source/runs/{run_id}/rerun
GET  /api/v1/source/datasets
```

- Attempt記錄通過auth／rate-limit gate的呼叫，包括contract rejection。
- Run代表已durable accept並排入normalization的工作。
- Rerun從仍保存的raw建立新run，不修改原run，也不算新的delivery arrival。
- 冪等範圍是已認證`source_client_id + dataset_key + idempotency_key`。
- 同一範圍內的canonical payload、source及schema/version皆相同時回既有run；任一項不同
  時回`409 IDEMPOTENCY_PAYLOAD_MISMATCH`。
- Credential綁定的source與request不符時，會先回`403 SOURCE_IDENTITY_MISMATCH`。
- `request_key`、`fetched_at`與`delivery` metadata不參與內容衝突比對。
- Raw retention清除對應payload後不再保證key可去重；Source key輪替產生的新client identity
  也屬新的冪等範圍。
- Timeout、429、502、503、504重試時沿用相同key並遵守`Retry-After`。

不要只看HTTP `202`判斷資料完成，也不要因client timeout自行生成新key。

## Scheduler與calendar

Fetcher以provider-scoped Source key輪詢並回報heartbeat：

```http
POST /api/v1/source/scheduler-controls/{scheduler_key}/poll
```

回應包含provider、dataset mapping、slot、本地時間、timezone、desired state與revision。
Scope或reviewed workload不一致時consumer必須fail closed。

Scheduler取得完整published calendar：

```http
GET /api/v1/serve/calendar/years/{market}/{year}
```

缺少published revision或年度不完整時回`404`。Dashboard calendar preview、draft、publish
與rollback由Admin API處理；只有Owner可publish／rollback。

## Serve API

Serve只讀canonical tables，涵蓋instrument、EOD、corporate action、macro、futures、
bonds、calendar及market freshness。保留的endpoint或歷史資料不代表有active provider
feed；active feed清單見[現行架構](../architecture/overview.md#active-feeds)。

List endpoint的filter、pagination與response envelope以OpenAPI為準。使用
`/serve/instruments`時可選`include_count=false`避免不必要的count query。

範例：

```bash
curl -H "X-API-Key: $SERVE_KEY" \
  "https://<host>/api/v1/serve/eod?market=TW&symbols=2330&start_date=2026-07-01"
```

## Admin API

Admin只供Dashboard與維運，主要能力包括：

- user session、Source／Serve／Admin credentials的簽發、輪替與撤銷；
- scheduler desired state、queue health、missing delivery與market freshness；
- DQ、raw payload、rerun、EOD correction與audit；
- instrument cache及市場日曆draft／publish／rollback。

Credential plaintext只在簽發時顯示一次。Admin machine key不得提供給Fetcher或一般
Serve consumer；同一請求不得同時帶Bearer session與`X-API-Key`。完整端點以OpenAPI的
`Admin API` tag為準。

## Error、時間與ID

| Status | 意義 |
| --- | --- |
| `202` | Delivery已durable accept並排隊 |
| `400`／`422` | Request語意、policy或schema不合法 |
| `401`／`403` | 缺少credential、credential無效、scope或來源IP不符 |
| `409` | Idempotency或狀態衝突 |
| `413` | Request或payload超限 |
| `429` | Rate limit，依`Retry-After`重試 |
| `503` | DB或ingestion暫時不可用，可安全重試 |

Canonical ingest error提供穩定`code`及可用時的`attempt_id`；client依code分類，不解析
人類訊息。Datetime使用含timezone的ISO 8601並正規化為UTC；業務日期用`YYYY-MM-DD`；
UUID視為opaque string；財務decimal不得轉成binary float後計算。

## 本機驗證

```bash
make up-db
make migrate
make seed
make up-server
curl http://localhost:8080/health
```

互動式schema位於`http://localhost:8080/docs`與`/openapi.json`。Staging smoke使用
專用client、bounded payload及可安全重送的固定idempotency key，並確認terminal state。
