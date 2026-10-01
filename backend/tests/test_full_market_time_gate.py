"""Actual exchange dates and published closing-time rollout gates."""

from datetime import datetime, time, timedelta
from unittest.mock import patch
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.models.canonical import CalendarRevisionDay, FuturesContract, FuturesContractEOD
from app.models.raw import RawMarketPayload
from app.models.registry import DailyDeliveryMember, IngestionRun
from app.schemas.full_market import (
    DeliveryOutcomeRequest,
    DeliveryPlanCreateRequest,
    FeedActivateRequest,
    UniversePublishRequest,
    UniverseSubmitRequest,
)
from app.schemas.ingress import FuturesEODIngressRequest, FuturesEODRow, minute_sequence_key_digest
from app.services import full_market
from app.services.full_market import (
    FullMarketError,
    _require_closed_acceptance_day,
    activate_feed,
    create_plan,
    deactivate_feed,
    get_plan,
    publish_universe,
    record_outcome,
    submit_universe,
)
from app.services.normalize.contracts import FuturesEODContractNormalizer
from tests.test_full_market import (
    DAY,
    EVIDENCE,
    _activated_plan,
    _calendar,
    _dataset,
    _ingress,
    _quote,
    _source_headers,
    _universe,
)
from tests.test_minute_normalize import _minute_request


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "calendar", "zone", "close"),
    [
        ("us_equity_eod", "US", "America/New_York", time(16)),
        ("hk_equity_eod", "HK", "Asia/Hong_Kong", time(16, 10)),
        ("tw_etf_eod", "TW", "Asia/Taipei", time(13, 30)),
        ("tw_futures_eod", "TAIFEX", "Asia/Taipei", time(13, 45)),
    ],
)
async def test_same_day_gate_uses_exchange_close_and_safe_missing_hour_policy(
    test_session, key, calendar, zone, close
):
    dataset = await _dataset(test_session, key)
    await _calendar(test_session, calendar)
    closed_at = datetime.combine(DAY, close, ZoneInfo(zone))
    with pytest.raises(FullMarketError, match="has not closed"):
        await _require_closed_acceptance_day(
            test_session, dataset, DAY, now=closed_at - timedelta(seconds=1)
        )
    await _require_closed_acceptance_day(test_session, dataset, DAY, now=closed_at)
    assert (
        dataset.config["delivery_expectation"]["latest_date"]["market_close_time"]
        == close.isoformat()
    )


@pytest.mark.asyncio
async def test_published_early_close_overrides_regular_market_policy(test_session):
    dataset = await _dataset(test_session, "us_equity_eod")
    await _calendar(test_session, "US")
    day = (
        await test_session.scalars(
            select(CalendarRevisionDay).where(CalendarRevisionDay.trade_date == DAY)
        )
    ).one()
    day.session_close = time(13)
    await test_session.flush()
    await _require_closed_acceptance_day(
        test_session,
        dataset,
        DAY,
        now=datetime.combine(DAY, time(13), ZoneInfo("America/New_York")),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_by", ["future", "preclose"])
async def test_five_planned_days_cannot_promote_before_actual_exchange_close(
    test_session, monkeypatch, blocked_by
):
    days = [DAY + timedelta(days=offset) for offset in range(5)]
    dataset, body, first = await _activated_plan(test_session, count=1, open_dates=days)
    for day in days[1:]:
        await create_plan(
            test_session,
            DeliveryPlanCreateRequest(
                version=1,
                dataset_key=dataset.dataset_key,
                provider=body.provider,
                trade_date=day,
                release_id=first.release_id,
            ),
            allowed_datasets=[dataset.dataset_key],
        )
    # Control the real server clock; no fabricated canonical completion evidence.
    monkeypatch.setattr(
        full_market,
        "utc_now",
        lambda: datetime.combine(
            days[-2] if blocked_by == "future" else days[-1],
            time(23) if blocked_by == "future" else time(13, 44, 59),
            ZoneInfo("Asia/Taipei"),
        ),
    )
    with pytest.raises(
        FullMarketError, match="actual elapsed" if blocked_by == "future" else "has not closed"
    ):
        await activate_feed(
            test_session,
            dataset.dataset_key,
            FeedActivateRequest(
                activation_date=DAY,
                mode="active",
                readiness_evidence_note="Planned dates cannot count as actual elapsed days.",
            ),
        )
    assert dataset.config["full_market"]["mode"] == "acceptance"


@pytest.mark.parametrize("month", ["202608W0", "202608F6", "202613", "202608/202609", "CONT"])
def test_universe_and_ingress_share_actual_month_and_week_identity_validation(month):
    universe = _universe(count=1).model_dump(mode="json")
    universe["members"][0].update(contract_month=month, contract_code=f"TX:{month}")
    with pytest.raises(ValidationError):
        UniverseSubmitRequest.model_validate(universe)
    with pytest.raises(ValidationError):
        FuturesEODRow.model_validate(_quote(month))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "exchange", "currency"),
    [
        ("hk_equity_eod", "XHKG", "HKD"),
        ("us_equity_eod", "XNAS", "USD"),
        ("us_equity_eod", "XNYS", "USD"),
        ("us_equity_eod", "XASE", "USD"),
        ("us_equity_eod", "ARCX", "USD"),
        ("us_equity_eod", "BATS", "USD"),
        ("us_equity_eod", "IEXG", "USD"),
        ("tw_equity_eod", "XTAI", "TWD"),
        ("tw_etf_eod", "ROCO", "TWD"),
        ("tw_futures_eod", "XTAF", "TWD"),
    ],
)
async def test_official_mic_exchange_members_are_accepted(test_session, key, exchange, currency):
    dataset = await _dataset(test_session, key)
    body = _universe(key, count=1)
    body.members[0].exchange = exchange
    body.members[0].currency = currency
    result = await submit_universe(test_session, body, allowed_datasets=[dataset.dataset_key])
    assert result.member_count == (2 if key == "tw_futures_eod" else 1)
    if key == "hk_equity_eod":
        assert body.members[0].symbol == "00001" and body.members[0].currency == "HKD"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "market", "zone"),
    [
        ("us_equity_eod", "US", "America/New_York"),
        ("hk_equity_eod", "HK", "Asia/Hong_Kong"),
        ("tw_etf_eod", "TW", "Asia/Taipei"),
        ("tw_futures_eod", "TAIFEX", "Asia/Taipei"),
    ],
)
@pytest.mark.parametrize("activation_offset", [0, 1])
async def test_first_activation_cannot_backdate_market_local_day(
    test_session, monkeypatch, key, market, zone, activation_offset
):
    await _dataset(test_session, key)
    days = [DAY + timedelta(days=offset) for offset in (-3, -2, -1, 0, 1)]
    await _calendar(test_session, market, days)
    body = _universe(key, count=1, effective_date=days[0])
    release = await submit_universe(test_session, body, allowed_datasets=[key])
    await publish_universe(
        test_session,
        release.release_id,
        UniversePublishRequest(
            first_baseline_approved=True, evidence_note="Official applicable baseline reviewed."
        ),
        actor="test-owner",
    )
    # At Taipei 08:30 it is still the previous date in New York.
    clock = datetime.combine(DAY, time(8, 30), ZoneInfo("Asia/Taipei"))
    monkeypatch.setattr(full_market, "utc_now", lambda: clock)
    local_day = clock.astimezone(ZoneInfo(zone)).date()
    with pytest.raises(FullMarketError, match="First activation date cannot precede"):
        await activate_feed(
            test_session,
            key,
            FeedActivateRequest(
                activation_date=local_day - timedelta(days=1),
                readiness_evidence_note="Reject historical activation.",
            ),
        )
    activated = await activate_feed(
        test_session,
        key,
        FeedActivateRequest(
            activation_date=local_day + timedelta(days=activation_offset),
            readiness_evidence_note="Actual local activation date reviewed.",
        ),
    )
    assert (
        activated["activation_date"] == (local_day + timedelta(days=activation_offset)).isoformat()
    )


@pytest.mark.asyncio
async def test_resume_preserves_original_activation_and_existing_gaps(test_session, monkeypatch):
    dataset, body, plan = await _activated_plan(test_session, count=1)
    await deactivate_feed(test_session, dataset.dataset_key)
    monkeypatch.setattr(
        full_market,
        "utc_now",
        lambda: datetime.combine(DAY + timedelta(days=8), time(23), ZoneInfo("Asia/Taipei")),
    )
    with pytest.raises(FullMarketError, match="Original activation date"):
        await activate_feed(
            test_session,
            dataset.dataset_key,
            FeedActivateRequest(
                activation_date=DAY + timedelta(days=8),
                readiness_evidence_note="Cannot reset original cutoff.",
            ),
        )
    await activate_feed(
        test_session,
        dataset.dataset_key,
        FeedActivateRequest(activation_date=DAY, readiness_evidence_note="Resume original gaps."),
    )
    existing = await get_plan(
        test_session, plan.plan_id, provider=body.provider, allowed_datasets=[dataset.dataset_key]
    )
    assert existing.plan_id == plan.plan_id and existing.summary.missing == 2
    with pytest.raises(FullMarketError, match="Original activation date"):
        await activate_feed(
            test_session,
            dataset.dataset_key,
            FeedActivateRequest(
                activation_date=DAY + timedelta(days=1),
                mode="active",
                readiness_evidence_note="Cannot move cutoff on promotion.",
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["trade_date", "fetched_at"])
async def test_actual_full_market_ingress_rejects_future_date_or_fetch_time(
    client, test_session, monkeypatch, invalid
):
    future = DAY + timedelta(days=1)
    dataset, body, first = await _activated_plan(test_session, count=1, open_dates=[DAY, future])
    target = future if invalid == "trade_date" else DAY
    plan = await create_plan(
        test_session,
        DeliveryPlanCreateRequest(
            version=1,
            dataset_key=dataset.dataset_key,
            provider=body.provider,
            trade_date=target,
            release_id=first.release_id,
        ),
        allowed_datasets=[dataset.dataset_key],
    )
    clock = datetime.combine(DAY, time(15), ZoneInfo("Asia/Taipei"))
    monkeypatch.setattr(full_market, "utc_now", lambda: clock)
    request = _ingress(plan, [_quote(trade_date=target)])
    request["fetched_at"] = (
        clock + timedelta(minutes=6 if invalid == "fetched_at" else 0)
    ).isoformat()
    headers = await _source_headers(test_session)
    response = await client.post("/api/v1/source/ingest", headers=headers, json=request)
    assert response.status_code == 409, response.text
    assert ("future exchange" if invalid == "trade_date" else "clock skew") in response.text
    assert await test_session.scalar(select(func.count()).select_from(RawMarketPayload)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["tw_etf_eod", "tw_equity_minute"])
@pytest.mark.parametrize("preclose_clock", ["server", "fetched_at"])
async def test_governed_market_ingress_requires_both_clocks_after_close(
    client, test_session, monkeypatch, key, preclose_clock
):
    _, body, plan = await _activated_plan(test_session, key, count=1)
    close = datetime.combine(DAY, time(13, 30), ZoneInfo("Asia/Taipei"))
    clock = close - timedelta(seconds=1) if preclose_clock == "server" else close
    fetched_at = close - timedelta(seconds=1) if preclose_clock == "fetched_at" else close
    monkeypatch.setattr(full_market, "utc_now", lambda: clock)
    request = _ingress(plan, [])
    if key.endswith("minute"):
        request = _minute_request(key, symbol="0001", source_symbol="0001")
        request["delivery"] = _ingress(plan, [])["delivery"]
        for field in ("data_date", "coverage_start_date", "coverage_end_date"):
            request["payload"]["batch"][field] = DAY.isoformat()
        for field in ("trade_date", "bar_start_time", "bar_end_time", "signal_time"):
            row = request["payload"]["data"][0]
            row[field] = row[field].replace("2026-07-30", DAY.isoformat())
        digest = minute_sequence_key_digest(
            dataset_key=key,
            data_date=DAY,
            snapshot_id=request["payload"]["batch"]["snapshot_id"],
            sequence=1,
        )
        request.update(request_key=f"mmr:{digest}", idempotency_key=f"mms:{digest}")
    else:
        request.update(dataset_key=key, schema_id="market_eod", source=body.provider)
        request["payload"]["batch"]["declared_record_count"] = 1
        request["payload"]["data"] = [
            {
                "symbol": "0001",
                "source_symbol": "0001",
                "trade_date": DAY.isoformat(),
                "currency": "TWD",
                "close": "100",
                "volume": 5,
            }
        ]
    request["fetched_at"] = fetched_at.isoformat()
    headers = await _source_headers(test_session, body.provider, [key])
    response = await client.post("/api/v1/source/ingest", headers=headers, json=request)
    assert response.status_code == 409 and "has not closed" in response.text, response.text
    assert await test_session.scalar(select(func.count()).select_from(RawMarketPayload)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session", "at", "accepted"),
    [
        ("after_hours", time(4, 59, 59), False),
        ("after_hours", time(5), True),
        ("regular", time(13, 44, 59), False),
        ("regular", time(13, 45), True),
    ],
)
async def test_taifex_source_respects_actual_session_cutoff(
    client, test_session, monkeypatch, session, at, accepted
):
    _, _, plan = await _activated_plan(test_session, count=1)
    clock = datetime.combine(DAY, at, ZoneInfo("Asia/Taipei"))
    monkeypatch.setattr(full_market, "utc_now", lambda: clock)
    request = _ingress(plan, [_quote(session=session)])
    request["fetched_at"] = clock.isoformat()
    headers = await _source_headers(test_session)
    response = await client.post("/api/v1/source/ingest", headers=headers, json=request)
    assert response.status_code == (202 if accepted else 409), response.text
    if not accepted:
        assert "has not closed" in response.text
        assert await test_session.scalar(select(func.count()).select_from(RawMarketPayload)) == 0


@pytest.mark.asyncio
async def test_friday_expiry_preserves_product_through_universe_source_and_canonical(
    client, test_session
):
    dataset = await _dataset(test_session)
    await _calendar(test_session)
    body = _universe(count=1)
    member = body.members[0]
    member.contract_month, member.contract_code, member.provider_symbol = (
        "202608F2",
        "TX:202608F2",
        "TXU202608F2",
    )
    release = await submit_universe(test_session, body, allowed_datasets=[dataset.dataset_key])
    await publish_universe(
        test_session,
        release.release_id,
        UniversePublishRequest(
            first_baseline_approved=True, evidence_note="Official Friday expiry baseline reviewed."
        ),
        actor="test-owner",
    )
    with patch(
        "app.services.full_market.utc_now",
        return_value=datetime.combine(DAY, time(15), ZoneInfo("Asia/Taipei")),
    ):
        await activate_feed(
            test_session,
            dataset.dataset_key,
            FeedActivateRequest(
                activation_date=DAY,
                readiness_evidence_note="Official Friday contracts readiness reviewed.",
            ),
        )
    await test_session.commit()
    plan = await create_plan(
        test_session,
        DeliveryPlanCreateRequest(
            version=1,
            dataset_key=dataset.dataset_key,
            provider="taifex",
            trade_date=DAY,
            release_id=release.release_id,
        ),
        allowed_datasets=[dataset.dataset_key],
    )
    assert "TX|TX:202608F2|regular" in plan.parts[0].member_keys
    request = _ingress(plan, [_quote("202608F2")])
    assert isinstance(
        FuturesEODIngressRequest.model_validate(request).payload.data[0], FuturesEODRow
    )
    headers = await _source_headers(test_session)
    response = await client.post("/api/v1/source/ingest", headers=headers, json=request)
    assert response.status_code == 202, response.text
    run_id = UUID(response.json()["run_id"])
    assert (await test_session.get(IngestionRun, run_id)).delivery_part_id == plan.parts[0].part_id
    normalizer = FuturesEODContractNormalizer(
        test_session, {**dataset.config, "_ingest_source": "taifex"}
    )
    assert (await normalizer.process(request["payload"], run_id)).success_records == 1
    contract = (await test_session.scalars(select(FuturesContract))).one()
    assert contract.contract_code == "TX:202608F2" and contract.contract_month == "202608F2"
    assert await test_session.scalar(select(func.count()).select_from(FuturesContractEOD)) == 1


async def _no_data_claim(db, plan, session, observed_at):
    member = (
        await db.scalars(
            select(DailyDeliveryMember).where(
                DailyDeliveryMember.plan_id == plan.plan_id,
                DailyDeliveryMember.session == session,
            )
        )
    ).one()
    return DeliveryOutcomeRequest(
        version=1,
        work_item_id=plan.parts[0].work_item_id,
        member_key=member.member_key,
        outcome="no_data",
        reason="no_trade",
        evidence={
            **EVIDENCE,
            "observed_at": observed_at,
            "source_symbol": member.provider_symbol,
            "trade_date": plan.trade_date,
            "session": session,
            "source_status": "no_trade",
            "source_excerpt": "Official final report confirms no trades for this date/session.",
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "session", "zone", "close"),
    [
        ("us_equity_eod", None, "America/New_York", time(16)),
        ("hk_equity_eod", None, "Asia/Hong_Kong", time(16, 10)),
        ("tw_etf_eod", None, "Asia/Taipei", time(13, 30)),
        ("tw_equity_minute", None, "Asia/Taipei", time(13, 30)),
        ("tw_futures_eod", "regular", "Asia/Taipei", time(13, 45)),
        ("tw_futures_eod", "after_hours", "Asia/Taipei", time(5)),
    ],
)
@pytest.mark.parametrize("preclose_clock", ["server", "observed_at"])
async def test_final_no_data_requires_both_clocks_at_actual_close(
    test_session, monkeypatch, key, session, zone, close, preclose_clock
):
    _, body, plan = await _activated_plan(test_session, key, count=1)
    closed_at = datetime.combine(DAY, close, ZoneInfo(zone))
    clock = closed_at - timedelta(seconds=1) if preclose_clock == "server" else closed_at
    observed_at = closed_at - timedelta(seconds=1) if preclose_clock == "observed_at" else closed_at
    monkeypatch.setattr(full_market, "utc_now", lambda: clock)
    claim = await _no_data_claim(test_session, plan, session, observed_at)
    # Nonfinal failures remain recordable even before the market closes.
    blocked = DeliveryOutcomeRequest(
        version=1,
        work_item_id=claim.work_item_id,
        member_key=claim.member_key,
        outcome="blocked",
        reason="source_error",
    )
    summary = await record_outcome(
        test_session, plan.plan_id, blocked, provider=body.provider, allowed_datasets=[key]
    )
    assert summary.blocked == 1 and summary.no_data == 0
    with pytest.raises(FullMarketError, match="has not closed"):
        await record_outcome(
            test_session, plan.plan_id, claim, provider=body.provider, allowed_datasets=[key]
        )
    unchanged = await get_plan(
        test_session, plan.plan_id, provider=body.provider, allowed_datasets=[key]
    )
    assert unchanged.summary.blocked == 1 and unchanged.summary.no_data == 0
    monkeypatch.setattr(full_market, "utc_now", lambda: closed_at)
    claim.evidence.observed_at = closed_at
    summary = await record_outcome(
        test_session, plan.plan_id, claim, provider=body.provider, allowed_datasets=[key]
    )
    assert summary.no_data == 1 and summary.blocked == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("session", ["regular", "after_hours"])
async def test_no_data_rejects_future_trade_day_but_allows_plan_and_blocked(
    test_session, monkeypatch, session
):
    tomorrow = DAY + timedelta(days=1)
    _, body, first = await _activated_plan(test_session, count=1, open_dates=[DAY, tomorrow])
    plan = await create_plan(
        test_session,
        DeliveryPlanCreateRequest(
            version=1,
            dataset_key=first.dataset_key,
            provider=body.provider,
            trade_date=tomorrow,
            release_id=first.release_id,
        ),
        allowed_datasets=[first.dataset_key],
    )
    clock = datetime.combine(DAY, time(23), ZoneInfo("Asia/Taipei"))
    monkeypatch.setattr(full_market, "utc_now", lambda: clock)
    claim = await _no_data_claim(test_session, plan, session, clock)
    with pytest.raises(FullMarketError, match="future exchange trading date"):
        await record_outcome(
            test_session,
            plan.plan_id,
            claim,
            provider=body.provider,
            allowed_datasets=[plan.dataset_key],
        )
    blocked = DeliveryOutcomeRequest(
        version=1,
        work_item_id=claim.work_item_id,
        member_key=claim.member_key,
        outcome="blocked",
        reason="source_error",
    )
    summary = await record_outcome(
        test_session,
        plan.plan_id,
        blocked,
        provider=body.provider,
        allowed_datasets=[plan.dataset_key],
    )
    assert summary.blocked == 1 and summary.no_data == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("skew_seconds", [300, 301])
async def test_no_data_observed_clock_skew_exact_boundary(test_session, monkeypatch, skew_seconds):
    _, body, plan = await _activated_plan(test_session, count=1)
    clock = datetime.combine(DAY, time(15), ZoneInfo("Asia/Taipei"))
    monkeypatch.setattr(full_market, "utc_now", lambda: clock)
    claim = await _no_data_claim(
        test_session, plan, "regular", clock + timedelta(seconds=skew_seconds)
    )
    if skew_seconds == 301:
        with pytest.raises(FullMarketError, match="observed_at exceeds allowed clock skew"):
            await record_outcome(
                test_session,
                plan.plan_id,
                claim,
                provider=body.provider,
                allowed_datasets=[plan.dataset_key],
            )
    else:
        summary = await record_outcome(
            test_session,
            plan.plan_id,
            claim,
            provider=body.provider,
            allowed_datasets=[plan.dataset_key],
        )
        assert summary.no_data == 1


@pytest.mark.asyncio
async def test_no_data_respects_published_early_close(test_session, monkeypatch):
    key = "us_equity_eod"
    _, body, plan = await _activated_plan(test_session, key, count=1)
    day = (
        await test_session.scalars(
            select(CalendarRevisionDay).where(CalendarRevisionDay.trade_date == DAY)
        )
    ).one()
    day.session_close = time(13)
    await test_session.flush()
    clock = datetime.combine(DAY, time(13), ZoneInfo("America/New_York"))
    monkeypatch.setattr(full_market, "utc_now", lambda: clock)
    claim = await _no_data_claim(test_session, plan, None, clock)
    summary = await record_outcome(
        test_session, plan.plan_id, claim, provider=body.provider, allowed_datasets=[key]
    )
    assert summary.no_data == 1
