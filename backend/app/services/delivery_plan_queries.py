"""Read-only Admin delivery plan discovery and bounded pagination."""

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.registry import DailyDeliveryPlan
from app.schemas.admin import DeliveryPlanListResponse, PaginatedDeliveryPlanListResponse
from app.schemas.common import PaginationInfo
from app.services.full_market import get_plan


async def list_plan_datasets(db: AsyncSession) -> list[str]:
    return list(
        (
            await db.scalars(
                select(DailyDeliveryPlan.dataset_key)
                .distinct()
                .order_by(DailyDeliveryPlan.dataset_key)
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
) -> DeliveryPlanListResponse | PaginatedDeliveryPlanListResponse:
    stmt = select(DailyDeliveryPlan)
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
    if not paginated:
        return DeliveryPlanListResponse(data=data)
    return PaginatedDeliveryPlanListResponse(
        data=data,
        pagination=PaginationInfo(
            page=current_page,
            page_size=size,
            total_records=total,
            total_pages=(total + size - 1) // size,
        ),
    )
