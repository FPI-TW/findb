"""An exchange's own halt records, cited for a provider feed of the same market (#593).

FactSet writes no row for a suspended Hong Kong security, so its member is
missing. HKEX's own daily feed reads the exchange's daily quotation report, which
prints such a security as ``TRADING SUSPENDED`` or ``TRADING HALTED``, and keeps
that line with the report's raw digest as its member's no-data evidence. A
provider member with no row of its own counts as halted no-data when the
exchange feed recorded it so for the same date; its own outcome stays as it was.
"""

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.registry import (
    DailyDeliveryMember,
    DailyDeliveryPlan,
    HistoricalBackfillItem,
    HistoricalBackfillRequest,
)

# A provider feed and the exchange feed of its market whose halt records it may cite.
OFFICIAL_HALT_FEEDS: dict[tuple[str, str], tuple[str, str]] = {
    ("factset", "hk_equity_eod"): ("hkex", "hk_equity_official_eod"),
}


def citing_feeds(provider: str, dataset_key: str) -> list[tuple[str, str]]:
    """Provider feeds that cite this exchange feed's halt records."""
    return [
        cited for cited, source in OFFICIAL_HALT_FEEDS.items() if source == (provider, dataset_key)
    ]


async def official_halts(
    db: AsyncSession, *, provider: str, dataset_key: str, trade_date: date, keys: set[str]
) -> dict[str, dict]:
    """The given members the exchange feed recorded halted on the date, with its evidence.

    The exchange's daily plan records a halt as a ``no_data`` outcome with reason
    ``halted`` and the report line. A historical date of the exchange feed proves it
    by a ``no_data`` member citing the report kept in that feed's raw scope; the
    exchange's report lists a security that did not trade only as halted.
    """
    source = OFFICIAL_HALT_FEEDS.get((provider, dataset_key))
    if source is None or not keys:
        return {}
    official_provider, official_dataset = source
    rows = await db.execute(
        select(DailyDeliveryMember.member_key, DailyDeliveryMember.outcome_evidence)
        .join(DailyDeliveryPlan, DailyDeliveryPlan.plan_id == DailyDeliveryMember.plan_id)
        .where(
            DailyDeliveryPlan.provider == official_provider,
            DailyDeliveryPlan.dataset_key == official_dataset,
            DailyDeliveryPlan.trade_date == trade_date,
            DailyDeliveryMember.member_key.in_(sorted(keys)),
            DailyDeliveryMember.outcome == "no_data",
            DailyDeliveryMember.outcome_reason == "halted",
            DailyDeliveryMember.outcome_evidence.is_not(None),
        )
    )
    found: dict[str, dict] = {
        key: {"dataset_key": official_dataset, **evidence}
        for key, evidence in rows
        if evidence.get("source_symbol") == key
        and evidence.get("trade_date") == trade_date.isoformat()
    }
    remaining = keys - set(found)
    if not remaining:
        return found
    from app.schemas.eod_completion import EODCompletionEvidence

    scope = f"/{official_provider}/{official_dataset}/"
    items = await db.scalars(
        select(HistoricalBackfillItem.result_evidence)
        .join(HistoricalBackfillRequest)
        .where(
            HistoricalBackfillRequest.provider == official_provider,
            HistoricalBackfillRequest.dataset_key == official_dataset,
            HistoricalBackfillItem.trade_date == trade_date,
            HistoricalBackfillItem.status.in_(("completed", "partial")),
            HistoricalBackfillItem.result_evidence.is_not(None),
        )
    )
    for raw_evidence in items:
        try:
            evidence = EODCompletionEvidence.model_validate(raw_evidence)
        except ValueError:
            continue
        if evidence.trade_date != trade_date:
            continue
        for row in evidence.members:
            if (
                row.member_key in remaining
                and row.outcome == "no_data"
                and row.raw is not None
                and scope in row.raw.ref
            ):
                found[row.member_key] = {
                    "dataset_key": official_dataset,
                    "trade_date": trade_date.isoformat(),
                    "source_status": "halted",
                    **row.raw.model_dump(mode="json"),
                }
        remaining = keys - set(found)
        if not remaining:
            break
    return found
