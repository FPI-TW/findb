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


@pytest.fixture(autouse=True)
def isolated_unit_work_boundary(request, monkeypatch, tmp_path):
    """Stub the separately integration-tested ownership boundary in layer unit tests.

    Account/SDK/mapper tests supply their own authorization and transport harness.
    Protocol, lifecycle, and retirement tests keep the real boundary untouched.
    """
    # Delivery transport tests trust the owner header boundary; dedicated protocol
    # tests verify its real behavior, including rejection before transport.
    from findb_fetcher import client as delivery_client

    if request.path.name in {"test_client.py", "test_twelve_data.py"}:
        monkeypatch.setattr(
            delivery_client,
            "_provider_work_headers",
            lambda delivery: {"X-FinDB-Work-Id": "owned-transport-fixture"},
        )
    modules = {
        "test_acquisition_boundary.py",
        "test_acquisition_repairs.py",
        "test_sdk_bootstrap.py",
        "test_local_account_config.py",
        "test_finlab.py",
        "test_finlab_file_cache.py",
        "test_finlab_dataframe_bytes.py",
        "test_finlab_native_identity.py",
        "test_shioaji.py",
        "test_twelve_data.py",
        "test_twelve_data_catalogue_scope.py",
        "test_taifex_dated_universe.py",
        "test_full_market.py",
        "test_full_market_governor.py",
        "test_full_market_finlab_rate.py",
        "test_shioaji_daily_runtime.py",
        "test_staging_pilots.py",
        "test_shioaji_staging.py",
        "test_shioaji_scheduler.py",
        "test_taifex_historical.py",
        "test_historical_runtime_adapters.py",
        "test_full_market_recovery.py",
        "test_full_market_idle.py",
        "test_execution_profile_parity.py",
        "test_historical_backfill.py",
    }
    if request.path.name not in modules:
        yield
        return
    from findb_fetcher import provider_work

    monkeypatch.setattr(provider_work, "require_owner", lambda provider: None)
    if request.path.name == "test_historical_runtime_adapters.py":
        monkeypatch.setattr("findb_fetcher.historical_runtime.require_owner", lambda provider: None)
    monkeypatch.setattr(provider_work, "require_acquisition", lambda provider, **kwargs: True)
    monkeypatch.setattr(
        provider_work, "acquisition_dispatch_deadline", lambda provider, deadline: deadline
    )
    with provider_work.host_lock("unit-boundary", root=tmp_path):
        yield
