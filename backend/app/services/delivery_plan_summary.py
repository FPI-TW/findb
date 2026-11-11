"""Persist affected-plan summaries on writes, with a bounded legacy backfill."""

import logging
import time
from uuid import UUID

from sqlalchemy import case, exists, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.raw import RawMarketPayload
from app.models.registry import (
    DailyDeliveryPart,
    DailyDeliveryPlan,
    DailyDeliveryPlanSummary,
    DeliveryMemberEvidence,
    IngestionRun,
)
from app.utils import utc_now

logger = logging.getLogger(__name__)

TERMINAL_RUN_STATUSES = frozenset({"completed", "completed_with_errors", "failed"})
# Fields describing the newest run in the plan scope; a writer that observed an
# older run must never overwrite a newer one recorded without the plan lock.
LATEST_RUN_FIELDS = (
    "latest_run_id",
    "latest_run_created_at",
    "last_fetched_at",
    "last_failure_code",
    "policy_outcome",
)


def _not_older(created_at, run_id):
    summary = DailyDeliveryPlanSummary
    return or_(
        summary.latest_run_created_at.is_(None),
        tuple_(created_at, run_id) >= tuple_(summary.latest_run_created_at, summary.latest_run_id),
    )


async def refresh_plan_summary(db: AsyncSession, plan_id: UUID) -> None:
    """Serialize refreshes for a plan, and publish within the writer's transaction.

    Flush before locking so the current run/evidence/outcome participates. The
    plan lock serializes competing writers; READ COMMITTED queries after acquiring
    it observe the previous holder's committed changes as well as our own.
    """
    from app.services.full_market import reconcile_plan

    await db.flush()
    started = time.perf_counter()
    plan = await db.scalar(
        select(DailyDeliveryPlan).where(DailyDeliveryPlan.plan_id == plan_id).with_for_update()
    )
    if plan is None:
        return
    locked = time.perf_counter()
    summary = await reconcile_plan(db, plan, gap_limit=0)
    reconciled = time.perf_counter()
    scope = (
        select(IngestionRun)
        .join(DailyDeliveryPart, DailyDeliveryPart.part_id == IngestionRun.delivery_part_id)
        .where(DailyDeliveryPart.plan_id == plan_id)
    )
    latest = await db.scalar(
        scope.order_by(IngestionRun.created_at.desc(), IngestionRun.run_id.desc()).limit(1)
    )
    fetched = (
        await db.scalar(
            select(RawMarketPayload.fetched_at).where(
                RawMarketPayload.raw_payload_id == latest.raw_payload_id
            )
        )
        if latest and latest.raw_payload_id
        else None
    )
    completed = await db.scalar(
        select(func.max(IngestionRun.completed_at))
        .join(DailyDeliveryPart, DailyDeliveryPart.part_id == IngestionRun.delivery_part_id)
        .where(
            DailyDeliveryPart.plan_id == plan_id,
            IngestionRun.status.in_(("completed", "completed_with_errors")),
            select(DeliveryMemberEvidence.run_id)
            .where(DeliveryMemberEvidence.run_id == IngestionRun.run_id)
            .exists(),
        )
    )
    values = {
        "plan_id": plan_id,
        "expected_count": summary.expected,
        "data_count": summary.data,
        "no_data_count": summary.no_data,
        "missing_count": summary.missing,
        "blocked_count": summary.blocked,
        "latest_run_id": latest.run_id if latest else None,
        "latest_run_created_at": latest.created_at if latest else None,
        "last_fetched_at": fetched,
        "last_completed_at": completed,
        "last_failure_code": latest.failure_code if latest else None,
        "policy_outcome": latest.policy_outcome if latest else None,
        "updated_at": utc_now(),
    }
    stmt = insert(DailyDeliveryPlanSummary).values(**values)
    newer = _not_older(stmt.excluded.latest_run_created_at, stmt.excluded.latest_run_id)
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[DailyDeliveryPlanSummary.plan_id],
            set_={
                key: case((newer, stmt.excluded[key]), else_=getattr(DailyDeliveryPlanSummary, key))
                if key in LATEST_RUN_FIELDS
                else stmt.excluded[key]
                for key in values
                if key != "plan_id"
            },
        )
    )
    finished = time.perf_counter()
    logger.info(
        "Plan summary refreshed: plan_id=%s expected=%s lock_wait_ms=%.1f "
        "reconcile_ms=%.1f total_ms=%.1f",
        plan_id,
        summary.expected,
        (locked - started) * 1000,
        (reconciled - locked) * 1000,
        (finished - started) * 1000,
    )


async def record_run_created(db: AsyncSession, run: IngestionRun) -> None:
    """Publish a new run; only terminal or sequenced runs can move completeness.

    A non-terminal run adds no canonical or member evidence, so the bound plan
    only advances its latest-run metadata, without the plan lock or a reconcile.
    A sequenced retry immediately invalidates its group and still reconciles.
    """
    if run.status in TERMINAL_RUN_STATUSES or run.delivery_mode == "sequenced_snapshot":
        await refresh_summaries_for_run(db, run)
        return
    if run.delivery_part_id is None:
        return
    await db.flush()
    plan_id = await db.scalar(
        select(DailyDeliveryPart.plan_id).where(DailyDeliveryPart.part_id == run.delivery_part_id)
    )
    if plan_id is None:
        return
    fetched = (
        await db.scalar(
            select(RawMarketPayload.fetched_at).where(
                RawMarketPayload.raw_payload_id == run.raw_payload_id
            )
        )
        if run.raw_payload_id
        else None
    )
    updated = await db.execute(
        update(DailyDeliveryPlanSummary)
        .where(
            DailyDeliveryPlanSummary.plan_id == plan_id,
            _not_older(run.created_at, run.run_id),
        )
        .values(
            latest_run_id=run.run_id,
            latest_run_created_at=run.created_at,
            last_fetched_at=fetched,
            last_failure_code=run.failure_code,
            policy_outcome=run.policy_outcome,
            updated_at=utc_now(),
        )
        .returning(DailyDeliveryPlanSummary.plan_id)
    )
    if updated.scalar_one_or_none() is None and not await db.scalar(
        select(exists().where(DailyDeliveryPlanSummary.plan_id == plan_id))
    ):
        # Counts cannot be invented; an uninitialized plan takes the full path.
        await refresh_plan_summary(db, plan_id)


async def refresh_summaries_for_run(db: AsyncSession, run: IngestionRun) -> None:
    """Refresh only the bound plan and any FactSet historical repair scopes."""
    plan_ids: set[UUID] = set()
    if run.delivery_part_id is not None:
        plan_id = await db.scalar(
            select(DailyDeliveryPart.plan_id).where(
                DailyDeliveryPart.part_id == run.delivery_part_id
            )
        )
        if plan_id is not None:
            plan_ids.add(plan_id)
    if run.source == "factset" and run.dataset_key == "hk_equity_eod" and run.batch_data_date:
        # A verified historical receipt can repair both profiles even without fp1.
        plan_ids.update(
            await db.scalars(
                select(DailyDeliveryPlan.plan_id).where(
                    DailyDeliveryPlan.provider == run.source,
                    DailyDeliveryPlan.dataset_key == run.dataset_key,
                    DailyDeliveryPlan.trade_date == run.batch_data_date,
                )
            )
        )
    for plan_id in sorted(plan_ids):
        await refresh_plan_summary(db, plan_id)


async def backfill_missing_summaries(db: AsyncSession, *, limit: int = 5) -> int:
    """Initialize a bounded batch; concurrent writers/backfill replicas skip locks."""
    plans = list(
        await db.scalars(
            select(DailyDeliveryPlan)
            .where(
                ~select(DailyDeliveryPlanSummary.plan_id)
                .where(DailyDeliveryPlanSummary.plan_id == DailyDeliveryPlan.plan_id)
                .exists()
            )
            .order_by(DailyDeliveryPlan.plan_id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    for plan in plans:
        await refresh_plan_summary(db, plan.plan_id)
    await db.commit()
    return len(plans)
