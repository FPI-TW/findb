"""Frozen scope repair approvals never rewrite prior mapping or delivery bytes."""

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.models.provider_work import ProviderWork, ProviderWorkAttempt
from app.models.registry import DailyDeliveryMember, DailyDeliveryPlan, UniverseMember
from app.models.shioaji_repair import ShioajiMemberRepair, ShioajiRepairAck
from app.schemas.admin import ShioajiRepairApplyResponse, ShioajiRepairPreviewResponse
from app.schemas.full_market import DeliveryOutcomeRequest
from app.schemas.shioaji_repair import RepairApplyRequest, RepairPreviewRequest
from app.services import provider_work as queue
from app.services.full_market import (
    FullMarketError,
    _digest,
    reconcile_plan,
    record_outcome,
    validate_ingest_part,
)
from app.services.shioaji_repair import apply, approved, effective_mappings, inventory, preview
from app.utils import utc_now, uuid7
from tests.test_full_market import DAY, _activated_plan

EVIDENCE = {"url": "https://example.org/reviewed-evidence", "sha256": "a" * 64}


async def setup(db, *, gap=True):
    _, _, plan = await _activated_plan(db, "tw_equity_minute", count=2)
    member = await db.scalar(
        select(DailyDeliveryMember)
        .where(DailyDeliveryMember.plan_id == plan.plan_id)
        .order_by(DailyDeliveryMember.member_key)
    )
    frozen = await db.scalar(
        select(UniverseMember).where(
            UniverseMember.release_id == plan.release_id,
            UniverseMember.member_key == member.member_key,
        )
    )
    if gap:
        member.mapping_status = frozen.mapping_status = "gap"
        member.outcome, member.outcome_reason = "blocked", "mapping_gap"
        member.outcome_history = [{"reason": "mapping_gap", "evidence": "original"}]
    work = ProviderWork(
        environment="local",
        provider="shioaji",
        stable_key=f"daily:full_market:tw_equity_minute:{DAY}",
        kind="daily",
        dataset_key="tw_equity_minute",
        trade_date=DAY,
        execution_profile="full_market",
        manifest={
            "plan_id": str(plan.plan_id),
            "members": [{"key": row} for part in plan.parts for row in part.member_keys],
        },
        state="failed",
        completed_at=utc_now(),
    )
    db.add(work)
    await db.flush()
    attempt = ProviderWorkAttempt(
        work_id=work.work_id,
        attempt_number=1,
        state="settled",
        outcome="failed",
        proof={"original": "unchanged"},
    )
    db.add(attempt)
    await db.commit()
    return work, member, frozen, plan, attempt


def proposal(member, *, mapping=True, request_id="reviewed-repair"):
    change = {
        "member_key": member.member_key,
        "evidence": EVIDENCE,
        "reason": "Reviewed cached empty observation and frozen mapping evidence",
    }
    if mapping:
        change["mapping"] = {
            "symbol": member.symbol,
            "provider_symbol": member.provider_symbol,
            "exchange": "TWSE",
            "currency": "TWD",
            "reference": EVIDENCE,
        }
    return RepairPreviewRequest(request_id=request_id, members=[change])


async def authorize(db, work, body):
    report = await preview(db, work.work_id, body)
    request = RepairApplyRequest(**body.model_dump(), expected_sha256=report["sha256"])
    result = await apply(db, work.work_id, request, actor="owner:reviewer")
    await db.commit()
    return request, result


@pytest.mark.asyncio
async def test_mapping_overlay_preserves_frozen_rows_and_requeues_original_work(test_session):
    db = test_session
    work, member, frozen, plan, attempt = await setup(db)
    original = (
        deepcopy(work.manifest),
        member.mapping_status,
        frozen.mapping_status,
        deepcopy(member.outcome_history),
        deepcopy(attempt.proof),
    )
    request, result = await authorize(db, work, proposal(member))
    assert work.state == "queued" and not work.completed_at
    attempts = list(
        await db.scalars(
            select(ProviderWorkAttempt)
            .where(ProviderWorkAttempt.work_id == work.work_id)
            .order_by(ProviderWorkAttempt.attempt_number)
        )
    )
    assert [(row.attempt_number, row.state) for row in attempts] == [(1, "settled"), (2, "queued")]
    assert await effective_mappings(db, plan.plan_id) == {member.member_key: member.provider_symbol}
    summary = await reconcile_plan(db, await db.get(DailyDeliveryPlan, plan.plan_id))
    assert (
        summary.expected == summary.data + summary.no_data + summary.missing + summary.blocked == 2
    )
    assert summary.data == summary.no_data == summary.blocked == 0
    assert (
        work.manifest,
        member.mapping_status,
        frozen.mapping_status,
        member.outcome_history,
        attempt.proof,
    ) == original
    replay = await apply(db, work.work_id, request, actor="another-owner")
    assert replay["replayed"] and replay["repair_ids"] == result["repair_ids"]
    assert len(list(await db.scalars(select(ShioajiMemberRepair)))) == 1
    assert len(list(await db.scalars(select(ProviderWorkAttempt)))) == 2


@pytest.mark.asyncio
async def test_stale_preview_and_changed_idempotency_bytes_are_rejected(test_session):
    work, member, _, _, _ = await setup(test_session)
    body = proposal(member)
    report = await preview(test_session, work.work_id, body)
    member.outcome_reason = "source_error"
    await test_session.flush()
    with pytest.raises(FullMarketError, match="stale"):
        await apply(
            test_session,
            work.work_id,
            RepairApplyRequest(**body.model_dump(), expected_sha256=report["sha256"]),
            actor="owner",
        )
    request, _ = await authorize(test_session, work, body)
    changed = request.model_copy(
        update={"members": [request.members[0].model_copy(update={"observation_limit": 1})]}
    )
    with pytest.raises(FullMarketError, match="different bytes"):
        await apply(test_session, work.work_id, changed, actor="owner")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variant", ["prepared", "wrong_identity", "missing_mapping", "completed", "cancelled"]
)
async def test_repair_rejects_unsafe_or_unproven_changes(test_session, variant):
    work, member, _, _, _ = await setup(test_session)
    body = proposal(member, mapping=variant != "missing_mapping")
    if variant == "prepared":
        work.prepared_deliveries = {
            "opaque-delivery-key": {
                "canonical_members": [member.member_key],
                "body_sha256": "c" * 64,
            }
        }
    elif variant == "wrong_identity":
        body.members[0].mapping.currency = "USD"
    else:
        if variant in {"completed", "cancelled"}:
            work.state = variant
    await test_session.flush()
    with pytest.raises(FullMarketError):
        await preview(test_session, work.work_id, body)


@pytest.mark.asyncio
async def test_effective_mapping_is_used_by_ingress_and_no_data_still_requires_evidence(
    test_session, monkeypatch
):
    work, member, _, plan, _ = await setup(test_session)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "fetcher/src"))
    from findb_fetcher.providers.shioaji import build_market_minute_request

    from app.services.ingress_contracts import validate_ingress_request

    ts = int(
        (datetime(DAY.year, DAY.month, DAY.day, 9, 1) - datetime(1970, 1, 1)).total_seconds() * 1e9
    )
    raw = build_market_minute_request(
        {
            "ts": (ts,),
            "Open": (100,),
            "High": (101,),
            "Low": (99,),
            "Close": (100,),
            "Volume": (1,),
            "Amount": (100,),
        },
        dataset_key=work.dataset_key,
        target_date=DAY,
        symbols=(member.symbol,),
        fetched_at=utc_now(),
        usage_before_requests=0,
        usage_after_requests=1,
        snapshot_id="original-snapshot",
        daily_update_id=str(plan.plan_id),
        universe_id=str(plan.release_id),
    )
    raw["delivery"] = {
        "work_item_id": plan.parts[0].work_item_id,
        "target_data_date": str(DAY),
        "slot_id": "taiwan_market_window",
        "scheduled_for": datetime(
            DAY.year, DAY.month, DAY.day, 10, tzinfo=timezone.utc
        ).isoformat(),
    }
    body = validate_ingress_request(raw)
    dataset = await _dataset_for(test_session, work.dataset_key)
    with pytest.raises(FullMarketError, match="Mapping-gap"):
        await validate_ingest_part(test_session, body, dataset)
    await authorize(test_session, work, proposal(member))
    assert await validate_ingest_part(test_session, body, dataset) == member.part_id
    with pytest.raises(ValueError, match="durable source evidence"):
        DeliveryOutcomeRequest(
            version=1,
            work_item_id=plan.parts[0].work_item_id,
            member_key=member.member_key,
            outcome="no_data",
            reason="no_trade",
        )
    await record_outcome(
        test_session,
        plan.plan_id,
        DeliveryOutcomeRequest(
            version=1,
            work_item_id=plan.parts[0].work_item_id,
            member_key=member.member_key,
            outcome="blocked",
            reason="empty_response",
        ),
        provider="shioaji",
        allowed_datasets=[work.dataset_key],
    )
    state = await inventory(test_session, work.work_id)
    assert state["members"][0]["reason"] == "empty_response"


async def _dataset_for(db, key):
    from app.models.registry import DatasetRegistry

    return await db.get(DatasetRegistry, key)


def test_repair_openapi_uses_typed_responses():
    from app.main import app

    schema = app.openapi()
    root = "/api/v1/admin/provider-work/{work_id}/shioaji-repairs"
    for path, model in (
        (root + "/preview", ShioajiRepairPreviewResponse),
        (root, ShioajiRepairApplyResponse),
    ):
        response = schema["paths"][path]["post"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]
        assert response == {"$ref": f"#/components/schemas/{model.__name__}"}
    models = schema["components"]["schemas"]
    for name, hashes in (
        ("ShioajiRepairPreviewResponse", ["sha256"]),
        ("ShioajiRepairInventoryResponse", ["sha256", "manifest_sha256", "prepared_sha256"]),
    ):
        assert models[name]["additionalProperties"] is False
        for key in hashes:
            assert key in models[name]["required"]
            assert models[name]["properties"][key]["pattern"] == r"^[0-9a-f]{64}$"


@pytest.mark.asyncio
async def test_owner_typed_responses_preserve_digest_and_replay(
    client, test_session, admin_headers
):
    work, member, _, _, _ = await setup(test_session)
    body = proposal(member)
    body.members[0].reason = "已核對凍結 mapping 與原始空回應證據"
    expected = await preview(test_session, work.work_id, body)
    url = f"/api/v1/admin/provider-work/{work.work_id}/shioaji-repairs"
    response = await client.post(
        url + "/preview", json=body.model_dump(mode="json"), headers=admin_headers
    )
    assert response.status_code == 200, response.text
    report = response.json()
    assert report == expected
    assert ShioajiRepairPreviewResponse.model_validate(report).model_dump(mode="json") == expected
    assert report["inventory"]["sha256"] == _digest(
        {key: value for key, value in report["inventory"].items() if key != "sha256"}
    )
    assert report["sha256"] == _digest(
        {"inventory": report["inventory"]["sha256"], "proposal": report["proposal"]}
    )
    for path, value in (
        (("sha256",), "invalid"),
        (("inventory", "sha256"), "A" * 64),
        (("inventory", "manifest_sha256"), "a" * 63),
        (("inventory", "prepared_sha256"), None),
        (("inventory", "members", 0, "has_prepared"), "false"),
    ):
        invalid = deepcopy(report)
        target = invalid
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        with pytest.raises(ValidationError):
            ShioajiRepairPreviewResponse.model_validate(invalid)
    for path in (("sha256",), ("inventory", "sha256"), ("inventory", "members", 0, "reason")):
        invalid = deepcopy(report)
        target = invalid
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]
        with pytest.raises(ValidationError):
            ShioajiRepairPreviewResponse.model_validate(invalid)
    invalid = deepcopy(report)
    invalid["inventory"]["unrecognized_evidence"] = "must not be silently dropped"
    with pytest.raises(ValidationError):
        ShioajiRepairPreviewResponse.model_validate(invalid)
    request = {**report["proposal"], "expected_sha256": report["sha256"]}
    response = await client.post(url, json=request, headers=admin_headers)
    assert response.status_code == 200, response.text
    applied = response.json()
    assert ShioajiRepairApplyResponse.model_validate(applied).model_dump(mode="json") == applied
    assert applied["replayed"] is False and len(applied["repair_ids"]) == 1
    response = await client.post(url, json=request, headers=admin_headers)
    assert response.status_code == 200, response.text
    assert response.json() == {**applied, "replayed": True}
    assert len(list(await test_session.scalars(select(ShioajiMemberRepair)))) == 1
    assert len(list(await test_session.scalars(select(ProviderWorkAttempt)))) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        {"repair_ids": ["invalid"], "replayed": False},
        {"repair_ids": [], "replayed": False},
        {"repair_ids": ["00000000-0000-0000-0000-000000000001"], "replayed": "false"},
        {"repair_ids": ["00000000-0000-0000-0000-000000000001"]},
    ],
)
async def test_invalid_apply_response_is_rejected_before_commit(monkeypatch, result):
    from app.api.deps import AdminPrincipal
    from app.api.v1.admin import apply_shioaji_repair

    db = AsyncMock()
    monkeypatch.setattr("app.services.shioaji_repair.apply", AsyncMock(return_value=result))
    body = RepairApplyRequest(
        **proposal(
            SimpleNamespace(member_key="2330", symbol="2330", provider_symbol="2330")
        ).model_dump(),
        expected_sha256="a" * 64,
    )
    with pytest.raises(ValidationError):
        await apply_shioaji_repair(
            uuid7(), body, AdminPrincipal("machine_key", "test-owner", "tests", "owner"), db
        )
    db.commit.assert_not_awaited()


@pytest.mark.parametrize("field", ["symbol", "provider_symbol"])
@pytest.mark.parametrize(
    "symbol",
    [
        "2330",
        "00416A",
        ".A--",
        "A" * 50,
        "TSE:2330",
        "TW_2330",
        "A" * 51,
        "--",
        ".",
        "",
        "２３３０",
        " 2330 ",
        "A\nB",
        None,
        42,
    ],
)
def test_mapping_symbols_match_fetcher_validator(monkeypatch, field, symbol):
    from pydantic import ValidationError

    from app.schemas.shioaji_repair import MappingEvidence

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "fetcher/src"))
    from findb_fetcher.providers.shioaji import ShioajiPayloadError, _validate_symbol

    values = {
        "symbol": "2330",
        "provider_symbol": "2330",
        "exchange": "XTAI",
        "currency": "TWD",
        "reference": EVIDENCE,
        field: symbol,
    }
    try:
        _validate_symbol(symbol)
    except ShioajiPayloadError:
        with pytest.raises(ValidationError):
            MappingEvidence.model_validate(values)
    else:
        assert getattr(MappingEvidence.model_validate(values), field) == symbol


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["symbol", "provider_symbol"])
@pytest.mark.parametrize("symbol", ["TSE:2330", "TW_2330", "A" * 51, "--", "２３３０", " 2330 "])
async def test_owner_api_rejects_unfetchable_symbols_before_approval(
    client, test_session, admin_headers, field, symbol
):
    work, member, frozen, _, _ = await setup(test_session)
    body = proposal(member).model_dump(mode="json")
    body["members"][0]["mapping"][field] = symbol
    url = f"/api/v1/admin/provider-work/{work.work_id}/shioaji-repairs"
    for endpoint, value in ((url + "/preview", body), (url, {**body, "expected_sha256": "0" * 64})):
        response = await client.post(endpoint, json=value, headers=admin_headers)
        assert response.status_code == 422, response.text
    assert not list(await test_session.scalars(select(ShioajiMemberRepair)))
    assert work.state == "failed" and member.mapping_status == frozen.mapping_status == "gap"


@pytest.mark.asyncio
async def test_owner_required_and_source_repairs_are_fenced(
    client, test_session, admin_headers, source_headers
):
    work, member, _, _, _ = await setup(test_session)
    body = proposal(member).model_dump(mode="json")
    url = f"/api/v1/admin/provider-work/{work.work_id}/shioaji-repairs/preview"
    assert (await client.post(url, json=body)).status_code == 401
    response = await client.post(url, json=body, headers=admin_headers)
    assert response.status_code == 200, response.text
    from app.services.api_keys import create_api_key
    from app.services.source_clients import create_source_client

    _, operator = await create_api_key(
        test_session,
        owner="tests",
        tier="standard",
        scopes=["admin"],
        rate_limit_requests=100,
        rate_limit_window=60,
        page_size_limit=1000,
        kind="admin",
        name="repair-operator",
        role="operator",
        commit=False,
    )
    assert (await client.post(url, json=body, headers={"X-API-Key": operator})).status_code == 403
    diagnostic = f"/api/v1/source/provider-work/{work.work_id}/shioaji-diagnostics"
    assert (await client.get(diagnostic)).status_code == 401
    assert (await client.get(diagnostic, headers=source_headers)).status_code == 403
    _, source_key = await create_source_client(
        test_session,
        name="repair-source",
        source_name="shioaji",
        allowed_datasets=["tw_equity_minute"],
        rate_limit_requests=100,
        rate_limit_window=60,
        commit=False,
    )
    headers = {"X-API-Key": source_key}
    assert (await client.get(diagnostic, headers=headers)).status_code == 200
    await authorize(test_session, work, proposal(member))
    # Claim uses the original work and a fresh fence; direct stale fence is rejected.
    owner = await queue.lock_provider(test_session, "shioaji")
    attempt = await test_session.scalar(
        select(ProviderWorkAttempt).where(
            ProviderWorkAttempt.work_id == work.work_id, ProviderWorkAttempt.state == "queued"
        )
    )
    owner.attempt_id, owner.token, owner.generation = attempt.attempt_id, uuid7(), 2
    from datetime import timedelta

    owner.expires_at = utc_now() + timedelta(minutes=5)
    await test_session.commit()
    token = str(owner.token)
    fenced_url = f"/api/v1/source/provider-work/{work.work_id}/shioaji-repairs"
    assert (
        await client.post(
            fenced_url, json={"generation": 1, "token": str(uuid7())}, headers=headers
        )
    ).status_code == 409
    response = await client.post(
        fenced_url, json={"generation": 2, "token": token}, headers=headers
    )
    assert response.status_code == 200, response.text
    with pytest.raises(queue.ProviderWorkError, match="fence differs"):
        await queue.fence(
            test_session, provider="shioaji", work_id=work.work_id, generation=1, token=uuid7()
        )
    approved_rows = await approved(test_session, work)
    repair_id = approved_rows["repairs"][0]["repair_id"]
    for _ in range(2):
        await approved(test_session, work, ack_ids=[repair_id], generation=2)
        await test_session.flush()
    assert len(list(await test_session.scalars(select(ShioajiRepairAck)))) == 1
    with pytest.raises(FullMarketError, match="ACK differs"):
        await approved(test_session, work, ack_ids=[str(uuid7())], generation=2)
