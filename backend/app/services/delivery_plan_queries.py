"""Read-only Admin delivery plan discovery and bounded pagination."""

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.execution_profiles import ExecutionProfile
from app.models.registry import DailyDeliveryPlan, DailyExecutionDecision
from app.schemas.admin import DeliveryPlanListResponse, PaginatedDeliveryPlanListResponse
from app.schemas.common import PaginationInfo
from app.services.effective_calendar import execution_policy
from app.services.full_market import _skip_response, get_plan


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


async def list_delivery_plans(
    db: AsyncSession,
    *,
    dataset_key: str | None,
    trade_date: date | None,
    limit: int | None,
    page: int | None,
    page_size: int | None,
    profile: ExecutionProfile = "full_market",
) -> DeliveryPlanListResponse | PaginatedDeliveryPlanListResponse:
    stmt = select(DailyDeliveryPlan).where(DailyDeliveryPlan.execution_profile == profile)
    if dataset_key is not None:
        stmt = stmt.where(DailyDeliveryPlan.dataset_key == dataset_key)
    if trade_date is not None:
        stmt = stmt.where(DailyDeliveryPlan.trade_date == trade_date)
    paginated = page is not None or page_size is not None
    current_page, size = page or 1, page_size or 25
    total = (
        int(await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
        if paginated
        else 0
    )
    stmt = stmt.order_by(
        DailyDeliveryPlan.trade_date.desc(),
        DailyDeliveryPlan.created_at.desc(),
        DailyDeliveryPlan.plan_id.desc(),
    ).limit(size if paginated else limit or 20)
    if paginated:
        stmt = stmt.offset((current_page - 1) * size)
    plans = (await db.scalars(stmt)).all()
    data = [
        await get_plan(
            db, plan.plan_id, provider=plan.provider, allowed_datasets=[plan.dataset_key]
        )
        for plan in plans
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
    policies = {
        provider: execution_policy(provider)
        for provider in ("twelve_data", "finlab", "shioaji", "taifex")
    }
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
