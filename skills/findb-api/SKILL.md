---
name: findb-api
description: |
  Use when integrating with the FinDB staging financial-data backend
  (http://localhost:8080 in local development or the staging host) to read canonical
  data or submit one of the four active provider/dataset feeds. The only active Source
  write contract is POST /api/v1/source/ingest with an explicit versioned envelope:
  twelve_data/us_equity_eod, finlab/tw_equity_eod, shioaji/tw_equity_minute, and
  shioaji/tw_etf_minute. Trigger on FinDB, /api/v1/source/, /api/v1/serve/,
  dataset_key, idempotency_key, run_id status checks, instrument lookup, or Serve
  API X-API-Key. Do not invent provider-specific routes, direct payload routes, retired
  providers, or inactive datasets.
---

# FinDB API Skill

Use this skill to build clients, dashboards, ETL jobs, or AI agents that read the FinDB
staging backend or submit a bounded, versioned delivery. FinDB is a Fetch → Normalize →
Serve pipeline: the Source API durably accepts a provider-neutral contract, workers
normalize it into canonical tables, and Serve exposes read-only canonical data.

## Staging boundary

The only active provider/dataset pairs are:

| Provider | Dataset | Contract |
| --- | --- | --- |
| `twelve_data` | `us_equity_eod` | `market_eod.v1` |
| `finlab` | `tw_equity_eod` | `market_eod.v1` |
| `shioaji` | `tw_equity_minute` | `market_minute.v1` |
| `shioaji` | `tw_etf_minute` | `market_minute.v1` |

Serve and lookup may expose retained canonical/history read models outside these four
feeds. A read model is not evidence that an active provider feed exists. Retired
provider names, direct/market ingest routes, and old dataset payload shapes are not
supported. If a new domain is needed, add a complete versioned contract, dataset
registry declaration, normalizer, DQ policy, Serve model, and staging acceptance in a
separate change.

## Surfaces, URLs, and authentication

| Surface | Prefix | Purpose | Auth |
| --- | --- | --- | --- |
| Source API | `/api/v1/source` | Versioned ingest and run/attempt status | `X-API-Key` required |
| Serve API | `/api/v1/serve` | Read canonical data | Controlled by `SERVE_REQUIRE_AUTH`; read-only |
| Admin API | `/api/v1/admin` | Governance, DQ, corrections, and operations | Named session or DB-backed Admin key |

Use the staging base URL supplied by the deployment environment. Local development is
`http://localhost:8080`; OpenAPI is `<base>/docs`, and the public lookup page is
`<base>/dashboard/lookup`.

Source clients must use a DB-backed provider-scoped key. Never put credentials in a
payload, URL, generated contract, raw artifact, or browser bundle. Serve keys are
separate from Source keys; Admin keys are separate from both. Follow the staging nginx
IP allowlist and rate-limit policy when a request is rejected with `403` or `429`.

## Reading canonical data

Serve list responses normally use `{success, data, pagination}`. Dates are `YYYY-MM-DD`,
timestamps are UTC ISO 8601, and identifiers are opaque UUID strings. Common read-only
queries are:

```bash
# Instruments
curl "$FINDB_BASE/api/v1/serve/instruments?market=US&asset_class=equity" \
  -H "X-API-Key: $SERVE_KEY"

# EOD rows from the canonical read model
curl "$FINDB_BASE/api/v1/serve/eod?market=US&symbols=AAPL&start_date=2026-01-01" \
  -H "X-API-Key: $SERVE_KEY"

# Trading calendar
curl "$FINDB_BASE/api/v1/serve/calendar?market=US" \
  -H "X-API-Key: $SERVE_KEY"
```

The Serve surface also contains retained canonical read models for instruments, EOD,
corporate actions, macro observations, futures/bonds, calendars, and market freshness.
Those endpoints are read-only and must not be described as active provider feeds.

Useful endpoints include:

- `GET /api/v1/serve/instruments`
- `GET /api/v1/serve/instruments/{instrument_id}`
- `GET /api/v1/serve/eod`
- `GET /api/v1/serve/eod/{instrument_id}`
- `GET /api/v1/serve/corporate-actions`
- `GET /api/v1/serve/calendar`
- `GET /api/v1/serve/calendar/years/{market}/{year}` (complete published year for schedulers)
- `GET /api/v1/serve/market-freshness`

Use the generated OpenAPI document for optional read-model endpoints and exact query
parameters. Serve never creates ingest jobs or writes canonical data.

## Writing a delivery: one contract-only route

All four active feeds use exactly:

```http
POST /api/v1/source/ingest
Content-Type: application/json
X-API-Key: <provider-scoped-source-key>
```

The envelope must include `dataset_key`, `schema_id`, `schema_version`, `source`,
`request_key`, `idempotency_key`, `fetched_at`, and `payload`. The `source` and dataset
must match one of the four pairs above. `market_eod.v1` uses a `payload.batch` plus EOD
rows; `market_minute.v1` uses the minute sequence identity and Taiwan-local trade date
rules. See `references/ingest-payloads.md` for provider-neutral examples.

Example EOD delivery:

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
        "source_symbol": "2330",
        "trade_date": "2026-07-24",
        "currency": "TWD",
        "open": "1120.00",
        "high": "1145.00",
        "low": "1115.00",
        "close": "1140.00",
        "volume": 25000000
      }
    ]
  }
}
```

The `202 Accepted` response is durable acceptance, not canonical completion. Save the
returned `attempt_id` and `run_id` and poll the run:

```bash
curl "$FINDB_BASE/api/v1/source/runs/<run_id>" \
  -H "X-API-Key: $SOURCE_KEY"
```

Use the same idempotency key for transport retries. A same-key/same-content retry returns
the existing run; a same-key/different-content request is rejected. Raw rerun, when
authorized and within retention, is `POST /api/v1/source/runs/{run_id}/rerun` and creates
a new run from the retained versioned contract.

## Minute delivery notes

`shioaji/tw_equity_minute` and `shioaji/tw_etf_minute` use `market_minute.v1`:

- `payload.batch.delivery_mode` is `sequenced_snapshot`.
- `snapshot_id`, `daily_update_id`, `universe_id`, `symbols_sha256`, `sequence`, and
  `sequence_count` are required.
- Every row has UTC-aware bar timestamps, Taiwan-local `trade_date`,
  `market_timezone=Asia/Taipei`, and `price_adjustment=none`.
- Shioaji deliveries include credential-free provider usage snapshots and use the
  canonical `mmr:<sha256>` request key and `mms:<sha256>` idempotency key.

## Errors and operational safety

| Status | Meaning |
| --- | --- |
| `202` | Contract durably accepted; poll the run |
| `400` | Dataset, policy, or semantic request error |
| `401` | Missing API key |
| `403` | Invalid key, provider/dataset scope, or staging IP policy |
| `404` | Run/read-model resource not found or raw expired |
| `409` | Idempotency conflict or state conflict |
| `422` | Versioned contract validation failed |
| `429` | Rate limit; respect `Retry-After` |
| `503` | Temporary DB/queue unavailability; retry with the same key |

Do not parse free-form error text as a stable API. Keep request/attempt/run identities in
your own audit log, but never log API keys, full raw payloads, or provider credentials.

## Where to dig deeper

- **`references/endpoints.md`** — staging Source/Serve/Admin endpoints and response shapes.
- **`references/ingest-payloads.md`** — only the four active provider/dataset contract examples.
- **`references/responses.md`** — pagination, run lifecycle, DQ, and error handling.
- **`assets/sample_payload.json`** — one complete `market_eod.v1` sample for an active feed.
