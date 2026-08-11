# Response Shapes, Pagination, Runs, and Errors

## Pagination wrapper

Serve list endpoints and most Admin list endpoints use:

```json
{
  "success": true,
  "data": [],
  "pagination": {
    "page": 1,
    "page_size": 100,
    "total_records": 0,
    "total_pages": 0
  }
}
```

`page` starts at 1. `page_size` is bounded by the endpoint (normally 1–1000). Continue
until `page >= total_pages`; use cursor pagination for instruments when the response
provides `next_cursor`.

```python
import httpx


def fetch_all(client: httpx.Client, url: str, params: dict, *, page_size: int = 500):
    page = 1
    while True:
        response = client.get(url, params={**params, "page": page, "page_size": page_size})
        response.raise_for_status()
        body = response.json()
        yield from body["data"]
        pagination = body.get("pagination") or {}
        if page >= pagination.get("total_pages", page):
            return
        page += 1
```

## Source ingest response

`POST /api/v1/source/ingest` returns `202 Accepted` with:

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

The response means raw, run, normalization job, and outbox records were durably accepted.
It does not mean canonical rows are ready. A same-key/same-content retry returns the same
run with a duplicate message; a same-key/different-content request returns a conflict.

## Ingest run lifecycle

`GET /api/v1/source/runs/{run_id}` returns:

| Status | Meaning | Action |
| --- | --- | --- |
| `pending` | Accepted and queued | Poll again |
| `running` | Normalization is processing | Poll again |
| `completed` | All records passed blocking DQ and were written | Done |
| `completed_with_errors` | Some rows failed DQ/write or warnings remain | Inspect counts and DQ issues |
| `failed` | Terminal processing failure | Inspect failure code; retry only with the same policy |

Always compare `total_records`, `success_records`, and `failed_records`. Keep both
`attempt_id` and `run_id` for audit. A retained raw contract can be requeued with
`POST /api/v1/source/runs/{run_id}/rerun`; the rerun has a new run ID and is bounded by
the configured raw-retention window.

## Error response shape

```json
{
  "detail": "human-readable message"
}
```

Use the HTTP status and, for Source contract errors, the stable error code. Do not pattern-
match free-form detail text as a long-term contract.

| Code | Meaning | Typical cause |
| --- | --- | --- |
| `200` | Successful read/mutation | — |
| `202` | Contract durably accepted | Source ingest |
| `400` | Request or policy error | Dataset/source scope or semantic policy |
| `401` | Missing authentication | No `X-API-Key` or session |
| `403` | Forbidden | Wrong key, provider/dataset scope, or staging IP policy |
| `404` | Resource not found | Unknown run/instrument or expired raw |
| `409` | Conflict | Idempotency or state conflict |
| `422` | Validation failure | Wrong versioned contract field/type |
| `429` | Rate limit | Back off and honor `Retry-After` |
| `500` | Unexpected/configuration error | Server fault |
| `503` | Temporary unavailable | DB/queue outage; retry same key |

## Source contract error categories

| Category | What to check |
| --- | --- |
| `INGRESS_SCHEMA_INVALID` | `schema_id`, version, required envelope/payload fields, UTC timestamps |
| Dataset/source scope rejection | One of the four exact provider/dataset pairs and the Source key allowlist |
| Idempotency conflict | Same key was reused with a different body or schema identity |
| Payload limit rejection | Data item count or serialized body exceeds staging limits |
| DQ/normalization failure | Run status, failed records, and Admin DQ issue detail |

## DQ severity

Normalizers run data-quality checks before canonical upsert:

| Check | Severity | Effect |
| --- | --- | --- |
| OHLC integrity | error | Blocks the affected row |
| Non-negative volume/turnover | error | Blocks the affected row |
| Natural-key uniqueness | error | Blocks duplicate row |
| Suspicious return or missing optional value | warning | Writes row and records a DQ issue |

An `error` can produce `completed_with_errors`; a warning does not by itself block a row.
Use Admin DQ endpoints only with a named session or DB-backed Admin key.

## Read-model response notes

Serve responses may include `source`, dates, and retained historical domains. These fields
are lineage/read-model metadata; they do not authorize Source writes or imply an active
provider feed. Read-only canonical data remains available even when an active feed is
stopped or a provider is not configured.
