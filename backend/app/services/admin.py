"""
Admin service for canonical data corrections.
"""

import hashlib
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canonical import InstrumentStats, MarketDataEOD
from app.models.correction import CanonicalCorrection
from app.models.registry import DQIssue
from app.schemas.admin import PatchEODRequest, ResolveDQIssueRequest
from app.utils import utc_now, uuid7


class RecordNotFoundError(ValueError):
    """Raised when the target canonical record does not exist."""


class NoChangesError(ValueError):
    """Raised when a PATCH request would produce no actual changes."""


class AlreadyResolvedError(ValueError):
    """Raised when attempting to resolve an already-resolved DQ issue."""


class InvalidCorrectionError(ValueError):
    """Raised when a correction would violate blocking data-quality rules."""


_CORRECTABLE_EOD_FIELDS = {
    "open",
    "high",
    "low",
    "close",
    "volume",
    "total_ticks",
    "turnover",
}


def _mask_key(key: str) -> str:
    """Return first 4 chars + **** for display."""
    if len(key) <= 4:
        return "****"
    return key[:4] + "****"


def build_correction_actor(api_key: str) -> str:
    """Persist a non-reversible identifier instead of the raw admin API key."""
    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]
    return f"key_fp:{digest}"


def present_correction_actor(value: str) -> str:
    """Show fingerprints as-is while masking legacy raw API keys."""
    if value.startswith("key_fp:"):
        return value
    return _mask_key(value)


def _normalize_decimal(value: Decimal) -> str:
    """Serialize Decimal to a consistent string, removing unnecessary trailing zeros."""
    normalized = value.normalize()
    # avoid scientific notation (e.g. 1E+2)
    return format(normalized, "f")


def _snapshot(obj: Any, fields: set[str]) -> dict[str, Any]:
    """Capture specified attributes of an ORM object as a serialisable dict."""
    result: dict[str, Any] = {}
    for field in sorted(fields):
        value = getattr(obj, field, None)
        if isinstance(value, Decimal):
            result[field] = _normalize_decimal(value)
        elif isinstance(value, (datetime, date)):
            result[field] = value.isoformat()
        elif isinstance(value, UUID):
            result[field] = str(value)
        else:
            result[field] = value
    return result


def eod_record_id(instrument_id: UUID, trade_date: date) -> UUID:
    digest = hashlib.md5(
        f"market_data_eod:{instrument_id}:{trade_date.isoformat()}".encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()
    return UUID(digest)


def _values_equal(a: Any, b: Any) -> bool:
    """Compare two values with numeric equality for Decimal types."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, Decimal) or isinstance(b, Decimal):
        try:
            return Decimal(str(a)) == Decimal(str(b))
        except Exception:
            pass
    return a == b


def _validate_blocking_dq_rules(eod: MarketDataEOD) -> None:
    """Apply blocking EOD DQ rules after patch values are merged."""
    blocking_messages: list[str] = []
    if eod.high is not None and eod.open is not None and eod.close is not None:
        max_oc = max(eod.open, eod.close)
        if eod.high < max_oc:
            blocking_messages.append(f"High ({eod.high}) is less than max(open, close) ({max_oc})")

    if eod.low is not None and eod.open is not None and eod.close is not None:
        min_oc = min(eod.open, eod.close)
        if eod.low > min_oc:
            blocking_messages.append(f"Low ({eod.low}) is greater than min(open, close) ({min_oc})")

    if eod.volume is not None and eod.volume < 0:
        blocking_messages.append(f"Volume ({eod.volume}) is negative")

    if not blocking_messages:
        return

    raise InvalidCorrectionError("; ".join(blocking_messages))


async def patch_eod_record(
    db: AsyncSession,
    instrument_id: UUID,
    trade_date: date,
    patch: PatchEODRequest,
    api_key: str,
) -> tuple[CanonicalCorrection, MarketDataEOD]:
    """
    Apply a partial update to a MarketDataEOD record and write an audit row.

    Returns:
        (correction, eod) tuple on success.
    Raises:
        RecordNotFoundError: if no EOD record matches instrument_id + trade_date.
        NoChangesError: if the patch contains no OHLCV fields or values are identical.
    """
    stmt = select(MarketDataEOD).where(
        MarketDataEOD.instrument_id == instrument_id,
        MarketDataEOD.trade_date == trade_date,
    )
    result = await db.execute(stmt)
    eod = result.scalar_one_or_none()
    if eod is None:
        raise RecordNotFoundError(
            f"EOD record not found for instrument {instrument_id} on {trade_date}"
        )

    changed_fields = patch.model_fields_set & _CORRECTABLE_EOD_FIELDS
    if not changed_fields:
        raise NoChangesError("No OHLCV fields provided in the patch request")

    original_values = {f: getattr(eod, f) for f in changed_fields}
    before = _snapshot(eod, changed_fields)

    for field in changed_fields:
        setattr(eod, field, getattr(patch, field))

    if all(_values_equal(original_values[f], getattr(eod, f)) for f in changed_fields):
        raise NoChangesError(
            "No changes detected — submitted values are identical to current values"
        )

    _validate_blocking_dq_rules(eod)

    after = _snapshot(eod, changed_fields)

    correction = CanonicalCorrection(
        id=uuid7(),
        table_name="market_data_eod",
        record_id=eod_record_id(instrument_id, trade_date),
        instrument_id=instrument_id,
        trade_date=trade_date,
        corrected_by=build_correction_actor(api_key),
        correction_reason=patch.correction_reason,
        before_snapshot=before,
        after_snapshot=after,
        created_at=utc_now(),
    )
    db.add(correction)
    await _update_stats_after_eod_patch(db, eod)
    await db.commit()

    return correction, eod


async def _update_stats_after_eod_patch(db: AsyncSession, eod: MarketDataEOD) -> None:
    stats = InstrumentStats.__table__
    now = utc_now()
    stmt = insert(InstrumentStats).values(
        instrument_id=eod.instrument_id,
        first_trade_date=eod.trade_date,
        latest_trade_date=eod.trade_date,
        latest_price=eod.close,
        updated_at=now,
    )
    excluded = stmt.excluded
    stmt = stmt.on_conflict_do_update(
        index_elements=["instrument_id"],
        set_={
            "first_trade_date": func.least(stats.c.first_trade_date, excluded.first_trade_date),
            "latest_trade_date": case(
                (
                    stats.c.latest_trade_date.is_(None)
                    | (excluded.latest_trade_date >= stats.c.latest_trade_date),
                    excluded.latest_trade_date,
                ),
                else_=stats.c.latest_trade_date,
            ),
            "latest_price": case(
                (
                    stats.c.latest_trade_date.is_(None)
                    | (excluded.latest_trade_date >= stats.c.latest_trade_date),
                    excluded.latest_price,
                ),
                else_=stats.c.latest_price,
            ),
            "updated_at": now,
        },
    )
    await db.execute(stmt)


async def resolve_dq_issue(
    db: AsyncSession,
    issue_id: UUID,
    request: ResolveDQIssueRequest,
    api_key: str,
) -> tuple[CanonicalCorrection, DQIssue]:
    """
    Mark a DQ issue as resolved and write an audit row.

    Returns:
        (correction, issue) tuple on success.
    Raises:
        RecordNotFoundError: if the DQ issue does not exist.
        AlreadyResolvedError: if the issue is already resolved.
    """
    issue = await db.get(DQIssue, issue_id)
    if issue is None:
        raise RecordNotFoundError(f"DQ issue {issue_id} not found")

    if issue.resolved:
        raise AlreadyResolvedError(f"DQ issue {issue_id} is already resolved")

    before: dict[str, Any] = {"resolved": False, "resolved_at": None}

    issue.resolved = True
    issue.resolved_at = utc_now()

    after: dict[str, Any] = {
        "resolved": True,
        "resolved_at": issue.resolved_at.isoformat(),
    }

    correction = CanonicalCorrection(
        id=uuid7(),
        table_name="dq_issue",
        record_id=issue_id,
        instrument_id=issue.instrument_id,
        trade_date=None,
        corrected_by=build_correction_actor(api_key),
        correction_reason=request.correction_reason,
        before_snapshot=before,
        after_snapshot=after,
        created_at=utc_now(),
    )
    db.add(correction)
    await db.commit()

    return correction, issue


async def list_dq_issues(
    db: AsyncSession,
    resolved: Optional[bool],
    instrument_id: Optional[UUID],
    severity: Optional[str],
    page: int,
    page_size: int,
) -> tuple[list[DQIssue], int]:
    """Return a paginated, newest-first list of DQ issues."""
    filters = []
    if resolved is not None:
        filters.append(DQIssue.resolved == resolved)
    if instrument_id is not None:
        filters.append(DQIssue.instrument_id == instrument_id)
    if severity is not None:
        filters.append(DQIssue.severity == severity)

    count_stmt = select(func.count(DQIssue.id)).where(*filters)
    total = (await db.execute(count_stmt)).scalar_one()

    offset = (page - 1) * page_size
    rows_stmt = (
        select(DQIssue)
        .where(*filters)
        .order_by(DQIssue.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    rows = list((await db.execute(rows_stmt)).scalars().all())
    return rows, total


async def list_corrections(
    db: AsyncSession,
    table_name: Optional[str],
    instrument_id: Optional[UUID],
    page: int,
    page_size: int,
) -> tuple[list[CanonicalCorrection], int]:
    """
    Return a paginated, newest-first list of correction audit records.

    Returns:
        (rows, total_count) tuple.
    """
    base_filter = []
    if table_name is not None:
        base_filter.append(CanonicalCorrection.table_name == table_name)
    if instrument_id is not None:
        base_filter.append(CanonicalCorrection.instrument_id == instrument_id)

    count_stmt = select(func.count(CanonicalCorrection.id)).where(*base_filter)
    count_result = await db.execute(count_stmt)
    total = count_result.scalar_one()

    offset = (page - 1) * page_size
    rows_stmt = (
        select(CanonicalCorrection)
        .where(*base_filter)
        .order_by(CanonicalCorrection.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    rows_result = await db.execute(rows_stmt)
    rows = list(rows_result.scalars().all())

    return rows, total
