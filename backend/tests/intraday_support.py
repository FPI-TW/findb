"""Builders for intraday partition tests: rows in the published layout and both buckets."""

from __future__ import annotations

import io
from datetime import date, datetime, timedelta, timezone

import pyarrow as pa
import pyarrow.parquet as pq

from app.services import intraday_partitions as store
from app.services import object_storage as storage
from tests.object_storage_fake import SECRET, FakeR2

ACCOUNT = "a" * 32
RAW_BUCKET = "findb-test-raw"
CANONICAL_BUCKET = "findb-test-canonical"
DAY = date(2026, 7, 21)
FETCHED = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
DATASETS = {"bar_1m": "tw_equity_bar_1m", "tick": "tw_equity_tick"}


def dataset_config(kind: str) -> dict:
    return {
        "schema_id": "intraday_partition",
        "accepted_schema_versions": [1],
        "current_schema_version": 1,
        "schema_enforcement": "enforce",
        "allowed_sources": ["finlab"],
        "defaults": {
            "market": "TW",
            "asset_class": "equity",
            "currency": "TWD",
            "market_timezone": "Asia/Taipei",
            "price_adjustment": "none",
            "record_kind": kind,
        },
    }


def market_open(day: date, minute: int = 0) -> datetime:
    """09:00 Taipei on ``day`` plus ``minute`` minutes, as a UTC instant."""
    return datetime(day.year, day.month, day.day, 1, 0, tzinfo=timezone.utc) + timedelta(
        minutes=minute
    )


def _delivered_schema(kind: str) -> pa.Schema:
    """A producer's file has the layout's names and types; it need not declare nullability."""
    return pa.schema([pa.field(field.name, field.type) for field in store.row_schema(kind)])


def bars(symbol: str = "2330", day: date = DAY, minutes=(0, 1, 2), **columns) -> pa.Table:
    count = len(minutes)
    values = {
        "symbol": [symbol] * count,
        "trade_date": [day] * count,
        "bar_start_time": [market_open(day, minute) for minute in minutes],
        "session": ["regular"] * count,
        "open": [100.0 + minute for minute in minutes],
        "high": [101.0 + minute for minute in minutes],
        "low": [99.5 + minute for minute in minutes],
        "close": [100.5 + minute for minute in minutes],
        "volume": [10 + minute for minute in minutes],
        "trade_count": [3] * count,
        "vwap": [100.25 + minute for minute in minutes],
        **columns,
    }
    return pa.table(values, schema=_delivered_schema("bar_1m"))


def ticks(symbol: str = "2330", day: date = DAY, sequences=(0, 1, 2), **columns) -> pa.Table:
    count = len(sequences)
    values = {
        "symbol": [symbol] * count,
        "trade_date": [day] * count,
        # Several trades may share one instant; the sequence tells them apart.
        "trade_time": [
            market_open(day) + timedelta(seconds=sequence // 2) for sequence in sequences
        ],
        "sequence": list(sequences),
        "price": [100.0 + sequence for sequence in sequences],
        "volume": [1 + sequence for sequence in sequences],
        "tick_type": [1] * count,
        "bid_price": [99.5] * count,
        "bid_volume": [5] * count,
        "ask_price": [100.5] * count,
        "ask_volume": [7] * count,
        "session": ["regular"] * count,
        **columns,
    }
    return pa.table(values, schema=_delivered_schema("tick"))


def parquet_bytes(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    pq.write_table(table, sink)
    return sink.getvalue()


class Stores:
    """Raw and canonical buckets of one environment, as the Worker sees them."""

    def __init__(self) -> None:
        self.r2 = FakeR2()
        self.raw = storage.RawObjectReader(
            storage.R2Access(ACCOUNT, RAW_BUCKET, "reader-id", SECRET), client=self.r2
        )
        self.canonical = storage.CanonicalPublisher(
            storage.R2Access(ACCOUNT, CANONICAL_BUCKET, "publisher-id", SECRET), client=self.r2
        )

    def canonical_objects(self) -> dict[str, bytes]:
        return {
            key: body for (bucket, key), body in self.r2.objects.items() if bucket != RAW_BUCKET
        }

    def writes(self) -> list[str]:
        return [arguments["Key"] for name, arguments in self.r2.calls if name == "put"]

    def rows(self, key: str) -> list[dict]:
        return pq.read_table(io.BytesIO(self.r2.objects[(CANONICAL_BUCKET, key)])).to_pylist()
