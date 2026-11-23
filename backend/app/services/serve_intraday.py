"""Read-only Serve queries over canonical intraday bars and ticks.

Rows live in Parquet objects of the canonical bucket; the ``intraday_partition``
index decides which objects a query reads, and a day without an index row is
never read.  Queries are for one instrument at a time and bounded: bars by date
range and page size, ticks by a single trade date.  Object reads run in worker
threads, are capped per process, and come from a small cache of recently read
days keyed by object version.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal, TypeVar, cast
from uuid import UUID
from zoneinfo import ZoneInfo

import pyarrow as pa
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.canonical import Instrument, IntradayPartition
from app.schemas.serve import (
    CursorPaginationInfo,
    IntradayBarListResponse,
    IntradayBarResponse,
    IntradayCoverageResponse,
    IntradayInstrumentResponse,
    IntradayInterval,
    IntradayTickListResponse,
    IntradayTickResponse,
    IntradayUnitsResponse,
    ResolvedDateRangeResponse,
)
from app.services.intraday_partitions import IntradayStorageError
from app.services.intraday_reads import DayCache, IndexedDay, read_days
from app.services.object_storage import (
    CanonicalReader,
    ObjectChangedError,
    ObjectNotFoundError,
    ObjectStorageConfigError,
    ObjectStorageError,
)
from app.services.serve import (
    IntradayDatasetScope,
    _decode_cursor,
    _encode_cursor,
    active_intraday_datasets,
    serve_error,
)

RecordKind = Literal["bar_1m", "tick"]
T = TypeVar("T")

INTERVAL_MINUTES: dict[str, int] = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}
# Calendar days one bars query may span: 31 for one-minute bars, a year when aggregated.
MAX_BAR_RANGE_DAYS = {"1m": 31, "aggregated": 366}
# Distinct objects one page may read; a page with more days left ends early with a cursor.
MAX_OBJECTS_PER_PAGE = 40
# Objects read together before checking whether the page is full.
BAR_BATCH_OBJECTS = 8


class _Session:
    """When a market's regular session opens, and the label FinLab gives its last bucket."""

    def __init__(self, timezone_name: str, opens: time, close_label: time) -> None:
        self.zone = ZoneInfo(timezone_name)
        self.timezone_name = timezone_name
        self.opens = opens
        self.close_label = close_label


# Buckets follow FinLab's own aggregation: labelled by their end, counted from the
# open, the last one runs from its start to the close and is labelled 13:33.
SESSIONS: dict[str, _Session] = {"TW": _Session("Asia/Taipei", time(9, 0), time(13, 33))}


# ---------------------------------------------------------------------------
# Process-wide read protection
# ---------------------------------------------------------------------------

_reader_lock = threading.Lock()
_reader: CanonicalReader | None = None
_read_slots: threading.BoundedSemaphore | None = None
_cache: DayCache | None = None


def canonical_reader() -> CanonicalReader:
    """The Serve role's canonical reader, created on first use."""
    global _reader
    with _reader_lock:
        if _reader is None:
            _reader = CanonicalReader()
        return _reader


def _slots() -> threading.BoundedSemaphore:
    global _read_slots
    with _reader_lock:
        if _read_slots is None:
            _read_slots = threading.BoundedSemaphore(
                max(1, get_settings().INTRADAY_SERVE_MAX_CONCURRENT_READS)
            )
        return _read_slots


def day_cache() -> DayCache:
    global _cache
    with _reader_lock:
        if _cache is None:
            _cache = DayCache(max(0, get_settings().INTRADAY_SERVE_CACHE_BYTES))
        return _cache


def reset_read_state() -> None:
    """Forget the reader, read slots and cache (tests and configuration reloads)."""
    global _reader, _read_slots, _cache
    with _reader_lock:
        _reader = None
        _read_slots = None
        _cache = None


class _ReadsBusyError(Exception):
    pass


def _read_with_slot(work: Callable[[], T]) -> T:
    slots = _slots()
    if not slots.acquire(timeout=max(0.0, get_settings().INTRADAY_SERVE_READ_WAIT_SECONDS)):
        raise _ReadsBusyError
    try:
        return work()
    finally:
        slots.release()


def _unavailable(code: str, message: str, retry_after: int | None = None) -> HTTPException:
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return HTTPException(
        status_code=503, detail={"code": code, "message": message}, headers=headers
    )


async def _indexed_days(
    db: AsyncSession, dataset_key: str, symbol: str, first: date, last: date
) -> list[IndexedDay]:
    rows = await db.execute(
        select(
            IntradayPartition.trade_date,
            IntradayPartition.row_count,
            IntradayPartition.object_key,
            IntradayPartition.object_etag,
            IntradayPartition.source,
            IntradayPartition.source_fetched_at,
        )
        .where(
            IntradayPartition.dataset_key == dataset_key,
            IntradayPartition.symbol == symbol,
            IntradayPartition.trade_date.between(first, last),
        )
        .order_by(IntradayPartition.trade_date)
    )
    return [IndexedDay(*row) for row in rows]


async def _read(
    db: AsyncSession,
    dataset_key: str,
    symbol: str,
    days: Sequence[IndexedDay],
    kind: RecordKind,
) -> tuple[list[IndexedDay], dict[date, pa.Table]]:
    """Rows of ``days``; when an object moved under the index, read the index again once.

    A month merge or a republished day replaces the object a day is indexed to.
    The second attempt follows the committed index; a second disagreement is
    reported as temporary rather than answered with part of a day.
    """
    try:
        reader = canonical_reader()
    except ObjectStorageConfigError:
        raise _unavailable(
            "INTRADAY_STORAGE_UNAVAILABLE", "Intraday storage is not available"
        ) from None
    cache = day_cache()
    for attempt in range(2):
        try:
            chosen = list(days)
            rows = await asyncio.to_thread(
                _read_with_slot, lambda: read_days(reader, chosen, kind, cache)
            )
            return chosen, rows
        except _ReadsBusyError:
            raise _unavailable(
                "INTRADAY_READS_BUSY", "Too many intraday reads in progress", retry_after=1
            ) from None
        except (ObjectChangedError, ObjectNotFoundError, IntradayStorageError):
            if attempt == 1 or not days:
                break
            wanted = {day.trade_date for day in days}
            fresh = await _indexed_days(db, dataset_key, symbol, min(wanted), max(wanted))
            days = [day for day in fresh if day.trade_date in wanted]
        except ObjectStorageError:
            raise _unavailable(
                "INTRADAY_STORAGE_UNAVAILABLE", "Intraday storage is not available", 5
            ) from None
    raise _unavailable(
        "INTRADAY_DATA_CHANGING", "Intraday data is being replaced; retry shortly", 5
    )


# ---------------------------------------------------------------------------
# Shared request handling
# ---------------------------------------------------------------------------


async def _resolve(
    db: AsyncSession, instrument_id: UUID, kind: RecordKind
) -> tuple[Instrument, IntradayDatasetScope]:
    instrument = await db.get(Instrument, instrument_id)
    if instrument is None:
        raise serve_error(404, "INSTRUMENT_NOT_FOUND", "Instrument not found")
    for scope in await active_intraday_datasets(db):
        if (
            scope.record_kind == kind
            and scope.market == instrument.market
            and scope.asset_class == instrument.asset_class
        ):
            if instrument.market not in SESSIONS:  # pragma: no cover - scopes are TW only
                break
            return instrument, scope
    label = "Intraday bars" if kind == "bar_1m" else "Intraday ticks"
    raise serve_error(
        422, "DATASET_NOT_AVAILABLE", f"{label} are not available for this instrument"
    )


async def _latest_date(db: AsyncSession, dataset_key: str, symbol: str) -> date | None:
    return cast(
        date | None,
        await db.scalar(
            select(func.max(IntradayPartition.trade_date)).where(
                IntradayPartition.dataset_key == dataset_key,
                IntradayPartition.symbol == symbol,
            )
        ),
    )


def _instrument(instrument: Instrument) -> IntradayInstrumentResponse:
    return IntradayInstrumentResponse(
        instrument_id=instrument.instrument_id,
        symbol=instrument.symbol,
        name=instrument.name,
        market=instrument.market,
        asset_class=instrument.asset_class,
        currency=instrument.currency,
    )


def _decimal(value: float | None) -> Decimal | None:
    # The shortest text that reads back as the stored double: no binary tail.
    return None if value is None else Decimal(repr(value))


def _cursor_payload(cursor: str, kind: str, expected: dict[str, Any], last: set[str]) -> dict:
    payload = _decode_cursor(cursor)
    if (
        set(payload) != {"v", "kind", "scope", "last"}
        or payload.get("v") != 1
        or payload.get("kind") != kind
        or payload.get("scope") != expected
        or not isinstance(payload.get("last"), dict)
        or set(payload["last"]) != last
    ):
        raise serve_error(422, "INVALID_CURSOR", "Cursor does not match this query")
    return cast(dict, payload["last"])


# ---------------------------------------------------------------------------
# Bars
# ---------------------------------------------------------------------------


def _aggregate(
    rows: list[dict[str, Any]], interval: str, session: _Session
) -> list[dict[str, Any]]:
    """One trading day's one-minute bars grouped into ``interval`` buckets.

    A bucket holds the bars whose start falls in it and is labelled by its end;
    the bucket reaching past the close is labelled with the close label.  Only
    minutes that traded have bars, so a bucket without trades has no row.
    """
    minutes = INTERVAL_MINUTES[interval]
    if minutes == 1:
        return [
            {**row, "bar_end_time": row["bar_start_time"] + timedelta(minutes=1)} for row in rows
        ]
    buckets: dict[tuple[str, datetime], list[dict[str, Any]]] = {}
    for row in rows:
        local = row["bar_start_time"].astimezone(session.zone)
        opens = datetime.combine(local.date(), session.opens, session.zone)
        index = int((local - opens).total_seconds() // (minutes * 60))
        start = opens + timedelta(minutes=index * minutes)
        buckets.setdefault((row["session"], start), []).append(row)
    result: list[dict[str, Any]] = []
    for (session_name, start), members in sorted(buckets.items(), key=lambda item: item[0][1]):
        end = start + timedelta(minutes=minutes)
        close = datetime.combine(start.date(), session.close_label, session.zone)
        if session_name == "regular" and end > close >= start:
            end = close
        volume = sum(member["volume"] for member in members)
        counts = [member["trade_count"] for member in members]
        weighted = [
            (member["vwap"], member["volume"]) for member in members if member["vwap"] is not None
        ]
        vwap = None
        if len(weighted) == len(members) and volume > 0:
            vwap = sum(price * lots for price, lots in weighted) / volume
        result.append(
            {
                "trade_date": members[0]["trade_date"],
                "bar_start_time": start.astimezone(timezone.utc),
                "bar_end_time": end.astimezone(timezone.utc),
                "session": session_name,
                "open": members[0]["open"],
                "high": max(member["high"] for member in members),
                "low": min(member["low"] for member in members),
                "close": members[-1]["close"],
                "volume": volume,
                "trade_count": None if None in counts else sum(cast(list[int], counts)),
                "vwap": vwap,
            }
        )
    return result


async def list_intraday_bars(
    db: AsyncSession,
    *,
    instrument_id: UUID,
    interval: IntradayInterval,
    start_date: date | None,
    end_date: date | None,
    cursor: str | None,
    page_size: int,
) -> IntradayBarListResponse:
    if (start_date is None) != (end_date is None):
        raise serve_error(
            422, "INVALID_DATE_RANGE", "start_date and end_date must be provided together"
        )
    limit = MAX_BAR_RANGE_DAYS["1m" if interval == "1m" else "aggregated"]
    if start_date and end_date and not 0 <= (end_date - start_date).days < limit:
        raise serve_error(
            422,
            "INVALID_DATE_RANGE",
            f"Bar range must be ordered and span at most {limit} calendar days",
        )
    instrument, scope = await _resolve(db, instrument_id, "bar_1m")
    session = SESSIONS[instrument.market]
    anchor: Literal["explicit", "latest_available"] = "explicit"
    if start_date is None:
        anchor = "latest_available"
        start_date = end_date = await _latest_date(db, scope.dataset_key, instrument.symbol)
    resolved = ResolvedDateRangeResponse(start_date=start_date, end_date=end_date, anchor=anchor)

    def respond(
        data: list[IntradayBarResponse], next_cursor: str | None = None
    ) -> IntradayBarListResponse:
        return IntradayBarListResponse(
            instrument=_instrument(instrument),
            dataset_key=scope.dataset_key,
            interval=interval,
            units=IntradayUnitsResponse(market_timezone=session.timezone_name),
            range=resolved,
            data=data,
            pagination=CursorPaginationInfo(page_size=page_size, next_cursor=next_cursor),
        )

    if start_date is None or end_date is None:
        return respond([])
    expected = {
        "instrument_id": str(instrument_id),
        "interval": interval,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "anchor": anchor,
    }
    after: tuple[date, datetime] | None = None
    if cursor:
        position = _cursor_payload(
            cursor, "intraday_bars", expected, {"trade_date", "bar_start_time"}
        )
        try:
            after = (
                date.fromisoformat(position["trade_date"]),
                datetime.fromisoformat(position["bar_start_time"]),
            )
        except (TypeError, ValueError):
            raise serve_error(422, "INVALID_CURSOR", "Cursor is invalid") from None
        if after[1].tzinfo is None or not start_date <= after[0] <= end_date:
            raise serve_error(422, "INVALID_CURSOR", "Cursor is invalid")
    pending = await _indexed_days(
        db, scope.dataset_key, instrument.symbol, after[0] if after else start_date, end_date
    )
    # Whole days are read in small batches until the page is full, and a page
    # reads at most MAX_OBJECTS_PER_PAGE distinct objects; days left unread are
    # reached through the cursor, so a page may be shorter than page_size.
    collected: list[dict[str, Any]] = []
    objects_read: set[str] = set()
    last_read: date | None = None
    while pending and len(collected) <= page_size and len(objects_read) < MAX_OBJECTS_PER_PAGE:
        batch: list[IndexedDay] = []
        objects: set[str] = set()
        for day in pending:
            if day.object_key not in objects and (
                len(objects) == BAR_BATCH_OBJECTS
                or len(objects_read | objects) == MAX_OBJECTS_PER_PAGE
            ):
                break
            objects.add(day.object_key)
            batch.append(day)
        pending = pending[len(batch) :]
        objects_read |= objects
        batch, stored = await _read(db, scope.dataset_key, instrument.symbol, batch, "bar_1m")
        for day in batch:
            last_read = day.trade_date
            table = stored.get(day.trade_date)
            if table is None:
                continue
            for row in _aggregate(table.to_pylist(), interval, session):
                if after is not None and (row["trade_date"], row["bar_start_time"]) <= after:
                    continue
                row["source"] = day.source
                row["source_fetched_at"] = day.source_fetched_at
                collected.append(row)
    next_cursor = None
    last: dict[str, str] | None = None
    if len(collected) > page_size or (pending and collected):
        collected = collected[:page_size]
        last = {
            "trade_date": collected[-1]["trade_date"].isoformat(),
            "bar_start_time": collected[-1]["bar_start_time"].isoformat(),
        }
    elif pending and last_read is not None:
        # Nothing left on the days read: continue after the last of them.
        day_end = datetime.combine(last_read + timedelta(days=1), time.min, session.zone)
        last = {
            "trade_date": last_read.isoformat(),
            "bar_start_time": day_end.astimezone(timezone.utc).isoformat(),
        }
    if last is not None:
        next_cursor = _encode_cursor(
            {"v": 1, "kind": "intraday_bars", "scope": expected, "last": last}
        )
    return respond(
        [
            IntradayBarResponse(
                trade_date=row["trade_date"],
                bar_start_time=row["bar_start_time"],
                bar_end_time=row["bar_end_time"],
                session=row["session"],
                open=cast(Decimal, _decimal(row["open"])),
                high=cast(Decimal, _decimal(row["high"])),
                low=cast(Decimal, _decimal(row["low"])),
                close=cast(Decimal, _decimal(row["close"])),
                volume=row["volume"],
                trade_count=row["trade_count"],
                vwap=_decimal(row["vwap"]),
                source=row["source"],
                source_fetched_at=row["source_fetched_at"],
            )
            for row in collected
        ],
        next_cursor,
    )


# ---------------------------------------------------------------------------
# Ticks
# ---------------------------------------------------------------------------


async def list_intraday_ticks(
    db: AsyncSession,
    *,
    instrument_id: UUID,
    trade_date: date | None,
    start_time: time | None,
    end_time: time | None,
    cursor: str | None,
    page_size: int,
) -> IntradayTickListResponse:
    if start_time and end_time and start_time > end_time:
        raise serve_error(422, "INVALID_TIME_RANGE", "start_time must not be after end_time")
    if (start_time and start_time.tzinfo) or (end_time and end_time.tzinfo):
        raise serve_error(
            422, "INVALID_TIME_RANGE", "start_time and end_time are exchange-local times"
        )
    instrument, scope = await _resolve(db, instrument_id, "tick")
    session = SESSIONS[instrument.market]
    anchor: Literal["explicit", "latest_available"] = "explicit"
    if trade_date is None:
        anchor = "latest_available"
        trade_date = await _latest_date(db, scope.dataset_key, instrument.symbol)
    resolved_date = trade_date

    def respond(
        data: list[IntradayTickResponse], next_cursor: str | None = None
    ) -> IntradayTickListResponse:
        return IntradayTickListResponse(
            instrument=_instrument(instrument),
            dataset_key=scope.dataset_key,
            units=IntradayUnitsResponse(market_timezone=session.timezone_name),
            trade_date=resolved_date,
            anchor=anchor,
            data=data,
            pagination=CursorPaginationInfo(page_size=page_size, next_cursor=next_cursor),
        )

    if trade_date is None:
        return respond([])
    expected = {
        "instrument_id": str(instrument_id),
        "trade_date": trade_date.isoformat(),
        "start_time": start_time.isoformat() if start_time else None,
        "end_time": end_time.isoformat() if end_time else None,
    }
    after: int | None = None
    if cursor:
        last = _cursor_payload(cursor, "intraday_ticks", expected, {"sequence"})
        if type(last["sequence"]) is not int:
            raise serve_error(422, "INVALID_CURSOR", "Cursor is invalid")
        after = last["sequence"]
    days = await _indexed_days(db, scope.dataset_key, instrument.symbol, trade_date, trade_date)
    days, stored = await _read(db, scope.dataset_key, instrument.symbol, days, "tick")
    if not days or trade_date not in stored:
        return respond([])
    day = days[0]
    table = stored[trade_date]
    rows: list[dict[str, Any]] = []
    for row in table.to_pylist():
        if after is not None and row["sequence"] <= after:
            continue
        local = row["trade_time"].astimezone(session.zone).time()
        if (start_time and local < start_time) or (end_time and local > end_time):
            continue
        rows.append(row)
        if len(rows) > page_size:
            break
    next_cursor = None
    if len(rows) > page_size:
        rows = rows[:page_size]
        next_cursor = _encode_cursor(
            {
                "v": 1,
                "kind": "intraday_ticks",
                "scope": expected,
                "last": {"sequence": rows[-1]["sequence"]},
            }
        )
    return respond(
        [
            IntradayTickResponse(
                trade_date=row["trade_date"],
                trade_time=row["trade_time"],
                sequence=row["sequence"],
                session=row["session"],
                price=cast(Decimal, _decimal(row["price"])),
                volume=row["volume"],
                tick_type=row["tick_type"],
                bid_price=_decimal(row["bid_price"]),
                bid_volume=row["bid_volume"],
                ask_price=_decimal(row["ask_price"]),
                ask_volume=row["ask_volume"],
                source=day.source,
                source_fetched_at=day.source_fetched_at,
            )
            for row in rows
        ],
        next_cursor,
    )


# ---------------------------------------------------------------------------
# Catalog and coverage
# ---------------------------------------------------------------------------


async def intraday_dataset_coverage(
    db: AsyncSession, dataset_key: str
) -> tuple[date | None, date | None, int]:
    first, latest, symbols = (
        await db.execute(
            select(
                func.min(IntradayPartition.trade_date),
                func.max(IntradayPartition.trade_date),
                func.count(func.distinct(IntradayPartition.symbol)),
            ).where(IntradayPartition.dataset_key == dataset_key)
        )
    ).one()
    return first, latest, int(symbols or 0)


async def intraday_instrument_coverage(
    db: AsyncSession,
    scopes: Sequence[IntradayDatasetScope],
    instruments: Sequence[Instrument],
) -> dict[tuple[UUID, RecordKind], IntradayCoverageResponse]:
    """First and latest stored day of each instrument's bars and ticks."""
    by_key = {scope.dataset_key: scope for scope in scopes}
    wanted: dict[tuple[str, str], UUID] = {}
    for instrument in instruments:
        for scope in scopes:
            if (scope.market, scope.asset_class) == (instrument.market, instrument.asset_class):
                wanted[(scope.dataset_key, instrument.symbol)] = instrument.instrument_id
    if not wanted:
        return {}
    rows = await db.execute(
        select(
            IntradayPartition.dataset_key,
            IntradayPartition.symbol,
            func.min(IntradayPartition.trade_date),
            func.max(IntradayPartition.trade_date),
        )
        .where(
            IntradayPartition.dataset_key.in_(sorted({key for key, _ in wanted})),
            IntradayPartition.symbol.in_(sorted({symbol for _, symbol in wanted})),
        )
        .group_by(IntradayPartition.dataset_key, IntradayPartition.symbol)
    )
    result: dict[tuple[UUID, RecordKind], IntradayCoverageResponse] = {}
    for dataset_key, symbol, first, latest in rows:
        instrument_id = wanted.get((dataset_key, symbol))
        if instrument_id is None:
            continue
        result[(instrument_id, by_key[dataset_key].record_kind)] = IntradayCoverageResponse(
            first_date=first, latest_date=latest
        )
    return result
