"""Tests for the current instrument cache generator."""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path


def load_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "generate_instrument_cache.py"
    spec = importlib.util.spec_from_file_location("generate_instrument_cache", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def instrument(instrument_id: str, market: str, symbol: str) -> dict:
    return {
        "instrument_id": instrument_id,
        "market": market,
        "asset_class": "equity",
        "symbol": symbol,
        "name": symbol,
        "currency": "USD",
        "status": "active",
        "coverage": {
            "eod": {
                "first_date": "2026-01-02",
                "latest_date": "2026-04-09",
                "latest_close": "42.00000000",
            },
            "minute": None,
        },
        "provider": "must-not-leak",
    }


def test_build_cache_payload_sorts_and_keeps_nested_coverage():
    module = load_module()
    payload = module.build_cache_payload(
        [instrument("2", "US", "MSFT"), instrument("1", "TW", "2330")],
        generated_at=datetime(2026, 4, 9, 10, 0, tzinfo=timezone.utc),
    )

    assert payload["generated_at"] == "2026-04-09T10:00:00Z"
    assert payload["total"] == 2
    assert payload["markets"] == ["TW", "US"]
    assert payload["data"][0]["symbol"] == "2330"
    assert payload["data"][0]["coverage"]["eod"]["latest_close"] == "42.00000000"
    assert "provider" not in payload["data"][0]


def test_collect_instruments_reads_every_100_item_page(monkeypatch):
    module = load_module()
    responses = {
        1: {"success": True, "data": [{"instrument_id": "1"}], "pagination": {"total_pages": 2}},
        2: {"success": True, "data": [{"instrument_id": "2"}], "pagination": {"total_pages": 2}},
    }
    calls: list[tuple[int, int]] = []

    def fake_fetch(base_url, page, page_size, api_key=None):
        assert base_url == "http://localhost:8080"
        assert api_key == "demo-key"
        calls.append((page, page_size))
        return responses[page]

    monkeypatch.setattr(module, "fetch_instruments_page", fake_fetch)
    result = module.collect_instruments("http://localhost:8080", api_key="demo-key")

    assert result == [{"instrument_id": "1"}, {"instrument_id": "2"}]
    assert calls == [(1, 100), (2, 100)]


def test_collect_instruments_rejects_page_size_above_api_limit():
    module = load_module()
    try:
        module.collect_instruments("http://localhost:8080", page_size=101)
    except ValueError as exc:
        assert "between 1 and 100" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_main_removes_retired_macro_cache_even_when_refresh_fails(monkeypatch, tmp_path):
    module = load_module()
    macro_path = tmp_path / "macro-series.json"
    macro_path.write_text("stale", encoding="utf-8")
    monkeypatch.setattr(module, "MACRO_OUTPUT_PATH", macro_path)

    def fail(*_args, **_kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(module, "collect_instruments", fail)

    assert module.main() == 1
    assert not macro_path.exists()
