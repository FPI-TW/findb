"""Delivered intraday rows become one canonical object per symbol-day, all or nothing."""

from __future__ import annotations

import hashlib
import io
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from botocore.exceptions import EndpointConnectionError
from sqlalchemy import select, text

from app.models.canonical import IntradayPartition
from app.models.registry import DatasetRegistry, DeliveryMemberEvidence, DQIssue, IngestionRun
from app.services import intraday_partitions as store
from app.services import object_storage as storage
from app.services.ingestion import IngestionService
from app.services.intraday_merge import merge_boundary, merge_finished_months
from app.services.normalize import intraday
from app.services.normalize.intraday import IntradayPartitionContractNormalizer
from tests.intraday_support import (
    ACCOUNT,
    CANONICAL_BUCKET,
    DATASETS,
    DAY,
    FETCHED,
    RAW_BUCKET,
    Stores,
    bars,
    dataset_config,
    market_open,
    parquet_bytes,
    ticks,
)
from tests.object_storage_fake import SECRET


@pytest.fixture
def stores(monkeypatch) -> Stores:
    stores = Stores()
    monkeypatch.setattr(intraday, "raw_reader", lambda: stores.raw)
    monkeypatch.setattr(intraday, "canonical_publisher", lambda: stores.canonical)
    return stores


@pytest.fixture
async def datasets(test_session) -> None:
    for kind, key in DATASETS.items():
        test_session.add(
            DatasetRegistry(
                dataset_key=key,
                name=key,
                asset_class="equity",
                market="TW",
                frequency="minute",
                is_active=True,
                config=dataset_config(kind),
            )
        )
    await test_session.commit()


async def deliver(
    test_session,
    stores: Stores,
    rows: pa.Table | bytes,
    *,
    kind: str = "bar_1m",
    symbol: str = "2330",
    day: date = DAY,
    fetched_at: datetime = FETCHED,
    raw_sha256: str | None = None,
    commit: bool = True,
    **partition,
):
    """Stage the rows object like a producer, then normalize its delivery."""
    body = rows if isinstance(rows, bytes) else parquet_bytes(rows)
    digest = hashlib.sha256(body).hexdigest()
    key = f"finlab/{DATASETS[kind]}/0123456789abcdef/{digest}.rows.parquet"
    stores.r2.objects[(RAW_BUCKET, key)] = body
    payload = {
        "batch": {
            "data_date": day.isoformat(),
            "delivery_mode": "incremental",
            "declared_record_count": 1,
            "source_raw_ref": f"r2://{ACCOUNT}/{RAW_BUCKET}/finlab/original/{digest}.parquet",
            # The provider's own file; one original maps to one rows object here.
            "source_raw_sha256": raw_sha256 or hashlib.sha256(digest.encode()).hexdigest(),
        },
        "data": [
            {
                "symbol": symbol,
                "trade_date": day.isoformat(),
                "record_kind": kind,
                "row_count": 1 if isinstance(rows, bytes) else rows.num_rows,
                "rows_ref": f"r2://{ACCOUNT}/{RAW_BUCKET}/{key}",
                "rows_sha256": digest,
                "rows_bytes": len(body),
                "rows_format": "parquet",
                "price_adjustment": "none",
                "volume_unit": "lot",
                **partition,
            }
        ],
    }
    run = await IngestionService(test_session).create_ingestion_run(
        DATASETS[kind],
        source="finlab",
        raw_records=1,
        schema_id="intraday_partition",
        schema_version=1,
        batch_data_date=day,
        delivery_mode="incremental",
    )
    await test_session.commit()
    run_id = run.run_id
    normalizer = IntradayPartitionContractNormalizer(
        test_session,
        {**dataset_config(kind), "_ingest_source": "finlab", "_ingest_fetched_at": fetched_at},
    )
    result = await normalizer.process(payload, run_id, commit=commit)
    return run_id, result


async def _index(test_session, kind: str = "bar_1m") -> list[IntradayPartition]:
    test_session.expire_all()
    return list(
        await test_session.scalars(
            select(IntradayPartition)
            .where(IntradayPartition.dataset_key == DATASETS[kind])
            .order_by(IntradayPartition.symbol, IntradayPartition.trade_date)
        )
    )


async def _evidence(test_session, run_id) -> set[str]:
    return set(
        await test_session.scalars(
            select(DeliveryMemberEvidence.member_key).where(DeliveryMemberEvidence.run_id == run_id)
        )
    )


@pytest.mark.asyncio
async def test_a_delivered_day_is_stored_indexed_and_counted_as_evidence(
    test_session, stores, datasets
) -> None:
    run_id, result = await deliver(test_session, stores, bars(minutes=(2, 0, 1)))

    assert (result.success_records, result.failed_records) == (1, 0)
    (row,) = await _index(test_session)
    key = "intraday/v1/tw_equity_bar_1m/symbol=2330/year=2026/month=07/day=21/part.parquet"
    assert (row.symbol, row.trade_date, row.row_count) == ("2330", DAY, 3)
    assert (row.object_layout, row.object_key, row.run_id) == ("day", key, run_id)
    assert (row.source, row.source_fetched_at) == ("finlab", FETCHED)
    assert row.object_etag == stores.canonical.version(key)
    # Stored in time order whatever the delivered order, instants in UTC.
    stored = stores.rows(key)
    assert [item["bar_start_time"] for item in stored] == [
        market_open(DAY, minute) for minute in (0, 1, 2)
    ]
    assert stored[0]["symbol"] == "2330" and stored[0]["volume"] == 10
    # The stored file is in the published layout, including which columns may be empty.
    assert pq.read_schema(io.BytesIO(stores.canonical_objects()[key])).equals(
        store.row_schema("bar_1m")
    )
    run = await test_session.get(IngestionRun, run_id)
    assert (run.status, run.success_records) == ("completed", 1)
    assert await _evidence(test_session, run_id) == {"2330"}


@pytest.mark.asyncio
async def test_ticks_are_ordered_by_sequence_and_may_share_an_instant(
    test_session, stores, datasets
) -> None:
    _, result = await deliver(test_session, stores, ticks(sequences=(3, 1, 0, 2)), kind="tick")

    assert result.success_records == 1
    (row,) = await _index(test_session, "tick")
    stored = stores.rows(row.object_key)
    assert [item["sequence"] for item in stored] == [0, 1, 2, 3]
    assert stored[0]["trade_time"] == stored[1]["trade_time"]


@pytest.mark.asyncio
async def test_the_same_data_again_writes_nothing_and_still_proves_its_delivery(
    test_session, stores, datasets
) -> None:
    first, _ = await deliver(test_session, stores, bars())
    written = stores.writes()

    again, result = await deliver(
        test_session, stores, bars(), fetched_at=FETCHED + timedelta(hours=1)
    )

    assert result.success_records == 1
    assert stores.writes() == written
    (row,) = await _index(test_session)
    assert row.run_id == again != first
    assert await _evidence(test_session, again) == {"2330"}


@pytest.mark.asyncio
async def test_a_republished_day_replaces_the_whole_day(test_session, stores, datasets) -> None:
    await deliver(test_session, stores, bars(minutes=(0, 1, 2)))
    (before,) = await _index(test_session)
    before_etag = before.object_etag

    # The revision no longer has minute 2: it must not survive from the old day.
    run_id, result = await deliver(
        test_session, stores, bars(minutes=(0, 1)), fetched_at=FETCHED + timedelta(hours=1)
    )

    assert result.success_records == 1
    (row,) = await _index(test_session)
    assert (row.row_count, row.run_id) == (2, run_id)
    assert row.object_etag != before_etag
    assert len(stores.rows(row.object_key)) == 2
    replace = [arguments for name, arguments in stores.r2.calls if name == "put"][-1]
    assert replace["IfMatch"] == before_etag


@pytest.mark.asyncio
async def test_an_older_fetch_arriving_late_never_replaces_a_newer_one(
    test_session, stores, datasets
) -> None:
    newest, _ = await deliver(test_session, stores, bars(minutes=(0, 1, 2)))
    written = stores.writes()

    late, result = await deliver(
        test_session, stores, bars(minutes=(0, 1)), fetched_at=FETCHED - timedelta(hours=1)
    )

    assert (result.success_records, result.failed_records) == (0, 0)
    assert stores.writes() == written
    (row,) = await _index(test_session)
    assert (row.row_count, row.run_id) == (3, newest)
    # It is recorded as a completed run that wrote nothing, not as delivered data.
    run = await test_session.get(IngestionRun, late)
    assert run.status == "completed"
    assert await _evidence(test_session, late) == set()
    issue = await test_session.scalar(select(DQIssue).where(DQIssue.run_id == late))
    assert (issue.issue_type, issue.severity) == ("INTRADAY_PARTITION_STALE", "warning")


def _retyped(table: pa.Table, column: str, values, type_: pa.DataType) -> pa.Table:
    index = table.schema.get_field_index(column)
    return table.set_column(index, pa.field(column, type_), pa.array(values, type=type_))


LOCAL = pa.timestamp("us", tz="Asia/Taipei")

REJECTED = {
    "not_parquet": lambda: b"not a parquet file",
    "layout: a column is missing": lambda: bars().drop_columns(["vwap"]),
    "layout: columns are reordered": lambda: bars().select(
        ["trade_date", "symbol", *store.row_schema("bar_1m").names[2:]]
    ),
    "layout: instants are not UTC": lambda: _retyped(
        bars(), "bar_start_time", [market_open(DAY, minute) for minute in (0, 1, 2)], LOCAL
    ),
    "layout: the symbol is not a string": lambda: _retyped(
        bars(), "symbol", [2330] * 3, pa.int64()
    ),
    "null_value": lambda: bars(close=[100.5, None, 102.5]),
    "other_symbol": lambda: bars(symbol="0050"),
    "other_trade_date": lambda: bars(trade_date=[DAY, DAY, DAY - timedelta(days=1)]),
    # 00:30 Taipei of the next day is still 2026-07-21 in UTC.
    "instant_outside_trade_date": lambda: bars(
        bar_start_time=[
            market_open(DAY),
            market_open(DAY, 1),
            datetime(2026, 7, 21, 16, 30, tzinfo=timezone.utc),
        ]
    ),
    "duplicate_row_key": lambda: bars(
        bar_start_time=[market_open(DAY), market_open(DAY), market_open(DAY, 2)]
    ),
    "bar_not_minute_aligned": lambda: bars(
        bar_start_time=[
            market_open(DAY),
            market_open(DAY, 1) + timedelta(seconds=30),
            market_open(DAY, 2),
        ]
    ),
    "negative_or_not_finite: volume": lambda: bars(volume=[10, -1, 12]),
    "negative_or_not_finite: price": lambda: bars(high=[101.0, float("nan"), 103.0]),
    "ohlc_bounds": lambda: bars(low=[99.5, 200.0, 101.5]),
    "empty_session": lambda: bars(session=["regular", "", "regular"]),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(REJECTED))
async def test_one_bad_row_rejects_the_day_and_keeps_what_was_stored(
    test_session, stores, datasets, case
) -> None:
    await deliver(test_session, stores, bars(minutes=(0, 1)))
    (before,) = await _index(test_session)
    kept = (before.row_count, before.object_etag, before.run_id)
    written = stores.writes()

    run_id, result = await deliver(
        test_session, stores, REJECTED[case](), fetched_at=FETCHED + timedelta(hours=1)
    )

    assert (result.success_records, result.failed_records) == (0, 1)
    assert stores.writes() == written
    (row,) = await _index(test_session)
    assert (row.row_count, row.object_etag, row.run_id) == kept
    run = await test_session.get(IngestionRun, run_id)
    assert run.status == "completed_with_errors"
    issue = await test_session.scalar(select(DQIssue).where(DQIssue.run_id == run_id))
    assert (issue.issue_type, issue.severity) == ("INTRADAY_PARTITION_REJECTED", "error")
    assert issue.raw_data["rule"] == case.split(":")[0]
    assert await _evidence(test_session, run_id) == set()


@pytest.mark.asyncio
async def test_rows_that_differ_from_their_description_are_rejected(
    test_session, stores, datasets
) -> None:
    cases = {
        "row_count": (bars(), {"row_count": 4}),
        "record_kind": (bars(), {"record_kind": "tick"}),
        "rows_object": (bars(), {"rows_sha256": "0" * 64}),
        "duplicate_row_key": (ticks(sequences=(0, 1, 1)), {}),
    }
    for rule, (rows, description) in cases.items():
        kind = "tick" if rows.schema.names == store.row_schema("tick").names else "bar_1m"
        run_id, result = await deliver(test_session, stores, rows, kind=kind, **description)

        assert result.failed_records == 1, rule
        issue = await test_session.scalar(select(DQIssue).where(DQIssue.run_id == run_id))
        assert issue.raw_data["rule"] == rule
    assert stores.writes() == []
    assert await _index(test_session) == [] and await _index(test_session, "tick") == []


@pytest.mark.asyncio
async def test_a_missing_rows_object_is_a_data_error_not_a_retry(
    test_session, stores, datasets
) -> None:
    body = parquet_bytes(bars())
    digest = hashlib.sha256(body).hexdigest()

    # The reference names an object that was never stored.
    run_id, result = await deliver(
        test_session,
        stores,
        bars(),
        rows_ref=f"r2://{ACCOUNT}/{RAW_BUCKET}/finlab/missing/{digest}.rows.parquet",
    )

    assert result.failed_records == 1
    issue = await test_session.scalar(select(DQIssue).where(DQIssue.run_id == run_id))
    assert issue.raw_data["rule"] == "rows_object"
    assert SECRET not in str(issue.raw_data)


@pytest.mark.asyncio
async def test_a_storage_failure_is_raised_for_retry_and_indexes_nothing(
    test_session, stores, datasets, monkeypatch
) -> None:
    def unavailable(**arguments):
        raise EndpointConnectionError(endpoint_url="https://r2.invalid")

    monkeypatch.setattr(stores.r2, "put_object", unavailable)

    with pytest.raises(storage.ObjectStorageError):
        await deliver(test_session, stores, bars())

    assert await _index(test_session) == []
    assert (await test_session.scalars(select(DQIssue))).all() == []


@pytest.mark.asyncio
async def test_a_day_written_before_a_crash_is_replaced_not_duplicated_on_retry(
    test_session, stores, datasets
) -> None:
    # The object reaches the store, then the transaction is lost before commit.
    await deliver(test_session, stores, bars(), commit=False)
    await test_session.rollback()
    assert await _index(test_session) == []
    assert len(stores.canonical_objects()) == 1

    run_id, result = await deliver(test_session, stores, bars())

    assert result.success_records == 1
    (row,) = await _index(test_session)
    assert (row.row_count, row.run_id) == (3, run_id)
    assert list(stores.canonical_objects()) == [row.object_key]
    assert row.object_etag == stores.canonical.version(row.object_key)
    assert len(stores.rows(row.object_key)) == 3


async def _merge(test_session, stores, *, year=2026, month=7, kind="bar_1m") -> list[str]:
    superseded = await store.merge_month(
        test_session,
        stores.canonical,
        dataset_key=DATASETS[kind],
        symbol="2330",
        year=year,
        month=month,
        kind=kind,
    )
    await test_session.commit()
    store.delete_superseded(stores.canonical, superseded)
    return superseded


DAYS = (date(2026, 7, 20), date(2026, 7, 21), date(2026, 7, 22))
MONTH_KEY = "intraday/v1/tw_equity_bar_1m/symbol=2330/year=2026/month=07/part.parquet"


def _row_group_dates(stores: Stores, key: str) -> list[tuple[date, date]]:
    """(min, max) trade date of each row group of a stored object."""
    metadata = pq.ParquetFile(io.BytesIO(stores.canonical_objects()[key])).metadata
    column = metadata.schema.names.index("trade_date")
    return [
        (stats.min, stats.max)
        for stats in (
            metadata.row_group(index).column(column).statistics
            for index in range(metadata.num_row_groups)
        )
    ]


async def _three_days(test_session, stores) -> None:
    for day in DAYS:
        await deliver(test_session, stores, bars(day=day), day=day)
    # Another month and another symbol stay out of the merge.
    await deliver(test_session, stores, bars(day=date(2026, 8, 3)), day=date(2026, 8, 3))
    await deliver(test_session, stores, bars(symbol="0050"), symbol="0050")


@pytest.mark.asyncio
async def test_a_finished_month_is_merged_into_one_object(test_session, stores, datasets) -> None:
    await _three_days(test_session, stores)

    superseded = await _merge(test_session, stores)

    assert len(superseded) == 3
    july = [
        row for row in await _index(test_session) if row.symbol == "2330" and row.trade_date in DAYS
    ]
    assert {(row.object_layout, row.object_key) for row in july} == {("month", MONTH_KEY)}
    assert {row.object_etag for row in july} == {stores.canonical.version(MONTH_KEY)}
    stored = stores.rows(MONTH_KEY)
    assert [item["trade_date"] for item in stored] == [day for day in DAYS for _ in range(3)]
    # One row group per trade date, so a reader can take a single day of the month.
    assert _row_group_dates(stores, MONTH_KEY) == [(day, day) for day in DAYS]
    assert all(
        store.day_row_groups(
            pq.ParquetFile(io.BytesIO(stores.canonical_objects()[MONTH_KEY])).metadata, day
        )
        == [index]
        for index, day in enumerate(DAYS)
    )
    # The day objects are gone; the other month and symbol were not touched.
    assert sorted(stores.canonical_objects()) == sorted(
        [
            MONTH_KEY,
            store.day_key("tw_equity_bar_1m", "2330", date(2026, 8, 3)),
            store.day_key("tw_equity_bar_1m", "0050", DAY),
        ]
    )
    writes = stores.writes()
    assert await _merge(test_session, stores) == [] and stores.writes() == writes


@pytest.mark.asyncio
async def test_a_day_republished_after_the_merge_is_read_and_merged_from_its_new_object(
    test_session, stores, datasets
) -> None:
    await _three_days(test_session, stores)
    await _merge(test_session, stores)

    await deliver(
        test_session,
        stores,
        bars(day=DAYS[1], minutes=(0,)),
        day=DAYS[1],
        fetched_at=FETCHED + timedelta(days=30),
    )

    july = [
        row for row in await _index(test_session) if row.symbol == "2330" and row.trade_date in DAYS
    ]
    assert [row.object_layout for row in july] == ["month", "day", "month"]
    # The month object still carries the superseded rows; a reader follows the index.
    assert len(stores.rows(MONTH_KEY)) == 9
    read = store.read_partitions(stores.canonical, july, "bar_1m")
    assert [(item["trade_date"]) for item in read.to_pylist()] == [
        DAYS[0],
        DAYS[0],
        DAYS[0],
        DAYS[1],
        DAYS[2],
        DAYS[2],
        DAYS[2],
    ]

    await _merge(test_session, stores)

    assert len(stores.rows(MONTH_KEY)) == 7
    assert _row_group_dates(stores, MONTH_KEY) == [(day, day) for day in DAYS]
    july = [
        row for row in await _index(test_session) if row.symbol == "2330" and row.trade_date in DAYS
    ]
    assert {(row.object_layout, row.object_etag) for row in july} == {
        ("month", stores.canonical.version(MONTH_KEY))
    }
    assert store.day_key("tw_equity_bar_1m", "2330", DAYS[1]) not in stores.canonical_objects()


@pytest.mark.asyncio
async def test_a_merge_stops_when_a_stored_day_is_not_the_indexed_one(
    test_session, stores, datasets
) -> None:
    await _three_days(test_session, stores)
    key = store.day_key("tw_equity_bar_1m", "2330", DAYS[0])
    # A newer day was written but its index row is not committed yet.
    stores.r2.objects[(CANONICAL_BUCKET, key)] = store.encode_rows(bars(day=DAYS[0], minutes=(0,)))
    written = stores.writes()

    with pytest.raises(store.IntradayStorageError):
        await _merge(test_session, stores)
    await test_session.rollback()

    assert stores.writes() == written
    assert {row.object_layout for row in await _index(test_session)} == {"day"}


@pytest.mark.asyncio
async def test_a_merge_lost_before_commit_is_redone_without_losing_a_day(
    test_session, stores, datasets
) -> None:
    await _three_days(test_session, stores)
    # The month object is written, then the transaction is lost: no day object is deleted.
    await store.merge_month(
        test_session,
        stores.canonical,
        dataset_key="tw_equity_bar_1m",
        symbol="2330",
        year=2026,
        month=7,
        kind="bar_1m",
    )
    await test_session.rollback()
    assert MONTH_KEY in stores.canonical_objects()
    assert {row.object_layout for row in await _index(test_session)} == {"day"}

    assert len(await _merge(test_session, stores)) == 3

    assert len(stores.rows(MONTH_KEY)) == 9
    july = [
        row for row in await _index(test_session) if row.symbol == "2330" and row.trade_date in DAYS
    ]
    assert {row.object_etag for row in july} == {stores.canonical.version(MONTH_KEY)}


def _taipei(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 12, 0, tzinfo=ZoneInfo("Asia/Taipei"))


def test_a_month_waits_for_its_grace_days_before_it_is_merged() -> None:
    # July is merged from the day its last trading days are more than 3 days old.
    assert merge_boundary(_taipei(2026, 8, 3), 3) == date(2026, 7, 1)
    assert merge_boundary(_taipei(2026, 8, 4), 3) == date(2026, 8, 1)
    # The boundary follows the exchange calendar, not UTC: 00:30 Taipei on the 4th.
    assert merge_boundary(datetime(2026, 8, 3, 16, 30, tzinfo=timezone.utc), 3) == date(2026, 8, 1)


async def _merge_pass(test_engine, now: datetime) -> int:
    return await merge_finished_months(
        database_url=test_engine.url.render_as_string(hide_password=False), now=now
    )


@pytest.mark.asyncio
async def test_the_worker_merges_finished_months_and_leaves_the_open_month(
    test_session, test_engine, stores, datasets
) -> None:
    await _three_days(test_session, stores)

    # Still inside July's grace days: nothing is merged, nothing is written.
    written = stores.writes()
    assert await _merge_pass(test_engine, _taipei(2026, 8, 3)) == 0
    assert stores.writes() == written

    # July is finished for both symbols; August is the open month.
    assert await _merge_pass(test_engine, _taipei(2026, 8, 4)) == 2

    layouts = {
        (row.symbol, row.trade_date): row.object_layout for row in await _index(test_session)
    }
    assert layouts == {
        ("0050", DAY): "month",
        **{("2330", day): "month" for day in DAYS},
        ("2330", date(2026, 8, 3)): "day",
    }
    assert sorted(stores.canonical_objects()) == sorted(
        [
            MONTH_KEY,
            MONTH_KEY.replace("symbol=2330", "symbol=0050"),
            store.day_key("tw_equity_bar_1m", "2330", date(2026, 8, 3)),
        ]
    )
    written = stores.writes()
    assert await _merge_pass(test_engine, _taipei(2026, 8, 4)) == 0
    assert stores.writes() == written


@pytest.mark.asyncio
async def test_the_merge_yields_to_waiting_deliveries_and_to_a_held_dataset_lock(
    test_session, test_engine, stores, datasets
) -> None:
    await _three_days(test_session, stores)
    written = stores.writes()
    run = await IngestionService(test_session).create_ingestion_run(
        "tw_equity_bar_1m", source="finlab", schema_id="intraday_partition", schema_version=1
    )
    job = await IngestionService(test_session).create_normalization_job(run)
    await test_session.commit()

    assert await _merge_pass(test_engine, _taipei(2026, 8, 4)) == 0

    job.status = "completed"
    await test_session.commit()
    async with test_engine.connect() as holder:
        await holder.execute(text("SELECT pg_advisory_lock(hashtext('tw_equity_bar_1m'))"))
        assert await _merge_pass(test_engine, _taipei(2026, 8, 4)) == 0
        assert stores.writes() == written
        await holder.execute(text("SELECT pg_advisory_unlock(hashtext('tw_equity_bar_1m'))"))

    assert await _merge_pass(test_engine, _taipei(2026, 8, 4)) == 2


@pytest.mark.asyncio
async def test_one_month_that_cannot_be_merged_does_not_block_the_others(
    test_session, test_engine, stores, datasets
) -> None:
    await _three_days(test_session, stores)
    key = store.day_key("tw_equity_bar_1m", "2330", DAYS[0])
    stores.r2.objects[(CANONICAL_BUCKET, key)] = store.encode_rows(bars(day=DAYS[0], minutes=(0,)))

    assert await _merge_pass(test_engine, _taipei(2026, 8, 4)) == 1

    layouts = {
        row.symbol: row.object_layout for row in await _index(test_session) if row.trade_date == DAY
    }
    assert layouts == {"0050": "month", "2330": "day"}


def test_the_merge_loop_survives_a_failed_pass_and_can_be_switched_off(monkeypatch) -> None:
    from app.workers import intraday_merge as worker

    passes: list[int] = []

    async def one_pass() -> int:
        passes.append(len(passes))
        if len(passes) == 1:
            raise RuntimeError("database unavailable")
        if len(passes) == 3:
            worker._stop.set()
        return 1

    monkeypatch.setattr(worker, "merge_finished_months", one_pass)
    worker._stop.clear()
    try:
        worker._merge_loop(0)
    finally:
        worker._stop.clear()
    assert len(passes) == 3

    monkeypatch.setattr(worker.settings, "INTRADAY_MERGE_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(worker, "_thread", None)
    worker.start_intraday_merge()
    assert worker._thread is None


def test_rows_given_out_of_order_are_stored_by_day_then_row_key() -> None:
    later, earlier = date(2026, 7, 22), date(2026, 7, 21)
    table = pa.concat_tables(
        [
            store.row_schema("tick").empty_table(),
            ticks(day=later, sequences=(1, 0)).cast(store.row_schema("tick")),
            ticks(day=earlier, sequences=(2, 0, 1)).cast(store.row_schema("tick")),
        ]
    )

    body = store.encode_rows(table)

    stored = pq.read_table(io.BytesIO(body))
    assert [(row["trade_date"], row["sequence"]) for row in stored.to_pylist()] == [
        (earlier, 0),
        (earlier, 1),
        (earlier, 2),
        (later, 0),
        (later, 1),
    ]
    metadata = pq.ParquetFile(io.BytesIO(body)).metadata
    assert metadata.num_row_groups == 2
    assert store.day_row_groups(metadata, later) == [1]
    assert store.day_row_groups(metadata, date(2026, 7, 23)) == []
    assert store.encode_rows(table) == body


def test_an_object_written_as_one_row_group_is_still_read_for_every_day() -> None:
    """Objects from before days had their own row groups are read whole, never missed."""
    table = pa.concat_tables(
        [bars(day=day).cast(store.row_schema("bar_1m")) for day in (DAY, DAY + timedelta(days=1))]
    )
    sink = io.BytesIO()
    pq.write_table(table, sink)
    metadata = pq.ParquetFile(io.BytesIO(sink.getvalue())).metadata
    assert metadata.num_row_groups == 1
    assert store.day_row_groups(metadata, DAY) == [0]
    assert store.day_row_groups(metadata, DAY + timedelta(days=1)) == [0]
