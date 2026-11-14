"""Persist affected-plan summaries on writes, with a bounded legacy backfill."""

import logging
import time
from collections import Counter
from datetime import datetime
from uuid import UUID

from sqlalchemy import case, exists, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.raw import RawMarketPayload
from app.models.registry import (
    DailyDeliveryMember,
    DailyDeliveryPart,
    DailyDeliveryPlan,
    DailyDeliveryPlanSummary,
    DeliveryMemberEvidence,
    IngestionRun,
)
from app.schemas.full_market import DeliverySummaryResponse
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
COUNT_FIELDS = ("data_count", "no_data_count", "missing_count", "blocked_count")
PART_COUNT_FIELDS = (*COUNT_FIELDS, "mapping_gap_count")


def _part_counts(states) -> dict[UUID, dict[str, int]]:
    """Group classified members into each part's share of the plan summary."""
    tallies: dict[UUID, Counter] = {}
    for item in states:
        tally = tallies.setdefault(item.member.part_id, Counter())
        tally[f"{item.state}_count"] += 1
        if item.state == "blocked" and item.reason == "mapping_gap":
            tally["mapping_gap_count"] += 1
    return {
        part_id: {key: tally[key] for key in PART_COUNT_FIELDS}
        for part_id, tally in tallies.items()
    }


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
    from app.services.full_market import classify_plan

    await db.flush()
    started = time.perf_counter()
    plan = await db.scalar(
        select(DailyDeliveryPlan).where(DailyDeliveryPlan.plan_id == plan_id).with_for_update()
    )
    if plan is None:
        return
    locked = time.perf_counter()
    states = await classify_plan(db, plan)
    reconciled = time.perf_counter()
    parts = _part_counts(states)
    if parts:
        await db.execute(
            update(DailyDeliveryPart),
            [{"part_id": part_id, **counts} for part_id, counts in sorted(parts.items())],
        )
    totals = {key: sum(counts[key] for counts in parts.values()) for key in COUNT_FIELDS}
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
    now = utc_now()
    values = {
        "plan_id": plan_id,
        "expected_count": len(states),
        **totals,
        "latest_run_id": latest.run_id if latest else None,
        "latest_run_created_at": latest.created_at if latest else None,
        "last_fetched_at": fetched,
        "last_completed_at": completed,
        "last_failure_code": latest.failure_code if latest else None,
        "policy_outcome": latest.policy_outcome if latest else None,
        "updated_at": now,
        "parts_synced_at": now,
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
        "Plan summary refreshed: plan_id=%s scope=plan members=%s lock_wait_ms=%.1f "
        "reconcile_ms=%.1f total_ms=%.1f",
        plan_id,
        len(states),
        (locked - started) * 1000,
        (reconciled - locked) * 1000,
        (finished - started) * 1000,
    )


async def _parts_synced(db: AsyncSession, plan_id: UUID, part_id: UUID):
    """Read the summary and one part together, or None when counts are not trusted."""
    row = (
        await db.execute(
            select(DailyDeliveryPlanSummary, DailyDeliveryPart)
            .join(DailyDeliveryPart, DailyDeliveryPart.plan_id == DailyDeliveryPlanSummary.plan_id)
            .where(
                DailyDeliveryPlanSummary.plan_id == plan_id,
                DailyDeliveryPart.part_id == part_id,
            )
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        return None
    stored, part = row
    if stored.parts_synced_at is None or stored.parts_synced_at != stored.updated_at:
        return None
    if part.data_count is None:
        return None
    return stored, part


async def _classify_part(db: AsyncSession, plan: DailyDeliveryPlan, part_id: UUID):
    from app.services.full_market import classify_plan

    members = list(
        await db.scalars(
            select(DailyDeliveryMember)
            .where(DailyDeliveryMember.part_id == part_id)
            .order_by(DailyDeliveryMember.member_key)
        )
    )
    states = await classify_plan(db, plan, members)
    return _part_counts(states).get(part_id, dict.fromkeys(PART_COUNT_FIELDS, 0)), len(states)


async def refresh_part_summary(
    db: AsyncSession, plan_id: UUID, part_id: UUID, run: IngestionRun | None = None
) -> None:
    """Reclassify one part under the plan lock and apply its difference.

    Falls back to a full refresh whenever the stored part counts cannot be
    trusted: never initialized, written by another release, or sequenced data
    whose group completeness spans runs.
    """
    from app.services.full_market import SEQUENCED_DATASETS

    await db.flush()
    started = time.perf_counter()
    plan = await db.scalar(
        select(DailyDeliveryPlan).where(DailyDeliveryPlan.plan_id == plan_id).with_for_update()
    )
    if plan is None:
        return
    synced = (
        None
        if plan.dataset_key in SEQUENCED_DATASETS
        else await _parts_synced(db, plan_id, part_id)
    )
    if synced is None:
        await refresh_plan_summary(db, plan_id)
        return
    _, part = synced
    locked = time.perf_counter()
    counts, members = await _classify_part(db, plan, part_id)
    reconciled = time.perf_counter()
    delta = {key: counts[key] - getattr(part, key) for key in COUNT_FIELDS}
    await db.execute(
        update(DailyDeliveryPart).where(DailyDeliveryPart.part_id == part_id).values(**counts)
    )
    now = utc_now()
    await db.execute(
        update(DailyDeliveryPlanSummary)
        .where(DailyDeliveryPlanSummary.plan_id == plan_id)
        .values(
            **{
                key: getattr(DailyDeliveryPlanSummary, key) + value
                for key, value in delta.items()
                if value
            },
            updated_at=now,
            parts_synced_at=now,
        )
    )
    if run is not None:
        await _advance_latest_run(db, plan_id, run)
        if (
            run.status in ("completed", "completed_with_errors")
            and run.completed_at is not None
            and await db.scalar(select(exists().where(DeliveryMemberEvidence.run_id == run.run_id)))
        ):
            await db.execute(
                update(DailyDeliveryPlanSummary)
                .where(DailyDeliveryPlanSummary.plan_id == plan_id)
                .values(
                    last_completed_at=func.greatest(
                        DailyDeliveryPlanSummary.last_completed_at, run.completed_at
                    )
                )
            )
    finished = time.perf_counter()
    logger.info(
        "Plan summary refreshed: plan_id=%s scope=part members=%s lock_wait_ms=%.1f "
        "reconcile_ms=%.1f total_ms=%.1f",
        plan_id,
        members,
        (locked - started) * 1000,
        (reconciled - locked) * 1000,
        (finished - started) * 1000,
    )


async def stored_summary_response(
    db: AsyncSession, plan: DailyDeliveryPlan, *, now: datetime | None = None
) -> DeliverySummaryResponse:
    """Shape the stored counts; gaps stay with reads that classify the cohort."""
    from app.services.full_market import reconcile_plan, summary_response

    stored = await db.scalar(
        select(DailyDeliveryPlanSummary)
        .where(DailyDeliveryPlanSummary.plan_id == plan.plan_id)
        .execution_options(populate_existing=True)
    )
    mapping_gap = await db.scalar(
        select(func.sum(DailyDeliveryPart.mapping_gap_count)).where(
            DailyDeliveryPart.plan_id == plan.plan_id
        )
    )
    if (
        stored is None
        or stored.parts_synced_at != stored.updated_at
        or mapping_gap is None
        or await db.scalar(
            select(
                exists().where(
                    DailyDeliveryPart.plan_id == plan.plan_id,
                    DailyDeliveryPart.data_count.is_(None),
                )
            )
        )
    ):
        return await reconcile_plan(db, plan, now=now, gap_limit=0)
    return summary_response(
        plan,
        expected=stored.expected_count,
        data=stored.data_count,
        no_data=stored.no_data_count,
        missing=stored.missing_count,
        blocked=stored.blocked_count,
        mapping_gap=mapping_gap,
        now=now,
        gaps=[],
    )


async def plan_complete_with_part(db: AsyncSession, plan: DailyDeliveryPlan, part_id: UUID) -> bool:
    """Whether the plan is complete once this part's pending writes count.

    Reads the committed summary and part together and reclassifies only the
    part; untrusted counts fall back to classifying the whole cohort.
    """
    from app.services.full_market import SEQUENCED_DATASETS, reconcile_plan

    await db.flush()
    synced = (
        None
        if plan.dataset_key in SEQUENCED_DATASETS
        else await _parts_synced(db, plan.plan_id, part_id)
    )
    if synced is None:
        summary = await reconcile_plan(db, plan, gap_limit=0)
        return summary.data + summary.no_data == summary.expected
    stored, part = synced
    counts, _ = await _classify_part(db, plan, part_id)
    settled = (
        stored.data_count
        + stored.no_data_count
        - part.data_count
        - part.no_data_count
        + counts["data_count"]
        + counts["no_data_count"]
    )
    return settled == stored.expected_count


async def stored_counts(db: AsyncSession, plan_id: UUID) -> tuple | None:
    """The summary counts with their write stamp, for drift checks."""
    summary = DailyDeliveryPlanSummary
    row = (
        await db.execute(
            select(
                summary.updated_at,
                summary.expected_count,
                *(getattr(summary, key) for key in COUNT_FIELDS),
            ).where(summary.plan_id == plan_id)
        )
    ).one_or_none()
    return tuple(row) if row else None


async def correct_summary_drift(
    db: AsyncSession,
    plan_id: UUID,
    before: tuple | None,
    summary: DeliverySummaryResponse,
) -> bool:
    """Compare a periodic full classification with the stored counts.

    ``before`` is read ahead of the classification; a summary written meanwhile
    is not comparable and waits for the next scan. Only true drift refreshes.
    """
    if before is None or await stored_counts(db, plan_id) != before:
        return False
    reconciled = (summary.expected, summary.data, summary.no_data, summary.missing, summary.blocked)
    if before[1:] == reconciled:
        return False
    logger.warning(
        "Plan summary drift corrected: plan_id=%s stored=%s reconciled=%s",
        plan_id,
        before[1:],
        reconciled,
    )
    await refresh_plan_summary(db, plan_id)
    return True


async def _advance_latest_run(db: AsyncSession, plan_id: UUID, run: IngestionRun) -> bool:
    """Monotonically publish a run's metadata; False when no summary row exists."""
    fetched = (
        await db.scalar(
            select(RawMarketPayload.fetched_at).where(
                RawMarketPayload.raw_payload_id == run.raw_payload_id
            )
        )
        if run.raw_payload_id
        else None
    )
    now = utc_now()
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
            # Metadata leaves the part counts as they were, so they stay in sync.
            updated_at=now,
            parts_synced_at=case(
                (
                    DailyDeliveryPlanSummary.parts_synced_at == DailyDeliveryPlanSummary.updated_at,
                    now,
                ),
                else_=DailyDeliveryPlanSummary.parts_synced_at,
            ),
        )
        .returning(DailyDeliveryPlanSummary.plan_id)
    )
    return updated.scalar_one_or_none() is not None or bool(
        await db.scalar(select(exists().where(DailyDeliveryPlanSummary.plan_id == plan_id)))
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
    if not await _advance_latest_run(db, plan_id, run):
        # Counts cannot be invented; an uninitialized plan takes the full path.
        await refresh_plan_summary(db, plan_id)


async def refresh_summaries_for_run(db: AsyncSession, run: IngestionRun) -> None:
    """Update only the parts a run can have changed.

    A daily run changes its bound part; a sequenced retry invalidates a group
    across runs, so its plan refreshes in full. A historical run repairs only the
    members it actually wrote, in that date's plans for its provider.
    """
    if run.delivery_part_id is not None:
        plan_id = await db.scalar(
            select(DailyDeliveryPart.plan_id).where(
                DailyDeliveryPart.part_id == run.delivery_part_id
            )
        )
        if plan_id is None:
            return
        if run.delivery_mode == "sequenced_snapshot":
            await refresh_plan_summary(db, plan_id)
        else:
            await refresh_part_summary(db, plan_id, run.delivery_part_id, run)
        return
    if (
        run.batch_data_date is None
        or run.schema_id != "market_eod"
        or run.status not in ("completed", "completed_with_errors")
    ):
        return
    await db.flush()
    parts = (
        await db.execute(
            select(DailyDeliveryMember.plan_id, DailyDeliveryMember.part_id)
            .join(DailyDeliveryPlan, DailyDeliveryPlan.plan_id == DailyDeliveryMember.plan_id)
            .where(
                DailyDeliveryPlan.provider == run.source,
                DailyDeliveryPlan.dataset_key == run.dataset_key,
                DailyDeliveryPlan.trade_date == run.batch_data_date,
                DailyDeliveryMember.member_key.in_(
                    select(DeliveryMemberEvidence.member_key).where(
                        DeliveryMemberEvidence.run_id == run.run_id
                    )
                ),
            )
            .distinct()
        )
    ).all()
    for plan_id, part_id in sorted(parts):
        await refresh_part_summary(db, plan_id, part_id)


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
