"""Authoritative start eligibility for governed full-market controls."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.registry import DatasetRegistry, SchedulerControl, SchedulerDataset
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
    from app.services.full_market_admission import eligibility

    return (await eligibility(db, row.provider, datasets))["blockers"]
