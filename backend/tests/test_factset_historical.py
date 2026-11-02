"""Request-time current cohort and partial dates through authenticated Source."""

from copy import deepcopy
from datetime import date, datetime, timezone
from uuid import UUID

import pytest

from app.config import get_settings
from app.models.registry import DatasetRegistry, HistoricalBackfillRequest
from app.schemas.full_market import UniversePublishRequest
from app.services import historical_backfill, hk_provider_cutover
from app.services.full_market import publish_universe, submit_universe
from app.services.source_clients import create_source_client
from scripts.seed_data import seed_datasets
from tests.test_canonical_ingest_api import _execute_run
from tests.test_full_market import _universe
from tests.test_provider_work import attest_cutover

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


async def setup(db, monkeypatch, members=None):
    monkeypatch.setattr(get_settings(), "PROVIDER_WORK_REQUIRED", True)
    monkeypatch.setattr("app.services.provider_work.utc_now", lambda: NOW)
    monkeypatch.setattr("app.services.full_market.utc_now", lambda: NOW)
    monkeypatch.setattr(historical_backfill, "utc_now", lambda: NOW)
    monkeypatch.setattr(hk_provider_cutover, "utc_now", lambda: NOW)
    await seed_datasets(db)
    await attest_cutover(db)
    body = _universe("hk_equity_eod", 2, date(2026, 10, 20))
    body.provider = "factset"
    for index, member in enumerate(body.members):
        member.provider_symbol = ("AAAAAA-L", "BBBBBB-L")[index]
        member.exchange = "XHKG"
        member.classification = "hk_main_ordinary"
    if members is not None:
        member_type = type(body.members[0])
        body.members = [member_type(**member) for member in members]
    release = await submit_universe(db, body, allowed_datasets=["hk_equity_eod"])
    await publish_universe(
        db,
        release.release_id,
        UniversePublishRequest(
            evidence_note="Verified official first baseline.", first_baseline_approved=True
        ),
        actor="test-owner",
    )
    await hk_provider_cutover.apply_cutover(db, effective_date=NOW.date(), actor="test-owner")
    _, key = await create_source_client(
        db,
        name="FactSet",
        source_name="factset",
        allowed_datasets=["hk_equity_eod"],
        rate_limit_requests=100000,
        rate_limit_window=60,
        commit=False,
    )
    await db.commit()
    return {"X-API-Key": key}, body


@pytest.mark.asyncio
async def test_request_cohort_stays_frozen_and_partial_day_continues(
    client, test_session, test_engine, monkeypatch
):
    headers, universe = await setup(test_session, monkeypatch)
    request, _ = await historical_backfill.create_request(
        test_session,
        provider="factset",
        dataset_key="hk_equity_eod",
        start_date=date(2026, 10, 15),
        end_date=date(2026, 10, 16),
        request_key="factset-current-cohort",
        created_by="owner",
    )
    request_id = request.request_id
    frozen = deepcopy(request.universe_snapshot)
    # Historical actual writes must refresh both existing daily-profile summaries.
    from app.models.registry import DailyDeliveryMember, DailyDeliveryPart, DailyDeliveryPlan
    from app.services.delivery_plan_summary import refresh_plan_summary
    from app.utils import uuid7

    summaries = []
    for profile in ("pilot", "full_market"):
        plan = DailyDeliveryPlan(
            plan_id=uuid7(),
            dataset_key="hk_equity_eod",
            provider="factset",
            trade_date=date(2026, 10, 16),
            release_id=UUID(frozen["release_id"]),
            execution_profile=profile,
            activation_cutoff=date(2026, 10, 15),
            deadline_at=NOW,
            member_sha256="a" * 64,
            expected_count=2,
        )
        test_session.add(plan)
        await test_session.flush()
        part = DailyDeliveryPart(
            part_id=uuid7(),
            plan_id=plan.plan_id,
            part_number=1,
            work_item_id="fp1:" + uuid7().hex,
            member_sha256="a" * 64,
            expected_count=2,
        )
        test_session.add(part)
        await test_session.flush()
        for member in frozen["members"]:
            test_session.add(
                DailyDeliveryMember(
                    member_id=uuid7(),
                    plan_id=plan.plan_id,
                    part_id=part.part_id,
                    member_key=member["member_key"],
                    symbol=member["symbol"],
                    provider_symbol=member["provider_symbol"],
                    mapping_status="mapped",
                )
            )
        await refresh_plan_summary(test_session, plan.plan_id)
        summaries.append(plan.plan_id)
    await test_session.commit()
    # Daily refresh removes one member. The existing historical request retains both.
    universe.members = universe.members[:1]
    newer = await submit_universe(test_session, universe, allowed_datasets=["hk_equity_eod"])
    await publish_universe(
        test_session,
        newer.release_id,
        UniversePublishRequest(
            evidence_note="Verified changed current list.",
            threshold_exception_approved=True,
        ),
        actor="test-owner",
    )
    await test_session.commit()
    for day in ("2026-10-15", "2026-10-16"):
        claimed = await client.post(
            "/api/v1/source/provider-work/claim",
            headers=headers,
            json={
                "protocol": "provider-work-v1",
                "host_lock_identity": "local:1:22",
                "recover": True,
            },
        )
        assert claimed.status_code == 200, claimed.text
        owner = claimed.json()["work"]
        assert owner["trade_date"] == day
        work_headers = {
            **headers,
            "X-FinDB-Work-Id": owner["work_id"],
            "X-FinDB-Work-Generation": str(owner["generation"]),
            "X-FinDB-Work-Token": owner["token"],
        }
        lease = await client.post("/api/v1/source/historical-backfills/claim", headers=work_headers)
        assert lease.status_code == 200, lease.text
        item = lease.json()
        assert item["universe_snapshot"] == frozen
        manifest = {
            "mode": "canonical",
            "contract": {"schema_id": "market_eod", "schema_version": 2},
            "universe_release_id": frozen["release_id"],
            "members": [
                {
                    "key": m["member_key"],
                    "source_symbol": m["provider_symbol"],
                    "mapping_status": m["mapping_status"],
                }
                for m in frozen["members"]
            ],
        }
        result = await client.post(
            f"/api/v1/source/provider-work/{owner['work_id']}/manifest",
            headers=headers,
            json={"generation": owner["generation"], "token": owner["token"], "manifest": manifest},
        )
        assert result.status_code == 200, result.text
        rows = [
            {
                "symbol": m["symbol"],
                "source_symbol": m["provider_symbol"],
                "currency": "HKD",
                "trade_date": day,
                "open": "10",
                "high": "11" if i == 0 else "1",
                "low": "9",
                "close": "10",
                "volume": 100,
            }
            for i, m in enumerate(frozen["members"])
        ]
        body = {
            "dataset_key": "hk_equity_eod",
            "source": "factset",
            "schema_id": "market_eod",
            "schema_version": 2,
            "request_key": "history:" + item["item_id"],
            "idempotency_key": "history:" + item["item_id"],
            "fetched_at": NOW.isoformat(),
            "delivery": {
                "slot_id": "asia_pacific_markets_window",
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
                    "declared_record_count": 2,
                },
                "data": rows,
            },
        }
        accepted = await client.post(
            "/api/v1/source/ingest",
            headers={**work_headers, "X-FinDB-Work-Member": body["idempotency_key"]},
            json=body,
        )
        assert accepted.status_code == 202, accepted.text
        await _execute_run(test_session, test_engine, UUID(accepted.json()["run_id"]))
        evidence = {
            "version": 2,
            "schema_id": "market_eod",
            "schema_version": 2,
            "trade_date": day,
            "item_id": item["item_id"],
            "request_id": str(request_id),
            "members": [
                {"member_key": rows[0]["symbol"], "outcome": "data"},
                {"member_key": rows[1]["symbol"], "outcome": "invalid"},
            ],
        }
        complete = await client.post(
            f"/api/v1/source/historical-backfills/{item['item_id']}/complete",
            headers=work_headers,
            json={"status": "partial", "lease_token": owner["token"], "result_evidence": evidence},
        )
        assert complete.status_code == 200, complete.text
        replay = await client.post(
            f"/api/v1/source/historical-backfills/{item['item_id']}/complete",
            headers=work_headers,
            json={"status": "partial", "lease_token": owner["token"], "result_evidence": evidence},
        )
        assert replay.status_code == 200 and replay.json() == complete.json(), replay.text
        changed = deepcopy(evidence)
        changed["members"][1]["outcome"] = "no_data"
        rejected = await client.post(
            f"/api/v1/source/historical-backfills/{item['item_id']}/complete",
            headers=work_headers,
            json={"status": "partial", "lease_token": owner["token"], "result_evidence": changed},
        )
        assert rejected.status_code == 409
    from app.models.registry import DailyDeliveryPlanSummary

    for plan_id in summaries:
        stored = await test_session.get(DailyDeliveryPlanSummary, plan_id)
        assert (stored.data_count, stored.missing_count) == (1, 1)
    await test_session.refresh(request)
    assert request.status == "partial" and request.universe_snapshot == frozen
    loaded = await test_session.get(HistoricalBackfillRequest, request_id)
    assert loaded.completed_at is not None
    from app.models.registry import DailyDeliveryMember, DailyDeliveryPlan
    from app.services.full_market import _factset_historical_repairs

    plan = DailyDeliveryPlan(
        dataset_key="hk_equity_eod", provider="factset", trade_date=date(2026, 10, 16)
    )
    daily_members = [
        DailyDeliveryMember(
            member_key=m["member_key"],
            provider_symbol=m["provider_symbol"],
            mapping_status="mapped",
        )
        for m in frozen["members"]
    ]
    assert await _factset_historical_repairs(test_session, plan, daily_members) == {"00001"}
    daily_members[0].provider_symbol = "CCCCCC-L"
    assert await _factset_historical_repairs(test_session, plan, daily_members) == set()
    daily_members[0].provider_symbol = "AAAAAA-L"
    daily_members[0].mapping_status = "gap"
    assert await _factset_historical_repairs(test_session, plan, daily_members) == set()
    daily_members[0].mapping_status = "mapped"
    plan.trade_date = date(2026, 10, 17)
    assert await _factset_historical_repairs(test_session, plan, daily_members) == set()


def _hk_dataset(cutover=None):
    config = {} if cutover is None else {"hk_provider_cutover": cutover}
    return DatasetRegistry(dataset_key="hk_equity_eod", config=config)


@pytest.mark.parametrize(
    "cutover",
    [
        None,
        {
            "provider": "factset",
            "previous_provider": "twelve_data",
            "effective_date": "2026-10-20",
        },
        {
            "provider": "factset",
            "previous_provider": "twelve_data",
            "effective_date": "2026-10-21",
        },
    ],
)
@pytest.mark.parametrize("maintenance", [False, True])
def test_twelve_data_hk_is_retired_regardless_of_cutover(monkeypatch, cutover, maintenance):
    monkeypatch.setattr(hk_provider_cutover, "utc_now", lambda: NOW)
    assert not hk_provider_cutover.acquisition_allowed(
        _hk_dataset(cutover), "twelve_data", maintenance=maintenance
    )


@pytest.mark.parametrize("cutover", [None, {"provider": "factset"}])
def test_hk_gate_does_not_restrict_future_sources_to_factset(cutover):
    assert hk_provider_cutover.acquisition_allowed(_hk_dataset(cutover), "future_hk_source")


def test_factset_gate_still_requires_cutover_except_for_maintenance(monkeypatch):
    monkeypatch.setattr(hk_provider_cutover, "utc_now", lambda: NOW)
    assert not hk_provider_cutover.acquisition_allowed(_hk_dataset(), "factset")
    assert hk_provider_cutover.acquisition_allowed(_hk_dataset(), "factset", maintenance=True)
    future = {
        "provider": "factset",
        "previous_provider": "twelve_data",
        "effective_date": "2026-10-21",
    }
    assert not hk_provider_cutover.acquisition_allowed(_hk_dataset(future), "factset")


@pytest.mark.asyncio
async def test_cutover_is_explicit_and_keeps_v1_acceptance(test_session, monkeypatch):
    await setup(test_session, monkeypatch)
    dataset = await test_session.get(DatasetRegistry, "hk_equity_eod")
    assert dataset.config["accepted_schema_versions"] == [1, 2]
    assert dataset.config["allowed_sources"] == ["factset"]
    assert not hk_provider_cutover.acquisition_allowed(dataset, "twelve_data")
    assert hk_provider_cutover.acquisition_allowed(dataset, "factset")
    assert not hk_provider_cutover.acquisition_allowed(
        dataset, "factset", trade_date=date(2026, 10, 19)
    )
    before = deepcopy(dataset.config)
    await seed_datasets(test_session)
    await test_session.refresh(dataset)
    assert dataset.config["hk_provider_cutover"] == before["hk_provider_cutover"]
    assert dataset.config["source_precedence"] == {"factset": -1, "twelve_data": 0}
