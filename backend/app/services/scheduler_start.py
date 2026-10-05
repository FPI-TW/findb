"""Authoritative start eligibility for governed full-market controls."""

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canonical import CalendarYearRevision
from app.models.registry import DatasetRegistry, SchedulerControl, SchedulerDataset, UniverseRelease
from app.services.calendar_management import complete_published_revision_ids
from app.services.full_market_governance import full_market_configuration
from app.services.source_clients import PROVIDER_DATASET_SCOPE


async def scheduler_start_datasets(
    db: AsyncSession, scheduler_key: str, *, lock: bool = False
) -> list[DatasetRegistry]:
    """Refresh governance; acquire dataset locks before any control mutation lock."""
    statement = (
        select(DatasetRegistry)
        .join(SchedulerDataset, SchedulerDataset.dataset_key == DatasetRegistry.dataset_key)
        .where(SchedulerDataset.scheduler_key == scheduler_key)
        .order_by(DatasetRegistry.dataset_key)
        .execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update(of=DatasetRegistry)
    return list((await db.scalars(statement)).all())


async def scheduler_start_blockers(
    db: AsyncSession,
    row: SchedulerControl,
    dataset_keys: list[str],
    datasets: list[DatasetRegistry] | None = None,
) -> list[str]:
    """Return safe reason codes, allowing partial approved acceptance scopes."""
    if not row.scheduler_key.startswith("full_market_"):
        return []
    expected = PROVIDER_DATASET_SCOPE.get(row.provider)
    if (
        expected is None
        or row.scheduler_key != f"full_market_{row.provider}_v1"
        or set(dataset_keys) != set(expected)
    ):
        return ["full_market_scope_invalid"]
    if datasets is None:
        datasets = await scheduler_start_datasets(db, row.scheduler_key)
    if {dataset.dataset_key for dataset in datasets} != set(dataset_keys):
        return ["full_market_scope_invalid"]
    enabled: list[tuple[DatasetRegistry, date]] = []
    for dataset in datasets:
        opted_in, _, errors, _ = full_market_configuration(dataset, row.provider)
        if errors:
            return ["full_market_configuration_invalid"]
        if opted_in:
            governance = (dataset.config or {})["full_market"]
            enabled.append((dataset, date.fromisoformat(governance["activation_date"])))
    if not enabled:
        return ["full_market_no_enabled_datasets"]
    for dataset, activation in enabled:
        baseline = await db.scalar(
            select(UniverseRelease.release_id)
            .where(
                UniverseRelease.dataset_key == dataset.dataset_key,
                UniverseRelease.provider == row.provider,
                UniverseRelease.status == "published",
                UniverseRelease.effective_date <= activation,
            )
            .limit(1)
        )
        if baseline is None:
            return ["full_market_baseline_missing"]
        calendar_market = "TAIFEX" if dataset.dataset_key == "tw_futures_eod" else dataset.market
        calendar = await db.scalar(
            select(CalendarYearRevision.id)
            .where(
                CalendarYearRevision.market == calendar_market,
                CalendarYearRevision.year == activation.year,
                CalendarYearRevision.id.in_(complete_published_revision_ids()),
            )
            .limit(1)
        )
        if calendar is None:
            return ["full_market_calendar_missing"]
    return []
