#!/usr/bin/env python3
"""Export bounded Source-to-canonical evidence for the five staging pilot feeds."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib import error, parse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.models.base import async_session_maker, engine
from app.services.calendar_management import published_year
from app.utils import utc_now

ACTIVE_FEEDS = (
    ("twelve_data", "us_equity_eod", "market_eod"),
    ("finlab", "tw_equity_eod", "market_eod"),
    ("shioaji", "tw_equity_minute", "market_minute"),
    ("shioaji", "tw_etf_minute", "market_minute"),
    ("taifex", "tw_futures_eod", "futures_eod"),
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
              ('shioaji', 'tw_etf_minute'),
              ('taifex', 'tw_futures_eod')
          )
          AND r.batch_data_date IS NOT NULL
    )
    SELECT r.source, r.dataset_key, r.run_id::text, r.batch_data_date::text,
           r.status, r.policy_outcome, r.schema_id, r.schema_version, r.total_records, r.success_records,
           (r.raw_payload_id IS NOT NULL) AS raw_persisted,
           coalesce(a.source_http_status, 0) AS source_http_status,
           coalesce(a.attempt_status, 'missing') AS attempt_status,
           coalesce(a.attempt_receipts, '[]'::jsonb) AS attempt_receipts,
           coalesce(j.status, 'missing') AS job_status,
           j.job_id::text AS job_id, j.delivery_id::text AS job_delivery_id,
           coalesce(o.outbox_receipts, '[]'::jsonb) AS outbox_receipts,
           coalesce(o.status, 'missing') AS outbox_status,
           (SELECT count(*) FROM dq_issue d
              WHERE d.run_id = r.run_id AND d.severity = 'error') AS dq_errors,
           CASE r.schema_id
               WHEN 'market_eod' THEN
                   (SELECT count(*) FROM market_data_eod c WHERE c.run_id = r.run_id)
               WHEN 'market_minute' THEN
                   (SELECT count(*) FROM market_data_minute c WHERE c.run_id = r.run_id)
               WHEN 'futures_eod' THEN
                   (SELECT count(*) FROM futures_contract_eod c WHERE c.run_id = r.run_id)
               ELSE 0
           END AS canonical_rows,
           CASE r.schema_id
             WHEN 'market_eod' THEN (
                SELECT jsonb_agg(x) FROM (
                  SELECT i.symbol, c.trade_date::text, count(*) AS row_count
                    FROM market_data_eod c JOIN instruments i USING (instrument_id)
                   WHERE c.run_id=r.run_id GROUP BY i.symbol,c.trade_date
                ) x)
             WHEN 'market_minute' THEN (
                SELECT jsonb_agg(x) FROM (
                  SELECT i.symbol,c.trade_date::text,count(*) AS row_count
                    FROM market_data_minute c JOIN instruments i USING (instrument_id)
                   WHERE c.run_id=r.run_id GROUP BY i.symbol,c.trade_date
                ) x)
             WHEN 'futures_eod' THEN (
                SELECT jsonb_agg(x) FROM (
                  SELECT i.symbol,c.trade_date::text,f.contract_code,f.contract_month,c.session,count(*) AS row_count
                    FROM futures_contract_eod c JOIN instruments i USING (instrument_id)
                    JOIN futures_contract f ON f.contract_id=c.contract_id
                   WHERE c.run_id=r.run_id GROUP BY i.symbol,c.trade_date,f.contract_code,f.contract_month,c.session
                ) x)
             ELSE NULL
           END AS canonical_coverage
    FROM ranked r
    LEFT JOIN LATERAL (
        SELECT min(http_status) AS source_http_status,
               CASE WHEN bool_and(http_status = 202 AND status IN ('accepted', 'duplicate'))
                    THEN 'accepted' ELSE 'inconsistent' END AS attempt_status,
               jsonb_agg(jsonb_build_object(
                   'attempt_id', attempt_id::text, 'run_id', run_id::text,
                   'source', source, 'dataset_key', dataset_key,
                   'schema_id', schema_id, 'schema_version', schema_version,
                   'http_status', http_status, 'status', status,
                   'request_sha256', request_sha256,
                   'created_at', created_at, 'completed_at', completed_at
               ) ORDER BY created_at, attempt_id) AS attempt_receipts
          FROM ingestion_attempt WHERE run_id = r.run_id
    ) a ON true
    LEFT JOIN normalization_job j ON j.run_id = r.run_id
    LEFT JOIN LATERAL (
        SELECT CASE WHEN count(*) FILTER (WHERE delivery_id = j.delivery_id) = 0
                    THEN 'missing'
                    WHEN count(DISTINCT status) FILTER (WHERE delivery_id = j.delivery_id) = 1
                    THEN min(status) FILTER (WHERE delivery_id = j.delivery_id)
                    ELSE 'inconsistent' END AS status,
               jsonb_agg(jsonb_build_object(
                   'outbox_id', outbox_id::text, 'job_id', job_id::text,
                   'run_id', run_id::text, 'delivery_id', delivery_id::text,
                   'status', status, 'event_type', event_type,
                   'created_at', created_at, 'published_at', published_at,
                   'publish_attempts', publish_attempts
               ) ORDER BY created_at, outbox_id) AS outbox_receipts
          FROM normalization_outbox WHERE run_id = r.run_id
    ) o ON true
    WHERE r.date_rank <= 2
    ORDER BY r.source, r.dataset_key, r.batch_data_date DESC, r.run_id
    """
)

SAMPLES = text(
    """
    WITH samples AS (
        (SELECT 'twelve_data' AS source, 'us_equity_eod' AS dataset_key,
                c.instrument_id::text AS sample_instrument_id,
                c.trade_date::text AS sample_trade_date, c.run_id::text AS sample_run_id,
                to_jsonb(c) || jsonb_build_object('open', c.open::text, 'high', c.high::text, 'low', c.low::text, 'close', c.close::text, 'total_ticks', c.total_ticks, 'turnover', c.turnover::text) AS sample_record
           FROM market_data_eod c
           JOIN instruments i ON i.instrument_id = c.instrument_id
          WHERE i.market = 'US' AND i.asset_class = 'equity' AND i.symbol IN ('AAPL','MSFT') AND c.source='twelve_data'
          ORDER BY c.trade_date DESC, c.instrument_id
          LIMIT 1)
        UNION ALL
        (SELECT 'finlab', 'tw_equity_eod', c.instrument_id::text, c.trade_date::text, c.run_id::text,
                to_jsonb(c) || jsonb_build_object('open', c.open::text, 'high', c.high::text, 'low', c.low::text, 'close', c.close::text, 'total_ticks', c.total_ticks, 'turnover', c.turnover::text)
           FROM market_data_eod c
           JOIN instruments i ON i.instrument_id = c.instrument_id
          WHERE i.market = 'TW' AND i.asset_class = 'equity' AND i.symbol IN ('2330','2317') AND c.source='finlab'
          ORDER BY c.trade_date DESC, c.instrument_id
          LIMIT 1)
        UNION ALL
        (SELECT 'shioaji', 'tw_equity_minute', c.instrument_id::text, c.trade_date::text, c.run_id::text,
                to_jsonb(c) || jsonb_build_object('open', c.open::text, 'high', c.high::text, 'low', c.low::text, 'close', c.close::text, 'turnover', c.turnover::text)
           FROM market_data_minute c
           JOIN instruments i ON i.instrument_id = c.instrument_id
          WHERE i.market = 'TW' AND i.asset_class = 'equity' AND i.symbol='2330' AND c.source='shioaji'
          ORDER BY c.bar_start_time DESC, c.instrument_id
          LIMIT 1)
        UNION ALL
        (SELECT 'shioaji', 'tw_etf_minute', c.instrument_id::text, c.trade_date::text, c.run_id::text,
                to_jsonb(c) || jsonb_build_object('open', c.open::text, 'high', c.high::text, 'low', c.low::text, 'close', c.close::text, 'turnover', c.turnover::text)
           FROM market_data_minute c
           JOIN instruments i ON i.instrument_id = c.instrument_id
          WHERE i.market = 'TW' AND i.asset_class = 'etf' AND i.symbol='0050' AND c.source='shioaji'
          ORDER BY c.bar_start_time DESC, c.instrument_id
          LIMIT 1)
    )
    SELECT source, dataset_key, sample_instrument_id, sample_trade_date, sample_run_id, sample_record
      FROM samples
     ORDER BY source, dataset_key
    """
)


FUTURES_SAMPLE = text("""
    SELECT 'taifex' AS source, 'tw_futures_eod' AS dataset_key,
           f.instrument_id::text AS sample_instrument_id,
           c.trade_date::text AS sample_trade_date, c.run_id::text AS sample_run_id,
           f.contract_code AS sample_contract_code, f.contract_month AS sample_contract_month,
           c.session AS sample_session,
           to_jsonb(c) || jsonb_build_object('open', c.open::text, 'high', c.high::text, 'low', c.low::text, 'close', c.close::text, 'settlement_price', c.settlement_price::text, 'contract_code', f.contract_code, 'contract_month', f.contract_month, 'product_code', i.symbol) AS sample_record
      FROM futures_contract_eod c JOIN futures_contract f USING (contract_id)
      JOIN instruments i ON i.instrument_id=f.instrument_id
     WHERE c.source='taifex' AND i.symbol IN ('TX','MTX') AND i.asset_class='future'
     ORDER BY c.trade_date DESC, f.contract_code, c.session LIMIT 1
""")


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


# Kept identical in the standalone collector: both scripts execute on separate hosts.
PROJECTION_FIELDS = {
    "market_eod": ("total_ticks", "turnover"),
    "market_minute": (
        "bar_start_time",
        "bar_end_time",
        "signal_time",
        "market_timezone",
        "turnover",
        "trade_count",
        "price_adjustment",
    ),
    "futures_eod": (
        "contract_id",
        "product_code",
        "contract_code",
        "contract_month",
        "session",
        "settlement_price",
        "open_interest",
    ),
}
NUMERIC_FIELDS = {
    "open",
    "high",
    "low",
    "close",
    "volume",
    "total_ticks",
    "turnover",
    "trade_count",
    "settlement_price",
    "open_interest",
}
TIME_FIELDS = {"source_fetched_at", "bar_start_time", "bar_end_time", "signal_time"}


def _canonical_projection(schema_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """Normalize only public canonical values, requiring provenance and all fields."""
    fields = (
        "instrument_id",
        "trade_date",
        "source",
        "source_fetched_at",
        "open",
        "high",
        "low",
        "close",
        "volume",
        *PROJECTION_FIELDS[schema_id],
    )
    result: dict[str, Any] = {}
    for field in fields:
        value = record[field]
        if field in TIME_FIELDS:
            if value is None:
                raise ValueError("canonical timestamp missing")
            stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError("canonical timestamp must be aware")
            value = stamp.astimezone(timezone.utc).isoformat(timespec="microseconds")
        elif field in NUMERIC_FIELDS and value is not None:
            numeric = Decimal(str(value))
            if not numeric.is_finite():
                raise ValueError("canonical numeric value must be finite")
            value = format(numeric.normalize(), "f") if numeric else "0"
        elif value is not None:
            value = str(value)
        result[field] = value
    if not result["source"]:
        raise ValueError("canonical source missing")
    return result


def _projection_fingerprint(projection: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _serve_probe(schema_id: str, run: dict[str, Any] | None) -> dict[str, Any]:
    """Probe one latest canonical sample through the internal Serve HTTP boundary."""
    if not run or not run.get("sample_instrument_id") or not run.get("sample_trade_date"):
        return {"success": False, "http_status": None, "reason": "canonical_sample_missing"}
    endpoint = {"market_eod": "eod", "market_minute": "minute", "futures_eod": "futures/eod"}[
        schema_id
    ]
    query = parse.urlencode(
        {
            "instrument_id": run["sample_instrument_id"],
            "start_date": run["sample_trade_date"],
            "end_date": run["sample_trade_date"],
            "page_size": 1000 if schema_id == "market_minute" else 1,
        }
    )
    if schema_id == "futures_eod":
        query += "&" + parse.urlencode(
            {"contract_code": run["sample_contract_code"], "session": run["sample_session"]}
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
            payload = json.load(response, parse_float=Decimal)
            items = payload.get("data") if isinstance(payload, dict) else None
            expected = _canonical_projection(schema_id, run["sample_record"])
            actual = None
            if (
                isinstance(payload, dict)
                and payload.get("success") is True
                and isinstance(items, list)
            ):
                for item in items:
                    projection = _canonical_projection(schema_id, item)
                    # Minute probes select the exact DB sample bar within the bounded day.
                    if projection == expected:
                        actual = projection
                        break
            matched = response.status == 200 and actual is not None
            return {
                "success": matched,
                "http_status": response.status,
                "instrument_id": run["sample_instrument_id"],
                "trade_date": run["sample_trade_date"],
                "run_id": run.get("sample_run_id") if matched else None,
                "projection_version": 1,
                "canonical_projection": expected,
                "returned_projection": actual,
                "canonical_fingerprint": _projection_fingerprint(expected),
                "returned_fingerprint": _projection_fingerprint(actual) if actual else None,
                "reason": None if matched else "canonical_content_mismatch",
            }
    except error.HTTPError as exc:
        return {"success": False, "http_status": exc.code, "reason": "http_error"}
    except (error.URLError, TimeoutError, ValueError, KeyError, TypeError, InvalidOperation):
        return {"success": False, "http_status": None, "reason": "probe_failed"}


async def _target_calendar_evidence(session: Any, observed_at: datetime) -> dict[str, Any]:
    """Export only official complete revision metadata and a bounded date window."""
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("evidence observation must be timezone-aware")
    observed_at = observed_at.astimezone(timezone.utc)
    markets = {}
    for market, timezone_name in {
        "US": "America/New_York",
        "TW": "Asia/Taipei",
        "TAIFEX": "Asia/Taipei",
    }.items():
        # Availability schedules use Taipei dates, including the US next-day window.
        today = observed_at.astimezone(ZoneInfo("Asia/Taipei")).date()
        window = [today - timedelta(days=offset) for offset in range(16)]
        revisions = []
        days: list[dict[str, Any]] = []
        valid = True
        for year in sorted({day.year for day in window}):
            published = await published_year(session, market, year)
            if published is None:
                valid = False
                continue
            revision, config, calendar_days = published
            valid = valid and revision.timezone == config.timezone == timezone_name
            revisions.append(
                {
                    "year": year,
                    "revision": revision.revision,
                    "status": revision.status,
                    "revision_id": str(revision.id),
                    "coverage_complete": True,
                    "source_kind": revision.source_kind,
                    "source_sha256": revision.source_sha256,
                    "timezone": revision.timezone,
                }
            )
            days.extend(
                {
                    "trade_date": day.trade_date.isoformat(),
                    "day_status": day.day_status,
                    "is_open": day.is_open,
                    "session_close": day.session_close.isoformat() if day.session_close else None,
                    "revision": revision.revision,
                    "revision_id": str(day.calendar_revision_id),
                }
                for day in calendar_days
                if day.trade_date in window
            )
        markets[market] = {
            "valid": valid,
            "timezone": timezone_name,
            "revisions": revisions,
            "days": days,
        }
    return {"schema_version": 1, "observed_at_utc": observed_at.isoformat(), "markets": markets}


async def export(*, observed_at: datetime | None = None) -> dict[str, Any]:
    observed_at = observed_at or utc_now()
    async with async_session_maker() as session:
        calendar_evidence = await _target_calendar_evidence(session, observed_at)
        rows = (await session.execute(RUNS)).mappings().all()
        sample_rows = [
            *(await session.execute(SAMPLES)).mappings().all(),
            *(await session.execute(FUTURES_SAMPLE)).mappings().all(),
        ]
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
                "canonical_sample": samples.get((source, dataset_key)),
            }
        )
    return {
        "active_feed_count": len(feeds),
        "feeds": feeds,
        "target_calendar_evidence": calendar_evidence,
        "evidence_kind": "live_pipeline_observation",
    }


async def main() -> int:
    try:
        print(json.dumps(await export(), separators=(",", ":"), sort_keys=True, default=str))
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
