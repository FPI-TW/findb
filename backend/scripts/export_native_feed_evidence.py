"""Read-only seven-feed evidence for an explicit execution profile and trade date."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib import parse
from urllib.request import Request, urlopen

from sqlalchemy import select, text

from app.config import get_settings
from app.models.base import async_session_maker, engine
from app.models.registry import DailyDeliveryPlan
from app.services.full_market import get_plan
from app.services.sequenced_snapshots import list_sequenced_snapshot_groups
from scripts.export_staging_feed_evidence import _canonical_projection, _projection_fingerprint


def _serve_probe(schema, sample):
    endpoint = {"market_eod": "eod", "market_minute": "minute", "futures_eod": "futures/eod"}[
        schema
    ]
    query = {
        "instrument_id": sample["sample_instrument_id"],
        "start_date": sample["sample_trade_date"],
        "end_date": sample["sample_trade_date"],
        "page_size": 1000,
    }
    if schema == "futures_eod":
        query.update(contract_code=sample["sample_contract_code"], session=sample["sample_session"])
    key = os.environ.get("FINDB_STATIC_CACHE_SERVE_API_KEY", "").strip()
    if not key:
        return {"success": False, "reason": "serve_credential_missing"}
    base = os.environ.get("FINDB_STATIC_CACHE_BASE_URL", "http://serve:8080").rstrip("/")
    request = Request(
        base + "/api/v1/serve/" + endpoint + "?" + parse.urlencode(query),
        headers={"X-API-Key": key, "Accept": "application/json"},
    )
    try:
        with urlopen(request, timeout=10) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                return {"success": False, "reason": "serve_response_too_large"}
            payload = json.loads(raw, parse_float=Decimal)
            expected = _canonical_projection(schema, sample["sample_record"])
            if response.status != 200 or payload.get("success") is not True:
                return {"success": False, "reason": "serve_response_failed"}
            matches = [
                _canonical_projection(schema, row)
                for row in payload["data"]
                if _canonical_projection(schema, row) == expected
            ]
            return {
                "success": bool(matches),
                "http_status": response.status,
                "canonical_projection": expected,
                "returned_projection": matches[0] if matches else None,
                "canonical_sha256": _projection_fingerprint(expected),
            }
    except Exception:
        return {"success": False, "reason": "serve_probe_failed"}


async def export(spec):
    environment, profile = spec["environment"], spec["execution_profile"]
    if (
        environment not in ("staging", "production")
        or environment != get_settings().APP_ENVIRONMENT
    ):
        raise ValueError("environment mismatch")
    if profile not in ("pilot", "full_market"):
        raise ValueError("invalid execution profile")
    trade_date = date.fromisoformat(spec["trade_date"])
    catalog = json.loads(Path("configs/execution_feeds.v1.json").read_text())["feeds"]
    feeds = []
    async with async_session_maker() as db:
        await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        for feed in catalog:
            provider, dataset = feed["provider"], feed["dataset_key"]
            item = {
                "provider": provider,
                "dataset_key": dataset,
                "plan": None,
                "sample": None,
                "passed": False,
            }
            feeds.append(item)
            plan = await db.scalar(
                select(DailyDeliveryPlan).where(
                    DailyDeliveryPlan.provider == provider,
                    DailyDeliveryPlan.dataset_key == dataset,
                    DailyDeliveryPlan.execution_profile == profile,
                    DailyDeliveryPlan.trade_date == trade_date,
                )
            )
            if plan is None:
                item["reason"] = "plan_not_created"
                continue
            details = (
                await get_plan(db, plan.plan_id, provider=provider, allowed_datasets=[dataset])
            ).model_dump(mode="json")
            parts = details.pop("parts")
            details["summary"].pop("gaps")
            details["parts_sha256"] = hashlib.sha256(
                json.dumps(
                    parts, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            details["part_count"] = len(parts)
            details["member_sha256"] = plan.member_sha256
            details["expected_count"] = plan.expected_count
            item["plan"] = details
            schema = (
                "futures_eod"
                if dataset == "tw_futures_eod"
                else "market_minute"
                if dataset.endswith("_minute")
                else "market_eod"
            )
            params = {
                "plan_id": plan.plan_id,
                "provider": provider,
                "dataset": dataset,
                "trade_date": trade_date,
                "market": feed["market"],
                "asset_class": feed["asset_class"],
            }
            if schema == "futures_eod":
                canonical_join = "JOIN futures_contract f ON f.instrument_id=i.instrument_id AND f.contract_code=m.contract_code JOIN futures_contract_eod c ON c.contract_id=f.contract_id AND c.session=m.session"
                record = "to_jsonb(c)||jsonb_build_object('contract_code',f.contract_code,'contract_month',f.contract_month,'product_code',i.symbol)"
            else:
                table = "market_data_minute" if schema == "market_minute" else "market_data_eod"
                canonical_join = f"JOIN {table} c ON c.instrument_id=i.instrument_id"
                record = "to_jsonb(c)"
            query = text(
                """
                SELECT r.run_id::text,r.schema_id,r.schema_version,r.status,r.success_records,r.total_records,
                    r.raw_payload_id::text,(SELECT a.request_sha256 FROM ingestion_attempt a WHERE a.run_id=r.run_id AND a.http_status=202 AND a.status IN ('accepted','duplicate') ORDER BY a.created_at DESC LIMIT 1) AS source_request_sha256,p.work_item_id,m.member_key,r.snapshot_id,r.daily_update_id,r.delivery_part_id,
                    raw.idempotency_key,raw.payload_sha256,
                    raw.payload->'batch'->>'source_raw_ref' AS source_raw_ref,
                    raw.payload->'batch'->>'source_raw_sha256' AS source_raw_sha256,
                    j.job_id::text,j.delivery_id::text,j.status AS job_status,
                    canonical_run.delivery_part_id AS sample_delivery_part_id,
                    canonical_run.snapshot_id AS sample_snapshot_id,
                    canonical_run.daily_update_id AS sample_daily_update_id,
                    c.instrument_id::text AS sample_instrument_id,c.trade_date::text AS sample_trade_date,
                    c.run_id::text AS sample_run_id,"""
                + record
                + """ AS sample_record
                FROM daily_delivery_part p
                JOIN daily_delivery_member m ON m.part_id=p.part_id
                JOIN ingestion_run r ON r.delivery_part_id=p.part_id AND r.source=:provider AND r.dataset_key=:dataset
                JOIN delivery_member_evidence e ON e.run_id=r.run_id AND e.member_key=m.member_key
                JOIN raw.market_payload raw ON raw.raw_payload_id=r.raw_payload_id AND raw.run_id=r.run_id
                JOIN normalization_job j ON j.run_id=r.run_id
                JOIN instruments i ON i.symbol=m.symbol AND i.market=:market AND i.asset_class=:asset_class
                """
                + canonical_join
                + " JOIN ingestion_run canonical_run ON canonical_run.run_id=c.run_id "
                + """
                WHERE p.plan_id=:plan_id AND r.is_rerun IS false AND r.batch_data_date=:trade_date
                  AND raw.source=:provider AND raw.dataset_key=:dataset
                  AND r.status='completed' AND r.success_records>0 AND r.total_records=r.success_records
                  AND j.status='completed' AND c.trade_date=:trade_date AND c.source=:provider
                  AND NOT EXISTS (SELECT 1 FROM dq_issue q WHERE q.run_id=r.run_id AND q.severity='error')
                  AND EXISTS (SELECT 1 FROM ingestion_attempt a WHERE a.run_id=r.run_id AND a.http_status=202 AND a.status IN ('accepted','duplicate'))
                  AND EXISTS (SELECT 1 FROM normalization_outbox o WHERE o.run_id=r.run_id AND o.job_id=j.job_id AND o.delivery_id=j.delivery_id AND o.status='published')
                ORDER BY m.member_key,r.created_at DESC LIMIT 100
            """
            )
            candidates = (await db.execute(query, params)).mappings().all()
            if schema == "market_minute":
                complete_groups = set()
                for part_id in {
                    row[key]
                    for row in candidates
                    for key in ("delivery_part_id", "sample_delivery_part_id")
                }:
                    groups = await list_sequenced_snapshot_groups(
                        db,
                        dataset_key=dataset,
                        source=provider,
                        schema_id="market_minute",
                        schema_version=1,
                        data_date=trade_date,
                        delivery_part_id=part_id,
                    )
                    complete_groups.update(
                        (part_id, g.snapshot_id, g.daily_update_id) for g in groups if g.complete
                    )
                candidates = [
                    row
                    for row in candidates
                    if (row["delivery_part_id"], row["snapshot_id"], row["daily_update_id"])
                    in complete_groups
                    and (
                        row["sample_delivery_part_id"],
                        row["sample_snapshot_id"],
                        row["sample_daily_update_id"],
                    )
                    in complete_groups
                ]
            sample = candidates[0] if candidates else None
            if sample is None:
                item["reason"] = "nonzero_native_sample_missing"
                continue
            sample = dict(sample)
            if schema == "futures_eod":
                sample["sample_contract_code"] = sample["sample_record"]["contract_code"]
                sample["sample_session"] = sample["sample_record"]["session"]
            sample["serve_probe"] = await asyncio.to_thread(_serve_probe, schema, sample)
            # Only proof metadata and the public projection leave this host.
            sample.pop("sample_record")
            item["sample"] = sample
            summary = details["summary"]
            item["passed"] = bool(
                summary["data"] > 0
                and summary["missing"] == 0
                and summary["blocked"] == 0
                and sample["source_raw_ref"]
                and sample["source_raw_sha256"]
                and sample["serve_probe"]["success"]
            )
            item["reason"] = None if item["passed"] else "native_plan_incomplete"
        await db.rollback()
    return {
        "environment": environment,
        "execution_profile": profile,
        "trade_date": trade_date.isoformat(),
        "feeds": feeds,
        "passed": len(feeds) == 7 and all(f["passed"] for f in feeds),
    }


async def main():
    try:
        result = await export(json.loads(os.environ["FINDB_NATIVE_EVIDENCE_SPEC"]))
        print(json.dumps(result, default=str, sort_keys=True, separators=(",", ":")))
    except Exception:
        raise SystemExit("native backend evidence export failed") from None
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
