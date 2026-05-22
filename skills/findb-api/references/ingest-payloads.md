# Source API Payload Reference

Per-market payload shapes for both ingest styles:

- **Standard** (`POST /api/v1/source/ingest/{market}`) — full `IngestRequest`
  wrapper around the provider payload.
- **Direct** (`POST /api/v1/source/ingest/{market}/direct`) — bare provider
  payload; FinDB infers `dataset_key` / `source` / `idempotency_key`.

All examples are valid request bodies — copy, change the values, POST.
See `../assets/sample_payload.json` for a complete standard-format crypto payload.

## Standard `IngestRequest` template

```json
{
  "dataset_key": "<dataset_key>",
  "source": "<lowercase provider name>",
  "request_key": "<upstream request id>",
  "idempotency_key": "<deterministic dedupe key>",
  "payload": {
    "metadata": { "source": "<provider>", "query_time": "2026-01-16T14:49:14Z" },
    "data": [ /* provider-shaped records */ ]
  },
  "fetched_at": "2026-01-16T14:49:14Z"
}
```

**Idempotency key**: use a deterministic string per upstream batch, e.g.
`<source>_<market>_<yyyymmdd>_<hhmmss>`. Replaying with the same key is a no-op.

**dataset_key**: discover valid keys via `GET /api/v1/source/datasets`. Common
values: `crypto_eod`, `us_stock_eod`, `fx_eod`, `wtx_eod`, `tw_equity_eod`,
`tw_etf_eod`, `hk_equity_eod`, `cn_equity_eod`, `macro_bloomberg_observation`.

## Direct payload base shape

```json
{
  "metadata": {
    "source": "bloomberg",
    "query_time": "2026-02-04T16:00:51Z"
  },
  "data": [ /* records; shape depends on market */ ]
}
```

- `metadata.query_time` is recorded as the batch fetch time.
- `metadata.source` is normalized to lowercase; defaults to the endpoint's
  provider if omitted.
- `data` accepts flat records by default; nested `price`/`timestamp` shapes
  are also tolerated for backward compatibility with the original Bloomberg
  exports.

---

## Crypto

### Direct — `POST /ingest/crypto/direct`

Flat OHLCV per ticker.

```json
{
  "metadata": { "source": "bloomberg", "query_time": "2026-01-16T14:49:14Z" },
  "data": [
    { "ticker": "XBTUSD BGN Curncy", "date": "2026-01-16",
      "open": 95550.07, "high": 95825.34, "low": 95119.76, "close": 95709.01,
      "volume": 18500 }
  ]
}
```

### Standard — `POST /ingest/crypto`

The crypto standard payload accepts the original Bloomberg nested shape inside
`payload.data` (see `assets/sample_payload.json` for the full example):

```json
{
  "dataset_key": "crypto_eod",
  "source": "bloomberg",
  "request_key": "bloomberg_crypto_20260116_144914",
  "idempotency_key": "bloomberg_crypto_20260116_144914",
  "payload": {
    "metadata": {
      "source": "Bloomberg API",
      "category": "Cryptocurrency",
      "query_time": "2026-01-16T14:49:14.910965",
      "total_records": 1
    },
    "data": [
      {
        "crypto_id": "bitcoin",
        "symbol": "BTC",
        "name": "Bitcoin",
        "ticker": "XBTUSD BGN Curncy",
        "price": { "last": 95709.01, "open": 95550.07, "high": 95825.34, "low": 95119.76 },
        "change": { "net": 158.81, "percent_1d": 0.1662 },
        "timestamp": { "query_time": "2026-01-16T14:49:14", "last_update": "2026-01-16" },
        "metadata": { "source": "Bloomberg", "data_type": "cryptocurrency" }
      }
    ]
  },
  "fetched_at": "2026-01-16T14:49:14Z"
}
```

---

## US equities

### Direct — `POST /ingest/usstock/direct`

```json
{
  "metadata": { "source": "bloomberg", "query_time": "2026-02-04T16:00:51Z" },
  "data": [
    { "ticker": "AAPL US Equity", "date": "2026-02-04",
      "open": 231.0, "high": 233.0, "low": 230.5, "close": 232.5, "volume": 45000000 },
    { "ticker": "MSFT US Equity", "date": "2026-02-04",
      "open": 410.5, "high": 415.0, "low": 408.0, "close": 412.7, "volume": 22000000 }
  ]
}
```

The `ticker` suffix (` US Equity`) is parsed; only the leading symbol enters
canonical. Indices (e.g. `SPX Index`) are also accepted and routed to
`asset_class=index`.

---

## FX

### Direct — `POST /ingest/fx/direct`

```json
{
  "metadata": { "source": "bloomberg", "query_time": "2026-02-04T16:00:51Z" },
  "data": [
    { "ticker": "EURUSD BGN Curncy", "date": "2026-02-04",
      "open": 1.0825, "high": 1.0847, "low": 1.0810, "close": 1.0840 }
  ]
}
```

FX records typically omit `volume` (set to `null` or drop entirely).

---

## HK + CN (mixed)

### Direct — `POST /ingest/hkchina/direct`

Mixed payload: equity + index in one batch. Routing is per-ticker based on the
Bloomberg suffix:
- ` HK Equity` → market `HK`, asset_class `equity`
- ` Index` (Shanghai/Shenzhen tickers `SH...` / `SZ...`) → market `CN`,
  asset_class `index`
- ` Index` (HK Hang Seng family `HSI`, `HSCEI`, ...) → market `HK`, asset_class `index`

```json
{
  "metadata": { "source": "bloomberg", "query_time": "2026-04-08T02:29:42Z" },
  "data": [
    { "ticker": "700 HK Equity", "date": "2026-04-08",
      "open": 558.0, "high": 567.0, "low": 550.0, "close": 560.0, "volume": 23494910 },
    { "ticker": "SH000911 Index", "date": "2026-04-08",
      "open": 5941.521, "high": 5966.992, "low": 5940.137, "close": 5960.012, "volume": 0 }
  ]
}
```

### Legacy — `POST /ingest/hkchina-index/direct`

Index-only feed for the older pipeline. Same record shape but no equities allowed.

---

## TW (FinLab)

### Direct — `POST /ingest/twstock/direct`

`TWStockDirectIngestPayload`. Routes on `metadata.asset_class`:
- `STOCK` / `equity` / `stock` (or absent) → `tw_equity_eod`
- `ETF` / `etf` → `tw_etf_eod`

```json
{
  "metadata": {
    "symbol": "2330",
    "name": "台積電",
    "source": "finlab",
    "asset_class": "STOCK",
    "file_name": "finlab_stocks_ohlcv.jsonl",
    "query_time": "2026-05-14T13:30:00Z"
  },
  "data": [
    { "date": "2026-05-14",
      "open": 2250, "high": 2270, "low": 2230, "close": 2270,
      "total_volume": 39564699, "total_ticks": 83872 }
  ]
}
```

| Field | Notes |
| --- | --- |
| `metadata.symbol` | Required **unless** every `data[]` row carries its own `symbol` |
| `data[].symbol` | Required when `metadata.symbol` absent |
| `data[].date` | `YYYY-MM-DD`; for delisted symbols FinLab returns the last trading date (≠ `metadata.query_time`) |
| `data[].open/high/low/close` | Required |
| `data[].total_volume` | Required; `volume` accepted as alias |
| `data[].total_ticks` | Required |
| `data[].time` | Optional; kept in raw only, never in canonical |

Legacy MultiCharts fields (`up_volume`, `down_volume`, `up_ticks`, `down_ticks`)
were removed; FinLab doesn't supply them.

---

## WTX (台指期 futures)

### Direct — `POST /ingest/wtx/direct`

```json
{
  "metadata": { "source": "bloomberg", "query_time": "2026-01-16T14:49:14Z" },
  "data": [
    { "ticker": "TXFH6 Index", "date": "2026-01-16",
      "open": 22500.0, "high": 22650.0, "low": 22400.0, "close": 22600.0,
      "volume": 120000 }
  ]
}
```

`metadata.source` selects the normalizer (`bloomberg` vs `finlab`); both
write to `dataset_key=wtx_eod`. Contract code (`TXFH6`, `TXFM6`, ...) is parsed
into the futures_contract registry.

---

## Macro

### Direct — `POST /ingest/macro/direct`

```json
{
  "metadata": {
    "source": "bloomberg",
    "query_time": "2026-01-15T08:00:00Z"
  },
  "data": [
    { "source_code": "CPI_YOY", "series_name": "US CPI YoY",
      "obs_date": "2026-01-15", "value": 3.2,
      "unit": "percent", "frequency": "monthly" }
  ]
}
```

If `market` is omitted, macro defaults to `MACRO`. The series registry is
auto-created on first observation, then reused.

---

## Tracking and replay

```bash
# Look up run status
curl "https://findb.tingfong.com/api/v1/source/runs/<run_id>" \
  -H "X-API-Key: <key>"

# Re-run normalization (new run_id returned)
curl -X POST "https://findb.tingfong.com/api/v1/source/runs/<run_id>/rerun" \
  -H "X-API-Key: <key>"
```

Re-runs only work while the raw payload is still inside the
`RAW_RETENTION_DAYS` window (default 14 days; retention may be disabled in prod).
Past that, you have to re-fetch from the upstream provider.

---

## Idempotency reminders

- Re-POSTing the same `idempotency_key` returns the original `run_id` with
  `message: "Duplicate idempotency_key, returning existing run"`. The payload
  body is **not** re-checked, so changing data while keeping the key is silently
  ignored.
- If you re-fetch with **different** content for the same logical batch (rare),
  use a new key (e.g. append `_v2`).
- A backfill loop is safe to retry: design the key as a pure function of the
  upstream batch (`<source>_<market>_<yyyymmdd>` for daily, plus session if
  intraday).
