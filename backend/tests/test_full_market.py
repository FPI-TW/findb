"""Governed whole-market delivery, canonical reconciliation and rollout gates."""

from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.models.canonical import (
    CalendarRevisionDay,
    CalendarYearRevision,
    FuturesContractEOD,
    Instrument,
)
from app.models.registry import DailyDeliveryMember, DatasetRegistry, IngestionRun, UniverseMember
from app.schemas.full_market import (
    DeliveryOutcomeRequest,
    DeliveryPlanCreateRequest,
    UniversePublishRequest,
    UniverseSubmitRequest,
)
from app.schemas.ingress import FuturesEODRow
from app.services.full_market import (
    FullMarketError,
    create_plan,
    get_plan,
    list_universes,
    publish_universe,
    record_outcome,
    submit_universe,
)
from app.services.normalize.contracts import FuturesEODContractNormalizer
from app.services.source_clients import create_source_client
from app.utils import utc_now
from scripts.seed_data import DATASETS

DAY = date(2026, 7, 21)
EVIDENCE = {"url": "https://www.taifex.com.tw/cht/3/futDailyMarketReport", "sha256": "a" * 64}


async def _dataset(db, key="tw_futures_eod"):
    values = deepcopy(next(item for item in DATASETS if item["dataset_key"] == key))
    dataset = DatasetRegistry(**values)
    db.add(dataset)
    await db.commit()
    return dataset


async def _calendar(db, market="TAIFEX", open_dates=None):
    open_dates = set(open_dates or [DAY])
    revision = CalendarYearRevision(
        market=market,
        year=2026,
        revision=1,
        status="published",
        expected_days=365,
        actual_days=365,
        timezone="America/New_York"
        if market == "US"
        else "Asia/Hong_Kong"
        if market == "HK"
        else "Asia/Taipei",
        source_kind="full_year",
    )
    db.add(revision)
    await db.flush()
    for offset in range(365):
        day = date(2026, 1, 1) + timedelta(days=offset)
        db.add(
            CalendarRevisionDay(
                calendar_revision_id=revision.id,
                trade_date=day,
                day_status="open" if day in open_dates else "closed",
                is_open=day in open_dates,
                source_kind="full_year",
            )
        )
    await db.commit()


def _universe(key="tw_futures_eod", count=2, effective_date=DAY):
    futures = key == "tw_futures_eod"
    provider = (
        "taifex"
        if futures
        else "twelve_data"
        if key in {"hk_equity_eod", "us_equity_eod"}
        else "shioaji"
        if key.endswith("minute")
        else "finlab"
    )
    members = (
        [
            {
                "symbol": "TX",
                "provider_symbol": f"TX{202608 + i}",
                "exchange": "TAIFEX",
                "currency": "TWD",
                "asset_class": "future",
                "product_code": "TX",
                "contract_code": f"TX:{202608 + i}",
                "contract_month": str(202608 + i),
                "sessions": ["regular", "after_hours"],
            }
            for i in range(count)
        ]
        if futures
        else [
            {
                "symbol": f"{i + 1:05d}" if key == "hk_equity_eod" else f"{i + 1:04d}",
                "provider_symbol": f"{i + 1:05d}" if key == "hk_equity_eod" else f"{i + 1:04d}",
                "exchange": "HKEX" if key == "hk_equity_eod" else "TWSE",
                "currency": "HKD" if key == "hk_equity_eod" else "TWD",
                "asset_class": "etf" if key == "tw_etf_eod" else "equity",
            }
            for i in range(count)
        ]
    )
    return UniverseSubmitRequest(
        version=1,
        dataset_key=key,
        provider=provider,
        effective_date=effective_date,
        observed_at=utc_now(),
        source_timezone="Asia/Taipei",
        evidence=[EVIDENCE],
        members=members,
    )


async def _baseline(db, key="tw_futures_eod", count=2):
    body = _universe(key, count)
    release = await submit_universe(db, body, allowed_datasets=[key])
    await publish_universe(
        db,
        release.release_id,
        UniversePublishRequest(
            evidence_note="Verified official first baseline.", first_baseline_approved=True
        ),
        actor="test-owner",
    )
    await db.commit()
    return body, release


async def _activated_plan(db, key="tw_futures_eod", count=2, open_dates=None):
    dataset = await _dataset(db, key)
    await _calendar(
        db,
        "TAIFEX" if key == "tw_futures_eod" else dataset.market,
        open_dates,
    )
    body, release = await _baseline(db, key, count)
    from tests.full_market_fixtures import historical_admission

    revision = await db.scalar(
        select(CalendarYearRevision).where(
            CalendarYearRevision.market == ("TAIFEX" if key == "tw_futures_eod" else dataset.market)
        )
    )
    await historical_admission(db, dataset, release, DAY, revision.id)
    plan = await create_plan(
        db,
        DeliveryPlanCreateRequest(
            version=1,
            dataset_key=key,
            provider=body.provider,
            trade_date=DAY,
            release_id=release.release_id,
        ),
        allowed_datasets=[key],
    )
    return dataset, body, plan


def _quote(month="202608", session="regular", trade_date=DAY):
    return {
        "product_code": "TX",
        "contract_code": f"TX:{month}",
        "contract_month": month,
        "session": session,
        "trade_date": trade_date.isoformat(),
        "open": "100",
        "high": "110",
        "low": "90",
        "close": "105",
        "volume": 5,
    }


def _ingress(plan, data):
    return {
        "dataset_key": plan.dataset_key,
        "schema_id": "futures_eod",
        "schema_version": 1,
        "source": "taifex",
        "request_key": "full-market-test",
        "idempotency_key": "full-market-test",
        "fetched_at": utc_now().isoformat(),
        "delivery": {
            "slot_id": "taiwan_market_window",
            "scheduled_for": utc_now().isoformat(),
            "target_data_date": plan.trade_date.isoformat(),
            "work_item_id": plan.parts[0].work_item_id,
        },
        "payload": {
            "batch": {
                "data_date": plan.trade_date.isoformat(),
                "delivery_mode": "incremental",
                "declared_record_count": len(data),
            },
            "data": data,
        },
    }


async def _source_headers(db, provider="taifex", datasets=None):
    _, key = await create_source_client(
        db,
        name=f"full-market-source-{provider}",
        owner="tests",
        source_name=provider,
        allowed_datasets=datasets or ["tw_futures_eod"],
        rate_limit_requests=100,
        rate_limit_window=60,
        commit=False,
    )
    return {"X-API-Key": key}


@pytest.mark.parametrize(
    "change",
    [
        {"open": None, "high": None, "low": None, "close": None, "volume": 0},
        {"open": None, "high": None, "low": None, "close": None, "volume": None},
        {"close": -1},
        {"volume": -1},
        {"high": "99"},
        {"contract_code": "TX:continuous"},
        {"contract_month": "202608W6"},
        {"session": "night"},
        {"settlement_price": -1},
        {"open_interest": -1},
    ],
)
def test_futures_rejects_invalid_identity_prices_or_empty_observation(change):
    with pytest.raises(ValidationError):
        FuturesEODRow.model_validate({**_quote(), **change})


def test_futures_weekly_and_null_optional_fields_are_preserved():
    row = FuturesEODRow.model_validate(_quote("202608W2", "after_hours"))
    assert row.settlement_price is None and row.open_interest is None
    assert row.contract_month == "202608W2" and row.trade_date == DAY


@pytest.mark.parametrize("product", ["TX", "MTX", "TMF", "TE", "TF"])
@pytest.mark.parametrize(
    "observation",
    [{"open": "100"}, {"settlement_price": "0"}, {"open_interest": 0}, {"volume": 1}],
)
def test_futures_nullable_prices_require_a_provided_observation(product, observation):
    quote = {
        **_quote(),
        "product_code": product,
        "contract_code": f"{product}:202608",
        "open": None,
        "high": None,
        "low": None,
        "close": None,
        "volume": None,
        **observation,
    }
    row = FuturesEODRow.model_validate(quote)
    assert row.close is None
    for field, value in observation.items():
        assert getattr(row, field) == (int(value) if isinstance(value, str) else value)


@pytest.mark.asyncio
async def test_nullable_futures_ingest_serve_and_rerun_preserve_session_metrics(
    client, test_session
):
    dataset, _, plan = await _activated_plan(test_session, count=1)
    headers = await _source_headers(test_session)
    regular = {
        **_quote(),
        "open": None,
        "high": None,
        "low": None,
        "close": None,
        "volume": 0,
        "settlement_price": "104.5",
        "open_interest": 0,
    }
    after_hours = {
        **_quote(session="after_hours"),
        "open": "103",
        "high": "106",
        "low": None,
        "close": None,
        "volume": None,
    }
    request = _ingress(plan, [regular, after_hours])
    response = await client.post("/api/v1/source/ingest", headers=headers, json=request)
    assert response.status_code == 202, response.text
    run_id = UUID(response.json()["run_id"])
    duplicate = await client.post("/api/v1/source/ingest", headers=headers, json=request)
    assert duplicate.json()["run_id"] == str(run_id)
    normalizer = FuturesEODContractNormalizer(
        test_session,
        {**dataset.config, "_ingest_source": "taifex", "_ingest_fetched_at": request["fetched_at"]},
    )
    assert (await normalizer.process(request["payload"], run_id)).success_records == 2
    summary = (
        await get_plan(
            test_session, plan.plan_id, provider="taifex", allowed_datasets=[dataset.dataset_key]
        )
    ).summary
    assert (summary.data, summary.no_data, summary.missing) == (2, 0, 0)
    rerun = await client.post(f"/api/v1/source/runs/{run_id}/rerun", headers=headers)
    assert rerun.status_code == 202, rerun.text
    rerun_id = UUID(rerun.json()["run_id"])
    assert (await normalizer.process(request["payload"], rerun_id)).success_records == 2
    quotes = (await test_session.scalars(select(FuturesContractEOD))).all()
    assert len(quotes) == 2 and len({quote.contract_id for quote in quotes}) == 1
    assert {quote.session for quote in quotes} == {"regular", "after_hours"}
    served = await client.get("/api/v1/serve/futures/eod", params={"contract_code": "TX:202608"})
    assert served.status_code == 200, served.text
    by_session = {row["session"]: row for row in served.json()["data"]}
    for field in ("open", "high", "low", "close"):
        assert by_session["regular"][field] is None
    assert Decimal(by_session["regular"]["settlement_price"]) == Decimal("104.5")
    assert by_session["regular"]["open_interest"] == 0
    assert by_session["regular"]["volume"] == 0
    assert Decimal(by_session["after_hours"]["open"]) == Decimal("103")
    assert Decimal(by_session["after_hours"]["high"]) == Decimal("106")
    for field in ("low", "close", "volume", "settlement_price", "open_interest"):
        assert by_session["after_hours"][field] is None
    filtered = await client.get(
        "/api/v1/serve/futures/eod", params={"contract_code": "TX:202608", "session": "regular"}
    )
    assert [row["session"] for row in filtered.json()["data"]] == ["regular"]
    # Partial OHLC remains subject to bounds; invalid direct normalization cannot write it.
    invalid = deepcopy(request["payload"])
    invalid["data"] = [{**after_hours, "high": "102"}]
    invalid["batch"]["declared_record_count"] = 1
    result = await normalizer.process(invalid, rerun_id)
    assert result.failed_records == 1 and result.success_records == 0
    assert result.dq_issues[0].severity == "error"
    assert await test_session.scalar(select(func.count()).select_from(FuturesContractEOD)) == 2


@pytest.mark.asyncio
async def test_universe_threshold_retains_previous_and_counts_mapping_changes(test_session):
    await _dataset(test_session, "hk_equity_eod")
    body, baseline = await _baseline(test_session, "hk_equity_eod", 1000)
    identical = await submit_universe(test_session, body, allowed_datasets=[body.dataset_key])
    assert identical.release_id == baseline.release_id
    replacement = body.model_copy(deep=True)
    replacement.effective_date = DAY + timedelta(days=1)
    replacement.members = replacement.members[:-20]
    accepted = await submit_universe(test_session, replacement, allowed_datasets=[body.dataset_key])
    assert accepted.status == "published" and accepted.change_count == 20
    replacement.effective_date += timedelta(days=1)
    for member in replacement.members[:21]:
        member.mapping_status = "gap"
    candidate = await submit_universe(
        test_session, replacement, allowed_datasets=[body.dataset_key]
    )
    assert candidate.status == "candidate" and candidate.change_count == 21
    with pytest.raises(FullMarketError, match="exception"):
        await publish_universe(
            test_session,
            candidate.release_id,
            UniversePublishRequest(evidence_note="Anomalous change reviewed by owner."),
            actor="test-owner",
        )


@pytest.mark.asyncio
async def test_daily_provenance_refresh_is_immutable_but_not_material(test_session):
    key = "hk_equity_eod"
    await _dataset(test_session, key)
    body, baseline = await _baseline(test_session, key, 100)
    daily = body.model_copy(deep=True)
    daily.effective_date += timedelta(days=1)
    for member in daily.members:
        member.raw_evidence_ref = "r2://official/universe/new-daily-content-sha256.json"
    refreshed = await submit_universe(test_session, daily, allowed_datasets=[key])
    assert refreshed.status == "published" and refreshed.change_count == 0
    assert refreshed.change_ratio == 0
    assert refreshed.member_sha256 != baseline.member_sha256
    previous = (
        await test_session.scalars(
            select(UniverseMember).where(UniverseMember.release_id == baseline.release_id)
        )
    ).all()
    current = (
        await test_session.scalars(
            select(UniverseMember).where(UniverseMember.release_id == refreshed.release_id)
        )
    ).all()
    assert all(member.raw_evidence_ref is None for member in previous)
    assert all(member.raw_evidence_ref == daily.members[0].raw_evidence_ref for member in current)
    duplicate = await submit_universe(test_session, daily, allowed_datasets=[key])
    assert duplicate.release_id == refreshed.release_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("classification", "ordinary_share"),
        ("provider_symbol", "changed-source-symbol"),
        ("mapping_status", "gap"),
        ("currency", "USD"),
        ("exchange", "XHKG"),
    ],
)
async def test_member_metadata_changes_still_trigger_ratio_threshold(test_session, field, value):
    key = "hk_equity_eod"
    await _dataset(test_session, key)
    body, _ = await _baseline(test_session, key, 100)
    body.effective_date += timedelta(days=1)
    for member in body.members[:3]:
        setattr(member, field, value)
    changed = await submit_universe(test_session, body, allowed_datasets=[key])
    assert changed.status == "candidate" and changed.change_count == 3
    assert changed.change_ratio == pytest.approx(0.03)


@pytest.mark.asyncio
async def test_new_listings_count_toward_absolute_threshold(test_session):
    key = "hk_equity_eod"
    await _dataset(test_session, key)
    _, baseline = await _baseline(test_session, key, 1000)
    body = _universe(key, 1020, DAY + timedelta(days=1))
    accepted = await submit_universe(test_session, body, allowed_datasets=[key])
    assert accepted.status == "published" and accepted.change_count == 20
    body = _universe(key, 1041, DAY + timedelta(days=2))
    held = await submit_universe(test_session, body, allowed_datasets=[key])
    assert held.status == "candidate" and held.change_count == 21
    assert held.release_id != baseline.release_id


@pytest.mark.asyncio
async def test_source_plan_list_exact_date_retains_scope_and_existing_order(client, test_session):
    key = "tw_futures_eod"
    days = [DAY, DAY + timedelta(days=1)]
    _, body, first = await _activated_plan(test_session, count=1, open_dates=days)
    last = await create_plan(
        test_session,
        DeliveryPlanCreateRequest(
            version=1,
            dataset_key=key,
            provider=body.provider,
            trade_date=days[1],
            release_id=first.release_id,
        ),
        allowed_datasets=[key],
    )
    params = {
        "dataset_key": key,
        "start_date": DAY.isoformat(),
        "end_date": DAY.isoformat(),
        "limit": 1,
    }
    assert (await client.get("/api/v1/source/delivery-plans", params=params)).status_code == 401
    headers = await _source_headers(test_session)
    response = await client.get("/api/v1/source/delivery-plans", params=params, headers=headers)
    assert response.status_code == 200, response.text
    assert [item["plan_id"] for item in response.json()["data"]] == [str(first.plan_id)]
    params.pop("end_date")
    response = await client.get("/api/v1/source/delivery-plans", params=params, headers=headers)
    assert [item["plan_id"] for item in response.json()["data"]] == [str(last.plan_id)]
    params["dataset_key"] = "tw_equity_eod"
    assert (
        await client.get("/api/v1/source/delivery-plans", params=params, headers=headers)
    ).status_code == 403
    wrong_provider = await _source_headers(test_session, "finlab", ["tw_equity_eod"])
    params["dataset_key"] = key
    response = await client.get(
        "/api/v1/source/delivery-plans", params=params, headers=wrong_provider
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_source_routes_auth_provider_scope_and_inactive_feed(
    client, test_session, admin_headers
):
    await _dataset(test_session)
    body = _universe()
    assert (
        await client.post("/api/v1/source/universes", json=body.model_dump(mode="json"))
    ).status_code == 401
    headers = await _source_headers(test_session, "finlab", ["tw_equity_eod"])
    assert (
        await client.post(
            "/api/v1/source/universes", headers=headers, json=body.model_dump(mode="json")
        )
    ).status_code == 403
    headers = await _source_headers(test_session)
    response = await client.post(
        "/api/v1/source/universes", headers=headers, json=body.model_dump(mode="json")
    )
    assert response.status_code == 200 and response.json()["status"] == "candidate"
    release_id = response.json()["release_id"]
    response = await client.post(
        f"/api/v1/admin/universes/{release_id}/publish",
        headers=admin_headers,
        json={
            "evidence_note": "First baseline official evidence checked.",
            "first_baseline_approved": True,
        },
    )
    assert response.status_code == 200
    response = await client.post(
        "/api/v1/source/delivery-plans",
        headers=headers,
        json={
            "version": 1,
            "dataset_key": body.dataset_key,
            "provider": "taifex",
            "trade_date": DAY.isoformat(),
            "release_id": release_id,
        },
    )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_ingest_links_part_canonical_incremental_and_idempotent_rerun(client, test_session):
    dataset, _, plan = await _activated_plan(test_session)
    headers = await _source_headers(test_session)
    request = _ingress(plan, [_quote()])
    response = await client.post("/api/v1/source/ingest", headers=headers, json=request)
    assert response.status_code == 202, response.text
    run_id = UUID(response.json()["run_id"])
    run = await test_session.get(IngestionRun, run_id)
    assert run.delivery_part_id == plan.parts[0].part_id
    duplicate = await client.post("/api/v1/source/ingest", headers=headers, json=request)
    assert duplicate.status_code == 202 and duplicate.json()["run_id"] == str(run_id)
    normalizer = FuturesEODContractNormalizer(
        test_session,
        {**dataset.config, "_ingest_source": "taifex", "_ingest_fetched_at": request["fetched_at"]},
    )
    result = await normalizer.process(request["payload"], run_id)
    assert result.success_records == 1
    summary = (
        await get_plan(
            test_session, plan.plan_id, provider="taifex", allowed_datasets=[dataset.dataset_key]
        )
    ).summary
    assert (summary.expected, summary.data, summary.missing) == (4, 1, 3)
    await normalizer.process(request["payload"], run_id)
    assert await test_session.scalar(select(func.count()).select_from(FuturesContractEOD)) == 1
    assert (await test_session.scalars(select(Instrument))).one().symbol == "TX"
    rerun = await client.post(f"/api/v1/source/runs/{run_id}/rerun", headers=headers)
    assert rerun.status_code == 202, rerun.text
    rerun_id = UUID(rerun.json()["run_id"])
    assert (await test_session.get(IngestionRun, rerun_id)).delivery_part_id == plan.parts[
        0
    ].part_id
    await normalizer.process(request["payload"], rerun_id)
    assert await test_session.scalar(select(func.count()).select_from(FuturesContractEOD)) == 1
    changed = deepcopy(request)
    changed["payload"]["data"][0]["contract_code"] = "TX:202610"
    changed["payload"]["data"][0]["contract_month"] = "202610"
    assert (
        await client.post("/api/v1/source/ingest", headers=headers, json=changed)
    ).status_code == 409


@pytest.mark.asyncio
async def test_outcomes_retain_failure_audit_and_original_no_data_time(test_session):
    dataset, _, plan = await _activated_plan(test_session)
    part = plan.parts[0]
    key = part.member_keys[0]
    member = (
        await test_session.scalars(
            select(DailyDeliveryMember).where(DailyDeliveryMember.member_key == key)
        )
    ).one()
    blocked = DeliveryOutcomeRequest(
        version=1,
        work_item_id=part.work_item_id,
        member_key=key,
        outcome="blocked",
        reason="source_error",
    )
    await record_outcome(
        test_session,
        plan.plan_id,
        blocked,
        provider="taifex",
        allowed_datasets=[dataset.dataset_key],
    )
    no_data = DeliveryOutcomeRequest(
        version=1,
        work_item_id=part.work_item_id,
        member_key=key,
        outcome="no_data",
        reason="no_trade",
        evidence={
            **EVIDENCE,
            "observed_at": utc_now(),
            "source_symbol": member.provider_symbol,
            "trade_date": DAY,
            "session": member.session,
            "source_status": "no_trade",
            "source_excerpt": "Official session report: no trades for this contract.",
        },
    )
    summary = await record_outcome(
        test_session,
        plan.plan_id,
        no_data,
        provider="taifex",
        allowed_datasets=[dataset.dataset_key],
    )
    original_time = member.outcome_at
    assert summary.no_data == 1 and len(member.outcome_history) == 2
    await record_outcome(
        test_session,
        plan.plan_id,
        no_data,
        provider="taifex",
        allowed_datasets=[dataset.dataset_key],
    )
    assert member.outcome_at == original_time and len(member.outcome_history) == 2
    with pytest.raises(FullMarketError, match="immutable"):
        await record_outcome(
            test_session,
            plan.plan_id,
            blocked,
            provider="taifex",
            allowed_datasets=[dataset.dataset_key],
        )


@pytest.mark.asyncio
async def test_full_scale_frozen_plan_and_pre_activation_boundary(test_session):
    dataset, body, plan = await _activated_plan(test_session, "hk_equity_eod", 12000)
    assert plan.summary.expected == 12000 and len(plan.parts) == 240
    assert sum(len(part.member_keys) for part in plan.parts) == 12000
    assert plan.parts[0].member_keys[0] == "00001"
    assert plan.deadline_at.hour == 15
    repeated = await create_plan(
        test_session,
        DeliveryPlanCreateRequest(
            version=1,
            dataset_key=dataset.dataset_key,
            provider=body.provider,
            trade_date=DAY,
            release_id=plan.release_id,
        ),
        allowed_datasets=[dataset.dataset_key],
    )
    assert repeated.plan_id == plan.plan_id
    with pytest.raises(FullMarketError, match="Pre-first-start"):
        await create_plan(
            test_session,
            DeliveryPlanCreateRequest(
                version=1,
                dataset_key=dataset.dataset_key,
                provider=body.provider,
                trade_date=DAY - timedelta(days=1),
                release_id=plan.release_id,
            ),
            allowed_datasets=[dataset.dataset_key],
        )


@pytest.mark.asyncio
async def test_as_of_uses_published_baseline_even_after_fifty_candidates(test_session):
    await _dataset(test_session, "hk_equity_eod")
    body, baseline = await _baseline(test_session, "hk_equity_eod", count=1)
    for number in range(51):
        candidate = body.model_copy(deep=True)
        candidate.effective_date = DAY + timedelta(days=number + 1)
        candidate.members[0].provider_symbol = f"provider-{number}"
        await submit_universe(test_session, candidate, allowed_datasets=[body.dataset_key])
    current = await list_universes(
        test_session,
        dataset_key=body.dataset_key,
        provider=body.provider,
        allowed_datasets=[body.dataset_key],
    )
    assert len(current.data) == 50
    assert current.published_release_id == baseline.release_id
    historical = await list_universes(
        test_session,
        dataset_key=body.dataset_key,
        provider=body.provider,
        allowed_datasets=[body.dataset_key],
        as_of=DAY,
    )
    assert historical.published_release_id == baseline.release_id and len(historical.data) == 1
