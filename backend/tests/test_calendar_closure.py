"""Calendar retirement is distinct from canonical success and preserves frozen evidence."""

import json
from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.calendar_closure import ProviderWorkCalendarClosure
from app.models.canonical import CalendarRevisionDay
from app.models.provider_work import ProviderWork, ProviderWorkAttempt
from app.models.registry import (
    AdminAuditEvent,
    DailyExecutionDecision,
    MissingDeliveryAlert,
    SchedulerControl,
)
from app.models.shioaji_repair import ShioajiMemberRepair, ShioajiRepairAck
from app.schemas.calendar_closure import CalendarClosureApplyRequest, CalendarClosureRequest
from app.services import provider_work as queue
from app.services.calendar_closure import apply, evaluate, inventory, preview
from app.services.effective_calendar import execution_snapshot
from app.services.full_market import FullMarketError, _digest
from app.services.provider_work import prepared_map
from app.services.shioaji_repair import preview as repair_preview
from app.utils import utc_now, uuid7
from tests.test_shioaji_repair import proposal, setup


@pytest.fixture(autouse=True)
def closed_day_policy(monkeypatch):
    monkeypatch.setattr(get_settings(), "SHIOAJI_SKIP_NON_TRADING_DAYS", True)


def encoded(v):
    return json.loads(json.dumps(v, default=str))


async def setup_closure(db):
    work, member, _, plan, _ = await setup(db, gap=False)
    work.manifest = {**work.manifest, "mode": "canonical"}
    work.state, work.completed_at = "queued", None
    for i in range(2, 10):
        db.add(
            ProviderWorkAttempt(
                work_id=work.work_id,
                attempt_number=i,
                state="queued" if i == 9 else "settled",
                proof={"original_attempts": 8},
            )
        )
    repair = ShioajiMemberRepair(
        work_id=work.work_id,
        plan_id=plan.plan_id,
        environment="local",
        execution_profile="full_market",
        request_id="original-grant",
        request_sha256="a" * 64,
        member_key=member.member_key,
        original_sha256="b" * 64,
        observation_limit=3,
        evidence={"original": True},
        reason="Original reviewed recovery allowance",
        actor="original-owner",
    )
    db.add(repair)
    await db.flush()
    db.add(ShioajiRepairAck(repair_id=repair.repair_id, generation=2))
    for control in await db.scalars(
        select(SchedulerControl).where(SchedulerControl.provider == "shioaji")
    ):
        control.desired_state = control.observed_state = "stopped"
    if await db.get(SchedulerControl, "pilot_shioaji_v1") is None:
        db.add(
            SchedulerControl(
                scheduler_key="pilot_shioaji_v1",
                provider="shioaji",
                execution_profile="pilot",
                slot_id="taiwan_market_window",
                desired_state="stopped",
                observed_state="stopped",
            )
        )
    decision = await db.get(
        DailyExecutionDecision, (work.dataset_key, "shioaji", work.trade_date, "full_market")
    )
    before = encoded({c.name: getattr(decision, c.name) for c in decision.__table__.columns})
    for day in await db.scalars(
        select(CalendarRevisionDay).where(CalendarRevisionDay.trade_date == work.trade_date)
    ):
        day.is_open, day.day_status, day.holiday_name = False, "closed", "Typhoon closure"
    await db.flush()
    aid = uuid7()
    decision.kind, decision.plan_id = "skipped_calendar", None
    decision.snapshot = {
        **await execution_snapshot(db, "TW", work.trade_date, "shioaji"),
        "snapshot_origin": "manual_calendar_repair",
        "manual_repair": {"original_plan_id": str(plan.plan_id), "audit_event_id": str(aid)},
    }
    after = encoded({c.name: getattr(decision, c.name) for c in decision.__table__.columns})
    payload = {"before": {"decision": before}, "after": {"decision": after}}
    payload.update(before_sha256=_digest(payload["before"]), after_sha256=_digest(payload["after"]))
    db.add(
        AdminAuditEvent(
            event_id=aid,
            actor_type="aws_operator",
            actor_display_name="reviewed original owner",
            action="calendar_decision_repair",
            resource_type="daily_execution_decision",
            resource_id=str(plan.plan_id),
            details=payload,
        )
    )
    db.add(
        MissingDeliveryAlert(
            dataset_key=work.dataset_key,
            source="shioaji",
            schema_id="market_minute",
            schema_version=1,
            execution_profile="full_market",
            expected_data_date=work.trade_date,
            status="resolved",
            resolved_at=utc_now(),
        )
    )
    await queue.lock_provider(db, "shioaji")
    await db.commit()
    current = await inventory(db, work.work_id)
    facts = {
        "environment": "local",
        "execution_profile": "full_market",
        "work_id": str(work.work_id),
        "plan_id": str(plan.plan_id),
        "member_keys": [m["member_key"] for m in current.snapshot["members"]],
        "manifest_sha256": _digest(work.manifest),
        "checksums_valid": True,
        "local_attempts": 8,
    }
    facts.update(
        {
            f: 0
            for f in [
                "prepared_count",
                "receipt_count",
                "active_leases",
                "journal_deliveries",
                "journal_jobs",
                "unknown_probes",
                "active_intents",
            ]
        }
    )
    body = CalendarClosureRequest(
        request_id="calendar-close-1",
        environment="local",
        execution_profile="full_market",
        plan_id=plan.plan_id,
        reason="Review confirmed typhoon closed date and preserved original work",
        checkpoint={
            "observed_at": utc_now(),
            "host_lock_identity": "local:123:456",
            "inventory_sha256": current.sha256,
            "facts": facts,
            "sha256": _digest(facts),
        },
    )
    return work, plan, member, current, body


@pytest.mark.asyncio
async def test_closure_preserves_attempts_manifest_grants_and_summary_and_never_reopens(
    test_session,
):
    db = test_session
    work, plan, member, current, body = await setup_closure(db)
    report = await preview(db, work.work_id, body)
    assert report.apply_ready, report.blockers
    request = CalendarClosureApplyRequest(**body.model_dump(), expected_sha256=report.sha256)
    result = await apply(db, work.work_id, request, actor="owner:reviewer")
    await db.commit()
    assert result.state == work.state == "calendar_closed"
    after = await inventory(db, work.work_id)
    for key in [
        "attempts",
        "plan",
        "members",
        "parts",
        "summary",
        "repairs",
        "repair_acks",
        "decision",
        "alerts",
        "receipts",
    ]:
        assert current.snapshot[key] == after.snapshot[key], key
    assert current.snapshot["work"][0]["manifest"] == after.snapshot["work"][0]["manifest"]
    assert current.snapshot["work"][0]["scope"] == after.snapshot["work"][0]["scope"]
    assert len(after.snapshot["closure"]) == 1
    assert await queue.projection(db, provider="shioaji") == []
    assert (
        await queue.claim(
            db, provider="shioaji", datasets=[work.dataset_key], host_lock_identity="local:123:456"
        )
        is None
    )
    assert not await queue.acquisition_open(db, work)
    replay = await queue.enqueue(
        db,
        provider="shioaji",
        stable_key=work.stable_key,
        kind=work.kind,
        dataset_key=work.dataset_key,
        trade_date=work.trade_date,
        execution_profile=work.execution_profile,
        scope=work.scope,
    )
    assert replay.work_id == work.work_id and replay.state == "calendar_closed"
    again = await apply(db, work.work_id, request, actor="another-owner")
    assert again.replayed and again.closure_id == result.closure_id
    assert len(list(await db.scalars(select(ProviderWorkAttempt)))) == 9
    with pytest.raises(FullMarketError):
        await repair_preview(db, work.work_id, proposal(member, mapping=False))
    with pytest.raises(FullMarketError, match="differs"):
        await apply(
            db,
            work.work_id,
            request.model_copy(update={"reason": "Changed approved request reason"}),
            actor="owner",
        )


@pytest.mark.asyncio
async def test_corrected_calendar_blocks_acquisition_before_closure_but_keeps_prepared_drain(
    test_session,
):
    work, _, _, _, _ = await setup_closure(test_session)
    assert not await queue.acquisition_open(test_session, work)
    assert (await queue.current_calendar_status(test_session, work))["repair_required"] is True
    # Existing preparation remains readable and is not mutated by either gate.
    await queue.store_prepared(
        test_session, work.work_id, "original", {"body": "original-bytes", "body_sha256": "a" * 64}
    )
    await test_session.flush()
    assert not await queue.acquisition_open(test_session, work)
    assert (await prepared_map(test_session, work.work_id))["original"]["body"] == "original-bytes"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variant",
    [
        "calendar_open",
        "policy_off",
        "decision_planned",
        "audit_changed",
        "prepared",
        "receipt",
        "runs",
        "owner",
        "controls",
        "member_keys",
        "manifest",
        "lease",
        "debt",
        "unknown_probe",
        "expiry",
        "scope",
        "schema",
    ],
)
async def test_evaluator_rejects_each_unsafe_snapshot(test_session, variant):
    _, _, _, current, body = await setup_closure(test_session)
    s = deepcopy(current.snapshot)
    b = body.model_copy(deep=True)
    if variant == "calendar_open":
        s["calendar"]["is_open"] = True
    elif variant == "policy_off":
        s["calendar"]["policy"]["skip_non_trading_days"] = False
    elif variant == "decision_planned":
        s["decision"][0]["kind"] = "planned"
    elif variant == "audit_changed":
        s["audit"][0]["details"]["after_sha256"] = "a" * 64
    elif variant == "prepared":
        s["work"][0]["prepared_deliveries"] = {"bytes": "retained"}
    elif variant == "receipt":
        s["receipts"] = [{"run_id": str(uuid7())}]
    elif variant == "runs":
        s["runs"] = [{"run_id": str(uuid7())}]
    elif variant == "owner":
        s["owner"][0]["attempt_id"] = str(uuid7())
    elif variant == "controls":
        s["controls"][0]["desired_state"] = "running"
    elif variant == "member_keys":
        b.checkpoint.facts["member_keys"] = []
    elif variant == "manifest":
        b.checkpoint.facts["manifest_sha256"] = "a" * 64
    elif variant == "lease":
        b.checkpoint.facts["active_leases"] = 1
    elif variant == "debt":
        b.checkpoint.facts["active_intents"] = 1
    elif variant == "unknown_probe":
        b.checkpoint.facts["unknown_probes"] = 1
    elif variant == "expiry":
        b.checkpoint.observed_at = utc_now() - timedelta(minutes=16)
    elif variant == "scope":
        b.environment = "staging"
    elif variant == "schema":
        current = current.model_copy(update={"schema_ready": False})
    current = current.model_copy(update={"snapshot": s, "sha256": _digest(s)})
    b.checkpoint.inventory_sha256 = current.sha256
    b.checkpoint.sha256 = _digest(b.checkpoint.facts)
    assert not evaluate(current, b, now=utc_now()).apply_ready


@pytest.mark.asyncio
async def test_stale_preview_and_transaction_rollback(test_session):
    work, _, _, _, body = await setup_closure(test_session)
    report = await preview(test_session, work.work_id, body)
    req = CalendarClosureApplyRequest(**body.model_dump(), expected_sha256=report.sha256)
    wid = work.work_id
    async with test_session.begin_nested() as transaction:
        await apply(test_session, work.work_id, req, actor="owner")
        await transaction.rollback()
    assert await test_session.get(ProviderWorkCalendarClosure, wid) is None
    assert (await test_session.get(ProviderWork, wid, populate_existing=True)).state == "queued"
    control = await test_session.get(SchedulerControl, "full_market_shioaji_v1")
    control.revision += 1
    await test_session.flush()
    with pytest.raises(FullMarketError, match="stale"):
        await apply(test_session, work.work_id, req, actor="owner")


@pytest.mark.asyncio
async def test_owner_api_auth_schema_and_source_inventory_scope(
    client, test_session, admin_headers
):
    work, _, _, _, body = await setup_closure(test_session)
    url = f"/api/v1/admin/provider-work/{work.work_id}/calendar-closure"
    assert (
        await client.post(url + "/preview", json=body.model_dump(mode="json"))
    ).status_code == 401
    from app.services.api_keys import create_api_key

    _, key = await create_api_key(
        test_session,
        owner="test",
        tier="standard",
        scopes=["admin"],
        kind="admin",
        name="closure-viewer",
        role="viewer",
        rate_limit_requests=100,
        rate_limit_window=60,
        page_size_limit=1000,
        commit=False,
    )
    assert (
        await client.post(
            url + "/preview", json=body.model_dump(mode="json"), headers={"X-API-Key": key}
        )
    ).status_code == 403
    r = await client.post(
        url + "/preview", json=body.model_dump(mode="json"), headers=admin_headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["apply_ready"]
    r = await client.post(
        url,
        json={**body.model_dump(mode="json"), "expected_sha256": r.json()["sha256"]},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    from app.services.source_clients import create_source_client

    _, key = await create_source_client(
        test_session,
        name="closure-reader",
        owner="tests",
        source_name="shioaji",
        allowed_datasets=["tw_etf_minute"],
        rate_limit_requests=100,
        rate_limit_window=60,
        commit=False,
    )
    source = f"/api/v1/source/provider-work/{work.work_id}/calendar-closure"
    assert (await client.get(source, headers={"X-API-Key": key})).status_code == 403
    from app.main import app

    schema = app.openapi()["paths"]
    assert schema[url.replace(str(work.work_id), "{work_id}")]["post"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"] == {"$ref": "#/components/schemas/CalendarClosureResult"}


@pytest.mark.asyncio
async def test_owner_fence_race_invalidates_preview_without_losing_evidence(test_engine):
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    async with sessions() as db:
        work, _, _, _, body = await setup_closure(db)
        wid = work.work_id
        report = await preview(db, wid, body)
        req = CalendarClosureApplyRequest(**body.model_dump(), expected_sha256=report.sha256)
    # A real competing owner transaction wins the provider lock. Closure must
    # wait, then reject its stale generation instead of retiring active work.
    async with sessions() as other:
        owner = await queue.lock_provider(other, "shioaji")
        attempt = await other.scalar(
            select(ProviderWorkAttempt).where(
                ProviderWorkAttempt.work_id == wid, ProviderWorkAttempt.attempt_number == 9
            )
        )
        owner.attempt_id, owner.generation = attempt.attempt_id, 7
        owner.expires_at = utc_now() + timedelta(minutes=5)
        attempt.state = "open"
        await other.flush()

        async def closing():
            async with sessions() as close_db:
                with pytest.raises(FullMarketError, match="stale"):
                    await apply(close_db, wid, req, actor="owner")
                await close_db.rollback()

        task = asyncio.create_task(closing())
        await asyncio.sleep(0.05)
        assert not task.done()
        await other.commit()
        await asyncio.wait_for(task, 5)
    async with sessions() as db:
        assert await db.get(ProviderWorkCalendarClosure, wid) is None
        assert (await db.get(ProviderWork, wid)).state == "queued"
        assert len(list(await db.scalars(select(ShioajiRepairAck)))) == 1
