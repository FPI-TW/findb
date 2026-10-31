"""Repeated Owner cutover retains only legitimate terminal-domain drains."""

import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app.models.provider_work import (
    ProviderWork,
    ProviderWorkAttempt,
    ProviderWorkOwner,
    ProviderWorkPrepared,
)
from app.models.registry import HistoricalBackfillItem, HistoricalBackfillRequest
from app.services import provider_work as queue
from app.services.historical_backfill import expire_requests
from app.services.provider_work import prepared_map
from app.utils import utc_now
from tests.provider_work_runtime_support import SCOPES
from tests.test_provider_work import attest_cutover
from tests.test_provider_work_adapters import native_runtime as native_runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("native_runtime", [(p, d, 5) for p, d in SCOPES], indirect=True)
@pytest.mark.parametrize("empty_head", [False, True])
async def test_repeated_cutover_retains_expired_prepared_and_authenticated_drain(
    native_runtime, test_engine, empty_head
):
    h = native_runtime
    head = await h.db.scalar(
        select(ProviderWork).where(ProviderWork.stable_key == f"historical:{h.item_id}")
    )
    head_id = head.work_id
    if empty_head:
        first_attempt = await h.db.scalar(
            select(ProviderWorkAttempt).where(ProviderWorkAttempt.work_id == head_id)
        )
        first_attempt.not_before = utc_now() + timedelta(hours=1)
        await h.db.commit()
    handle = h.handle

    def outage(request):
        if request.url.path == "/api/v1/source/ingest":
            return httpx.Response(503, request=request)
        return handle(request)

    h.handle = outage
    assert await h.run()
    h.db.expire_all()
    work = await h.db.scalar(
        select(ProviderWork).where(ProviderWork.work_id.in_(select(ProviderWorkPrepared.work_id)))
    )
    assert work is not None and work.state == "queued" and not h.runs
    work_id = work.work_id
    prepared = json.loads(json.dumps((await prepared_map(h.db, work.work_id))))
    frozen = json.loads(json.dumps(work.manifest))
    retry = await h.db.scalar(
        select(ProviderWorkAttempt).where(
            ProviderWorkAttempt.work_id == work_id, ProviderWorkAttempt.state == "queued"
        )
    )
    retry_id, retry_at = retry.attempt_id, retry.not_before
    await queue.lock_provider(h.db, h.provider)
    await expire_requests(h.db, provider=h.provider, now=utc_now() + timedelta(days=90))
    await h.db.commit()
    failed_item = await h.db.get(HistoricalBackfillItem, h.item_id)
    failed_details = (failed_item.status, failed_item.failure_code, failed_item.completed_at)
    assert failed_details[:2] == ("failed", "BACKFILL_EXPIRED")
    identities = {
        owner.provider: owner.host_lock_identity
        for owner in await h.db.scalars(select(ProviderWorkOwner))
    }
    for _ in range(2):
        await attest_cutover(h.db, identities)
        await h.db.refresh(work)
        await h.db.refresh(retry)
        assert work.state == retry.state == "queued" and retry.drain_only
        assert retry.attempt_id == retry_id and retry.not_before == retry_at
        assert (await prepared_map(h.db, work.work_id)) == prepared and work.manifest == frozen
        assert (
            failed_item.status,
            failed_item.failure_code,
            failed_item.completed_at,
        ) == failed_details
    if empty_head:
        await h.db.refresh(head)
        assert head.state == "cancelled" and not (await prepared_map(h.db, head.work_id))
    calls = h.native_calls()
    retry.not_before = utc_now() - timedelta(seconds=1)
    await h.db.commit()
    h.handle = handle
    assert await h.run()
    h.db.expire_all()
    work = await h.db.get(ProviderWork, work_id)
    assert work.state == "cancelled"
    assert h.native_calls() == calls and h.runs
    assert (await prepared_map(h.db, work.work_id)) == prepared and work.manifest == frozen
    request = await h.db.get(HistoricalBackfillRequest, h.request_id)
    failed_item = await h.db.get(HistoricalBackfillItem, h.item_id)
    assert request.status == "failed" and request.failure_code == "BACKFILL_EXPIRED"
    assert (
        failed_item.status,
        failed_item.failure_code,
        failed_item.completed_at,
    ) == failed_details
    owner = await h.db.get(ProviderWorkOwner, (h.environment, h.provider))
    assert owner.attempt_id is None
    following = await queue.enqueue(
        h.db,
        provider=h.provider,
        stable_key=f"after-expiry:{work_id}",
        kind="maintenance",
        dataset_key=h.dataset,
        trade_date=None,
    )
    await h.db.commit()
    with h.queue.host_lock(h.provider) as (_, inode):
        response = await h.client.post(
            "/api/v1/source/provider-work/claim",
            headers=h.headers,
            json={
                "protocol": "provider-work-v1",
                "host_lock_identity": inode,
                "accepted_kinds": ["maintenance"],
            },
        )
        assert response.status_code == 200, response.text
        claimed = response.json()["work"]
        assert claimed["work_id"] == str(following.work_id)
        fence = {"generation": claimed["generation"], "token": claimed["token"]}
        route = f"/api/v1/source/provider-work/{claimed['work_id']}"
        assert (
            await h.client.post(route + "/close", headers=h.headers, json=fence)
        ).status_code == 200
        assert (
            await h.client.post(
                route + "/settle",
                headers=h.headers,
                json={**fence, "outcome": "cancelled", "proof": {}},
            )
        ).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("native_runtime", [("twelve_data", "us_equity_eod")], indirect=True)
async def test_cutover_does_not_revive_manual_failure_with_retained_bytes(
    native_runtime, test_engine
):
    h = native_runtime
    original = h.handle
    h.handle = lambda req: (
        httpx.Response(503, request=req)
        if req.url.path == "/api/v1/source/ingest"
        else original(req)
    )
    assert await h.run()
    h.db.expire_all()
    item = await h.db.get(HistoricalBackfillItem, h.item_id)
    item.status, item.failure_code = "failed", "MANUAL_INTERVENTION_REQUIRED"
    work = await h.db.scalar(
        select(ProviderWork).where(ProviderWork.stable_key == f"historical:{h.item_id}")
    )
    prepared = json.loads(json.dumps((await prepared_map(h.db, work.work_id))))
    identities = {
        owner.provider: owner.host_lock_identity
        for owner in await h.db.scalars(select(ProviderWorkOwner))
    }
    await attest_cutover(h.db, identities)
    await h.db.refresh(work)
    assert work.state == "failed" and (await prepared_map(h.db, work.work_id)) == prepared
    assert not await h.db.scalar(
        select(ProviderWorkAttempt).where(
            ProviderWorkAttempt.work_id == work.work_id, ProviderWorkAttempt.state == "queued"
        )
    )
