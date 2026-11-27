"""Serve reads of Hang Seng Indexes' reference data: index lists, changes and HSICS.

Read only.  Membership is the rebuilt history (``index_membership``): as the
stored reports list it from the series' first report on, rolled back from the
published changes before that.  Weights and closes are the stored reports' own.
Each read needs the feed that stores its source to be active.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canonical import Instrument
from app.models.index_reference import (
    IndexChangeEvent,
    IndexDefinition,
    IndexMembership,
    IndexMembershipBuild,
    IndexSnapshot,
    IndexSnapshotConstituent,
)
from app.schemas.serve import (
    CursorPaginationInfo,
    HsicsClassificationListResponse,
    HsicsClassificationResponse,
    IndexChangeListResponse,
    IndexChangeResponse,
    IndexClassificationResponse,
    IndexConstituentListResponse,
    IndexConstituentResponse,
    IndexDefinitionResponse,
    IndexListResponse,
)
from app.services.serve import (
    _date_text,
    _decode_cursor,
    _encode_cursor,
    _instrument_summary,
    _validate_dates,
    active_reference_schemas,
)
from app.services.serve_errors import serve_error

PUBLISHER = "hsil"
HONG_KONG = ZoneInfo("Asia/Hong_Kong")
CONSTITUENTS = "index_constituents"
CHANGES = "index_constituent_changes"
HSCI = "00011.00"
MAX_SYMBOLS = 50
_CODE = re.compile(r"^[0-9]{5}\.[0-9]{2}$")
_SYMBOL = re.compile(r"^[0-9]{1,5}$")
_INDUSTRY = re.compile(r"^[0-9]{2}$")


async def _require_active(db: AsyncSession, *schemas: str) -> None:
    active = await active_reference_schemas(db)
    if not active.intersection(schemas):
        raise serve_error(422, "DATASET_NOT_AVAILABLE", "Hang Seng index data is not active")


def _today() -> date:
    return datetime.now(timezone.utc).astimezone(HONG_KONG).date()


def _classification(definition: IndexDefinition) -> IndexClassificationResponse | None:
    if definition.classification_code is None:
        return None
    return IndexClassificationResponse(
        scheme="hsics",
        code=definition.classification_code,
        name_en=definition.classification_name_en or definition.classification_code,
        name_zh=definition.classification_name_zh,
    )


async def _observed_from(db: AsyncSession) -> dict[str, date | None]:
    """Each series' first stored report, from its latest membership build."""
    latest = (
        select(
            IndexMembershipBuild.family,
            IndexMembershipBuild.observed_from,
            func.row_number()
            .over(
                partition_by=IndexMembershipBuild.family,
                order_by=IndexMembershipBuild.created_at.desc(),
            )
            .label("rank"),
        )
        .where(IndexMembershipBuild.publisher == PUBLISHER)
        .subquery()
    )
    rows = await db.execute(
        select(latest.c.family, latest.c.observed_from).where(latest.c.rank == 1)
    )
    return {family: observed for family, observed in rows.all()}


async def _describe(
    db: AsyncSession, definitions: Sequence[IndexDefinition]
) -> dict[UUID, IndexDefinitionResponse]:
    ids = [definition.index_id for definition in definitions]
    if not ids:
        return {}
    codes = {
        index_id: code
        for index_id, code in await db.execute(
            select(IndexDefinition.index_id, IndexDefinition.code).where(
                IndexDefinition.publisher == PUBLISHER
            )
        )
    }
    reports: dict[UUID, date] = {
        index_id: latest
        for index_id, latest in await db.execute(
            select(IndexSnapshot.index_id, func.max(IndexSnapshot.trade_date))
            .where(IndexSnapshot.index_id.in_(ids))
            .group_by(IndexSnapshot.index_id)
        )
    }
    counts: dict[UUID, int] = {
        index_id: count
        for index_id, count in await db.execute(
            select(IndexMembership.index_id, func.count())
            .where(IndexMembership.index_id.in_(ids), IndexMembership.valid_to.is_(None))
            .group_by(IndexMembership.index_id)
        )
    }
    observed = await _observed_from(db)
    return {
        definition.index_id: IndexDefinitionResponse(
            code=definition.code,
            name_en=definition.name_en,
            name_zh=definition.name_zh,
            series=definition.series,  # type: ignore[arg-type]
            parent_code=codes.get(definition.parent_index_id)  # type: ignore[arg-type]
            if definition.parent_index_id
            else None,
            classification=_classification(definition),
            history_start=definition.history_start,
            observed_from=observed.get(definition.series),
            latest_report_date=reports.get(definition.index_id),
            constituent_count=int(counts.get(definition.index_id, 0)),
        )
        for definition in definitions
    }


async def _index(db: AsyncSession, code: str) -> tuple[IndexDefinition, IndexDefinitionResponse]:
    if _CODE.fullmatch(code) is None:
        raise serve_error(
            422,
            "INVALID_PARAMETER",
            "code must be an HSIL index code such as 00001.00",
            details={"param": "code"},
        )
    definition = await db.scalar(
        select(IndexDefinition).where(
            IndexDefinition.publisher == PUBLISHER, IndexDefinition.code == code
        )
    )
    if definition is None:
        raise serve_error(404, "INDEX_NOT_FOUND", f"No stored index has code {code}")
    return definition, (await _describe(db, [definition]))[definition.index_id]


def _derived(day: date, observed_from: date | None) -> bool:
    return observed_from is None or day < observed_from


def _within_history(definition: IndexDefinition, day: date) -> bool:
    return definition.history_start is None or day >= definition.history_start


def _members_on(index_ids: Iterable[UUID], day: date) -> Any:
    return select(IndexMembership).where(
        IndexMembership.index_id.in_(list(index_ids)),
        or_(IndexMembership.valid_from.is_(None), IndexMembership.valid_from <= day),
        or_(IndexMembership.valid_to.is_(None), IndexMembership.valid_to > day),
    )


async def _instruments(db: AsyncSession, ids: Iterable[UUID | None]) -> dict[UUID, Instrument]:
    wanted = sorted({value for value in ids if value is not None})
    if not wanted:
        return {}
    rows = await db.scalars(select(Instrument).where(Instrument.instrument_id.in_(wanted)))
    return {row.instrument_id: row for row in rows}


async def list_indexes(db: AsyncSession) -> IndexListResponse:
    """Every stored HSIL index with its parent, industry and coverage."""
    await _require_active(db, CONSTITUENTS, CHANGES)
    definitions = list(
        (
            await db.scalars(
                select(IndexDefinition)
                .where(IndexDefinition.publisher == PUBLISHER)
                .order_by(IndexDefinition.code)
            )
        ).all()
    )
    described = await _describe(db, definitions)
    return IndexListResponse(data=[described[item.index_id] for item in definitions])


async def list_index_constituents(
    db: AsyncSession, *, code: str, as_of: date | None
) -> IndexConstituentListResponse:
    """An index's constituents on one date, with the latest stored weights by then."""
    await _require_active(db, CONSTITUENTS)
    definition, described = await _index(db, code)
    day = as_of or _today()
    if not _within_history(definition, day):
        raise serve_error(
            422,
            "DATE_BEFORE_HISTORY",
            f"{code} has no published history before {definition.history_start}",
            details={"history_start": _date_text(definition.history_start)},
        )
    members = list(
        (
            await db.scalars(
                _members_on([definition.index_id], day).order_by(IndexMembership.symbol)
            )
        ).all()
    )
    report = await db.scalar(
        select(IndexSnapshot)
        .where(IndexSnapshot.index_id == definition.index_id, IndexSnapshot.trade_date <= day)
        .order_by(IndexSnapshot.trade_date.desc())
        .limit(1)
    )
    weights: dict[str, IndexSnapshotConstituent] = {}
    if report is not None:
        weights = {
            row.symbol: row
            for row in await db.scalars(
                select(IndexSnapshotConstituent).where(
                    IndexSnapshotConstituent.snapshot_id == report.snapshot_id
                )
            )
        }
    instruments = await _instruments(db, (member.instrument_id for member in members))
    data = []
    for member in members:
        listed = weights.get(member.symbol)
        instrument = instruments.get(member.instrument_id) if member.instrument_id else None
        data.append(
            IndexConstituentResponse(
                symbol=member.symbol,
                name_en=member.name_en,
                instrument=_instrument_summary(instrument) if instrument else None,
                member_since=member.valid_from,
                weight_pct=listed.weight_pct if listed else None,
                close=listed.close if listed else None,
            )
        )
    return IndexConstituentListResponse(
        index=described,
        as_of=day,
        derived=_derived(day, described.observed_from),
        weights_date=report.trade_date if report else None,
        data=data,
    )


async def list_index_changes(
    db: AsyncSession,
    *,
    code: str,
    start_date: date | None,
    end_date: date | None,
    cursor: str | None,
    page_size: int,
) -> IndexChangeListResponse:
    """An index's effective changes as HSIL's workbook lists them, newest first."""
    await _require_active(db, CHANGES)
    _validate_dates(start_date, end_date)
    definition, described = await _index(db, code)
    scope = {"code": code, "start_date": _date_text(start_date), "end_date": _date_text(end_date)}
    query = select(IndexChangeEvent).where(
        IndexChangeEvent.index_id == definition.index_id,
        IndexChangeEvent.status == "effective",
    )
    if start_date is not None:
        query = query.where(IndexChangeEvent.effective_date >= start_date)
    if end_date is not None:
        query = query.where(IndexChangeEvent.effective_date <= end_date)
    if cursor:
        payload = _decode_cursor(cursor)
        if (
            set(payload) != {"v", "kind", "scope", "last_date", "last_ordinal"}
            or payload.get("v") != 1
            or payload.get("kind") != "index_changes"
            or payload.get("scope") != scope
        ):
            raise serve_error(422, "INVALID_CURSOR", "Cursor does not match this query")
        try:
            last_date = date.fromisoformat(payload["last_date"])
            last_ordinal = int(payload["last_ordinal"])
        except (TypeError, ValueError):
            raise serve_error(422, "INVALID_CURSOR", "Cursor is invalid") from None
        query = query.where(
            or_(
                IndexChangeEvent.effective_date < last_date,
                and_(
                    IndexChangeEvent.effective_date == last_date,
                    IndexChangeEvent.source_ordinal > last_ordinal,
                ),
            )
        )
    events = list(
        (
            await db.scalars(
                query.order_by(
                    IndexChangeEvent.effective_date.desc(), IndexChangeEvent.source_ordinal
                ).limit(page_size + 1)
            )
        ).all()
    )
    next_cursor = None
    if len(events) > page_size:
        tail = events[page_size - 1]
        next_cursor = _encode_cursor(
            {
                "v": 1,
                "kind": "index_changes",
                "scope": scope,
                "last_date": tail.effective_date.isoformat(),
                "last_ordinal": tail.source_ordinal,
            }
        )
        events = events[:page_size]
    return IndexChangeListResponse(
        index=described,
        data=[
            IndexChangeResponse(
                effective_date=event.effective_date,
                action=event.action,  # type: ignore[arg-type]
                symbol=event.symbol,
                name_en=event.name_en,
                name_zh=event.name_zh,
                constituents_after=event.constituents_after,
            )
            for event in events
        ],
        pagination=CursorPaginationInfo(page_size=page_size, next_cursor=next_cursor),
    )


def _symbols(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    values = [token.strip() for token in raw.split(",")]
    if (
        not 1 <= len(values) <= MAX_SYMBOLS
        or any(_SYMBOL.fullmatch(value) is None for value in values)
        or len({value.zfill(5) for value in values}) != len(values)
    ):
        raise serve_error(
            422,
            "INVALID_SYMBOLS",
            f"symbols must hold 1 to {MAX_SYMBOLS} distinct HKEX stock codes such as 00005",
            details={"param": "symbols"},
        )
    return [value.zfill(5) for value in values]


async def list_hsics_classifications(
    db: AsyncSession, *, as_of: date | None, industry: str | None, symbols: str | None
) -> HsicsClassificationListResponse:
    """HSCI constituents' HSICS industry on one date, from the industry indexes they are in."""
    await _require_active(db, CONSTITUENTS)
    if industry is not None and _INDUSTRY.fullmatch(industry) is None:
        raise serve_error(
            422,
            "INVALID_PARAMETER",
            "industry must be a two-digit HSICS code such as 50",
            details={"param": "industry"},
        )
    wanted = _symbols(symbols)
    day = as_of or _today()
    root = await db.scalar(
        select(IndexDefinition).where(
            IndexDefinition.publisher == PUBLISHER, IndexDefinition.code == HSCI
        )
    )
    query = select(IndexDefinition).where(
        IndexDefinition.publisher == PUBLISHER,
        IndexDefinition.classification_code.is_not(None),
    )
    if root is not None:
        query = query.where(IndexDefinition.parent_index_id == root.index_id)
    if industry is not None:
        query = query.where(IndexDefinition.classification_code == industry)
    industries = {
        item.index_id: item
        for item in (await db.scalars(query)).all()
        # An industry index before its own launch classifies nothing.
        if _within_history(item, day)
    }
    members = []
    if industries:
        statement = _members_on(industries, day)
        if wanted is not None:
            statement = statement.where(IndexMembership.symbol.in_(wanted))
        members = list((await db.scalars(statement.order_by(IndexMembership.symbol))).all())
    instruments = await _instruments(db, (member.instrument_id for member in members))
    observed = (await _observed_from(db)).get("hsci")
    data = []
    for member in members:
        index = industries[member.index_id]
        instrument = instruments.get(member.instrument_id) if member.instrument_id else None
        classification = _classification(index)
        assert classification is not None
        data.append(
            HsicsClassificationResponse(
                symbol=member.symbol,
                name_en=member.name_en,
                instrument=_instrument_summary(instrument) if instrument else None,
                industry=classification,
                index_code=index.code,
                member_since=member.valid_from,
            )
        )
    return HsicsClassificationListResponse(as_of=day, derived=_derived(day, observed), data=data)
