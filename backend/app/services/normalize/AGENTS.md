# NORMALIZE KNOWLEDGE BASE

Apply root `AGENTS.md` first. This file adds rules only for
`app/services/normalize/`.

## Overview

The staging normalizer accepts only provider-neutral versioned contracts:

- `market_eod.v1` via `MarketEODContractNormalizer`
- `market_minute.v1` via `MarketMinuteContractNormalizer`

`app/services/ingestion.py` is the sole dispatch entry point. It resolves a
normalizer only from an explicit `(schema_id, schema_version)` pair; missing or
unsupported metadata fails closed. Legacy provider/direct normalizer modules
are intentionally absent.

## Conventions

- New schema normalizers subclass `BaseNormalizer` and implement `map_fields`.
- Dataset defaults are validated by `ingress_contracts.py`; do not infer market,
  asset class, currency, or provider from arbitrary payload metadata.
- Evaluate DQ errors before canonical writes and preserve idempotent upserts.
- Trade timestamps are UTC-aware and all writes use async SQLAlchemy sessions.

## Anti-patterns

- Reintroducing dataset-key or provider-aware legacy routing.
- Accepting direct-format payloads outside the versioned contract schemas.
- Writing canonical rows before DQ error evaluation.
- Coupling HTTP request parsing or credential authorization into normalizers.
