"""Owner-only, stopped-work retirement; no canonical or accounting side effects."""

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.calendar_closure import ProviderWorkCalendarClosure
from app.models.canonical import CalendarMarket
from app.models.provider_work import ProviderWork
from app.models.registry import DatasetRegistry, SchedulerControl
from app.schemas.calendar_closure import (
    CalendarClosureApplyRequest,
    CalendarClosureInventory,
    CalendarClosurePreview,
    CalendarClosureRequest,
    CalendarClosureResult,
)
from app.services import provider_work as queue
from app.services.effective_calendar import execution_snapshot
from app.services.full_market import FullMarketError, _digest
from app.utils import utc_now

# These SELECTs also form the read-only deployed snapshot export. No ORM/new
# schema import is needed by that export; every result contributes to the digest.
SNAPSHOT_QUERIES = {
    "work": "SELECT to_jsonb(t) AS value FROM provider_work t WHERE work_id=:work_id",
    "plan": "SELECT to_jsonb(t) AS value FROM daily_delivery_plan t WHERE plan_id=:plan_id",
    "parts": "SELECT to_jsonb(t) AS value FROM daily_delivery_part t WHERE plan_id=:plan_id ORDER BY part_number",
    "members": "SELECT to_jsonb(t) AS value FROM daily_delivery_member t WHERE plan_id=:plan_id ORDER BY member_key",
    "summary": "SELECT to_jsonb(t) AS value FROM daily_delivery_plan_summary t WHERE plan_id=:plan_id",
    "attempts": "SELECT to_jsonb(t) AS value FROM provider_work_attempt t WHERE work_id=:work_id ORDER BY attempt_number",
    "repairs": "SELECT to_jsonb(t) AS value FROM shioaji_member_repair t WHERE work_id=:work_id ORDER BY repair_id",
    "repair_acks": "SELECT to_jsonb(a) AS value FROM shioaji_repair_ack a JOIN shioaji_member_repair r USING(repair_id) WHERE r.work_id=:work_id ORDER BY a.repair_id",
    "receipts": "SELECT to_jsonb(t) AS value FROM provider_work_receipt t WHERE work_id=:work_id ORDER BY run_id",
    "runs": "SELECT jsonb_build_object('run_id',run_id,'status',status) AS value FROM ingestion_run WHERE delivery_part_id IN (SELECT part_id FROM daily_delivery_part WHERE plan_id=:plan_id) OR (source='shioaji' AND dataset_key=:dataset AND batch_data_date=:day) ORDER BY run_id",
    "decision": "SELECT to_jsonb(t) AS value FROM daily_execution_decision t WHERE provider='shioaji' AND dataset_key=:dataset AND execution_profile=:profile AND trade_date=:day",
    "alerts": "SELECT to_jsonb(t) AS value FROM missing_delivery_alert t WHERE source='shioaji' AND dataset_key=:dataset AND execution_profile=:profile AND expected_data_date=:day ORDER BY alert_id",
    "controls": "SELECT jsonb_build_object('scheduler_key',scheduler_key,'desired_state',desired_state,'observed_state',observed_state,'revision',revision) AS value FROM scheduler_control WHERE provider='shioaji' AND retired_at IS NULL ORDER BY scheduler_key",
    "owner": "SELECT jsonb_build_object('attempt_id',attempt_id,'generation',generation,'host_lock_identity',host_lock_identity,'recovery_reason',recovery_reason,'acquisition_not_before',acquisition_not_before) AS value FROM provider_work_owner WHERE environment=:environment AND provider='shioaji'",
}


def _one(snapshot: dict, name: str) -> dict:
    rows = snapshot.get(name, [])
    return rows[0] if len(rows) == 1 else {}


def _decision(value: dict) -> dict:
    result = dict(value)
    if result.get("created_at"):
        result["created_at"] = (
            datetime.fromisoformat(result["created_at"]).astimezone(timezone.utc).isoformat()
        )
    return result


async def inventory(db: AsyncSession, work_id: UUID) -> CalendarClosureInventory:
    work = await db.get(ProviderWork, work_id, populate_existing=True)
    if (
        work is None
        or work.environment != queue.environment()
        or work.provider != "shioaji"
        or work.kind != "daily"
        or work.execution_profile not in {"pilot", "full_market"}
        or not (work.manifest or {}).get("plan_id")
    ):
        raise FullMarketError("Calendar closure requires a frozen Shioaji daily work", 409)
    try:
        plan_id = UUID((work.manifest or {})["plan_id"])
    except (ValueError, TypeError):
        raise FullMarketError("Calendar closure plan identity is invalid", 409) from None
    params = {
        "work_id": work_id,
        "plan_id": plan_id,
        "dataset": work.dataset_key,
        "profile": work.execution_profile,
        "day": work.trade_date,
        "environment": work.environment,
    }
    snapshot: dict[str, Any] = {}
    for name, sql in SNAPSHOT_QUERIES.items():
        snapshot[name] = list((await db.execute(text(sql), params)).scalars())
    dataset = await db.get(DatasetRegistry, work.dataset_key)
    if dataset is None or dataset.market != "TW" or work.trade_date is None:
        raise FullMarketError("Calendar closure dataset scope differs", 409)
    snapshot["calendar"] = await execution_snapshot(db, "TW", work.trade_date, work.provider)
    decision = _one(snapshot, "decision")
    audit_id = decision.get("snapshot", {}).get("manual_repair", {}).get("audit_event_id")
    snapshot["audit"] = list(
        (
            await db.execute(
                text("SELECT to_jsonb(t) FROM admin_audit_event t WHERE event_id::text=:id"),
                {"id": str(audit_id)},
            )
        ).scalars()
    )
    ready = bool(
        await db.scalar(
            text("SELECT to_regclass('public.provider_work_calendar_closure') IS NOT NULL")
        )
    )
    snapshot["closure"] = (
        list(
            (
                await db.execute(
                    text(
                        "SELECT to_jsonb(t) FROM provider_work_calendar_closure t WHERE work_id=:id"
                    ),
                    {"id": work_id},
                )
            ).scalars()
        )
        if ready
        else []
    )
    return CalendarClosureInventory(
        work_id=work_id,
        environment=work.environment,
        execution_profile=work.execution_profile,
        plan_id=plan_id,
        schema_ready=ready,
        snapshot=snapshot,
        sha256=_digest(snapshot),
    )


def evaluate(
    current: CalendarClosureInventory, body: CalendarClosureRequest, *, now: datetime
) -> CalendarClosurePreview:
    """Identical evaluator for HTTP and exported, read-only deployment previews."""
    s = current.snapshot
    w, plan, decision, calendar = (
        _one(s, "work"),
        _one(s, "plan"),
        _one(s, "decision"),
        s.get("calendar", {}),
    )
    blockers: list[str] = []

    def require(condition, reason):
        if not condition:
            blockers.append(reason)

    require(_digest(s) == current.sha256, "inventory_checksum")
    require(current.schema_ready, "backend_schema_deployment_required")
    require(
        (body.environment, body.execution_profile, body.plan_id)
        == (current.environment, current.execution_profile, current.plan_id),
        "request_scope",
    )
    require(
        w.get("state") in {"queued", "partial", "failed"} and not s.get("closure"),
        "work_not_unresolved",
    )
    require(
        w.get("provider") == "shioaji"
        and w.get("kind") == "daily"
        and w.get("environment") == body.environment
        and w.get("execution_profile") == body.execution_profile,
        "work_scope",
    )
    require(
        plan.get("plan_id") == str(body.plan_id)
        and plan.get("provider") == "shioaji"
        and plan.get("dataset_key") == w.get("dataset_key")
        and plan.get("trade_date") == w.get("trade_date")
        and plan.get("execution_profile") == body.execution_profile,
        "plan_scope",
    )
    require((w.get("manifest") or {}).get("mode") == "canonical", "manifest_not_canonical")
    require(
        calendar.get("calendar_source") == "published"
        and calendar.get("is_open") is False
        and calendar.get("policy", {}).get("skip_non_trading_days") is True,
        "published_calendar_not_closed",
    )
    correction = decision.get("snapshot", {}).get("manual_repair", {})
    require(
        decision.get("kind") == "skipped_calendar"
        and decision.get("plan_id") is None
        and decision.get("dataset_key") == w.get("dataset_key")
        and decision.get("trade_date") == w.get("trade_date")
        and decision.get("provider") == "shioaji"
        and decision.get("execution_profile") == body.execution_profile
        and correction.get("original_plan_id") == str(body.plan_id),
        "corrected_decision_required",
    )
    require(
        decision.get("snapshot", {}).get("calendar_revision_id")
        == calendar.get("calendar_revision_id")
        and decision.get("snapshot", {}).get("is_open") is False,
        "calendar_decision_conflict",
    )
    audit = _one(s, "audit")
    details = audit.get("details") or {}
    require(
        audit.get("action") == "calendar_decision_repair"
        and audit.get("event_id") == correction.get("audit_event_id")
        and _digest(details.get("before")) == details.get("before_sha256")
        and _digest(details.get("after")) == details.get("after_sha256")
        and _decision(details.get("after", {}).get("decision", {})) == _decision(decision)
        and details.get("before", {}).get("decision", {}).get("plan_id") == str(body.plan_id),
        "correction_audit_invalid",
    )
    require(
        bool(s.get("alerts")) and all(a.get("status") == "resolved" for a in s["alerts"]),
        "alerts_unresolved",
    )
    keys = {c.get("scheduler_key") for c in s.get("controls", [])}
    require(
        {"pilot_shioaji_v1", "full_market_shioaji_v1"} <= keys
        and all(
            c.get("desired_state") == c.get("observed_state") == "stopped"
            for c in s.get("controls", [])
        ),
        "controls_not_stopped",
    )
    owner = _one(s, "owner")
    require(
        bool(owner) and owner.get("attempt_id") is None and not owner.get("recovery_reason"),
        "owner_not_idle",
    )
    require(
        not w.get("prepared_deliveries") and not s.get("receipts") and not s.get("runs"),
        "delivery_evidence_present",
    )
    require(
        all(a.get("state") not in {"open", "recovery_blocked"} for a in s.get("attempts", [])),
        "attempt_active",
    )
    cp = body.checkpoint
    facts = cp.facts
    require(
        cp.inventory_sha256 == current.sha256 and _digest(facts) == cp.sha256, "checkpoint_checksum"
    )
    require(
        now - timedelta(minutes=15) <= cp.observed_at <= now + timedelta(seconds=30),
        "checkpoint_expired",
    )
    require(
        not owner.get("host_lock_identity") or owner["host_lock_identity"] == cp.host_lock_identity,
        "host_lock_differs",
    )
    require(cp.host_lock_identity.startswith(body.environment + ":"), "host_lock_environment")
    require(
        facts.get("work_id") == str(current.work_id)
        and facts.get("plan_id") == str(body.plan_id)
        and facts.get("execution_profile") == body.execution_profile
        and facts.get("environment") == body.environment,
        "checkpoint_scope",
    )
    members = {m["member_key"] for m in s.get("members", [])}
    require(
        bool(members)
        and set(facts.get("member_keys", [])) == members
        and len(facts.get("member_keys", [])) == len(members),
        "checkpoint_members",
    )
    require(facts.get("manifest_sha256") == _digest(w.get("manifest")), "journal_manifest_differs")
    for field in [
        "prepared_count",
        "receipt_count",
        "active_leases",
        "journal_deliveries",
        "journal_jobs",
        "unknown_probes",
        "active_intents",
    ]:
        require(type(facts.get(field)) is int and facts[field] == 0, "checkpoint_" + field)
    require(facts.get("checksums_valid") is True, "checkpoint_cache_checksum")
    request = body.model_dump(mode="json")
    digest = _digest({"inventory": current.sha256, "request": request})
    return CalendarClosurePreview(
        inventory=current, request=body, apply_ready=not blockers, blockers=blockers, sha256=digest
    )


async def preview(
    db: AsyncSession, work_id: UUID, body: CalendarClosureRequest
) -> CalendarClosurePreview:
    return evaluate(await inventory(db, work_id), body, now=utc_now())


async def apply(
    db: AsyncSession, work_id: UUID, body: CalendarClosureApplyRequest, *, actor: str
) -> CalendarClosureResult:
    # Same order as scheduler start: provider -> environment -> dataset -> market -> controls.
    await queue.lock_provider(db, "shioaji")
    previous = await db.get(ProviderWorkCalendarClosure, work_id, populate_existing=True)
    request_hash = _digest(body.model_dump(mode="json"))
    if previous:
        if (
            previous.environment != queue.environment()
            or previous.request_id != body.request_id
            or previous.request_sha256 != request_hash
        ):
            raise FullMarketError("Calendar closure request differs from retained approval", 409)
        return CalendarClosureResult(
            work_id=work_id,
            closure_id=previous.closure_id,
            closed_at=previous.created_at,
            replayed=True,
        )
    from app.services.full_market_admission import lock_environment

    await lock_environment(db)
    work = await db.get(ProviderWork, work_id, populate_existing=True)
    if work is None or work.environment != queue.environment():
        raise FullMarketError("Calendar closure work scope differs", 409)
    await db.execute(
        select(DatasetRegistry)
        .where(DatasetRegistry.dataset_key == work.dataset_key)
        .with_for_update()
    )
    await db.execute(select(CalendarMarket).where(CalendarMarket.market == "TW").with_for_update())
    await db.execute(
        select(SchedulerControl)
        .where(SchedulerControl.provider == "shioaji")
        .order_by(SchedulerControl.scheduler_key)
        .with_for_update()
    )
    proposal = CalendarClosureRequest.model_validate(body.model_dump(exclude={"expected_sha256"}))
    report = await preview(db, work_id, proposal)
    if report.sha256 != body.expected_sha256:
        raise FullMarketError("Calendar closure preview is stale", 409)
    if not report.apply_ready:
        raise FullMarketError("Calendar closure blocked: " + ",".join(report.blockers), 409)
    row = ProviderWorkCalendarClosure(
        work_id=work_id,
        plan_id=body.plan_id,
        request_id=body.request_id,
        request_sha256=request_hash,
        environment=body.environment,
        execution_profile=body.execution_profile,
        actor=actor,
        reason=body.reason,
        evidence=json.loads(report.model_dump_json()),
    )
    db.add(row)
    work.state, work.completed_at = "calendar_closed", utc_now()
    await db.flush()
    return CalendarClosureResult(
        work_id=work_id, closure_id=row.closure_id, closed_at=row.created_at, replayed=False
    )
