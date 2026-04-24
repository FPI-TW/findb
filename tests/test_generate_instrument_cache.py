"""
Tests for instrument cache generation helpers.
"""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path


def load_module():
    module_path = Path("scripts/generate_instrument_cache.py")
    spec = importlib.util.spec_from_file_location("generate_instrument_cache", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_build_cache_payload_sorts_and_extracts_metadata():
    module = load_module()

    payload = module.build_cache_payload(
        [
            {
                "instrument_id": "2",
                "market": "US",
                "asset_class": "equity",
                "symbol": "AAPL",
                "name": "Apple",
                "short_name": "APL",
                "currency": "USD",
                "status": "active",
                "timezone": "America/New_York",
                "latest_trade_date": "2026-04-08",
                "latest_price": "269.48000000",
            },
            {
                "instrument_id": "1",
                "market": "CRYPTO",
                "asset_class": "crypto",
                "symbol": "BTC",
                "name": "Bitcoin",
                "short_name": "BTC",
                "currency": "USD",
                "status": "active",
                "latest_trade_date": "2026-04-09",
                "latest_price": "95709.01000000",
            },
        ],
        generated_at=datetime(2026, 4, 9, 10, 0, tzinfo=timezone.utc),
    )

    assert payload["generated_at"] == "2026-04-09T10:00:00Z"
    assert payload["total"] == 2
    assert payload["markets"] == ["CRYPTO", "US"]
    assert payload["asset_classes"] == ["crypto", "equity"]
    assert payload["data"][0] == {
        "instrument_id": "1",
        "market": "CRYPTO",
        "asset_class": "crypto",
        "symbol": "BTC",
        "name": "Bitcoin",
        "short_name": "BTC",
        "currency": "USD",
        "status": "active",
        "latest_trade_date": "2026-04-09",
        "latest_price": "95709.01000000",
    }


def test_build_macro_series_payload_sorts_and_extracts_metadata():
    module = load_module()

    payload = module.build_macro_series_payload(
        [
            {
                "series_id": "2",
                "name": "US CPI YoY",
                "short_name": "UCY",
                "unit": "percent",
                "frequency": "monthly",
                "market": "US",
                "source_code": "CPI_YOY",
                "source": "bloomberg",
                "ignored": "value",
            },
            {
                "series_id": "1",
                "name": "Global Manufacturing PMI",
                "short_name": "GMP",
                "unit": "index",
                "frequency": "monthly",
                "market": "GLOBAL",
                "source_code": "PMI_GLOBAL",
                "source": "bloomberg",
            },
        ],
        generated_at=datetime(2026, 4, 9, 10, 0, tzinfo=timezone.utc),
    )

    assert payload["generated_at"] == "2026-04-09T10:00:00Z"
    assert payload["total"] == 2
    assert payload["markets"] == ["GLOBAL", "US"]
    assert payload["frequencies"] == ["monthly"]
    assert payload["sources"] == ["bloomberg"]
    assert payload["data"][0] == {
        "series_id": "1",
        "name": "Global Manufacturing PMI",
        "short_name": "GMP",
        "unit": "index",
        "frequency": "monthly",
        "market": "GLOBAL",
        "source_code": "PMI_GLOBAL",
        "source": "bloomberg",
    }


def test_collect_instruments_reads_all_pages(monkeypatch):
    module = load_module()

    responses = {
        1: {
            "success": True,
            "data": [{"instrument_id": "1"}],
            "pagination": {"page": 1, "total_pages": 2},
        },
        2: {
            "success": True,
            "data": [{"instrument_id": "2"}],
            "pagination": {"page": 2, "total_pages": 2},
        },
    }

    def fake_fetch(base_url, page, page_size, api_key=None):
        assert base_url == "http://localhost:8080"
        assert page_size == module.PAGE_SIZE
        assert api_key == "demo-key"
        return responses[page]

    monkeypatch.setattr(module, "fetch_instruments_page", fake_fetch)

    instruments = module.collect_instruments(
        "http://localhost:8080",
        api_key="demo-key",
        page_size=module.PAGE_SIZE,
    )

    assert instruments == [{"instrument_id": "1"}, {"instrument_id": "2"}]


def test_enrich_instruments_with_latest_prices(monkeypatch):
    module = load_module()

    calls = []

    def fake_fetch_latest_eod(base_url, instrument_id, api_key=None):
        calls.append((base_url, instrument_id, api_key))
        if instrument_id == "2":
            return None
        return {"trade_date": "2026-04-09", "close": "95709.01000000"}

    def fake_fetch_latest_futures_continuous(base_url, market, symbol, api_key=None):
        assert base_url == "http://localhost:8080"
        assert api_key == "demo-key"
        assert (market, symbol) == ("WTX", "TXF1")
        return {"trade_date": "2026-03-12", "close": "21000.00000000"}

    monkeypatch.setattr(module, "fetch_latest_eod", fake_fetch_latest_eod)
    monkeypatch.setattr(
        module,
        "fetch_latest_futures_continuous",
        fake_fetch_latest_futures_continuous,
    )

    enriched = module.enrich_instruments_with_latest_prices(
        [
            {"instrument_id": "1", "symbol": "BTC"},
            {
                "instrument_id": "existing",
                "symbol": "AAPL",
                "latest_trade_date": "2026-04-08",
                "latest_price": "269.48000000",
            },
            {"instrument_id": "2", "market": "WTX", "symbol": "TXF1"},
        ],
        "http://localhost:8080",
        api_key="demo-key",
        max_workers=1,
    )

    assert calls == [
        ("http://localhost:8080", "1", "demo-key"),
        ("http://localhost:8080", "2", "demo-key"),
    ]
    assert enriched[0]["latest_trade_date"] == "2026-04-09"
    assert enriched[0]["latest_price"] == "95709.01000000"
    assert enriched[1]["latest_trade_date"] == "2026-04-08"
    assert enriched[1]["latest_price"] == "269.48000000"
    assert enriched[2]["latest_trade_date"] == "2026-03-12"
    assert enriched[2]["latest_price"] == "21000.00000000"


def test_collect_macro_series_reads_all_pages(monkeypatch):
    module = load_module()

    responses = {
        1: {
            "success": True,
            "data": [{"series_id": "1"}],
            "pagination": {"page": 1, "total_pages": 2},
        },
        2: {
            "success": True,
            "data": [{"series_id": "2"}],
            "pagination": {"page": 2, "total_pages": 2},
        },
    }

    def fake_fetch(base_url, page, page_size, api_key=None):
        assert base_url == "http://localhost:8080"
        assert page_size == module.PAGE_SIZE
        assert api_key == "demo-key"
        return responses[page]

    monkeypatch.setattr(module, "fetch_macro_series_page", fake_fetch)

    series = module.collect_macro_series(
        "http://localhost:8080",
        api_key="demo-key",
        page_size=module.PAGE_SIZE,
    )

    assert series == [{"series_id": "1"}, {"series_id": "2"}]
