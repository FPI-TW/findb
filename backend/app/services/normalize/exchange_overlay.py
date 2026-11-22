"""Correct ``market_data_eod`` rows field by field from the exchange's own quote.

A provider row keeps its source, priority and run, so its delivery evidence is
unchanged; only the quoted fields are replaced.  Either write may come first:
the exchange quote corrects an existing row, and a provider rewrite is corrected
again right after it.  Both writers hold a per-date lock until commit, so the
second always reads what the first committed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canonical import (
    MANUAL_CORRECTION_PRIORITY,
    EODFieldCorrection,
    ExchangeDailyQuote,
    MarketDataEOD,
)
from app.services.dq.validators import DQIssueRecord
from app.utils import utc_now, uuid7

# Markets whose provider EOD rows an exchange quote corrects.
EXCHANGE_QUOTE_MARKETS = frozenset({"HK"})
# The exchange publishes no open price; FactSet's stays.
QUOTED_FIELDS = ("close", "high", "low", "volume", "turnover")
# A volume difference above this share of the exchange's volume is reported.
VOLUME_DIFFERENCE_RATIO = Decimal("0.01")


async def lock_quote_date(
    db: AsyncSession, market: str, trade_date: date, *, exclusive: bool
) -> None:
    """Order quote writes against provider writes of one trading date.

    Provider runs share the lock with each other; a quote write excludes them.
    Each transaction takes it before writing its own table, then reads the
    other table, so whichever commits second sees the first.
    """
    function = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    await db.execute(
        text(f"SELECT {function}(hashtextextended(:scope, 0))"),
        {"scope": f"exchange_quote_overlay:{market}:{trade_date.isoformat()}"},
    )


def _text(value: Any) -> str | None:
    """Decimal text without trailing zeros, as Admin corrections record values."""
    return None if value is None else format(Decimal(value).normalize(), "f")


def _differs(current: Any, quoted: Any) -> bool:
    return current is None or Decimal(current) != Decimal(quoted)


def _volume_differs(current: int | None, quoted: int) -> bool:
    if current is None:
        return True
    return Decimal(abs(current - quoted)) > VOLUME_DIFFERENCE_RATIO * Decimal(quoted)


@dataclass(frozen=True)
class OverlayResult:
    issues: list[DQIssueRecord]
    close: Decimal | None


async def apply_exchange_quote(
    db: AsyncSession, *, instrument_id: UUID, trade_date: date, run_id: UUID
) -> OverlayResult | None:
    """Put the stored quote's values on the provider row; None when nothing changed.

    A field the quote leaves empty (no high/low on a day without a board-lot
    trade) keeps the provider's value.  A manually corrected row is never
    touched.  Every replaced value is recorded; a different close, or a volume
    more than 1% off, is also a DQ warning on the applying run.
    """
    quote = await db.get(ExchangeDailyQuote, (instrument_id, trade_date), populate_existing=True)
    if quote is None:
        return None
    row = await db.scalar(
        select(MarketDataEOD)
        .where(
            MarketDataEOD.instrument_id == instrument_id,
            MarketDataEOD.trade_date == trade_date,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or row.source_priority == MANUAL_CORRECTION_PRIORITY:
        return None
    quoted = {field: getattr(quote, field) for field in QUOTED_FIELDS}
    quoted = {field: value for field, value in quoted.items() if value is not None}
    changed = {
        field: value for field, value in quoted.items() if _differs(getattr(row, field), value)
    }
    sources = {**(row.field_sources or {}), **dict.fromkeys(quoted, quote.source)}
    if not changed and sources == (row.field_sources or {}):
        return None
    before = {field: _text(getattr(row, field)) for field in changed}
    after = {field: _text(value) for field, value in changed.items()}
    reported = "close" in changed or (
        "volume" in changed and _volume_differs(row.volume, quote.volume)
    )
    replaced_source = row.source
    for field, value in changed.items():
        setattr(row, field, value)
    row.field_sources = sources
    row.updated_at = utc_now()
    issues: list[DQIssueRecord] = []
    if changed:
        db.add(
            EODFieldCorrection(
                correction_id=uuid7(),
                instrument_id=instrument_id,
                trade_date=trade_date,
                source=quote.source,
                replaced_source=replaced_source,
                before=before,
                after=after,
                archive_id=quote.archive_id,
                run_id=run_id,
                created_at=utc_now(),
            )
        )
        if reported:
            issues.append(
                DQIssueRecord(
                    issue_type="EOD_EXCHANGE_QUOTE_DIFFERENCE",
                    severity="warning",
                    description=(
                        f"{replaced_source} differs from the {quote.source} quote: "
                        + ", ".join(sorted(changed))
                    ),
                    trade_date=datetime.combine(trade_date, time.min, timezone.utc),
                    raw_data={
                        "source": quote.source,
                        "replaced_source": replaced_source,
                        "before": before,
                        "after": after,
                    },
                )
            )
    await db.flush()
    return OverlayResult(issues=issues, close=changed.get("close"))
