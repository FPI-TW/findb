"""Tests for Source API v2 endpoints (app/api/v2/source.py).

What is covered:
- Authentication: 401 (missing key), 403 (wrong key) — all 7 endpoints require auth
- Pydantic validation: 422 on bad/missing fields
- Celery dispatch: success → 200, broker down → 503
- RabbitMQ pressure guard: 429 + Retry-After, fail-open, 5-second cache
- Response shape: dataset_key per endpoint, items count, queue name, message_id
- _poll_rabbitmq_pressure unit tests: queue depth, node memory, fail-open

Strategy:
- `process_ingestion_task.apply_async` is always mocked (no real broker needed)
- `_poll_rabbitmq_pressure` is mocked per integration test (no real management API needed)
- `_http_get_json` is mocked in _poll_rabbitmq_pressure unit tests to inject fake API responses
- Module-level pressure cache is reset by `reset_pressure_cache` autouse fixture
"""

import time
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from httpx import AsyncClient
from kombu.exceptions import KombuError

import app.api.v2.source as v2_source
from app.config import get_settings

settings = get_settings()

BASE = "/api/v2/source"

# ---------------------------------------------------------------------------
# Shared payload helpers
# ---------------------------------------------------------------------------

# Payload for DirectIngestPayload endpoints (hkchina-index, crypto, fx, macro, wtx)
VALID_PAYLOAD = {
    "metadata": {
        "source": "bloomberg",
        "query_time": "2026-01-02T00:00:00Z",
    },
    "data": [
        {
            "ticker": "AAPL US Equity",
            "date": "2026-01-02",
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1_000_000,
        }
    ],
}

# Payload for IngestEquitieRequest endpoints (usstock, hkchina)
VALID_EQUITIE_PAYLOAD = {
    "dataset_key": "us_stock_eod",
    "source": "bloomberg",
    "request_key": "test-req-001",
    "idempotency_key": "test-idem-001",
    "payload": {
        "data": [
            {
                "ticker": "AAPL US Equity",
                "date": "2026-01-02",
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 1_000_000,
            }
        ]
    },
    "fetched_at": "2026-01-02T00:00:00Z",
}

# Endpoints grouped by body schema type
EQUITIE_ENDPOINTS = [
    (f"{BASE}/ingest/usstock/direct", "us_stock_eod"),
    (f"{BASE}/ingest/hkchina/direct", "hkchina_mixed_eod"),
]

DIRECT_ENDPOINTS = [
    (f"{BASE}/ingest/hkchina-index/direct", "hkchina_index_eod"),
    (f"{BASE}/ingest/crypto/direct", "crypto_bloomberg_eod"),
    (f"{BASE}/ingest/fx/direct", "fx_bloomberg_eod"),
    (f"{BASE}/ingest/macro/direct", "macro_bloomberg_observation"),
    (f"{BASE}/ingest/wtx/direct", "wtx_bloomberg_eod"),
]

ALL_ENDPOINTS = EQUITIE_ENDPOINTS + DIRECT_ENDPOINTS
ALL_URLS = [url for url, _ in ALL_ENDPOINTS]

# Parametrize list pairing each endpoint with its correct payload schema
ALL_ENDPOINTS_WITH_PAYLOAD = [
    (url, key, VALID_EQUITIE_PAYLOAD) for url, key in EQUITIE_ENDPOINTS
] + [(url, key, VALID_PAYLOAD) for url, key in DIRECT_ENDPOINTS]

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_pressure_cache():
    """Reset module-level pressure cache so tests are isolated."""
    v2_source._pressure_pressured = False
    v2_source._pressure_expires_at = 0.0
    yield
    v2_source._pressure_pressured = False
    v2_source._pressure_expires_at = 0.0


@pytest.fixture
def mock_task():
    """Patch Celery apply_async — returns None (success)."""
    with patch("app.api.v2.source.process_ingestion_task.apply_async") as m:
        m.return_value = None
        yield m


@pytest.fixture
def mock_no_pressure():
    """Patch pressure poll to always report no pressure."""
    with patch(
        "app.api.v2.source._poll_rabbitmq_pressure",
        new_callable=AsyncMock,
        return_value=False,
    ):
        yield


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class TestV2Auth:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", ALL_URLS)
    async def test_missing_api_key_returns_401(
        self, client: AsyncClient, mock_no_pressure, mock_task, url: str
    ):
        resp = await client.post(url, json=VALID_PAYLOAD)
        assert resp.status_code == 401

    @pytest.mark.asyncio
    @pytest.mark.parametrize("url", ALL_URLS)
    async def test_invalid_api_key_returns_403(
        self, client: AsyncClient, mock_no_pressure, mock_task, url: str
    ):
        resp = await client.post(
            url,
            json=VALID_PAYLOAD,
            headers={settings.API_KEY_HEADER: "wrong-key"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Pydantic validation (422)
# ---------------------------------------------------------------------------


class TestV2PayloadValidation:
    """Pydantic validation tests for IngestEquitieRequest (usstock/direct endpoint).

    IngestEquitieRequest validates top-level required fields (source, fetched_at, etc.)
    and item-level fields in payload.data via OhlcvDataItem (ticker, date, numeric prices).
    """

    URL = f"{BASE}/ingest/usstock/direct"

    def _item_payload(self, **overrides) -> dict:
        """Return VALID_EQUITIE_PAYLOAD with one item field overridden."""
        item = {**VALID_EQUITIE_PAYLOAD["payload"]["data"][0], **overrides}
        return {**VALID_EQUITIE_PAYLOAD, "payload": {"data": [item]}}

    @pytest.mark.asyncio
    async def test_missing_ticker_in_item_returns_422(
        self, client: AsyncClient, mock_no_pressure, mock_task, source_headers: dict
    ):
        item = {
            k: v for k, v in VALID_EQUITIE_PAYLOAD["payload"]["data"][0].items() if k != "ticker"
        }
        payload = {**VALID_EQUITIE_PAYLOAD, "payload": {"data": [item]}}
        resp = await client.post(self.URL, json=payload, headers=source_headers)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_date_in_item_returns_422(
        self, client: AsyncClient, mock_no_pressure, mock_task, source_headers: dict
    ):
        item = {k: v for k, v in VALID_EQUITIE_PAYLOAD["payload"]["data"][0].items() if k != "date"}
        payload = {**VALID_EQUITIE_PAYLOAD, "payload": {"data": [item]}}
        resp = await client.post(self.URL, json=payload, headers=source_headers)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_non_numeric_open_returns_422(
        self, client: AsyncClient, mock_no_pressure, mock_task, source_headers: dict
    ):
        resp = await client.post(
            self.URL, json=self._item_payload(open="not-a-number"), headers=source_headers
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_fetched_at_returns_422(
        self, client: AsyncClient, mock_no_pressure, mock_task, source_headers: dict
    ):
        payload = {k: v for k, v in VALID_EQUITIE_PAYLOAD.items() if k != "fetched_at"}
        resp = await client.post(self.URL, json=payload, headers=source_headers)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_empty_data_list_is_accepted(
        self, client: AsyncClient, mock_no_pressure, mock_task, source_headers: dict
    ):
        payload = {**VALID_EQUITIE_PAYLOAD, "payload": {"data": []}}
        resp = await client.post(self.URL, json=payload, headers=source_headers)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Celery dispatch
# ---------------------------------------------------------------------------


class TestV2Dispatch:
    @pytest.mark.asyncio
    async def test_successful_dispatch_calls_apply_async(
        self, client: AsyncClient, mock_no_pressure, mock_task, source_headers: dict
    ):
        resp = await client.post(
            f"{BASE}/ingest/usstock/direct", json=VALID_EQUITIE_PAYLOAD, headers=source_headers
        )
        assert resp.status_code == 200
        mock_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_apply_async_receives_correct_queue(
        self, client: AsyncClient, mock_no_pressure, mock_task, source_headers: dict
    ):
        await client.post(
            f"{BASE}/ingest/usstock/direct", json=VALID_EQUITIE_PAYLOAD, headers=source_headers
        )
        _, kwargs = mock_task.call_args
        assert kwargs["queue"] == "raw_data_ingest"

    @pytest.mark.asyncio
    async def test_broker_unavailable_returns_503(
        self, client: AsyncClient, mock_no_pressure, source_headers: dict
    ):
        with patch(
            "app.api.v2.source.process_ingestion_task.apply_async",
            side_effect=KombuError("connection refused"),
        ):
            resp = await client.post(
                f"{BASE}/ingest/hkchina/direct",
                json=VALID_EQUITIE_PAYLOAD,
                headers=source_headers,
            )
        assert resp.status_code == 503
        assert "unavailable" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_broker_unavailable_does_not_leak_internal_error(
        self, client: AsyncClient, mock_no_pressure, source_headers: dict
    ):
        with patch(
            "app.api.v2.source.process_ingestion_task.apply_async",
            side_effect=Exception("internal broker detail"),
        ):
            resp = await client.post(
                f"{BASE}/ingest/hkchina/direct",
                json=VALID_EQUITIE_PAYLOAD,
                headers=source_headers,
            )
        assert "internal broker detail" not in resp.text


# ---------------------------------------------------------------------------
# RabbitMQ pressure guard (429)
# ---------------------------------------------------------------------------


class TestV2PressureGuard:
    # Use a DirectIngestPayload endpoint so VALID_PAYLOAD is accepted.
    # Pressure guard fires before body parsing, so endpoint type doesn't
    # affect the 429 tests — but the 200/fail-open tests need a valid body.
    URL = f"{BASE}/ingest/crypto/direct"

    @pytest.mark.asyncio
    async def test_high_pressure_returns_429(
        self, client: AsyncClient, mock_task, source_headers: dict
    ):
        with patch(
            "app.api.v2.source._poll_rabbitmq_pressure",
            new_callable=AsyncMock,
            return_value=True,
        ):
            resp = await client.post(self.URL, json=VALID_PAYLOAD, headers=source_headers)

        assert resp.status_code == 429

    @pytest.mark.asyncio
    async def test_high_pressure_includes_retry_after_30(
        self, client: AsyncClient, mock_task, source_headers: dict
    ):
        with patch(
            "app.api.v2.source._poll_rabbitmq_pressure",
            new_callable=AsyncMock,
            return_value=True,
        ):
            resp = await client.post(self.URL, json=VALID_PAYLOAD, headers=source_headers)

        assert resp.headers.get("Retry-After") == "30"

    @pytest.mark.asyncio
    async def test_high_pressure_does_not_dispatch_task(
        self, client: AsyncClient, mock_task, source_headers: dict
    ):
        with patch(
            "app.api.v2.source._poll_rabbitmq_pressure",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await client.post(self.URL, json=VALID_PAYLOAD, headers=source_headers)

        mock_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_management_api_unreachable_is_fail_open(
        self, client: AsyncClient, mock_task, source_headers: dict
    ):
        """_poll_rabbitmq_pressure returns False when management API is down."""
        with patch(
            "app.api.v2.source._poll_rabbitmq_pressure",
            new_callable=AsyncMock,
            return_value=False,
        ):
            resp = await client.post(self.URL, json=VALID_PAYLOAD, headers=source_headers)

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_cache_prevents_second_poll_within_ttl(
        self, client: AsyncClient, mock_task, source_headers: dict
    ):
        poll_mock = AsyncMock(return_value=False)
        with patch("app.api.v2.source._poll_rabbitmq_pressure", poll_mock):
            await client.post(self.URL, json=VALID_PAYLOAD, headers=source_headers)
            await client.post(self.URL, json=VALID_PAYLOAD, headers=source_headers)

        poll_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_cache_expired_triggers_new_poll(
        self, client: AsyncClient, mock_task, source_headers: dict
    ):
        poll_mock = AsyncMock(return_value=False)
        with patch("app.api.v2.source._poll_rabbitmq_pressure", poll_mock):
            await client.post(self.URL, json=VALID_PAYLOAD, headers=source_headers)
            v2_source._pressure_expires_at = time.monotonic() - 1.0  # force expiry
            await client.post(self.URL, json=VALID_PAYLOAD, headers=source_headers)

        assert poll_mock.call_count == 2

    @pytest.mark.asyncio
    async def test_cached_pressure_blocks_without_new_poll(
        self, client: AsyncClient, mock_task, source_headers: dict
    ):
        """If the cache says pressured=True, 429 is returned without polling again."""
        poll_mock = AsyncMock(return_value=False)
        with patch("app.api.v2.source._poll_rabbitmq_pressure", poll_mock):
            v2_source._pressure_pressured = True
            v2_source._pressure_expires_at = time.monotonic() + 60.0

            resp = await client.post(self.URL, json=VALID_PAYLOAD, headers=source_headers)

        assert resp.status_code == 429
        poll_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------


class TestV2ResponseShape:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("url,dataset_key,payload", ALL_ENDPOINTS_WITH_PAYLOAD)
    async def test_dataset_key_per_endpoint(
        self,
        client: AsyncClient,
        mock_no_pressure,
        mock_task,
        source_headers: dict,
        url: str,
        dataset_key: str,
        payload: dict,
    ):
        resp = await client.post(url, json=payload, headers=source_headers)
        assert resp.status_code == 200
        envelope = mock_task.call_args.kwargs["args"][0]
        assert envelope["dataset_key"] == dataset_key

    @pytest.mark.asyncio
    async def test_items_reflects_data_length(
        self, client: AsyncClient, mock_no_pressure, mock_task, source_headers: dict
    ):
        data = [
            {
                "ticker": f"T{i} US Equity",
                "date": f"2026-01-{i+1:02d}",
                "open": 1.0,
                "high": 2.0,
                "low": 0.5,
                "close": 1.5,
                "volume": 100,
            }
            for i in range(7)
        ]
        await client.post(
            f"{BASE}/ingest/crypto/direct",
            json={"metadata": VALID_PAYLOAD["metadata"], "data": data},
            headers=source_headers,
        )
        envelope = mock_task.call_args.kwargs["args"][0]
        assert len(envelope["payload"]["data"]) == 7

    @pytest.mark.asyncio
    async def test_response_status_is_queued(
        self, client: AsyncClient, mock_no_pressure, mock_task, source_headers: dict
    ):
        resp = await client.post(
            f"{BASE}/ingest/crypto/direct", json=VALID_PAYLOAD, headers=source_headers
        )
        assert resp.json()["status"] == "queued"

    @pytest.mark.asyncio
    async def test_message_id_is_valid_uuid(
        self, client: AsyncClient, mock_no_pressure, mock_task, source_headers: dict
    ):
        resp = await client.post(
            f"{BASE}/ingest/crypto/direct", json=VALID_PAYLOAD, headers=source_headers
        )
        UUID(resp.json()["message_id"])  # raises ValueError if malformed

    @pytest.mark.asyncio
    async def test_identical_payloads_produce_same_request_key(
        self, client: AsyncClient, mock_no_pressure, source_headers: dict
    ):
        """Two identical payloads must generate the same request_key in the envelope."""
        captured: list[str] = []
        original = v2_source._build_request_key

        def spy(raw_payload: dict, key_prefix: str) -> str:
            key = original(raw_payload, key_prefix)
            captured.append(key)
            return key

        with patch("app.api.v2.source._build_request_key", side_effect=spy):
            with patch("app.api.v2.source.process_ingestion_task.apply_async"):
                await client.post(
                    f"{BASE}/ingest/crypto/direct", json=VALID_PAYLOAD, headers=source_headers
                )
                await client.post(
                    f"{BASE}/ingest/crypto/direct", json=VALID_PAYLOAD, headers=source_headers
                )

        assert len(captured) == 2
        assert captured[0] == captured[1]

    @pytest.mark.asyncio
    async def test_different_payloads_produce_different_request_keys(
        self, client: AsyncClient, mock_no_pressure, source_headers: dict
    ):
        captured: list[str] = []
        original = v2_source._build_request_key

        def spy(raw_payload: dict, key_prefix: str) -> str:
            key = original(raw_payload, key_prefix)
            captured.append(key)
            return key

        payload_b = {
            **VALID_PAYLOAD,
            "data": [{**VALID_PAYLOAD["data"][0], "close": 999.0}],
        }

        with patch("app.api.v2.source._build_request_key", side_effect=spy):
            with patch("app.api.v2.source.process_ingestion_task.apply_async"):
                await client.post(
                    f"{BASE}/ingest/crypto/direct", json=VALID_PAYLOAD, headers=source_headers
                )
                await client.post(
                    f"{BASE}/ingest/crypto/direct", json=payload_b, headers=source_headers
                )

        assert captured[0] != captured[1]


# ---------------------------------------------------------------------------
# _poll_rabbitmq_pressure unit tests
#
# Strategy: mock `_http_get_json` directly so no real RabbitMQ is needed.
# The function is called twice per poll (queue endpoint, then nodes endpoint),
# so side_effect=[queue_response, nodes_response] controls both calls.
#
# Default settings used here:
#   RABBITMQ_QUEUE_MAX_MESSAGES = 10000
#   RABBITMQ_PRESSURE_THRESHOLD = 0.8   (80%)
# ---------------------------------------------------------------------------

_QUEUE_OK = {"messages": 7999, "arguments": {}}  # 79.99% — under threshold
_QUEUE_HIGH = {"messages": 8001, "arguments": {}}  # 80.01% — over threshold
_NODES_OK = [{"name": "rabbit@node1", "mem_used": 799, "mem_limit": 1000}]  # 79.9%
_NODES_HIGH = [{"name": "rabbit@node1", "mem_used": 801, "mem_limit": 1000}]  # 80.1%


class TestPollRabbitmqPressure:
    """Unit tests for _poll_rabbitmq_pressure(); _http_get_json is always mocked."""

    # ------------------------------------------------------------------
    # Queue depth
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_queue_depth_above_threshold_returns_true(self):
        with patch("app.api.v2.source._http_get_json", side_effect=[_QUEUE_HIGH, _NODES_OK]):
            result = await v2_source._poll_rabbitmq_pressure()
        assert result is True

    @pytest.mark.asyncio
    async def test_queue_depth_below_threshold_returns_false(self):
        with patch("app.api.v2.source._http_get_json", side_effect=[_QUEUE_OK, _NODES_OK]):
            result = await v2_source._poll_rabbitmq_pressure()
        assert result is False

    @pytest.mark.asyncio
    async def test_queue_depth_uses_x_max_length_when_set(self):
        # x-max-length=500, 401 messages → 80.2% → pressured
        queue = {"messages": 401, "arguments": {"x-max-length": 500}}
        with patch("app.api.v2.source._http_get_json", side_effect=[queue, _NODES_OK]):
            result = await v2_source._poll_rabbitmq_pressure()
        assert result is True

    @pytest.mark.asyncio
    async def test_queue_depth_falls_back_to_settings_max_messages(self):
        # No x-max-length → uses RABBITMQ_QUEUE_MAX_MESSAGES (10000)
        # 7999/10000 = 79.99% → not pressured
        queue = {"messages": 7999, "arguments": {}}
        with patch("app.api.v2.source._http_get_json", side_effect=[queue, _NODES_OK]):
            result = await v2_source._poll_rabbitmq_pressure()
        assert result is False

    # ------------------------------------------------------------------
    # Node memory
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_node_memory_above_threshold_returns_true(self):
        with patch("app.api.v2.source._http_get_json", side_effect=[_QUEUE_OK, _NODES_HIGH]):
            result = await v2_source._poll_rabbitmq_pressure()
        assert result is True

    @pytest.mark.asyncio
    async def test_node_memory_below_threshold_returns_false(self):
        with patch("app.api.v2.source._http_get_json", side_effect=[_QUEUE_OK, _NODES_OK]):
            result = await v2_source._poll_rabbitmq_pressure()
        assert result is False

    @pytest.mark.asyncio
    async def test_any_node_above_threshold_returns_true(self):
        nodes = [
            {"name": "rabbit@node1", "mem_used": 100, "mem_limit": 1000},  # 10%
            {"name": "rabbit@node2", "mem_used": 810, "mem_limit": 1000},  # 81%
        ]
        with patch("app.api.v2.source._http_get_json", side_effect=[_QUEUE_OK, nodes]):
            result = await v2_source._poll_rabbitmq_pressure()
        assert result is True

    # ------------------------------------------------------------------
    # Fail-open: management API unreachable or broken
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_queue_api_returns_none_skips_to_node_check(self):
        # Queue call returns None → skip queue check → check nodes (ok) → False
        with patch("app.api.v2.source._http_get_json", side_effect=[None, _NODES_OK]):
            result = await v2_source._poll_rabbitmq_pressure()
        assert result is False

    @pytest.mark.asyncio
    async def test_both_api_calls_return_none_returns_false(self):
        with patch("app.api.v2.source._http_get_json", return_value=None):
            result = await v2_source._poll_rabbitmq_pressure()
        assert result is False

    @pytest.mark.asyncio
    async def test_exception_in_http_call_returns_false(self):
        # asyncio.to_thread propagates the exception; outer try/except catches it
        with patch("app.api.v2.source._http_get_json", side_effect=RuntimeError("boom")):
            result = await v2_source._poll_rabbitmq_pressure()
        assert result is False
