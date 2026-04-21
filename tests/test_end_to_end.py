"""
End-to-end test: Source -> Normalize -> Serve.
"""

import asyncio
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

from app.models.canonical import MarketDataEOD
from app.models.registry import DatasetRegistry, IngestionRun


@pytest.mark.asyncio
async def test_end_to_end_ingest_to_serve(client: AsyncClient, test_session):
    """Ingest raw payload and verify Serve API returns normalized data."""
    dataset = DatasetRegistry(
        dataset_key="crypto_eod",
        name="Crypto EOD",
        asset_class="crypto",
        market="CRYPTO",
        frequency="daily",
        is_active=True,
        config={
            "source_format": "bloomberg_crypto",
            "field_mapping": {
                "open": "price.open",
                "high": "price.high",
                "low": "price.low",
                "close": "price.last",
                "trade_date": "timestamp.last_update",
            },
            "identifier_type": "bloomberg",
            "identifier_field": "ticker",
        },
    )
    test_session.add(dataset)
    await test_session.commit()

    payload = {
        "dataset_key": "crypto_eod",
        "source": "bloomberg",
        "request_key": "e2e_request",
        "idempotency_key": "e2e_request",
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

    response = await client.post(
        "/api/v1/source/ingest/crypto",
        headers={"X-API-Key": "test-source-key"},
        json=payload,
    )
    assert response.status_code == 200
    run_id = response.json()["run_id"]

    # Wait for background normalization
    for _ in range(20):
        run = await test_session.get(IngestionRun, run_id)
        if run and run.status in {"completed", "completed_with_errors", "failed"}:
            break
        await asyncio.sleep(0.05)

    run = await test_session.get(IngestionRun, run_id)
    assert run is not None
    assert run.status in {"completed", "completed_with_errors"}

    result = await test_session.execute(MarketDataEOD.__table__.select().limit(1))
    assert result.first() is not None

    serve_response = await client.get(
        "/api/v1/serve/eod?market=CRYPTO&symbols=BTC&start_date=2026-01-16&end_date=2026-01-16"
    )
    assert serve_response.status_code == 200
    data = serve_response.json()
    assert data["success"] is True
    assert len(data["data"]) == 1
    assert data["data"][0]["symbol"] == "BTC"
