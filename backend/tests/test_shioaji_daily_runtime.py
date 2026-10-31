"""Daily windows cannot block independent work or discard original FIFO scope."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.provider_work import ProviderWorkAttempt
from app.models.registry import SchedulerControl
from app.services import provider_work as queue
from app.services.daily_runtime import daily_window
from app.services.provider_work import prepared_map
from tests.test_full_market import _dataset
from tests.test_provider_work import identity
from tests.test_provider_work_adapters import native_runtime as native_runtime


@pytest.mark.parametrize(
    ("hour", "minute", "opened"),
    [(17, 59, False), (18, 0, True), (23, 0, True), (0, 0, True), (5, 59, True), (6, 0, False)],
)
def test_authoritative_window_matches_exported_fetcher_policy(hour, minute, opened):
    import json
    from pathlib import Path

    from findb_fetcher.daily_runtime import window

    clock = datetime(2026, 10, 28, hour, minute, tzinfo=ZoneInfo("Asia/Taipei"))
    catalog = json.loads(
        (Path(__file__).parents[1] / "configs/execution_feeds.v1.json").read_text()
    )
    result = daily_window("shioaji", clock)
    assert result == window(catalog["daily_runtime_policies"]["shioaji"], clock)
    assert result["open"] is opened
    assert daily_window("finlab", clock)["open"] is True


async def setup_queue(db, monkeypatch):
    clock = [datetime(2026, 10, 28, 20, tzinfo=ZoneInfo("Asia/Taipei"))]
    monkeypatch.setattr(queue, "utc_now", lambda: clock[0])
    # The same clock must drive the live daily fence, not only queue selection.
    from app.services import daily_runtime

    monkeypatch.setattr(daily_runtime, "utc_now", lambda: clock[0])
    monkeypatch.setattr(get_settings(), "FULL_MARKET_ENABLED", True)
    keys = ["tw_equity_minute", "tw_etf_minute"]
    for key in keys:
        await _dataset(db, key)
    db.add(
        SchedulerControl(
            scheduler_key="full_market_shioaji_v1",
            provider="shioaji",
            execution_profile="full_market",
            timezone="Asia/Taipei",
            slot_id="taiwan_market_window",
            desired_state="running",
            observed_state="running",
        )
    )
    await db.flush()
    works = [
        await queue.enqueue(
            db,
            provider="shioaji",
            dataset_key=key,
            stable_key=f"daily:full_market:{key}:2026-10-27",
            kind="daily",
            execution_profile="full_market",
            trade_date=clock[0].date() - timedelta(days=1),
            scope={"frozen": key},
        )
        for key in keys
    ]
    await db.commit()
    return clock, keys, works


@pytest.mark.asyncio
async def test_slice_tail_retry_gives_etf_next_turn_and_preserves_frozen_work(
    test_session, monkeypatch
):
    clock, keys, works = await setup_queue(test_session, monkeypatch)
    claimed = await queue.claim(
        test_session,
        provider="shioaji",
        datasets=keys,
        host_lock_identity="local:1:22",
        now=clock[0],
    )
    assert claimed[0].work_id == works[0].work_id
    await queue.close(test_session, **identity(claimed))
    await queue.settle(
        test_session,
        **identity(claimed),
        outcome="retry",
        proof={
            "scheduling": {
                "processed": 25,
                "reason": "batch_yield",
                "elapsed_seconds": 12.5,
                "counts": {"pending": 75, "manual": 1},
                "failures": [{"member_key": "bad", "attempts": 8, "reason": "retry_exhausted"}],
            }
        },
        retry_at=clock[0] + timedelta(seconds=1),
    )
    clock[0] += timedelta(seconds=2)
    next_claim = await queue.claim(
        test_session,
        provider="shioaji",
        datasets=keys,
        host_lock_identity="local:1:22",
        now=clock[0],
    )
    assert next_claim[0].work_id == works[1].work_id
    rows = await queue.projection(test_session, provider="shioaji")
    stock = next(row for row in rows if row["dataset_key"] == keys[0])
    assert stock["last_batch"]["processed"] == 25
    assert stock["last_failure_code"] == "retry_exhausted"
    assert stock["attempt_number"] == 2
    assert works[0].scope == {"frozen": keys[0]}
    rounds = list(
        await test_session.scalars(
            select(ProviderWorkAttempt)
            .where(ProviderWorkAttempt.work_id == works[0].work_id)
            .order_by(ProviderWorkAttempt.attempt_number)
        )
    )
    assert rounds[0].failure_code is None
    assert rounds[1].queue_order > next_claim[1].queue_order
    # A later successful slice does not erase the most recent actual failure.
    await queue.close(test_session, **identity(next_claim))
    await queue.settle(
        test_session,
        **identity(next_claim),
        outcome="retry",
        proof={},
        retry_at=clock[0] + timedelta(hours=1),
    )
    stock_claim = await queue.claim(
        test_session,
        provider="shioaji",
        datasets=keys,
        host_lock_identity="local:1:22",
        now=clock[0],
    )
    await queue.close(test_session, **identity(stock_claim))
    await queue.settle(
        test_session,
        **identity(stock_claim),
        outcome="retry",
        proof={"scheduling": {"processed": 25, "reason": "batch_yield", "failures": []}},
        retry_at=clock[0] + timedelta(seconds=1),
    )
    stock = next(
        row
        for row in await queue.projection(test_session, provider="shioaji")
        if row["dataset_key"] == keys[0]
    )
    assert stock["last_batch"]["failures"] == []
    assert stock["last_failure_code"] == "retry_exhausted"


@pytest.mark.asyncio
async def test_live_close_blocks_daily_fence_but_maintenance_can_run_and_daily_resumes(
    test_session, monkeypatch
):
    clock, keys, works = await setup_queue(test_session, monkeypatch)
    clock[0] = datetime(2026, 10, 29, 5, 59, 59, tzinfo=ZoneInfo("Asia/Taipei"))
    claimed = await queue.claim(
        test_session,
        provider="shioaji",
        datasets=keys,
        host_lock_identity="local:1:22",
        now=clock[0],
    )
    clock[0] = datetime(2026, 10, 29, 6, tzinfo=ZoneInfo("Asia/Taipei"))
    # Keep the owner heartbeat live across the simulated boundary.
    await queue.heartbeat(test_session, **identity(claimed))
    with pytest.raises(queue.ProviderWorkSuspendedError):
        await queue.fence(test_session, acquisition=True, **identity(claimed))
    await queue.close(test_session, **identity(claimed))
    await queue.settle(
        test_session,
        **identity(claimed),
        outcome="retry",
        proof={"scheduling": {"reason": "acquisition_window_closed"}},
        retry_at=clock[0] + timedelta(seconds=1),
    )
    maintenance = await queue.enqueue(
        test_session,
        provider="shioaji",
        dataset_key=keys[0],
        stable_key="maintenance:window",
        kind="maintenance",
        trade_date=None,
        scope={},
    )
    await test_session.flush()
    maintenance_claim = await queue.claim(
        test_session,
        provider="shioaji",
        datasets=keys,
        host_lock_identity="local:1:22",
        now=clock[0],
    )
    assert maintenance_claim[0].work_id == maintenance.work_id
    await queue.fence(test_session, acquisition=True, **identity(maintenance_claim))
    await queue.freeze_manifest(
        test_session,
        **identity(maintenance_claim),
        manifest={
            "members": [{"key": "maintenance"}],
            "no_data_evidence": {"maintenance": {"test": "maintenance"}},
        },
    )
    await queue.close(test_session, **identity(maintenance_claim))
    await queue.settle(
        test_session,
        **identity(maintenance_claim),
        outcome="completed",
        proof={"no_data": ["maintenance"]},
    )
    rows = await queue.projection(test_session, provider="shioaji")
    assert all(
        row["wait_reason"] == "acquisition_window_closed" and row["position"] is None
        for row in rows
    )
    clock[0] = clock[0].replace(hour=18)
    resumed = await queue.claim(
        test_session,
        provider="shioaji",
        datasets=keys,
        host_lock_identity="local:1:22",
        now=clock[0],
    )
    assert resumed[0].work_id == works[1].work_id
    assert not resumed[1].drain_only


@pytest.mark.asyncio
@pytest.mark.parametrize("native_runtime", [("shioaji", "tw_equity_minute", 0)], indirect=True)
@pytest.mark.parametrize(
    ("close_after_response", "close_during_authorization"),
    [(False, False), (True, False), (False, True)],
)
async def test_owned_native_slice_drains_receipt_yields_to_etf_and_resumes_same_bytes(
    native_runtime, test_engine, monkeypatch, close_after_response, close_during_authorization
):
    import asyncio
    import json
    from pathlib import Path

    import httpx
    from findb_fetcher.full_market_client import FullMarketSourceClient
    from findb_fetcher.full_market_runtime import FullMarketRuntime
    from findb_fetcher.full_market_state import FullMarketState
    from findb_fetcher.platform_limits import limits

    from app.config import get_settings
    from app.models.provider_work import ProviderWork, ProviderWorkOwner, ProviderWorkReceipt
    from app.models.registry import DatasetRegistry, FullMarketEnvironment, SourceClient
    from app.services import daily_runtime
    from app.utils import utc_now
    from tests import test_full_market as fixtures
    from tests.provider_work_runtime_support import DAY

    h = native_runtime
    original_universe = fixtures._universe

    async def existing_dataset(db, key):
        return await db.get(DatasetRegistry, key)

    def mapped_universe(key, count=2, effective_date=DAY):
        body = original_universe(key, count, effective_date=DAY)
        for member, symbol in zip(body.members, ("2330", "2317"), strict=True):
            member.symbol = member.provider_symbol = symbol
        return body

    monkeypatch.setattr(fixtures, "_dataset", existing_dataset)
    monkeypatch.setattr(fixtures, "_universe", mapped_universe)
    monkeypatch.setattr(fixtures, "DAY", DAY)
    _, _, db_plan = await fixtures._activated_plan(h.db, h.dataset)
    get_settings().APP_ENVIRONMENT = h.environment
    h.db.add(FullMarketEnvironment(environment=h.environment, enabled=True))
    await _dataset(h.db, "tw_etf_minute")
    credential = await h.db.scalar(select(SourceClient).where(SourceClient.name == "native-matrix"))
    credential.allowed_datasets = [h.dataset, "tw_etf_minute"]
    feed = {
        "provider": "shioaji",
        "dataset_key": h.dataset,
        "market": "TW",
        "asset_class": "equity",
        "slot_id": "taiwan_market_window",
        "source_timezone": "Asia/Taipei",
    }
    work = await queue.enqueue(
        h.db,
        provider="shioaji",
        dataset_key=h.dataset,
        stable_key=f"daily:full_market:{h.dataset}:{DAY}",
        kind="daily",
        trade_date=DAY,
        execution_profile="full_market",
        scope={"feed": feed},
    )
    sibling = await queue.enqueue(
        h.db,
        provider="shioaji",
        dataset_key="tw_etf_minute",
        stable_key=f"daily:full_market:tw_etf_minute:{DAY}",
        kind="daily",
        trade_date=DAY,
        execution_profile="full_market",
        scope={},
    )
    work_id, sibling_id = work.work_id, sibling.work_id
    await h.db.commit()

    # Boundary semantics have dedicated clock tests; retain real grant clocks for native IPC.
    window_open = [True]

    def open_window(*args, **kwargs):
        if not window_open[0]:
            return {
                "open": False,
                "next_open_at": (utc_now() + timedelta(hours=12)).isoformat(),
                "closes_at": None,
            }
        return {
            "open": True,
            "next_open_at": None,
            "closes_at": (utc_now() + timedelta(hours=1)).isoformat(),
        }

    monkeypatch.setattr(daily_runtime, "daily_window", open_window)
    monkeypatch.setattr(queue, "daily_window", open_window)
    policy = json.loads(
        (Path(__file__).parents[1] / "configs/execution_feeds.v1.json").read_text()
    )["daily_runtime_policies"]["shioaji"]
    state_path = h.path / "daily.sqlite3"
    handle = h.handle

    def close_on_authorization(request):
        if (
            close_during_authorization
            and request.url.path == "/api/v1/source/full-market/authorization"
            and any(json.loads(call)[0] == "sdk_login" for call in h.native_calls())
        ):
            window_open[0] = False
        return handle(request)

    h.handle = close_on_authorization

    def run_slice():
        state = FullMarketState(state_path)
        state.bind_profile("full_market")
        with (
            FullMarketSourceClient(
                h.config, client=h.http(), execution_profile="full_market"
            ) as source,
            h.source_factory(h.config, h.registry) as delivery,
        ):
            runtime = FullMarketRuntime(
                provider="shioaji",
                config={
                    "feeds": [feed],
                    "daily_runtime_policies": {"shioaji": {**policy, "max_members": 1}},
                },
                state=state,
                source=source,
                delivery=delivery,
                raw_store=h.raw,
                client=httpx.Client(
                    transport=httpx.MockTransport(lambda request: pytest.fail("unexpected HTTP"))
                ),
            )
            runtime.proof = limits("shioaji")
            runtime.runtime_window = open_window
            original_acquire = runtime._acquire

            def acquire_and_close(*args):
                requests = original_acquire(*args)
                if close_after_response:
                    window_open[0] = False
                return requests

            runtime._acquire = acquire_and_close
            try:
                with h.queue.claimed_work(source, "shioaji", accepted_kinds=["daily"]) as owner:
                    assert owner.work["work_id"] == str(work_id)
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
                    state.record_plan(plan, "shioaji")
                    if close_during_authorization:
                        with state.connection() as db:
                            db.execute(
                                "UPDATE full_work SET attempts=7 WHERE key=(SELECT key FROM full_work ORDER BY key LIMIT 1)"
                            )
                    runtime._deliver_plan(plan, source.release(str(db_plan.release_id)), feed)
                    runtime._settle_queued_round(owner)
            finally:
                runtime.close()

    await asyncio.wait_for(asyncio.to_thread(run_slice), 30)
    h.db.expire_all()
    work = await h.db.get(ProviderWork, work_id)
    assert work.state == "queued"
    frozen, prepared = work.manifest, dict((await prepared_map(h.db, work.work_id)))
    if close_during_authorization:
        assert not h.runs
        assert not [call for call in h.native_calls() if json.loads(call)[0] == "kbars"]
        assert not [event for event in h.source_events if event[0].endswith("/outcomes")]
        state = FullMarketState(state_path)
        with state.connection() as db:
            row = db.execute(
                "SELECT status,attempts,reason FROM full_work ORDER BY key LIMIT 1"
            ).fetchone()
            assert tuple(row) == ("pending", 7, "acquisition_window_closed")
        assert (
            state.plan_progress(str(db_plan.plan_id))["last_batch"]["reason"]
            == "acquisition_window_closed"
        )
        assert (await h.db.get(ProviderWorkOwner, (h.environment, "shioaji"))).attempt_id is None
        return
    assert len(h.runs) == 1
    assert (await h.db.get(ProviderWorkOwner, (h.environment, "shioaji"))).attempt_id is None
    window_open[0] = True
    next_claim = await queue.claim(
        h.db,
        provider="shioaji",
        datasets=[h.dataset, "tw_etf_minute"],
        host_lock_identity=(await queue.lock_provider(h.db, "shioaji")).host_lock_identity,
    )
    assert next_claim[0].work_id == sibling_id
    await queue.close(h.db, **identity(next_claim))
    await queue.settle(
        h.db,
        **identity(next_claim),
        outcome="retry",
        proof={},
        retry_at=utc_now() + timedelta(hours=1),
    )
    attempt = await h.db.scalar(
        select(ProviderWorkAttempt).where(
            ProviderWorkAttempt.work_id == work_id, ProviderWorkAttempt.state == "queued"
        )
    )
    attempt.not_before = utc_now() - timedelta(seconds=1)
    await h.db.commit()
    await asyncio.wait_for(asyncio.to_thread(run_slice), 30)
    h.db.expire_all()
    work = await h.db.get(ProviderWork, work_id)
    assert work.state == "completed"
    assert work.manifest == frozen
    saved = await prepared_map(h.db, work.work_id)
    assert all(saved[key] == value for key, value in prepared.items())
    assert len(h.runs) == 2
    assert (
        len(
            list(
                await h.db.scalars(
                    select(ProviderWorkReceipt).where(ProviderWorkReceipt.work_id == work_id)
                )
            )
        )
        == 2
    )
    assert len([call for call in h.native_calls() if json.loads(call)[0] == "kbars"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("native_runtime", [("shioaji", "tw_equity_minute", 0)], indirect=True)
async def test_enabled_queue_freezes_source_sibling_before_dispatch_and_publication(
    native_runtime, test_engine, monkeypatch
):
    import asyncio
    import json
    from pathlib import Path

    import httpx
    from findb_fetcher import full_market_runtime as runtime_module
    from findb_fetcher.full_market_client import FullMarketSourceClient
    from findb_fetcher.full_market_state import FullMarketState

    from app.config import get_settings
    from app.models.registry import (
        DatasetRegistry,
        FullMarketAdmission,
        FullMarketAdmissionFeed,
        FullMarketDatasetState,
        FullMarketEnvironment,
        SourceClient,
    )
    from app.services import daily_runtime
    from app.utils import utc_now
    from tests import test_full_market as fixtures
    from tests.provider_work_runtime_support import DAY

    h = native_runtime

    async def existing_dataset(db, key):
        return await db.get(DatasetRegistry, key)

    monkeypatch.setattr(fixtures, "_dataset", existing_dataset)
    monkeypatch.setattr(fixtures, "DAY", DAY)
    await fixtures._activated_plan(h.db, h.dataset)
    await _dataset(h.db, "tw_etf_minute")
    _, etf_release = await fixtures._baseline(h.db, "tw_etf_minute", count=2, effective_date=DAY)
    admission = await h.db.scalar(
        select(FullMarketAdmission).where(
            FullMarketAdmission.scheduler_key == "full_market_shioaji_v1"
        )
    )
    h.db.add(
        FullMarketAdmissionFeed(
            admission_id=admission.admission_id,
            dataset_key="tw_etf_minute",
            baseline_id=etf_release.release_id,
            calendar_revision_id=None,
        )
    )
    h.db.add(
        FullMarketDatasetState(
            dataset_key="tw_etf_minute", execution_profile="full_market", first_start_date=DAY
        )
    )
    credential = await h.db.scalar(select(SourceClient).where(SourceClient.name == "native-matrix"))
    credential.allowed_datasets = [h.dataset, "tw_etf_minute"]
    get_settings().APP_ENVIRONMENT = h.environment
    h.db.add(FullMarketEnvironment(environment=h.environment, enabled=True))
    await h.db.commit()

    def open_window(*args, **kwargs):
        return {
            "open": True,
            "next_open_at": None,
            "closes_at": (utc_now() + timedelta(hours=1)).isoformat(),
        }

    monkeypatch.setattr(daily_runtime, "daily_window", open_window)
    monkeypatch.setattr(queue, "daily_window", open_window)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return (
                datetime.combine(DAY, datetime.min.time(), ZoneInfo("Asia/Taipei"))
                .replace(hour=20)
                .astimezone(tz)
            )

    monkeypatch.setattr(runtime_module, "datetime", Clock)
    feeds = [
        {
            "provider": "shioaji",
            "dataset_key": key,
            "market": "TW",
            "asset_class": "equity" if key == h.dataset else "etf",
            "slot_id": "taiwan_market_window",
            "source_timezone": "Asia/Taipei",
            "scheduled_time": "18:00",
            "target_date_lag_days": 0,
        }
        for key in credential.allowed_datasets
    ]
    policy = json.loads(
        (Path(__file__).parents[1] / "configs/execution_feeds.v1.json").read_text()
    )["daily_runtime_policies"]["shioaji"]
    observations = []

    def run_cycle():
        state = FullMarketState(h.path / "siblings.sqlite3")
        state.refresh("shioaji", now=0, result=[], next_at=9999999999)
        with (
            FullMarketSourceClient(
                h.config, client=h.http(), execution_profile="full_market"
            ) as source,
            h.source_factory(h.config, h.registry) as delivery,
        ):
            runtime = runtime_module.FullMarketRuntime(
                provider="shioaji",
                config={"feeds": feeds, "daily_runtime_policies": {"shioaji": policy}},
                state=state,
                source=source,
                delivery=delivery,
                raw_store=h.raw,
                client=httpx.Client(
                    transport=httpx.MockTransport(
                        lambda request: pytest.fail("unexpected provider HTTP")
                    )
                ),
            )
            runtime.runtime_window = open_window

            def dispatch(plan, release, feed, **kwargs):
                siblings = [
                    source.plans_for_date(dataset=entry["dataset_key"], trade_date=DAY.isoformat())[
                        "data"
                    ]
                    for entry in feeds
                ]
                assert all(len(plans) == 1 for plans in siblings)
                observations.append(
                    (
                        plan["dataset_key"],
                        release["release_id"],
                        sum(len(part["member_keys"]) for part in plan["parts"]),
                    )
                )
                return True

            runtime._deliver_plan = dispatch
            try:
                runtime._queued_cycle()
            finally:
                runtime.close()

    await asyncio.wait_for(asyncio.to_thread(run_cycle), 30)
    assert observations[0][0] == h.dataset
    new_release = await fixtures.submit_universe(
        h.db,
        fixtures._universe("tw_etf_minute", count=3, effective_date=DAY),
        allowed_datasets=["tw_etf_minute"],
    )
    await fixtures.publish_universe(
        h.db,
        new_release.release_id,
        fixtures.UniversePublishRequest(
            evidence_note="Verified revised official universe.", threshold_exception_approved=True
        ),
        actor="test-owner",
    )
    await h.db.commit()
    assert new_release.release_id != etf_release.release_id
    await asyncio.wait_for(asyncio.to_thread(run_cycle), 30)
    assert observations[1] == ("tw_etf_minute", str(etf_release.release_id), 2)
