#!/usr/bin/env python3
"""Export bounded Source-to-canonical evidence for the four active staging feeds."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any

from sqlalchemy import text

from app.models.base import async_session_maker, engine

ACTIVE_FEEDS = (
    ("twelve_data", "us_equity_eod", "market_eod"),
    ("finlab", "tw_equity_eod", "market_eod"),
    ("shioaji", "tw_equity_minute", "market_minute"),
    ("shioaji", "tw_etf_minute", "market_minute"),
)

RUNS = text(
    """
    WITH ranked AS (
        SELECT r.*,
               dense_rank() OVER (
                   PARTITION BY r.source, r.dataset_key ORDER BY r.batch_data_date DESC
               ) AS date_rank
        FROM ingestion_run r
        WHERE r.is_rerun IS false
          AND (r.source, r.dataset_key) IN (
              ('twelve_data', 'us_equity_eod'),
              ('finlab', 'tw_equity_eod'),
              ('shioaji', 'tw_equity_minute'),
              ('shioaji', 'tw_etf_minute')
          )
          AND r.batch_data_date IS NOT NULL
    )
    SELECT r.source, r.dataset_key, r.run_id::text, r.batch_data_date::text,
           r.status, r.policy_outcome, r.total_records, r.success_records,
           (r.raw_payload_id IS NOT NULL) AS raw_persisted,
           coalesce(a.http_status, 0) AS source_http_status,
           coalesce(a.status, 'missing') AS attempt_status,
           coalesce(j.status, 'missing') AS job_status,
           coalesce(o.status, 'missing') AS outbox_status,
           (SELECT count(*) FROM dq_issue d
              WHERE d.run_id = r.run_id AND d.severity = 'error') AS dq_errors,
           CASE r.schema_id
               WHEN 'market_eod' THEN
                   (SELECT count(*) FROM market_data_eod c WHERE c.run_id = r.run_id)
               WHEN 'market_minute' THEN
                   (SELECT count(*) FROM market_data_minute c WHERE c.run_id = r.run_id)
               ELSE 0
           END AS canonical_rows
    FROM ranked r
    LEFT JOIN ingestion_attempt a ON a.run_id = r.run_id
    LEFT JOIN normalization_job j ON j.run_id = r.run_id
    LEFT JOIN normalization_outbox o ON o.run_id = r.run_id
    WHERE r.date_rank <= 2
    ORDER BY r.source, r.dataset_key, r.batch_data_date DESC, r.run_id
    """
)


def _lineage_contract(schema_id: str) -> dict[str, Any]:
    if schema_id == "market_minute":
        return {
            "source": "required",
            "raw": "required",
            "normalize": "required",
            "canonical": "required",
            "serve": "not_applicable",
            "serve_reason": "market_minute_read_model_not_exposed",
            "operational_read_paths": ["admin_raw", "admin_market_freshness", "dashboard"],
        }
    return {
        "source": "required",
        "raw": "required",
        "normalize": "required",
        "canonical": "required",
        "serve": "required",
        "serve_reason": None,
        "operational_read_paths": ["serve", "admin_market_freshness", "dashboard"],
    }


async def export() -> dict[str, Any]:
    async with async_session_maker() as session:
        rows = (await session.execute(RUNS)).mappings().all()
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row["source"]), str(row["dataset_key"]))].append(dict(row))
    feeds = []
    for source, dataset_key, schema_id in ACTIVE_FEEDS:
        evidence = grouped[(source, dataset_key)]
        dates = sorted({str(item["batch_data_date"]) for item in evidence}, reverse=True)
        feeds.append(
            {
                "source": source,
                "dataset_key": dataset_key,
                "schema_id": schema_id,
                "lineage_contract": _lineage_contract(schema_id),
                "observed_trade_dates": dates,
                "multi_trade_date_ready": len(dates) >= 2,
                "runs": evidence,
            }
        )
    return {"active_feed_count": len(feeds), "feeds": feeds}


async def main() -> int:
    try:
        print(json.dumps(await export(), separators=(",", ":"), sort_keys=True, default=str))
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
