"""Fetcher retires local checkpoints only for works Source has closed."""

from datetime import timedelta

import pytest

from app.models.provider_work import ProviderWork
from app.models.registry import DatasetRegistry
from app.services import provider_work as queue
from app.utils import utc_now, uuid7


async def _work(db, stable_key, *, provider="finlab", dataset="tw_equity_eod", closed=True):
    if await db.get(DatasetRegistry, dataset) is None:
        db.add(
            DatasetRegistry(dataset_key=dataset, name=dataset, market="TW", asset_class="equity")
        )
        await db.flush()
    work = ProviderWork(
        environment=queue.environment(),
        provider=provider,
        stable_key=stable_key,
        kind="daily",
        dataset_key=dataset,
        trade_date=utc_now().date(),
        execution_profile="full_market",
        state="completed" if closed else "running",
        completed_at=utc_now() - timedelta(days=8) if closed else None,
    )
    db.add(work)
    await db.flush()
    return work


@pytest.mark.asyncio
async def test_closed_lookup_reports_only_scoped_closed_works(client, test_session, source_headers):
    closed = await _work(test_session, "daily:full_market:tw_equity_eod:2026-10-01")
    historical = await _work(test_session, "historical:tw_equity_eod:2026-09-01")
    open_work = await _work(
        test_session, "daily:full_market:tw_equity_eod:2026-10-02", closed=False
    )
    await _work(
        test_session,
        "daily:full_market:tw_futures_eod:2026-10-01",
        provider="taifex",
        dataset="tw_futures_eod",
    )
    keys = [
        "daily:full_market:tw_equity_eod:2026-10-01",
        "daily:full_market:tw_equity_eod:2026-10-02",
        "daily:full_market:tw_futures_eod:2026-10-01",
        "daily:full_market:tw_equity_eod:unknown",
    ]

    response = await client.post(
        "/api/v1/source/provider-work/closed",
        json={
            "stable_keys": keys,
            "work_ids": [str(historical.work_id), str(open_work.work_id)],
        },
        headers=source_headers,
    )

    assert response.status_code == 200
    assert sorted(response.json()["data"], key=lambda row: row["stable_key"]) == [
        {
            "work_id": str(work.work_id),
            "stable_key": work.stable_key,
            "state": "completed",
            "completed_at": work.completed_at.isoformat(),
        }
        for work in (closed, historical)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"stable_keys": []},
        {"stable_keys": ["x"] * 501},
        {"stable_keys": ["x"] * 300, "work_ids": [str(uuid7()) for _ in range(201)]},
    ],
)
async def test_closed_lookup_bounds_the_request(client, source_headers, body):
    response = await client.post(
        "/api/v1/source/provider-work/closed", json=body, headers=source_headers
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_closed_lookup_requires_source_credential(client):
    response = await client.post("/api/v1/source/provider-work/closed", json={"stable_keys": ["x"]})
    assert response.status_code in {401, 403}
