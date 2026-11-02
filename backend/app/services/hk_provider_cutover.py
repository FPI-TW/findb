"""Explicit HK acquisition cutover; retained delivery acceptance stays independent."""

from copy import deepcopy
from datetime import date
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canonical import Instrument, MarketDataEOD
from app.models.registry import AdminAuditEvent, DatasetRegistry, SchedulerControl, UniverseRelease
from app.services.provider_work import lock_provider
from app.utils import utc_now


def acquisition_allowed(
    dataset: DatasetRegistry,
    provider: str,
    *,
    trade_date: date | None = None,
    maintenance: bool = False,
) -> bool:
    if dataset.dataset_key != "hk_equity_eod":
        return True
    # Twelve Data's plan has no HK entitlement; this retirement is permanent.
    if provider == "twelve_data":
        return False
    # The cutover decision governs FactSet only, not any future HK source.
    if provider != "factset":
        return True
    # FactSet catalogue maintenance must precede baseline publication/cutover.
    if maintenance:
        return True
    cutover = (dataset.config or {}).get("hk_provider_cutover")
    if cutover is None:
        return False
    today = utc_now().astimezone(ZoneInfo("Asia/Hong_Kong")).date()
    try:
        boundary = date.fromisoformat(cutover["effective_date"])
        if cutover["provider"] != "factset" or cutover["previous_provider"] != "twelve_data":
            return False
    except (ValueError, KeyError, TypeError):
        return False
    if today < boundary:
        return False
    return trade_date is None or trade_date >= boundary


async def apply_cutover(db: AsyncSession, *, effective_date: date, actor: str) -> dict:
    """One operator transaction, after all acquisition owners and controls stop."""
    # Same FIFO -> environment -> dataset -> controls order as Owner start.
    owners = [await lock_provider(db, provider) for provider in ("factset", "twelve_data")]
    if any(owner.attempt_id or not owner.host_lock_identity for owner in owners):
        raise ValueError("HK providers must be attested and have no active owners")
    from app.services.full_market_admission import lock_environment

    await lock_environment(db)
    dataset = await db.get(
        DatasetRegistry, "hk_equity_eod", with_for_update=True, populate_existing=True
    )
    if dataset is None:
        raise ValueError("HK dataset is unavailable")
    config = deepcopy(dataset.config or {})
    decision = {
        "provider": "factset",
        "previous_provider": "twelve_data",
        "effective_date": effective_date.isoformat(),
    }
    if config.get("hk_provider_cutover") is not None:
        if config["hk_provider_cutover"] != decision:
            raise ValueError("HK provider cutover is immutable")
        return decision
    today = utc_now().astimezone(ZoneInfo("Asia/Hong_Kong")).date()
    if effective_date != today:
        raise ValueError("HK cutover requires the explicit current Hong Kong date")
    controls = list(
        await db.scalars(
            select(SchedulerControl)
            .where(SchedulerControl.provider.in_(("factset", "twelve_data")))
            .order_by(SchedulerControl.scheduler_key)
            .with_for_update()
        )
    )
    if len([c for c in controls if c.provider == "factset"]) != 2 or any(
        control.desired_state != "stopped" for control in controls
    ):
        raise ValueError("HK provider Pilot and Full controls must be installed and stopped")
    baseline = await db.scalar(
        select(UniverseRelease.release_id)
        .where(
            UniverseRelease.provider == "factset",
            UniverseRelease.dataset_key == "hk_equity_eod",
            UniverseRelease.status == "published",
            UniverseRelease.effective_date <= today,
        )
        .limit(1)
    )
    if baseline is None:
        raise ValueError("HK cutover requires a published current HKEX baseline")
    if config.get("schema_id") != "market_eod" or not {1, 2} <= set(
        config.get("accepted_schema_versions", [])
    ):
        raise ValueError("HK cutover requires retained v1 and EOD v2 acceptance")
    config["hk_provider_cutover"] = decision
    # Keep the established Twelve Data priority (zero); FactSet wins explicitly.
    # Normalize only priority metadata, preserving prices and historical lineage.
    config["source_precedence"] = {"factset": -1, "twelve_data": 0}
    await db.execute(
        update(MarketDataEOD)
        .where(
            MarketDataEOD.instrument_id.in_(
                select(Instrument.instrument_id).where(
                    Instrument.market == "HK", Instrument.asset_class == "equity"
                )
            ),
            MarketDataEOD.source == "twelve_data",
            MarketDataEOD.source_priority != 0,
        )
        .values(source_priority=0)
    )
    dataset.config = config
    db.add(
        AdminAuditEvent(
            actor_type="deployment",
            actor_id=actor,
            actor_display_name=actor,
            action="cutover",
            resource_type="dataset_registry",
            resource_id="hk_equity_eod",
            details={**decision, "baseline_release_id": str(baseline)},
        )
    )
    await db.flush()
    return decision
