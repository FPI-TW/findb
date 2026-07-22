"""
Tests for Bloomberg direct format normalizers: FX, CRYPTO, WTX, MACRO.
"""

from decimal import Decimal

from app.services.dq.validators import DQValidator
from app.services.ingestion import _select_normalizer_for_payload
from app.services.normalize.crypto import CryptoBloombergNormalizer
from app.services.normalize.finlab import WTXFinlabNormalizer
from app.services.normalize.futures import WTXBloombergNormalizer
from app.services.normalize.fx import FXBloombergNormalizer
from app.services.normalize.macro import MacroBloombergNormalizer


def _make_normalizer(cls):
    """Create normalizer instance without DB for map_fields unit tests."""

    class _N(cls):
        def __init__(self):
            self.dq_validator = DQValidator()
            self.dataset_config = {}

    return _N()


# ---------------------------------------------------------------------------
# FXBloombergNormalizer
# ---------------------------------------------------------------------------


class TestFXBloombergNormalizer:
    def _n(self):
        return _make_normalizer(FXBloombergNormalizer)

    def _payload(self, **overrides):
        item = {
            "pair": "EURUSD",
            "symbol": "EURUSD",
            "ticker": "EURUSD Curncy",
            "price": {"last": 1.0523, "open": 1.0498, "high": 1.0567, "low": 1.0489},
            "timestamp": {"query_time": "2026-03-12T10:00:00", "last_update": "2026-03-12"},
            "metadata": {"source": "Bloomberg"},
        }
        item.update(overrides)
        return {"metadata": {"source": "Bloomberg API", "category": "FX"}, "data": [item]}

    def test_basic_mapping(self):
        records = self._n().map_fields(self._payload())
        assert len(records) == 1
        r = records[0]
        assert r.symbol == "EURUSD"
        assert r.asset_class == "fx"
        assert r.close == Decimal("1.0523")
        assert r.open == Decimal("1.0498")
        assert r.high == Decimal("1.0567")
        assert r.low == Decimal("1.0489")
        assert r.source == "bloomberg"
        assert r.identifier_value == "EURUSD Curncy"
        assert r.identifier_type == "bloomberg"

    def test_symbol_from_pair_when_no_symbol(self):
        payload = self._payload()
        del payload["data"][0]["symbol"]
        records = self._n().map_fields(payload)
        assert records[0].symbol == "EURUSD"

    def test_symbol_derived_from_ticker(self):
        payload = self._payload()
        del payload["data"][0]["symbol"]
        del payload["data"][0]["pair"]
        records = self._n().map_fields(payload)
        assert records[0].symbol == "EURUSD"

    def test_fallback_to_query_time_when_no_last_update(self):
        payload = self._payload()
        payload["data"][0]["timestamp"] = {"query_time": "2026-03-12T10:00:00"}
        records = self._n().map_fields(payload)
        assert len(records) == 1

    def test_skip_record_without_date(self):
        payload = self._payload()
        payload["data"][0]["timestamp"] = {}
        records = self._n().map_fields(payload)
        assert len(records) == 0

    def test_skip_record_without_symbol_or_ticker(self):
        payload = self._payload()
        del payload["data"][0]["symbol"]
        del payload["data"][0]["pair"]
        del payload["data"][0]["ticker"]
        records = self._n().map_fields(payload)
        assert len(records) == 0

    def test_flat_price_fields(self):
        payload = self._payload()
        del payload["data"][0]["price"]
        payload["data"][0].update({"open": 1.04, "high": 1.06, "low": 1.03, "close": 1.05})
        records = self._n().map_fields(payload)
        assert records[0].close == Decimal("1.05")

    def test_source_from_global_metadata(self):
        payload = self._payload()
        del payload["data"][0]["metadata"]
        records = self._n().map_fields(payload)
        assert records[0].source == "bloomberg"

    def test_dataset_key(self):
        assert FXBloombergNormalizer.dataset_key == "fx_bloomberg_eod"
        assert FXBloombergNormalizer.market == "FX"
        assert FXBloombergNormalizer.asset_class == "fx"


# ---------------------------------------------------------------------------
# CryptoBloombergNormalizer
# ---------------------------------------------------------------------------


class TestCryptoBloombergNormalizer:
    def _n(self):
        return _make_normalizer(CryptoBloombergNormalizer)

    def _payload(self, ticker="XBTUSD BGN Curncy", symbol="BTC", **overrides):
        item = {
            "symbol": symbol,
            "name": "Bitcoin",
            "ticker": ticker,
            "price": {"last": 95709.01, "open": 95550.07, "high": 95825.34, "low": 95119.76},
            "timestamp": {"query_time": "2026-01-16T14:49:14", "last_update": "2026-01-16"},
            "metadata": {"source": "Bloomberg"},
        }
        item.update(overrides)
        return {
            "metadata": {"source": "Bloomberg API", "category": "Cryptocurrency"},
            "data": [item],
        }

    def test_basic_mapping(self):
        records = self._n().map_fields(self._payload())
        assert len(records) == 1
        r = records[0]
        assert r.symbol == "BTC"
        assert r.asset_class == "crypto"
        assert r.close == Decimal("95709.01")
        assert r.source == "bloomberg"

    def test_ticker_map_resolution(self):
        cases = [
            ("XBTUSD BGN Curncy", "BTC"),
            ("XETUSD BGN Curncy", "ETH"),
            ("XRP Curncy", "XRP"),
            ("XSO Curncy", "SOL"),
            ("XAD BGN Curncy", "ADA"),
        ]
        n = self._n()
        for ticker, expected_symbol in cases:
            records = n.map_fields(self._payload(ticker=ticker, symbol=ticker.split()[0]))
            assert (
                records[0].symbol == expected_symbol
            ), f"Ticker {ticker} should map to {expected_symbol}"

    def test_name_from_name_map_when_missing(self):
        payload = self._payload()
        del payload["data"][0]["name"]
        records = self._n().map_fields(payload)
        assert records[0].name == "Bitcoin"

    def test_unknown_ticker_keeps_symbol(self):
        records = self._n().map_fields(self._payload(ticker="DOGUSD Curncy", symbol="DOGE"))
        assert records[0].symbol == "DOGE"

    def test_original_flat_payload(self):
        payload = {
            "metadata": {"source": "bloomberg", "query_time": "2026-01-16T14:49:14Z"},
            "data": [
                {
                    "ticker": "XBTUSD BGN Curncy",
                    "date": "2026-01-16",
                    "open": 95550.07,
                    "high": 95825.34,
                    "low": 95119.76,
                    "close": 95709.01,
                    "volume": 18500,
                }
            ],
        }
        records = self._n().map_fields(payload)
        assert len(records) == 1
        assert records[0].symbol == "BTC"
        assert records[0].close == Decimal("95709.01")

    def test_dataset_key(self):
        assert CryptoBloombergNormalizer.dataset_key == "crypto_bloomberg_eod"
        assert CryptoBloombergNormalizer.market == "CRYPTO"
        assert CryptoBloombergNormalizer.asset_class == "crypto"


# ---------------------------------------------------------------------------
# WTXBloombergNormalizer
# ---------------------------------------------------------------------------


class TestWTXBloombergNormalizer:
    def _n(self):
        return _make_normalizer(WTXBloombergNormalizer)

    def _payload(self, **overrides):
        item = {
            "symbol": "TXF1",
            "name": "TAIFEX TX Futures 1",
            "ticker": "TXF1 Index",
            "price": {
                "last": 21000.0,
                "open": 20800.0,
                "high": 21100.0,
                "low": 20700.0,
                "volume": 50000,
            },
            "timestamp": {"query_time": "2026-03-12T10:00:00", "last_update": "2026-03-12"},
            "metadata": {"source": "Bloomberg"},
        }
        item.update(overrides)
        return {"metadata": {"source": "Bloomberg API", "category": "Futures"}, "data": [item]}

    def test_basic_mapping(self):
        records = self._n().map_fields(self._payload())
        assert len(records) == 1
        r = records[0]
        assert r.symbol == "TXF1"
        assert r.close == Decimal("21000.0")
        assert r.open == Decimal("20800.0")
        assert r.high == Decimal("21100.0")
        assert r.low == Decimal("20700.0")
        assert r.volume == 50000
        assert r.source == "bloomberg"
        assert r.identifier_value == "TXF1 Index"
        assert r.identifier_type == "bloomberg"

    def test_roll_rule_fields_are_none(self):
        records = self._n().map_fields(self._payload())
        r = records[0]
        assert r.roll_rule_name is None
        assert r.roll_rule_description is None
        assert r.roll_rule_config is None

    def test_maps_only_explicit_contract_fields(self):
        payload = self._payload(
            open_interest=12345,
            active_contract_code="TXF202603",
            roll_adjustment="1.75",
        )
        record = self._n().map_fields(payload)[0]
        assert record.open_interest == 12345
        assert record.active_contract_code == "TXF202603"
        assert record.roll_adjustment == Decimal("1.75")

        implicit = self._n().map_fields(self._payload())[0]
        assert implicit.active_contract_code is None
        assert implicit.open_interest is None
        assert implicit.roll_adjustment is None

    def test_symbol_derived_from_ticker(self):
        payload = self._payload()
        del payload["data"][0]["symbol"]
        records = self._n().map_fields(payload)
        assert records[0].symbol == "TXF1"

    def test_fallback_to_query_time(self):
        payload = self._payload()
        payload["data"][0]["timestamp"] = {"query_time": "2026-03-12T10:00:00"}
        records = self._n().map_fields(payload)
        assert len(records) == 1

    def test_flat_price_fields(self):
        payload = self._payload()
        del payload["data"][0]["price"]
        payload["data"][0].update(
            {"open": 20800.0, "high": 21100.0, "low": 20700.0, "close": 21000.0}
        )
        records = self._n().map_fields(payload)
        assert records[0].close == Decimal("21000.0")

    def test_skip_record_without_date(self):
        payload = self._payload()
        payload["data"][0]["timestamp"] = {}
        records = self._n().map_fields(payload)
        assert len(records) == 0

    def test_dataset_key(self):
        assert WTXBloombergNormalizer.dataset_key == "wtx_eod"
        assert WTXBloombergNormalizer.market == "WTX"
        assert WTXBloombergNormalizer.asset_class == "future"

    def test_wtx_payload_routing_normalizes_bloomberg_source_labels(self):
        for source in ("bloomberg", "Bloomberg API", "Bloomberg Direct"):
            normalizer_cls = _select_normalizer_for_payload(
                "wtx_eod",
                {"metadata": {"source": source}},
            )
            assert normalizer_cls is WTXBloombergNormalizer

    def test_wtx_payload_routing_keeps_finlab_and_unknown_default(self):
        assert (
            _select_normalizer_for_payload("wtx_eod", {"metadata": {"source": "finlab"}})
            is WTXFinlabNormalizer
        )
        assert (
            _select_normalizer_for_payload("wtx_eod", {"metadata": {"source": "unknown"}})
            is WTXFinlabNormalizer
        )


class TestWTXFinlabNormalizer:
    def _n(self):
        return _make_normalizer(WTXFinlabNormalizer)

    def test_maps_explicit_fields_without_deriving_active_contract(self):
        payload = {
            "metadata": {"source": "finlab", "symbol": "WTX"},
            "data": [
                {
                    "date": "2026-03-12",
                    "contract_month": "202603",
                    "close": 21000,
                    "open_interest": 45678,
                    "roll_adjustment": "2.25",
                }
            ],
        }
        record = self._n().map_fields(payload)[0]
        assert record.open_interest == 45678
        assert record.roll_adjustment == Decimal("2.25")
        assert record.active_contract_code is None
        assert record.roll_rule_name == "202603"

        payload["data"][0]["active_contract_code"] = "TXF202603"
        explicit = self._n().map_fields(payload)[0]
        assert explicit.active_contract_code == "TXF202603"


# ---------------------------------------------------------------------------
# MacroBloombergNormalizer
# ---------------------------------------------------------------------------


class TestMacroBloombergNormalizer:
    def _n(self):
        return _make_normalizer(MacroBloombergNormalizer)

    def _payload(self, **overrides):
        item = {
            "ticker": "CPI YOY Index",
            "name": "US CPI YoY",
            "price": {"last": 2.9},
            "timestamp": {"query_time": "2026-02-01T10:00:00", "last_update": "2026-01-01"},
            "metadata": {
                "market": "US",
                "unit": "%",
                "frequency": "monthly",
                "source": "Bloomberg",
            },
        }
        item.update(overrides)
        return {
            "metadata": {"source": "Bloomberg API", "category": "Macro Indicators"},
            "data": [item],
        }

    def test_basic_mapping(self):
        records = self._n().map_fields(self._payload())
        assert len(records) == 1
        r = records[0]
        assert r.source_code == "CPI YOY Index"
        assert r.name == "US CPI YoY"
        assert r.value == Decimal("2.9")
        assert r.market == "US"
        assert r.unit == "%"
        assert r.frequency == "monthly"
        assert r.source == "bloomberg"

    def test_obs_date_from_last_update(self):
        records = self._n().map_fields(self._payload())
        assert str(records[0].obs_date) == "2026-01-01"

    def test_fallback_to_query_time_when_no_last_update(self):
        payload = self._payload()
        payload["data"][0]["timestamp"] = {"query_time": "2026-02-01T10:00:00"}
        records = self._n().map_fields(payload)
        assert len(records) == 1
        assert records[0].obs_date is not None

    def test_obs_date_from_date_field(self):
        payload = self._payload()
        payload["data"][0]["timestamp"] = {}
        payload["data"][0]["date"] = "2026-01-01"
        records = self._n().map_fields(payload)
        assert str(records[0].obs_date) == "2026-01-01"

    def test_value_from_price_last(self):
        records = self._n().map_fields(self._payload())
        assert records[0].value == Decimal("2.9")

    def test_value_from_flat_value_field(self):
        payload = self._payload()
        del payload["data"][0]["price"]
        payload["data"][0]["value"] = 3.1
        records = self._n().map_fields(payload)
        assert records[0].value == Decimal("3.1")

    def test_original_flat_payload(self):
        payload = {
            "metadata": {"source": "bloomberg", "query_time": "2026-02-01T10:00:00Z"},
            "data": [
                {
                    "ticker": "CPI YOY Index",
                    "date": "2026-01-01",
                    "value": 2.9,
                }
            ],
        }
        records = self._n().map_fields(payload)
        assert len(records) == 1
        assert records[0].source_code == "CPI YOY Index"
        assert str(records[0].obs_date) == "2026-01-01"
        assert records[0].value == Decimal("2.9")
        assert records[0].market == "MACRO"
        assert records[0].source == "bloomberg"

    def test_skip_record_without_ticker_or_symbol(self):
        payload = self._payload()
        del payload["data"][0]["ticker"]
        records = self._n().map_fields(payload)
        assert len(records) == 0

    def test_source_code_from_symbol_fallback(self):
        payload = self._payload()
        del payload["data"][0]["ticker"]
        payload["data"][0]["symbol"] = "MY_SERIES"
        records = self._n().map_fields(payload)
        assert records[0].source_code == "MY_SERIES"

    def test_market_from_item_field(self):
        payload = self._payload()
        del payload["data"][0]["metadata"]
        payload["data"][0]["market"] = "TW"
        records = self._n().map_fields(payload)
        assert records[0].market == "TW"

    def test_multiple_records(self):
        payload = {
            "metadata": {"source": "Bloomberg API"},
            "data": [
                {
                    "ticker": "GDP YOY Index",
                    "name": "US GDP YoY",
                    "price": {"last": 3.2},
                    "timestamp": {"last_update": "2026-01-01"},
                    "metadata": {
                        "market": "US",
                        "unit": "%",
                        "frequency": "quarterly",
                        "source": "Bloomberg",
                    },
                },
                {
                    "ticker": "UNEMP Index",
                    "name": "US Unemployment Rate",
                    "price": {"last": 4.1},
                    "timestamp": {"last_update": "2026-02-01"},
                    "metadata": {
                        "market": "US",
                        "unit": "%",
                        "frequency": "monthly",
                        "source": "Bloomberg",
                    },
                },
            ],
        }
        records = self._n().map_fields(payload)
        assert len(records) == 2
        assert records[0].source_code == "GDP YOY Index"
        assert records[1].source_code == "UNEMP Index"

    def test_dataset_key(self):
        assert MacroBloombergNormalizer.dataset_key == "macro_bloomberg_observation"
        assert MacroBloombergNormalizer.market == "MACRO"
        assert MacroBloombergNormalizer.asset_class == "macro"
