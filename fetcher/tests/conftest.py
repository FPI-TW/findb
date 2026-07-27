from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def contracts_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "contracts"


@pytest.fixture
def market_request() -> dict[str, Any]:
    return {
        "dataset_key": "tw_market_eod",
        "schema_id": "market_eod",
        "schema_version": 1,
        "source": "test_provider",
        "request_key": "test-provider:tw-market-eod:2026-07-23",
        "idempotency_key": "test-provider:tw-market-eod:2026-07-23:v1",
        "fetched_at": "2026-07-23T12:00:00Z",
        "payload": {
            "batch": {
                "data_date": "2026-07-23",
                "delivery_mode": "full_snapshot",
                "declared_record_count": 1,
            },
            "data": [
                {
                    "symbol": "2330",
                    "trade_date": "2026-07-23",
                    "close": "1135.0",
                    "currency": "TWD",
                }
            ],
        },
    }
