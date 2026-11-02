"""Source credential scope must remain bounded to the retained feeds."""

import pytest
from sqlalchemy import select

from app.models.registry import SchedulerDataset
from app.services.ingestion import IngestionService
from app.services.source_clients import (
    SourceProviderScopeError,
    create_source_client,
    normalize_provider_scope,
)
from scripts.seed_data import DATASETS, seed_datasets
from tests.test_canonical_ingest_api import _canonical_request


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("twelve_data", ["us_equity_eod"]),
        ("finlab", ["tw_equity_eod", "tw_etf_eod"]),
        ("shioaji", ["tw_equity_minute", "tw_etf_minute"]),
        ("taifex", ["tw_futures_eod"]),
    ],
)
def test_provider_scope_expands_null_to_complete_mapping(provider, expected):
    assert normalize_provider_scope(provider, None) == (provider, expected)


def test_provider_scope_rejects_unknown_or_widened_mapping():
    with pytest.raises(SourceProviderScopeError):
        normalize_provider_scope("legacy_provider", None)
    with pytest.raises(SourceProviderScopeError):
        normalize_provider_scope("finlab", ["tw_equity_eod", "retired_dataset"])
    with pytest.raises(SourceProviderScopeError):
        normalize_provider_scope("shioaji", [])


def test_provider_scope_allows_non_empty_least_privilege_subset():
    assert normalize_provider_scope("shioaji", ["tw_etf_minute"]) == (
        "shioaji",
        ["tw_etf_minute"],
    )


@pytest.mark.asyncio
async def test_list_datasets_fails_closed_for_null_scope():
    class Session:
        info = {"allowed_datasets": None}

        async def execute(self, _statement):  # pragma: no cover - must not be called
            raise AssertionError("NULL source scope must not query the registry")

    assert await IngestionService(Session()).list_datasets() == []


def test_twelve_data_can_never_be_granted_retired_hk_scope():
    with pytest.raises(SourceProviderScopeError):
        normalize_provider_scope("twelve_data", ["hk_equity_eod"])
    hk = next(item for item in DATASETS if item["dataset_key"] == "hk_equity_eod")
    assert "twelve_data" not in hk["config"]["allowed_sources"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_credential", [False, True])
async def test_twelve_data_credential_cannot_enqueue_or_deliver_hk(
    client, test_session, stale_credential
):
    await seed_datasets(test_session)
    row, key = await create_source_client(
        test_session,
        name="twelve-data",
        source_name="twelve_data",
        allowed_datasets=["us_equity_eod"],
        rate_limit_requests=1000,
        rate_limit_window=60,
        commit=False,
    )
    if stale_credential:
        # A row that escaped the migration still cannot use the retired scope.
        row.allowed_datasets = ["us_equity_eod", "hk_equity_eod"]
    await test_session.commit()
    headers = {"X-API-Key": key}

    enqueue = await client.post(
        "/api/v1/source/provider-work/enqueue",
        headers=headers,
        json={
            "stable_key": "daily:full_market:hk_equity_eod:2026-10-15",
            "kind": "daily",
            "dataset_key": "hk_equity_eod",
            "trade_date": "2026-10-15",
            "execution_profile": "full_market",
        },
    )
    assert enqueue.status_code == 403

    request = _canonical_request(idempotency_key="retired-hk")
    request.update(dataset_key="hk_equity_eod", source="twelve_data")
    ingest = await client.post("/api/v1/source/ingest", headers=headers, json=request)
    # A stale scope gets past the credential check, but no fence can ever be claimed.
    assert ingest.status_code == (409 if stale_credential else 403)
    assert ingest.json()["error"]["code"] == (
        "PROVIDER_WORK_FENCE" if stale_credential else "DATASET_ACCESS_DENIED"
    )


@pytest.mark.asyncio
async def test_seed_schedules_hk_for_factset_but_not_twelve_data(test_session):
    await seed_datasets(test_session)
    schedulers = set(
        await test_session.scalars(
            select(SchedulerDataset.scheduler_key).where(
                SchedulerDataset.dataset_key == "hk_equity_eod"
            )
        )
    )
    assert schedulers == {"pilot_factset_v1", "full_market_factset_v1"}
