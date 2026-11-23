"""Canonical intraday objects: prove delivered rows, store a symbol's day, merge a month.

Bars and ticks live as Parquet objects in the canonical bucket; PostgreSQL keeps
one ``intraday_partition`` row per (dataset, symbol, trade date) naming the object
that holds that day.  A day is written as its own object and replaced as a whole.
After its month has ended the day objects are merged into one month object.

Every caller holds the dataset's normalization lock, so one writer at a time
touches a dataset's objects.  Writes are still conditional on the version last
observed, and a reader takes from an object only the trade dates the index
assigns to it: a month object may keep rows of a day that was republished later.
"""

from __future__ import annotations

import asyncio
import calendar
import io
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canonical import IntradayPartition
from app.schemas.ingress import INTRADAY_ROW_LAYOUTS
from app.services.object_storage import (
    CanonicalPublisher,
    CanonicalReader,
    ObjectChangedError,
    ObjectStorageError,
)
from app.utils import utc_now

logger = logging.getLogger(__name__)

KEY_PREFIX = "intraday/v1"
CONTENT_TYPE = "application/vnd.apache.parquet"
# Decoded size a delivered object may claim before it is read into memory.
MAX_DECODED_BYTES = 512 * 1024 * 1024
MAX_DAY_OBJECT_BYTES = 64 * 1024 * 1024
MAX_MONTH_OBJECT_BYTES = 1024 * 1024 * 1024

RecordKind = Literal["bar_1m", "tick"]
DayOutcome = Literal["written", "unchanged", "stale"]

_ARROW_TYPES: dict[str, pa.DataType] = {
    "string": pa.string(),
    "date32[day]": pa.date32(),
    "timestamp[us, tz=UTC]": pa.timestamp("us", tz="UTC"),
    "double": pa.float64(),
    "int64": pa.int64(),
    "int8": pa.int8(),
}
# The column that orders and identifies a row within its day.
_ROW_KEY: dict[str, str] = {"bar_1m": "bar_start_time", "tick": "sequence"}
_INSTANT: dict[str, str] = {"bar_1m": "bar_start_time", "tick": "trade_time"}
_NON_NEGATIVE: dict[str, tuple[str, ...]] = {
    "bar_1m": ("open", "high", "low", "close", "volume", "trade_count", "vwap"),
    "tick": ("price", "volume", "bid_price", "bid_volume", "ask_price", "ask_volume"),
}


class RowsRejectedError(ValueError):
    """Delivered rows break the contract; the day is not stored."""

    def __init__(self, rule: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(rule)
        self.rule = rule
        self.detail = detail or {}


class IntradayStorageError(RuntimeError):
    """A stored object does not hold what the index says; nothing is overwritten."""


@dataclass(frozen=True, slots=True)
class DayDelivery:
    """What one accepted delivery says about a symbol's trading day."""

    dataset_key: str
    symbol: str
    trade_date: date
    source: str
    source_raw_sha256: str
    source_rows_sha256: str
    fetched_at: datetime
    run_id: UUID


def row_schema(kind: str) -> pa.Schema:
    """The Arrow schema of the published layout for ``kind``."""
    return pa.schema(
        [
            pa.field(column.name, _ARROW_TYPES[column.type], nullable=column.nullable)
            for column in INTRADAY_ROW_LAYOUTS[kind]
        ]
    )


def day_key(dataset_key: str, symbol: str, trade_date: date) -> str:
    return (
        f"{KEY_PREFIX}/{dataset_key}/symbol={symbol}/year={trade_date.year:04d}"
        f"/month={trade_date.month:02d}/day={trade_date.day:02d}/part.parquet"
    )


def month_key(dataset_key: str, symbol: str, year: int, month: int) -> str:
    return (
        f"{KEY_PREFIX}/{dataset_key}/symbol={symbol}/year={year:04d}/month={month:02d}/part.parquet"
    )


def _all(expression: pa.ChunkedArray | pa.Array) -> bool:
    """True when every non-null value is true; an empty or all-null column passes."""
    return bool(pc.all(expression, min_count=0).as_py())


def read_rows(
    body: bytes,
    *,
    kind: str,
    symbol: str,
    trade_date: date,
    row_count: int,
    exchange_timezone: str,
) -> pa.Table:
    """Return the delivered rows in canonical order, or say which rule they break.

    The object must be exactly the published layout for one symbol and one
    exchange-local trade date.  A single bad row rejects the whole day.
    """
    layout = INTRADAY_ROW_LAYOUTS[kind]
    try:
        parquet = pq.ParquetFile(io.BytesIO(body))
        metadata = parquet.metadata
        actual = [(field.name, str(field.type)) for field in parquet.schema_arrow]
    except (pa.ArrowException, OSError, ValueError):
        raise RowsRejectedError("not_parquet") from None
    expected = [(column.name, column.type) for column in layout]
    if actual != expected:
        raise RowsRejectedError("layout", {"expected": expected, "actual": actual[:32]})
    if metadata.num_rows != row_count:
        raise RowsRejectedError(
            "row_count", {"declared": row_count, "actual": int(metadata.num_rows)}
        )
    decoded = sum(
        metadata.row_group(index).total_byte_size for index in range(metadata.num_row_groups)
    )
    if decoded > MAX_DECODED_BYTES:
        raise RowsRejectedError("decoded_size", {"bytes": int(decoded)})
    try:
        table = parquet.read().combine_chunks()
    except (pa.ArrowException, OSError, ValueError):
        raise RowsRejectedError("not_parquet") from None

    for column in layout:
        if not column.nullable and table[column.name].null_count:
            raise RowsRejectedError("null_value", {"column": column.name})
    if not _all(pc.equal(table["symbol"], symbol)):
        raise RowsRejectedError("other_symbol")
    if not _all(pc.equal(table["trade_date"], pa.scalar(trade_date, pa.date32()))):
        raise RowsRejectedError("other_trade_date")
    zone = ZoneInfo(exchange_timezone)
    day_start = datetime.combine(trade_date, time.min, zone).astimezone(timezone.utc)
    day_end = datetime.combine(trade_date + timedelta(days=1), time.min, zone).astimezone(
        timezone.utc
    )
    instants = table[_INSTANT[kind]]
    instant_type = pa.timestamp("us", tz="UTC")
    if not _all(
        pc.and_(
            pc.greater_equal(instants, pa.scalar(day_start, instant_type)),
            pc.less(instants, pa.scalar(day_end, instant_type)),
        )
    ):
        raise RowsRejectedError("instant_outside_trade_date", {"column": _INSTANT[kind]})
    key = _ROW_KEY[kind]
    if pc.count_distinct(table[key]).as_py() != table.num_rows:
        raise RowsRejectedError("duplicate_row_key", {"column": key})
    if not _all(pc.greater(pc.utf8_length(table["session"]), 0)):
        raise RowsRejectedError("empty_session")
    for name in _NON_NEGATIVE[kind]:
        values = table[name]
        valid = pc.greater_equal(values, 0)
        if pa.types.is_floating(values.type):
            valid = pc.and_(valid, pc.is_finite(values))
        if not _all(valid):
            raise RowsRejectedError("negative_or_not_finite", {"column": name})
    if kind == "bar_1m":
        if not _all(pc.equal(pc.floor_temporal(instants, unit="minute"), instants)):
            raise RowsRejectedError("bar_not_minute_aligned")
        low, high = table["low"], table["high"]
        body_low = pc.min_element_wise(table["open"], table["close"])
        body_high = pc.max_element_wise(table["open"], table["close"])
        if not _all(pc.and_(pc.less_equal(low, body_low), pc.greater_equal(high, body_high))):
            raise RowsRejectedError("ohlc_bounds")
    # Nullability becomes part of the stored schema, whatever the producer's file declared.
    return table.cast(row_schema(kind)).sort_by(key)


def encode_rows(table: pa.Table) -> bytes:
    """Serialize rows the same way every time, so an identical day is identical bytes.

    Each trade date gets its own row groups and no row group holds two dates, so
    a reader can fetch one day of a month object by the ``trade_date`` statistics
    instead of the whole month.
    """
    key = _ROW_KEY["bar_1m"] if _ROW_KEY["bar_1m"] in table.column_names else _ROW_KEY["tick"]
    table = table.sort_by([("trade_date", "ascending"), (key, "ascending")])
    sink = io.BytesIO()
    with pq.ParquetWriter(
        sink,
        table.schema,
        compression="zstd",
        use_dictionary=True,
        write_statistics=True,
        data_page_version="2.0",
    ) as writer:
        if table.num_rows == 0:
            writer.write_table(table)
        for trade_date in pc.unique(table["trade_date"]):
            day = table.filter(pc.equal(table["trade_date"], trade_date))
            writer.write_table(day, row_group_size=day.num_rows)
    return sink.getvalue()


def day_row_groups(metadata: pq.FileMetaData, trade_date: date) -> list[int]:
    """Row groups of a stored object that may hold ``trade_date``.

    Uses the ``trade_date`` statistics; a row group without them is always
    included, so an object written before days had their own row groups is
    still read correctly, only less selectively.
    """
    column = metadata.schema.names.index("trade_date")
    selected: list[int] = []
    for index in range(metadata.num_row_groups):
        statistics = metadata.row_group(index).column(column).statistics
        if (
            statistics is None
            or not statistics.has_min_max
            or statistics.min <= trade_date <= statistics.max
        ):
            selected.append(index)
    return selected


def _put(publisher: CanonicalPublisher, key: str, body: bytes, expected_etag: str | None) -> str:
    """Write ``key`` at the indexed version; adopt a version no index row recorded.

    An attempt that wrote the object and then failed to commit leaves a version
    the index never learned.  Only this dataset's lock holder writes here, so
    that version is replaced once at the version actually stored; a second
    conflict means another writer is active and is not overridden.
    """
    try:
        return publisher.write(key, body, content_type=CONTENT_TYPE, expected_etag=expected_etag)
    except ObjectChangedError:
        stored = publisher.version(key)
        if stored == expected_etag:
            raise
        logger.warning("Replacing an unindexed version of canonical object %s", key)
        return publisher.write(key, body, content_type=CONTENT_TYPE, expected_etag=stored)


async def store_day(
    db: AsyncSession,
    publisher: CanonicalPublisher,
    delivery: DayDelivery,
    table: pa.Table,
) -> DayOutcome:
    """Make ``table`` the stored day unless the index already holds it or something newer.

    The object is written before the index row, in the caller's transaction: a
    failure in between leaves an object the retry replaces, never an index row
    that names rows which were not stored.
    """
    row = await db.get(
        IntradayPartition,
        (delivery.dataset_key, delivery.symbol, delivery.trade_date),
        with_for_update=True,
    )
    if (
        row is not None
        and row.source_raw_sha256 == delivery.source_raw_sha256
        and row.source_rows_sha256 == delivery.source_rows_sha256
    ):
        # The same data again: the stored object stands, this run becomes its evidence.
        row.run_id = delivery.run_id
        row.source_fetched_at = max(row.source_fetched_at, delivery.fetched_at)
        return "unchanged"
    if row is not None and delivery.fetched_at < row.source_fetched_at:
        return "stale"

    key = day_key(delivery.dataset_key, delivery.symbol, delivery.trade_date)
    body = await asyncio.to_thread(encode_rows, table)
    # A day already merged into its month gets a day object again; the month
    # object keeps the superseded rows until the next merge and is not read for them.
    expected = row.object_etag if row is not None and row.object_layout == "day" else None
    etag = await asyncio.to_thread(_put, publisher, key, body, expected)
    if row is None:
        row = IntradayPartition(
            dataset_key=delivery.dataset_key,
            symbol=delivery.symbol,
            trade_date=delivery.trade_date,
        )
        db.add(row)
    row.row_count = table.num_rows
    row.source = delivery.source
    row.source_raw_sha256 = delivery.source_raw_sha256
    row.source_rows_sha256 = delivery.source_rows_sha256
    row.source_fetched_at = delivery.fetched_at
    row.object_layout = "day"
    row.object_key = key
    row.object_etag = etag
    row.run_id = delivery.run_id
    row.updated_at = utc_now()
    await db.flush()
    return "written"


def _stored_rows(
    reader: CanonicalReader, row: IntradayPartition, kind: str, cache: dict[str, pa.Table]
) -> pa.Table:
    """Rows of one indexed day, taken from the object the index names for it."""
    table = cache.get(row.object_key)
    if table is None:
        limit = MAX_DAY_OBJECT_BYTES if row.object_layout == "day" else MAX_MONTH_OBJECT_BYTES
        stored = reader.read(row.object_key, max_bytes=limit)
        if row.object_layout == "day" and stored.etag != row.object_etag:
            # A newer day was written but not indexed yet; its delivery will retry.
            raise IntradayStorageError("day object is not the indexed version")
        try:
            table = pq.read_table(io.BytesIO(stored.body))
        except (pa.ArrowException, OSError, ValueError):
            raise IntradayStorageError("stored object is not readable") from None
        if not table.schema.equals(row_schema(kind)):
            raise IntradayStorageError("stored object is not in its layout")
        cache[row.object_key] = table
    rows = table.filter(pc.equal(table["trade_date"], pa.scalar(row.trade_date, pa.date32())))
    if rows.num_rows != row.row_count:
        raise IntradayStorageError("stored object does not hold the indexed rows")
    return rows.sort_by(_ROW_KEY[kind])


def read_partitions(reader: CanonicalReader, rows: list[IntradayPartition], kind: str) -> pa.Table:
    """Rows of the given indexed days in (trade date, row key) order."""
    cache: dict[str, pa.Table] = {}
    ordered = sorted(rows, key=lambda row: row.trade_date)
    tables = [_stored_rows(reader, row, kind, cache) for row in ordered]
    return pa.concat_tables(tables) if tables else row_schema(kind).empty_table()


async def merge_month(
    db: AsyncSession,
    publisher: CanonicalPublisher,
    *,
    dataset_key: str,
    symbol: str,
    year: int,
    month: int,
    kind: str,
) -> list[str]:
    """Merge a symbol's indexed days of one month into its month object.

    Returns the day objects the index no longer names; the caller deletes them
    after it commits.  Nothing is written when a stored object disagrees with
    the index.
    """
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    rows = list(
        await db.scalars(
            select(IntradayPartition)
            .where(
                IntradayPartition.dataset_key == dataset_key,
                IntradayPartition.symbol == symbol,
                IntradayPartition.trade_date.between(first, last),
            )
            .order_by(IntradayPartition.trade_date)
            .with_for_update()
        )
    )
    superseded = sorted({row.object_key for row in rows if row.object_layout == "day"})
    if not superseded:
        return []
    merged = await asyncio.to_thread(read_partitions, publisher, rows, kind)
    key = month_key(dataset_key, symbol, year, month)
    indexed = {row.object_etag for row in rows if row.object_layout == "month"}
    expected = indexed.pop() if len(indexed) == 1 else None
    body = await asyncio.to_thread(encode_rows, merged)
    etag = await asyncio.to_thread(_put, publisher, key, body, expected)
    now = utc_now()
    for row in rows:
        row.object_layout = "month"
        row.object_key = key
        row.object_etag = etag
        row.updated_at = now
    await db.flush()
    return superseded


def delete_superseded(publisher: CanonicalPublisher, keys: list[str]) -> int:
    """Remove objects the committed index no longer names; a leftover is harmless."""
    removed = 0
    for key in keys:
        try:
            publisher.delete(key)
            removed += 1
        except ObjectStorageError:
            logger.warning("Superseded canonical object %s was not removed", key)
    return removed
