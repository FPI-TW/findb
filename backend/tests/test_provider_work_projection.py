"""Queue monitoring stays small even with retained bodies and many retry rounds."""

import json
import time
from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import event, select

from app.models.provider_work import (
    ProviderWork,
    ProviderWorkAttempt,
    ProviderWorkOwner,
    ProviderWorkReceipt,
)
from app.models.registry import (
    DatasetRegistry,
    HistoricalBackfillItem,
    HistoricalBackfillRequest,
    IngestionRun,
    SchedulerControl,
)
from app.services import provider_work as queue
from app.services.historical_backfill import expiry_cutoff
from app.services.provider_work import prepared_map
from app.utils import utc_now, uuid7

PROVIDERS = {
    "twelve_data": "us_equity_eod",
    "finlab": "tw_equity_eod",
    "shioaji": "tw_equity_minute",
    "taifex": "tw_futures_eod",
    "factset": "hk_equity_eod",
}


async def seed_work(db, provider="shioaji", *, kind="daily", prepared=None, profile="full_market"):
    dataset = PROVIDERS[provider]
    if await db.get(DatasetRegistry, dataset) is None:
        db.add(
            DatasetRegistry(dataset_key=dataset, name=dataset, market="TW", asset_class="equity")
        )
        await db.flush()
    work = ProviderWork(
        environment=queue.environment(),
        provider=provider,
        stable_key=f"{kind}:{uuid7()}",
        kind=kind,
        dataset_key=dataset,
        trade_date=utc_now().date(),
        execution_profile=profile,
    )
    db.add(work)
    await db.flush()
    for key, row in (prepared or {}).items():
        await queue.store_prepared(db, work.work_id, key, row)
    attempt = ProviderWorkAttempt(work_id=work.work_id, not_before=utc_now() - timedelta(seconds=1))
    db.add(attempt)
    await db.flush()
    return work, attempt


@contextmanager
def monitor_reads(db):
    statements = []

    def capture(conn, cursor, statement, params, context, many):
        assert statement.lstrip().upper().startswith("SELECT"), statement
        selected = context.compiled.statement.selected_columns
        # Reject transporting an ORM entity/full JSON object, even under aliases.
        assert not {"prepared_deliveries", "manifest", "scope", "proof", "token"} & {
            column.key for column in selected
        }
        statements.append(statement)

    engine = db.bind.sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)


@pytest.mark.asyncio
async def test_retained_rounds_and_bodies_do_not_expand_queue_reads(test_session):
    db = test_session
    batch = {"reason": "batch_yield", "processed": 20, "counts": {"prepared": 1}}
    work, attempt = await seed_work(db, prepared={"saved": {"body": "private-large-body" * 4096}})
    work.manifest = {"members": [{"key": "frozen"}], "retained": "not-a-monitoring-field"}
    for number in range(1, 65):
        db.add(
            ProviderWorkAttempt(
                work_id=work.work_id,
                attempt_number=number + 1,
                state="settled",
                proof={
                    "scheduling": batch if number == 64 else {"processed": number},
                    "private": "history" * 1024,
                },
                failure_code="original_failure" if number == 2 else None,
            )
        )
    attempt.attempt_number = 66
    # A current round's failure participates; newest settled batch stays independent.
    attempt.proof = {"unrelated": "current-proof"}
    await db.flush()
    with monitor_reads(db) as statements:
        rows = await queue.projection(db)
    assert len(rows) == 1 and rows[0]["attempt_number"] == 66
    assert rows[0]["last_batch"] == batch
    assert rows[0]["last_failure_code"] == "original_failure"
    assert "private-large-body" not in json.dumps(rows, default=str)
    first_count = len(statements)
    for _ in range(25):
        await seed_work(db)
    with monitor_reads(db) as statements:
        rows = await queue.projection(db)
    assert len(rows) == 26 and len(statements) == first_count == 3
    assert len(rows[0]["last_batch"]["counts"]) == 1
    assert (await prepared_map(db, work.work_id))["saved"]["body"].startswith("private-large-body")
    assert (
        len(
            (
                await db.scalars(
                    select(ProviderWorkAttempt).where(ProviderWorkAttempt.work_id == work.work_id)
                )
            ).all()
        )
        == 65
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    "receipt_state",
    [None, "completed", "completed_with_errors", "failed", "cancelled", "processing"],
)
async def test_original_prepared_and_run_drain_states(test_session, provider, receipt_state):
    db = test_session
    work, attempt = await seed_work(db, provider, prepared={"original": {"body": "retained"}})
    db.add(
        SchedulerControl(
            scheduler_key=f"full_market_{provider}_v1",
            provider=provider,
            execution_profile="full_market",
            desired_state="stopped",
            observed_state="stopped",
            slot_id="taiwan_market_window",
            timezone="Asia/Taipei",
        )
    )
    if receipt_state:
        run = IngestionRun(dataset_key=work.dataset_key, source=provider, status=receipt_state)
        db.add(run)
        await db.flush()
        db.add(
            ProviderWorkReceipt(
                run_id=run.run_id,
                work_id=work.work_id,
                attempt_id=attempt.attempt_id,
                member_key="original",
                payload_sha256="a" * 64,
            )
        )
    await db.flush()
    original = ((await prepared_map(db, work.work_id)), work.manifest, attempt.state)
    expected_open = await queue.acquisition_open(db, work)
    with monitor_reads(db):
        rows = await queue.projection(db, provider=provider)
    assert len(rows) == 1 and rows[0]["drain_only"] == (not expected_open)
    assert rows[0]["wait_reason"] == (
        "prepared_drain" if receipt_state is None or receipt_state == "processing" else "suspended"
    )
    assert ((await prepared_map(db, work.work_id)), work.manifest, attempt.state) == original


@pytest.mark.asyncio
async def test_profile_fifo_active_recovery_and_environment_filter(test_session):
    db = test_session
    full, a = await seed_work(db, "finlab")
    pilot, b = await seed_work(db, "finlab", profile="pilot")
    hidden, _ = await seed_work(db, "shioaji")
    hidden.environment = "staging"
    finished, done = await seed_work(db, "finlab")
    done.state = "settled"
    finished.state = "failed"
    for profile in ["pilot", "full_market"]:
        db.add(
            SchedulerControl(
                scheduler_key=f"{profile}_finlab_v1",
                provider="finlab",
                execution_profile=profile,
                desired_state="running",
                observed_state="running",
                slot_id="taiwan_market_window",
                timezone="Asia/Taipei",
            )
        )
    db.add(
        ProviderWorkOwner(
            environment=queue.environment(),
            provider="finlab",
            attempt_id=a.attempt_id,
            expires_at=utc_now() - timedelta(seconds=1),
            heartbeat_at=utc_now() - timedelta(minutes=5),
        )
    )
    a.state = "recovery_blocked"
    await db.flush()
    rows = await queue.projection(db, provider="finlab")
    assert [r["work_id"] for r in rows] == [str(full.work_id), str(pilot.work_id)]
    assert rows[0]["active"] and rows[0]["wait_reason"] == "recovery_required"
    assert rows[1]["wait_reason"] == "provider_active" and rows[1]["position"] == 1
    assert rows[0]["execution_profile"] == "full_market" and rows[1]["execution_profile"] == "pilot"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["queued", "running", "cancelled", "failed"])
@pytest.mark.parametrize("reference", ["item_id", "legacy_id", "invalid", "null_item_id"])
@pytest.mark.parametrize("expired", [False, True])
async def test_historical_drain_classification_matches_acquisition_gate(
    test_session, status, reference, expired
):
    db = test_session
    works = []
    for provider in PROVIDERS:
        work, attempt = await seed_work(
            db, provider, kind="historical", prepared={"kept": {"body": "original"}}
        )
        day = expiry_cutoff(provider, utc_now()) - timedelta(days=int(expired))
        request = HistoricalBackfillRequest(
            request_key=str(uuid7()),
            provider=provider,
            dataset_key=work.dataset_key,
            market="TW",
            start_date=day,
            end_date=day,
            status=status,
            created_by="test-owner",
        )
        db.add(request)
        await db.flush()
        item = HistoricalBackfillItem(
            request_id=request.request_id, trade_date=day, status="pending"
        )
        db.add(item)
        await db.flush()
        work.scope = (
            {reference: str(item.item_id)}
            if reference in ("item_id", "legacy_id")
            else {"item_id": "invalid"}
            if reference == "invalid"
            else {"item_id": None, "legacy_id": str(item.item_id)}
        )
        works.append((work, await queue.acquisition_open(db, work)))
    await db.flush()
    with monitor_reads(db) as statements:
        rows = await queue.projection(db)
    assert len(statements) <= 5
    indexed = {row["work_id"]: row for row in rows}
    for work, allowed in works:
        assert indexed[str(work.work_id)]["drain_only"] == (not allowed)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["daily", "maintenance", "rerun"])
@pytest.mark.parametrize("before_boundary", [False, True])
async def test_hk_cutover_drain_classification_uses_original_eligibility(
    test_session, kind, before_boundary
):
    db = test_session
    works = []
    for provider in ["factset", "twelve_data"]:
        work, _ = await seed_work(db, provider, kind=kind, prepared={"saved": {"body": "kept"}})
        work.dataset_key = "hk_equity_eod"
        work.trade_date = utc_now().date() - timedelta(days=int(before_boundary))
        db.add(
            SchedulerControl(
                scheduler_key=f"full_market_{provider}_v1",
                provider=provider,
                execution_profile="full_market",
                desired_state="running",
                observed_state="running",
                slot_id="asia_pacific_markets_window",
                timezone="Asia/Hong_Kong",
            )
        )
        works.append(work)
    dataset = await db.get(DatasetRegistry, "hk_equity_eod")
    dataset.config = {
        "hk_provider_cutover": {
            "provider": "factset",
            "previous_provider": "twelve_data",
            "effective_date": utc_now().date().isoformat(),
        }
    }
    await db.flush()
    expected = {work.work_id: not await queue.acquisition_open(db, work) for work in works}
    with monitor_reads(db):
        rows = await queue.projection(db)
    assert {row["work_id"]: row["drain_only"] for row in rows} == {
        str(key): value for key, value in expected.items()
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["full_market", "pilot"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_prepared_projection_matches_flag_and_profile_acquisition_gate(
    test_session, monkeypatch, profile, enabled
):
    from app.config import get_settings

    db = test_session
    monkeypatch.setattr(get_settings(), "FULL_MARKET_ENABLED", enabled)
    monkeypatch.setattr(queue, "daily_window", lambda *args: {"open": True})
    work, _ = await seed_work(db, profile=profile, prepared={"original": {"body": "immutable"}})
    db.add(
        SchedulerControl(
            scheduler_key=f"{profile}_shioaji_v1",
            provider="shioaji",
            execution_profile=profile,
            desired_state="running",
            slot_id="taiwan_market_window",
        )
    )
    await db.flush()
    expected = not await queue.acquisition_open(db, work)
    with monitor_reads(db):
        [row] = await queue.projection(db)
    assert row["drain_only"] is expected is (not enabled and profile == "full_market")


@pytest.mark.asyncio
async def test_minute_sized_preparation_with_many_rounds_stays_a_metadata_read(test_session):
    db = test_session
    body = "retained-minute-bar:" * 1000
    prepared = {f"sequence-{n}": {"body": body} for n in range(350)}
    retained = []
    for _ in range(6):
        work, attempt = await seed_work(db, prepared=prepared)
        retained.append(work.work_id)
        for number in range(1, 29):
            db.add(
                ProviderWorkAttempt(
                    work_id=work.work_id,
                    attempt_number=number + 1,
                    state="settled",
                    proof={"scheduling": {"processed": 20}},
                )
            )
        attempt.attempt_number = 30
    await db.flush()
    started = time.monotonic()
    with monitor_reads(db) as statements:
        rows = await queue.projection(db)
    elapsed = time.monotonic() - started
    assert len(rows) == 6 and len(statements) == 3
    assert all(row["drain_only"] and row["wait_reason"] == "prepared_drain" for row in rows)
    assert len(json.dumps(rows, default=str)) < 10_000
    assert (
        len(
            (
                await db.scalars(
                    select(ProviderWorkAttempt).where(ProviderWorkAttempt.work_id.in_(retained))
                )
            ).all()
        )
        == 174
    )
    print(
        f"minute projection: 6 works, 28 retained rounds each, {len(json.dumps(prepared)) * 6} prepared bytes, {len(statements)} SQL calls, {elapsed:.3f}s"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["window", "cooldown", "stopped", "not_before", "yield"])
@pytest.mark.parametrize("pending_error", [False, True])
async def test_wait_and_report_continuation_precedence(
    test_session, monkeypatch, gate, pending_error
):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "FULL_MARKET_ENABLED", True)
    db = test_session
    work, attempt = await seed_work(db, "finlab")
    db.add(
        SchedulerControl(
            scheduler_key="full_market_finlab_v1",
            provider="finlab",
            execution_profile="full_market",
            desired_state="stopped" if gate == "stopped" else "running",
            observed_state="stopped",
            slot_id="taiwan_market_window",
            timezone="Asia/Taipei",
        )
    )
    if pending_error:
        attempt.proof = {"source_error_pending": True, "pending_source_errors": ["member"]}
    if gate == "window":
        monkeypatch.setattr(
            queue,
            "daily_window",
            lambda provider, now=None: {
                "open": False,
                "next_open_at": (utc_now() + timedelta(hours=1)).isoformat(),
                "closes_at": None,
            },
        )
    if gate == "cooldown":
        db.add(
            ProviderWorkOwner(
                environment=queue.environment(),
                provider="finlab",
                acquisition_not_before=utc_now() + timedelta(minutes=2),
            )
        )
    if gate == "not_before":
        attempt.not_before = utc_now() + timedelta(minutes=2)
    if gate == "yield":
        attempt.attempt_number = 2
        db.add(
            ProviderWorkAttempt(
                work_id=work.work_id,
                attempt_number=1,
                state="settled",
                proof={"scheduling": {"reason": "batch_yield"}},
            )
        )
    await db.flush()
    row = (await queue.projection(db))[0]
    expected = {
        "window": "acquisition_window_closed",
        "cooldown": "account_cooldown",
        "stopped": "suspended",
        "not_before": "not_before",
        "yield": "batch_yield",
    }[gate]
    if pending_error and gate in {"window", "cooldown", "stopped"}:
        expected = "prepared_drain"
    assert row["wait_reason"] == expected
    assert row["drain_only"] == (pending_error and gate in {"window", "cooldown", "stopped"})


@pytest.mark.asyncio
async def test_current_failure_latest_batch_and_failed_historical_continuation_are_retained(
    test_session,
):
    db = test_session
    work, attempt = await seed_work(db, kind="historical")
    work.state = "failed"
    attempt.attempt_number = 3
    attempt.proof = {
        "source_error_pending": True,
        "scheduling": {"failures": [{"reason": "current_error"}]},
    }
    db.add_all(
        [
            ProviderWorkAttempt(
                work_id=work.work_id, attempt_number=1, state="settled", failure_code="old_error"
            ),
            ProviderWorkAttempt(
                work_id=work.work_id,
                attempt_number=2,
                state="settled",
                proof={"scheduling": {"reason": "batch_yield"}},
            ),
        ]
    )
    await db.flush()
    row = (await queue.projection(db))[0]
    assert row["work_id"] == str(work.work_id)
    assert row["last_failure_code"] == "current_error"
    assert row["last_batch"] == {"reason": "batch_yield"}
    assert row["drain_only"] and row["wait_reason"] == "batch_yield"


@pytest.mark.asyncio
async def test_admin_queue_remains_viewer_authenticated_and_hides_bodies(client, test_session):
    from app.services.api_keys import create_api_key

    work, _ = await seed_work(
        test_session, prepared={"original": {"body": "private-retained-body"}}
    )
    _, key = await create_api_key(
        test_session,
        owner="queue-viewer",
        tier="standard",
        scopes=["admin"],
        rate_limit_requests=100,
        rate_limit_window=60,
        page_size_limit=100,
        kind="admin",
        name="queue-viewer",
        role="viewer",
        commit=False,
    )
    assert (await client.get("/api/v1/admin/provider-work")).status_code == 401
    assert (
        await client.get("/api/v1/admin/provider-work", headers={"X-API-Key": "invalid"})
    ).status_code == 403
    response = await client.get("/api/v1/admin/provider-work", headers={"X-API-Key": key})
    assert response.status_code == 200
    assert response.json()["data"][0]["work_id"] == str(work.work_id)
    assert "private-retained-body" not in response.text
