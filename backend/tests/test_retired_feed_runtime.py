"""Retired HK work drains real Source/worker evidence while Full stays stopped."""

import asyncio
import time
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models.provider_work import (
    ProviderWork,
    ProviderWorkAttempt,
    ProviderWorkOwner,
    ProviderWorkReceipt,
)
from app.models.registry import IngestionRun, SchedulerControl
from app.services import provider_work
from app.services.provider_work import prepared_map
from app.utils import utc_now
from tests.provider_work_runtime_support import DAY
from tests.test_canonical_ingest_api import _canonical_request
from tests.test_provider_work_adapters import native_runtime as native_runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("native_runtime", [("twelve_data", "hk_equity_eod", 0)], indirect=True)
@pytest.mark.parametrize("prepared", [False, True])
async def test_retired_legacy_hk_round_drains_real_source_and_releases_fifo_without_provider_io(
    native_runtime, test_engine, monkeypatch, prepared
):
    from findb_fetcher.full_market_client import FullMarketSourceClient
    from findb_fetcher.full_market_runtime import FullMarketRuntime
    from findb_fetcher.full_market_state import FullMarketState

    h = native_runtime
    if prepared:
        from app.config import get_settings
        from app.models.registry import DatasetRegistry
        from app.schemas.full_market import DeliveryPlanCreateRequest
        from app.services.full_market import create_plan
        from tests import test_full_market as fixtures
        from tests.full_market_fixtures import historical_admission

        monkeypatch.setattr(fixtures, "DAY", DAY)
        _, release = await fixtures._baseline(h.db, h.dataset, count=1, effective_date=DAY)
        dataset = await h.db.get(DatasetRegistry, h.dataset)
        await historical_admission(h.db, dataset, release, DAY, None, profile="pilot")
        plan = await create_plan(
            h.db,
            DeliveryPlanCreateRequest(
                version=1,
                dataset_key=h.dataset,
                provider=h.provider,
                trade_date=DAY,
                release_id=release.release_id,
                execution_profile="pilot",
            ),
            allowed_datasets=[h.dataset],
        )
        get_settings().APP_ENVIRONMENT = h.environment
    full = await h.db.get(SchedulerControl, "full_market_twelve_data_v1")
    full.desired_state = "stopped"
    work = await provider_work.enqueue(
        h.db,
        provider=h.provider,
        stable_key=f"daily:pilot:{h.dataset}:{DAY}",
        kind="daily",
        dataset_key=h.dataset,
        trade_date=DAY,
        execution_profile="pilot",
        scope={"legacy_id": "original-plan"},
    )
    work_id = work.work_id
    following = await provider_work.enqueue(
        h.db,
        provider=h.provider,
        stable_key=f"daily:pilot:{h.dataset}:{DAY + timedelta(days=1)}",
        kind="daily",
        dataset_key=h.dataset,
        trade_date=DAY + timedelta(days=1),
        execution_profile="pilot",
        scope={"legacy_id": "next-original-plan"},
    )
    following_id = following.work_id
    await h.db.commit()
    with h.queue.host_lock(h.provider) as (_, inode):
        pass
    claimed = await h.client.post(
        "/api/v1/source/provider-work/claim",
        headers=h.headers,
        json={
            "protocol": "provider-work-v1",
            "host_lock_identity": inode,
            "execution_profile": "pilot",
        },
    )
    assert claimed.status_code == 200, claimed.text
    owner = claimed.json()["work"]
    fence = {"generation": owner["generation"], "token": owner["token"]}
    route = f"/api/v1/source/provider-work/{work_id}"
    if prepared:
        from findb_fetcher.full_market_universe import canonical_bytes

        manifest = await h.client.post(
            route + "/manifest",
            headers=h.headers,
            json={
                **fence,
                "manifest": {
                    "mode": "canonical",
                    "plan_id": str(plan.plan_id),
                    "members": [{"key": "00001"}],
                },
            },
        )
        assert manifest.status_code == 200, manifest.text
        body = _canonical_request(idempotency_key="retired-original")
        body.update(dataset_key=h.dataset, source=h.provider, fetched_at=utc_now().isoformat())
        body["request_key"] = plan.parts[0].work_item_id
        body["delivery"] = {
            "slot_id": "western_markets_window",
            "scheduled_for": utc_now().isoformat(),
            "target_data_date": DAY.isoformat(),
            "work_item_id": plan.parts[0].work_item_id,
        }
        body["payload"]["batch"]["delivery_mode"] = "incremental"
        body["payload"]["batch"]["data_date"] = DAY.isoformat()
        body["payload"]["data"][0].update(
            symbol="00001", source_symbol="00001", currency="HKD", trade_date=DAY.isoformat()
        )
        original = canonical_bytes(body).decode()
        intent = await h.client.post(
            route + "/prepare",
            headers=h.headers,
            json={**fence, "body": original, "stage": "intent"},
        )
        assert intent.status_code == 200, intent.text
    assert (await h.client.post(route + "/close", headers=h.headers, json=fence)).status_code == 200

    def run_cycle():
        with FullMarketSourceClient(h.config, client=h.http(), execution_profile="pilot") as source:
            runtime = FullMarketRuntime(
                provider=h.provider,
                config={"feeds": [{"provider": h.provider, "dataset_key": "us_equity_eod"}]},
                state=FullMarketState(h.path / "pilot.sqlite3"),
                source=source,
                delivery=None,
                raw_store=h.raw,
                execution_profile="pilot",
            )
            runtime._ensure_proof = lambda: None
            try:
                runtime._queued_cycle()
            finally:
                runtime.close()

    h.hold_terminal = prepared
    task = asyncio.create_task(asyncio.to_thread(run_cycle))
    try:
        if prepared:
            deadline = time.monotonic() + 10
            while not any(
                path.endswith("/settle") and status == 409 for path, status, _ in h.source_events
            ):
                assert not task.done(), h.source_events
                assert time.monotonic() < deadline, h.source_events
                await asyncio.sleep(0.01)
            with pytest.raises(h.queue.ProviderWorkWaitingError):
                with h.queue.host_lock(h.provider):
                    pytest.fail("normalization still requires the same owner")
            h.db.expire_all()
            assert (
                await h.db.get(ProviderWorkOwner, (h.environment, h.provider))
            ).attempt_id is not None
            from tests.test_canonical_ingest_api import _execute_run

            assert len(h.runs) == 1
            await _execute_run(h.db, h.engine, h.runs[0])
        await asyncio.wait_for(task, 20)
    except BaseException as exc:
        raise AssertionError(h.source_events) from exc
    h.db.expire_all()
    saved = await h.db.get(ProviderWork, work_id)
    assert saved.state == "failed"
    attempt = await h.db.scalar(
        select(ProviderWorkAttempt).where(ProviderWorkAttempt.work_id == work_id)
    )
    assert attempt.failure_code == "RETIRED_FEED_MANUAL_REVIEW"
    assert saved.scope == {"legacy_id": "original-plan"}
    assert not h.native_calls()
    assert (await h.db.get(ProviderWorkOwner, (h.environment, h.provider))).attempt_id is None
    assert (
        await h.db.get(SchedulerControl, "full_market_twelve_data_v1")
    ).desired_state == "stopped"
    receipts = list(
        await h.db.scalars(
            select(ProviderWorkReceipt).where(ProviderWorkReceipt.work_id == work_id)
        )
    )
    assert len(receipts) == int(prepared)
    if prepared:
        assert (await prepared_map(h.db, saved.work_id))["retired-original"]["body"] == original
        run = await h.db.get(IngestionRun, receipts[0].run_id)
        assert run.status == "completed" and run.success_records == 1
    # The next original stable work is claimable after retired settlement.
    next_claim = await h.client.post(
        "/api/v1/source/provider-work/claim",
        headers=h.headers,
        json={
            "protocol": "provider-work-v1",
            "host_lock_identity": inode,
            "execution_profile": "pilot",
        },
    )
    assert next_claim.status_code == 200, next_claim.text
    assert next_claim.json()["work"]["work_id"] == str(following_id)
