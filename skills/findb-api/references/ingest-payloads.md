# Active Source contract payloads

All four active feeds use the same route and explicit versioned envelope:

```http
POST /api/v1/source/ingest
X-API-Key: <provider-scoped-source-key>
Content-Type: application/json
```

There is no market-suffixed route and no direct provider-shaped payload. The envelope
must contain `dataset_key`, `schema_id`, `schema_version`, `source`, `request_key`,
`idempotency_key`, `fetched_at`, and `payload`.

## Exact active mapping

| `source` | `dataset_key` | `schema_id` | Payload |
| --- | --- | --- | --- |
| `twelve_data` | `us_equity_eod` | `market_eod` v1 | EOD batch + rows |
| `finlab` | `tw_equity_eod` | `market_eod` v1 | EOD batch + rows |
| `shioaji` | `tw_equity_minute` | `market_minute` v1 | Sequenced minute batch |
| `shioaji` | `tw_etf_minute` | `market_minute` v1 | Sequenced minute batch |

Any other source/dataset combination is rejected by registry scope. Canonical read models
outside this table are not valid Source targets.

## Shared identity rules

- `request_key` identifies one fetch attempt; `idempotency_key` identifies a safely
  repeatable delivery.
- Reuse both keys and the same body on transport retry.
- `fetched_at`, and scheduler `delivery.scheduled_for` when present, must include a
  timezone; FinDB normalizes them to UTC.
- `payload.batch.declared_record_count` must equal the number of rows.
- A provider raw reference and SHA-256, when present, are credential-free and refer to
  the Fetcher-owned raw artifact.

## `market_eod.v1` — Twelve Data and FinLab

The EOD payload contains a `batch` and provider-neutral `data` rows:

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
        "volume": 25000000,
        "total_ticks": 120000
      }
    ]
  }
}
```

Required row fields are `symbol`, `trade_date`, and non-negative `close`. Include
`currency` when the dataset does not declare a default. `open`, `high`, `low`, `volume`,
`turnover`, and `total_ticks` are optional but must satisfy non-negative and OHLC bounds.
Use `full_snapshot` for a complete reviewed universe, `incremental` for changed rows,
and `backfill` only when the approved contract covers a bounded historical range.

### Twelve Data mapping notes

The adapter maps the provider response before building this contract:

- `meta.symbol` → `source_symbol` (and canonical `symbol` when approved).
- `meta.currency` → each row's `currency`.
- `values[].datetime` → `trade_date`.
- `open`, `high`, `low`, `close`, `volume` → same-named canonical fields.

Keep the reviewed universe and per-request record/date/credit limits; do not expand them
from an unreviewed runtime string.

### FinLab mapping notes

FinLab's reviewed equity output maps its symbol/date/OHLCV columns to the same provider-
neutral row. `tw_equity_eod` is the only active FinLab dataset. ETF EOD and other asset
classes are not active Source targets.

## `market_minute.v1` — Shioaji

The minute payload adds sequence identity and symbol outcomes:

```json
{
  "dataset_key": "tw_equity_minute",
  "schema_id": "market_minute",
  "schema_version": 1,
  "source": "shioaji",
  "request_key": "mmr:<sha256-of-canonical-sequence>",
  "idempotency_key": "mms:<sha256-of-canonical-sequence>",
  "fetched_at": "2026-07-24T07:00:00Z",
  "payload": {
    "batch": {
      "data_date": "2026-07-24",
      "coverage_start_date": "2026-07-24",
      "coverage_end_date": "2026-07-24",
      "delivery_mode": "sequenced_snapshot",
      "declared_record_count": 1,
      "snapshot_id": "snapshot-20260724",
      "daily_update_id": "update-20260724",
      "universe_id": "tw-pilot-v1",
      "symbols_sha256": "<sha256-of-sorted-symbols>",
      "sequence": 1,
      "sequence_count": 1,
      "provider_usage_before": { "requests_used": 1, "requests_limit": 50 },
      "provider_usage_after": { "requests_used": 2, "requests_limit": 50 },
      "anomalies": []
    },
    "symbol_statuses": [
      { "symbol": "2330", "outcome": "data" }
    ],
    "data": [
      {
        "symbol": "2330",
        "source_symbol": "2330",
        "trade_date": "2026-07-24",
        "market_timezone": "Asia/Taipei",
        "bar_start_time": "2026-07-24T01:00:00Z",
        "bar_end_time": "2026-07-24T01:01:00Z",
        "signal_time": "2026-07-24T01:01:00Z",
        "price_adjustment": "none",
        "trade_count": null,
        "open": "1140.00",
        "high": "1140.00",
        "low": "1140.00",
        "close": "1140.00",
        "volume": 1000,
        "turnover": 1140000
      }
    ]
  }
}
```

Minute invariants:

- `coverage_start_date` and `coverage_end_date` equal `data_date`.
- `bar_end_time = bar_start_time + 1 minute`, `signal_time = bar_end_time`, and all
  timestamps are timezone-aware UTC values.
- `trade_date` is the Taiwan-local date of `bar_start_time`.
- `symbols_sha256` hashes sorted `symbol_statuses` symbols joined by LF.
- A non-`data` symbol status must include a reason. A row requires a matching `data`
  status. Shioaji usage snapshots are paired and non-decreasing.
- A null minute `volume` or `turnover` requires exactly one matching credential-free
  anomaly record in `payload.batch.anomalies`.

The canonical minute identity is the SHA-256 of the compact sorted-key JSON object
`{"data_date":"…","dataset_key":"…","sequence":N,"snapshot_id":"…"}`.

## Status polling

After `202 Accepted`, poll `GET /api/v1/source/runs/{run_id}` with the same provider key.
Do not treat a read-model row or a retained historical dataset as permission to submit a
different payload shape.
