"""
Tests for Normalize services.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.models.canonical import Instrument, MarketDataEOD
from app.services.dq.validators import DQValidator
from app.services.normalize.base import BaseNormalizer
from app.services.normalize.crypto import CryptoNormalizer
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
        seen_keys: set[tuple[str, datetime]] = set()

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
