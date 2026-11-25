"""Read-only Admin delivery plan discovery and bounded pagination."""

from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.execution_profiles import ExecutionProfile, execution_profile
from app.models.registry import (
    DailyDeliveryPart,
    DailyDeliveryPlan,
    DailyDeliveryPlanSummary,
    DailyExecutionDecision,
)
from app.schemas.admin import (
    AdminDeliveryPlanResponse,
    DeliveryPlanListResponse,
    PaginatedDeliveryPlanListResponse,
)
from app.schemas.common import PaginationInfo
from app.schemas.full_market import DeliverySummaryResponse
from app.services.effective_calendar import PROVIDER_CALENDAR_SWITCHES, execution_policy
from app.services.full_market import _skip_response, plan_complete, plan_response, summary_response


async def list_plan_datasets(
    db: AsyncSession, profile: ExecutionProfile = "full_market"
) -> list[str]:
    return list(
        (
            await db.scalars(
                select(DailyDeliveryPlan.dataset_key)
                .where(DailyDeliveryPlan.execution_profile == profile)
                .union(
                    select(DailyExecutionDecision.dataset_key).where(
                        DailyExecutionDecision.execution_profile == profile
                    )
                )
                .order_by("dataset_key")
            )
        ).all()
    )


def part_aggregates():
    """Per plan: total mapping gaps and whether any part still lacks stored counts."""
    return (
        select(
            DailyDeliveryPart.plan_id,
            func.sum(DailyDeliveryPart.mapping_gap_count).label("mapping_gap"),
            func.bool_or(DailyDeliveryPart.data_count.is_(None)).label("part_unsynced"),
        )
        .group_by(DailyDeliveryPart.plan_id)
        .subquery("plan_part_counts")
    )


def stored_counts_synced(
    stored: DailyDeliveryPlanSummary | None, mapping_gap: int | None, part_unsynced: bool | None
) -> bool:
    """The same trust rule `stored_summary_response` applies before it would reclassify."""
    return bool(
        stored is not None
        and stored.parts_synced_at == stored.updated_at
        and mapping_gap is not None
        and not part_unsynced
    )


def stored_summary(
    plan: DailyDeliveryPlan,
    stored: DailyDeliveryPlanSummary | None,
    mapping_gap: int | None,
    *,
    now: datetime,
) -> DeliverySummaryResponse:
    """Stored counts as a summary; without a stored row every count is zero."""
    return summary_response(
        plan,
        expected=stored.expected_count if stored else plan.expected_count,
        data=stored.data_count if stored else 0,
        no_data=stored.no_data_count if stored else 0,
        missing=stored.missing_count if stored else 0,
        blocked=stored.blocked_count if stored else 0,
        unavailable=stored.unavailable_count if stored else 0,
        mapping_gap=int(mapping_gap or 0),
        now=now,
        gaps=[],
        unlisted_data=stored.unlisted_data_count if stored else 0,
    )


async def _stored_plan_rows(
    db: AsyncSession, plans_statement: Any, *, now: datetime
) -> list[AdminDeliveryPlanResponse]:
    parts = part_aggregates()
    rows = (
        await db.execute(
            plans_statement.add_columns(
                DailyDeliveryPlanSummary,
                parts.c.mapping_gap,
                parts.c.part_unsynced,
            )
            .outerjoin(
                DailyDeliveryPlanSummary,
                DailyDeliveryPlanSummary.plan_id == DailyDeliveryPlan.plan_id,
            )
            .outerjoin(parts, parts.c.plan_id == DailyDeliveryPlan.plan_id)
            .execution_options(populate_existing=True)
        )
    ).all()
    result = []
    for plan, stored, mapping_gap, part_unsynced in rows:
        summary = stored_summary(plan, stored, mapping_gap, now=now)
        result.append(
            AdminDeliveryPlanResponse(
                plan_id=plan.plan_id,
                execution_profile=execution_profile(plan.execution_profile),
                dataset_key=plan.dataset_key,
                provider=plan.provider,
                trade_date=plan.trade_date,
                release_id=plan.release_id,
                deadline_at=plan.deadline_at,
                status="complete" if plan_complete(summary) else "incomplete",
                # Lists never carry part or member detail; the summary read does.
                parts=[],
                summary=summary,
                counts_synced=stored_counts_synced(stored, mapping_gap, part_unsynced),
            )
        )
    return result


async def list_delivery_plans(
    db: AsyncSession,
    *,
    dataset_key: str | None,
    trade_date: date | None,
    limit: int | None,
    page: int | None,
    page_size: int | None,
    profile: ExecutionProfile = "full_market",
    summary_only: bool = False,
    now: datetime | None = None,
) -> DeliveryPlanListResponse | PaginatedDeliveryPlanListResponse:
    from app.utils import utc_now

    evaluated_at = now or utc_now()
    filters = [DailyDeliveryPlan.execution_profile == profile]
    if dataset_key is not None:
        filters.append(DailyDeliveryPlan.dataset_key == dataset_key)
    if trade_date is not None:
        filters.append(DailyDeliveryPlan.trade_date == trade_date)
    paginated = page is not None or page_size is not None
    current_page, size = page or 1, page_size or 25
    total = (
        int(
            await db.scalar(select(func.count()).select_from(DailyDeliveryPlan).where(*filters))
            or 0
        )
        if paginated
        else 0
    )
    stmt = (
        select(DailyDeliveryPlan)
        .where(*filters)
        .order_by(
            DailyDeliveryPlan.trade_date.desc(),
            DailyDeliveryPlan.created_at.desc(),
            DailyDeliveryPlan.plan_id.desc(),
        )
        .limit(size if paginated else limit or 20)
    )
    if paginated:
        stmt = stmt.offset((current_page - 1) * size)
    data: list[AdminDeliveryPlanResponse]
    if summary_only:
        # One statement for the page: stored counts and part aggregates, no reclassification.
        data = await _stored_plan_rows(db, stmt, now=evaluated_at)
    else:
        # Admin reads history; Source provider scope must not hide retired providers.
        data = [
            AdminDeliveryPlanResponse(**(await plan_response(db, plan)).model_dump())
            for plan in (await db.scalars(stmt)).all()
        ]
    skips = select(DailyExecutionDecision).where(
        DailyExecutionDecision.execution_profile == profile,
        DailyExecutionDecision.kind == "skipped_calendar",
    )
    if dataset_key is not None:
        skips = skips.where(DailyExecutionDecision.dataset_key == dataset_key)
    if trade_date is not None:
        skips = skips.where(DailyExecutionDecision.trade_date == trade_date)
    skipped_total = int(await db.scalar(select(func.count()).select_from(skips.subquery())) or 0)
    skipped = [
        _skip_response(row)
        for row in (
            await db.scalars(
                skips.order_by(
                    DailyExecutionDecision.trade_date.desc(),
                    DailyExecutionDecision.dataset_key,
                    DailyExecutionDecision.provider,
                ).limit(100)
            )
        ).all()
    ]
    # Every catalog provider carries a calendar switch; retired ones are not reported.
    policies = {provider: execution_policy(provider) for provider in PROVIDER_CALENDAR_SWITCHES}
    if not paginated:
        return DeliveryPlanListResponse(
            data=data, skipped=skipped, skipped_total=skipped_total, execution_policies=policies
        )
    return PaginatedDeliveryPlanListResponse(
        data=data,
        skipped=skipped,
        skipped_total=skipped_total,
        execution_policies=policies,
        pagination=PaginationInfo(
            page=current_page,
            page_size=size,
            total_records=total,
            total_pages=(total + size - 1) // size,
        ),
    )
