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

Admin `GET /api/v1/admin/schedulers` 與 Owner `PATCH /api/v1/admin/schedulers/{scheduler_key}`
回應另包含 `start_allowed: boolean` 與 `start_blockers: string[]`。全市場啟動會在同一
transaction 鎖定 registry／control 並重新驗證資格；資格不符回 `409`，detail 為
`{"code":"scheduler_start_blocked","start_blockers":["full_market_no_enabled_datasets"]}`
（原因依未完成條件而異），不變更 desired state、revision 或 audit。既有 revision conflict
仍回 `409`；停止永遠保留既有 Owner 權限／revision 檢查，但不要求啟動資格。

Scheduler取得完整published calendar：

```http
GET /api/v1/serve/calendar/years/{market}/{year}
```

缺少published revision或年度不完整時回`404`。Dashboard calendar preview、draft、publish
與rollback由Admin API處理；只有Owner可publish／rollback。

`GET /api/v1/admin/market-freshness` 保留既有 freshness／heartbeat 欄位，另回傳
`monitor_kind`（`bounded`／`full_market`）、`activation_state`（bounded 為 null；
全市場為 `not_activated`／`activated`／`deactivated`）、`active_dataset_keys`、
`pending_feeds[{dataset_key, blockers}]`。`include_feeds=false` 仍保留待啟用前提。
Feed 明細新增 `runtime_eligible` 與 `activation_blockers`；全市場 aggregate freshness、
coverage 與 feed counts 只計入 enabled 範圍，歷史 fetched／completed 與 Feed 明細仍可查詢。
`configuration_status=ready` 不代表已啟用、已部署或通過 full-market acceptance。

## Serve API

Serve只讀canonical tables，且只公開active registry可解析的EOD／minute／futures scopes。現行路由為：

- `GET /serve/datasets`：provider-free active catalog與coverage。
- `GET /serve/instruments`、`GET /serve/instruments/{instrument_id}`：商品、facets與分離的
  `coverage.eod`／`coverage.minute`。
- `GET /serve/eod`：必須指定`instrument_id`，或`market`加最多50個可選symbols；使用
  opaque cursor且固定最新優先。
- `GET /serve/minute`：單一`instrument_id`；明確日期需成對且最多五個曆年。省略日期時以
  最新canonical minute日期向前一個曆月，resolved range會固定在cursor內。
- `GET /serve/futures/eod`：實際到期合約與 `regular`／`after_hours` session，使用 opaque cursor；
  OHLC（包含 close）、volume、`settlement_price`／`open_interest` 缺漏時回傳 null；
  零成交但來源仍提供 settlement／OI 的紀錄可查詢，明確的 0 保留，不補造價格或 continuous series。
- `GET /serve/calendar`、`GET /serve/calendar/years/{market}/{year}`與
  `GET /serve/market-freshness`。

成功回應使用`{success, data}` typed envelope。Instrument list使用page pagination，EOD／minute
使用opaque cursor且不做total count。已知商品不屬active資料域時回
`DATASET_NOT_AVAILABLE`；找不到商品、日期範圍與cursor錯誤分別使用
`INSTRUMENT_NOT_FOUND`、`INVALID_DATE_RANGE`、`INVALID_CURSOR`。Catalog若遇到未知或矛盾的
active registry config會fail closed，不回傳provider、scheduler或credential資訊。

`/serve/lookup/*`、`/serve/eod/{instrument_id}`、corporate actions、macro與bonds已移除；期貨使用獨立 `/serve/futures/eod`。
Canonical tables仍保留，但沒有active feed的資料域不構成公開Serve contract。

範例：

```bash
curl -H "X-API-Key: $SERVE_KEY" \
  "https://<host>/api/v1/serve/eod?instrument_id=<uuid>&start_date=2026-07-01&end_date=2026-07-31&page_size=100"
```

## Full-market control API

Source 以自身 provider-scoped key 呼叫 universe／delivery plan v1；完整 request 型別以部署版本
OpenAPI 為準。Universe first baseline 或異動超過 20 個成員／2% 的 candidate，需要 Owner
呼叫 `POST /admin/universes/{release_id}/publish`，提供 `evidence_note` 與對應
`first_baseline_approved`／`threshold_exception_approved`，並留下 audit。

Owner 使用既有 `PATCH /admin/schedulers/{scheduler_key}`，傳 `desired_state` 與
`expected_revision`。Flag、installed enrollment、有效 scoped readiness、Owner published baseline、
完整 calendar 與 provider aggregate capacity 通過後，凍結 ready feeds；部分 scope 可 start，重複
running PATCH 不重凍結。首次日期為交易所當地今天，合法 legacy date 保留，停止／重新啟動不重設。
舊 `/admin/feeds/{dataset_key}/activate|deactivate` 需 Owner 認證並回傳 410。
Source `POST /source/full-market/readiness` 只接受已 enrollment 的 ID/digest/runtime/scope acknowledgement；
`GET /source/full-market/authorization` 為唯讀 immediate acquisition projection，heartbeat 不延長 evidence。
`GET /source/full-market/enrollment-verification` 需要 Source key 與 `enrollment_id`、`source_client_id`、
`declaration_sha256`、`runtime_id` query；只驗證目前有效管理 enrollment 和 90 秒內 installed ACK，
回傳 typed canonical declaration、installation digest、UUID 與 ACK timestamp。Owner stopped/flag-off
仍可核對容量修復證據；它不授權抓取，也不寫 control/audit。
旗標關閉／證據失效阻擋新 acquisition/plan，既有 frozen prepared delivery 與 normalization 可完成。
詳細管理 enrollment 與 flags 見 [runbook](../operations/full_market.md)。
`GET /admin/delivery-plans` 與 `/{plan_id}/summary` 提供 expected／data／no_data／missing／blocked、
deadline、late 與 gaps。這些 control mutation 不可使用 Serve key，也不可將 Admin key 提供給 Fetcher。

Admin 計畫查詢支援 `dataset_key` 與 `trade_date`；指定任一 `page/page_size` 即啟用分頁（缺省值 1／25，
每頁上限 100）；回傳 `data` 及標準 `pagination`。依交易日、建立時間、plan ID 降冪穩定排序。
只使用舊 `limit` 時保持原 `{data}` 回應與預設 20 筆；`limit` 與任一分頁參數混用回傳 422。
`GET /admin/delivery-plans/datasets` 回傳 `{"data":["dataset_key", ...]}`，由全部計畫去重並排序，
不受日期或最近筆數限制。以上端點須具 viewer 以上 Admin 權限，皆為唯讀，不改交付狀態。
缺口總數使用 `missing + blocked`；`summary.gaps` 明細最多 1,000 筆。

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
