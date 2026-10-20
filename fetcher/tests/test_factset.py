"""Listing identity, lossless values and credential-safe MySQL boundaries."""

import json
import ssl
import sys
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from findb_fetcher.providers import factset

DAY = date(2026, 10, 15)


def native(**changes):
    return {
        "fsym_id": "J3TRKQ-L",
        "price_date": DAY.isoformat(),
        "currency": "HKD",
        "price": 600,
        "price_open": None,
        "price_high": 610,
        "price_low": 590,
        "volume": 1234.0,
        "turnover": 999999,
        **changes,
    }


def mapped(row):
    return factset.contract_row(row, symbol="00700", listing_id="J3TRKQ-L", day=DAY)


def test_lossless_mapping_and_native_preservation():
    row = native(price=Decimal("600.123"), price_high=500, volume=9007199254740993)
    raw = factset.encode_native([row], scope={"trade_date": DAY})
    result = mapped(json.loads(raw)["rows"][0])
    assert result["close"] == "600.123"
    assert result["volume"] == 9007199254740993
    assert result["high"] == "500"  # Relational failure belongs to v2 DQ.
    assert result["open"] is None and "turnover" not in result
    assert json.loads(raw)["rows"][0]["turnover"] == 999999


@pytest.mark.parametrize(
    "changes",
    [
        {"price": None},
        {"price": -1},
        {"price": float("nan")},
        {"price": float("inf")},
        {"price": 10**12},
        {"volume": 0.5},
        {"volume": True},
        {"volume": -1},
        {"volume": 2**63},
        {"fsym_id": "J3TRKQ-R"},
        {"price_date": "2026-10-14"},
        {"currency": None},
    ],
)
def test_structural_bad_rows_remain_rejectable_raw(changes):
    raw = factset.encode_native([native(**changes)], scope={})
    with pytest.raises(factset.FactSetRowError):
        mapped(json.loads(raw)["rows"][0])
    assert raw


def test_official_members_survive_missing_ambiguous_currency_and_classification_mapping():
    official = [
        {"symbol": key, "currency": "HKD", "classification": "ordinary"}
        for key in ("00700", "00005", "00001", "00002", "00003")
    ]
    official[-1]["classification"] = "classification_gap"
    catalogue = [
        {
            "ticker_exchange": ticker,
            "fsym_id": listing,
            "currency": currency,
            "listing_flag": 1,
            "fref_security_type": "SHARE",
        }
        for ticker, listing, currency in (
            ("700-HKG", "J3TRKQ-L", "HKD"),
            ("1-HKG", "AAAAAA-L", "HKD"),
            ("1-HKG", "BBBBBB-L", "HKD"),
            ("2-HKG", "CCCCCC-L", "USD"),
            ("3-HKG", "DDDDDD-L", "HKD"),
        )
    ]
    result = factset.map_current_cohort(official, factset.encode_native(catalogue, scope={}))
    assert len(result) == len(official)
    assert [row["mapping_status"] for row in result] == ["mapped", "gap", "gap", "gap", "gap"]
    assert result[0]["provider_symbol"] == "J3TRKQ-L"
    assert "provider_symbol" not in official[0]


def test_query_uses_tls_read_only_and_bound_parameters(monkeypatch, tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("test CA")
    config = factset.FactSetConfig("example.invalid", "reader", "do-not-log", ca)
    assert "reader" not in repr(config) and "do-not-log" not in repr(config)
    events = []
    context = ssl.create_default_context()
    monkeypatch.setattr(factset.ssl, "create_default_context", lambda **kw: context)

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, params=()):
            events.append((sql, params))

        def fetchmany(self, count):
            assert count == 10001
            return [native()]

    class Connection:
        def cursor(self):
            return Cursor()

        def rollback(self):
            events.append("rollback")

        def close(self):
            events.append("close")

    def connect(**kwargs):
        assert kwargs["ssl"].verify_mode == ssl.CERT_REQUIRED
        assert kwargs["ssl"].check_hostname is True
        assert kwargs["autocommit"] is False and kwargs["read_timeout"] <= 65
        events.append("connect")
        return Connection()

    monkeypatch.setitem(
        sys.modules,
        "pymysql",
        SimpleNamespace(
            connect=connect,
            cursors=SimpleNamespace(SSDictCursor=object),
        ),
    )

    class Operation:
        def __init__(self, provider, credential):
            assert provider == "factset" and "do-not-log" not in credential

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def dispatch_checkpoint(self):
            events.append("authorize")

    def supervise(callback, *, initialize, checkpoint, timeout):
        assert timeout <= 80
        checkpoint()
        initialize()
        checkpoint()
        return callback()

    monkeypatch.setattr(factset, "Acquisition", Operation)
    monkeypatch.setattr(factset, "supervised_call", supervise)
    raw = factset.FactSetGateway(config).prices(DAY, ("J3TRKQ-L",))
    assert json.loads(raw)["rows"][0]["fsym_id"] == "J3TRKQ-L"
    assert events[:2] == ["authorize", "connect"]
    assert events[3][0] == "START TRANSACTION READ ONLY"
    assert events[4] == "authorize"
    sql, params = events[5]
    assert "J3TRKQ-L" not in sql and "LIMIT 10001" in sql
    assert params == (DAY, "J3TRKQ-L")
    assert events[-2:] == ["rollback", "close"]

    def rejected(**kwargs):
        raise RuntimeError(1045, "do-not-log reader")

    sys.modules["pymysql"].connect = rejected
    with pytest.raises(factset.FactSetOperationalError) as error:
        factset.FactSetGateway(config).prices(DAY, ("J3TRKQ-L",))
    assert str(error.value) == "FACTSET_QUERY_REJECTED" and not error.value.retryable


@pytest.mark.parametrize(
    "identities",
    [(), ("bad'; SELECT",), ("AAAAAA-R",), ("BBBBBB-L", "AAAAAA-L"), ("AAAAAA-L", "AAAAAA-L")],
)
def test_listing_scope_rejected_before_query(tmp_path, identities):
    ca = tmp_path / "ca.pem"
    ca.touch()
    gateway = factset.FactSetGateway(factset.FactSetConfig("example.invalid", "user", "secret", ca))
    with pytest.raises(factset.FactSetConfigError):
        gateway.prices(DAY, identities)


def test_catalogue_uses_bounded_official_ticker_index_and_current_association(
    tmp_path, monkeypatch
):
    ca = tmp_path / "ca.pem"
    ca.touch()
    gateway = factset.FactSetGateway(factset.FactSetConfig("example.invalid", "user", "secret", ca))
    calls = []
    monkeypatch.setattr(
        gateway, "_query", lambda sql, params, **kw: calls.append((sql, params, kw)) or b"native"
    )
    assert gateway.catalogue(("00001", "00700")) == b"native"
    sql, params, scope = calls[0]
    assert params == ("1-HKG", "700-HKG", "HKG", "SHARE")
    assert "FROM sym_v1_sym_ticker_exchange_hist h STRAIGHT_JOIN" in sql
    assert "t.fsym_id=h.fsym_id AND t.ticker_exchange=h.ticker_exchange" in sql
    assert "SELECT DISTINCT" in sql and "LIMIT 10001" in sql
    assert scope["scope"]["symbols"] == ("00001", "00700")
    for invalid in ((), ("700",), ("00700", "00001"), ("00700", "00700"), ("';--",)):
        with pytest.raises(factset.FactSetConfigError):
            gateway.catalogue(invalid)
    assert len(calls) == 1
