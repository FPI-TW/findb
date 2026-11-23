"""Serve's reads of canonical intraday objects: only the indexed days, by row group.

The ``intraday_partition`` index names, for each (dataset, symbol, trade date),
the object holding that day and the version the store reported for it.  A read
takes from an object only the days the index assigns to it and fetches only the
row groups that can hold them: a month object is read by byte ranges of the
indexed version, never as a whole.  A day whose rows differ from the index, or
an object that is no longer the indexed version, raises instead of returning
part of a day.
"""

from __future__ import annotations

import io
import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from app.services.intraday_partitions import (
    IntradayStorageError,
    day_row_groups,
    row_schema,
)
from app.services.object_storage import CanonicalReader

# The first request reads this much of the end of an object: a day object and
# most month objects of bars fit entirely, and it always holds a usual footer.
TAIL_BYTES = 256 * 1024
# Bytes one range request may return, and range requests one object may take.
MAX_RANGE_BYTES = 64 * 1024 * 1024
MAX_FETCHES_PER_OBJECT = 8
ROW_KEY = {"bar_1m": "bar_start_time", "tick": "sequence"}


@dataclass(frozen=True, slots=True)
class IndexedDay:
    """One index row as a read needs it."""

    trade_date: date
    row_count: int
    object_key: str
    object_etag: str
    source: str
    source_fetched_at: datetime


class _RangedObject(io.RawIOBase):
    """A read-only file over byte ranges of one object version, fetched on demand."""

    def __init__(self, size: int, fetch: Callable[[int, int], bytes]) -> None:
        super().__init__()
        self._size = size
        self._fetch = fetch
        self._fetches = 0
        self._position = 0
        self._segments: list[tuple[int, bytes]] = []

    def add(self, start: int, body: bytes) -> None:
        self._segments.append((start, body))

    def covered(self, start: int, end: int) -> bool:
        return any(s <= start and end <= s + len(b) for s, b in self._segments)

    def load(self, start: int, end: int) -> None:
        """Make bytes ``[start, end)`` available, in as few requests as allowed."""
        while start < end:
            if self.covered(start, end):
                return
            stop = min(end, start + MAX_RANGE_BYTES)
            self._fetches += 1
            if self._fetches > MAX_FETCHES_PER_OBJECT:
                raise IntradayStorageError("object needs too many range reads")
            body = self._fetch(start, stop - 1)
            if len(body) != stop - start:
                raise IntradayStorageError("object range is shorter than requested")
            self.add(start, body)
            start = stop

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._position, io.SEEK_END: self._size}[whence]
        self._position = max(0, base + offset)
        return self._position

    def readinto(self, buffer: Any) -> int:
        view = memoryview(buffer).cast("B")
        start = self._position
        end = min(self._size, start + len(view))
        if start >= end:
            return 0
        self.load(start, end)
        for segment_start, body in self._segments:
            if segment_start <= start and end <= segment_start + len(body):
                view[: end - start] = body[start - segment_start : end - segment_start]
                self._position = end
                return end - start
        raise IntradayStorageError("object range was not loaded")  # pragma: no cover


def _row_group_span(metadata: pq.FileMetaData, index: int) -> tuple[int, int]:
    group = metadata.row_group(index)
    starts: list[int] = []
    ends: list[int] = []
    for column_index in range(group.num_columns):
        column = group.column(column_index)
        start = column.data_page_offset
        if column.has_dictionary_page and column.dictionary_page_offset is not None:
            start = min(start, column.dictionary_page_offset)
        starts.append(start)
        ends.append(start + column.total_compressed_size)
    return min(starts), max(ends)


def read_object_days(
    reader: CanonicalReader,
    object_key: str,
    object_etag: str,
    days: Sequence[IndexedDay],
    kind: str,
) -> dict[date, pa.Table]:
    """Rows of ``days``, all indexed to one object version, each in row-key order."""

    def fetch(start: int, end: int) -> bytes:
        body, _ = reader.read_range(
            object_key,
            etag=object_etag,
            byte_range=f"bytes={start}-{end}",
            max_bytes=MAX_RANGE_BYTES,
        )
        return body

    tail, size = reader.read_range(
        object_key, etag=object_etag, byte_range=f"bytes=-{TAIL_BYTES}", max_bytes=TAIL_BYTES
    )
    if size < 12 or len(tail) < 8 or tail[-4:] != b"PAR1":
        raise IntradayStorageError("stored object is not a Parquet file")
    source = _RangedObject(size, fetch)
    source.add(size - len(tail), tail)
    footer = int.from_bytes(tail[-8:-4], "little") + 8
    if footer > size:
        raise IntradayStorageError("stored object has an invalid footer")
    source.load(size - footer, size)
    try:
        parquet = pq.ParquetFile(source)
        if not parquet.schema_arrow.equals(row_schema(kind)):
            raise IntradayStorageError("stored object is not in its layout")
        groups = sorted(
            {index for day in days for index in day_row_groups(parquet.metadata, day.trade_date)}
        )
        # Days have neighbouring row groups; one request covers a contiguous run.
        run: tuple[int, int] | None = None
        for index in groups:
            start, end = _row_group_span(parquet.metadata, index)
            if run is not None and start <= run[1]:
                run = (run[0], max(run[1], end))
                continue
            if run is not None:
                source.load(*run)
            run = (start, end)
        if run is not None:
            source.load(*run)
        table = (
            parquet.read_row_groups(groups) if groups else row_schema(kind).empty_table()
        ).combine_chunks()
    except (pa.ArrowException, OSError, ValueError):
        raise IntradayStorageError("stored object is not readable") from None
    result: dict[date, pa.Table] = {}
    for day in days:
        rows = table.filter(pc.equal(table["trade_date"], pa.scalar(day.trade_date, pa.date32())))
        if rows.num_rows != day.row_count:
            raise IntradayStorageError("stored object does not hold the indexed rows")
        result[day.trade_date] = rows.sort_by(ROW_KEY[kind])
    return result


class DayCache:
    """Recently read days, keyed by object version so a replaced day is never served."""

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._bytes = 0
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple[str, str, date], pa.Table] = OrderedDict()

    def get(self, key: tuple[str, str, date]) -> pa.Table | None:
        with self._lock:
            table = self._entries.get(key)
            if table is not None:
                self._entries.move_to_end(key)
            return table

    def put(self, key: tuple[str, str, date], table: pa.Table) -> None:
        size = table.nbytes
        if size > self._max_bytes:
            return
        with self._lock:
            previous = self._entries.pop(key, None)
            if previous is not None:
                self._bytes -= previous.nbytes
            self._entries[key] = table
            self._bytes += size
            while self._bytes > self._max_bytes:
                _, evicted = self._entries.popitem(last=False)
                self._bytes -= evicted.nbytes

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes = 0


def read_days(
    reader: CanonicalReader,
    days: Sequence[IndexedDay],
    kind: str,
    cache: DayCache,
) -> dict[date, pa.Table]:
    """Rows of each indexed day, reading each object version at most once."""
    result: dict[date, pa.Table] = {}
    by_object: dict[tuple[str, str], list[IndexedDay]] = {}
    for day in days:
        cached = cache.get((day.object_key, day.object_etag, day.trade_date))
        if cached is not None:
            result[day.trade_date] = cached
        else:
            by_object.setdefault((day.object_key, day.object_etag), []).append(day)
    for (object_key, object_etag), pending in by_object.items():
        for trade_date, rows in read_object_days(
            reader, object_key, object_etag, pending, kind
        ).items():
            cache.put((object_key, object_etag, trade_date), rows)
            result[trade_date] = rows
    return result
