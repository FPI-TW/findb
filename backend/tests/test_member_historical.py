"""Twelve Data/FinLab historical dates freeze their Full release cohort (#409, #433)."""

import json
from copy import deepcopy
from datetime import date, datetime, timezone
from uuid import UUID

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.registry import (
    DailyDeliveryMember,
    DailyDeliveryPart,
    DailyDeliveryPlan,
    DailyDeliveryPlanSummary,
    HistoricalBackfillItem,
    HistoricalBackfillRequest,
)
from app.schemas.full_market import UniversePublishRequest
from app.services import historical_backfill
from app.services.delivery_plan_summary import refresh_plan_summary
from app.services.full_market import publish_universe, submit_universe
from app.services.source_clients import create_source_client
from app.utils import uuid7
from scripts.seed_data import seed_datasets
from tests.test_canonical_ingest_api import _execute_run
from tests.test_full_market import _universe
from tests.test_provider_work import attest_cutover

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
FIRST, SECOND = date(2026, 10, 15), date(2026, 10, 16)
SCOPES = {"finlab": "tw_equity_eod", "twelve_data": "us_equity_eod"}
SHA = "b" * 64
RAW = f"r2://{'a' * 32}/findb-raw/finlab/tw_equity_eod/0123456789abcdef/{SHA}.json"


async def _publish(db, body, *, first: bool):
    release = await submit_universe(db, body, allowed_datasets=[body.dataset_key])
    await publish_universe(
        db,
        release.release_id,
        UniversePublishRequest(
            evidence_note="Verified official universe for the test.",
            **(
                {"first_baseline_approved": True}
                if first
                else {"threshold_exception_approved": True}
            ),
        ),
        actor="test-owner",
    )
    await db.commit()
    return release


async def setup(db, monkeypatch, provider="finlab"):
    monkeypatch.setattr(get_settings(), "PROVIDER_WORK_REQUIRED", True)
    monkeypatch.setattr("app.services.provider_work.utc_now", lambda: NOW)
    monkeypatch.setattr("app.services.full_market.utc_now", lambda: NOW)
    monkeypatch.setattr(historical_backfill, "utc_now", lambda: NOW)
    await seed_datasets(db)
    await attest_cutover(db)
    dataset = SCOPES[provider]
    body = _universe(dataset, 4, date(2026, 10, 1))
    # One mapping gap and one listing after the first date never enter a cohort.
    body.members[2].mapping_status = "gap"
    body.members[3].listed_date = SECOND
    release = await _publish(db, body, first=True)
    _, key = await create_source_client(
        db,
        name=provider,
        source_name=provider,
        allowed_datasets=[dataset],
        rate_limit_requests=100000,
        rate_limit_window=60,
        commit=False,
    )
    await db.commit()
    return {"X-API-Key": key}, body, release


async def _plan(db, provider, release_id, day, members):
    plan = DailyDeliveryPlan(
        plan_id=uuid7(),
        dataset_key=SCOPES[provider],
        provider=provider,
        trade_date=day,
        release_id=release_id,
        execution_profile="full_market",
        activation_cutoff=FIRST,
        deadline_at=NOW,
        member_sha256="a" * 64,
        expected_count=len(members),
    )
    db.add(plan)
    await db.flush()
    part = DailyDeliveryPart(
        part_id=uuid7(),
        plan_id=plan.plan_id,
        part_number=1,
        work_item_id="fp1:" + uuid7().hex,
        member_sha256="a" * 64,
        expected_count=len(members),
    )
    db.add(part)
    await db.flush()
    for member in members:
        db.add(
            DailyDeliveryMember(
                member_id=uuid7(),
                plan_id=plan.plan_id,
                part_id=part.part_id,
                member_key=member.symbol,
                symbol=member.symbol,
                provider_symbol=member.provider_symbol,
                mapping_status=member.mapping_status,
            )
        )
    await refresh_plan_summary(db, plan.plan_id)
    await db.commit()
    return plan


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["finlab", "twelve_data"])
async def test_each_date_freezes_its_plan_release_or_latest_published(
    test_session, monkeypatch, provider
):
    _, body, release = await setup(test_session, monkeypatch, provider)
    plan = await _plan(test_session, provider, release.release_id, FIRST, body.members[:3])
    changed = deepcopy(body)
    # A later list adds 0005 without a listing date: the first date's list missed it.
    late = changed.members[0].model_copy(update={"symbol": "0005", "provider_symbol": "0005"})
    changed.members = [*changed.members[1:], late]
    changed.effective_date = date(2026, 10, 16)
    newer = await _publish(test_session, changed, first=False)
    # The operator preview counts the members each date would freeze, before creation.
    assert await historical_backfill.preview_member_counts(
        test_session, provider=provider, dataset_key=SCOPES[provider], days=[FIRST, SECOND]
    ) == {FIRST: 2, SECOND: 3}
    request, _ = await historical_backfill.create_request(
        test_session,
        provider=provider,
        dataset_key=SCOPES[provider],
        start_date=FIRST,
        end_date=SECOND,
        request_key=f"{provider}-cohort",
        created_by="owner",
    )
    snapshot = request.universe_snapshot
    assert snapshot["version"] == 3 and snapshot["cohort"] == "full_release"
    first, second = snapshot["dates"][FIRST.isoformat()], snapshot["dates"][SECOND.isoformat()]
    # The plan's release wins for its date; otherwise the latest published by then.
    assert (first["release_id"], first["plan_id"]) == (str(release.release_id), str(plan.plan_id))
    assert (second["release_id"], second["plan_id"]) == (str(newer.release_id), None)
    assert (first["member_count"], second["member_count"]) == (2, 3)
    # Only the first date has a later release; the gap and the later listing stay out.
    assert first["supplement_release_ids"] == [str(newer.release_id)]
    assert (first["supplement_count"], second["supplement_count"]) == (1, 0)
    items = {
        item.trade_date: item
        for item in await test_session.scalars(
            select(HistoricalBackfillItem).where(
                HistoricalBackfillItem.request_id == request.request_id
            )
        )
    }
    for item in items.values():
        await test_session.refresh(item, ["request"])
    cohort = await historical_backfill.frozen_cohort(test_session, items[FIRST])
    # Mapped members listed by the date only; the gap and the later listing are out.
    assert [m["member_key"] for m in cohort["members"]] == [m.symbol for m in body.members[:2]]
    assert cohort["release_id"] == str(release.release_id)
    assert [m["member_key"] for m in cohort["supplementary"]] == ["0005"]
    # The verified cohort is cached per date; callers get copies, never the cache.
    cohort["members"].clear()
    cohort["supplementary"][0]["provider_symbol"] = "CHANGED"
    again = await historical_backfill.frozen_cohort(test_session, items[FIRST])
    assert [m["member_key"] for m in again["members"]] == [m.symbol for m in body.members[:2]]
    assert again["supplementary"][0]["provider_symbol"] == "0005"
    later = await historical_backfill.frozen_cohort(test_session, items[SECOND])
    assert [m["member_key"] for m in later["members"]] == [
        body.members[1].symbol,
        body.members[3].symbol,
        "0005",
    ]
    assert later["supplementary"] == []


@pytest.mark.asyncio
async def test_date_without_any_published_release_is_rejected(test_session, monkeypatch):
    await setup(test_session, monkeypatch)
    with pytest.raises(historical_backfill.HistoricalBackfillError, match="no published universe"):
        await historical_backfill.create_request(
            test_session,
            provider="finlab",
            dataset_key="tw_equity_eod",
            start_date=date(2026, 9, 29),
            end_date=date(2026, 9, 30),
            request_key="before-baseline",
            created_by="owner",
        )


async def _claim(client, headers):
    claimed = await client.post(
        "/api/v1/source/provider-work/claim",
        headers=headers,
        json={"protocol": "provider-work-v1", "host_lock_identity": "local:1:22", "recover": True},
    )
    assert claimed.status_code == 200, claimed.text
    owner = claimed.json()["work"]
    work_headers = {
        **headers,
        "X-FinDB-Work-Id": owner["work_id"],
        "X-FinDB-Work-Generation": str(owner["generation"]),
        "X-FinDB-Work-Token": owner["token"],
    }
    lease = await client.post("/api/v1/source/historical-backfills/claim", headers=work_headers)
    assert lease.status_code == 200, lease.text
    return owner, work_headers, lease.json()


def _body(item, member, day, *, symbol=None):
    return {
        "dataset_key": "tw_equity_eod",
        "source": "finlab",
        "schema_id": "market_eod",
        "schema_version": 1,
        "request_key": f"history:{item['item_id']}:{member['member_key']}",
        "idempotency_key": f"history:{item['item_id']}:{member['member_key']}",
        "fetched_at": NOW.isoformat(),
        "delivery": {
            "slot_id": "taiwan_market_window",
            "scheduled_for": NOW.isoformat(),
            "target_data_date": day,
            "work_item_id": "historical:" + item["item_id"],
        },
        "payload": {
            "batch": {
                "data_date": day,
                "delivery_mode": "backfill",
                "coverage_start_date": day,
                "coverage_end_date": day,
                "declared_record_count": 1,
            },
            "data": [
                {
                    "symbol": symbol or member["symbol"],
                    "source_symbol": member["provider_symbol"],
                    "currency": "TWD",
                    "trade_date": day,
                    "open": "10",
                    "high": "11",
                    "low": "9",
                    "close": "10",
                    "volume": 100,
                }
            ],
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("quiet", [False, True], ids=["missing", "no_trade"])
async def test_member_evidence_completes_and_repairs_the_full_plan(
    client, test_session, test_engine, monkeypatch, quiet
):
    headers, body, release = await setup(test_session, monkeypatch)
    plan = await _plan(test_session, "finlab", release.release_id, FIRST, body.members[:3])
    plan_id = plan.plan_id
    request, _ = await historical_backfill.create_request(
        test_session,
        provider="finlab",
        dataset_key="tw_equity_eod",
        start_date=FIRST,
        end_date=FIRST,
        request_key="finlab-repair",
        created_by="owner",
    )
    request_id = request.request_id
    await test_session.commit()
    owner, work_headers, item = await _claim(client, headers)
    snapshot = item["universe_snapshot"]
    assert snapshot["trade_date"] == FIRST.isoformat()
    members = snapshot["members"]
    assert [m["member_key"] for m in members] == ["0001", "0002"]
    manifest = {
        "mode": "canonical",
        "contract": {"schema_id": "market_eod", "schema_version": 1},
        "universe_release_id": snapshot["release_id"],
        "members": [
            {
                "key": m["member_key"],
                "source_symbol": m["provider_symbol"],
                "mapping_status": m["mapping_status"],
            }
            for m in members
        ],
    }
    frozen = await client.post(
        f"/api/v1/source/provider-work/{owner['work_id']}/manifest",
        headers=headers,
        json={"generation": owner["generation"], "token": owner["token"], "manifest": manifest},
    )
    assert frozen.status_code == 200, frozen.text
    # FinLab serves the whole market: a served symbol outside the frozen cohort (here
    # the release's mapping gap) is written as unlisted, in the dataset's currency only.
    gap = {"member_key": "0003", "symbol": "0003", "provider_symbol": "0003"}
    outside = _body(item, gap, FIRST.isoformat())
    foreign = deepcopy(outside)
    foreign["payload"]["data"][0]["currency"] = "USD"
    rejected = await client.post(
        "/api/v1/source/ingest",
        headers={**work_headers, "X-FinDB-Work-Member": foreign["idempotency_key"]},
        json=foreign,
    )
    assert rejected.status_code == 409, rejected.text
    for stage in ("intent", "confirm"):
        prepared = await client.post(
            f"/api/v1/source/provider-work/{owner['work_id']}/prepare",
            headers=headers,
            json={
                "generation": owner["generation"],
                "token": owner["token"],
                "body": json.dumps(outside),
                "stage": stage,
            },
        )
        assert prepared.status_code == 200, prepared.text
    unlisted = await client.post(
        "/api/v1/source/ingest",
        headers={
            **work_headers,
            "X-FinDB-Work-Member": outside["idempotency_key"],
            "Content-Type": "application/json",
        },
        content=json.dumps(outside),
    )
    assert unlisted.status_code == 202, unlisted.text
    await _execute_run(test_session, test_engine, UUID(unlisted.json()["run_id"]))
    delivered = _body(item, members[0], FIRST.isoformat())
    accepted = await client.post(
        "/api/v1/source/ingest",
        headers={**work_headers, "X-FinDB-Work-Member": delivered["idempotency_key"]},
        json=delivered,
    )
    assert accepted.status_code == 202, accepted.text
    await _execute_run(test_session, test_engine, UUID(accepted.json()["run_id"]))
    evidence = {
        "version": 2,
        "schema_id": "market_eod",
        "schema_version": 1,
        "trade_date": FIRST.isoformat(),
        "item_id": item["item_id"],
        "request_id": str(request_id),
        "members": [
            {"member_key": "0001", "outcome": "data"},
            # A no-trade member is proven by its scoped raw observation; a missing
            # series (provider 404 / no row) stays a gap without one.
            {"member_key": "0002", "outcome": "no_data", "raw": {"ref": RAW, "sha256": SHA}}
            if quiet
            else {"member_key": "0002", "outcome": "missing", "reason": "no_source_row"},
        ],
    }
    status = "completed" if quiet else "partial"
    path = f"/api/v1/source/historical-backfills/{item['item_id']}/complete"
    for invalid, claimed in (
        ({**evidence, "members": evidence["members"][:1]}, status),
        ({**evidence, "schema_version": 2}, status),
        (evidence, "partial" if quiet else "completed"),
        (
            {
                **evidence,
                "members": [evidence["members"][0], {"member_key": "0002", "outcome": "no_data"}],
            },
            "completed",
        ),
    ):
        response = await client.post(
            path,
            headers=work_headers,
            json={"status": claimed, "lease_token": owner["token"], "result_evidence": invalid},
        )
        assert response.status_code == 422, response.text
    complete = await client.post(
        path,
        headers=work_headers,
        json={"status": status, "lease_token": owner["token"], "result_evidence": evidence},
    )
    assert complete.status_code == 200, complete.text
    replay = await client.post(
        path,
        headers=work_headers,
        json={"status": status, "lease_token": owner["token"], "result_evidence": evidence},
    )
    assert replay.status_code == 200, replay.text
    loaded = await test_session.get(HistoricalBackfillRequest, request_id, populate_existing=True)
    assert loaded.status == status
    stored = await test_session.get(
        HistoricalBackfillItem, UUID(item["item_id"]), populate_existing=True
    )
    assert stored.unlisted_symbols == ["0003"]
    # The historical write resolves the Full plan's gap and its no-trade proof
    # completes the member; a missing series stays missing, the gap stays unavailable.
    # Completion itself refreshes the stored counts; a full rebuild agrees.
    expected = (1, 1, 0, 0, 1) if quiet else (1, 0, 1, 0, 1)
    for rebuild in (False, True):
        if rebuild:
            await refresh_plan_summary(test_session, plan_id)
        summary = await test_session.get(DailyDeliveryPlanSummary, plan_id, populate_existing=True)
        counts = (
            summary.data_count,
            summary.no_data_count,
            summary.missing_count,
            summary.blocked_count,
            summary.unavailable_count,
        )
        assert counts == expected
    # The Full plan's own members keep the outcomes their daily work recorded.
    assert {
        member.outcome
        for member in await test_session.scalars(
            select(DailyDeliveryMember).where(DailyDeliveryMember.plan_id == plan_id)
        )
    } == {None}


async def _historical_item(db, provider, day, *, status, members, frozen, schema_version=1):
    """A settled historical date with its work manifest, as completion leaves them."""
    from app.models.provider_work import ProviderWork

    request = HistoricalBackfillRequest(
        request_id=uuid7(),
        request_key=f"{provider}-{uuid7().hex}",
        provider=provider,
        dataset_key=SCOPES[provider],
        market="TW" if provider == "finlab" else "US",
        start_date=day,
        end_date=day,
        status=status,
        created_by="owner",
    )
    item = HistoricalBackfillItem(
        item_id=uuid7(),
        request_id=request.request_id,
        trade_date=day,
        status=status,
        result_evidence={
            "version": 2,
            "schema_id": "market_eod",
            "schema_version": schema_version,
            "trade_date": day.isoformat(),
            "item_id": None,
            "request_id": str(request.request_id),
            "members": members,
        },
    )
    db.add_all([request, item])
    await db.flush()
    db.add(
        ProviderWork(
            work_id=uuid7(),
            environment=get_settings().APP_ENVIRONMENT,
            provider=provider,
            stable_key=f"historical:{item.item_id}",
            kind="historical",
            dataset_key=SCOPES[provider],
            trade_date=day,
            scope={},
            manifest={
                "mode": "canonical",
                "contract": {"schema_id": "market_eod", "schema_version": 1},
                "members": [
                    {"key": key, "source_symbol": symbol, "mapping_status": "mapped"}
                    for key, symbol in frozen.items()
                ],
            },
            state="completed",
        )
    )
    await db.flush()
    return item


@pytest.mark.asyncio
async def test_historical_no_data_outranks_daily_blocked_but_never_a_mapping_gap(
    test_session, monkeypatch
):
    _, body, release = await setup(test_session, monkeypatch)
    plan = await _plan(test_session, "finlab", release.release_id, FIRST, body.members[:3])
    members = {
        member.member_key: member
        for member in await test_session.scalars(
            select(DailyDeliveryMember).where(DailyDeliveryMember.plan_id == plan.plan_id)
        )
    }
    # The daily left 0001 blocked by a provider error; 0003 is the release's gap.
    members["0001"].outcome, members["0001"].outcome_reason = "blocked", "source_error"
    members["0003"].outcome, members["0003"].outcome_reason = "blocked", "mapping_gap"
    raw = {"ref": RAW, "sha256": SHA}
    proof = [
        {"member_key": "0001", "outcome": "no_data", "raw": raw},
        {"member_key": "0002", "outcome": "missing", "reason": "no_source_row"},
        {"member_key": "0003", "outcome": "no_data", "raw": raw},
    ]
    symbols = {key: member.provider_symbol for key, member in members.items()}
    await _historical_item(
        test_session, "finlab", FIRST, status="completed", members=proof, frozen=symbols
    )
    await refresh_plan_summary(test_session, plan.plan_id)

    async def counts():
        summary = await test_session.get(
            DailyDeliveryPlanSummary, plan.plan_id, populate_existing=True
        )
        return (
            summary.data_count,
            summary.no_data_count,
            summary.missing_count,
            summary.blocked_count,
            summary.unavailable_count,
        )

    # 0001 becomes no-data; a missing series stays missing; the gap stays unavailable.
    assert await counts() == (0, 1, 1, 0, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["failed_item", "other_symbol", "other_contract", "no_raw", "other_date"]
)
async def test_historical_no_data_requires_the_same_frozen_member_and_a_settled_item(
    test_session, monkeypatch, case
):
    _, body, release = await setup(test_session, monkeypatch)
    plan = await _plan(test_session, "finlab", release.release_id, FIRST, body.members[:2])
    symbols = {member.symbol: member.provider_symbol for member in body.members[:2]}
    row = {"member_key": "0001", "outcome": "no_data", "raw": {"ref": RAW, "sha256": SHA}}
    if case == "no_raw":
        # Stored evidence without its raw observation never proves no-data.
        row = {"member_key": "0001", "outcome": "no_data"}
    await _historical_item(
        test_session,
        "finlab",
        SECOND if case == "other_date" else FIRST,
        status="failed" if case == "failed_item" else "partial",
        members=[row, {"member_key": "0002", "outcome": "data"}],
        frozen={**symbols, "0001": "OTHER"} if case == "other_symbol" else symbols,
        schema_version=2 if case == "other_contract" else 1,
    )
    await refresh_plan_summary(test_session, plan.plan_id)
    summary = await test_session.get(DailyDeliveryPlanSummary, plan.plan_id, populate_existing=True)
    assert (summary.no_data_count, summary.missing_count) == (0, 2)


@pytest.mark.asyncio
async def test_a_request_without_a_cohort_can_no_longer_complete(client, test_session, monkeypatch):
    headers, _, _ = await setup(test_session, monkeypatch)
    request, _ = await historical_backfill.create_request(
        test_session,
        provider="finlab",
        dataset_key="tw_equity_eod",
        start_date=FIRST,
        end_date=FIRST,
        request_key="finlab-legacy",
        created_by="owner",
    )
    # As before cohorts existed: no universe snapshot. Those requests have all drained.
    request.universe_snapshot = None
    await test_session.commit()
    owner, work_headers, item = await _claim(client, headers)
    assert item["universe_snapshot"] is None
    response = await client.post(
        f"/api/v1/source/historical-backfills/{item['item_id']}/complete",
        headers=work_headers,
        json={
            "status": "partial",
            "lease_token": owner["token"],
            "result_evidence": {
                "version": 2,
                "schema_id": "market_eod",
                "schema_version": 1,
                "trade_date": FIRST.isoformat(),
                "members": [{"member_key": "0001", "outcome": "data"}],
            },
        },
    )
    assert response.status_code == 422 and "partial" in response.text
    response = await client.post(
        f"/api/v1/source/historical-backfills/{item['item_id']}/complete",
        headers=work_headers,
        json={"status": "completed", "lease_token": owner["token"]},
    )
    assert response.status_code == 422 and "member or report evidence" in response.text


def _us(body: dict) -> dict:
    value = deepcopy(body)
    value.update(dataset_key="us_equity_eod", source="twelve_data")
    value["delivery"]["slot_id"] = "western_markets_window"
    value["payload"]["data"][0]["currency"] = "USD"
    return value


@pytest.mark.asyncio
async def test_later_release_member_is_requested_and_recorded_but_never_expected(
    client, test_session, test_engine, monkeypatch
):
    """Twelve Data delivers no unlisted rows, yet a later list's member is requested."""
    headers, body, release = await setup(test_session, monkeypatch, "twelve_data")
    changed = deepcopy(body)
    late = changed.members[0].model_copy(update={"symbol": "0005", "provider_symbol": "0005"})
    changed.members = [*changed.members, late]
    changed.effective_date = SECOND
    await _publish(test_session, changed, first=False)
    request, _ = await historical_backfill.create_request(
        test_session,
        provider="twelve_data",
        dataset_key="us_equity_eod",
        start_date=FIRST,
        end_date=FIRST,
        request_key="twelve-data-supplement",
        created_by="owner",
    )
    request_id = request.request_id
    await test_session.commit()
    owner, work_headers, item = await _claim(client, headers)
    snapshot = item["universe_snapshot"]
    assert [m["member_key"] for m in snapshot["members"]] == ["0001", "0002"]
    assert [m["member_key"] for m in snapshot["supplementary"]] == ["0005"]
    manifest = {
        "mode": "canonical",
        "contract": {"schema_id": "market_eod", "schema_version": 1},
        "universe_release_id": snapshot["release_id"],
        "members": [
            {
                "key": m["member_key"],
                "source_symbol": m["provider_symbol"],
                "mapping_status": m["mapping_status"],
            }
            for m in snapshot["members"]
        ],
    }
    fence = {"generation": owner["generation"], "token": owner["token"]}
    path = f"/api/v1/source/provider-work/{owner['work_id']}/manifest"
    # The manifest must carry exactly the frozen supplement.
    missing = await client.post(path, headers=headers, json={**fence, "manifest": manifest})
    assert missing.status_code == 409, missing.text
    manifest["supplementary"] = [{"key": "0005", "source_symbol": "0005"}]
    frozen = await client.post(path, headers=headers, json={**fence, "manifest": manifest})
    assert frozen.status_code == 200, frozen.text

    def post(value):
        return client.post(
            "/api/v1/source/ingest",
            headers={**work_headers, "X-FinDB-Work-Member": value["idempotency_key"]},
            json=value,
        )

    members = {m["member_key"]: m for m in (*snapshot["members"], *snapshot["supplementary"])}
    # A member-scoped feed still refuses a symbol in no frozen list.
    stray = {"member_key": "0009", "symbol": "0009", "provider_symbol": "0009"}
    assert (await post(_us(_body(item, stray, FIRST.isoformat())))).status_code == 409
    for key in ("0001", "0005"):
        accepted = await post(_us(_body(item, members[key], FIRST.isoformat())))
        assert accepted.status_code == 202, accepted.text
        await _execute_run(test_session, test_engine, UUID(accepted.json()["run_id"]))
    evidence = {
        "version": 2,
        "schema_id": "market_eod",
        "schema_version": 1,
        "trade_date": FIRST.isoformat(),
        "item_id": item["item_id"],
        "request_id": str(request_id),
        "members": [
            {"member_key": "0001", "outcome": "data"},
            {"member_key": "0002", "outcome": "missing"},
        ],
    }
    complete = await client.post(
        f"/api/v1/source/historical-backfills/{item['item_id']}/complete",
        headers=work_headers,
        json={"status": "partial", "lease_token": owner["token"], "result_evidence": evidence},
    )
    assert complete.status_code == 200, complete.text
    stored = await test_session.get(
        HistoricalBackfillItem, UUID(item["item_id"]), populate_existing=True
    )
    assert (stored.status, stored.unlisted_symbols) == ("partial", ["0005"])


@pytest.mark.asyncio
async def test_a_dates_catalogue_supplement_is_requested_beside_its_cohort(
    test_session, monkeypatch
):
    from app.schemas.full_market import UniverseMemberInput

    _, body, _ = await setup(test_session, monkeypatch, "twelve_data")
    observed = deepcopy(body)
    observed.supplementary = [
        UniverseMemberInput(
            symbol="CAT1",
            provider_symbol="CAT1",
            exchange="XNAS",
            currency="USD",
            asset_class="equity",
            classification="ordinary",
        )
    ]
    await submit_universe(test_session, observed, allowed_datasets=["us_equity_eod"])
    await test_session.commit()
    request, _ = await historical_backfill.create_request(
        test_session,
        provider="twelve_data",
        dataset_key="us_equity_eod",
        start_date=FIRST,
        end_date=FIRST,
        request_key="catalogue-supplement",
        created_by="owner",
    )
    binding = request.universe_snapshot["dates"][FIRST.isoformat()]
    assert binding["catalogue_supplement_id"] and binding["supplement_count"] == 1
    item = await test_session.scalar(
        select(HistoricalBackfillItem).where(
            HistoricalBackfillItem.request_id == request.request_id
        )
    )
    await test_session.refresh(item, ["request"])
    cohort = await historical_backfill.frozen_cohort(test_session, item)
    assert [row["member_key"] for row in cohort["supplementary"]] == ["CAT1"]
    assert cohort["supplementary"][0]["mapping_status"] == "mapped"
    assert "CAT1" not in {row["member_key"] for row in cohort["members"]}
