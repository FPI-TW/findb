"""
Tests for Source API endpoints.
"""

from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.config import get_settings
from app.models.raw import RawMarketPayload
from app.models.registry import DatasetRegistry, IngestionRun
from app.utils import uuid7

settings = get_settings()


class TestSourceAPI:
    """Tests for Source API."""

    @pytest.mark.asyncio
    async def test_ingest_without_api_key(self, client: AsyncClient):
        """Test ingestion without API key returns 401."""
        response = await client.post(
            "/api/v1/source/ingest/crypto",
            json={
                "dataset_key": "crypto_eod",
                "source": "bloomberg",
                "request_key": "test_request_1",
                "idempotency_key": "test_request_1",
                "payload": {"data": []},
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_ingest_with_invalid_api_key(self, client: AsyncClient):
        """Test ingestion with invalid API key returns 403."""
        response = await client.post(
            "/api/v1/source/ingest/crypto",
            headers={settings.API_KEY_HEADER: "invalid-key"},
            json={
                "dataset_key": "crypto_eod",
                "source": "bloomberg",
                "request_key": "test_request_1",
                "idempotency_key": "test_request_1",
                "payload": {"data": []},
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_ingest_rejects_non_allowlisted_client(
        self,
        client: AsyncClient,
        source_headers: dict,
    ):
        original_allowlist = settings.SOURCE_ALLOWLIST_CIDRS
        settings.SOURCE_ALLOWLIST_CIDRS = "10.0.0.0/8"

        try:
            response = await client.post(
                "/api/v1/source/ingest/crypto",
                headers=source_headers,
                json={
                    "dataset_key": "unknown_dataset",
                    "source": "bloomberg",
                    "request_key": "allowlist_block",
                    "idempotency_key": "allowlist_block",
                    "payload": {"data": []},
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            assert response.status_code == 403
            assert "allowlisted" in response.json()["detail"].lower()
        finally:
            settings.SOURCE_ALLOWLIST_CIDRS = original_allowlist

    @pytest.mark.asyncio
    async def test_ingest_rate_limit_returns_429(
        self,
        client: AsyncClient,
        source_headers: dict,
    ):
        original_limit = settings.RATE_LIMIT_REQUESTS
        original_window = settings.RATE_LIMIT_WINDOW
        original_allowlist = settings.SOURCE_ALLOWLIST_CIDRS
        settings.RATE_LIMIT_REQUESTS = 1
        settings.RATE_LIMIT_WINDOW = 60
        settings.SOURCE_ALLOWLIST_CIDRS = ""

        body = {
            "dataset_key": "unknown_dataset",
            "source": "bloomberg",
            "request_key": "rl_req",
            "idempotency_key": "rl_req",
            "payload": {"data": []},
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            first = await client.post(
                "/api/v1/source/ingest/crypto",
                headers=source_headers,
                json=body,
            )
            assert first.status_code == 400

            second = await client.post(
                "/api/v1/source/ingest/crypto",
                headers=source_headers,
                json={**body, "request_key": "rl_req_2", "idempotency_key": "rl_req_2"},
            )
            assert second.status_code == 429
            assert "rate limit" in second.json()["detail"].lower()
        finally:
            settings.RATE_LIMIT_REQUESTS = original_limit
            settings.RATE_LIMIT_WINDOW = original_window
            settings.SOURCE_ALLOWLIST_CIDRS = original_allowlist

    @pytest.mark.asyncio
    async def test_ingest_requires_allowlist_in_production(
        self,
        client: AsyncClient,
        source_headers: dict,
    ):
        original_debug = settings.DEBUG
        original_allowlist = settings.SOURCE_ALLOWLIST_CIDRS
        settings.DEBUG = False
        settings.SOURCE_ALLOWLIST_CIDRS = ""

        try:
            response = await client.post(
                "/api/v1/source/ingest/crypto",
                headers=source_headers,
                json={
                    "dataset_key": "unknown_dataset",
                    "source": "bloomberg",
                    "request_key": "prod_allowlist_required",
                    "idempotency_key": "prod_allowlist_required",
                    "payload": {"data": []},
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            assert response.status_code == 500
            assert "source_allowlist_cidrs" in response.json()["detail"].lower()
        finally:
            settings.DEBUG = original_debug
            settings.SOURCE_ALLOWLIST_CIDRS = original_allowlist

    @pytest.mark.asyncio
    async def test_ingest_unknown_dataset_returns_400(
        self,
        client: AsyncClient,
        source_headers: dict,
    ):
        """Test ingestion with unknown dataset returns 400."""
        response = await client.post(
            "/api/v1/source/ingest/crypto",
            headers=source_headers,
            json={
                "dataset_key": "unknown_dataset",
                "source": "bloomberg",
                "request_key": "test_request_unknown",
                "idempotency_key": "test_request_unknown",
                "payload": {"data": []},
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        assert response.status_code == 400
        assert "not found" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_ingest_inactive_dataset_returns_400(
        self,
        client: AsyncClient,
        source_headers: dict,
        test_session,
    ):
        """Test ingestion with inactive dataset returns 400."""
        dataset = DatasetRegistry(
            dataset_key="inactive_dataset",
            name="Inactive Dataset",
            asset_class="crypto",
            market="CRYPTO",
            frequency="daily",
            is_active=False,
        )
        test_session.add(dataset)
        await test_session.commit()

        response = await client.post(
            "/api/v1/source/ingest/crypto",
            headers=source_headers,
            json={
                "dataset_key": "inactive_dataset",
                "source": "bloomberg",
                "request_key": "test_request_inactive",
                "idempotency_key": "test_request_inactive",
                "payload": {"data": []},
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        assert response.status_code == 400
        assert "inactive" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_ingest_success_returns_run_id(
        self,
        client: AsyncClient,
        source_headers: dict,
        test_session,
    ):
        """Test ingestion succeeds with active dataset and payload."""
        dataset = DatasetRegistry(
            dataset_key="crypto_eod",
            name="Crypto EOD",
            asset_class="crypto",
            market="CRYPTO",
            frequency="daily",
            is_active=True,
        )
        test_session.add(dataset)
        await test_session.commit()

        response = await client.post(
            "/api/v1/source/ingest/crypto",
            headers=source_headers,
            json={
                "dataset_key": "crypto_eod",
                "source": "bloomberg",
                "request_key": "test_request_success",
                "idempotency_key": "test_request_success",
                "payload": {
                    "metadata": {"source": "Bloomberg API"},
                    "data": [
                        {
                            "symbol": "BTC",
                            "ticker": "XBTUSD BGN Curncy",
                            "price": {
                                "open": 100.0,
                                "high": 110.0,
                                "low": 90.0,
                                "last": 105.0,
                            },
                            "timestamp": {"last_update": "2026-01-16"},
                            "metadata": {"source": "Bloomberg"},
                        }
                    ],
                },
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        run_id = data["run_id"]

        run = await test_session.get(IngestionRun, run_id)
        assert run is not None
        assert run.dataset_key == "crypto_eod"

        raw_stmt = select(RawMarketPayload).where(
            RawMarketPayload.dataset_key == "crypto_eod",
            RawMarketPayload.idempotency_key == "test_request_success",
        )
        raw_result = await test_session.execute(raw_stmt)
        assert raw_result.scalar_one_or_none() is not None

    @pytest.mark.asyncio
    async def test_ingest_duplicate_idempotency_returns_existing_run(
        self,
        client: AsyncClient,
        source_headers: dict,
        test_session,
    ):
        """Ensure duplicate idempotency key returns existing run."""
        dataset = DatasetRegistry(
            dataset_key="crypto_eod",
            name="Crypto EOD",
            asset_class="crypto",
            market="CRYPTO",
            frequency="daily",
            is_active=True,
        )
        test_session.add(dataset)
        await test_session.commit()

        payload = {
            "dataset_key": "crypto_eod",
            "source": "bloomberg",
            "request_key": "dup_request",
            "idempotency_key": "dup_request",
            "payload": {
                "metadata": {"source": "Bloomberg API"},
                "data": [
                    {
                        "symbol": "BTC",
                        "ticker": "XBTUSD BGN Curncy",
                        "price": {
                            "open": 100.0,
                            "high": 110.0,
                            "low": 90.0,
                            "last": 101.0,
                        },
                        "timestamp": {"last_update": "2026-01-16"},
                        "metadata": {"source": "Bloomberg"},
                    }
                ],
            },
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

        first = await client.post(
            "/api/v1/source/ingest/crypto",
            headers=source_headers,
            json=payload,
        )
        assert first.status_code == 200
        first_payload = first.json()
        first_run_id = first_payload["run_id"]

        second = await client.post(
            "/api/v1/source/ingest/crypto",
            headers=source_headers,
            json=payload,
        )
        assert second.status_code == 200
        second_payload = second.json()
        assert second_payload["success"] is True
        assert second_payload["run_id"] == first_run_id

    @pytest.mark.asyncio
    async def test_rerun_from_raw_success(
        self,
        client: AsyncClient,
        source_headers: dict,
        test_session,
    ):
        """Ensure rerun creates a new ingestion run from raw payload."""
        dataset = DatasetRegistry(
            dataset_key="crypto_eod",
            name="Crypto EOD",
            asset_class="crypto",
            market="CRYPTO",
            frequency="daily",
            is_active=True,
        )
        test_session.add(dataset)
        await test_session.commit()

        payload = {
            "dataset_key": "crypto_eod",
            "source": "bloomberg",
            "request_key": "test_request_rerun",
            "idempotency_key": "test_request_rerun",
            "payload": {
                "metadata": {"source": "Bloomberg API"},
                "data": [
                    {
                        "symbol": "BTC",
                        "ticker": "XBTUSD BGN Curncy",
                        "price": {
                            "open": 100.0,
                            "high": 110.0,
                            "low": 90.0,
                            "last": 105.0,
                        },
                        "timestamp": {"last_update": "2026-01-16"},
                        "metadata": {"source": "Bloomberg"},
                    }
                ],
            },
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

        ingest_response = await client.post(
            "/api/v1/source/ingest/crypto",
            headers=source_headers,
            json=payload,
        )
        assert ingest_response.status_code == 200
        ingest_data = ingest_response.json()
        original_run_id = ingest_data["run_id"]

        rerun_response = await client.post(
            f"/api/v1/source/runs/{original_run_id}/rerun",
            headers=source_headers,
        )
        assert rerun_response.status_code == 200
        rerun_data = rerun_response.json()
        new_run_id = rerun_data["run_id"]
        assert new_run_id != original_run_id

        new_run = await test_session.get(IngestionRun, new_run_id)
        assert new_run is not None
        assert new_run.dataset_key == "crypto_eod"
        assert new_run.metadata_ is not None
        assert new_run.metadata_.get("rerun_from_run_id") == str(original_run_id)
        assert new_run.metadata_.get("rerun_from_idempotency_key") == "test_request_rerun"

    @pytest.mark.asyncio
    async def test_rerun_from_raw_not_found(
        self,
        client: AsyncClient,
        source_headers: dict,
    ):
        """Ensure rerun returns 404 when raw payload is missing."""
        missing_run_id = uuid7()
        response = await client.post(
            f"/api/v1/source/runs/{missing_run_id}/rerun",
            headers=source_headers,
        )
        assert response.status_code == 404
        assert "raw payload" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_ingest_invalid_payload_creates_failed_run(
        self,
        client: AsyncClient,
        source_headers: dict,
        test_session,
    ):
        """Ensure rejected payload still creates a failed ingestion run."""
        dataset = DatasetRegistry(
            dataset_key="crypto_eod",
            name="Crypto EOD",
            asset_class="crypto",
            market="CRYPTO",
            frequency="daily",
            is_active=True,
        )
        test_session.add(dataset)
        await test_session.commit()

        response = await client.post(
            "/api/v1/source/ingest/crypto",
            headers=source_headers,
            json={
                "dataset_key": "crypto_eod",
                "source": "bloomberg",
                "request_key": "reject_request",
                "idempotency_key": "reject_request",
                "payload": {"data": [{"symbol": "BTC"}]},
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        assert response.status_code == 400
        assert "required field" in response.json()["detail"].lower()

        stmt = select(IngestionRun).where(IngestionRun.request_key == "reject_request")
        result = await test_session.execute(stmt)
        run = result.scalar_one_or_none()

        assert run is not None
        assert run.status == "failed"
        assert run.source == "bloomberg"
        assert run.raw_records == 0
        assert "required field" in (run.error_message or "").lower()

    @pytest.mark.asyncio
    async def test_ingest_market_mismatch_returns_400(
        self,
        client: AsyncClient,
        source_headers: dict,
        test_session,
    ):
        """Ensure dataset market must match market-specific ingest endpoint."""
        dataset = DatasetRegistry(
            dataset_key="us_equity_eod",
            name="US Equity EOD",
            asset_class="equity",
            market="US",
            frequency="daily",
            is_active=True,
        )
        test_session.add(dataset)
        await test_session.commit()

        response = await client.post(
            "/api/v1/source/ingest/crypto",
            headers=source_headers,
            json={
                "dataset_key": "us_equity_eod",
                "source": "bloomberg",
                "request_key": "market_mismatch_request",
                "idempotency_key": "market_mismatch_request",
                "payload": {"data": [{"date": "2026-01-16"}]},
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        assert response.status_code == 400
        assert "belongs to market us" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_ingest_usstock_direct_success_and_deduplicate(
        self,
        client: AsyncClient,
        source_headers: dict,
        test_session,
    ):
        """Ensure US stock direct payload format can be ingested and deduplicated."""
        dataset = DatasetRegistry(
            dataset_key="us_stock_eod",
            name="US Stock EOD",
            asset_class="equity",
            market="US",
            frequency="daily",
            is_active=True,
            config={
                "data_path": "data",
                "field_mapping": {
                    "trade_date": "timestamp.query_time",
                    "open": "price.open",
                    "high": "price.high",
                    "low": "price.low",
                    "close": "price.last",
                    "volume": "price.volume",
                },
            },
        )
        test_session.add(dataset)
        await test_session.commit()

        payload = {
            "metadata": {
                "source": "Bloomberg API",
                "category": "US Stock",
                "query_time": "2026-02-04T16:00:51.164789",
                "total_records": 1,
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
                    "timestamp": {
                        "query_time": "2026-02-04T16:00:51.151296",
                        "last_update": "2026-02-04",
                    },
                    "metadata": {"source": "Bloomberg", "data_type": "stock"},
                }
            ],
        }

        first_response = await client.post(
            "/api/v1/source/ingest/usstock/direct",
            headers=source_headers,
            json=payload,
        )
        assert first_response.status_code == 200
        first_data = first_response.json()
        first_run_id = first_data["run_id"]

        second_response = await client.post(
            "/api/v1/source/ingest/usstock/direct",
            headers=source_headers,
            json=payload,
        )
        assert second_response.status_code == 200
        second_data = second_response.json()
        assert second_data["run_id"] == first_run_id

        run = await test_session.get(IngestionRun, first_run_id)
        assert run is not None
        assert run.dataset_key == "us_stock_eod"

    @pytest.mark.asyncio
    async def test_ingest_hkchina_direct_success(
        self,
        client: AsyncClient,
        source_headers: dict,
        test_session,
    ):
        """Ensure HK/China direct payload format can be ingested."""
        dataset = DatasetRegistry(
            dataset_key="hkchina_stock_eod",
            name="HK China Stock EOD",
            asset_class="equity",
            market="GLOBAL",
            frequency="daily",
            is_active=True,
            config={
                "data_path": "data",
                "field_mapping": {
                    "trade_date": "timestamp.query_time",
                    "open": "price.open",
                    "high": "price.high",
                    "low": "price.low",
                    "close": "price.last",
                    "volume": "price.volume",
                },
            },
        )
        test_session.add(dataset)
        await test_session.commit()

        response = await client.post(
            "/api/v1/source/ingest/hkchina/direct",
            headers=source_headers,
            json={
                "metadata": {
                    "source": "Bloomberg API",
                    "category": "HK/China Stock",
                    "query_time": "2026-02-09T15:55:22.900700",
                    "total_records": 1,
                },
                "data": [
                    {
                        "stock_id": "700",
                        "symbol": "700",
                        "name": "TENCENT HOLDINGS LTD",
                        "ticker": "700 HK Equity",
                        "market": "HK",
                        "price": {
                            "last": 560.0,
                            "open": 550.0,
                            "high": 562.5,
                            "low": 550.0,
                            "volume": 23494910.0,
                        },
                        "timestamp": {
                            "query_time": "2026-02-09T15:55:22.581572",
                            "last_update": "2026-02-09",
                        },
                        "metadata": {"source": "Bloomberg", "data_type": "stock"},
                    }
                ],
            },
        )

        assert response.status_code == 200
        data = response.json()
        run_id = data["run_id"]
        run = await test_session.get(IngestionRun, run_id)
        assert run is not None
        assert run.dataset_key == "hkchina_stock_eod"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("endpoint", "dataset_key", "market", "ticker"),
        [
            ("/api/v1/source/ingest/tw", "tw_index_eod", "TW", "TWII TT Index"),
            ("/api/v1/source/ingest/hk", "hk_index_eod", "HK", "HSI HK Index"),
            ("/api/v1/source/ingest/cn", "cn_index_eod", "CN", "SHCOMP CH Index"),
        ],
    )
    async def test_ingest_regional_index_success(
        self,
        endpoint: str,
        dataset_key: str,
        market: str,
        ticker: str,
        client: AsyncClient,
        source_headers: dict,
        test_session,
    ):
        """Ensure TW/HK/CN index datasets can be ingested from market-specific endpoints."""
        dataset = DatasetRegistry(
            dataset_key=dataset_key,
            name=f"{market} Index EOD",
            asset_class="index",
            market=market,
            frequency="daily",
            is_active=True,
            config={
                "data_path": "data",
                "field_mapping": {
                    "trade_date": "timestamp.query_time",
                    "open": "price.open",
                    "high": "price.high",
                    "low": "price.low",
                    "close": "price.last",
                    "volume": "price.volume",
                },
            },
        )
        test_session.add(dataset)
        await test_session.commit()

        response = await client.post(
            endpoint,
            headers=source_headers,
            json={
                "dataset_key": dataset_key,
                "source": "bloomberg",
                "request_key": f"{dataset_key}_request",
                "idempotency_key": f"{dataset_key}_request",
                "payload": {
                    "metadata": {"source": "Bloomberg API"},
                    "data": [
                        {
                            "symbol": market,
                            "name": f"{market} INDEX",
                            "ticker": ticker,
                            "price": {
                                "open": 100.0,
                                "high": 101.0,
                                "low": 99.0,
                                "last": 100.5,
                                "volume": 1000.0,
                            },
                            "timestamp": {"query_time": "2026-02-09T15:55:22.581572"},
                        }
                    ],
                },
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        assert response.status_code == 200
        run_id = response.json()["run_id"]
        run = await test_session.get(IngestionRun, run_id)
        assert run is not None
        assert run.dataset_key == dataset_key

    @pytest.mark.asyncio
    async def test_ingest_regional_index_market_mismatch_returns_400(
        self,
        client: AsyncClient,
        source_headers: dict,
        test_session,
    ):
        """Ensure regional index dataset must use the matching regional ingest endpoint."""
        dataset = DatasetRegistry(
            dataset_key="tw_index_eod",
            name="TW Index EOD",
            asset_class="index",
            market="TW",
            frequency="daily",
            is_active=True,
            config={"data_path": "data", "field_mapping": {"trade_date": "timestamp.query_time"}},
        )
        test_session.add(dataset)
        await test_session.commit()

        response = await client.post(
            "/api/v1/source/ingest/hk",
            headers=source_headers,
            json={
                "dataset_key": "tw_index_eod",
                "source": "bloomberg",
                "request_key": "tw_index_wrong_market",
                "idempotency_key": "tw_index_wrong_market",
                "payload": {
                    "data": [
                        {
                            "ticker": "TWII TT Index",
                            "timestamp": {"query_time": "2026-02-09T15:55:22.581572"},
                        }
                    ]
                },
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            },
        )

        assert response.status_code == 400
        assert "belongs to market tw" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_ingest_macro_direct_success(
        self,
        client: AsyncClient,
        source_headers: dict,
        test_session,
    ):
        """Ensure macro direct payload format can be ingested."""
        dataset = DatasetRegistry(
            dataset_key="macro_observation",
            name="Macro Observation",
            asset_class="macro",
            market="MACRO",
            frequency="varies",
            is_active=True,
            config={
                "data_path": "data",
                "field_mapping": {
                    "source_code": "source_code",
                    "obs_date": "date",
                    "value": "value",
                },
            },
        )
        test_session.add(dataset)
        await test_session.commit()

        response = await client.post(
            "/api/v1/source/ingest/macro/direct",
            headers=source_headers,
            json={
                "metadata": {
                    "source": "Bloomberg API",
                    "category": "Macro Economic",
                    "query_time": "2026-02-09T14:51:37.266145",
                    "total_records": 1,
                },
                "data": [
                    {
                        "index_id": "move",
                        "symbol": "MOVE",
                        "name": "MOVE",
                        "ticker": "MOVE Index",
                        "price": {"last": 63.62},
                        "timestamp": {
                            "query_time": "2026-02-09T14:51:35.347331",
                            "last_update": "2026-02-07",
                        },
                        "metadata": {"source": "Bloomberg", "data_type": "macro"},
                    }
                ],
            },
        )

        assert response.status_code == 200
        data = response.json()
        run_id = data["run_id"]
        run = await test_session.get(IngestionRun, run_id)
        assert run is not None
        assert run.dataset_key == "macro_observation"

    @pytest.mark.asyncio
    async def test_health_check(self, client: AsyncClient):
        """Test health check endpoint."""
        response = await client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "source_allowlist_configured" in data

    @pytest.mark.asyncio
    async def test_root_endpoint(self, client: AsyncClient):
        """Test root endpoint."""
        response = await client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert "name" in data
        assert "version" in data
