"""
Tests for US Stock normalizer.
"""

import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.services.normalize.usstock import (
    CNEquityNormalizer,
    GlobalStockNormalizer,
    HKEquityNormalizer,
    TWEquityNormalizer,
    USIndexNormalizer,
    USStockNormalizer,
)

# Load sample data from the actual file
SAMPLE_DATA_PATH = (
    Path(__file__).parent.parent / "bloomberg_usstock_20260204_160051_api_format.json"
)


@pytest.fixture
def sample_usstock_data():
    """Load sample US Stock data."""
    if SAMPLE_DATA_PATH.exists():
        with open(SAMPLE_DATA_PATH) as f:
            return json.load(f)
    # Fallback minimal test data
    return {
        "metadata": {
            "source": "Bloomberg API",
            "category": "US Stock",
            "query_time": "2026-02-04T16:00:51.164789",
            "total_records": 3,
        },
        "data": [
            {
                "stock_id": "aapl",
                "symbol": "AAPL",
                "name": "APPLE INC",
                "ticker": "AAPL US Equity",
                "price": {
                    "last": 269.48,
                    "open": 269.2,
                    "high": 271.875,
                    "low": 267.61,
                    "volume": 64394655.0,
                },
                "change": {
                    "percent_1d": -0.196289,
                    "percent_5d": 4.340419,
                    "percent_1m": -0.5645584,
                    "percent_ytd": -0.8754453,
                },
                "market_cap": {"value": 3956273.6072, "currency": "USD"},
                "timestamp": {"query_time": "2026-02-04T16:00:51.151296", "last_update": None},
                "metadata": {"source": "Bloomberg", "data_type": "stock"},
            },
            {
                "stock_id": "spx",
                "symbol": "SPX",
                "name": "S&P 500 INDEX",
                "ticker": "SPX Index",
                "price": {
                    "last": 6917.81,
                    "open": 6985.45,
                    "high": 6993.08,
                    "low": 6862.05,
                    "volume": 1178883695.0,
                },
                "change": {
                    "percent_1d": -0.8404,
                    "percent_5d": -0.871093,
                    "percent_1m": 0.8652053,
                    "percent_ytd": 1.056315,
                },
                "market_cap": {"value": 61590639746167.91, "currency": "USD"},
                "timestamp": {"query_time": "2026-02-04T16:00:51.071095", "last_update": None},
                "metadata": {"source": "Bloomberg", "data_type": "stock"},
            },
            {
                "stock_id": "6125",
                "symbol": "6125",
                "name": "KENMEC MECHANICAL ENGINEERIN",
                "ticker": "6125 TT Equity",
                "price": {
                    "last": 67.4,
                    "open": 66.7,
                    "high": 67.6,
                    "low": 66.2,
                    "volume": 894092.0,
                },
                "change": {
                    "percent_1d": 0.8982036,
                    "percent_5d": -10.84656,
                    "percent_1m": -5.46985,
                    "percent_ytd": -6.518722,
                },
                "market_cap": {"value": 16985.55151, "currency": "TWD"},
                "timestamp": {"query_time": "2026-02-04T16:00:50.966719", "last_update": None},
                "metadata": {"source": "Bloomberg", "data_type": "stock"},
            },
        ],
    }


class TestUSStockNormalizer:
    """Tests for USStockNormalizer."""

    def test_map_fields_basic(self, sample_usstock_data):
        """Test basic field mapping for US stocks."""
        db_mock = MagicMock()
        normalizer = USStockNormalizer(db_mock)

        records = normalizer.map_fields(sample_usstock_data)

        assert len(records) > 0

        # Find AAPL record
        aapl_records = [r for r in records if r.symbol == "AAPL"]
        assert len(aapl_records) == 1

        aapl = aapl_records[0]
        assert aapl.symbol == "AAPL"
        assert aapl.name == "APPLE INC"
        assert aapl.identifier_value == "AAPL US Equity"
        assert aapl.identifier_type == "bloomberg"
        assert aapl.source == "bloomberg"
        assert aapl.close == Decimal("269.48")
        assert aapl.open == Decimal("269.2")
        assert aapl.high == Decimal("271.875")
        assert aapl.low == Decimal("267.61")
        assert aapl.volume == 64394655
        assert aapl.trade_date is not None

    def test_map_fields_handles_null_last_update(self, sample_usstock_data):
        """Test that query_time is used when last_update is null."""
        db_mock = MagicMock()
        normalizer = USStockNormalizer(db_mock)

        records = normalizer.map_fields(sample_usstock_data)

        for record in records:
            assert record.trade_date is not None

    def test_map_fields_uppercase_symbol(self, sample_usstock_data):
        """Test that symbols are uppercased."""
        db_mock = MagicMock()
        normalizer = USStockNormalizer(db_mock)

        records = normalizer.map_fields(sample_usstock_data)

        for record in records:
            assert record.symbol == record.symbol.upper()

    def test_map_fields_source_normalization(self, sample_usstock_data):
        """Test that source is normalized to 'bloomberg'."""
        db_mock = MagicMock()
        normalizer = USStockNormalizer(db_mock)

        records = normalizer.map_fields(sample_usstock_data)

        for record in records:
            assert record.source == "bloomberg"

    def test_map_fields_drops_non_us_ticker_suffix(self, sample_usstock_data):
        """Tickers carrying a non-US Bloomberg suffix must not be created as US/equity."""
        db_mock = MagicMock()
        normalizer = USStockNormalizer(db_mock)

        records = normalizer.map_fields(sample_usstock_data)
        tickers = {record.identifier_value for record in records}

        assert "6125 TT Equity" not in tickers
        # AAPL still passes through
        assert "AAPL US Equity" in tickers

    def test_map_fields_drops_bare_numeric_symbol(self):
        """A bare 4-/6-digit numeric symbol must not be admitted as US/equity."""
        payload = {
            "metadata": {"source": "Bloomberg API"},
            "data": [
                {
                    "symbol": "4938",
                    "ticker": "4938 Equity",
                    "name": "PEGATRON CORP",
                    "price": {"last": 76.5, "open": 76.0, "high": 77.0, "low": 75.5, "volume": 1},
                    "timestamp": {"query_time": "2026-05-19T08:00:00", "last_update": None},
                },
                {
                    "symbol": "688322",
                    "ticker": "688322 Equity",
                    "name": "Some CN STAR co",
                    "price": {"last": 99.0, "open": 98.0, "high": 100.0, "low": 97.0, "volume": 1},
                    "timestamp": {"query_time": "2026-05-19T08:00:00", "last_update": None},
                },
                {
                    "symbol": "AAPL",
                    "ticker": "AAPL US Equity",
                    "name": "APPLE INC",
                    "price": {
                        "last": 200.0,
                        "open": 199.0,
                        "high": 201.0,
                        "low": 198.0,
                        "volume": 1,
                    },
                    "timestamp": {"query_time": "2026-05-19T08:00:00", "last_update": None},
                },
            ],
        }
        db_mock = MagicMock()
        normalizer = USStockNormalizer(db_mock)

        records = normalizer.map_fields(payload)
        symbols = {record.symbol for record in records}

        assert symbols == {"AAPL"}

    def test_is_index_recognises_bare_symbol(self):
        """Bare base symbols (e.g. plain 'SPX') must still classify as index."""
        db_mock = MagicMock()
        normalizer = USStockNormalizer(db_mock)

        assert normalizer._is_index(None, "SPX") is True
        assert normalizer._is_index("", "VIX") is True
        assert normalizer._is_index(None, "AAPL") is False


class TestUSIndexNormalizer:
    """Tests for USIndexNormalizer."""

    def test_map_fields_filters_indices(self, sample_usstock_data):
        """Test that only index records are returned."""
        db_mock = MagicMock()
        normalizer = USIndexNormalizer(db_mock)

        records = normalizer.map_fields(sample_usstock_data)

        # Should only contain index records
        for record in records:
            ticker = record.identifier_value
            assert ticker is not None
            assert normalizer._is_index(ticker)

    def test_is_index_detection(self):
        """Test index detection logic."""
        db_mock = MagicMock()
        normalizer = USIndexNormalizer(db_mock)

        # Should be indices
        assert normalizer._is_index("SPX Index") is True
        assert normalizer._is_index("NDX INDEX") is True
        assert normalizer._is_index("INDU INDEX") is True
        assert normalizer._is_index("VIX Index") is True
        assert normalizer._is_index("S5INFT Index") is True

        # Should not be indices
        assert normalizer._is_index("AAPL US Equity") is False
        assert normalizer._is_index("MSFT US Equity") is False
        assert normalizer._is_index(None) is False


class TestGlobalStockNormalizer:
    """Tests for GlobalStockNormalizer."""

    def test_map_fields_excludes_indices(self, sample_usstock_data):
        """Test that index records are excluded."""
        db_mock = MagicMock()
        normalizer = GlobalStockNormalizer(db_mock)

        records = normalizer.map_fields(sample_usstock_data)

        # Should not contain index records
        for record in records:
            ticker = record.identifier_value
            if ticker:
                assert not normalizer._is_index(ticker)

    def test_extract_market_from_ticker(self):
        """Test market extraction from Bloomberg ticker."""
        db_mock = MagicMock()
        normalizer = GlobalStockNormalizer(db_mock)

        # Test various market suffixes
        assert normalizer._extract_market_from_ticker("AAPL US Equity") == "US"
        assert normalizer._extract_market_from_ticker("SIE GY Equity") == "DE"
        assert normalizer._extract_market_from_ticker("700 HK Equity") == "HK"
        assert normalizer._extract_market_from_ticker("6125 TT Equity") == "TW"
        assert normalizer._extract_market_from_ticker("TCS IN Equity") == "IN"
        assert normalizer._extract_market_from_ticker("6506 JP Equity") == "JP"
        assert normalizer._extract_market_from_ticker("688322 CH Equity") == "CN"
        assert normalizer._extract_market_from_ticker("HEXAB SS Equity") == "SE"

    def test_handles_taiwan_stock(self, sample_usstock_data):
        """Test handling of Taiwan stocks."""
        db_mock = MagicMock()
        normalizer = GlobalStockNormalizer(db_mock)

        records = normalizer.map_fields(sample_usstock_data)

        # Find Taiwan stock
        tw_records = [
            r for r in records if r.identifier_value and "TT Equity" in r.identifier_value
        ]

        if tw_records:
            tw_record = tw_records[0]
            assert tw_record.symbol == "6125"
            assert tw_record.name == "KENMEC MECHANICAL ENGINEERIN"
            assert tw_record.market == "TW"

    def test_regional_equity_normalizers_filter_market(self, sample_usstock_data):
        db_mock = MagicMock()

        tw_records = TWEquityNormalizer(db_mock).map_fields(sample_usstock_data)
        hk_records = HKEquityNormalizer(db_mock).map_fields(sample_usstock_data)
        cn_records = CNEquityNormalizer(db_mock).map_fields(sample_usstock_data)

        assert all(record.market == "TW" for record in tw_records)
        assert all(record.market == "HK" for record in hk_records)
        assert all(record.market == "CN" for record in cn_records)


class TestUSStockNormalizerWithRealData:
    """Integration tests with real sample data file."""

    @pytest.mark.skipif(not SAMPLE_DATA_PATH.exists(), reason="Sample data file not found")
    def test_real_data_parsing(self):
        """Test parsing of real sample data file."""
        with open(SAMPLE_DATA_PATH) as f:
            data = json.load(f)

        db_mock = MagicMock()
        normalizer = USStockNormalizer(db_mock)

        records = normalizer.map_fields(data)

        # Should have parsed all 63 records
        assert len(records) == data["metadata"]["total_records"]

        # Check for specific stocks
        symbols = {r.symbol for r in records}
        assert "AAPL" in symbols
        assert "MSFT" in symbols
        assert "NVDA" in symbols
        assert "GOOGL" in symbols
        assert "TSLA" in symbols

    @pytest.mark.skipif(not SAMPLE_DATA_PATH.exists(), reason="Sample data file not found")
    def test_real_data_index_filtering(self):
        """Test that index normalizer properly filters real data."""
        with open(SAMPLE_DATA_PATH) as f:
            data = json.load(f)

        db_mock = MagicMock()
        normalizer = USIndexNormalizer(db_mock)

        records = normalizer.map_fields(data)

        # Should only have index records
        index_symbols = {r.symbol for r in records}

        # Known indices in the sample data
        expected_indices = {"SPX", "NDX", "INDU", "SOX", "RTY", "VIX"}
        assert expected_indices.issubset(index_symbols)

        # Should not have regular stocks
        assert "AAPL" not in index_symbols
        assert "MSFT" not in index_symbols


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
