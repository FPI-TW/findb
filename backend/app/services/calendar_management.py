"""Managed market/year calendar parsing and transactional lifecycle services."""

import csv
import hashlib
import io
import re
from calendar import isleap
from datetime import date, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canonical import (
    CalendarImportBatch,
    CalendarMarket,
    CalendarRevisionDay,
    CalendarYearRevision,
)
from app.utils import utc_now, uuid7

_TWSE_TITLE = re.compile(r"中華民國\s*(\d{2,3})\s*年有價證券集中交易市場開（休）市日期表")
_TWSE_DATE = re.compile(r"^\s*(\d{1,2})月(\d{1,2})日\s*\(([一二三四五六日])\)\s*$")
_WEEKDAYS = "一二三四五六日"
_BR = re.compile(r"<\s*/?br\s*/?\s*>", re.I)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class CalendarError(ValueError):
    pass


class RevisionConflictError(CalendarError):
    pass


def _text(value: Any, max_length: int) -> str | None:
    if value is None:
        return None
    clean = _CONTROL.sub("", _BR.sub("\n", str(value))).strip()
    return clean[:max_length] or None


def _expected_days(year: int) -> int:
    return 366 if isleap(year) else 365


def _time_or_none(value: Any) -> time | None:
    if value in (None, ""):
        return None
    if isinstance(value, time):
        return value
    try:
        return time.fromisoformat(str(value))
    except ValueError as exc:
        raise CalendarError(f"invalid session time: {value!r}") from exc


def _all_days(year: int, market: CalendarMarket) -> list[dict[str, Any]]:
    first = date(year, 1, 1)
    values: list[dict[str, Any]] = []
    for offset in range(_expected_days(year)):
        day = first + timedelta(days=offset)
        is_open = day.weekday() not in market.weekend_days
        values.append(
            {
                "trade_date": day.isoformat(),
                "status": "open" if is_open else "closed",
                "holiday_name": None,
                "description": None,
                "session_open": market.default_session_open.isoformat()
                if is_open and market.default_session_open
                else None,
                "session_close": market.default_session_close.isoformat()
                if is_open and market.default_session_close
                else None,
            }
        )
    return values


def _apply_rows(
    *, year: int, market: CalendarMarket, rows: list[dict[str, Any]], coverage_mode: str
) -> list[dict[str, Any]]:
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for row in rows:
        raw_day = row.get("trade_date")
        try:
            parsed = date.fromisoformat(str(raw_day))
        except ValueError as exc:
            raise CalendarError(f"invalid trade_date: {raw_day!r}") from exc
        if parsed.year != year:
            raise CalendarError(f"date {parsed.isoformat()} does not belong to {year}")
        key = parsed.isoformat()
        if key in seen:
            raise CalendarError(f"duplicate trade_date: {key}")
        seen.add(key)
        state = str(row.get("status", ""))
        if state not in {"open", "closed", "settlement_only"}:
            raise CalendarError(f"unknown day status for {key}: {state!r}")
        normalized.append(
            {
                "trade_date": key,
                "status": state,
                "holiday_name": _text(row.get("holiday_name"), 100),
                "description": _text(row.get("description"), 2000),
                "session_open": row.get("session_open") if state == "open" else None,
                "session_close": row.get("session_close") if state == "open" else None,
            }
        )
    if coverage_mode == "full_year":
        if len(normalized) != _expected_days(year):
            raise CalendarError("full_year input must contain every calendar date exactly once")
        return sorted(normalized, key=lambda item: item["trade_date"])
    expanded = {row["trade_date"]: row for row in _all_days(year, market)}
    expanded.update({row["trade_date"]: row for row in normalized})
    return [expanded[key] for key in sorted(expanded)]


def parse_twse_csv(
    raw: bytes, *, year: int, market: CalendarMarket
) -> tuple[list[dict[str, Any]], str]:
    if not raw or len(raw) > 1_000_000:
        raise CalendarError("CSV must be between 1 byte and 1 MiB")
    decoded: str | None = None
    encoding = ""
    candidates = (
        ("utf-8-sig", "cp950", "big5")
        if raw.startswith(b"\xef\xbb\xbf")
        else (
            "utf-8",
            "cp950",
            "big5",
        )
    )
    for candidate in candidates:
        try:
            decoded = raw.decode(candidate)
            encoding = candidate
            break
        except UnicodeDecodeError:
            continue
    if decoded is None or "\x00" in decoded:
        raise CalendarError("unsupported CSV encoding")
    parsed_rows = list(csv.reader(io.StringIO(decoded)))
    if len(parsed_rows) > 402:
        raise CalendarError("TWSE CSV exceeds 400 data rows")
    if len(parsed_rows) < 3 or len(parsed_rows[0]) != 1:
        raise CalendarError("unsupported TWSE CSV shape")
    title = _TWSE_TITLE.search(parsed_rows[0][0].strip())
    if title is None:
        raise CalendarError("unsupported TWSE CSV title")
    if int(title.group(1)) + 1911 != year:
        raise CalendarError("TWSE ROC year does not match requested year")
    header = [cell.strip() for cell in parsed_rows[1]]
    if (
        len(header) != 4
        or header[:3] != ["日期", "名稱", "說明"]
        or not header[3].startswith("備註")
    ):
        raise CalendarError("unsupported TWSE CSV header")
    exceptions: list[dict[str, Any]] = []
    for row_number, row in enumerate(parsed_rows[2:], start=3):
        if not any(cell.strip() for cell in row):
            continue
        if len(row) != 4:
            raise CalendarError(f"row {row_number} must contain four columns")
        match = _TWSE_DATE.match(row[0])
        if match is None:
            raise CalendarError(f"row {row_number} has invalid date")
        month, day, weekday = int(match.group(1)), int(match.group(2)), match.group(3)
        try:
            parsed = date(year, month, day)
        except ValueError as exc:
            raise CalendarError(f"row {row_number} has invalid calendar date") from exc
        if _WEEKDAYS[parsed.weekday()] != weekday:
            raise CalendarError(f"row {row_number} weekday does not match date")
        marker = row[3].strip()
        if marker == "o":
            state = "open"
        elif marker == "*":
            state = "settlement_only"
        elif not marker:
            state = "closed"
        else:
            raise CalendarError(f"row {row_number} has unknown marker {marker!r}")
        exceptions.append(
            {
                "trade_date": parsed.isoformat(),
                "status": state,
                "holiday_name": _text(row[1], 100),
                "description": _text(row[2], 2000),
                "session_open": None,
                "session_close": None,
            }
        )
    return _apply_rows(
        year=year, market=market, rows=exceptions, coverage_mode="exceptions"
    ), encoding


def _summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(rows),
        "open": sum(row["status"] == "open" for row in rows),
        "closed": sum(row["status"] == "closed" for row in rows),
        "settlement_only": sum(row["status"] == "settlement_only" for row in rows),
    }


async def latest_revision(db: AsyncSession, market: str, year: int) -> CalendarYearRevision | None:
    stmt = (
        select(CalendarYearRevision)
        .where(CalendarYearRevision.market == market, CalendarYearRevision.year == year)
        .order_by(CalendarYearRevision.revision.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def create_preview(
    db: AsyncSession,
    *,
    market: CalendarMarket,
    year: int,
    input_format: str,
    rows: list[dict[str, Any]],
    source_bytes: bytes | None,
    source_filename: str | None,
    detected_encoding: str | None,
    created_by: str | None,
    commit: bool = True,
) -> CalendarImportBatch:
    current = await latest_revision(db, market.market, year)
    batch = CalendarImportBatch(
        id=uuid7(),
        market=market.market,
        year=year,
        input_format=input_format,
        detected_encoding=detected_encoding,
        source_filename=_text(source_filename, 255),
        source_sha256=hashlib.sha256(source_bytes).hexdigest() if source_bytes else None,
        candidate_rows=rows,
        summary=_summary(rows),
        warnings=[],
        errors=[],
        base_revision=current.revision if current else 0,
        status="previewed",
        created_by=created_by,
        created_at=utc_now(),
        expires_at=utc_now() + timedelta(minutes=30),
    )
    db.add(batch)
    if commit:
        await db.commit()
    else:
        await db.flush()
    await db.refresh(batch)
    return batch


async def apply_preview(
    db: AsyncSession,
    *,
    batch_id: Any,
    expected_revision: int,
    actor: str | None,
    commit: bool = True,
) -> CalendarYearRevision:
    batch = await db.get(CalendarImportBatch, batch_id, with_for_update=True)
    if batch is None:
        raise CalendarError("preview is unavailable or expired")
    if batch.status == "applied":
        if expected_revision != batch.base_revision:
            raise RevisionConflictError("calendar changed since preview")
        existing = (
            await db.execute(
                select(CalendarYearRevision).where(
                    CalendarYearRevision.market == batch.market,
                    CalendarYearRevision.year == batch.year,
                    CalendarYearRevision.revision == batch.base_revision + 1,
                    CalendarYearRevision.source_kind == batch.input_format,
                    CalendarYearRevision.source_sha256 == batch.source_sha256,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            raise CalendarError("applied preview revision is unavailable")
        return existing
    if batch.status != "previewed" or batch.expires_at <= utc_now():
        raise CalendarError("preview is unavailable or expired")
    # Serialise all mutations for a market before checking the optimistic revision.
    # This also covers the first revision, when there is no year row to lock yet.
    await db.execute(
        select(CalendarMarket).where(CalendarMarket.market == batch.market).with_for_update()
    )
    current = await latest_revision(db, batch.market, batch.year)
    current_revision = current.revision if current else 0
    if expected_revision != batch.base_revision or current_revision != expected_revision:
        raise RevisionConflictError("calendar changed since preview")
    _ = actor
    revision = CalendarYearRevision(
        id=uuid7(),
        market=batch.market,
        year=batch.year,
        revision=current_revision + 1,
        status="draft",
        expected_days=_expected_days(batch.year),
        actual_days=len(batch.candidate_rows),
        source_kind=batch.input_format,
        source_sha256=batch.source_sha256,
        source_filename=batch.source_filename,
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    db.add(revision)
    await db.flush()
    db.add_all(
        [
            CalendarRevisionDay(
                id=uuid7(),
                calendar_revision_id=revision.id,
                trade_date=date.fromisoformat(row["trade_date"]),
                day_status=row["status"],
                is_open=row["status"] == "open",
                session_open=_time_or_none(row.get("session_open")),
                session_close=_time_or_none(row.get("session_close")),
                holiday_name=row.get("holiday_name"),
                description=row.get("description"),
                source_kind=batch.input_format,
            )
            for row in batch.candidate_rows
        ]
    )
    batch.status = "applied"
    if commit:
        await db.commit()
    else:
        await db.flush()
    await db.refresh(revision)
    return revision


async def revision_days(db: AsyncSession, revision_id: Any) -> list[CalendarRevisionDay]:
    return list(
        (
            await db.execute(
                select(CalendarRevisionDay)
                .where(CalendarRevisionDay.calendar_revision_id == revision_id)
                .order_by(CalendarRevisionDay.trade_date)
            )
        )
        .scalars()
        .all()
    )


async def published_year(
    db: AsyncSession, market: str, year: int
) -> tuple[CalendarYearRevision, CalendarMarket, list[CalendarRevisionDay]] | None:
    revision = (
        await db.execute(
            select(CalendarYearRevision).where(
                CalendarYearRevision.market == market,
                CalendarYearRevision.year == year,
                CalendarYearRevision.status == "published",
            )
        )
    ).scalar_one_or_none()
    if revision is None or revision.actual_days != revision.expected_days:
        return None
    config = await db.get(CalendarMarket, market)
    if config is None:
        return None
    days = await revision_days(db, revision.id)
    if len(days) != _expected_days(year):
        return None
    return revision, config, days


async def clone_with_day_change(
    db: AsyncSession,
    *,
    market: str,
    trade_date: date,
    expected_revision: int,
    replacement: dict[str, Any],
    commit: bool = True,
) -> CalendarYearRevision:
    market_config = (
        await db.execute(
            select(CalendarMarket).where(CalendarMarket.market == market).with_for_update()
        )
    ).scalar_one_or_none()
    if market_config is None:
        raise CalendarError("calendar market was not found")
    current = await latest_revision(db, market, trade_date.year)
    if current is None:
        if expected_revision != 0:
            raise RevisionConflictError("calendar changed before manual edit")
        source_values = [
            {**row, "source_kind": "market_default"}
            for row in _all_days(trade_date.year, market_config)
        ]
        next_revision = 1
    elif current.revision != expected_revision:
        raise RevisionConflictError("calendar changed before manual edit")
    else:
        source_days = await revision_days(db, current.id)
        if len(source_days) != _expected_days(trade_date.year):
            raise CalendarError("current revision is incomplete")
        source_values = [
            {
                "trade_date": source.trade_date.isoformat(),
                "status": source.day_status,
                "session_open": source.session_open,
                "session_close": source.session_close,
                "holiday_name": source.holiday_name,
                "description": source.description,
                "source_kind": source.source_kind,
            }
            for source in source_days
        ]
        next_revision = current.revision + 1
    revision = CalendarYearRevision(
        id=uuid7(),
        market=market,
        year=trade_date.year,
        revision=next_revision,
        status="draft",
        expected_days=_expected_days(trade_date.year),
        actual_days=len(source_values),
        source_kind="manual",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    db.add(revision)
    await db.flush()
    changed = False
    rows: list[CalendarRevisionDay] = []
    for source in source_values:
        source_date = date.fromisoformat(source["trade_date"])
        values = replacement if source_date == trade_date else None
        if values is not None:
            changed = True
        rows.append(
            CalendarRevisionDay(
                id=uuid7(),
                calendar_revision_id=revision.id,
                trade_date=source_date,
                day_status=values["status"] if values else source["status"],
                is_open=(values["status"] == "open") if values else source["status"] == "open",
                session_open=_time_or_none(values.get("session_open"))
                if values and values["status"] == "open"
                else _time_or_none(source.get("session_open"))
                if not values
                else None,
                session_close=_time_or_none(values.get("session_close"))
                if values and values["status"] == "open"
                else _time_or_none(source.get("session_close"))
                if not values
                else None,
                holiday_name=values.get("holiday_name") if values else source.get("holiday_name"),
                description=values.get("description") if values else source.get("description"),
                source_kind="manual" if values else source["source_kind"],
            )
        )
    if not changed:
        raise CalendarError("date is outside the managed calendar revision")
    db.add_all(rows)
    if commit:
        await db.commit()
    else:
        await db.flush()
    await db.refresh(revision)
    return revision


async def publish_revision(
    db: AsyncSession,
    *,
    market: str,
    year: int,
    expected_revision: int,
    published_by: str | None,
    commit: bool = True,
) -> CalendarYearRevision:
    await db.execute(
        select(CalendarMarket).where(CalendarMarket.market == market).with_for_update()
    )
    revision = await latest_revision(db, market, year)
    if revision is None or revision.revision != expected_revision:
        raise RevisionConflictError("calendar changed before publish")
    if revision.status == "published":
        return revision
    if revision.status != "draft":
        raise RevisionConflictError("only the latest draft revision can be published")
    if revision.actual_days != _expected_days(year) or len(
        await revision_days(db, revision.id)
    ) != _expected_days(year):
        raise CalendarError("cannot publish incomplete calendar year")
    previous = (
        await db.execute(
            select(CalendarYearRevision)
            .where(
                CalendarYearRevision.market == market,
                CalendarYearRevision.year == year,
                CalendarYearRevision.status == "published",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if previous and previous.id != revision.id:
        previous.status = "superseded"
    revision.status = "published"
    revision.published_at = utc_now()
    revision.published_by = published_by
    revision.updated_at = utc_now()
    if commit:
        await db.commit()
    else:
        await db.flush()
    await db.refresh(revision)
    return revision


async def rollback_revision(
    db: AsyncSession,
    *,
    market: str,
    year: int,
    target_revision: int,
    published_by: str | None,
    commit: bool = True,
) -> CalendarYearRevision:
    """Publish a new highest revision cloned from a prior complete revision."""
    await db.execute(
        select(CalendarMarket).where(CalendarMarket.market == market).with_for_update()
    )
    current = await latest_revision(db, market, year)
    target = (
        await db.execute(
            select(CalendarYearRevision).where(
                CalendarYearRevision.market == market,
                CalendarYearRevision.year == year,
                CalendarYearRevision.revision == target_revision,
            )
        )
    ).scalar_one_or_none()
    if current is None or target is None:
        raise RevisionConflictError("calendar rollback revision was not found")
    target_days = await revision_days(db, target.id)
    if target.actual_days != _expected_days(year) or len(target_days) != _expected_days(year):
        raise CalendarError("cannot roll back to an incomplete calendar year")

    previous = (
        await db.execute(
            select(CalendarYearRevision)
            .where(
                CalendarYearRevision.market == market,
                CalendarYearRevision.year == year,
                CalendarYearRevision.status == "published",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if previous is not None:
        previous.status = "superseded"

    created = CalendarYearRevision(
        id=uuid7(),
        market=market,
        year=year,
        revision=current.revision + 1,
        status="published",
        expected_days=_expected_days(year),
        actual_days=len(target_days),
        source_kind="rollback",
        source_sha256=target.source_sha256,
        source_filename=target.source_filename,
        published_at=utc_now(),
        published_by=published_by,
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    db.add(created)
    await db.flush()
    db.add_all(
        [
            CalendarRevisionDay(
                id=uuid7(),
                calendar_revision_id=created.id,
                trade_date=source.trade_date,
                day_status=source.day_status,
                is_open=source.is_open,
                session_open=source.session_open,
                session_close=source.session_close,
                holiday_name=source.holiday_name,
                description=source.description,
                source_kind="rollback",
            )
            for source in target_days
        ]
    )
    if commit:
        await db.commit()
    else:
        await db.flush()
    await db.refresh(created)
    return created
