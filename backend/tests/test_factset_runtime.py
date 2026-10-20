"""Fenced FactSet runner → Source → raw/queue → canonical → Serve acceptance."""

import asyncio
import hashlib
import json
import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.models.provider_work import ProviderWork, ProviderWorkOwner
from app.models.raw import RawMarketPayload
from app.models.registry import DeliveryMemberEvidence, HistoricalBackfillRequest, IngestionRun
from app.services.historical_backfill import create_request
from tests import test_factset_historical as fixtures
from tests.provider_work_runtime_support import RuntimeHarness
from tests.test_provider_work import attest_cutover


async def factset_harness(client, db, engine, patch, path, *, members=None, snapshots=None):
    root = Path(__file__).resolve().parents[2]
    patch.syspath_prepend(str(root / "fetcher/src"))
    from findb_fetcher import factset_eod
    from findb_fetcher import historical_backfill as history
    from findb_fetcher import provider_work as coordinator
    from findb_fetcher.client import SourceAPIClient
    from findb_fetcher.config import FetcherConfig
    from findb_fetcher.contracts import ContractRegistry
    from findb_fetcher.factset_historical import FactSetHistoricalRunner
    from findb_fetcher.full_market_client import FullMarketSourceClient
    from findb_fetcher.providers.factset import encode_native
    from findb_fetcher.raw_storage import RawObject

    h = RuntimeHarness(client, db, engine, patch, path, "factset", "hk_equity_eod", 2)
    h.environment, h.queue = "local", coordinator
    for key, value in {
        "APP_ENVIRONMENT": "local",
        "PROVIDER_WORK_REQUIRED": "true",
        "FETCHER_ACCOUNT_STATE_PATH": str(path / "account/governor.sqlite3"),
    }.items():
        patch.setenv(key, value)
    headers, _ = await fixtures.setup(db, patch, members=members)

    # Native snapshots and Source validation share the fixture's aware UTC clock.
    # Keep existing lease durations, monotonic deadlines and account pacing.
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixtures.NOW.astimezone(tz) if tz is not None else fixtures.NOW

    patch.setattr(factset_eod, "datetime", Clock)
    identities = {}
    for provider in ("factset", "finlab", "shioaji", "taifex", "twelve_data"):
        with coordinator.host_lock(provider) as (_, identity):
            identities[provider] = identity
    await attest_cutover(db, identities)
    request, _ = await create_request(
        db,
        provider="factset",
        dataset_key="hk_equity_eod",
        start_date=date(2026, 10, 15),
        end_date=date(2026, 10, 16),
        request_key="factset-runtime",
        created_by="test-owner",
    )
    await db.commit()
    # Long native cohorts cross heartbeat boundaries. Match production's
    # per-request sessions so heartbeat and delivery never share AsyncSession.
    from app.dependencies import get_db
    from app.main import app

    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def independent_request_session():
        async with sessions() as session:
            yield session

    patch.setitem(app.dependency_overrides, get_db, independent_request_session)
    h.request_id = request.request_id
    h.config = FetcherConfig(
        "https://source.test",
        headers["X-API-Key"],
        contracts_dir=root / "contracts",
        max_attempts=1,
    )
    h.registry = ContractRegistry(h.config.contracts_dir)
    patch.setattr(
        history,
        "FullMarketSourceClient",
        lambda config, *, client=None: FullMarketSourceClient(config, client=client or h.http()),
    )
    patch.setattr(get_settings(), "SERVE_REQUIRE_AUTH", False)

    class Gateway:
        def prices(self, day, identities):
            h.native(["prices", day.isoformat(), len(identities)])
            if snapshots is not None:
                return snapshots[day.isoformat()]
            return encode_native(
                [
                    {
                        "fsym_id": identity,
                        "price_date": day.isoformat(),
                        "currency": "HKD",
                        "price": 10,
                        "price_open": 10,
                        "price_high": 11 if i == 0 else 1,
                        "price_low": 9,
                        "volume": 100,
                    }
                    for i, identity in enumerate(identities)
                ],
                scope={"trade_date": day, "listing_ids": identities},
            )

    class Raw:
        def persist(self, raw, **kwargs):
            digest = hashlib.sha256(raw).hexdigest()
            (path / f"{digest}.json").write_bytes(raw)
            return RawObject(
                f"r2://{'a' * 32}/fixture-raw/factset/hk_equity_eod/{digest}.json", digest, len(raw)
            )

    h.gateway, h.raw = Gateway(), Raw()

    def runner():
        return FactSetHistoricalRunner(
            gateway=h.gateway,
            delivery=SourceAPIClient(h.config, h.registry, client=h.http(), sleep=lambda _: None),
            raw_store=h.raw,
            feed={"slot_id": "asia_pacific_markets_window"},
        )

    h.runner = runner
    return h


@pytest.mark.asyncio
@pytest.mark.parametrize("clock_offset", [timedelta(), timedelta(days=1)])
async def test_factset_fenced_runner_partial_continues_and_serve_only_has_valid_rows(
    client, test_session, test_engine, monkeypatch, tmp_path, clock_offset
):
    monkeypatch.setattr(fixtures, "NOW", fixtures.NOW + clock_offset)
    h = await factset_harness(client, test_session, test_engine, monkeypatch, tmp_path)
    try:
        assert await h.run(), h.failure
        assert await h.run(), h.failure
        assert len(h.native_calls()) == 2
        request = await test_session.get(
            HistoricalBackfillRequest, h.request_id, populate_existing=True
        )
        assert request.status == "partial"
        runs = list(await test_session.scalars(select(IngestionRun)))
        assert len(runs) == 4
        fetched_at = list(await test_session.scalars(select(RawMarketPayload.fetched_at)))
        assert fetched_at == [fixtures.NOW] * 4
        assert (
            sorted(run.status for run in runs) == ["completed"] * 2 + ["completed_with_errors"] * 2
        )
        evidence = list(await test_session.scalars(select(DeliveryMemberEvidence)))
        assert len(evidence) == 2
        works = list(await test_session.scalars(select(ProviderWork)))
        assert all(work.state == "partial" for work in works)
        owner = await test_session.get(ProviderWorkOwner, ("local", "factset"))
        assert owner.attempt_id is None
        response = await client.get(
            "/api/v1/serve/eod", params={"market": "HK", "symbols": "00001"}
        )
        assert response.status_code == 200, response.text
        assert len(response.json()["data"]) == 2
        invalid = await client.get("/api/v1/serve/eod", params={"market": "HK", "symbols": "00002"})
        assert invalid.status_code == 404  # Rejected rows never create the instrument.
    finally:
        h.release_process_lock()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["pilot", "full_market"])
async def test_daily_factset_partial_keeps_frozen_plan_and_prepared_receipts(
    client, test_session, test_engine, monkeypatch, tmp_path, profile
):
    h = await factset_harness(client, test_session, test_engine, monkeypatch, tmp_path)
    from findb_fetcher import full_market_runtime as runtime
    from findb_fetcher.client import SourceAPIClient
    from findb_fetcher.full_market_client import FullMarketSourceClient
    from findb_fetcher.full_market_state import FullMarketState
    from findb_fetcher.provider_work import claimed_work

    from app.models.registry import DatasetRegistry, UniverseRelease
    from app.schemas.full_market import DeliveryPlanCreateRequest
    from app.services import provider_work as queue
    from app.services.full_market import create_plan
    from tests.full_market_fixtures import historical_admission
    from tests.test_factset_historical import NOW

    monkeypatch.setattr(runtime.FactSetConfig, "from_env", classmethod(lambda cls: None))
    monkeypatch.setattr(runtime, "FactSetGateway", lambda config: h.gateway)
    try:
        assert await h.run() and await h.run(), h.failure
        dataset = await test_session.get(DatasetRegistry, "hk_equity_eod")
        release = await test_session.scalar(
            select(UniverseRelease).where(UniverseRelease.provider == "factset")
        )
        await historical_admission(
            test_session, dataset, release, NOW.date(), None, profile=profile
        )
        plan = await create_plan(
            test_session,
            DeliveryPlanCreateRequest(
                version=1,
                provider="factset",
                dataset_key="hk_equity_eod",
                trade_date=NOW.date(),
                release_id=release.release_id,
                execution_profile=profile,
            ),
            allowed_datasets=["hk_equity_eod"],
        )
        work = await queue.enqueue(
            test_session,
            provider="factset",
            stable_key=f"daily:{profile}:hk_equity_eod:{NOW.date()}",
            kind="daily",
            dataset_key="hk_equity_eod",
            trade_date=NOW.date(),
            execution_profile=profile,
            scope={},
        )
        work_id = work.work_id
        await test_session.commit()
        config = json.loads(
            (
                Path(__file__).resolve().parents[2] / "fetcher/configs/full_market.local.v1.json"
            ).read_text()
        )
        feed = next(f for f in config["feeds"] if f["provider"] == "factset")
        state = FullMarketState(tmp_path / "daily.sqlite3")
        state.bind_profile(profile)
        frozen = plan.model_dump(mode="json")
        state.record_plan(frozen, "factset")

        def deliver():
            with FullMarketSourceClient(h.config, client=h.http()) as source:
                with claimed_work(
                    source, "factset", accepted_kinds=["daily"], execution_profile=profile
                ) as owner:
                    assert owner is not None
                    instance = runtime.FullMarketRuntime(
                        provider="factset",
                        config=config,
                        state=state,
                        source=source,
                        delivery=SourceAPIClient(h.config, h.registry, client=h.http()),
                        raw_store=h.raw,
                        execution_profile=profile,
                        client=h.http(),
                    )
                    instance._deliver_factset_plan(
                        owner, frozen, source.release(str(release.release_id)), feed
                    )

        await asyncio.to_thread(deliver)
        work = await test_session.get(ProviderWork, work_id, populate_existing=True)
        assert work.state == "partial"
        assert work.manifest["plan_id"] == str(plan.plan_id)
        with state.connection() as db:
            rows = db.execute(
                "SELECT status,receipt,body FROM full_work WHERE plan_id=?", (str(plan.plan_id),)
            ).fetchall()
            assert len(rows) == 2 and all(row["receipt"] and row["body"] for row in rows)
            assert sorted(row["status"] for row in rows) == ["complete", "manual"]
        from app.services.full_market import get_plan

        result = await get_plan(
            test_session, plan.plan_id, provider="factset", allowed_datasets=["hk_equity_eod"]
        )
        assert result.summary.data == 1
    finally:
        h.release_process_lock()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["cancelled", "expired"])
async def test_factset_prepared_drain_preserves_terminal_domain_and_completion_ack(
    client, test_session, test_engine, monkeypatch, tmp_path, decision
):
    from app.services.historical_backfill import cancel_request, expire_requests
    from tests.test_factset_historical import NOW

    h = await factset_harness(client, test_session, test_engine, monkeypatch, tmp_path)
    h.confirm_barrier = True
    original_handle = h.handle
    acknowledgements = []

    def replay_ack(request):
        response = original_handle(request)
        if request.url.path.endswith("/complete") and response.status_code == 200:
            replay = original_handle(request)
            acknowledgements.append(replay.status_code)
            assert replay.status_code == 200, replay.text
        return response

    h.handle = replay_ack
    task = asyncio.create_task(h.run())
    try:
        await asyncio.wait_for(h.prepared.wait(), 15)
        calls = h.native_calls()
        if decision == "cancelled":
            await cancel_request(test_session, h.request_id, actor="test-owner")
        else:
            await expire_requests(test_session, provider="factset", now=NOW + timedelta(days=40))
        await test_session.commit()
        h.allow_confirm.set()
        assert await asyncio.wait_for(task, 25), h.failure
        request = await test_session.get(
            HistoricalBackfillRequest, h.request_id, populate_existing=True
        )
        assert request.status == ("cancelled" if decision == "cancelled" else "failed")
        assert h.native_calls() == calls and len(h.runs) == 1
        assert acknowledgements == [200]
        owner = await test_session.get(
            ProviderWorkOwner, ("local", "factset"), populate_existing=True
        )
        assert owner.attempt_id is None
    finally:
        h.allow_confirm.set()
        if not task.done():
            await asyncio.wait_for(task, 25)
        h.release_process_lock()


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["pilot", "anomaly", "full"])
async def test_captured_native_factset_snapshots_through_fenced_pipeline(
    client, test_session, test_engine, monkeypatch, tmp_path, scope
):
    """Opt-in private captures; Source/DB are local, raw storage is a fixture."""
    capture = os.getenv("FINDB_FACTSET_NATIVE_SNAPSHOT_DIR")
    if not capture:
        pytest.skip("set FINDB_FACTSET_NATIVE_SNAPSHOT_DIR to captured HKEX/FactSet bytes")
    directory = Path(capture)
    members = sorted(
        json.loads((directory / "members.json").read_text()), key=lambda m: m["symbol"]
    )
    snapshots = {
        day: (directory / f"{day}.json").read_bytes() for day in ("2026-10-15", "2026-10-16")
    }

    def invalid(row):
        prices = [
            Decimal(str(row[k]))
            for k in ("price", "price_open", "price_high", "price_low")
            if row[k] is not None
        ]
        return (
            row["price_high"] is not None and Decimal(str(row["price_high"])) < max(prices)
        ) or (row["price_low"] is not None and Decimal(str(row["price_low"])) > min(prices))

    if scope == "pilot":
        members = members[:2]
    elif scope == "anomaly":
        lookup = {r["fsym_id"]: r for r in json.loads(snapshots["2026-10-16"])["rows"]}
        groups = {}
        for member in members:
            row = lookup.get(member["provider_symbol"])
            kind = (
                "mapping_gap"
                if member["mapping_status"] != "mapped"
                else "missing"
                if row is None
                else "invalid"
                if invalid(row)
                else "data"
            )
            groups.setdefault(kind, member)
        assert set(groups) == {"mapping_gap", "missing", "invalid", "data"}
        members = sorted(groups.values(), key=lambda m: m["symbol"])
    h = await factset_harness(
        client,
        test_session,
        test_engine,
        monkeypatch,
        tmp_path,
        members=members,
        snapshots=snapshots,
    )
    h.execution_timeout = 1800
    try:
        for day in snapshots:
            assert await h.run(), h.failure
        request = await test_session.get(
            HistoricalBackfillRequest, h.request_id, populate_existing=True
        )
        assert len(request.universe_snapshot["members"]) == len(members)
        expected_valid = 0
        expected_invalid = 0
        for native in snapshots.values():
            lookup = {r["fsym_id"]: r for r in json.loads(native)["rows"]}
            for member in members:
                row = lookup.get(member["provider_symbol"])
                if member["mapping_status"] == "mapped" and row is not None:
                    expected_invalid += int(invalid(row))
                    expected_valid += int(not invalid(row))
        evidence = list(await test_session.scalars(select(DeliveryMemberEvidence)))
        assert len(evidence) == expected_valid
        runs = list(await test_session.scalars(select(IngestionRun)))
        assert sum(r.failed_records for r in runs) == expected_invalid
        assert sum(r.success_records for r in runs) == expected_valid
        assert all(r.status in {"completed", "completed_with_errors"} for r in runs)
        assert request.status == ("completed" if expected_valid == 2 * len(members) else "partial")
        assert len(h.native_calls()) == 2
        served = 0
        cursor = None
        while True:
            params = {"market": "HK", "page_size": 1000}
            if cursor:
                params["cursor"] = cursor
            response = await client.get("/api/v1/serve/eod", params=params)
            assert response.status_code == 200, response.text
            result = response.json()
            served += len(result["data"])
            cursor = result["pagination"]["next_cursor"]
            if not cursor:
                break
        assert served == expected_valid
        print(
            json.dumps(
                {
                    "scope": scope,
                    "members": len(members),
                    "actual_writes": expected_valid,
                    "dq_rejected": expected_invalid,
                    "request_status": request.status,
                }
            )
        )
    finally:
        h.release_process_lock()
