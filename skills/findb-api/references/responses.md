# Response Shapes, Pagination & Errors

## Pagination wrapper

Every Serve list endpoint and most Admin list endpoints share the same wrapper:

```json
{
  "success": true,
  "data": [ /* result records */ ],
  "pagination": {
    "page": 1,
    "page_size": 100,
    "total_records": 2500,
    "total_pages": 25
  }
}
```

Params:

| Param | Default | Range | Description |
| --- | --- | --- | --- |
| `page` | `1` | `≥ 1` | 1-indexed page number |
| `page_size` | `100` | `1`–`1000` | Records per page |

Iteration: keep incrementing `page` until `page > total_pages`. Avoid using
`total_records` to compute offsets — `page` + `page_size` is the contract.

```python
import httpx

def fetch_all(client: httpx.Client, url: str, params: dict, *, page_size: int = 500):
    page = 1
    while True:
        resp = client.get(url, params={**params, "page": page, "page_size": page_size})
        resp.raise_for_status()
        body = resp.json()
        yield from body["data"]
        if page >= body["pagination"]["total_pages"]:
            return
        page += 1
```

## Ingest run lifecycle

`GET /api/v1/source/runs/{run_id}` returns one of these statuses:

| Status | Meaning | Action |
| --- | --- | --- |
| `pending` | Queued, normalize hasn't started | Wait, then poll |
| `running` | Normalizer is processing | Wait, then poll |
| `completed` | All records written, no DQ issues at `error` severity | Done |
| `completed_with_errors` | Partial write or DQ warnings/errors triggered | Inspect `failed_records` and `error_message`; consider `/admin/dq-issues` |
| `failed` | Run hit a fatal error before/while writing | Read `error_message`; payload still in raw store for `RAW_RETENTION_DAYS` |

`completed_with_errors` is the most commonly misread status — it does NOT mean
"all good". Check:
- `total_records` vs `success_records` vs `failed_records`
- `GET /api/v1/admin/dq-issues?run_id=<run_id>` if you have admin access
- The `error_message` field (may be a summary like "5 records failed DQ checks")

## Error response shape

```json
{ "detail": "<human-readable message>" }
```

The HTTP status code is the primary signal; the `detail` string is for
debugging. Don't pattern-match on the detail text in production — it's not a
stable contract.

## HTTP status codes

| Code | Meaning | Common root cause |
| --- | --- | --- |
| `200` | OK | — |
| `400` | Bad request | Unknown `dataset_key`, dataset deactivated, market mismatch (payload market vs endpoint), malformed OHLCV patch, empty cache PUT |
| `401` | Missing auth | `X-API-Key` header absent |
| `403` | Forbidden | API key wrong, or Source request from non-allowlisted IP |
| `404` | Not found | `instrument_id` / `run_id` / `series_id` / `issue_id` doesn't exist; raw payload expired (past `RAW_RETENTION_DAYS`) |
| `409` | Conflict | Trying to resolve an already-resolved DQ issue |
| `422` | Validation | Pydantic schema failure on request body (wrong type, missing required field) |
| `429` | Rate limit | >100 requests / 60s for the same `(API key, client IP)` |
| `500` | Server error | Required runtime configuration missing (`SOURCE_ALLOWLIST_CIDRS` unset in prod) |

## Source API — known error messages

| Message | Status | Cause |
| --- | --- | --- |
| `Missing API key` | 401 | `X-API-Key` header absent |
| `Invalid API key` | 403 | Key value does not match an active DB-backed Source client |
| `Source API client IP not allowlisted` | 403 | Caller's real IP not in nginx allowlist; if behind Cloudflare, ensure nginx trusts `CF-Connecting-IP` |
| `Rate limit exceeded` | 429 | Backoff; resets at next minute boundary |
| `Dataset '<key>' not found` | 400 | Check `GET /source/datasets`; you may need to use the direct endpoint instead |
| `Dataset '<key>' is inactive` | 400 | Dataset registry disabled this key |
| `Market mismatch...` | 400 | Payload's inferred market ≠ endpoint's market (e.g. posting `MSFT` to `/ingest/tw`) |

## Admin API — known error messages

| Message | Status | Cause |
| --- | --- | --- |
| `Missing API key` | 401 | Header absent |
| `Invalid API key` | 403 | Wrong admin key |
| `No admin credential configured` | 500 | No active DB-backed Admin key or break-glass credential is configured |
| `EOD record not found...` | 404 | `(instrument_id, trade_date)` has no row |
| `DQ issue ... not found` | 404 | Wrong `issue_id` |
| `Raw payload not found` | 404 | Run's raw payload aged out (past `RAW_RETENTION_DAYS`) |
| `Instrument cache not found...` | 404 | Cron hasn't generated the cache file yet |
| `Instrument ... not found in cache` | 404 | Cache exists but doesn't include that `instrument_id` |
| `Instrument cache ... invalid` | 400 | PUT body doesn't conform to schema |
| `No OHLCV fields provided...` | 400 | PATCH body has no OHLCV keys |
| `No changes detected...` | 400 | All submitted values equal current values |
| `DQ issue ... is already resolved` | 409 | Issue already in resolved state |

## DQ check severities

Normalizers run data-quality checks before writing canonical:

| Check | Severity | Effect |
| --- | --- | --- |
| OHLC integrity (`high >= max(open, close)`, `low <= min(open, close)`) | error | Blocks write for that record |
| Volume non-negative | error | Blocks write |
| `(instrument_id, trade_date)` uniqueness | error | Blocks write (re-running upsert is fine) |
| Single-day return > ±30% | warning | Writes, raises DQ issue |
| Missing OHLC fields | warning | Writes (with nulls), raises DQ issue |

`error`-severity failures contribute to `failed_records` and may flip the run to
`completed_with_errors`. `warning` records still land in canonical but appear
in `/admin/dq-issues` for manual review.
