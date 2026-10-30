from __future__ import annotations

import json
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from findb_fetcher.client import SourceAPIClient
from findb_fetcher.config import FetcherConfig
from findb_fetcher.contracts import ContractRegistry
from findb_fetcher.providers.twelve_data import (
    TwelveDataClient,
    TwelveDataConfig,
    TwelveDataConfigError,
    TwelveDataNoNewDataError,
    TwelveDataPayloadError,
    TwelveDataResponseError,
    build_market_eod_request,
)

FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "twelve_data" / "aapl_1day_2024-01-02_2024-01-05.json"
)
FETCHED_AT = datetime(2024, 1, 6, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def twelve_data_response() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_twelve_data_config_requires_https_and_hides_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TWELVE_DATA_API_KEY", "provider-secret")
    monkeypatch.setenv("TWELVE_DATA_BASE_URL", "https://provider.example.test/")

    config = TwelveDataConfig.from_env()

    assert config.base_url == "https://provider.example.test"
    assert "provider-secret" not in repr(config)

    monkeypatch.setenv("TWELVE_DATA_BASE_URL", "http://provider.example.test")
    with pytest.raises(TwelveDataConfigError, match="HTTPS"):
        TwelveDataConfig.from_env()


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity"])
def test_twelve_data_config_rejects_invalid_timeout(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("TWELVE_DATA_API_KEY", "provider-secret")
    monkeypatch.setenv("TWELVE_DATA_TIMEOUT_SECONDS", value)

    with pytest.raises(TwelveDataConfigError, match="finite, positive"):
        TwelveDataConfig.from_env()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"api_key": ""}, "API_KEY is required"),
        ({"api_key": "key", "base_url": "http://provider.example.test"}, "HTTPS origin"),
        (
            {"api_key": "key", "base_url": "https://user:pass@provider.example.test"},
            "HTTPS origin",
        ),
        (
            {"api_key": "key", "base_url": "https://provider.example.test/path"},
            "HTTPS origin",
        ),
        ({"api_key": "key", "timeout_seconds": 0}, "finite, positive"),
        ({"api_key": "key", "timeout_seconds": float("nan")}, "finite, positive"),
    ],
)
def test_direct_twelve_data_config_construction_enforces_invariants(
    kwargs: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(TwelveDataConfigError, match=message):
        TwelveDataConfig(**kwargs)


def test_client_requests_bounded_daily_json_without_leaking_key() -> None:
    captured_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(
            200,
            json={
                "meta": {},
                "values": [],
                "status": "ok",
            },
            request=request,
        )

    client = TwelveDataClient(
        TwelveDataConfig(api_key="provider-secret", base_url="https://provider.example.test"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    response = client.fetch_daily(
        " AAPL ",
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 6),
        exchange="NASDAQ",
    )

    assert response["status"] == "ok"
    assert captured_request is not None
    assert captured_request.url.path == "/time_series"
    assert captured_request.url.params["symbol"] == "AAPL"
    assert captured_request.url.params["interval"] == "1day"
    assert captured_request.url.params["order"] == "asc"
    assert captured_request.url.params["format"] == "JSON"
    assert captured_request.url.params["adjust"] == "none"
    assert captured_request.url.params["start_date"] == "2024-01-02"
    assert captured_request.url.params["end_date"] == "2024-01-07"
    assert captured_request.url.params["exchange"] == "NASDAQ"
    assert "apikey" not in captured_request.url.params
    assert "provider-secret" not in str(captured_request.url)
    assert captured_request.headers["authorization"] == "apikey provider-secret"
    assert captured_request.headers["accept-encoding"] == "identity"


def test_client_preserves_exact_identity_encoded_response_bytes() -> None:
    raw = FIXTURE_PATH.read_bytes()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=raw,
            headers={"Content-Encoding": "identity"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        result = TwelveDataClient(
            TwelveDataConfig(api_key="provider-secret"),
            client=http_client,
        ).fetch_daily("AAPL")

    assert result.raw_bytes == raw


def test_client_rejects_compressed_or_oversized_stream_before_full_read() -> None:
    def compressed(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=httpx.ByteStream(b"compressed"),
            headers={"Content-Encoding": "gzip"},
        )

    with httpx.Client(transport=httpx.MockTransport(compressed)) as http_client:
        with pytest.raises(TwelveDataResponseError, match="Content-Encoding"):
            TwelveDataClient(
                TwelveDataConfig(api_key="provider-secret"),
                client=http_client,
            ).fetch_daily("AAPL")

    yielded = 0

    def chunked(_request: httpx.Request) -> httpx.Response:
        class Chunks(httpx.SyncByteStream):
            def __iter__(self):
                nonlocal yielded
                for chunk in (b"1234", b"5678", b"not-read"):
                    yielded += 1
                    yield chunk

        return httpx.Response(200, stream=Chunks())

    with httpx.Client(transport=httpx.MockTransport(chunked)) as http_client:
        with pytest.raises(TwelveDataResponseError, match="size limit"):
            TwelveDataClient(
                TwelveDataConfig(api_key="provider-secret", max_response_bytes=6),
                client=http_client,
            ).fetch_daily("AAPL")

    assert yielded == 2


def test_client_rejects_provider_error_and_redacts_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "code": 429,
                "message": "limit reached for provider-secret",
                "status": "error",
            },
            request=request,
        )

    client = TwelveDataClient(
        TwelveDataConfig(api_key="provider-secret", base_url="https://provider.example.test"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(TwelveDataResponseError) as error:
        client.fetch_daily("AAPL", outputsize=1)

    assert error.value.status_code == 429
    assert error.value.provider_code == 429
    assert error.value.is_rate_limited is True
    assert "429" in str(error.value)
    assert "provider-secret" not in str(error.value)
    assert "[redacted]" in str(error.value)


def test_provider_error_code_string_is_typed_for_rate_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": "429",
                "message": "rate limited",
                "status": "error",
            },
            request=request,
        )

    client = TwelveDataClient(
        TwelveDataConfig(api_key="provider-secret"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(TwelveDataResponseError) as error:
        client.fetch_daily("AAPL", outputsize=1)

    assert error.value.status_code == 200
    assert error.value.provider_code == 429
    assert error.value.is_rate_limited is True


@pytest.mark.parametrize(
    ("status", "body", "category"),
    [
        (
            400,
            {
                "code": 400,
                "message": "No data is available on the specified dates. Try setting "
                "different start/end dates.",
                "status": "error",
            },
            "no_data",
        ),
        (
            200,
            {
                "code": 400,
                "message": "No data is available on the specified dates.",
                "status": "error",
            },
            "no_data",
        ),
        (
            200,
            {
                "code": 400,
                "message": "**symbol** not found: ZZZZ. Please specify it correctly.",
                "status": "error",
            },
            "invalid_symbol",
        ),
        (200, {"code": 400, "message": "bad parameter", "status": "error"}, "bad_request"),
        (401, {"code": 401, "message": "invalid key", "status": "error"}, "auth_failed"),
        (200, {"code": 403, "message": "Pro plan required", "status": "error"}, "plan_restricted"),
        (404, {"code": 404, "message": "missing", "status": "error"}, "not_found"),
        (502, {"code": 502, "message": "gateway", "status": "error"}, "server_error"),
        (200, {"code": 429, "message": "credits", "status": "error"}, "rate_limited"),
        (418, {"message": "teapot"}, "http_error"),
        (200, {"status": "pending"}, "malformed_response"),
        (200, ["not", "an", "object"], "malformed_response"),
    ],
)
def test_provider_errors_have_bounded_categories(status, body, category) -> None:
    client = TwelveDataClient(
        TwelveDataConfig(api_key="provider-secret"),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(status, json=body, request=request)
            )
        ),
    )

    with pytest.raises(TwelveDataResponseError) as error:
        client.fetch_daily("AAPL", start_date=date(2024, 1, 2), end_date=date(2024, 1, 2))

    assert error.value.category == category
    assert "provider-secret" not in str(error.value)


def test_non_json_and_transport_failures_are_categorized() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    for transport, category in (
        (
            lambda request: httpx.Response(200, content=b"<html>", request=request),
            "malformed_response",
        ),
        (lambda request: httpx.Response(503, content=b"<html>", request=request), "server_error"),
        (broken, "transport"),
    ):
        client = TwelveDataClient(
            TwelveDataConfig(api_key="provider-secret"),
            client=httpx.Client(transport=httpx.MockTransport(transport)),
        )
        with pytest.raises(TwelveDataResponseError) as error:
            client.fetch_daily("AAPL", outputsize=1)
        assert error.value.category == category


def test_unknown_error_category_is_rejected() -> None:
    with pytest.raises(ValueError, match="category"):
        TwelveDataResponseError("x", category="provider said something")


def test_client_rejects_ambiguous_date_window_and_outputsize() -> None:
    client = TwelveDataClient(
        TwelveDataConfig(api_key="provider-secret"),
        client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200))),
    )

    with pytest.raises(ValueError, match="outputsize must be omitted"):
        client.fetch_daily(
            "AAPL",
            start_date=date(2024, 1, 2),
            end_date=date(2024, 1, 6),
            outputsize=4,
        )


@pytest.mark.parametrize("exchange", ["", "x" * 101, "NASDAQ\ninjected"])
def test_client_rejects_invalid_exchange(exchange: str) -> None:
    client = TwelveDataClient(
        TwelveDataConfig(api_key="provider-secret"),
        client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200))),
    )

    with pytest.raises(ValueError, match="exchange"):
        client.fetch_daily("AAPL", exchange=exchange)


def test_live_fixture_maps_to_versioned_contract(
    twelve_data_response: dict[str, Any],
    contracts_dir: Path,
) -> None:
    request = build_market_eod_request(
        twelve_data_response,
        dataset_key="us_equity_eod",
        fetched_at=FETCHED_AT,
        requested_symbol="AAPL",
    )

    ContractRegistry(contracts_dir).validate("market_eod", 1, request)
    assert request["source"] == "twelve_data"
    assert request["fetched_at"] == "2024-01-06T12:00:00Z"
    assert request["payload"]["batch"] == {
        "data_date": "2024-01-05",
        "delivery_mode": "backfill",
        "declared_record_count": 4,
        "coverage_start_date": "2024-01-02",
        "coverage_end_date": "2024-01-05",
    }
    assert request["payload"]["data"][0] == {
        "symbol": "AAPL",
        "source_symbol": "AAPL",
        "trade_date": "2024-01-02",
        "currency": "USD",
        "open": "187.14999",
        "high": "188.44000",
        "low": "183.89000",
        "close": "185.64000",
        "volume": 82488700,
    }


def test_mapping_rejects_response_for_another_requested_instrument(
    twelve_data_response: dict[str, Any],
) -> None:
    with pytest.raises(TwelveDataPayloadError, match="requested symbol"):
        build_market_eod_request(
            twelve_data_response,
            dataset_key="us_equity_eod",
            fetched_at=FETCHED_AT,
            requested_symbol="MSFT",
        )

    with pytest.raises(TwelveDataPayloadError, match="requested exchange"):
        build_market_eod_request(
            twelve_data_response,
            dataset_key="us_equity_eod",
            fetched_at=FETCHED_AT,
            requested_symbol="AAPL",
            requested_exchange="NYSE",
        )


def test_mapping_sorts_rows_and_keeps_logical_delivery_identity_stable(
    twelve_data_response: dict[str, Any],
) -> None:
    reversed_response = deepcopy(twelve_data_response)
    reversed_response["values"].reverse()

    first = build_market_eod_request(
        twelve_data_response,
        dataset_key="us_equity_eod",
        fetched_at=FETCHED_AT,
        requested_symbol="AAPL",
    )
    second = build_market_eod_request(
        reversed_response,
        dataset_key="us_equity_eod",
        fetched_at=datetime(2024, 1, 6, 13, 0, tzinfo=timezone.utc),
        requested_symbol="AAPL",
    )

    assert second["payload"]["data"] == first["payload"]["data"]
    assert second["request_key"] == first["request_key"]
    assert second["idempotency_key"] == first["idempotency_key"]
    assert second["fetched_at"] != first["fetched_at"]


def test_single_daily_value_maps_to_incremental_delivery(
    twelve_data_response: dict[str, Any],
) -> None:
    single_value = deepcopy(twelve_data_response)
    single_value["values"] = [single_value["values"][-1]]

    request = build_market_eod_request(
        single_value,
        dataset_key="us_equity_eod",
        fetched_at=FETCHED_AT,
        requested_symbol="AAPL",
    )

    assert request["payload"]["batch"] == {
        "data_date": "2024-01-05",
        "delivery_mode": "incremental",
        "declared_record_count": 1,
    }


def test_mapping_filters_rows_at_or_before_checkpoint(
    twelve_data_response: dict[str, Any],
) -> None:
    request = build_market_eod_request(
        twelve_data_response,
        dataset_key="us_equity_eod",
        fetched_at=FETCHED_AT,
        requested_symbol="AAPL",
        after_trade_date=date(2024, 1, 3),
    )

    assert [row["trade_date"] for row in request["payload"]["data"]] == [
        "2024-01-04",
        "2024-01-05",
    ]
    assert request["payload"]["batch"]["coverage_start_date"] == "2024-01-04"

    with pytest.raises(TwelveDataNoNewDataError):
        build_market_eod_request(
            twelve_data_response,
            dataset_key="us_equity_eod",
            fetched_at=FETCHED_AT,
            requested_symbol="AAPL",
            after_trade_date=date(2024, 1, 5),
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda body: body["meta"].update({"interval": "1min"}), "interval"),
        (lambda body: body["meta"].update({"type": "ETF"}), "instrument type"),
        (lambda body: body["values"].append(deepcopy(body["values"][0])), "duplicate"),
        (lambda body: body["values"][0].update({"high": "100"}), "OHLC"),
        (lambda body: body["values"][0].update({"close": "NaN"}), "finite"),
        (lambda body: body["values"][0].update({"volume": "12.5"}), "integer"),
    ],
)
def test_mapping_rejects_provider_drift(
    twelve_data_response: dict[str, Any],
    mutation: Any,
    message: str,
) -> None:
    mutation(twelve_data_response)

    with pytest.raises(TwelveDataPayloadError, match=message):
        build_market_eod_request(
            twelve_data_response,
            dataset_key="us_equity_eod",
            fetched_at=FETCHED_AT,
            requested_symbol="AAPL",
        )


def test_adapter_contract_and_source_delivery_integration(
    twelve_data_response: dict[str, Any],
    contracts_dir: Path,
) -> None:
    delivered: dict[str, Any] = {}
    attempt_id = "019f98b5-ee0b-7b16-9a28-d8e2ed638a91"
    run_id = "019f98b5-ee0b-7b16-9a28-d8e2ed638a92"

    def handler(request: httpx.Request) -> httpx.Response:
        delivered.update(json.loads(request.content))
        return httpx.Response(
            202,
            json={
                "success": True,
                "attempt_id": attempt_id,
                "run_id": run_id,
                "status": "queued",
                "schema_id": "market_eod",
                "schema_version": 1,
                "message": "Data received, processing queued",
            },
            request=request,
        )

    request = build_market_eod_request(
        twelve_data_response,
        dataset_key="us_equity_eod",
        fetched_at=FETCHED_AT,
        requested_symbol="AAPL",
    )
    source_client = SourceAPIClient(
        FetcherConfig(
            source_api_url="https://source.example.test",
            source_client_key="source-client-secret",
        ),
        ContractRegistry(contracts_dir),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    prepared = source_client.prepare(request)
    receipt = source_client.deliver(prepared)
    assert str(receipt.attempt_id) == attempt_id
    assert str(receipt.run_id) == run_id
    assert receipt.status == "queued"
    assert delivered == request


def test_fixture_contains_no_credentials() -> None:
    fixture = FIXTURE_PATH.read_text(encoding="utf-8").lower()

    assert "apikey" not in fixture
    assert "api_key" not in fixture


@pytest.mark.parametrize(
    ("start", "end", "wire_end"),
    [
        (date(2024, 1, 5), date(2024, 1, 5), "2024-01-06"),
        (date(2024, 1, 2), date(2024, 1, 5), "2024-01-06"),
        (date(2024, 2, 29), date(2024, 2, 29), "2024-03-01"),
        (date(2024, 12, 31), date(2024, 12, 31), "2025-01-01"),
        (None, date(2024, 1, 5), "2024-01-06"),
        (None, None, None),
    ],
)
def test_client_inclusive_dates_reach_provider_as_next_midnight(
    start: date | None, end: date | None, wire_end: str | None
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.params.get("start_date") == (start.isoformat() if start else None)
        assert request.url.params.get("end_date") == wire_end
        assert request.url.params.get("outputsize") == ("2" if start is None else None)
        return httpx.Response(200, content=FIXTURE_PATH.read_bytes())

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        provider = TwelveDataClient(TwelveDataConfig(api_key="test-key"), client=http)
        response = provider.fetch_daily(
            "AAPL", start_date=start, end_date=end, outputsize=2 if start is None else None
        )
    assert len(requests) == 1
    assert response.raw_bytes == FIXTURE_PATH.read_bytes()


def test_client_rejects_unrepresentable_end_before_acquisition() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid upper bound must not consume provider quota")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        provider = TwelveDataClient(TwelveDataConfig(api_key="test-key"), client=http)
        with pytest.raises(ValueError, match="following midnight"):
            provider.fetch_daily("AAPL", end_date=date.max)


def test_mapping_contains_inclusive_window_without_changing_raw(
    twelve_data_response: dict[str, Any],
) -> None:
    original = deepcopy(twelve_data_response)
    request = build_market_eod_request(
        twelve_data_response,
        dataset_key="us_equity_eod",
        fetched_at=FETCHED_AT,
        requested_symbol="AAPL",
        from_trade_date=date(2024, 1, 3),
        through_trade_date=date(2024, 1, 4),
    )
    assert [row["trade_date"] for row in request["payload"]["data"]] == ["2024-01-03", "2024-01-04"]
    assert twelve_data_response == original
    with pytest.raises(TwelveDataNoNewDataError):
        build_market_eod_request(
            twelve_data_response,
            dataset_key="us_equity_eod",
            fetched_at=FETCHED_AT,
            requested_symbol="AAPL",
            from_trade_date=date(2024, 1, 6),
            through_trade_date=date(2024, 1, 6),
        )
