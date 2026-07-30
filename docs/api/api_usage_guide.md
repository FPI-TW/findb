# FinDB API 使用指南

> 精確 request/response model 以部署版本的 `/docs`、`/openapi.json` 與 Source
> contract endpoint 為準。本文件只維護穩定的使用規則。

## Base paths

```text
/api/v1/source   資料寫入與 delivery status
/api/v1/serve    Canonical 資料查詢
/api/v1/admin    營運與修正
/health          Process health
```

Production 由 nginx 將 Source/Admin 導向 ingest role，Serve 導向 serve role。

## 認證

受保護端點使用：

```http
X-API-Key: <key>
```

Admin human session改用：

```http
Authorization: Bearer <admin-session>
```

| API | 規則 |
| --- | --- |
| Source | 必須；使用 DB-backed source client key |
| Serve | 由 `SERVE_REQUIRE_AUTH` 控制；production 建議啟用 |
| Admin | 永遠必須；Dashboard使用具名user session，machine client使用DB-backed Admin key |

Source client可限制 `source_name`、`allowed_datasets`與rate limit。每個
provider/client應使用獨立 key。Source production入口另受 nginx IP allowlist保護。
`ADMIN_BREAK_GLASS_API_KEY`只供初次bootstrap與緊急復原，不得作為日常Dashboard身分。

## Canonical ingest

所有新 Fetcher使用：

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
    "data": [
      {
        "symbol": "2330",
        "trade_date": "2026-07-24",
        "currency": "TWD",
        "close": "1140.00"
      }
    ]
  }
}
```

成功回 `202`：

```json
{
  "attempt_id": "019...",
  "run_id": "019...",
  "status": "pending",
  "schema_id": "market_eod",
  "schema_version": 1,
  "message": "Data received, processing queued"
}
```

`202` 只表示 durable accept，不表示 canonical data已完成。呼叫端必須保存
`attempt_id` 與 `run_id`，並查詢 terminal state。

完整 contract見
[Versioned Ingress Contract](../architecture/ingress_contracts.md)，或直接取得：

```http
GET /api/v1/source/contracts/market_eod/versions/1
GET /api/v1/source/contracts/futures_continuous_eod/versions/1
```

## Delivery status

```http
GET /api/v1/source/attempts/{attempt_id}
GET /api/v1/source/runs/{run_id}
POST /api/v1/source/runs/{run_id}/rerun
GET /api/v1/source/datasets
```

- Attempt記錄每一次通過 auth/rate-limit gate 的 canonical呼叫，包括被拒絕的請求。
- Run代表已接受並排入 normalization的工作。
- Rerun從保留的 raw payload建立新 run，不修改原 run。

## Idempotency與重試

- 相同 source、dataset、idempotency key與內容：回既有 `run_id`。
- 相同 key、不同 source、schema/version或內容：`409`。
- Timeout、429、502、503、504重試時必須沿用相同 key。
- `Retry-After` 存在時應遵守。
- 不要因 client timeout自行生成新 key，否則可能建立重複業務 delivery。

建議 key：

```text
{source}_{dataset_key}_{data_date}
```

分批 delivery可再加入可穩定重建的 sequence。

## Legacy Source endpoints

`/source/ingest/{market}` 與 `/source/ingest/*/direct` 仍供既有 feed和raw rerun相容。
它們不是新 Fetcher的擴充點。新增 provider時必須轉成 canonical contract，不新增
provider-specific route或normalizer。

## Serve API

Serve只讀 canonical tables。主要端點：

| Endpoint | 用途 |
| --- | --- |
| `GET /serve/instruments` | Instrument列表、market/asset class/symbol篩選 |
| `GET /serve/instruments/{instrument_id}` | 單一 instrument |
| `GET /serve/eod` | 依 market、symbol、日期查 EOD |
| `GET /serve/eod/{instrument_id}` | 單一 instrument EOD |
| `GET /serve/corporate-actions` | 公司行為 |
| `GET /serve/macro/series` | Macro series |
| `GET /serve/macro/observations` | Macro observations |
| `GET /serve/futures/contracts` | 期貨合約 |
| `GET /serve/futures/continuous` | 連續期貨 EOD |
| `GET /serve/bonds` | 債券 master |
| `GET /serve/bonds/eod` | 債券 EOD |
| `GET /serve/calendar` | 交易日曆 |
| `GET /serve/calendar/years/{market}/{year}` | Scheduler 專用；只回傳完整已發布年度，否則404 |

範例：

```bash
curl -H "X-API-Key: $SERVE_KEY" \
  "https://<host>/api/v1/serve/eod?market=TW&symbols=2330&start_date=2026-07-01"
```

List endpoint使用 response內的 pagination資訊。`/serve/instruments` 支援 cursor
keyset pagination；不需要總筆數時使用 `include_count=false` 降低DB負擔。其他
endpoint的實際 query parameters以 OpenAPI為準。

## Admin API

Admin API具有敏感讀寫能力，只供Dashboard與維運：

- Source、Serve與Admin machine credential簽發、列表、輪替與撤銷
- Admin user、角色與session管理
- Queue health與missing delivery
- DQ issue查詢與resolve
- EOD人工修正與correction audit
- Raw payload查詢
- Bulk rerun
- Instrument cache管理
- 市場交易日曆設定、JSON／TWSE CSV preview、草稿、發布與回滾

Credential與登入主要端點：

| Endpoint | 用途 |
| --- | --- |
| `POST /admin/auth/bootstrap` | 尚無user時，以break-glass credential建立第一位Owner |
| `POST /admin/auth/login`、`POST /admin/auth/logout` | 建立或撤銷Dashboard user session |
| `GET /admin/credentials`、`GET /admin/credentials/overview` | 統一清單與近即時usage摘要 |
| `POST /admin/credentials` | 簽發一次性顯示plaintext的credential |
| `POST /admin/credentials/{kind}/{id}/rotate` | 建立successor；不自動撤銷舊key |
| `DELETE /admin/credentials/{kind}/{id}` | 立即撤銷credential |

其餘端點可由 `/docs` 的 `Admin API` tag查看。Admin machine key不得提供給Fetcher或
一般Serve consumer；同一請求不得同時帶Bearer session與`X-API-Key`。

## 常見狀態碼

| Status | 意義 |
| --- | --- |
| `202` | Delivery已 durable accept並排隊 |
| `400` | Request語意或delivery policy不合法 |
| `401` | 缺少 API key |
| `403` | Key無效、source/dataset scope不符或來源IP不允許 |
| `409` | Idempotency衝突、dataset未設定contract等狀態衝突 |
| `413` | Request或payload超過限制 |
| `422` | Contract/schema/欄位驗證失敗 |
| `429` | Rate limit |
| `500` | 非預期內部錯誤 |
| `503` | DB或ingestion暫時不可用，可依 `Retry-After` 重試 |

Canonical ingest error會回固定 `code` 與可查詢的 `attempt_id`（若 attempt已能建立）。
Client應依 code分類，不解析人類訊息。

## 時間、日期與ID

- Datetime使用含 timezone的 ISO 8601；系統正規化為 UTC。
- 業務日期使用 `YYYY-MM-DD`。
- UUID視為opaque string，不依版本或排序特性建立client邏輯。
- Decimal可能以JSON number或字串呈現；財務程式不得轉成binary float後再計算。

## 本機驗證

```bash
make up-db
make migrate
make seed
make up-server
curl http://localhost:8080/health
```

互動式文件：

```text
http://localhost:8080/docs
http://localhost:8080/openapi.json
```

自動化測試：

```bash
make test
uv --directory backend run pytest tests/test_source_api.py
```

Production smoke test必須使用專用測試dataset/client或可安全重送的固定 idempotency
key，並確認 run最後進入 terminal state。
