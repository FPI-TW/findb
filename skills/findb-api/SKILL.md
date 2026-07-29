---
name: findb-api
description: |
  Use when integrating with the FinDB financial-data backend (https://findb.tingfong.com,
  or http://localhost:8080 in dev) — reading instruments, EOD bars, corporate actions,
  macro series, futures contracts/continuous EOD, or trading calendars; or POSTing
  Bloomberg / FinLab / MultiCharts payloads via the Source ingest API. Trigger on any
  mention of findb, findb.tingfong.com, /api/v1/source/, /api/v1/serve/, dataset_key,
  idempotency_key, "ingest crypto/fx/us/tw/hk/cn/wtx/macro/global", instrument-lookup,
  serve API X-API-Key, run_id status checks, or downstream tasks that read or write
  FinDB — even when the user doesn't name the service explicitly (e.g. "fetch BTC daily
  bars from our market-data backend"). FinDB is a Fetch → Normalize → Serve pipeline;
  consumers see Source (write, X-API-Key required), Serve (read-only, optional auth),
  and Admin (corrections, X-API-Key required) surfaces.
---

# FinDB API Skill

Use this skill to build clients, dashboards, ETL jobs, or AI agents that read from or
write to the **FinDB** financial-data backend. FinDB is a three-layer pipeline —
**Fetch → Normalize → Serve** — that ingests market data from upstream providers,
normalizes into canonical tables, and exposes them through three HTTP surfaces.

## What FinDB exposes

| Surface | Prefix | Purpose | Auth |
| --- | --- | --- | --- |
| **Source API** | `/api/v1/source` | Write raw market payloads; FinDB normalizes them asynchronously. | `X-API-Key` **required**. IP allowlist enforced by nginx in prod. |
| **Serve API** | `/api/v1/serve` | Read canonical instruments, EOD, corporate actions, macro, futures, calendar. | Optional — depends on `SERVE_REQUIRE_AUTH`. Read-only. |
| **Admin API** | `/api/v1/admin` | Manual corrections, DQ-issue review, raw-payload inspection, instrument-cache. | `X-API-Key` **required**, no bypass. |

Base URLs:
- Production: `https://findb.tingfong.com`
- Local dev: `http://localhost:8080`

Interactive docs (OpenAPI): `<base>/docs`. Manual tester UI: `<base>/test`. Static
lookup UI: `<base>/instrument-lookup`.

## Authentication & nginx behaviour

1. **Source API** — `X-API-Key: <SOURCE_API_KEY>` header is **required** on every
   request. In production the request must also originate from an IP listed in
   `SOURCE_ALLOWLIST_CIDRS` (the check happens at nginx, returning 403 before reaching
   FastAPI). If the service sits behind Cloudflare, the allowlist matches the real
   client IP, not the Cloudflare edge.
2. **Serve API** — auth is optional and governed by the server's `SERVE_REQUIRE_AUTH`
   flag. If enabled, pass an active DB-backed Serve API key in `X-API-Key`. **Production nginx
   shortcut**: same-origin requests from `/instrument-lookup` get `X-API-Key` injected
   by nginx based on `Referer`, so the static page calls Serve without exposing the
   key to the browser. Programmatic clients still need to send their own key.
3. **Admin API** — `X-API-Key: <ADMIN_API_KEY>` always required; no DEBUG bypass.
   Treat the admin key as separate from Source/Serve keys.

Rate limit: ~100 requests / 60 seconds per `(API key + client IP)`; exceeding returns
429.

## Reading data — Serve API quick reference

All list endpoints return `{success, data: [...], pagination: {...}}`. Date params are
`YYYY-MM-DD`. Pagination: `page` (≥ 1, default 1), `page_size` (1–1000, default 100).

### Five most common queries

```bash
# 1. Instruments — list / filter by market or asset class
curl "https://findb.tingfong.com/api/v1/serve/instruments?market=US&asset_class=equity"

# 2. Daily bars (EOD) — by market + symbols + date range
curl "https://findb.tingfong.com/api/v1/serve/eod?market=CRYPTO&symbols=BTC,ETH&start_date=2026-01-01&end_date=2026-01-31"

# 3. Corporate actions — dividends / splits
curl "https://findb.tingfong.com/api/v1/serve/corporate-actions?market=US&action_type=dividend"

# 4. Macro observations — by source_code or series_id
curl "https://findb.tingfong.com/api/v1/serve/macro/observations?source_code=CPI_YOY&start_date=2025-01-01"

# 5. Futures continuous EOD — by symbol
curl "https://findb.tingfong.com/api/v1/serve/futures/continuous?symbols=WTX&start_date=2026-01-01"
```

Other Serve endpoints:
- `GET /serve/instruments/{instrument_id}` — single instrument detail
- `GET /serve/eod/{instrument_id}` — single-instrument bars
- `GET /serve/corporate-actions/{instrument_id}` — single-instrument actions
- `GET /serve/macro/series` and `/macro/observations/{series_id}` — macro series catalog & values
- `GET /serve/futures/contracts` — listed futures contracts
- `GET /serve/futures/continuous/{instrument_id}` — single-instrument continuous bars
- `GET /serve/calendar?market=<MARKET>` — trading calendar (`market` **required**)

Supported markets: `CRYPTO`, `US`, `FX`, `TW`, `HK`, `CN`, `WTX`, `GLOBAL`, `MACRO`.

For full parameter tables, response shapes, and edge cases, read
`references/endpoints.md`.

## Writing data — Source API

The Source API supports two payload styles. Pick **one** per endpoint.

### Style A — standard `IngestRequest` (wrapper format)

POST to `/api/v1/source/ingest/{market}` where `{market}` is one of
`crypto`, `us`, `fx`, `macro`, `wtx`, `global`, `tw`, `hk`, `cn`.

Body:

```json
{
  "dataset_key": "crypto_eod",
  "source": "bloomberg",
  "request_key": "bloomberg_crypto_20260116_144914",
  "idempotency_key": "bloomberg_crypto_20260116_144914",
  "payload": {
    "metadata": { "source": "Bloomberg API", "query_time": "2026-01-16T14:49:14Z" },
    "data": [ /* provider-shaped records */ ]
  },
  "fetched_at": "2026-01-16T14:49:14Z"
}
```

All six top-level keys are required. `idempotency_key` makes the call **idempotent**:
re-sending the same key returns the existing run and skips reprocessing. Use a stable,
collision-free key per upstream batch (e.g. `<source>_<market>_<yyyymmdd>_<hhmmss>`).

Response:

```json
{
  "success": true,
  "run_id": "019462f0-7c00-7000-8000-000000000001",
  "status": "pending",
  "message": "Data received, processing queued"
}
```

### Style B — direct format (provider-shaped, no wrapper)

For payloads that already match a provider's native shape, post the bare `{metadata,
data}` object to `/api/v1/source/ingest/{market}/direct`. FinDB infers `dataset_key`,
`source`, and `idempotency_key` from the payload.

| Endpoint | Auto `dataset_key` | Typical source |
| --- | --- | --- |
| `POST /ingest/crypto/direct` | `crypto_bloomberg_eod` | Bloomberg crypto |
| `POST /ingest/fx/direct` | `fx_bloomberg_eod` | Bloomberg FX |
| `POST /ingest/wtx/direct` | `wtx_eod` | Bloomberg / FinLab WTX futures |
| `POST /ingest/usstock/direct` | `us_stock_eod` | Bloomberg US equities |
| `POST /ingest/hkchina/direct` | `hkchina_mixed_eod` | Bloomberg HK + CN equity/index |
| `POST /ingest/hkchina-index/direct` | `hkchina_index_eod` | Legacy HK/CN index-only feed |
| `POST /ingest/macro/direct` | `macro_bloomberg_observation` | Bloomberg macro |
| `POST /ingest/twstock/direct` | `tw_equity_eod` / `tw_etf_eod` | FinLab TW equity / ETF (routes on `metadata.asset_class`) |

Sample minimal direct payload (US stock):

```json
{
  "metadata": { "source": "bloomberg", "query_time": "2026-02-04T16:00:51Z" },
  "data": [
    { "ticker": "AAPL US Equity", "date": "2026-02-04",
      "open": 231.0, "high": 233.0, "low": 230.5, "close": 232.5, "volume": 45000000 }
  ]
}
```

For per-market direct payload shapes, FinLab TW field naming, hkchina mixed routing
rules, and full curl examples, see `references/ingest-payloads.md` and
`assets/sample_payload.json` (a complete standard-format example).

### Tracking ingest runs

```bash
# Poll until status ∈ {completed, completed_with_errors, failed}
curl "https://findb.tingfong.com/api/v1/source/runs/<run_id>" \
  -H "X-API-Key: <key>"

# Re-run normalization for an existing run using the stored raw payload (creates a NEW run_id)
curl -X POST "https://findb.tingfong.com/api/v1/source/runs/<run_id>/rerun" \
  -H "X-API-Key: <key>"

# List active datasets (dataset_key, market, asset_class, frequency)
curl "https://findb.tingfong.com/api/v1/source/datasets" -H "X-API-Key: <key>"
```

Run statuses: `pending` → `running` → `completed` | `completed_with_errors` | `failed`.

## Pagination & errors at a glance

Every list response includes `pagination: { page, page_size, total_records,
total_pages }`. Iterate by incrementing `page` until `page > total_pages`.

| Status | Common cause |
| --- | --- |
| `400` | Unknown `dataset_key`, dataset inactive, market mismatch, missing field, invalid OHLCV patch |
| `401` | `X-API-Key` header missing |
| `403` | API key wrong, or Source client IP not in nginx allowlist |
| `404` | `instrument_id` / `run_id` / `series_id` / `issue_id` not found |
| `409` | Conflict (e.g. resolving an already-resolved DQ issue) |
| `422` | Pydantic schema validation failure on request body |
| `429` | Rate-limit (100 req / 60s per key+IP) exceeded |
| `500` | Missing required env (e.g. `ADMIN_API_KEY` not set, `SOURCE_ALLOWLIST_CIDRS` unset in prod) |

Body shape on error: `{"detail": "<message>"}`. For the full Source/Admin error
message table and how to interpret `completed_with_errors` runs (DQ flagged but data
written), see `references/responses.md`.

## Admin API (corrections, only when you need to fix data)

Most consumers won't touch Admin. If you do (e.g. patching a wrong close price,
resolving a DQ issue, inspecting raw payloads):

```bash
# List DQ issues filtered by severity
curl "https://findb.tingfong.com/api/v1/admin/dq-issues?severity=error&resolved=false" \
  -H "X-API-Key: <admin-key>"

# Patch a single EOD field (audited; writes to canonical_correction)
curl -X PATCH "https://findb.tingfong.com/api/v1/admin/eod/<instrument_id>/2026-01-16" \
  -H "X-API-Key: <admin-key>" \
  -H "Content-Type: application/json" \
  -d '{"close": 95710.00, "reason": "vendor correction"}'

# Mark a DQ issue resolved
curl -X PATCH "https://findb.tingfong.com/api/v1/admin/dq-issues/<issue_id>/resolve" \
  -H "X-API-Key: <admin-key>" \
  -H "Content-Type: application/json" \
  -d '{"resolution_note": "verified against source"}'
```

Full Admin surface (raw-payload inspection, corrections audit log, bulk rerun,
instrument-cache CRUD) is in `references/endpoints.md`.

## Where to dig deeper

- **`references/endpoints.md`** — every endpoint with method, path, parameters,
  response shape, and edge cases. Read this when you need a parameter the SKILL
  doesn't mention, or when validating a response field.
- **`references/ingest-payloads.md`** — per-market payload skeletons for both
  standard and direct formats, plus FinLab/Bloomberg/hkchina-specific quirks.
- **`references/responses.md`** — pagination wrapper details, the full error
  message catalog, and how to interpret `completed_with_errors` runs.
- **`assets/sample_payload.json`** — a complete, ready-to-POST standard-format
  payload for `/ingest/crypto`. Useful as a template to clone for other markets.

## Practical patterns

- **Backfill loop**: pull `dataset_key` list once via `/source/datasets`, iterate
  over (market, date), POST with a deterministic `idempotency_key` like
  `<source>_<market>_<yyyymmdd>`. Idempotency means safe to retry after partial
  failures.
- **Polling**: after every ingest, sleep a few seconds then GET
  `/source/runs/{run_id}` until status is terminal. Don't assume `completed`
  means error-free — also handle `completed_with_errors` (DQ warnings or partial
  writes; check `failed_records` and `error_message`).
- **Reads with auth disabled**: if `SERVE_REQUIRE_AUTH=false` (default in dev),
  you can drop the `X-API-Key` header entirely on Serve calls. Don't rely on
  this in production — check by hitting `/health` or a sample query first.
- **Browser clients hitting Serve in prod**: don't bake DB-backed Serve keys into
  JavaScript. If your page lives at `findb.tingfong.com/instrument-lookup` or
  a path nginx rewrites for, the key is injected server-side; otherwise route
  through your own backend.
