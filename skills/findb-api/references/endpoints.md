# FinDB API Endpoint Reference

Full endpoint surface for FinDB, organized by API. Every list endpoint supports
pagination via `page` (≥ 1, default 1) and `page_size` (1–1000, default 100), and
returns `{success, data: [...], pagination: {...}}`.

Common conventions:
- Dates: `YYYY-MM-DD`
- Timestamps: UTC ISO 8601 (e.g. `2026-01-16T14:49:14Z`)
- IDs: UUIDv7 strings
- Base URL: `https://findb.tingfong.com` (prod) or `http://localhost:8080` (dev)
- Markets: `CRYPTO`, `US`, `FX`, `TW`, `HK`, `CN`, `WTX`, `GLOBAL`, `MACRO`

## Table of contents

- [Source API — write](#source-api--write)
  - [Standard ingest](#standard-ingest-postapiv1sourceingestmarket)
  - [Direct ingest](#direct-ingest-postapiv1sourceingestmarketdirect)
  - [Run management](#run-management)
  - [Dataset registry](#dataset-registry)
- [Serve API — read-only](#serve-api--read-only)
  - [Instruments](#instruments)
  - [Daily bars (EOD)](#daily-bars-eod)
  - [Corporate actions](#corporate-actions)
  - [Macro](#macro)
  - [Futures](#futures)
  - [Trading calendar](#trading-calendar)
- [Admin API — corrections](#admin-api--corrections)

---

## Source API — write

**Prefix**: `/api/v1/source` • **Auth**: `X-API-Key` required on every endpoint
• **IP allowlist**: enforced by nginx in prod.

### Standard ingest `POST /api/v1/source/ingest/{market}`

| `{market}` | Description |
| --- | --- |
| `crypto`  | CRYPTO market |
| `us`      | US market |
| `fx`      | FX market |
| `macro`   | MACRO market |
| `wtx`     | WTX (台指期) market |
| `global`  | GLOBAL market |
| `tw`      | TW market |
| `hk`      | HK market |
| `cn`      | CN market |

Request body (`IngestRequest`):

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `dataset_key` | string | yes | e.g. `crypto_eod`, `us_stock_eod`, `tw_equity_eod` |
| `source` | string | yes | Upstream provider, lowercase (`bloomberg`, `finlab`, ...) |
| `request_key` | string | yes | Upstream request identifier (traceability) |
| `idempotency_key` | string | yes | Dedupe key; same value → returns existing run |
| `payload` | object | yes | `{metadata: {...}, data: [...]}` provider-shaped |
| `fetched_at` | datetime | yes | When upstream fetched the data (UTC ISO 8601) |

Response (`IngestResponse`):

```json
{
  "success": true,
  "run_id": "019462f0-7c00-7000-8000-000000000001",
  "status": "pending",
  "message": "Data received, processing queued"
}
```

On duplicate `idempotency_key`: `message` becomes
`"Duplicate idempotency_key, returning existing run"` and `run_id` is the prior run.

### Direct ingest `POST /api/v1/source/ingest/{market}/direct`

Bare provider payload (`{metadata, data}`) without the wrapper. FinDB infers
`dataset_key`, `source`, `idempotency_key` from the payload.

| Endpoint | Inferred `dataset_key` | Notes |
| --- | --- | --- |
| `POST /ingest/crypto/direct` | `crypto_bloomberg_eod` | Bloomberg crypto |
| `POST /ingest/fx/direct` | `fx_bloomberg_eod` | Bloomberg FX |
| `POST /ingest/wtx/direct` | `wtx_eod` | Switches between FinLab and Bloomberg normalizer by `metadata.source` |
| `POST /ingest/usstock/direct` | `us_stock_eod` | Bloomberg US equities |
| `POST /ingest/hkchina/direct` | `hkchina_mixed_eod` | Mixed HK/CN equity + index; routes per ticker |
| `POST /ingest/hkchina-index/direct` | `hkchina_index_eod` | Legacy index-only feed |
| `POST /ingest/macro/direct` | `macro_bloomberg_observation` | Bloomberg macro (defaults to `MACRO` market if omitted) |
| `POST /ingest/twstock/direct` | `tw_equity_eod` or `tw_etf_eod` | FinLab TW; routes on `metadata.asset_class` |

Direct payload top-level shape:

```json
{
  "metadata": { "source": "bloomberg", "query_time": "2026-02-04T16:00:51Z" },
  "data": [ /* records */ ]
}
```

Direct ingest returns the same `IngestResponse` as standard ingest.

For per-market record shapes (FinLab TW, hkchina mixed, etc.), see
`ingest-payloads.md`.

### Run management

#### `GET /api/v1/source/runs/{run_id}`

Returns the lifecycle state of an ingest run.

| Field | Type | Description |
| --- | --- | --- |
| `run_id` | UUID | The run identifier |
| `dataset_key` | string | The dataset processed |
| `status` | string | `pending` / `running` / `completed` / `completed_with_errors` / `failed` |
| `started_at` | datetime | When processing began (nullable while pending) |
| `completed_at` | datetime | When processing ended (nullable until terminal) |
| `total_records` | int | Records seen in payload |
| `success_records` | int | Records written to canonical |
| `failed_records` | int | Records that failed DQ / write |
| `error_message` | string | Top-level error if `status=failed` |

#### `POST /api/v1/source/runs/{run_id}/rerun`

Re-trigger normalization using the stored raw payload. Returns a fresh
`IngestResponse` with a **new** `run_id`. Useful after a normalizer fix.

Note: raw payloads expire per `RAW_RETENTION_DAYS` (default 14, retention disabled
by default in prod). Once expired, rerun will fail with `404`.

### Dataset registry

#### `GET /api/v1/source/datasets`

Lists active datasets and their attributes.

```json
{
  "success": true,
  "data": [
    {
      "dataset_key": "crypto_eod",
      "name": "Crypto EOD",
      "description": "Cryptocurrency end-of-day prices",
      "asset_class": "crypto",
      "market": "CRYPTO",
      "frequency": "daily",
      "is_active": true
    }
  ]
}
```

Use this to discover valid `dataset_key` values before standard-format ingest.

---

## Serve API — read-only

**Prefix**: `/api/v1/serve` • **Auth**: optional, governed by `SERVE_REQUIRE_AUTH`.
All endpoints are read-only. All list responses paginate.

### Instruments

#### `GET /serve/instruments`

| Param | Type | Description |
| --- | --- | --- |
| `market` | string | `CRYPTO` / `US` / `FX` / `TW` / `HK` / `CN` / `WTX` / `GLOBAL` |
| `asset_class` | string | `crypto` / `equity` / `index` / `fx` / `etf` / `future` |
| `status` | string | `active` / `delisted` |
| `symbol` | string | Exact symbol match |
| `page`, `page_size` | int | Pagination |

Response item:
```json
{
  "instrument_id": "019462f0-7c00-7000-8000-000000000001",
  "asset_class": "crypto",
  "market": "CRYPTO",
  "symbol": "BTC",
  "name": "Bitcoin",
  "currency": "USD",
  "timezone": "UTC",
  "status": "active",
  "listed_date": null,
  "delisted_date": null
}
```

#### `GET /serve/instruments/{instrument_id}`

Single-instrument detail. `instrument_id` is a UUIDv7.

### Daily bars (EOD)

#### `GET /serve/eod`

| Param | Type | Description |
| --- | --- | --- |
| `market` | string | Optional |
| `symbols` | string | Comma-separated, e.g. `BTC,ETH` |
| `start_date`, `end_date` | date | Range filter |
| `page`, `page_size` | int | Pagination |

Response item:
```json
{
  "instrument_id": "019462f0-...",
  "symbol": "BTC", "name": "Bitcoin", "market": "CRYPTO",
  "trade_date": "2026-01-16",
  "open": 95550.07, "high": 95825.34, "low": 95119.76, "close": 95709.01,
  "volume": null, "turnover": null,
  "source": "bloomberg"
}
```

`volume` / `turnover` can be `null` when the provider doesn't supply them (e.g.
some indices).

#### `GET /serve/eod/{instrument_id}`

Single-instrument bars. Same query params as list, minus `market` / `symbols`.

### Corporate actions

#### `GET /serve/corporate-actions`

| Param | Type | Description |
| --- | --- | --- |
| `market` | string | Optional |
| `symbols` | string | Comma-separated |
| `action_type` | string | `dividend` / `split` / `spinoff` / ... |
| `start_date`, `end_date` | date | Ex-date range |
| `page`, `page_size` | int | Pagination |

Response item:
```json
{
  "action_id": "...",
  "instrument_id": "...",
  "symbol": "AAPL", "name": "Apple Inc", "market": "US",
  "action_type": "dividend",
  "ex_date": "2026-02-07", "record_date": "2026-02-10", "pay_date": "2026-02-14",
  "ratio": null, "cash_amount": 0.25, "currency": "USD",
  "source": "bloomberg",
  "extra": null
}
```

#### `GET /serve/corporate-actions/{instrument_id}`

Single-instrument actions, with `action_type` and date filters.

### Macro

#### `GET /serve/macro/series`

| Param | Type | Description |
| --- | --- | --- |
| `market` | string | Optional |
| `source` | string | Provider filter |
| `source_code` | string | Exact upstream code (e.g. `CPI_YOY`) |
| `name` | string | Fuzzy series-name search |
| `page`, `page_size` | int | Pagination |

Response item:
```json
{
  "series_id": "...",
  "name": "US CPI YoY", "unit": "percent", "frequency": "monthly",
  "market": "MACRO", "source_code": "CPI_YOY", "source": "bloomberg"
}
```

#### `GET /serve/macro/observations`

| Param | Type | Description |
| --- | --- | --- |
| `market` | string | Optional |
| `source_code` | string | Filter by upstream code |
| `start_date`, `end_date` | date | Range filter |
| `page`, `page_size` | int | Pagination |

Response item:
```json
{
  "id": "...", "series_id": "...", "series_name": "US CPI YoY",
  "obs_date": "2026-01-15", "value": 3.2,
  "source": "bloomberg"
}
```

#### `GET /serve/macro/observations/{series_id}`

Observations for a specific series. Date params and pagination as above.

### Futures

#### `GET /serve/futures/contracts`

| Param | Type | Description |
| --- | --- | --- |
| `market` | string | Optional |
| `symbols` | string | Comma-separated (underlying symbol) |
| `contract_code` | string | Exact contract code (e.g. `TXFH6`) |
| `start_expiry`, `end_expiry` | date | Expiry date range |
| `page`, `page_size` | int | Pagination |

Response item:
```json
{
  "contract_id": "...", "instrument_id": "...",
  "symbol": "WTX", "name": "台指期",
  "contract_code": "TXFH6", "contract_month": "2026-03", "expiry_date": "2026-03-18",
  "currency": "TWD", "source": "bloomberg", "extra": null
}
```

#### `GET /serve/futures/continuous`

Continuous-contract EOD bars (joined via the configured `roll_rule`).

| Param | Type | Description |
| --- | --- | --- |
| `market`, `symbols` | string | Filters |
| `start_date`, `end_date` | date | Range |
| `page`, `page_size` | int | Pagination |

Response item:
```json
{
  "id": "...", "instrument_id": "...",
  "symbol": "WTX", "name": "台指期",
  "trade_date": "2026-01-16",
  "open": 22500.0, "high": 22650.0, "low": 22400.0, "close": 22600.0,
  "volume": 120000, "turnover": null,
  "source": "bloomberg",
  "roll_rule_id": "...", "roll_rule_name": "volume_based"
}
```

#### `GET /serve/futures/continuous/{instrument_id}`

Single-instrument continuous bars.

### Trading calendar

#### `GET /serve/calendar`

| Param | Type | Description |
| --- | --- | --- |
| `market` | string | **Required** |
| `start_date`, `end_date` | date | Range |
| `is_open` | bool | `true` open days only / `false` holidays only |
| `page`, `page_size` | int | Pagination |

Response item:
```json
{
  "market": "US", "trade_date": "2026-01-19",
  "is_open": false, "session_open": null, "session_close": null,
  "holiday_name": "Martin Luther King Jr. Day"
}
```

---

## Admin API — corrections

**Prefix**: `/api/v1/admin` • **Auth**: `X-API-Key` always required; key separate
from Source/Serve keys. Each correction writes an immutable audit record to
`canonical_correction`.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/dq-issues` | List DQ issues; filter by `resolved`, `severity`, `instrument_id` |
| `PATCH` | `/eod/{instrument_id}/{trade_date}` | Patch one EOD record's OHLCV fields |
| `PATCH` | `/dq-issues/{issue_id}/resolve` | Mark a DQ issue resolved with a note |
| `GET` | `/raw-payloads` | List recent raw payloads (filter by `dataset_key`, dates) |
| `GET` | `/raw-payloads/{run_id}` | Fetch the original raw payload for a run |
| `GET` | `/corrections` | Browse the correction audit log |
| `POST` | `/runs/bulk-rerun` | Re-trigger multiple runs (optionally filtered by `dataset_key`, `status`) |
| `GET` | `/instrument-cache` | Read the generated `backend/app/static/data/instruments.json` |
| `PUT` | `/instrument-cache` | Full overwrite (server validates + normalizes) |
| `PATCH` | `/instrument-cache/items/{instrument_id}` | Update one instrument cache item |

### `PATCH /admin/eod/{instrument_id}/{trade_date}`

Request body (only the fields you want to change):

| Field | Type | Description |
| --- | --- | --- |
| `open` / `high` / `low` / `close` | number\|null | Pass `null` to clear |
| `volume` / `turnover` | int\|number\|null | Same |
| `reason` | string | Required; appears in the audit log |

Common errors:
- `400` `"No OHLCV fields provided..."` — body has no OHLCV keys
- `400` `"No changes detected..."` — provided values equal current values
- `404` `"EOD record not found..."` — no row for that `(instrument_id, trade_date)`

### `PATCH /admin/dq-issues/{issue_id}/resolve`

Body: `{"resolution_note": "<text>"}`.

- `404` if `issue_id` doesn't exist.
- `409` if the issue is already resolved.

### Instrument cache notes

The cache JSON (`backend/app/static/data/instruments.json`) is generated by a cron job
that calls Serve, computes latest price/date, and writes a static file used by
the `/instrument-lookup` UI. Use the Admin cache endpoints only for manual
intervention (e.g. correcting a stale name) — for normal updates, let the cron
job regenerate.
