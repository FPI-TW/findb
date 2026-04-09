"""
Tests for Normalize services.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.canonical import (
    FuturesContinuousEOD,
    Instrument,
    MarketDataEOD,
    TradingCalendar,
)
from app.models.registry import DQIssue, DatasetRegistry, IngestionRun
from app.services.dq.validators import DQValidator
from app.services.normalize.base import BaseNormalizer
from app.services.normalize.crypto import CryptoNormalizer
from app.services.normalize.crypto_index import CryptoIndexNormalizer
from app.services.normalize.equity import IndexNormalizer
from app.services.normalize.futures import FuturesContinuousNormalizer
from app.services.normalize.macro import MacroNormalizer
from app.services.normalize.types import MappedRecord
from app.utils import utc_now, uuid7


class TestCryptoNormalizer:
    """Tests for Crypto Normalizer."""

    def test_map_fields_bloomberg_format(self):
        """Test mapping Bloomberg crypto data format."""
        raw_data = {
            "metadata": {"source": "bloomberg", "fetch_time": "2026-01-16T14:49:14Z"},
            "data": [
                {
                    "ticker": "XBTUSD BGN Curncy",
                    "price": {
                        "open": 95550.07,
                        "high": 95825.34,
                        "low": 95119.76,
                        "last": 95709.01,
                    },
                    "timestamp": {"last_update": "2026-01-16T14:49:14Z"},
                }
            ],
        }

        # Create normalizer without db session for mapping test
        class TestNormalizer(CryptoNormalizer):
            def __init__(self):
                self.dq_validator = DQValidator()

        normalizer = TestNormalizer()
        records = normalizer.map_fields(raw_data)

        assert len(records) == 1
        record = records[0]
        assert record.symbol == "BTC"
        assert record.open == Decimal("95550.07")
        assert record.high == Decimal("95825.34")
        assert record.low == Decimal("95119.76")
        assert record.close == Decimal("95709.01")
        assert record.source == "bloomberg"

    def test_ticker_mapping(self):
        """Test Bloomberg ticker to symbol mapping."""

        class TestNormalizer(CryptoNormalizer):
            def __init__(self):
                pass

        normalizer = TestNormalizer()

        assert normalizer._resolve_symbol("XBTUSD BGN Curncy") == "BTC"
        assert normalizer._resolve_symbol("XETUSD BGN Curncy") == "ETH"
        assert normalizer._resolve_symbol("XRP Curncy") == "XRP"
        assert normalizer._resolve_symbol("XSO Curncy") == "SOL"
        assert normalizer._resolve_symbol("XAD BGN Curncy") == "ADA"
        assert normalizer._resolve_symbol("UNKNOWN") == "UNKNOWN"


class TestCryptoIndexNormalizer:
    """Tests for crypto index normalization."""

    def test_map_fields_bloomberg_index_format(self):
        """Crypto index payload should normalize into CRYPTO/index records."""
        raw_data = {
            "metadata": {"source": "Bloomberg API"},
            "data": [
                {
                    "symbol": "BGCI",
                    "name": "Bloomberg Galaxy Crypto Index",
                    "ticker": "BGCI Index",
                    "price": {
                        "open": 1825.11,
                        "high": 1840.22,
                        "low": 1805.44,
                        "last": 1836.78,
                    },
                    "timestamp": {"last_update": "2026-04-08T02:29:52Z"},
                }
            ],
        }

        class TestNormalizer(CryptoIndexNormalizer):
            def __init__(self):
                self.dq_validator = DQValidator()

        normalizer = TestNormalizer()
        records = normalizer.map_fields(raw_data)

        assert len(records) == 1
        record = records[0]
        assert record.symbol == "BGCI"
        assert record.identifier_value == "BGCI Index"
        assert record.identifier_type == "bloomberg"
        assert record.source == "bloomberg"
        assert record.open == Decimal("1825.11")
        assert record.high == Decimal("1840.22")
        assert record.low == Decimal("1805.44")
        assert record.close == Decimal("1836.78")


class TestLegacyIndexNormalizer:
    """Tests for the legacy us_index_eod normalizer."""

    def test_map_fields_legacy_index_payload(self):
        """Legacy index payload should normalize as an index record."""
        raw_data = {
            "metadata": {"source": "Bloomberg API"},
            "data": [
                {
                    "symbol": "SPX",
                    "name": "S&P 500 INDEX",
                    "ticker": "SPX Index",
                    "price": {
                        "open": 6534.55,
                        "high": 6618.26,
                        "low": 6520.31,
                        "last": 6616.85,
                    },
                    "volume": 1178883695,
                    "timestamp": {"last_update": "2026-04-08T02:29:54Z"},
                }
            ],
        }

        class TestNormalizer(IndexNormalizer):
            def __init__(self):
                self.dq_validator = DQValidator()

        normalizer = TestNormalizer()
        records = normalizer.map_fields(raw_data)

        assert len(records) == 1
        record = records[0]
        assert record.symbol == "SPX"
        assert record.identifier_value == "SPX Index"
        assert record.identifier_type == "bloomberg"
        assert record.source == "bloomberg"
        assert record.open == Decimal("6534.55")
        assert record.high == Decimal("6618.26")
        assert record.low == Decimal("6520.31")
        assert record.close == Decimal("6616.85")
        assert record.volume == 1178883695


class TestMacroNormalizer:
    """Tests for Macro Normalizer."""

    def test_minimal_macro_payload_defaults_market(self):
        """Minimal macro payload should default market to MACRO."""

        class TestNormalizer(MacroNormalizer):
            def __init__(self):
                self.dq_validator = DQValidator()

        normalizer = TestNormalizer()
        records = normalizer.map_fields(
            {
                "metadata": {
                    "source": "Bloomberg",
                    "query_time": "2023-10-27T10:00:00Z",
                },
                "data": [
                    {
                        "ticker": "SOFRRATE Index",
                        "date": "2023-10-26",
                        "value": 5.32,
                    }
                ],
            }
        )

        assert len(records) == 1
        record = records[0]
        assert record.source_code == "SOFRRATE Index"
        assert str(record.obs_date) == "2023-10-26"
        assert record.value == Decimal("5.32")
        assert record.market == "MACRO"
        assert record.source == "bloomberg"


class TestDQValidator:
    """Tests for DQ Validator."""

    def test_ohlc_high_check_pass(self):
        """Test OHLC high check passes for valid data."""
        validator = DQValidator()
        record = MappedRecord(
            symbol="BTC",
            trade_date=datetime.now(timezone.utc),
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("90"),
            close=Decimal("105"),
        )
        issues = validator.validate_eod(record)
        blocking = [i for i in issues if i.issue_type == "OHLC_HIGH_CHECK"]
        assert len(blocking) == 0

    def test_ohlc_high_check_fail(self):
        """Test OHLC high check fails when high < max(open, close)."""
        validator = DQValidator()
        record = MappedRecord(
            symbol="BTC",
            trade_date=datetime.now(timezone.utc),
            open=Decimal("100"),
            high=Decimal("95"),  # Invalid: high < open
            low=Decimal("90"),
            close=Decimal("105"),
        )
        issues = validator.validate_eod(record)
        blocking = [i for i in issues if i.issue_type == "OHLC_HIGH_CHECK"]
        assert len(blocking) == 1
        assert blocking[0].severity == "error"

    def test_ohlc_low_check_fail(self):
        """Test OHLC low check fails when low > min(open, close)."""
        validator = DQValidator()
        record = MappedRecord(
            symbol="BTC",
            trade_date=datetime.now(timezone.utc),
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("105"),  # Invalid: low > open
            close=Decimal("108"),
        )
        issues = validator.validate_eod(record)
        blocking = [i for i in issues if i.issue_type == "OHLC_LOW_CHECK"]
        assert len(blocking) == 1
        assert blocking[0].severity == "error"

    def test_volume_positive_fail(self):
        """Test volume check fails for negative volume."""
        validator = DQValidator()
        record = MappedRecord(
            symbol="BTC",
            trade_date=datetime.now(timezone.utc),
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("90"),
            close=Decimal("105"),
            volume=-100,  # Invalid
        )
        issues = validator.validate_eod(record)
        blocking = [i for i in issues if i.issue_type == "VOLUME_POSITIVE"]
        assert len(blocking) == 1
        assert blocking[0].severity == "error"

    def test_missing_ohlc_warning(self):
        """Test missing OHLC fields generate warning."""
        validator = DQValidator()
        record = MappedRecord(
            symbol="BTC",
            trade_date=datetime.now(timezone.utc),
            open=Decimal("100"),
            high=None,  # Missing
            low=Decimal("90"),
            close=Decimal("105"),
        )
        issues = validator.validate_eod(record)
        warnings = [i for i in issues if i.issue_type == "MISSING_OHLC"]
        assert len(warnings) == 1
        assert warnings[0].severity == "warning"

    def test_abnormal_return_warning(self):
        """Test abnormal return generates warning."""
        validator = DQValidator()
        record = MappedRecord(
            symbol="BTC",
            trade_date=datetime.now(timezone.utc),
            open=Decimal("100"),
            high=Decimal("150"),
            low=Decimal("90"),
            close=Decimal("140"),  # 40% return
        )
        issues = validator.validate_eod(record)
        warnings = [i for i in issues if i.issue_type == "ABNORMAL_RETURN"]
        assert len(warnings) == 1
        assert warnings[0].severity == "warning"

    def test_duplicate_key_error(self):
        """Test duplicate key check detects repeated instrument/date."""
        validator = DQValidator()
        trade_date = datetime.now(timezone.utc)
        seen_keys: set[tuple[str, str, datetime]] = set()

        record_one = MappedRecord(
            symbol="BTC",
            trade_date=trade_date,
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("90"),
            close=Decimal("105"),
        )
        record_two = MappedRecord(
            symbol="BTC",
            trade_date=trade_date,
            open=Decimal("101"),
            high=Decimal("111"),
            low=Decimal("91"),
            close=Decimal("106"),
        )

        first_issues = validator.validate_eod(record_one, seen_keys=seen_keys)
        assert not any(issue.issue_type == "DUPLICATE_KEY" for issue in first_issues)

        second_issues = validator.validate_eod(record_two, seen_keys=seen_keys)
        duplicate_issues = [i for i in second_issues if i.issue_type == "DUPLICATE_KEY"]
        assert len(duplicate_issues) == 1
        assert duplicate_issues[0].severity == "error"

    def test_duplicate_key_allows_same_symbol_different_market(self):
        """Same symbol/date across different markets should not be treated as duplicate."""
        validator = DQValidator()
        trade_date = datetime.now(timezone.utc)
        seen_keys: set[tuple[str, str, datetime]] = set()

        hk_record = MappedRecord(
            symbol="700",
            trade_date=trade_date,
            market="HK",
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("90"),
            close=Decimal("105"),
        )
        tw_record = MappedRecord(
            symbol="700",
            trade_date=trade_date,
            market="TW",
            open=Decimal("101"),
            high=Decimal("111"),
            low=Decimal("91"),
            close=Decimal("106"),
        )

        first_issues = validator.validate_eod(hk_record, seen_keys=seen_keys)
        second_issues = validator.validate_eod(tw_record, seen_keys=seen_keys)

        assert not any(issue.issue_type == "DUPLICATE_KEY" for issue in first_issues)
        assert not any(issue.issue_type == "DUPLICATE_KEY" for issue in second_issues)


@pytest.mark.asyncio
async def test_db_duplicate_key_check(test_session):
    """Test database duplicate check detects existing records."""

    class DummyNormalizer(BaseNormalizer):
        dataset_key = "crypto_eod"
        asset_class = "crypto"
        market = "CRYPTO"

        def map_fields(self, raw_data: dict) -> list[MappedRecord]:
            return []

    normalizer = DummyNormalizer(test_session)
    instrument_id = uuid7()
    trade_date = datetime.now(timezone.utc).date()

    instrument = Instrument(
        instrument_id=instrument_id,
        asset_class="crypto",
        market="CRYPTO",
        symbol="BTC",
        name="Bitcoin",
        status="active",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    test_session.add(instrument)

    eod = MarketDataEOD(
        id=uuid7(),
        instrument_id=instrument_id,
        trade_date=trade_date,
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal("105"),
        asof_ts=utc_now(),
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    test_session.add(eod)
    await test_session.commit()

    assert await normalizer.check_duplicate_in_db(instrument_id, trade_date) is True
    assert (
        await normalizer.check_duplicate_in_db(
            instrument_id, trade_date + timedelta(days=1)
        )
        is False
    )


@pytest.mark.asyncio
async def test_dynamic_market_for_instrument_and_calendar(test_session):
    """Ensure per-record market is used when creating instrument and calendar entries."""

    class DummyNormalizer(BaseNormalizer):
        dataset_key = "global_stock_eod"
        asset_class = "equity"
        market = "GLOBAL"

        def map_fields(self, raw_data: dict) -> list[MappedRecord]:
            return []

    normalizer = DummyNormalizer(test_session)
    trade_dt = datetime(2026, 2, 9, tzinfo=timezone.utc)
    record = MappedRecord(
        symbol="700",
        trade_date=trade_dt,
        market="HK",
        name="TENCENT HOLDINGS LTD",
        identifier_type="bloomberg",
        identifier_value="700 HK Equity",
    )

    instrument = await normalizer.resolve_instrument(record)
    await normalizer.get_or_create_trading_day(record.trade_date, market=record.market)
    await test_session.commit()

    assert instrument.market == "HK"

    cal_stmt = select(TradingCalendar).where(
        TradingCalendar.market == "HK",
        TradingCalendar.trade_date == trade_dt.date(),
    )
    cal_result = await test_session.execute(cal_stmt)
    calendar = cal_result.scalar_one_or_none()
    assert calendar is not None


@pytest.mark.asyncio
async def test_process_upserts_existing_eod_instead_of_failing_duplicate(test_session):
    """Existing DB rows should be updated by upsert, not rejected as duplicate."""

    class DummyNormalizer(BaseNormalizer):
        dataset_key = "crypto_eod"
        asset_class = "crypto"
        market = "CRYPTO"

        def __init__(self, db, records: list[MappedRecord]):
            super().__init__(db)
            self._records = records

        def map_fields(self, raw_data: dict) -> list[MappedRecord]:
            return self._records

    dataset = DatasetRegistry(
        dataset_key="crypto_eod",
        name="Crypto EOD",
        asset_class="crypto",
        market="CRYPTO",
        frequency="daily",
        is_active=True,
        config={},
    )
    run_id = uuid7()
    run = IngestionRun(
        run_id=run_id,
        dataset_key="crypto_eod",
        status="pending",
        raw_records=1,
        created_at=utc_now(),
    )

    instrument_id = uuid7()
    trade_dt = datetime(2026, 2, 9, tzinfo=timezone.utc)
    instrument = Instrument(
        instrument_id=instrument_id,
        asset_class="crypto",
        market="CRYPTO",
        symbol="BTC",
        name="Bitcoin",
        status="active",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    existing = MarketDataEOD(
        id=uuid7(),
        instrument_id=instrument_id,
        trade_date=trade_dt.date(),
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal("105"),
        volume=1000,
        asof_ts=utc_now(),
        run_id=uuid7(),
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    test_session.add_all([dataset, run, instrument, existing])
    await test_session.commit()

    record = MappedRecord(
        symbol="BTC",
        trade_date=trade_dt,
        open=Decimal("200"),
        high=Decimal("220"),
        low=Decimal("190"),
        close=Decimal("210"),
        volume=3000,
        source="bloomberg",
    )
    normalizer = DummyNormalizer(test_session, [record])

    result = await normalizer.process({}, run_id)

    assert result.success_records == 1
    assert result.failed_records == 0

    stmt = select(MarketDataEOD).where(
        MarketDataEOD.instrument_id == instrument_id,
        MarketDataEOD.trade_date == trade_dt.date(),
    )
    updated = (await test_session.execute(stmt)).scalar_one()
    assert updated.open == Decimal("200")
    assert updated.close == Decimal("210")
    assert updated.volume == 3000
    assert updated.run_id == run_id

    dq_stmt = select(DQIssue).where(
        DQIssue.run_id == run_id,
        DQIssue.issue_type == "DUPLICATE_KEY",
    )
    duplicate_issue = (await test_session.execute(dq_stmt)).scalar_one_or_none()
    assert duplicate_issue is None

    persisted_run = await test_session.get(IngestionRun, run_id)
    assert persisted_run is not None
    assert persisted_run.status == "completed"


@pytest.mark.asyncio
async def test_futures_continuous_upserts_existing_eod_instead_of_failing_duplicate(test_session):
    """Continuous futures rows with same key should be updated by upsert."""
    dataset = DatasetRegistry(
        dataset_key="futures_continuous_eod",
        name="Futures Continuous EOD",
        asset_class="future",
        market="WTX",
        frequency="daily",
        is_active=True,
        config={},
    )
    run_id = uuid7()
    run = IngestionRun(
        run_id=run_id,
        dataset_key="futures_continuous_eod",
        status="pending",
        raw_records=1,
        created_at=utc_now(),
    )

    instrument_id = uuid7()
    trade_date = datetime(2026, 2, 9, tzinfo=timezone.utc).date()
    instrument = Instrument(
        instrument_id=instrument_id,
        asset_class="future",
        market="WTX",
        symbol="TX",
        name="TAIEX FUTURES",
        status="active",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    existing = FuturesContinuousEOD(
        id=uuid7(),
        instrument_id=instrument_id,
        trade_date=trade_date,
        open=Decimal("98"),
        high=Decimal("101"),
        low=Decimal("95"),
        close=Decimal("99"),
        volume=900,
        asof_ts=utc_now(),
        run_id=uuid7(),
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    test_session.add_all([dataset, run, instrument, existing])
    await test_session.commit()

    payload = {
        "metadata": {"source": "Bloomberg"},
        "data": [
            {
                "symbol": "TX",
                "name": "TAIEX FUTURES",
                "trade_date": "2026-02-09",
                "open": 100,
                "high": 110,
                "low": 99,
                "close": 108,
                "volume": 1200,
            }
        ],
    }

    normalizer = FuturesContinuousNormalizer(test_session)
    result = await normalizer.process(payload, run_id)

    assert result.success_records == 1
    assert result.failed_records == 0

    stmt = select(FuturesContinuousEOD).where(
        FuturesContinuousEOD.instrument_id == instrument_id,
        FuturesContinuousEOD.trade_date == trade_date,
    )
    updated = (await test_session.execute(stmt)).scalar_one()
    assert updated.open == Decimal("100")
    assert updated.close == Decimal("108")
    assert updated.volume == 1200
    assert updated.run_id == run_id

    dq_stmt = select(DQIssue).where(
        DQIssue.run_id == run_id,
        DQIssue.issue_type == "DUPLICATE_KEY",
    )
    duplicate_issue = (await test_session.execute(dq_stmt)).scalar_one_or_none()
    assert duplicate_issue is None

    persisted_run = await test_session.get(IngestionRun, run_id)
    assert persisted_run is not None
    assert persisted_run.status == "completed"


@pytest.mark.asyncio
async def test_completed_with_errors_sets_completed_at(test_session):
    """Run status should set completed_at for completed_with_errors."""

    class DummyNormalizer(BaseNormalizer):
        dataset_key = "crypto_eod"
        asset_class = "crypto"
        market = "CRYPTO"

        def __init__(self, db, records: list[MappedRecord]):
            super().__init__(db)
            self._records = records

        def map_fields(self, raw_data: dict) -> list[MappedRecord]:
            return self._records

    dataset = DatasetRegistry(
        dataset_key="crypto_eod",
        name="Crypto EOD",
        asset_class="crypto",
        market="CRYPTO",
        frequency="daily",
        is_active=True,
        config={},
    )
    run_id = uuid7()
    run = IngestionRun(
        run_id=run_id,
        dataset_key="crypto_eod",
        status="pending",
        raw_records=1,
        created_at=utc_now(),
    )
    test_session.add_all([dataset, run])
    await test_session.commit()

    invalid_record = MappedRecord(
        symbol="BTC",
        trade_date=datetime(2026, 2, 9, tzinfo=timezone.utc),
        open=Decimal("100"),
        high=Decimal("95"),
        low=Decimal("90"),
        close=Decimal("105"),
        source="bloomberg",
    )
    normalizer = DummyNormalizer(test_session, [invalid_record])

    result = await normalizer.process({}, run_id)

    assert result.success_records == 0
    assert result.failed_records == 1

    persisted_run = await test_session.get(IngestionRun, run_id)
    assert persisted_run is not None
    assert persisted_run.status == "completed_with_errors"
    assert persisted_run.completed_at is not None


@pytest.mark.asyncio
async def test_process_handles_multiple_dates_for_same_symbol(test_session):
    """Repeated dates for the same symbol in one run should not fail after the first insert."""

    class DummyNormalizer(BaseNormalizer):
        dataset_key = "crypto_eod"
        asset_class = "crypto"
        market = "CRYPTO"

        def __init__(self, db, records: list[MappedRecord]):
            super().__init__(db)
            self._records = records

        def map_fields(self, raw_data: dict) -> list[MappedRecord]:
            return self._records

    dataset = DatasetRegistry(
        dataset_key="crypto_eod",
        name="Crypto EOD",
        asset_class="crypto",
        market="CRYPTO",
        frequency="daily",
        is_active=True,
        config={},
    )
    run_id = uuid7()
    run = IngestionRun(
        run_id=run_id,
        dataset_key="crypto_eod",
        status="pending",
        raw_records=2,
        created_at=utc_now(),
    )
    test_session.add_all([dataset, run])
    await test_session.commit()

    records = [
        MappedRecord(
            symbol="BTC",
            trade_date=datetime(2026, 2, 9, tzinfo=timezone.utc),
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("95"),
            close=Decimal("108"),
            volume=1000,
            source="bloomberg",
            raw_data={"symbol": "BTC", "date": "2026-02-09"},
        ),
        MappedRecord(
            symbol="BTC",
            trade_date=datetime(2026, 2, 10, tzinfo=timezone.utc),
            open=Decimal("108"),
            high=Decimal("112"),
            low=Decimal("101"),
            close=Decimal("109"),
            volume=1200,
            source="bloomberg",
            raw_data={"symbol": "BTC", "date": "2026-02-10"},
        ),
    ]
    normalizer = DummyNormalizer(test_session, records)

    result = await normalizer.process({}, run_id)

    assert result.success_records == 2
    assert result.failed_records == 0

    rows = (
        await test_session.execute(select(MarketDataEOD).where(MarketDataEOD.run_id == run_id))
    ).scalars().all()
    assert len(rows) == 2

    persisted_run = await test_session.get(IngestionRun, run_id)
    assert persisted_run is not None
    assert persisted_run.status == "completed"
    assert persisted_run.error_message is None


@pytest.mark.asyncio
async def test_process_persists_processing_errors(test_session):
    """Unexpected record-level exceptions should be stored as DQ issues and summarized on the run."""

    class DummyNormalizer(BaseNormalizer):
        dataset_key = "crypto_eod"
        asset_class = "crypto"
        market = "CRYPTO"

        def __init__(self, db, records: list[MappedRecord]):
            super().__init__(db)
            self._records = records

        def map_fields(self, raw_data: dict) -> list[MappedRecord]:
            return self._records

        async def upsert_eod(self, instrument_id, record, run_id):
            raise RuntimeError("boom")

    dataset = DatasetRegistry(
        dataset_key="crypto_eod",
        name="Crypto EOD",
        asset_class="crypto",
        market="CRYPTO",
        frequency="daily",
        is_active=True,
        config={},
    )
    run_id = uuid7()
    run = IngestionRun(
        run_id=run_id,
        dataset_key="crypto_eod",
        status="pending",
        raw_records=1,
        created_at=utc_now(),
    )
    test_session.add_all([dataset, run])
    await test_session.commit()

    record = MappedRecord(
        symbol="BTC",
        trade_date=datetime(2026, 2, 9, tzinfo=timezone.utc),
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("95"),
        close=Decimal("108"),
        volume=1000,
        source="bloomberg",
        raw_data={"symbol": "BTC", "date": "2026-02-09"},
    )
    normalizer = DummyNormalizer(test_session, [record])

    result = await normalizer.process({}, run_id)

    assert result.success_records == 0
    assert result.failed_records == 1

    dq_issue = (
        await test_session.execute(
            select(DQIssue).where(
                DQIssue.run_id == run_id,
                DQIssue.issue_type == "processing_error",
            )
        )
    ).scalar_one()
    assert dq_issue.description == "RuntimeError: boom"
    assert dq_issue.instrument_id is not None

    persisted_run = await test_session.get(IngestionRun, run_id)
    assert persisted_run is not None
    assert persisted_run.status == "completed_with_errors"
    assert persisted_run.error_message is not None
    assert "RuntimeError: boom" in persisted_run.error_message
