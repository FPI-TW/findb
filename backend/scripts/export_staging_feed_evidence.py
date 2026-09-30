#!/usr/bin/env python3
"""Export bounded Source-to-canonical evidence for the four active staging feeds."""

from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict
from typing import Any
from urllib import error, parse
from urllib.request import Request, urlopen

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

SAMPLES = text(
    """
    WITH samples AS (
        (SELECT 'twelve_data' AS source, 'us_equity_eod' AS dataset_key,
                c.instrument_id::text AS sample_instrument_id,
                c.trade_date::text AS sample_trade_date
           FROM market_data_eod c
           JOIN instruments i ON i.instrument_id = c.instrument_id
          WHERE i.market = 'US' AND i.asset_class = 'equity'
          ORDER BY c.trade_date DESC, c.instrument_id
          LIMIT 1)
        UNION ALL
        (SELECT 'finlab', 'tw_equity_eod', c.instrument_id::text, c.trade_date::text
           FROM market_data_eod c
           JOIN instruments i ON i.instrument_id = c.instrument_id
          WHERE i.market = 'TW' AND i.asset_class = 'equity'
          ORDER BY c.trade_date DESC, c.instrument_id
          LIMIT 1)
        UNION ALL
        (SELECT 'shioaji', 'tw_equity_minute', c.instrument_id::text, c.trade_date::text
           FROM market_data_minute c
           JOIN instruments i ON i.instrument_id = c.instrument_id
          WHERE i.market = 'TW' AND i.asset_class = 'equity'
          ORDER BY c.bar_start_time DESC, c.instrument_id
          LIMIT 1)
        UNION ALL
        (SELECT 'shioaji', 'tw_etf_minute', c.instrument_id::text, c.trade_date::text
           FROM market_data_minute c
           JOIN instruments i ON i.instrument_id = c.instrument_id
          WHERE i.market = 'TW' AND i.asset_class = 'etf'
          ORDER BY c.bar_start_time DESC, c.instrument_id
          LIMIT 1)
    )
    SELECT source, dataset_key, sample_instrument_id, sample_trade_date
      FROM samples
     ORDER BY source, dataset_key
    """
)


def _lineage_contract(schema_id: str) -> dict[str, Any]:
    return {
        "source": "required",
        "raw": "required",
        "normalize": "required",
        "canonical": "required",
        "serve": "required",
        "serve_reason": None,
        "operational_read_paths": ["serve", "admin_market_freshness", "dashboard"],
    }


def _serve_probe(schema_id: str, run: dict[str, Any] | None) -> dict[str, Any]:
    """Probe one latest canonical sample through the internal Serve HTTP boundary."""
    if not run or not run.get("sample_instrument_id") or not run.get("sample_trade_date"):
        return {"success": False, "http_status": None, "reason": "canonical_sample_missing"}
    endpoint = "eod" if schema_id == "market_eod" else "minute"
    query = parse.urlencode(
        {
            "instrument_id": run["sample_instrument_id"],
            "start_date": run["sample_trade_date"],
            "end_date": run["sample_trade_date"],
            "page_size": 1,
        }
    )
    base_url = (os.getenv("FINDB_STATIC_CACHE_BASE_URL") or "http://serve:8080").rstrip("/")
    headers = {"Accept": "application/json"}
    key = os.getenv("FINDB_STATIC_CACHE_SERVE_API_KEY", "").strip()
    if not key:
        return {"success": False, "http_status": None, "reason": "serve_credential_missing"}
    headers["X-API-Key"] = key
    request = Request(f"{base_url}/api/v1/serve/{endpoint}?{query}", headers=headers)
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.load(response)
            items = payload.get("data") if isinstance(payload, dict) else None
            matched = bool(
                payload.get("success") is True
                and isinstance(items, list)
                and items
                and str(items[0].get("instrument_id")) == run["sample_instrument_id"]
                and str(items[0].get("trade_date")) == run["sample_trade_date"]
            )
            return {
                "success": matched,
                "http_status": response.status,
                "instrument_id": run["sample_instrument_id"],
                "trade_date": run["sample_trade_date"],
                "reason": None if matched else "sample_not_returned",
            }
    except error.HTTPError as exc:
        return {"success": False, "http_status": exc.code, "reason": "http_error"}
    except (error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return {"success": False, "http_status": None, "reason": "probe_failed"}


async def export() -> dict[str, Any]:
    async with async_session_maker() as session:
        rows = (await session.execute(RUNS)).mappings().all()
        sample_rows = (await session.execute(SAMPLES)).mappings().all()
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row["source"]), str(row["dataset_key"]))].append(dict(row))
    samples = {(str(row["source"]), str(row["dataset_key"])): dict(row) for row in sample_rows}
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
                "serve_probe": await asyncio.to_thread(
                    _serve_probe, schema_id, samples.get((source, dataset_key))
                ),
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
