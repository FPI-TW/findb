"""Provider catalogue symbols outside the official list (#629).

The official list measures gaps. A member-scoped provider (Twelve Data) can only
serve what it is asked for, so a refresh also submits the catalogue's in-scope
symbols the list lacks. A plan freezes the latest such supplement observed by its
trade date; each symbol is requested, a served row is written as unlisted data,
and a symbol without data is no gap.
"""

import hashlib
import json
from datetime import date

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.registry import DailyDeliveryPlan, DatasetRegistry, UniverseSupplement
from app.schemas.full_market import UniverseSubmitRequest
from app.utils import utc_now, uuid7

SUPPLEMENT_FIELDS = (
    "symbol",
    "provider_symbol",
    "exchange",
    "currency",
    "asset_class",
    "classification",
)


class UniverseSupplementError(ValueError):
    """A submitted supplement that cannot stand beside its list."""


def _rows(body: UniverseSubmitRequest) -> list[dict]:
    return sorted(
        (
            {field: getattr(member, field) for field in SUPPLEMENT_FIELDS}
            for member in body.supplementary
        ),
        key=lambda row: row["symbol"],
    )


async def store_supplement(
    db: AsyncSession, body: UniverseSubmitRequest, dataset: DatasetRegistry
) -> None:
    """Keep one observation per distinct supplement and effective date."""
    if not body.supplementary:
        return
    rows = _rows(body)
    listed = {member.symbol for member in body.members}
    symbols = [row["symbol"] for row in rows]
    if (
        len(set(symbols)) != len(symbols)
        or listed & set(symbols)
        or any(
            member.mapping_status != "mapped"
            or member.asset_class != dataset.asset_class
            or member.contract_code is not None
            for member in body.supplementary
        )
    ):
        raise UniverseSupplementError(
            "supplement symbols must be unique, mapped, of the dataset's class and off the list"
        )
    digest = hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    await db.execute(
        insert(UniverseSupplement)
        .values(
            supplement_id=uuid7(),
            dataset_key=body.dataset_key,
            provider=body.provider,
            effective_date=body.effective_date,
            observed_at=body.observed_at,
            member_sha256=digest,
            member_count=len(rows),
            members=rows,
            created_at=utc_now(),
        )
        .on_conflict_do_nothing(constraint="uq_universe_supplement_observation")
    )


async def latest_supplement(
    db: AsyncSession, *, dataset_key: str, provider: str, day: date
) -> UniverseSupplement | None:
    """The newest supplement observed on or before ``day``."""
    return await db.scalar(
        select(UniverseSupplement)
        .where(
            UniverseSupplement.dataset_key == dataset_key,
            UniverseSupplement.provider == provider,
            UniverseSupplement.effective_date <= day,
        )
        .order_by(UniverseSupplement.effective_date.desc(), UniverseSupplement.created_at.desc())
        .limit(1)
    )


async def plan_supplement(db: AsyncSession, plan: DailyDeliveryPlan) -> dict[str, dict]:
    """The plan's frozen supplement rows by symbol; empty when it froze none."""
    if plan.supplement_id is None:
        return {}
    supplement = await db.get(UniverseSupplement, plan.supplement_id)
    return {row["symbol"]: row for row in supplement.members} if supplement else {}
