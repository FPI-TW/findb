from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import CheckConstraint, text
from sqlalchemy.exc import IntegrityError

from app.models.canonical import Instrument, MarketDataMinute
from app.models.registry import (
    TWMinuteArchiveChunk,
    TWMinuteArchiveRelease,
    TWMinuteDailyUpdate,
    TWMinuteDatasetSnapshot,
    TWMinutePublicationRevision,
    TWMinuteSnapshotPart,
    TWMinuteUniverseRelease,
)
from app.utils import uuid7
from scripts.seed_data import seed_datasets


@pytest.mark.asyncio
async def test_minute_partition_and_constraints(test_session):
    default_partition = await test_session.scalar(
        text("SELECT to_regclass('public.market_data_minute_default')")
    )
    assert default_partition == "market_data_minute_default"

    instrument = Instrument(instrument_id=uuid7(), asset_class="equity", market="TW", symbol="2330")
    test_session.add(instrument)
    await test_session.flush()

    start = datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc)
    minute = MarketDataMinute(
        instrument_id=instrument.instrument_id,
        trade_date=date(2026, 7, 30),
        bar_start_time=start,
        bar_end_time=start + timedelta(minutes=1),
        signal_time=start + timedelta(minutes=1),
        market_timezone="Asia/Taipei",
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=10,
        turnover=Decimal("1005"),
        trade_count=None,
        price_adjustment="none",
        source="shioaji",
        source_fetched_at=start + timedelta(minutes=1),
        asof_ts=start + timedelta(minutes=2),
    )
    test_session.add(minute)
    await test_session.flush()

    invalid = MarketDataMinute(
        instrument_id=instrument.instrument_id,
        trade_date=date(2026, 7, 30),
        bar_start_time=start + timedelta(minutes=1),
        bar_end_time=start + timedelta(minutes=3),
        signal_time=start + timedelta(minutes=3),
        market_timezone="Asia/Taipei",
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        price_adjustment="none",
        source="shioaji",
        source_fetched_at=start + timedelta(minutes=2),
        asof_ts=start + timedelta(minutes=3),
    )
    async with test_session.begin_nested():
        test_session.add(invalid)
        with pytest.raises(IntegrityError, match="market_data_minute_one_minute_interval"):
            await test_session.flush()

    invalid_trade_count = MarketDataMinute(
        instrument_id=instrument.instrument_id,
        trade_date=date(2026, 7, 30),
        bar_start_time=start + timedelta(minutes=2),
        bar_end_time=start + timedelta(minutes=3),
        signal_time=start + timedelta(minutes=3),
        market_timezone="Asia/Taipei",
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        trade_count=1,
        price_adjustment="none",
        source="shioaji",
        source_fetched_at=start + timedelta(minutes=2),
        asof_ts=start + timedelta(minutes=3),
    )
    async with test_session.begin_nested():
        test_session.add(invalid_trade_count)
        with pytest.raises(IntegrityError, match="market_data_minute_trade_count_null"):
            await test_session.flush()

    invalid_timezone = MarketDataMinute(
        instrument_id=instrument.instrument_id,
        trade_date=date(2026, 7, 30),
        bar_start_time=start + timedelta(minutes=3),
        bar_end_time=start + timedelta(minutes=4),
        signal_time=start + timedelta(minutes=4),
        market_timezone="UTC",
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        price_adjustment="none",
        source="shioaji",
        source_fetched_at=start + timedelta(minutes=3),
        asof_ts=start + timedelta(minutes=4),
    )
    async with test_session.begin_nested():
        test_session.add(invalid_timezone)
        with pytest.raises(IntegrityError, match="market_data_minute_timezone_taipei"):
            await test_session.flush()

    invalid_trade_date = MarketDataMinute(
        instrument_id=instrument.instrument_id,
        trade_date=date(2026, 7, 29),
        bar_start_time=start + timedelta(minutes=4),
        bar_end_time=start + timedelta(minutes=5),
        signal_time=start + timedelta(minutes=5),
        market_timezone="Asia/Taipei",
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        price_adjustment="none",
        source="shioaji",
        source_fetched_at=start + timedelta(minutes=4),
        asof_ts=start + timedelta(minutes=5),
    )
    async with test_session.begin_nested():
        test_session.add(invalid_trade_date)
        with pytest.raises(IntegrityError, match="market_data_minute_local_trade_date"):
            await test_session.flush()


def test_minute_metadata_keeps_partition_key_in_primary_key():
    table = MarketDataMinute.__table__
    assert tuple(table.primary_key.columns.keys()) == (
        "instrument_id",
        "trade_date",
        "bar_start_time",
    )
    assert table.dialect_options["postgresql"]["partition_by"] == "RANGE (trade_date)"


def test_minute_workflow_metadata_enforces_publication_and_batch_bounds():
    checks = {
        constraint.name: str(constraint.sqltext)
        for table in (
            TWMinuteUniverseRelease.__table__,
            TWMinuteDailyUpdate.__table__,
            TWMinuteDatasetSnapshot.__table__,
            TWMinuteSnapshotPart.__table__,
            TWMinutePublicationRevision.__table__,
            TWMinuteArchiveRelease.__table__,
            TWMinuteArchiveChunk.__table__,
        )
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name
    }

    assert (
        "skipped_calendar"
        in checks["ck_tw_minute_daily_update_tw_minute_daily_update_status_valid"]
    )
    assert (
        "= 20" in checks["ck_tw_minute_universe_release_tw_minute_universe_count_threshold_fixed"]
    )
    assert (
        "change_ratio <= 0.02"
        in checks["ck_tw_minute_universe_release_tw_minute_universe_published_within_thresholds"]
    )
    assert ">= 1" in checks["ck_tw_minute_dataset_snapshot_tw_minute_snapshot_counts_nonnegative"]
    assert ">= 1" in checks["ck_tw_minute_snapshot_part_tw_minute_snapshot_part_sequence_positive"]
    assert (
        "unresolved_gap"
        in checks["ck_tw_minute_publication_revision_tw_minute_publication_latest_resolved"]
    )
    assert (
        "expected_no_data"
        in checks["ck_tw_minute_publication_revision_tw_minute_publication_warning_kind_coherent"]
    )
    assert "dataset_key" in TWMinuteArchiveRelease.__table__.columns
    assert {
        "upstream_source",
        "overlap_precedence",
        "trading_calendar_checksum",
    }.issubset(TWMinuteArchiveRelease.__table__.columns.keys())
    assert "shioaji_eligibility_snapshot" in TWMinuteUniverseRelease.__table__.columns
    assert "official_membership_snapshot" in TWMinuteUniverseRelease.__table__.columns
    assert {
        "snapshot_sequence",
        "chunk_sequence",
        "chunk_count",
        "chunk_checksum",
        "instrument_checksum",
        "object_key",
        "object_checksum",
        "object_size_bytes",
    }.issubset(set(TWMinuteArchiveChunk.__table__.columns.keys()))
    assert "<= 5000" in checks["ck_tw_minute_archive_chunk_tw_minute_archive_chunk_row_count_range"]


@pytest.mark.asyncio
async def test_universe_thresholds_and_latest_warning_kind_are_fail_closed(test_session):
    await seed_datasets(test_session)
    now = datetime(2026, 7, 30, 5, 0, tzinfo=timezone.utc)

    over_threshold = TWMinuteUniverseRelease(
        dataset_key="tw_equity_minute",
        effective_date=date(2026, 7, 30),
        version=1,
        status="published",
        shioaji_eligibility_snapshot={"source": "shioaji", "eligibility": ["2330"]},
        shioaji_eligibility_checksum="a" * 64,
        official_membership_snapshot={
            "source": "twse",
            "membership": ["2330"],
            "classification": {"kind": "equity"},
        },
        official_membership_checksum="b" * 64,
        member_checksum="c" * 64,
        member_count=10,
        change_count=21,
        change_ratio=Decimal("0.01"),
        change_count_threshold=20,
        change_ratio_threshold=Decimal("0.02"),
        change_audit={"decision": "blocked", "differences": ["exceeded_count_threshold"]},
        published_at=now,
    )
    async with test_session.begin_nested():
        test_session.add(over_threshold)
        with pytest.raises(IntegrityError, match="CheckViolationError"):
            await test_session.flush()

    over_ratio = TWMinuteUniverseRelease(
        dataset_key="tw_equity_minute",
        effective_date=date(2026, 7, 30),
        version=1,
        status="published",
        shioaji_eligibility_snapshot={"source": "shioaji", "eligibility": ["2330"]},
        shioaji_eligibility_checksum="a" * 64,
        official_membership_snapshot={
            "source": "twse",
            "membership": ["2330"],
            "classification": {"kind": "equity"},
        },
        official_membership_checksum="b" * 64,
        member_checksum="c" * 64,
        member_count=10,
        change_count=1,
        change_ratio=Decimal("0.03"),
        change_count_threshold=20,
        change_ratio_threshold=Decimal("0.02"),
        change_audit={"decision": "blocked", "differences": ["exceeded_ratio_threshold"]},
        published_at=now,
    )
    async with test_session.begin_nested():
        test_session.add(over_ratio)
        with pytest.raises(IntegrityError, match="CheckViolationError"):
            await test_session.flush()

    missing_shioaji_evidence = TWMinuteUniverseRelease(
        dataset_key="tw_equity_minute",
        effective_date=date(2026, 7, 30),
        version=1,
        status="published",
        official_membership_snapshot={
            "source": "twse",
            "membership": ["2330"],
            "classification": {"kind": "equity"},
        },
        official_membership_checksum="d" * 64,
        member_checksum="e" * 64,
        member_count=10,
        change_count=1,
        change_ratio=Decimal("0.01"),
        change_count_threshold=20,
        change_ratio_threshold=Decimal("0.02"),
        published_at=now,
    )
    async with test_session.begin_nested():
        test_session.add(missing_shioaji_evidence)
        with pytest.raises(IntegrityError, match="CheckViolationError"):
            await test_session.flush()

    missing_official_evidence = TWMinuteUniverseRelease(
        dataset_key="tw_equity_minute",
        effective_date=date(2026, 7, 30),
        version=1,
        status="published",
        shioaji_eligibility_snapshot={"source": "shioaji", "eligibility": ["2330"]},
        shioaji_eligibility_checksum="f" * 64,
        member_checksum="a" * 64,
        member_count=10,
        change_count=1,
        change_ratio=Decimal("0.01"),
        change_count_threshold=20,
        change_ratio_threshold=Decimal("0.02"),
        published_at=now,
    )
    async with test_session.begin_nested():
        test_session.add(missing_official_evidence)
        with pytest.raises(IntegrityError, match="CheckViolationError"):
            await test_session.flush()

    raised_threshold = TWMinuteUniverseRelease(
        dataset_key="tw_equity_minute",
        effective_date=date(2026, 7, 30),
        version=1,
        status="candidate",
        member_checksum="a" * 64,
        member_count=10,
        change_count=1,
        change_ratio=Decimal("0.01"),
        change_count_threshold=21,
        change_ratio_threshold=Decimal("0.02"),
    )
    async with test_session.begin_nested():
        test_session.add(raised_threshold)
        with pytest.raises(IntegrityError, match="CheckViolationError"):
            await test_session.flush()

    empty_evidence = TWMinuteUniverseRelease(
        dataset_key="tw_equity_minute",
        effective_date=date(2026, 7, 30),
        version=1,
        status="published",
        shioaji_eligibility_snapshot={"source": "shioaji", "eligibility": []},
        shioaji_eligibility_checksum="1" * 64,
        official_membership_snapshot={
            "source": "twse",
            "membership": ["2330"],
            "classification": {"kind": "equity"},
        },
        official_membership_checksum="2" * 64,
        member_checksum="3" * 64,
        member_count=10,
        change_count=1,
        change_ratio=Decimal("0.01"),
        change_count_threshold=20,
        change_ratio_threshold=Decimal("0.02"),
        change_audit={"decision": "published", "differences": []},
        published_at=now,
    )
    async with test_session.begin_nested():
        test_session.add(empty_evidence)
        with pytest.raises(IntegrityError, match="CheckViolationError"):
            await test_session.flush()

    published_release = TWMinuteUniverseRelease(
        dataset_key="tw_equity_minute",
        effective_date=date(2026, 7, 30),
        version=1,
        status="candidate",
        shioaji_eligibility_snapshot={"source": "shioaji", "eligibility": ["2330"]},
        shioaji_eligibility_checksum="1" * 64,
        official_membership_snapshot={
            "source": "tpex",
            "membership": ["2330"],
            "classification": {"kind": "equity"},
        },
        official_membership_checksum="2" * 64,
        member_checksum="3" * 64,
        member_count=10,
        change_count=1,
        change_ratio=Decimal("0.01"),
        change_count_threshold=20,
        change_ratio_threshold=Decimal("0.02"),
        change_audit={"decision": "published", "differences": []},
        published_at=None,
    )
    test_session.add(published_release)
    await test_session.flush()

    daily_update = TWMinuteDailyUpdate(
        update_id=uuid7(),
        environment="test",
        market="TW",
        trade_date=date(2026, 7, 30),
        status="completed",
    )
    test_session.add(daily_update)
    await test_session.flush()

    test_session.add(
        TWMinuteDatasetSnapshot(
            daily_update_id=daily_update.update_id,
            dataset_key="tw_equity_minute",
            trade_date=date(2026, 7, 30),
            universe_release_id=published_release.release_id,
            symbols_checksum="a" * 64,
            sequence_count=1,
            expected_rows=0,
            received_rows=0,
            status="completed",
            symbol_results={"2330": {"outcome": "expected_no_data"}},
        )
    )
    await test_session.flush()

    invalid_warning = TWMinutePublicationRevision(
        daily_update_id=daily_update.update_id,
        revision=1,
        status="completed_with_warnings",
        manifest={"expected_no_data_symbols": ["2330"]},
        manifest_checksum="c" * 64,
        warning_kind="unexpected_gap",
        is_latest=True,
        published_at=now,
    )
    async with test_session.begin_nested():
        test_session.add(invalid_warning)
        with pytest.raises(IntegrityError, match="CheckViolationError"):
            await test_session.flush()

    null_warning = TWMinutePublicationRevision(
        daily_update_id=daily_update.update_id,
        revision=1,
        status="completed_with_warnings",
        manifest={"expected_no_data_symbols": ["2330"]},
        manifest_checksum="d" * 64,
        warning_kind=None,
        is_latest=False,
    )
    async with test_session.begin_nested():
        test_session.add(null_warning)
        with pytest.raises(IntegrityError, match="CheckViolationError"):
            await test_session.flush()

    missing_warning_evidence = TWMinutePublicationRevision(
        daily_update_id=daily_update.update_id,
        revision=1,
        status="completed_with_warnings",
        manifest={},
        manifest_checksum="e" * 64,
        warning_kind="expected_no_data",
        is_latest=True,
        published_at=now,
    )
    async with test_session.begin_nested():
        test_session.add(missing_warning_evidence)
        with pytest.raises(IntegrityError, match="CheckViolationError"):
            await test_session.flush()

    expected_no_data_warning = TWMinutePublicationRevision(
        daily_update_id=daily_update.update_id,
        revision=1,
        status="completed_with_warnings",
        manifest={"expected_no_data_symbols": ["2330"]},
        manifest_checksum="f" * 64,
        warning_kind="expected_no_data",
        is_latest=True,
        published_at=now,
    )
    test_session.add(expected_no_data_warning)
    await test_session.flush()

    invalid_archive = TWMinuteArchiveRelease(
        release_key="tw-2026-07-invalid",
        dataset_key="tw_equity_minute",
        source="tw_recorder_archive",
        upstream_source="shioaji",
        overlap_precedence="direct_daily_shioaji",
        trading_calendar_checksum="not-a-checksum",
        instruments_checksum="a" * 64,
        trading_dates_checksum="b" * 64,
        expected_trading_date_count=1,
        covered_trading_date_count=1,
        chunk_count=1,
        coverage_start=date(2026, 7, 1),
        coverage_end=date(2026, 7, 31),
        instrument_count=1,
        row_count=1,
        sequence_count=1,
        manifest_checksum="f" * 64,
        content_checksum="c" * 64,
        manifest={},
        status="staged",
    )
    async with test_session.begin_nested():
        test_session.add(invalid_archive)
        with pytest.raises(IntegrityError, match="CheckViolationError"):
            await test_session.flush()

    archive = TWMinuteArchiveRelease(
        release_key="tw-2026-07-staged",
        dataset_key="tw_equity_minute",
        source="tw_recorder_archive",
        upstream_source="shioaji",
        overlap_precedence="direct_daily_shioaji",
        trading_calendar_checksum="a" * 64,
        instruments_checksum="a" * 64,
        trading_dates_checksum="b" * 64,
        expected_trading_date_count=1,
        covered_trading_date_count=1,
        chunk_count=1,
        coverage_start=date(2026, 7, 1),
        coverage_end=date(2026, 7, 31),
        instrument_count=1,
        row_count=1,
        sequence_count=1,
        manifest_checksum="b" * 64,
        content_checksum="c" * 64,
        manifest={},
        status="staged",
    )
    test_session.add(archive)
    await test_session.flush()


@pytest.mark.asyncio
async def test_minute_datasets_are_seeded_active_for_reviewed_pilot_with_governance(test_session):
    await seed_datasets(test_session)
    rows = (
        (
            await test_session.execute(
                text(
                    "SELECT dataset_key, is_active, config FROM dataset_registry WHERE dataset_key LIKE 'tw_%_minute'"
                )
            )
        )
        .mappings()
        .all()
    )

    assert {row["dataset_key"] for row in rows} == {"tw_equity_minute", "tw_etf_minute"}
    for row in rows:
        assert row["is_active"] is True
        assert row["config"]["schema_id"] == "market_minute"
        assert "universe_symbol_limit" not in row["config"]["governance"]
        assert row["config"]["governance"]["sequence_symbol_limit"] == 50
        assert row["config"]["governance"]["sequence_row_limit"] == 15000
        assert row["config"]["governance"]["request_rate_limit"] == {
            "requests": 50,
            "window_seconds": 60,
        }
        assert row["config"]["governance"]["request_max_attempts"] == 3
        assert row["config"]["governance"]["universe_change_limit"] == {
            "absolute": 20,
            "ratio": 0.02,
            "fail_when_either_exceeded": True,
        }
        assert row["config"]["governance"]["schedule"]["acquisition_start"] == "14:30:00"
