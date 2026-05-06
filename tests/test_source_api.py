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
    async def test_ingest_rate_limit_returns_429(
        self,
        client: AsyncClient,
        source_headers: dict,
    ):
        original_limit = settings.RATE_LIMIT_REQUESTS
        original_window = settings.RATE_LIMIT_WINDOW
        settings.RATE_LIMIT_REQUESTS = 1
        settings.RATE_LIMIT_WINDOW = 60

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
                "source": "bloomberg",
                "query_time": "2026-02-04T16:00:51.164789Z",
                "total_records": 1,
            },
            "data": [
                {
                    "ticker": "AAPL US Equity",
                    "date": "2026-02-04",
                    "open": 269.2,
                    "high": 271.875,
                    "low": 267.61,
                    "close": 269.48,
                    "volume": 64394655.0,
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
        """Ensure HK/China mixed direct payload persists both equities and indices."""
        response = await client.post(
            "/api/v1/source/ingest/hkchina/direct",
            headers=source_headers,
            json={
                "metadata": {
                    "source": "bloomberg",
                    "query_time": "2026-02-09T15:55:22.900700Z",
                    "total_records": 2,
                },
                "data": [
                    {
                        "ticker": "700 HK Equity",
                        "date": "2026-02-09",
                        "open": 550.0,
                        "high": 562.5,
                        "low": 550.0,
                        "close": 560.0,
                        "volume": 23494910.0,
                    },
                    {
                        "ticker": "SH000911 Index",
                        "date": "2026-02-09",
                        "open": 5901.0,
                        "high": 5968.4,
                        "low": 5899.5,
                        "close": 5950.8,
                        "volume": 0,
                    },
                ],
            },
        )

        assert response.status_code == 200
        run_id = response.json()["run_id"]
        run = await test_session.get(IngestionRun, run_id)
        assert run is not None
        assert run.dataset_key == "hkchina_mixed_eod"
        assert run.status == "completed"
        assert run.total_records == 2
        assert run.success_records == 2

        hk_equity_response = await client.get(
            "/api/v1/serve/instruments?market=HK&asset_class=equity&symbol=700"
        )
        assert hk_equity_response.status_code == 200
        hk_equities = hk_equity_response.json()["data"]
        assert len(hk_equities) == 1
        assert hk_equities[0]["symbol"] == "700"

        cn_index_response = await client.get(
            "/api/v1/serve/instruments?market=CN&asset_class=index&symbol=SH000911"
        )
        assert cn_index_response.status_code == 200
        cn_indices = cn_index_response.json()["data"]
        assert len(cn_indices) == 1
        assert cn_indices[0]["symbol"] == "SH000911"

    @pytest.mark.asyncio
    async def test_ingest_hkchina_index_direct_routes_records_to_hk_and_cn(
        self,
        client: AsyncClient,
        source_headers: dict,
        test_session,
    ):
        """Ensure HK/China index direct payloads infer market and persist as regional indices."""
        response = await client.post(
            "/api/v1/source/ingest/hkchina-index/direct",
            headers=source_headers,
            json={
                "metadata": {
                    "source": "bloomberg",
                    "query_time": "2026-04-08T02:29:42.484080Z",
                    "total_records": 2,
                },
                "data": [
                    {
                        "ticker": "HSI Index",
                        "date": "2026-04-09",
                        "open": 25772.56,
                        "high": 25872.30,
                        "low": 25711.23,
                        "close": 25741.55,
                        "volume": 0,
                    },
                    {
                        "ticker": "SH000911 Index",
                        "date": "2026-04-08",
                        "open": 5941.521,
                        "high": 5966.992,
                        "low": 5940.137,
                        "close": 5960.012,
                        "volume": 0,
                    },
                ],
            },
        )

        assert response.status_code == 200
        run_id = response.json()["run_id"]

        run = await test_session.get(IngestionRun, run_id)
        assert run is not None
        assert run.dataset_key == "hkchina_index_eod"
        assert run.status == "completed"
        assert run.total_records == 2
        assert run.success_records == 2
        assert run.failed_records == 0

        hk_inst_response = await client.get(
            "/api/v1/serve/instruments?market=HK&asset_class=index&symbol=HSI"
        )
        assert hk_inst_response.status_code == 200
        hk_instruments = hk_inst_response.json()["data"]
        assert len(hk_instruments) == 1
        assert hk_instruments[0]["symbol"] == "HSI"

        cn_inst_response = await client.get(
            "/api/v1/serve/instruments?market=CN&asset_class=index&symbol=SH000911"
        )
        assert cn_inst_response.status_code == 200
        cn_instruments = cn_inst_response.json()["data"]
        assert len(cn_instruments) == 1
        assert cn_instruments[0]["symbol"] == "SH000911"

        cn_eod_response = await client.get(
            "/api/v1/serve/eod?market=CN&symbols=SH000911&start_date=2026-04-08&end_date=2026-04-08"
        )
        assert cn_eod_response.status_code == 200
        cn_eod_data = cn_eod_response.json()["data"]
        assert len(cn_eod_data) == 1
        assert cn_eod_data[0]["close"] == "5960.01200000"

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
            dataset_key="macro_bloomberg_observation",
            name="Macro Bloomberg Observation",
            asset_class="macro",
            market="MACRO",
            frequency="varies",
            is_active=True,
            config={},
        )
        test_session.add(dataset)
        await test_session.commit()

        response = await client.post(
            "/api/v1/source/ingest/macro/direct",
            headers=source_headers,
            json={
                "metadata": {
                    "source": "bloomberg",
                    "query_time": "2026-02-09T14:51:37.266145Z",
                    "total_records": 1,
                },
                "data": [
                    {
                        "ticker": "MOVE Index",
                        "date": "2026-02-07",
                        "value": 63.62,
                    }
                ],
            },
        )

        assert response.status_code == 200
        data = response.json()
        run_id = data["run_id"]
        run = await test_session.get(IngestionRun, run_id)
        assert run is not None
        assert run.dataset_key == "macro_bloomberg_observation"

    @pytest.mark.asyncio
    async def test_ingest_twstock_direct_success_deduplicates_and_preserves_raw_payload(
        self,
        client: AsyncClient,
        source_headers: dict,
        test_session,
    ):
        """Ensure TW stock MultiCharts direct payloads ingest, deduplicate, and persist fields."""
        payload = {
            "metadata": {
                "symbol": "6160",
                "name": "欣技",
                "source": "multicharts",
                "file_name": "6160 1 日.txt",
                "query_time": "2026-04-30T08:00:00Z",
            },
            "data": [
                {
                    "<Date>": "2024-04-29",
                    "<Time>": "13:30:00",
                    "<Open>": 20.45,
                    "<High>": 21.50,
                    "<Low>": 20.45,
                    "<Close>": 21.10,
                    "<UpVolume>": 81,
                    "<DownVolume>": 36,
                    "<TotalVolume>": 528,
                    "<UpTicks>": 37,
                    "<DownTicks>": 19,
                    "<TotalTicks>": 221,
                    "<OpenInterest>": 0,
                }
            ],
        }

        first_response = await client.post(
            "/api/v1/source/ingest/twstock/direct",
            headers=source_headers,
            json=payload,
        )
        assert first_response.status_code == 200
        first_data = first_response.json()
        first_run_id = first_data["run_id"]

        second_response = await client.post(
            "/api/v1/source/ingest/twstock/direct",
            headers=source_headers,
            json=payload,
        )
        assert second_response.status_code == 200
        assert second_response.json()["run_id"] == first_run_id

        run = await test_session.get(IngestionRun, first_run_id)
        assert run is not None
        assert run.dataset_key == "tw_equity_multicharts_eod"
        assert run.status == "completed"
        assert run.total_records == 1
        assert run.success_records == 1

        dataset = await test_session.get(DatasetRegistry, "tw_equity_multicharts_eod")
        assert dataset is not None
        assert dataset.market == "TW"

        raw_stmt = select(RawMarketPayload).where(RawMarketPayload.run_id == first_run_id)
        raw_payload = (await test_session.execute(raw_stmt)).scalar_one()
        assert raw_payload.payload["metadata"]["symbol"] == "6160"
        assert raw_payload.payload["data"][0]["time"] == "13:30:00"
        assert raw_payload.payload["data"][0]["open_interest"] == 0

        eod_response = await client.get(
            "/api/v1/serve/eod?market=TW&symbols=6160&start_date=2024-04-29&end_date=2024-04-29"
        )
        assert eod_response.status_code == 200
        eod_payload = eod_response.json()["data"]
        assert len(eod_payload) == 1
        assert eod_payload[0]["volume"] == 528
        assert eod_payload[0]["up_volume"] == 81
        assert eod_payload[0]["down_volume"] == 36
        assert eod_payload[0]["up_ticks"] == 37
        assert eod_payload[0]["down_ticks"] == 19
        assert eod_payload[0]["total_ticks"] == 221
        assert "open_interest" not in eod_payload[0]

    @pytest.mark.asyncio
    async def test_ingest_twstock_direct_multicharts_daily_file_format_success(
        self,
        client: AsyncClient,
        source_headers: dict,
        test_session,
    ):
        """Ensure TW MultiCharts daily CSV-style rows can be ingested end-to-end."""
        response = await client.post(
            "/api/v1/source/ingest/twstock/direct",
            headers=source_headers,
            json={
                "metadata": {
                    "name": "富邦科技",
                    "source": "multicharts",
                    "file_name": "0052-Day-Trade.csv",
                    "query_time": "2025-05-05T13:30:00Z",
                },
                "data": [
                    {
                        "Symbol": "0052",
                        "Date": "2025/5/5",
                        "Open": 168.50,
                        "High": 168.50,
                        "Low": 162.75,
                        "Close": 164.90,
                        "UpVolume": 174,
                        "DownVolume": 225,
                        "TotalVolume": 1096,
                        "UpTicks": 46,
                        "DownTicks": 59,
                        "TotalTicks": 306,
                    }
                ],
            },
        )

        assert response.status_code == 200
        run_id = response.json()["run_id"]

        run = await test_session.get(IngestionRun, run_id)
        assert run is not None
        assert run.status == "completed"
        assert run.total_records == 1
        assert run.success_records == 1

        raw_stmt = select(RawMarketPayload).where(RawMarketPayload.run_id == run_id)
        raw_payload = (await test_session.execute(raw_stmt)).scalar_one()
        assert raw_payload.payload["data"][0]["symbol"] == "0052"
        assert raw_payload.payload["data"][0]["date"] == "2025/5/5"

        eod_response = await client.get(
            "/api/v1/serve/eod?market=TW&symbols=0052&start_date=2025-05-05&end_date=2025-05-05"
        )

        assert eod_response.status_code == 200
        eod_payload = eod_response.json()["data"]
        assert len(eod_payload) == 1
        assert eod_payload[0]["symbol"] == "0052"
        assert eod_payload[0]["name"] == "富邦科技"
        assert eod_payload[0]["trade_date"] == "2025-05-05"
        assert eod_payload[0]["open"] == "168.50000000"
        assert eod_payload[0]["high"] == "168.50000000"
        assert eod_payload[0]["low"] == "162.75000000"
        assert eod_payload[0]["close"] == "164.90000000"
        assert eod_payload[0]["volume"] == 1096
        assert eod_payload[0]["up_volume"] == 174
        assert eod_payload[0]["down_volume"] == 225
        assert eod_payload[0]["up_ticks"] == 46
        assert eod_payload[0]["down_ticks"] == 59
        assert eod_payload[0]["total_ticks"] == 306

    @pytest.mark.asyncio
    async def test_ingest_twstock_direct_missing_metadata_symbol_returns_400(
        self,
        client: AsyncClient,
        source_headers: dict,
    ):
        """Ensure TW stock direct payload requires metadata.symbol."""
        response = await client.post(
            "/api/v1/source/ingest/twstock/direct",
            headers=source_headers,
            json={
                "metadata": {
                    "source": "multicharts",
                    "query_time": "2025-05-05T13:30:00Z",
                },
                "data": [
                    {
                        "Date": "2025/5/5",
                        "Open": 168.50,
                        "High": 168.50,
                        "Low": 162.75,
                        "Close": 164.90,
                        "UpVolume": 174,
                        "DownVolume": 225,
                        "TotalVolume": 1096,
                        "UpTicks": 46,
                        "DownTicks": 59,
                        "TotalTicks": 306,
                    }
                ],
            },
        )

        assert response.status_code == 400
        assert response.json()["detail"] == "metadata.symbol or data[].symbol is required"

    @pytest.mark.asyncio
    async def test_ingest_twstock_direct_missing_required_row_field_returns_400(
        self,
        client: AsyncClient,
        source_headers: dict,
    ):
        """Ensure TW stock direct payload rejects rows with missing required fields."""
        response = await client.post(
            "/api/v1/source/ingest/twstock/direct",
            headers=source_headers,
            json={
                "metadata": {
                    "symbol": "6160",
                    "source": "multicharts",
                    "query_time": "2026-04-30T08:00:00Z",
                },
                "data": [
                    {
                        "date": "2024-04-29",
                        "time": "13:30:00",
                        "open": 20.45,
                        "high": 21.50,
                        "low": 20.45,
                        "close": 21.10,
                        "up_volume": 81,
                        "down_volume": 36,
                        "total_volume": 528,
                        "up_ticks": 37,
                        "down_ticks": 19,
                    }
                ],
            },
        )

        assert response.status_code == 400
        assert response.json()["detail"] == "data[0] missing required fields: total_ticks"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("endpoint", "dataset_key", "expected_market", "payload"),
        [
            (
                "/api/v1/source/ingest/crypto/direct",
                "crypto_bloomberg_eod",
                "CRYPTO",
                {
                    "metadata": {"source": "bloomberg", "query_time": "2026-03-12T08:00:00Z"},
                    "data": [
                        {
                            "ticker": "XBTUSD BGN Curncy",
                            "date": "2026-03-12",
                            "open": 95550.07,
                            "high": 95825.34,
                            "low": 95119.76,
                            "close": 95709.01,
                            "volume": 18500,
                        }
                    ],
                },
            ),
            (
                "/api/v1/source/ingest/fx/direct",
                "fx_bloomberg_eod",
                "FX",
                {
                    "metadata": {"source": "bloomberg", "query_time": "2026-03-12T08:00:00Z"},
                    "data": [
                        {
                            "pair": "EURUSD",
                            "ticker": "EURUSD Curncy",
                            "date": "2026-03-12",
                            "open": 1.0498,
                            "high": 1.0567,
                            "low": 1.0489,
                            "close": 1.0523,
                        }
                    ],
                },
            ),
            (
                "/api/v1/source/ingest/wtx/direct",
                "wtx_bloomberg_eod",
                "WTX",
                {
                    "metadata": {"source": "bloomberg", "query_time": "2026-03-12T08:00:00Z"},
                    "data": [
                        {
                            "symbol": "TXF1",
                            "ticker": "TXF1 Index",
                            "date": "2026-03-12",
                            "open": 20800,
                            "high": 21100,
                            "low": 20700,
                            "close": 21000,
                            "volume": 50000,
                        }
                    ],
                },
            ),
            (
                "/api/v1/source/ingest/macro/direct",
                "macro_bloomberg_observation",
                "MACRO",
                {
                    "metadata": {"source": "bloomberg", "query_time": "2026-03-12T08:00:00Z"},
                    "data": [
                        {
                            "ticker": "CPI YOY Index",
                            "date": "2026-01-01",
                            "value": 2.9,
                        }
                    ],
                },
            ),
        ],
    )
    async def test_direct_ingest_bootstraps_missing_builtin_dataset(
        self,
        endpoint: str,
        dataset_key: str,
        expected_market: str,
        payload: dict,
        client: AsyncClient,
        source_headers: dict,
        test_session,
    ):
        """Ensure direct ingest auto-registers missing built-in dataset definitions."""
        response = await client.post(
            endpoint,
            headers=source_headers,
            json=payload,
        )

        assert response.status_code == 200

        dataset = await test_session.get(DatasetRegistry, dataset_key)
        assert dataset is not None
        assert dataset.market == expected_market
        assert dataset.is_active is True

    @pytest.mark.asyncio
    async def test_health_check(self, client: AsyncClient):
        """Test health check endpoint."""
        response = await client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "source_allowlist_configured" not in data

    @pytest.mark.asyncio
    async def test_root_endpoint(self, client: AsyncClient):
        """Test root endpoint."""
        response = await client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert "name" in data
        assert "version" in data
