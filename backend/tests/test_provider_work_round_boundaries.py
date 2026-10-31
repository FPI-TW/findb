"""Owned native/read failures leave durable eligible work, never a quarantined known error."""

import asyncio
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app.models.provider_work import ProviderWork, ProviderWorkAttempt, ProviderWorkOwner
from app.models.registry import HistoricalBackfillItem, HistoricalBackfillRequest
from app.services import provider_work
from app.services.provider_work import prepared_map
from app.utils import utc_now
from tests.provider_work_runtime_support import DAY, SCOPES
from tests.test_provider_work_adapters import native_runtime as native_runtime

DAILY = [*SCOPES, ("twelve_data", "hk_equity_eod"), ("finlab", "tw_etf_eod")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "native_runtime", [(provider, dataset, 0) for provider, dataset in DAILY], indirect=True
)
@pytest.mark.parametrize("fault", [503, "transport"])
async def test_daily_prefetch_source_failure_closes_and_requeues_without_native(
    native_runtime, test_engine, fault
):
    from findb_fetcher.full_market_client import FullMarketSourceClient
    from findb_fetcher.full_market_runtime import FullMarketRuntime
    from findb_fetcher.full_market_state import FullMarketState

    h = native_runtime
    feed = {
        "provider": h.provider,
        "dataset_key": h.dataset,
        "source_timezone": "Asia/Taipei",
        "target_date_lag_days": 0,
        "scheduled_time": "17:00",
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
    handle = h.handle
    reads = []

    def unavailable(request):
        if request.url.path == "/api/v1/source/delivery-plans" and request.method == "GET":
            reads.append(request.url.path)
            if fault == "transport":
                raise httpx.ReadError("offline Source read", request=request)
            return httpx.Response(503, request=request)
        return handle(request)

    h.handle = unavailable

    def run():
        with (
            FullMarketSourceClient(h.config, client=h.http()) as source,
            h.source_factory(h.config, h.registry) as delivery,
        ):
            runtime = FullMarketRuntime(
                provider=h.provider,
                config={"feeds": [feed]},
                state=FullMarketState(h.path / "daily.sqlite3"),
                source=source,
                delivery=delivery,
                raw_store=h.raw,
                client=httpx.Client(
                    transport=httpx.MockTransport(lambda r: pytest.fail("unexpected native HTTP"))
                ),
                can_acquire=lambda: False,
            )
            try:
                return runtime._queued_cycle()
            finally:
                runtime.close()

    await asyncio.wait_for(asyncio.to_thread(run), 30)
    h.db.expire_all()
    work = await h.db.get(ProviderWork, work_id)
    attempts = list(
        await h.db.scalars(
            select(ProviderWorkAttempt)
            .where(ProviderWorkAttempt.work_id == work_id)
            .order_by(ProviderWorkAttempt.attempt_number)
        )
    )
    assert (
        reads
        and work.state == "queued"
        and not (await prepared_map(h.db, work.work_id))
        and work.manifest is None
    )
    assert attempts[0].outcome == "retry" and attempts[0].failure_code == "DAILY_SOURCE_UNAVAILABLE"
    assert attempts[1].not_before > utc_now()
    assert not h.native_calls() and not h.runs
    assert (await h.db.get(ProviderWorkOwner, (h.environment, h.provider))).attempt_id is None
    with h.queue.host_lock(h.provider):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("native_runtime", [("twelve_data", "us_equity_eod", 5)], indirect=True)
@pytest.mark.parametrize("prepared", [False, True])
async def test_account_cooldown_skips_other_unprepared_dates_but_allows_retained_rerun(
    native_runtime, test_engine, monkeypatch, prepared
):
    from findb_fetcher import historical_runtime
    from findb_fetcher.providers.twelve_data import TwelveDataClient, TwelveDataConfig

    from tests.provider_work_clock import drain_retained_rerun_during_cooldown

    h = native_runtime
    retained_id = None
    original = None
    if prepared:
        handle = h.handle
        h.handle = lambda request: (
            httpx.Response(503, request=request)
            if request.url.path == "/api/v1/source/ingest"
            else handle(request)
        )
        assert await h.run()
        h.handle = handle
        h.db.expire_all()
        retained = await h.db.scalar(
            select(ProviderWork).where(ProviderWork.stable_key == f"historical:{h.item_id}")
        )
        retained_id, original = retained.work_id, dict((await prepared_map(h.db, retained.work_id)))
        assert original
    calls = []

    def native(request):
        calls.append(request.url.params["symbol"])
        return httpx.Response(
            429,
            json={"status": "error", "code": 429},
            headers={"Retry-After": "120"},
            request=request,
        )

    monkeypatch.setattr(
        historical_runtime,
        "TwelveDataClient",
        lambda config: TwelveDataClient(
            TwelveDataConfig("offline-twelve"),
            client=httpx.Client(transport=httpx.MockTransport(native)),
        ),
    )
    assert await h.run()
    h.db.expire_all()
    owner = await h.db.get(ProviderWorkOwner, (h.environment, h.provider))
    floor = owner.acquisition_not_before
    assert floor > utc_now() + timedelta(seconds=110) and owner.attempt_id is None
    assert not await h.run(), (
        "a fresh runner must not claim the next unfetched date into governor sleep"
    )
    assert calls == ["AAPL"]
    if prepared:
        retry = await h.db.scalar(
            select(ProviderWorkAttempt).where(
                ProviderWorkAttempt.work_id == retained_id, ProviderWorkAttempt.state == "queued"
            )
        )
        retry.not_before = utc_now() - timedelta(seconds=1)
        await h.db.commit()
        assert await h.run(), "original bytes drain under cooldown without a new provider operation"
        h.db.expire_all()
        retained = await h.db.get(ProviderWork, retained_id)
        assert (
            retained.state == "queued" and (await prepared_map(h.db, retained.work_id)) == original
        )
        assert calls == ["AAPL"] and h.runs
        assert (await h.db.get(ProviderWorkOwner, (h.environment, h.provider))).attempt_id is None
    await drain_retained_rerun_during_cooldown(h)
    h.db.expire_all()
    assert (
        await h.db.get(ProviderWorkOwner, (h.environment, h.provider))
    ).acquisition_not_before == floor
    rows = await provider_work.projection(h.db, provider=h.provider)
    assert any(row["wait_reason"] == "account_cooldown" for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "native_runtime", [(provider, dataset, 5) for provider, dataset in SCOPES], indirect=True
)
async def test_enabled_queue_expires_old_request_and_cancels_empty_siblings_before_claim(
    native_runtime, test_engine, monkeypatch
):
    h = native_runtime
    later = utc_now() + timedelta(days=90)
    monkeypatch.setattr(provider_work, "utc_now", lambda: later)
    with h.queue.host_lock(h.provider) as (_, inode):
        response = await h.client.post(
            "/api/v1/source/provider-work/claim",
            headers=h.headers,
            json={
                "protocol": "provider-work-v1",
                "host_lock_identity": inode,
                "accepted_kinds": ["historical"],
            },
        )
    assert response.status_code == 200 and response.json()["work"] is None, response.text
    h.db.expire_all()
    request = await h.db.get(HistoricalBackfillRequest, h.request_id)
    assert request.status == "failed" and request.failure_code == "BACKFILL_EXPIRED"
    items = list(
        await h.db.scalars(
            select(HistoricalBackfillItem)
            .where(HistoricalBackfillItem.request_id == h.request_id)
            .order_by(HistoricalBackfillItem.trade_date)
        )
    )
    assert items[0].status == "failed" and all(
        item.status in {"cancelled", "skipped_calendar"} for item in items[1:]
    )
    assert not h.native_calls()
    assert not await provider_work.projection(h.db, provider=h.provider)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "native_runtime", [(provider, dataset, 5) for provider, dataset in SCOPES], indirect=True
)
async def test_expiry_preserves_original_prepared_date_as_failed_domain_drain(
    native_runtime, test_engine, monkeypatch
):
    h = native_runtime
    handle = h.handle

    def unavailable(request):
        if request.url.path == "/api/v1/source/ingest":
            return httpx.Response(503, request=request)
        return handle(request)

    h.handle = unavailable
    assert await h.run()
    h.db.expire_all()
    work = await h.db.scalar(
        select(ProviderWork).where(ProviderWork.stable_key == f"historical:{h.item_id}")
    )
    work_id = work.work_id
    original = dict((await prepared_map(h.db, work.work_id)))
    assert original
    calls = h.native_calls()
    later = utc_now() + timedelta(days=90)
    monkeypatch.setattr(provider_work, "utc_now", lambda: later)
    h.handle = handle
    assert await h.run()
    h.db.expire_all()
    item = await h.db.get(HistoricalBackfillItem, h.item_id)
    request = await h.db.get(HistoricalBackfillRequest, h.request_id)
    work = await h.db.get(ProviderWork, work_id)
    assert item.status == request.status == "failed"
    assert request.failure_code == "BACKFILL_EXPIRED"
    assert (await prepared_map(h.db, work.work_id)) == original and work.state == "cancelled", [
        event for event in h.source_events if event[1] >= 400
    ]
    assert h.native_calls() == calls
    assert (await h.db.get(ProviderWorkOwner, (h.environment, h.provider))).attempt_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("native_runtime", [("twelve_data", "us_equity_eod", 0)], indirect=True)
async def test_fenced_cooldown_is_monotonic_survives_release_and_other_provider_claims(
    native_runtime, test_engine
):
    from app.models.registry import DatasetRegistry
    from app.services.source_clients import create_source_client

    h = native_runtime
    h.db.add(
        DatasetRegistry(dataset_key="tw_equity_eod", name="TW", market="TW", asset_class="equity")
    )
    await h.db.flush()
    _, secret = await create_source_client(
        h.db,
        name="cooldown-parallel",
        source_name="finlab",
        allowed_datasets=["tw_equity_eod"],
        rate_limit_requests=1000,
        rate_limit_window=60,
        commit=False,
    )
    await h.db.commit()
    parallel_headers = {"X-API-Key": secret}
    for dataset, headers in [(h.dataset, h.headers), ("tw_equity_eod", parallel_headers)]:
        response = await h.client.post(
            "/api/v1/source/provider-work/enqueue",
            headers=headers,
            json={
                "kind": "maintenance",
                "dataset_key": dataset,
                "stable_key": f"maintenance:{dataset}:floor",
            },
        )
        assert response.status_code == 200
    with h.queue.host_lock(h.provider) as (_, inode):
        payload = {"protocol": "provider-work-v1", "host_lock_identity": inode}
        claim = await h.client.post(
            "/api/v1/source/provider-work/claim", headers=h.headers, json=payload
        )
        work = claim.json()["work"]
        path = f"/api/v1/source/provider-work/{work['work_id']}"
        fence = {"generation": work["generation"], "token": work["token"]}
        floor = utc_now() + timedelta(seconds=120)
        for deadline in [floor, floor - timedelta(seconds=60)]:
            response = await h.client.post(
                path + "/heartbeat",
                headers=h.headers,
                json={**fence, "acquisition_not_before": deadline.isoformat()},
            )
            assert response.status_code == 200, response.text
        assert (
            await h.client.post(path + "/authorize", headers=h.headers, json=fence)
        ).status_code == 409
        assert (
            await h.client.post(path + "/close", headers=h.headers, json=fence)
        ).status_code == 200
        assert (
            await h.client.post(
                path + "/settle",
                headers=h.headers,
                json={
                    **fence,
                    "outcome": "retry",
                    "proof": {},
                    "retry_at": (utc_now() + timedelta(seconds=60)).isoformat(),
                },
            )
        ).status_code == 200
        response = await h.client.post(
            "/api/v1/source/provider-work/claim", headers=h.headers, json=payload
        )
        assert response.json()["work"] is None
    h.db.expire_all()
    assert (
        await h.db.get(ProviderWorkOwner, (h.environment, h.provider))
    ).acquisition_not_before == floor
    with h.queue.host_lock("finlab") as (_, inode):
        response = await h.client.post(
            "/api/v1/source/provider-work/claim",
            headers=parallel_headers,
            json={"protocol": "provider-work-v1", "host_lock_identity": inode},
        )
        assert response.status_code == 200 and response.json()["work"]["kind"] == "maintenance"
    assert not h.native_calls()
