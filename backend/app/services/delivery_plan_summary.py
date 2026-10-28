"""Persist affected-plan summaries on writes, with a bounded legacy backfill."""

from uuid import UUID

from sqlalchemy import func, select
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


async def refresh_plan_summary(db: AsyncSession, plan_id: UUID) -> None:
    """Serialize refreshes for a plan, and publish within the writer's transaction.

    Flush before locking so the current run/evidence/outcome participates. The
    plan lock serializes competing writers; READ COMMITTED queries after acquiring
    it observe the previous holder's committed changes as well as our own.
    """
    from app.services.full_market import reconcile_plan

    await db.flush()
    plan = await db.scalar(
        select(DailyDeliveryPlan).where(DailyDeliveryPlan.plan_id == plan_id).with_for_update()
    )
    if plan is None:
        return
    summary = await reconcile_plan(db, plan, gap_limit=0)
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
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[DailyDeliveryPlanSummary.plan_id],
            set_={key: stmt.excluded[key] for key in values if key != "plan_id"},
        )
    )


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
