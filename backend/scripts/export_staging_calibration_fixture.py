#!/usr/bin/env python3
"""Export one non-secret retained Twelve Data request for staging calibration."""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import text

from app.models.base import async_session_maker, engine

QUERY = text(
    """
    SELECT p.dataset_key, p.schema_id, p.schema_version, p.source,
           p.request_key, p.idempotency_key, p.fetched_at, p.payload
    FROM raw.market_payload p
    JOIN ingestion_run r ON r.run_id = p.run_id
    WHERE p.source = 'twelve_data'
      AND p.dataset_key = 'us_equity_eod'
      AND r.status = 'completed'
      AND r.is_rerun IS false
      AND jsonb_array_length(p.payload->'data') > 0
    ORDER BY r.batch_data_date DESC, p.created_at DESC
    LIMIT 1
    """
)


async def main() -> int:
    try:
        async with async_session_maker() as session:
            row = (await session.execute(QUERY)).mappings().one()
        print(json.dumps(dict(row), separators=(",", ":"), sort_keys=True, default=str))
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
