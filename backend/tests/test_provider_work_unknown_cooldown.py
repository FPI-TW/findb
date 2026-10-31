"""A crashed account intent becomes durable PG eligibility, without waiting as owner."""

import time
from datetime import timedelta
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from app.models.provider_work import ProviderWork, ProviderWorkAttempt, ProviderWorkOwner
from app.models.registry import HistoricalBackfillItem
from app.services.provider_work import prepared_map
from app.utils import utc_now
from tests.provider_work_clock import account_clock, drain_retained_rerun_during_cooldown
from tests.test_provider_work_adapters import native_runtime as native_runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("native_runtime", [("twelve_data", "us_equity_eod", 5)], indirect=True)
@pytest.mark.parametrize("origin", ["armed", "unknown_dispatch", "legacy_unknown"])
@pytest.mark.parametrize("lost_publish_ack", [False, True])
async def test_unknown_account_floor_recovery_drains_prepared_and_resumes_original_scope(
    native_runtime, test_engine, monkeypatch, origin, lost_publish_ack
):
    from findb_fetcher import acquisition, historical_runtime
    from findb_fetcher.full_market_state import FullMarketState
    from findb_fetcher.historical_runtime import load_symbol_universe
    from findb_fetcher.platform_limits import account_identity

    h = native_runtime
    offset = account_clock(monkeypatch)
    handle = h.handle
    h.handle = lambda request: (
        httpx.Response(503, request=request)
        if request.url.path == "/api/v1/source/ingest"
        else handle(request)
    )
    assert await h.run()  # A has immutable prepared bytes, not yet accepted.
    h.handle = handle
    h.db.expire_all()
    retained = await h.db.scalar(
        select(ProviderWork).where(ProviderWork.stable_key == f"historical:{h.item_id}")
    )
    retained_id, prepared = retained.work_id, dict((await prepared_map(h.db, retained.work_id)))
    calls = h.native_calls()
    assert prepared and not h.runs
    state = FullMarketState(h.path / "account/governor.sqlite3")
    account = account_identity(h.provider, state, credential="offline-twelve")
    schedule = historical_runtime._schedule(h.provider, h.dataset)
    universe = load_symbol_universe(schedule.universe_file)
    manifest = {
        "mode": "canonical",
        "members": [
            {"key": member.canonical_symbol, "symbol": member.symbol, "exchange": member.exchange}
            for member in universe.symbols
        ],
        "universe_id": universe.universe_id,
        "universe_version": universe.universe_version,
    }
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
        original = response.json()["work"]
        work_id = UUID(original["work_id"])
        fence = {"generation": original["generation"], "token": original["token"]}
        headers = {
            **h.headers,
            "X-FinDB-Work-Id": str(work_id),
            "X-FinDB-Work-Generation": str(fence["generation"]),
            "X-FinDB-Work-Token": fence["token"],
        }
        item_response = await h.client.post(
            "/api/v1/source/historical-backfills/claim", headers=headers
        )
        assert item_response.status_code == 200, item_response.text
        item = await h.db.get(HistoricalBackfillItem, UUID(item_response.json()["item_id"]))
        job_key = f"historical:{h.request_id}:{item.trade_date}:{universe.symbols[0].symbol}"
        journal = h.queue.WorkJournal(h.path / "account/provider-work.sqlite3", str(work_id))
        journal.freeze(manifest)
        journal.reserve_attempt(job_key, schedule.max_attempts)
        response = await h.client.post(
            f"/api/v1/source/provider-work/{work_id}/manifest",
            headers=h.headers,
            json={**fence, "manifest": manifest},
        )
        assert response.status_code == 200, response.text
        # Snapshot at a real protocol crash point: owner/manifest/budget and
        # account armed intent committed, consumer and all I/O descendants gone.
        now = time.time()
        if origin == "unknown_dispatch":
            with acquisition.Acquisition(
                h.provider, state_path=state.path, account=account
            ) as operation:
                operation.prepare()
                operation.possibly_started = True
            unknown_id = operation.operation_id
        else:
            unknown_id = "crashed-native"
            with state.connection() as db:
                db.execute(
                    "INSERT INTO acquisition_intent(operation_id,account,provider,armed_at,status,cooldown_bound,completed_at,request_units,login_units,operation_kind) VALUES(?,?,?,?,?,300,?,1,0,'acquisition')",
                    (
                        unknown_id,
                        account,
                        h.provider,
                        now - 1,
                        "armed" if origin == "armed" else "unknown",
                        None if origin == "armed" else now,
                    ),
                )
                if origin == "legacy_unknown":
                    db.execute(
                        "INSERT INTO full_pacing VALUES(?,?) ON CONFLICT(account) DO UPDATE SET next_at=excluded.next_at",
                        (account, now + 300),
                    )
                    db.execute("DELETE FROM full_account_cooldown WHERE account=?", (account,))
        with state.connection() as db:
            charged_before = db.execute(
                "SELECT COALESCE(SUM(requests),0) FROM full_quota WHERE account=?", (account,)
            ).fetchone()[0]
        if lost_publish_ack:
            # The original process recovered the account before publishing.
            with acquisition.Acquisition(h.provider, state_path=state.path, account=account):
                pass
            # Source committed the floor, then the original consumer died
            # without observing its ACK or saving a per-work cooldown.
            response = await h.client.post(
                f"/api/v1/source/provider-work/{work_id}/heartbeat",
                headers=h.headers,
                json={
                    **fence,
                    "acquisition_not_before": (utc_now() + timedelta(seconds=300)).isoformat(),
                },
            )
            assert response.status_code == 200
        assert journal.cooldown_until() == 0
    assert await h.run()  # New coordinator recovers same work/fence and tail-retries.
    h.db.expire_all()
    owner = await h.db.get(ProviderWorkOwner, (h.environment, h.provider))
    assert owner.attempt_id is None and owner.acquisition_not_before > utc_now() + timedelta(
        seconds=290
    )
    work = await h.db.get(ProviderWork, work_id)
    assert work.state == "queued" and work.manifest == manifest and h.native_calls() == calls
    assert not await h.run(), (
        "other unfetched dates cannot hold owner and sleep on the account floor"
    )
    with journal.connection() as db:
        assert (
            db.execute(
                "SELECT attempts FROM work_budget WHERE work_key=? AND operation_key=?",
                (str(work_id), job_key),
            ).fetchone()[0]
            == 1
        )
    with state.connection() as db:
        unknown = db.execute(
            "SELECT status,actual_start FROM acquisition_intent WHERE operation_id=?", (unknown_id,)
        ).fetchone()
        assert tuple(unknown) == ("unknown", None)
        assert (
            db.execute(
                "SELECT COALESCE(SUM(requests),0) FROM full_quota WHERE account=?", (account,)
            ).fetchone()[0]
            == charged_before
        )
    # Another provider owns its independent slot while TD drains originals.
    from app.models.registry import DatasetRegistry
    from app.services.source_clients import create_source_client

    h.db.add(
        DatasetRegistry(dataset_key="tw_equity_eod", name="TW", market="TW", asset_class="equity")
    )
    await h.db.flush()
    _, secret = await create_source_client(
        h.db,
        name="unknown-parallel",
        source_name="finlab",
        allowed_datasets=["tw_equity_eod"],
        rate_limit_requests=1000,
        rate_limit_window=60,
        commit=False,
    )
    await h.db.commit()
    parallel_headers = {"X-API-Key": secret}
    response = await h.client.post(
        "/api/v1/source/provider-work/enqueue",
        headers=parallel_headers,
        json={
            "kind": "maintenance",
            "dataset_key": "tw_equity_eod",
            "stable_key": "maintenance:tw_equity_eod:parallel",
        },
    )
    assert response.status_code == 200
    with h.queue.host_lock("finlab") as (_, inode):
        response = await h.client.post(
            "/api/v1/source/provider-work/claim",
            headers=parallel_headers,
            json={"protocol": "provider-work-v1", "host_lock_identity": inode},
        )
        assert response.status_code == 200 and response.json()["work"]
        parallel = response.json()["work"]
        parallel_base = f"/api/v1/source/provider-work/{parallel['work_id']}"
        parallel_fence = {"generation": parallel["generation"], "token": parallel["token"]}
        offset[0] += 61
        retry = await h.db.scalar(
            select(ProviderWorkAttempt).where(
                ProviderWorkAttempt.work_id == retained_id, ProviderWorkAttempt.state == "queued"
            )
        )
        retry.not_before = utc_now() - timedelta(seconds=1)
        await h.db.commit()
        assert await h.run()
        h.db.expire_all()
        assert (await prepared_map(h.db, retained_id)) == prepared
        assert h.runs and h.native_calls() == calls
        await drain_retained_rerun_during_cooldown(h)
        assert (
            await h.client.post(
                parallel_base + "/close", headers=parallel_headers, json=parallel_fence
            )
        ).status_code == 200
        assert (
            await h.client.post(
                parallel_base + "/settle",
                headers=parallel_headers,
                json={
                    **parallel_fence,
                    "outcome": "failed",
                    "proof": {},
                    "failure_code": "TEST_FINISHED",
                },
            )
        ).status_code == 200
    offset[0] += 301
    for _ in range(8):
        h.db.expire_all()
        work = await h.db.get(ProviderWork, work_id)
        if work.state == "completed":
            break
        assert await h.run()
    assert work.state == "completed" and work.manifest == manifest
    with journal.connection() as db:
        assert (
            db.execute(
                "SELECT attempts FROM work_budget WHERE work_key=? AND operation_key=?",
                (str(work_id), job_key),
            ).fetchone()[0]
            == 1
        )
    with state.connection() as db:
        assert (
            db.execute(
                "SELECT status FROM acquisition_intent WHERE operation_id=?", (unknown_id,)
            ).fetchone()[0]
            == "unknown"
        )
