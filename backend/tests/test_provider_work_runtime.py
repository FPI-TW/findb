"""Actual Fetcher coordinators traverse the authenticated Source/DB FIFO.

Only the external provider acquisition and R2 upload are replaced. Prepared
Source delivery, normalization, canonical proof, leases, journals and host locks
are real. The sync transport crosses to the ASGI app on the pytest event loop.
"""

import asyncio
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.canonical import CalendarYearRevision
from app.models.provider_work import ProviderWork
from app.models.registry import SchedulerControl, SchedulerDataset
from app.services import historical_backfill, provider_work
from app.services.source_clients import create_source_client
from app.utils import utc_now
from tests.full_market_fixtures import historical_admission
from tests.test_canonical_ingest_api import _execute_run
from tests.test_full_market import DAY, _activated_plan


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stop_before_confirmation,close_crash,temporary_rejection,overlap",
    [
        (False, False, None, False),
        (True, False, None, False),
        (False, True, None, False),
        (False, False, 503, False),
        (False, False, 429, False),
        (False, False, 503, True),
    ],
    ids=[
        "running-handoff",
        "daily-stop-prepared-drain",
        "closed-restart",
        "503-retry-then-stop",
        "429-retry-then-stop",
        "503-tail-retry-after-newer-pilot-write",
    ],
)
async def test_daily_historical_daily_handoff_with_real_fenced_source_and_proofs(
    client,
    test_session,
    test_engine,
    monkeypatch,
    tmp_path,
    stop_before_confirmation,
    close_crash,
    temporary_rejection,
    overlap,
):
    root = Path(__file__).resolve().parents[2]
    from tests.test_scheduler_control import _admin_headers

    owner_headers = await _admin_headers(test_session)
    monkeypatch.syspath_prepend(str(root / "fetcher/src"))
    from findb_fetcher import historical_backfill as history_client
    from findb_fetcher import historical_runtime
    from findb_fetcher import provider_work as coordinator
    from findb_fetcher.client import SourceAPIClient
    from findb_fetcher.config import FetcherConfig
    from findb_fetcher.contracts import ContractRegistry
    from findb_fetcher.full_market_client import FullMarketSourceClient
    from findb_fetcher.full_market_runtime import FullMarketRuntime
    from findb_fetcher.full_market_state import FullMarketState
    from findb_fetcher.providers.finlab import FinLabDatasetTable
    from findb_fetcher.raw_storage import RawObject

    monkeypatch.setattr(get_settings(), "PROVIDER_WORK_REQUIRED", True)
    monkeypatch.setenv("PROVIDER_WORK_REQUIRED", "true")
    monkeypatch.setenv("APP_ENVIRONMENT", "local")
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(tmp_path / "account/governor.sqlite3"))
    from tests.test_provider_work import attest_cutover

    inodes = {}
    for provider in ("finlab", "shioaji", "taifex", "twelve_data"):
        with coordinator.host_lock(provider) as (_, inode):
            inodes[provider] = inode
    await attest_cutover(test_session, inodes)
    monkeypatch.setenv(
        "FETCHER_HISTORICAL_SCHEDULE_FILE", str(root / "fetcher/configs/daily_scheduler.v2.json")
    )
    monkeypatch.setenv(
        "FETCHER_FINLAB_UNIVERSE_FILE",
        str(root / "fetcher/configs/finlab_tw_review_required.v1.json"),
    )
    dataset, _, daily_plan = await _activated_plan(test_session, "tw_equity_eod", count=2)
    release = daily_plan.release_id
    from app.models.registry import UniverseRelease

    baseline = await test_session.get(UniverseRelease, release)
    calendar_id = await test_session.scalar(
        select(CalendarYearRevision.id).where(CalendarYearRevision.market == "TW")
    )
    await historical_admission(test_session, dataset, baseline, DAY, calendar_id, profile="pilot")
    from copy import deepcopy

    from app.models.registry import DatasetRegistry
    from scripts.seed_data import DATASETS

    test_session.add(
        DatasetRegistry(
            **deepcopy(next(item for item in DATASETS if item["dataset_key"] == "tw_etf_eod"))
        )
    )
    for profile in ("pilot", "full_market"):
        key = f"{profile}_finlab_v1"
        control = await test_session.get(SchedulerControl, key)
        control.observed_state = "running"
        test_session.add(SchedulerDataset(scheduler_key=key, dataset_key="tw_equity_eod"))
        test_session.add(SchedulerDataset(scheduler_key=key, dataset_key="tw_etf_eod"))
    _, secret = await create_source_client(
        test_session,
        name="fifo-bridge",
        source_name="finlab",
        allowed_datasets=["tw_equity_eod"],
        rate_limit_requests=100000,
        rate_limit_window=60,
        commit=False,
    )
    await test_session.commit()
    config = FetcherConfig(
        "https://source.test", secret, contracts_dir=root / "contracts", max_attempts=1
    )
    monkeypatch.setattr(FetcherConfig, "from_env", classmethod(lambda cls: config))
    registry = ContractRegistry(config.contracts_dir)
    loop = asyncio.get_running_loop()
    events = []
    responses = []
    prepared, confirm, accepted, terminal = (asyncio.Event() for _ in range(4))
    pending = []
    paused = False
    rejected = False
    crashed = False

    class Transport(httpx.BaseTransport):
        def handle_request(self, request):
            async def invoke():
                nonlocal paused, rejected, crashed
                if (
                    temporary_rejection
                    and not rejected
                    and request.url.path == "/api/v1/source/ingest"
                ):
                    rejected = True
                    return httpx.Response(
                        temporary_rejection,
                        json={"detail": "temporary Source rejection"},
                        request=request,
                    )
                if (
                    stop_before_confirmation
                    and not paused
                    and request.url.path.endswith("/prepare")
                    and json.loads(request.content).get("stage") == "confirm"
                ):
                    paused = True
                    prepared.set()
                    await confirm.wait()
                if (
                    stop_before_confirmation
                    and request.url.path.startswith("/api/v1/source/runs/")
                    and pending
                ):
                    accepted.set()
                    await terminal.wait()
                response = await client.request(
                    request.method,
                    str(request.url),
                    content=request.content,
                    headers=dict(request.headers),
                )
                if response.status_code >= 400:
                    responses.append((request.url.path, response.status_code, response.text))
                if (
                    close_crash
                    and not crashed
                    and request.url.path.endswith("/close")
                    and response.status_code == 200
                ):
                    crashed = True
                    raise coordinator.ProviderWorkRecoveryBlockedError(
                        "lost close acknowledgement after commit"
                    )
                if request.url.path == "/api/v1/source/ingest" and response.status_code == 202:
                    run_id = UUID(response.json()["run_id"])
                    if stop_before_confirmation and not terminal.is_set():
                        pending.append(run_id)
                    else:
                        await _execute_run(test_session, test_engine, run_id)
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=response.content,
                    request=request,
                )

            return asyncio.run_coroutine_threadsafe(invoke(), loop).result(timeout=20)

    def source_factory(value, *, client=None, execution_profile="full_market"):
        return FullMarketSourceClient(
            value,
            client=client or httpx.Client(transport=Transport()),
            execution_profile=execution_profile,
        )

    monkeypatch.setattr(history_client, "FullMarketSourceClient", source_factory)
    monkeypatch.setattr(
        historical_runtime,
        "SourceAPIClient",
        lambda value, contracts: SourceAPIClient(
            value, contracts, client=httpx.Client(transport=Transport())
        ),
    )

    class Raw:
        def persist(self, body, **kwargs):
            digest = hashlib.sha256(body).hexdigest()
            return RawObject(
                f"r2://{'a' * 32}/raw/finlab/tw_equity_eod/{digest}.json", digest, len(body)
            )

    class Provider:
        def fetch_dataset(self, name, *, target_date, symbols):
            owner = coordinator.active()
            assert owner and owner.work["kind"] == "historical"
            events.append(("historical-acquisition", owner.work["work_id"]))
            with pytest.raises(coordinator.ProviderWorkWaitingError):
                with coordinator.host_lock("finlab"):
                    pass
            return FinLabDatasetTable(
                (target_date.isoformat(),),
                tuple(symbols),
                (tuple(10 if "成交" in name else 1 for _ in symbols),),
            )

    monkeypatch.setattr(
        historical_runtime.FinLabSdkGateway, "from_env", classmethod(lambda cls: Provider())
    )
    monkeypatch.setattr(historical_runtime, "R2RawPayloadStore", lambda value: Raw())
    monkeypatch.setattr(
        historical_runtime.RawStorageConfig, "from_env", classmethod(lambda cls: None)
    )
    feed = {
        "provider": "finlab",
        "dataset_key": "tw_equity_eod",
        "market": "TW",
        "asset_class": "equity",
        "source_timezone": "Asia/Taipei",
        "scheduled_time": "00:00",
        "target_date_lag_days": 0,
        "completion_window_seconds": 86400,
        "slot_id": "taiwan_market_window",
    }
    first = await provider_work.enqueue(
        test_session,
        provider="finlab",
        stable_key=f"daily:full_market:tw_equity_eod:{DAY}",
        kind="daily",
        dataset_key="tw_equity_eod",
        trade_date=DAY,
        execution_profile="full_market",
        scope={"feed": feed},
    )
    historical, _ = await historical_backfill.create_request(
        test_session,
        provider="finlab",
        dataset_key="tw_equity_eod",
        start_date=date(2026, 10, 15),
        end_date=date(2026, 10, 15),
        request_key="actual-runtime-handoff",
        created_by="owner",
    )
    third = await provider_work.enqueue(
        test_session,
        provider="finlab",
        stable_key=f"daily:pilot:tw_equity_eod:{DAY}",
        kind="daily",
        dataset_key="tw_equity_eod",
        trade_date=DAY,
        execution_profile="pilot",
        scope={"feed": feed},
    )
    expected = [first.work_id, historical.items[0].item_id, third.work_id]
    await test_session.commit()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return (
                datetime(2026, 7, 21, 8, tzinfo=timezone.utc).astimezone(tz)
                if tz
                else datetime(2026, 7, 21, 8)
            )

    import findb_fetcher.full_market_runtime as daily_runtime

    monkeypatch.setattr(daily_runtime, "datetime", Clock)

    def run_daily(profile):
        state = FullMarketState(tmp_path / f"{profile}.sqlite3")
        state.bind_profile(profile)
        state.refresh("finlab", now=0, result=[], next_at=2**40)
        with (
            source_factory(config, execution_profile=profile) as source,
            SourceAPIClient(
                config, registry, client=httpx.Client(transport=Transport())
            ) as delivery,
        ):
            runtime = FullMarketRuntime(
                provider="finlab",
                config={"feeds": [feed]},
                state=state,
                source=source,
                delivery=delivery,
                raw_store=Raw(),
                execution_profile=profile,
            )

            def acquire(member, plan, part, release, feed):
                owner = coordinator.active()
                assert owner and owner.work["execution_profile"] == profile
                events.append((f"{profile}-acquisition", owner.work["work_id"]))
                with pytest.raises(coordinator.ProviderWorkWaitingError):
                    with coordinator.host_lock("finlab"):
                        pass
                identity = f"{profile}:{member['symbol']}"
                return [
                    {
                        "dataset_key": "tw_equity_eod",
                        "schema_id": "market_eod",
                        "schema_version": 1,
                        "source": "finlab",
                        "request_key": identity,
                        "idempotency_key": identity,
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                        "delivery": {
                            "slot_id": "taiwan_market_window",
                            "scheduled_for": datetime.now(timezone.utc).isoformat(),
                            "target_data_date": str(DAY),
                            "work_item_id": part["work_item_id"],
                        },
                        "payload": {
                            "batch": {
                                "data_date": str(DAY),
                                "delivery_mode": "incremental",
                                "declared_record_count": 1,
                            },
                            "data": [
                                {
                                    "symbol": member["symbol"],
                                    "source_symbol": member["provider_symbol"],
                                    "trade_date": str(DAY),
                                    "currency": "TWD",
                                    "open": "1",
                                    "high": "1",
                                    "low": "1",
                                    "close": "1",
                                    "volume": 10,
                                }
                            ],
                        },
                    }
                ]

            runtime._acquire = acquire
            try:
                runtime.cycle()
            finally:
                runtime.close()

    full_task = asyncio.create_task(asyncio.to_thread(run_daily, "full_market"))
    try:
        if stop_before_confirmation:
            await asyncio.wait_for(prepared.wait(), 15)
            control = await test_session.get(SchedulerControl, "full_market_finlab_v1")
            response = await client.patch(
                "/api/v1/admin/schedulers/full_market_finlab_v1",
                headers=owner_headers,
                json={"desired_state": "stopped", "expected_revision": control.revision},
            )
            assert response.status_code == 200, response.text
            assert len(events) == 1 and not pending
            confirm.set()
            await asyncio.wait_for(accepted.wait(), 15)
            with pytest.raises(coordinator.ProviderWorkWaitingError):
                with coordinator.host_lock("finlab"):
                    pytest.fail("nonterminal accepted run must retain the whole-work lock")
            assert (
                await provider_work.claim(
                    test_session,
                    provider="finlab",
                    datasets=["tw_equity_eod"],
                    host_lock_identity=inodes["finlab"],
                    recover=False,
                )
                is None
            )
            await test_session.commit()
            for run_id in pending:
                await _execute_run(test_session, test_engine, run_id)
            terminal.set()
        if close_crash:
            with pytest.raises(coordinator.ProviderWorkRecoveryBlockedError):
                await asyncio.wait_for(full_task, 25)
            import os

            before_restart = list(events)
            for descriptor in coordinator._quarantine:
                os.close(descriptor)
            coordinator._quarantine.clear()
            await asyncio.to_thread(run_daily, "full_market")
            assert events == before_restart
        else:
            await asyncio.wait_for(full_task, 25)
    finally:
        confirm.set()
        terminal.set()
        if not full_task.done():
            await asyncio.wait_for(full_task, 25)
    runner = historical_runtime.FinLabHistoricalRunner()

    if temporary_rejection:
        test_session.expire_all()
        control = await test_session.get(SchedulerControl, "full_market_finlab_v1")
        response = await client.patch(
            "/api/v1/admin/schedulers/full_market_finlab_v1",
            headers=owner_headers,
            json={"desired_state": "stopped", "expected_revision": control.revision},
        )
        assert response.status_code == 200, response.text

    def run_history():
        with history_client.HistoricalControlClient(
            config, client=httpx.Client(transport=Transport()), provider="finlab"
        ) as control:
            assert history_client.run_once(control, runner)

    if temporary_rejection:
        from app.models.provider_work import ProviderWorkAttempt

        if overlap:
            await asyncio.to_thread(run_history)
            await asyncio.to_thread(run_daily, "pilot")

        retry = await test_session.scalar(
            select(ProviderWorkAttempt).where(
                ProviderWorkAttempt.work_id == expected[0], ProviderWorkAttempt.state == "queued"
            )
        )
        assert retry and retry.attempt_number == 2
        retry.not_before = utc_now() - timedelta(seconds=1)
        others = list(
            await test_session.scalars(
                select(ProviderWorkAttempt).where(
                    ProviderWorkAttempt.work_id != expected[0],
                    ProviderWorkAttempt.state == "queued",
                )
            )
        )
        for attempt in others:
            if not overlap:
                attempt.not_before = utc_now() + timedelta(minutes=5)
        await test_session.commit()
        with FullMarketState(tmp_path / "full_market.sqlite3").connection() as db:
            db.execute("UPDATE full_work SET next_at=0")
        previous_events = list(events)
        await asyncio.to_thread(run_daily, "full_market")
        assert events == previous_events
        for attempt in others:
            attempt.not_before = utc_now() - timedelta(seconds=1)
        await test_session.commit()
    if not overlap:
        await asyncio.to_thread(run_history)
        await asyncio.to_thread(run_daily, "pilot")
    test_session.expire_all()
    work = list(await test_session.scalars(select(ProviderWork).order_by(ProviderWork.created_at)))
    with FullMarketState(tmp_path / "full_market.sqlite3").connection() as db:
        state_rows = [
            tuple(row)
            for row in db.execute(
                "SELECT member_key,status,reason,attempts,lease_until,next_at,body IS NOT NULL FROM full_work"
            )
        ]
    assert len(work) == 3
    assert [row.state for row in work] == (
        ["failed", "completed", "completed"]
        if overlap
        else ["queued", "completed", "completed"]
        if stop_before_confirmation or temporary_rejection
        else ["completed"] * 3
    ), (responses, state_rows)
    full_count = 1 if stop_before_confirmation or temporary_rejection else 2
    assert [event[0] for event in events] == ["full_market-acquisition"] * full_count + [
        "historical-acquisition"
    ] * 5 + ["pilot-acquisition"] * 2
    assert len({row[1] for row in events[:full_count]}) == 1 and events[0][1] == str(expected[0])
    assert events[-1][1] == str(expected[2])
    if stop_before_confirmation or temporary_rejection or overlap:
        from app.services.full_market import get_plan

        summary = (
            await get_plan(
                test_session,
                daily_plan.plan_id,
                provider="finlab",
                allowed_datasets=["tw_equity_eod"],
            )
        ).summary
        assert (
            summary.expected,
            summary.data,
            summary.no_data,
            summary.missing,
            summary.blocked,
        ) == ((2, 0, 0, 2, 0) if overlap else (2, 1, 0, 1, 0)), state_rows
        assert all(gap["reason"] is None for gap in summary.gaps)
    if overlap:
        from app.models.provider_work import ProviderWorkAttempt, ProviderWorkOwner
        from app.models.registry import IngestionRun

        round = await test_session.scalar(
            select(ProviderWorkAttempt).where(
                ProviderWorkAttempt.work_id == expected[0], ProviderWorkAttempt.attempt_number == 2
            )
        )
        assert round.outcome == "failed" and round.failure_code == "MANUAL_INTERVENTION_REQUIRED"
        assert round.proof["manual_reason"] == "frozen_scope_requires_manual_review"
        assert work[0].prepared_deliveries and len(work[0].prepared_deliveries) == 1
        assert await test_session.scalar(
            select(IngestionRun.run_id).where(
                IngestionRun.success_records == 0, IngestionRun.status == "completed"
            )
        )
        assert (
            await test_session.get(ProviderWorkOwner, (get_settings().APP_ENVIRONMENT, "finlab"))
        ).attempt_id is None
    assert all(
        control.desired_state
        == (
            "stopped"
            if (stop_before_confirmation or temporary_rejection)
            and control.execution_profile == "full_market"
            else "running"
        )
        for control in await test_session.scalars(
            select(SchedulerControl).where(SchedulerControl.provider == "finlab")
        )
    )
    if (stop_before_confirmation or temporary_rejection) and not overlap:
        from app.models.provider_work import ProviderWorkAttempt, ProviderWorkOwner
        from app.models.registry import FullMarketDatasetState
        from app.services.full_market import get_plan

        retained = dict(work[0].prepared_deliveries)
        frozen_manifest = dict(work[0].manifest)
        first_date = (
            await test_session.get(FullMarketDatasetState, ("full_market", "tw_equity_eod"))
        ).first_start_date
        pending_attempt = await test_session.scalar(
            select(ProviderWorkAttempt).where(
                ProviderWorkAttempt.work_id == expected[0], ProviderWorkAttempt.state == "queued"
            )
        )
        pending_attempt.not_before = utc_now() - timedelta(seconds=1)
        await test_session.commit()
        following = await provider_work.enqueue(
            test_session,
            provider="finlab",
            stable_key="maintenance:after-stopped-drain",
            kind="maintenance",
            dataset_key="tw_equity_eod",
            trade_date=None,
        )
        await test_session.commit()
        headers = {"X-API-Key": secret}
        with coordinator.host_lock("finlab") as (_, inode):
            claimed = await client.post(
                "/api/v1/source/provider-work/claim",
                headers=headers,
                json={
                    "protocol": "provider-work-v1",
                    "host_lock_identity": inode,
                    "accepted_kinds": ["maintenance"],
                },
            )
            assert claimed.status_code == 200, claimed.text
            active = claimed.json()["work"]
            assert active and active["work_id"] == str(following.work_id), (
                "a stopped fully drained head must not require a historical/daily cleanup poll"
            )
            fence = {"generation": active["generation"], "token": active["token"]}
            path = f"/api/v1/source/provider-work/{active['work_id']}"
            assert (
                await client.post(path + "/close", headers=headers, json=fence)
            ).status_code == 200
            assert (
                await client.post(
                    path + "/settle",
                    headers=headers,
                    json={**fence, "outcome": "cancelled", "proof": {}},
                )
            ).status_code == 200
        paused = await provider_work.projection(test_session, provider="finlab")
        assert (
            next(row for row in paused if row["work_id"] == str(expected[0]))["wait_reason"]
            == "suspended"
        )
        control = await test_session.get(SchedulerControl, "full_market_finlab_v1")
        restarted = await client.patch(
            "/api/v1/admin/schedulers/full_market_finlab_v1",
            headers=owner_headers,
            json={"desired_state": "running", "expected_revision": control.revision},
        )
        assert restarted.status_code == 200, restarted.text
        pending_attempt = await test_session.scalar(
            select(ProviderWorkAttempt).where(
                ProviderWorkAttempt.work_id == expected[0], ProviderWorkAttempt.state == "queued"
            )
        )
        assert pending_attempt and pending_attempt.drain_only
        assert (
            await test_session.get(ProviderWorkOwner, (get_settings().APP_ENVIRONMENT, "finlab"))
        ).attempt_id is None
        pending_attempt.not_before = utc_now() - timedelta(seconds=1)
        await test_session.commit()
        with FullMarketState(tmp_path / "full_market.sqlite3").connection() as db:
            db.execute("UPDATE full_work SET next_at=0")
        before = len(events)
        await asyncio.to_thread(run_daily, "full_market")
        test_session.expire_all()
        resumed = await test_session.get(ProviderWork, expected[0])
        assert resumed.state == "completed" and resumed.manifest == frozen_manifest
        assert all(
            resumed.prepared_deliveries[key]["body"] == value["body"]
            for key, value in retained.items()
        )
        assert [event[0] for event in events[before:]] == ["full_market-acquisition"]
        assert events[-1][1] == str(expected[0]), "legal start resumes the same stable frozen work"
        assert (
            await test_session.get(FullMarketDatasetState, ("full_market", "tw_equity_eod"))
        ).first_start_date == first_date
        plan = await get_plan(
            test_session, daily_plan.plan_id, provider="finlab", allowed_datasets=["tw_equity_eod"]
        )
        assert (plan.summary.expected, plan.summary.data, plan.summary.missing) == (2, 2, 0)
