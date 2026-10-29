"""Real queue continuations report errors while acquisition remains prohibited."""

import asyncio
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.provider_work import ProviderWork, ProviderWorkAttempt, ProviderWorkOwner
from app.models.registry import (
    DailyDeliveryMember,
    DatasetRegistry,
    FullMarketEnvironment,
    SchedulerControl,
)
from app.services import provider_work
from app.utils import utc_now
from tests.provider_work_runtime_support import DAY
from tests.test_provider_work_adapters import native_runtime as native_runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("native_runtime", [("twelve_data", "us_equity_eod", 0)], indirect=True)
@pytest.mark.parametrize("gate", ["stopped", "window", "cooldown"])
@pytest.mark.parametrize("report_fault", ["503", "lost-ack"])
async def test_report_continuation_uses_real_queue_and_cannot_reopen_acquisition(
    native_runtime, test_engine, monkeypatch, gate, report_fault
):
    from findb_fetcher.full_market_client import FullMarketProtocolError, FullMarketSourceClient
    from findb_fetcher.full_market_runtime import FullMarketRuntime
    from findb_fetcher.full_market_state import FullMarketState
    from findb_fetcher.platform_limits import limits

    from tests import test_full_market as fixtures

    h = native_runtime

    async def dataset(db, key):
        return await db.get(DatasetRegistry, key)

    monkeypatch.setattr(fixtures, "_dataset", dataset)
    monkeypatch.setattr(fixtures, "DAY", DAY)
    _, _, db_plan = await fixtures._activated_plan(h.db, h.dataset, count=2)
    get_settings().APP_ENVIRONMENT = h.environment
    h.db.add(FullMarketEnvironment(environment=h.environment, enabled=True))
    feed = {
        "provider": h.provider,
        "dataset_key": h.dataset,
        "market": "US",
        "asset_class": "equity",
        "slot_id": "western_markets_window",
        "source_timezone": "America/New_York",
    }
    work = await provider_work.enqueue(
        h.db,
        provider=h.provider,
        stable_key=f"daily:full_market:{h.dataset}:{DAY}",
        kind="daily",
        dataset_key=h.dataset,
        trade_date=DAY,
        execution_profile="full_market",
        scope={"feed": feed},
    )
    work_id = work.work_id
    await h.db.commit()
    restricted = False
    failing_report = True
    original_window = provider_work.daily_window
    closed = {
        "open": False,
        "closes_at": None,
        "next_open_at": (utc_now() + timedelta(hours=3)).isoformat(),
    }
    monkeypatch.setattr(
        provider_work,
        "daily_window",
        lambda provider, now=None: (
            closed if restricted and gate == "window" else original_window(provider, now)
        ),
    )
    handle = h.handle

    def fault(request):
        if failing_report and request.url.path.endswith("/outcomes"):
            if report_fault == "lost-ack":
                handle(request)
                raise httpx.ReadError("lost outcome acknowledgement")
            return httpx.Response(503, request=request)
        return handle(request)

    h.handle = fault
    acquisitions = []
    state_path = h.path / "daily.sqlite3"

    async def stop_control():
        control = await h.db.get(SchedulerControl, f"full_market_{h.provider}_v1")
        control.desired_state = "stopped"
        await h.db.commit()

    def run_round():
        nonlocal restricted
        state = FullMarketState(state_path)
        with FullMarketSourceClient(h.config, client=h.http()) as source:
            runtime = FullMarketRuntime(
                provider=h.provider,
                config={"feeds": [feed]},
                state=state,
                source=source,
                delivery=h.source_factory(h.config, h.registry),
                raw_store=h.raw,
                execution_profile="full_market",
            )
            runtime.proof = limits(h.provider)
            runtime.daily_policy = {"max_members": 5, "max_seconds": 30}
            runtime.runtime_window = lambda: (
                closed if restricted and gate == "window" else {"open": True}
            )

            def acquire(member, *args):
                acquisitions.append(member["member_key"])
                raise RuntimeError("sanitized native failure")

            runtime._acquire = acquire
            try:
                with h.queue.claimed_work(source, h.provider, accepted_kinds=["daily"]) as owner:
                    assert owner
                    if restricted:
                        assert owner.work["drain_only"]
                        with pytest.raises(FullMarketProtocolError) as denied:
                            owner.request("authorize")
                        assert denied.value.status_code == 409
                    plan = source.evaluate(str(db_plan.plan_id))
                    owner.freeze(
                        {
                            "mode": "canonical",
                            "plan_id": str(db_plan.plan_id),
                            "members": [
                                {"key": key}
                                for part in plan["parts"]
                                for key in part["member_keys"]
                            ],
                        }
                    )
                    state.record_plan(plan, h.provider)
                    try:
                        assert not runtime._deliver_plan(
                            plan,
                            source.release(str(db_plan.release_id)),
                            feed,
                            drain_only=bool(owner.work.get("drain_only")),
                        )
                    except FullMarketProtocolError:
                        assert failing_report
                        owner.request("close")
                        for invalid in [
                            [],
                            ["outside-frozen-scope"],
                            [acquisitions[0]] * 2,
                            [{}],
                            "invalid",
                        ]:
                            with pytest.raises(FullMarketProtocolError):
                                owner.request(
                                    "settle",
                                    outcome="retry",
                                    retry_at=(utc_now() + timedelta(seconds=60)).isoformat(),
                                    proof={"pending_source_errors": invalid},
                                )
                        restricted = True
                        if gate == "stopped":
                            asyncio.run_coroutine_threadsafe(stop_control(), h.loop).result(10)
                        if gate == "cooldown":
                            owner.journal.observe_cooldown(
                                (utc_now() + timedelta(hours=1)).timestamp()
                            )
                        runtime._settle_queued_round(owner, outcome="retry")
                    else:
                        runtime._settle_queued_round(owner)
            finally:
                runtime.close()

    await asyncio.wait_for(asyncio.to_thread(run_round), 30)
    h.db.expire_all()
    work = await h.db.get(ProviderWork, work_id)
    assert work.state == "queued" and not work.prepared_deliveries
    original_manifest = work.manifest
    attempt = await h.db.scalar(
        select(ProviderWorkAttempt).where(
            ProviderWorkAttempt.work_id == work_id, ProviderWorkAttempt.state == "queued"
        )
    )
    assert attempt.proof == {"source_error_pending": True, "pending_source_errors": acquisitions}
    # Restrictions were present at settlement. Only normal report backoff may delay it.
    assert utc_now() < attempt.not_before < utc_now() + timedelta(seconds=120)
    attempt.not_before = utc_now() - timedelta(seconds=1)  # simulate that backoff elapsing
    await h.db.commit()
    projection = next(
        row for row in await provider_work.projection(h.db) if row["work_id"] == str(work_id)
    )
    assert projection["position"] == 1 and projection["drain_only"]
    assert projection["wait_reason"] == "prepared_drain"
    failing_report = False
    await asyncio.wait_for(asyncio.to_thread(run_round), 30)
    h.db.expire_all()
    work = await h.db.get(ProviderWork, work_id)
    assert work.state == "queued" and work.manifest == original_manifest
    assert len(acquisitions) == 1 and not h.native_calls()
    members = list(
        await h.db.scalars(
            select(DailyDeliveryMember).where(
                DailyDeliveryMember.plan_id == db_plan.plan_id,
                DailyDeliveryMember.outcome_reason == "source_error",
            )
        )
    )
    assert len(members) == len(members[0].outcome_history) == 1
    assert members[0].outcome == "blocked"
    state = FullMarketState(state_path)
    assert not state.pending_source_error_keys(str(db_plan.plan_id))
    with state.connection() as local:
        assert local.execute("SELECT SUM(attempts) FROM full_work").fetchone()[0] == 1
    owner_row = await h.db.get(ProviderWorkOwner, (h.environment, h.provider))
    if gate == "cooldown":
        assert owner_row.acquisition_not_before > utc_now() + timedelta(minutes=50)
    attempt = await h.db.scalar(
        select(ProviderWorkAttempt).where(
            ProviderWorkAttempt.work_id == work_id, ProviderWorkAttempt.state == "queued"
        )
    )
    assert not attempt.proof.get("source_error_pending")
    attempt.not_before = utc_now() - timedelta(seconds=1)
    if gate == "cooldown":
        await h.db.commit()
        with h.queue.host_lock(h.provider) as (_, inode):
            response = await h.client.post(
                "/api/v1/source/provider-work/claim",
                headers=h.headers,
                json={
                    "protocol": "provider-work-v1",
                    "host_lock_identity": inode,
                    "accepted_kinds": ["daily"],
                },
            )
        assert response.status_code == 200 and response.json()["work"] is None
        assert owner_row.acquisition_not_before > utc_now() + timedelta(minutes=50)
        return
    later = await provider_work.enqueue(
        h.db,
        provider=h.provider,
        stable_key="maintenance:after-report",
        kind="maintenance",
        dataset_key=h.dataset,
        trade_date=None,
    )
    later_id = str(later.work_id)
    await h.db.commit()
    # The original stopped daily work loses its report exception; other eligible work proceeds.
    with h.queue.host_lock(h.provider) as (_, inode):
        claimed = await h.client.post(
            "/api/v1/source/provider-work/claim",
            headers=h.headers,
            json={
                "protocol": "provider-work-v1",
                "host_lock_identity": inode,
                "accepted_kinds": ["daily", "maintenance"],
            },
        )
        assert claimed.status_code == 200, claimed.text
        payload = claimed.json()
        assert payload["work"]["work_id"] == later_id
        fence = {"generation": payload["work"]["generation"], "token": payload["work"]["token"]}
        for action, body in [
            ("close", {}),
            (
                "settle",
                {
                    "outcome": "retry",
                    "proof": {"pending_source_errors": acquisitions},
                    "retry_at": (utc_now() + timedelta(seconds=60)).isoformat(),
                },
            ),
            ("settle", {"outcome": "failed", "proof": {}}),
        ]:
            response = await h.client.post(
                f"/api/v1/source/provider-work/{later_id}/{action}",
                headers=h.headers,
                json={**fence, **body},
            )
            assert response.status_code == (
                409 if action == "settle" and body["outcome"] == "retry" else 200
            ), response.text
