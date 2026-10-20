"""Real native retries and domain transitions cannot strand the provider FIFO."""

import asyncio
import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from app.models.provider_work import ProviderWork, ProviderWorkAttempt
from app.models.registry import DatasetRegistry, HistoricalBackfillItem, HistoricalBackfillRequest
from app.utils import utc_now
from tests.provider_work_runtime_support import SCOPES
from tests.test_provider_work_adapters import native_runtime as native_runtime


@pytest.fixture
def taifex_dispatch_clock(native_runtime, monkeypatch):
    """Separate domain transitions from the CI runner's 50ms scheduling jitter."""
    if native_runtime.provider != "taifex":
        return None
    from findb_fetcher import acquisition, full_market_client

    clock = [time.monotonic()]
    facade = SimpleNamespace(time=time.time, sleep=time.sleep, monotonic=lambda: clock[0])
    # Keep UTC, pacing, asyncio and the harness timeout real. Only the grant's
    # receipt/dispatch monotonic clock is controlled, without changing its bound.
    monkeypatch.setattr(acquisition, "time", facade)
    monkeypatch.setattr(full_market_client, "time", facade)
    return clock


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "native_runtime", [(provider, dataset, 5) for provider, dataset in SCOPES], indirect=True
)
@pytest.mark.parametrize("retained_earlier", [False, True])
async def test_failed_request_cancels_empty_siblings_without_historical_consumer_but_retains_prepared_drain(
    native_runtime, test_engine, retained_earlier, taifex_dispatch_clock
):
    h = native_runtime
    handle = h.handle
    rejecting_date = False

    def fail_date(request):
        if request.url.path == "/api/v1/source/ingest":
            if retained_earlier and not rejecting_date:
                return httpx.Response(503, request=request)

            async def deactivate():
                dataset = await h.db.get(DatasetRegistry, h.dataset)
                dataset.is_active = False
                await h.db.commit()

            asyncio.run_coroutine_threadsafe(deactivate(), h.loop).result(timeout=15)
        return handle(request)

    h.handle = fail_date
    assert await h.run()
    if retained_earlier:
        rejecting_date = True
        assert await h.run(), "next original date runs before the delayed retry"
    h.db.expire_all()
    assert (await h.db.get(HistoricalBackfillRequest, h.request_id)).status == "failed", h.failure
    works = list(await h.db.scalars(select(ProviderWork).where(ProviderWork.kind == "historical")))
    retained = [work for work in works if work.state == "queued"]
    assert len(retained) == int(retained_earlier)
    for work in works:
        if work.state == "queued":
            assert work.prepared_deliveries
            attempt = await h.db.scalar(
                select(ProviderWorkAttempt).where(
                    ProviderWorkAttempt.work_id == work.work_id,
                    ProviderWorkAttempt.state == "queued",
                )
            )
            assert attempt.drain_only
            assert (
                await h.db.get(HistoricalBackfillItem, UUID(work.scope["item_id"]))
            ).status == "cancelled"
        elif work.state != "failed":
            assert work.state == "cancelled"
            assert not work.prepared_deliveries
    dataset = await h.db.get(DatasetRegistry, h.dataset)
    dataset.is_active = True
    await h.db.commit()
    following = await h.client.post(
        "/api/v1/source/provider-work/enqueue",
        headers=h.headers,
        json={
            "kind": "maintenance",
            "stable_key": f"maintenance:{h.dataset}:after-domain-failure",
            "dataset_key": h.dataset,
        },
    )
    assert following.status_code == 200, following.text
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
        assert claimed and claimed["kind"] == "maintenance", (
            "no historical consumer should be needed to clear empty cancelled dates"
        )
        fence = {"generation": claimed["generation"], "token": claimed["token"]}
        path = f"/api/v1/source/provider-work/{claimed['work_id']}"
        assert (
            await h.client.post(path + "/close", headers=h.headers, json=fence)
        ).status_code == 200
        assert (
            await h.client.post(
                path + "/settle",
                headers=h.headers,
                json={**fence, "outcome": "cancelled", "proof": {}},
            )
        ).status_code == 200
    if retained:
        work_id = retained[0].work_id
        original = json.loads(json.dumps(retained[0].prepared_deliveries))
        attempt.not_before = utc_now() - timedelta(seconds=1)
        await h.db.commit()
        calls = h.native_calls()
        h.handle = handle
        assert await h.run()
        h.db.expire_all()
        work = await h.db.get(ProviderWork, work_id)
        assert work.state == "cancelled" and work.prepared_deliveries == original
        assert h.native_calls() == calls
        assert (await h.db.get(HistoricalBackfillRequest, h.request_id)).status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "native_runtime",
    SCOPES,
    indirect=True,
)
async def test_actual_account_quota_waits_preserve_dispatched_budget_and_resume_after_window_change(
    native_runtime, test_engine, monkeypatch, taifex_dispatch_clock
):
    from findb_fetcher import acquisition

    h = native_runtime
    native = h.native
    from findb_fetcher.shioaji_scheduler import ProductionCoordinator

    run_coordinator = ProductionCoordinator.run
    results = []

    def observe(coordinator, *args, **kwargs):
        result = run_coordinator(coordinator, *args, **kwargs)
        results.append([(row.code, row.symbol, row.retryable) for row in result])
        return result

    monkeypatch.setattr(ProductionCoordinator, "run", observe)
    kind = {"taifex": "report", "shioaji": "kbars", "finlab": "dataset", "twelve_data": "daily"}[
        h.provider
    ]

    def fail_first_dispatch(operation):
        native(operation)
        if (
            operation[0] == kind
            and sum(json.loads(row)[0] == kind for row in h.native_calls()) == 1
        ):
            raise httpx.ReadError("offline dispatched provider fault")

    h.native = fail_first_dispatch
    clock = [time.time() - 2000]
    make_runner = h.runner

    def runner():
        target = make_runner()
        if h.provider == "taifex":
            target.clock = lambda: clock[0]
            target.sleep = lambda delay: clock.__setitem__(0, clock[0] + delay)
            target._cooldown = lambda: (
                0
            )  # account Acquisition still enforces the real durable floor
        return target

    h.runner = runner
    limits = acquisition.limits
    monkeypatch.setattr(
        acquisition, "limits", lambda provider: {**limits(provider), "requests_per_day": 100}
    )
    assert await h.run()
    governor = os.environ["FETCHER_ACCOUNT_STATE_PATH"]
    window = acquisition._windows(time.time())[0]
    # sqlite Connection.__exit__ commits but does not close. The offline
    # native launcher forks; no live governor VFS state may cross that boundary.
    # Production launches an exec child instead. Close before every awaited run.
    with closing(sqlite3.connect(governor)) as db, db:
        account = db.execute(
            "SELECT account FROM acquisition_intent WHERE provider=? AND request_units>0 AND actual_start IS NOT NULL ORDER BY armed_at DESC LIMIT 1",
            (h.provider,),
        ).fetchone()[0]
        db.execute(
            "UPDATE full_quota SET requests=100 WHERE account=? AND window=?", (account, window)
        )
    work = await h.db.scalar(
        select(ProviderWork).where(ProviderWork.stable_key == f"historical:{h.item_id}")
    )
    work_id, frozen_scope, frozen_manifest = work.work_id, dict(work.scope), work.manifest
    native_before = sum(json.loads(row)[0] == kind for row in h.native_calls())
    for _ in range(4):
        h.db.expire_all()
        attempt = await h.db.scalar(
            select(ProviderWorkAttempt).where(
                ProviderWorkAttempt.work_id == work_id, ProviderWorkAttempt.state == "queued"
            )
        )
        assert attempt
        attempt.not_before = utc_now() - timedelta(seconds=1)
        await h.db.commit()
        clock[0] += 200
        assert await h.run()
        h.db.expire_all()
        current = await h.db.get(ProviderWork, work_id)
        assert (
            current.state == "queued"
            and current.scope == frozen_scope
            and current.manifest == frozen_manifest
        ), h.failure
        assert (await h.db.get(HistoricalBackfillRequest, h.request_id)).status == "running"
        assert sum(json.loads(row)[0] == kind for row in h.native_calls()) == native_before
    if h.provider == "taifex":
        with closing(sqlite3.connect(h.path / "taifex.sqlite3")) as db, db:
            state = json.loads(db.execute("SELECT body FROM work").fetchone()[0])
        assert state["operations"]["fetch:TX"]["attempts"] == 1
        assert [row["outcome"] for row in state["operation_history"]] == [
            "retryable_failure",
            *(["quota_wait"] * 4),
        ]
    elif h.provider == "shioaji":
        checkpoint = h.path / "historical" / f"{h.request_id}.sqlite3"
        with closing(sqlite3.connect(checkpoint)) as db, db:
            audit = db.execute(
                "SELECT outcome,code FROM provider_attempts ORDER BY attempt_no"
            ).fetchall()
        assert audit == [("failed", "ACQUISITION_FAILED"), *([("waiting", "QUOTA_WAIT")] * 4)]
    else:
        with closing(sqlite3.connect(Path(governor).parent / "provider-work.sqlite3")) as db, db:
            assert (
                db.execute(
                    "SELECT sum(attempts) FROM work_budget WHERE work_key=?", (str(work_id),)
                ).fetchone()[0]
                == 1
            )
    windows = acquisition._windows
    from findb_fetcher.providers import shioaji_control

    def next_window(timestamp):
        return windows(timestamp + 86400)

    monkeypatch.setattr(acquisition, "_windows", next_window)
    monkeypatch.setattr(shioaji_control, "_windows", next_window)
    h.db.expire_all()
    attempt = await h.db.scalar(
        select(ProviderWorkAttempt).where(
            ProviderWorkAttempt.work_id == work_id, ProviderWorkAttempt.state == "queued"
        )
    )
    attempt.not_before = utc_now() - timedelta(seconds=1)
    await h.db.commit()
    clock[0] += 200
    assert await h.run()
    h.db.expire_all()
    assert (await h.db.get(ProviderWork, work_id)).state == "completed", (h.failure, results)
    request = await h.db.get(HistoricalBackfillRequest, h.request_id)
    assert request.status == "completed" and request.failure_code is None
    with closing(sqlite3.connect(governor)) as db, db:
        assert (
            db.execute(
                "SELECT requests FROM full_quota WHERE account=? AND window=?", (account, window)
            ).fetchone()[0]
            == 100
        ), "verified prior account usage must not be erased by retry"


@pytest.mark.asyncio
@pytest.mark.parametrize("native_runtime", [("taifex", "tw_futures_eod")], indirect=True)
async def test_taifex_expired_dispatch_grant_defers_without_native_io_then_retries_same_work(
    native_runtime, test_engine, taifex_dispatch_clock
):
    h = native_runtime
    handle = h.handle
    granted = False
    expired = False

    def expire_final_work_check(request):
        nonlocal granted, expired
        response = handle(request)
        if request.url.path.endswith("/authorization"):
            assert response.status_code == 200
            granted = True
        elif granted and request.url.path.endswith("/authorize"):
            assert response.status_code == 200
            taifex_dispatch_clock[0] += 0.051
            expired = True
        return response

    h.handle = expire_final_work_check
    assert await h.run()
    assert granted and expired
    assert not h.native_calls()
    h.db.expire_all()
    work = await h.db.scalar(
        select(ProviderWork).where(ProviderWork.stable_key == f"historical:{h.item_id}")
    )
    assert work.state == "queued", h.failure
    work_id = work.work_id
    with closing(sqlite3.connect(os.environ["FETCHER_ACCOUNT_STATE_PATH"])) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM acquisition_intent WHERE actual_start IS NOT NULL"
            ).fetchone()[0]
            == 0
        )
    attempt = await h.db.scalar(
        select(ProviderWorkAttempt).where(
            ProviderWorkAttempt.work_id == work_id, ProviderWorkAttempt.state == "queued"
        )
    )
    attempt.not_before = utc_now() - timedelta(seconds=1)
    await h.db.commit()
    h.handle = handle
    # Advance the runner's durable retry clock, retaining real account pacing.
    make_runner = h.runner

    def runner():
        target = make_runner()
        target.clock = lambda: time.time() + 31
        return target

    h.runner = runner
    assert await h.run()
    h.db.expire_all()
    assert (await h.db.get(ProviderWork, work_id)).state == "completed", h.failure
    assert (await h.db.get(HistoricalBackfillRequest, h.request_id)).status == "completed"
    assert h.native_calls()
