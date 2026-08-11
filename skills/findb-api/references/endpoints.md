# FinDB staging API Endpoint Reference

This reference documents the current staging surface. The only Source write route is
`POST /api/v1/source/ingest`; it accepts an explicit versioned provider-neutral contract.
There are no market-suffixed or direct ingest routes.

Common conventions:

- Base URL: use the staging deployment base URL; local development defaults to `http://localhost:8080`.
- Source and protected Serve/Admin calls use `X-API-Key`; Admin browser sessions use a Bearer session.
- Dates are `YYYY-MM-DD`; timestamps are timezone-aware ISO 8601 and normalized to UTC.
- IDs are opaque UUID strings. List responses normally use `{success, data, pagination}`.
- `page` is at least 1; `page_size` is 1–1000 unless OpenAPI states otherwise.

## Current active provider/dataset scope

| Provider (`source`) | Dataset (`dataset_key`) | Schema |
| --- | --- | --- |
| `twelve_data` | `us_equity_eod` | `market_eod.v1` |
| `finlab` | `tw_equity_eod` | `market_eod.v1` |
| `shioaji` | `tw_equity_minute` | `market_minute.v1` |
| `shioaji` | `tw_etf_minute` | `market_minute.v1` |

Canonical Serve read models can contain retained history outside this table. That read
surface is not an active provider authorization.

## Source API — contract ingest and status

**Prefix**: `/api/v1/source` • **Auth**: a provider-scoped `X-API-Key` is required.

### `POST /api/v1/source/ingest`

The body is one of the registered versioned contracts. The envelope fields are:

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `dataset_key` | string | yes | One of the four exact active datasets |
| `schema_id` | string | yes | `market_eod` or `market_minute` |
| `schema_version` | integer | yes | Currently `1` |
| `source` | string | yes | Matching active provider name |
| `request_key` | string | yes | Stable request identity |
| `idempotency_key` | string | yes | Stable dedupe identity |
| `fetched_at` | datetime | yes | Timezone-aware upstream fetch time |
| `delivery` | object | no | Scheduler context (`slot_id`, `scheduled_for`, `target_data_date`, `work_item_id`) |
| `payload` | object | yes | Contract-specific batch and rows |

Example `market_eod.v1` request:

```json
{
  "dataset_key": "us_equity_eod",
  "schema_id": "market_eod",
  "schema_version": 1,
  "source": "twelve_data",
  "request_key": "twelve_data_us_equity_eod_20260724_aapl",
  "idempotency_key": "twelve_data_us_equity_eod_20260724_aapl",
  "fetched_at": "2026-07-24T21:00:00Z",
  "payload": {
    "batch": {
      "data_date": "2026-07-24",
      "delivery_mode": "full_snapshot",
      "declared_record_count": 1
    },
    "data": [
      {
        "symbol": "AAPL",
        "source_symbol": "AAPL",
        "trade_date": "2026-07-24",
        "currency": "USD",
        "open": "220.00",
        "high": "223.00",
        "low": "219.00",
        "close": "222.00",
        "volume": 1000000
      }
    ]
  }
}
```

Response is `202 Accepted` and includes `attempt_id`, `run_id`, `status`, `schema_id`,
`schema_version`, and a message. `202` means durable acceptance, not canonical completion.

### `GET /api/v1/source/contracts/{schema_id}/versions/{schema_version}`

Returns the machine-readable schema and semantic rules for `market_eod.v1` or
`market_minute.v1`.

### `GET /api/v1/source/attempts/{attempt_id}`

Returns the accepted/rejected attempt audit record. Keep this identity when diagnosing a
contract or source-scope rejection.

### `GET /api/v1/source/runs/{run_id}`

Returns normalization lifecycle fields including `dataset_key`, schema identity,
`status`, timestamps, record counts, error/failure code, and retry state.

### `POST /api/v1/source/runs/{run_id}/rerun`

Requeues a retained versioned raw contract and creates a new run. It is bounded by raw
retention and still requires the caller's provider-scoped key.

### `GET /api/v1/source/datasets`

Lists registry rows. For active ingestion, expect only the four pairs in the table above;
do not infer a new provider or dataset from a retained canonical row.

## Serve API — canonical read-only surface

**Prefix**: `/api/v1/serve` • **Auth**: controlled by `SERVE_REQUIRE_AUTH`.

All Serve endpoints are read-only. The following endpoints are stable canonical reads:

| Endpoint | Purpose |
| --- | --- |
| `GET /serve/instruments` | Instrument list/filter with pagination |
| `GET /serve/instruments/{instrument_id}` | Instrument detail |
| `GET /serve/eod` | Canonical daily rows |
| `GET /serve/eod/{instrument_id}` | Daily rows for one instrument |
| `GET /serve/corporate-actions` | Corporate-action read model |
| `GET /serve/corporate-actions/{instrument_id}` | Actions for one instrument |
| `GET /serve/macro/series` | Retained series catalog (read-only) |
| `GET /serve/macro/observations` | Retained observations (read-only) |
| `GET /serve/futures/contracts` | Retained contract read model (read-only) |
| `GET /serve/futures/continuous` | Retained continuous read model (read-only) |
| `GET /serve/bonds` | Retained bond master read model (read-only) |
| `GET /serve/bonds/eod` | Retained bond EOD read model (read-only) |
| `GET /serve/calendar` | Calendar rows |
| `GET /serve/calendar/years/{market}/{year}` | Complete published scheduler year |
| `GET /serve/market-freshness` | Provider-free configured freshness summary |

Macro, futures, bonds, and other retained read models do not authorize a Source POST or
claim an active provider feed. Use `/docs` for exact filters and response fields.

### Instruments query parameters

`market`, `asset_class`, `status`, `symbol`, `cursor`, `include_count`, `page`, and
`page_size` are supported. Cursor pagination is optional; when `include_count=false`,
the response omits total counts to reduce database work.

### EOD query parameters

`market`, `symbols`, `start_date`, `end_date`, `page`, and `page_size` are supported.
Treat `source` in a response as lineage metadata, not as a permission to submit another
provider.

## Admin API — governance and operations

**Prefix**: `/api/v1/admin` • **Auth**: named Dashboard session or DB-backed Admin key.

Common operational endpoints:

- `GET/POST /admin/auth/*` — login, logout, bootstrap, and password change.
- `GET /admin/schedulers` and `PATCH /admin/schedulers/{scheduler_key}` — owner-controlled
  staging scheduler desired state.
- `GET/POST /admin/credentials`, `POST /admin/credentials/{kind}/{id}/rotate`, and
  `DELETE /admin/credentials/{kind}/{id}` — credential governance.
- `GET /admin/queue/health`, `/admin/market-freshness`, and `/admin/missing-deliveries` —
  pipeline monitoring.
- `GET /admin/dq-issues`, `PATCH /admin/dq-issues/{issue_id}/resolve` — DQ review.
- `GET /admin/raw-payloads` and `/admin/raw-payloads/{run_id}` — retained raw audit.
- `PATCH /admin/eod/{instrument_id}/{trade_date}` — audited canonical correction.
- `GET/POST /admin/runs/bulk-rerun` — bounded retained-run recovery.
- `/admin/instrument-cache` — generated lookup cache governance.

Admin machine keys must never be given to Fetcher or general Serve consumers.

## Status and error shape

Errors use `{"detail": "<message>"}`; the status code and stable error code are the
contract, not the prose detail.

| Status | Meaning |
| --- | --- |
| `200` | Successful read or mutation |
| `202` | Source contract durably accepted |
| `400` | Request or dataset policy error |
| `401` | Missing authentication |
| `403` | Invalid key, scope, or staging network policy |
| `404` | Resource not found or raw retention expired |
| `409` | Idempotency/state conflict |
| `422` | Contract/schema validation failure |
| `429` | Rate limit; respect `Retry-After` |
| `500` | Unexpected server/configuration error |
| `503` | Temporary DB/queue unavailability |

For detailed run lifecycle, pagination, and DQ behavior, read `responses.md`.
