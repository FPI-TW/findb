"""An elapsed scheduling floor must not turn a safe tail retry into owner recovery."""

import asyncio
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models.provider_work import ProviderWorkAttempt, ProviderWorkOwner
from app.services import provider_work as queue
from app.utils import utc_now
from tests.test_provider_work import add, identity, seed
from tests.test_provider_work_adapters import native_runtime as native_runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [-120, -1, 0, 60])
async def test_elapsed_retry_floor_keeps_fifo_and_durable_owner_settlement(
    test_session, monkeypatch, offset
):
    db = test_session
    key = await seed(db, "shioaji")
    first = await add(db, "shioaji", key, "first")
    second = await add(db, "shioaji", key, "following")
    claimed = await queue.claim(
        db, provider="shioaji", datasets=[key], host_lock_identity="host:1:22"
    )
    fence = identity(claimed)
    await queue.close(db, **fence)
    now = utc_now()
    monkeypatch.setattr(queue, "utc_now", lambda: now)
    proof = {"scheduling": {"reason": "batch_yield", "processed": 25}}
    requested = now + timedelta(seconds=offset)
    await queue.settle(db, **fence, outcome="retry", proof=proof, retry_at=requested)
    attempts = list(
        await db.scalars(select(ProviderWorkAttempt).order_by(ProviderWorkAttempt.queue_order))
    )
    assert attempts[0].state == "settled" and attempts[0].proof == proof
    assert attempts[-1].work_id == first.work_id and attempts[-1].attempt_number == 2
    assert attempts[-1].not_before == max(requested, now + timedelta(seconds=1))
    owner = await db.get(ProviderWorkOwner, (queue.environment(), "shioaji"))
    assert owner.attempt_id is None
    assert (
        await queue.claim(db, provider="shioaji", datasets=[key], host_lock_identity="host:1:22")
    )[0].work_id == second.work_id


@pytest.mark.asyncio
@pytest.mark.parametrize("native_runtime", [("shioaji", "tw_equity_minute", 0)], indirect=True)
async def test_real_shioaji_consumer_survives_slow_settlement_and_ack_replay(
    native_runtime, test_engine, monkeypatch
):
    from findb_fetcher.full_market_client import FullMarketSourceClient

    h = native_runtime
    first = await add(h.db, h.provider, h.dataset, "first")
    second = await add(h.db, h.provider, h.dataset, "second")
    first_id, second_id = first.work_id, second.work_id
    await h.db.commit()
    settle = queue.settle

    async def slow_settle(*args, **kwargs):
        # Move only the shared test clock, simulating lock/receipt-query latency
        # after the client has serialized retry_at. No real sleeps or SDK calls.
        h.shioaji_clock[0] += 2
        return await settle(*args, **kwargs)

    monkeypatch.setattr(queue, "settle", slow_settle)
    requests = []
    handle = h.handle

    def record(request):
        if request.url.path.endswith("/settle"):
            requests.append(json.loads(request.content))
        return handle(request)

    h.handle = record

    def run():
        with FullMarketSourceClient(h.config, client=h.http()) as source:
            with h.queue.claimed_work(source, h.provider) as owner:
                assert owner.work["work_id"] == str(first_id)
                owner.settle("retry", delay=1, proof={"scheduling": {"reason": "batch_yield"}})
                assert owner.settled
            with h.queue.claimed_work(source, h.provider) as owner:
                assert owner.work["work_id"] == str(second_id)
                owner.settle("cancelled")

    await asyncio.wait_for(asyncio.to_thread(run), 30)
    h.db.expire_all()
    attempts = list(
        await h.db.scalars(
            select(ProviderWorkAttempt)
            .where(ProviderWorkAttempt.work_id == first_id)
            .order_by(ProviderWorkAttempt.attempt_number)
        )
    )
    assert [row.state for row in attempts] == ["settled", "queued"]
    assert attempts[0].proof == requests[0]["proof"]
    assert attempts[1].not_before == attempts[0].settled_at + timedelta(seconds=1)
    assert attempts[1].queue_order > attempts[0].queue_order
    assert (await h.db.get(ProviderWorkOwner, (h.environment, h.provider))).attempt_id is None
    assert not h.native_calls()
    # A lost successful ACK can replay the original, now elapsed timestamp.
    path = f"/api/v1/source/provider-work/{first_id}/settle"
    replay = await h.client.post(path, headers=h.headers, json=requests[0])
    assert replay.status_code == 200, replay.text
    changed = await h.client.post(
        path, headers=h.headers, json={**requests[0], "proof": {"changed": True}}
    )
    assert changed.status_code == 409
    assert changed.headers["X-FinDB-Error-Code"] == "PROVIDER_WORK_REPLAY_MISMATCH"
    assert (
        await h.db.scalar(
            select(ProviderWorkAttempt.attempt_number)
            .where(ProviderWorkAttempt.work_id == first_id)
            .order_by(ProviderWorkAttempt.attempt_number.desc())
            .limit(1)
        )
        == 2
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("native_runtime", [("shioaji", "tw_equity_minute", 0)], indirect=True)
@pytest.mark.parametrize(
    ("fault", "code"),
    [
        ("open", "PROVIDER_WORK_NOT_CLOSED"),
        ("fence", "PROVIDER_WORK_FENCE_MISMATCH"),
        ("expired-owner", "PROVIDER_WORK_RECOVERY_REQUIRED"),
        ("proof", "PROVIDER_WORK_INVALID_PROOF"),
        ("missing-time", "PROVIDER_WORK_INVALID_RETRY"),
        ("naive-time", "PROVIDER_WORK_INVALID_RETRY"),
    ],
)
async def test_elapsed_retry_does_not_bypass_fence_proof_or_time_validation(
    native_runtime, test_engine, fault, code
):
    h = native_runtime
    work = await add(h.db, h.provider, h.dataset, "blocked")
    work_id = work.work_id
    with h.queue.host_lock(h.provider) as (_, inode):
        claimed = await queue.claim(
            h.db, provider=h.provider, datasets=[h.dataset], host_lock_identity=inode
        )
    attempt_id = claimed[1].attempt_id
    fence = identity(claimed)
    if fault != "open":
        await queue.close(h.db, **fence)
    if fault == "expired-owner":
        claimed[2].expires_at = queue.utc_now() - timedelta(seconds=1)
    await h.db.commit()
    body = {
        "generation": fence["generation"] + int(fault == "fence"),
        "token": str(fence["token"]),
        "outcome": "completed" if fault == "proof" else "retry",
        "proof": {},
        "retry_at": (queue.utc_now() - timedelta(seconds=1)).isoformat(),
    }
    if fault == "missing-time":
        body.pop("retry_at")
    elif fault == "naive-time":
        body["retry_at"] = "2026-10-30T10:00:00"
    response = await h.client.post(
        f"/api/v1/source/provider-work/{work_id}/settle", headers=h.headers, json=body
    )
    assert response.status_code == 409, response.text
    assert response.headers["X-FinDB-Error-Code"] == code
    h.db.expire_all()
    owner = await h.db.get(ProviderWorkOwner, (h.environment, h.provider))
    assert owner.attempt_id == attempt_id
    attempts = list(
        await h.db.scalars(
            select(ProviderWorkAttempt).where(ProviderWorkAttempt.work_id == work_id)
        )
    )
    assert len(attempts) == 1 and attempts[0].state != "settled"
