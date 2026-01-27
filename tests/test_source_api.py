"""
Tests for Source API endpoints.
"""

import pytest
from datetime import datetime, timezone
from httpx import AsyncClient

from sqlalchemy import select

from app.config import get_settings
from app.utils import uuid7
from app.models.raw import RawMarketPayload
from app.models.registry import DatasetRegistry, IngestionRun

settings = get_settings()


class TestSourceAPI:
    """Tests for Source API."""

    @pytest.mark.asyncio
    async def test_ingest_without_api_key(self, client: AsyncClient):
        """Test ingestion without API key returns 401."""
        response = await client.post(
            "/api/v1/source/ingest",
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
            "/api/v1/source/ingest",
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
    async def test_ingest_unknown_dataset_returns_400(
        self,
        client: AsyncClient,
        source_headers: dict,
    ):
        """Test ingestion with unknown dataset returns 400."""
        response = await client.post(
            "/api/v1/source/ingest",
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
            "/api/v1/source/ingest",
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
            "/api/v1/source/ingest",
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
            "/api/v1/source/ingest",
            headers=source_headers,
            json=payload,
        )
        assert first.status_code == 200
        first_payload = first.json()
        first_run_id = first_payload["run_id"]

        second = await client.post(
            "/api/v1/source/ingest",
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
            "/api/v1/source/ingest",
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
            "/api/v1/source/ingest",
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
    async def test_health_check(self, client: AsyncClient):
        """Test health check endpoint."""
        response = await client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_root_endpoint(self, client: AsyncClient):
        """Test root endpoint."""
        response = await client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert "name" in data
        assert "version" in data
