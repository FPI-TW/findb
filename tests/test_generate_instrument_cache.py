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
                "currency": "USD",
                "status": "active",
                "timezone": "America/New_York",
            },
            {
                "instrument_id": "1",
                "market": "CRYPTO",
                "asset_class": "crypto",
                "symbol": "BTC",
                "name": "Bitcoin",
                "currency": "USD",
                "status": "active",
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
        "currency": "USD",
        "status": "active",
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
