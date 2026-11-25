"""Dataset × trade-date completeness matrix for the Dashboard, from stored counts only.

Every cell comes from what writers already persisted: plan summaries, part
counts, calendar decisions and open alerts. Nothing here classifies a plan's
members, so the statement count is fixed whatever the number of feeds, days or
members; a plan whose stored counts are still catching up is shown with
`counts_synced=False` instead of being recomputed.
"""

from datetime import date, datetime, timedelta

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.execution_profiles import ExecutionProfile, unified_scheduler
from app.models.registry import (
    DailyDeliveryPlan,
    DailyDeliveryPlanSummary,
    DailyExecutionDecision,
    DatasetRegistry,
    FullMarketDatasetState,
    MissingDeliveryAlert,
)
from app.schemas.admin import (
    CompletenessCell,
    CompletenessCounts,
    CompletenessResponse,
    CompletenessRow,
    CompletenessState,
)
from app.services.delivery_plan_queries import (
    part_aggregates,
    stored_counts_synced,
    stored_summary,
)
from app.services.effective_calendar import AcquisitionRequest, acquisition_dates_many
from app.services.feed_schedule import latest_due_date
from app.services.full_market import plan_complete
from app.services.provider_metadata import dataset_calendar_market
from app.services.scheduler_control import list_scheduler_controls, scheduler_dataset_keys
from app.utils import ensure_utc, utc_now

DEFAULT_DAYS = 28
MAX_DAYS = 62


class CompletenessRangeError(ValueError):
    pass


def resolve_range(date_from: date | None, date_to: date | None, today: date) -> tuple[date, date]:
    """Default to the four weeks ending today; at most `MAX_DAYS` days."""
    end = date_to or today
    start = date_from or end - timedelta(days=DEFAULT_DAYS - 1)
    if start > end:
        raise CompletenessRangeError("date_from must not be after date_to")
    if (end - start).days + 1 > MAX_DAYS:
        raise CompletenessRangeError(f"the range may span at most {MAX_DAYS} days")
    return start, end


async def completeness_matrix(
    db: AsyncSession,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    dataset_key: str | None = None,
    profile: ExecutionProfile = "full_market",
    now: datetime | None = None,
) -> CompletenessResponse:
    evaluated_at = ensure_utc(now or utc_now())
    start, end = resolve_range(date_from, date_to, evaluated_at.date())
    feeds = sorted(
        {
            (key, row.provider)
            for row in await list_scheduler_controls(db)
            if unified_scheduler(row.scheduler_key)
            for key in scheduler_dataset_keys(row)
            if dataset_key is None or key == dataset_key
        }
    )
    keys = sorted({key for key, _ in feeds})
    datasets = (
        {
            row.dataset_key: row
            for row in await db.scalars(
                select(DatasetRegistry).where(DatasetRegistry.dataset_key.in_(keys))
            )
        }
        if keys
        else {}
    )
    first_dates = (
        {
            row.dataset_key: row.first_start_date
            for row in await db.scalars(
                select(FullMarketDatasetState).where(
                    FullMarketDatasetState.execution_profile == profile,
                    FullMarketDatasetState.dataset_key.in_(keys),
                )
            )
        }
        if keys
        else {}
    )
    rows: list[tuple[str, str, str, date | None, date]] = []
    for key, provider in feeds:
        dataset = datasets.get(key)
        if dataset is None:
            continue
        try:
            through = latest_due_date(key, evaluated_at)
        except ValueError:
            # A dataset without an acquisition schedule has no expected dates.
            continue
        rows.append(
            (
                key,
                provider,
                dataset_calendar_market(key, dataset.market),
                first_dates.get(key),
                through,
            )
        )
    # Projected through the range end, so a future closed day reads closed, not not_due.
    windows = [
        (index, max(first, start), end)
        for index, (_, _, _, first, through) in enumerate(rows)
        if first is not None
    ]
    projected = await acquisition_dates_many(
        db,
        [
            AcquisitionRequest(
                market=rows[index][2],
                dataset_key=rows[index][0],
                provider=rows[index][1],
                profile=profile,
                start=low,
                end=high,
            )
            for index, low, high in windows
        ],
    )
    expected_dates = {
        index: set(dates) for (index, _, _), dates in zip(windows, projected, strict=True)
    }
    pairs = [(key, provider) for key, provider, *_ in rows]
    plans: dict[tuple[str, str, date], CompletenessCell] = {}
    skipped: set[tuple[str, str, date]] = set()
    alerts: set[tuple[str, str, date]] = set()
    if pairs:
        parts = part_aggregates()
        for plan, stored, mapping_gap, part_unsynced in (
            await db.execute(
                select(
                    DailyDeliveryPlan,
                    DailyDeliveryPlanSummary,
                    parts.c.mapping_gap,
                    parts.c.part_unsynced,
                )
                .outerjoin(
                    DailyDeliveryPlanSummary,
                    DailyDeliveryPlanSummary.plan_id == DailyDeliveryPlan.plan_id,
                )
                .outerjoin(parts, parts.c.plan_id == DailyDeliveryPlan.plan_id)
                .where(
                    DailyDeliveryPlan.execution_profile == profile,
                    tuple_(DailyDeliveryPlan.dataset_key, DailyDeliveryPlan.provider).in_(pairs),
                    DailyDeliveryPlan.trade_date >= start,
                    DailyDeliveryPlan.trade_date <= end,
                )
                .execution_options(populate_existing=True)
            )
        ).all():
            summary = stored_summary(plan, stored, mapping_gap, now=evaluated_at)
            plans[(plan.dataset_key, plan.provider, plan.trade_date)] = CompletenessCell(
                trade_date=plan.trade_date,
                state="complete"
                if plan_complete(summary)
                else "late"
                if summary.is_late
                else "incomplete",
                plan_id=plan.plan_id,
                deadline_at=plan.deadline_at,
                counts=CompletenessCounts(
                    expected=summary.expected,
                    data=summary.data,
                    no_data=summary.no_data,
                    missing=summary.missing,
                    blocked=summary.blocked,
                    unavailable=summary.unavailable,
                    mapping_gap=summary.mapping_gap,
                    unlisted_data=summary.unlisted_data,
                ),
                counts_synced=stored_counts_synced(stored, mapping_gap, part_unsynced),
            )
        skipped = set(
            (
                await db.execute(
                    select(
                        DailyExecutionDecision.dataset_key,
                        DailyExecutionDecision.provider,
                        DailyExecutionDecision.trade_date,
                    ).where(
                        DailyExecutionDecision.execution_profile == profile,
                        DailyExecutionDecision.kind == "skipped_calendar",
                        tuple_(
                            DailyExecutionDecision.dataset_key, DailyExecutionDecision.provider
                        ).in_(pairs),
                        DailyExecutionDecision.trade_date >= start,
                        DailyExecutionDecision.trade_date <= end,
                    )
                )
            ).tuples()
        )
        alerts = set(
            (
                await db.execute(
                    select(
                        MissingDeliveryAlert.dataset_key,
                        MissingDeliveryAlert.source,
                        MissingDeliveryAlert.expected_data_date,
                    ).where(
                        MissingDeliveryAlert.execution_profile == profile,
                        MissingDeliveryAlert.status == "open",
                        tuple_(MissingDeliveryAlert.dataset_key, MissingDeliveryAlert.source).in_(
                            pairs
                        ),
                        MissingDeliveryAlert.expected_data_date >= start,
                        MissingDeliveryAlert.expected_data_date <= end,
                    )
                )
            ).tuples()
        )
    days = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
    result = []
    for index, (key, provider, market, first, through) in enumerate(rows):
        cells = []
        for day in days:
            identity = (key, provider, day)
            cell = plans.get(identity)
            if cell is None:
                cell = CompletenessCell(
                    trade_date=day,
                    state=_unplanned_state(
                        day,
                        first=first,
                        through=through,
                        skipped=identity in skipped,
                        expected=day in expected_dates.get(index, set()),
                    ),
                )
            cells.append(cell.model_copy(update={"open_alert": identity in alerts}))
        result.append(
            CompletenessRow(
                dataset_key=key,
                provider=provider,
                market=market,
                first_start_date=first,
                latest_due_date=through,
                cells=cells,
            )
        )
    return CompletenessResponse(
        execution_profile=profile,
        date_from=start,
        date_to=end,
        evaluated_at=evaluated_at,
        rows=result,
    )


def _unplanned_state(
    day: date, *, first: date | None, through: date, skipped: bool, expected: bool
) -> CompletenessState:
    """A date without a plan: before first start, closed, not yet due, or due but never planned."""
    if first is None or day < first:
        return "not_started"
    if skipped or not expected:
        return "closed"
    return "not_due" if day > through else "not_planned"
