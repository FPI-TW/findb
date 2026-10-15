"""TAIFEX exact-date new plans retain calendar skips and legacy frozen plans."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.models.registry import DailyDeliveryPlan, DailyExecutionDecision, UniverseRelease
from app.schemas.full_market import DeliveryPlanCreateRequest
from app.services.full_market import FullMarketError, create_plan
from tests.full_market_fixtures import historical_admission
from tests.test_full_market import DAY, _baseline, _dataset


@pytest.mark.parametrize("profile", ["pilot", "full_market"])
@pytest.mark.parametrize("skip", [False, True])
async def test_stale_taifex_release_requires_date_only_after_calendar_decision(
    test_session, monkeypatch, profile, skip
):
    dataset = await _dataset(test_session)
    _, release = await _baseline(test_session)
    await historical_admission(test_session, dataset, release, DAY, None, profile=profile)
    # Sunday with no published calendar uses weekday default.
    day = DAY + timedelta(days=5)
    assert day.weekday() == 6
    monkeypatch.setattr(get_settings(), "TAIFEX_SKIP_NON_TRADING_DAYS", skip)
    body = DeliveryPlanCreateRequest(
        version=1,
        dataset_key=dataset.dataset_key,
        provider="taifex",
        execution_profile=profile,
        trade_date=day,
        release_id=release.release_id,
    )
    if skip:
        response = await create_plan(test_session, body, allowed_datasets=[dataset.dataset_key])
        assert response.status == "skipped_calendar"
        monkeypatch.setattr(get_settings(), "TAIFEX_SKIP_NON_TRADING_DAYS", False)
        assert (
            await create_plan(test_session, body, allowed_datasets=[dataset.dataset_key])
        ).status == "skipped_calendar"
    else:
        with pytest.raises(FullMarketError) as error:
            await create_plan(test_session, body, allowed_datasets=[dataset.dataset_key])
        assert error.value.code == "taifex_dated_universe_required"
    assert await test_session.scalar(select(func.count()).select_from(DailyDeliveryPlan)) == 0
    assert await test_session.scalar(
        select(func.count()).select_from(DailyExecutionDecision)
    ) == int(skip)


@pytest.mark.parametrize("profile", ["pilot", "full_market"])
async def test_existing_taifex_plan_remains_readable_with_legacy_older_release(
    test_session, profile
):
    dataset = await _dataset(test_session)
    _, release = await _baseline(test_session)
    await historical_admission(test_session, dataset, release, DAY, None, profile=profile)
    body = DeliveryPlanCreateRequest(
        version=1,
        dataset_key=dataset.dataset_key,
        provider="taifex",
        execution_profile=profile,
        trade_date=DAY,
        release_id=release.release_id,
    )
    frozen = await create_plan(test_session, body, allowed_datasets=[dataset.dataset_key])
    saved = await test_session.get(UniverseRelease, release.release_id)
    saved.effective_date = DAY - timedelta(days=1)
    await test_session.commit()
    assert await create_plan(test_session, body, allowed_datasets=[dataset.dataset_key]) == frozen


async def test_source_exposes_only_static_dated_error_code(client, test_session):
    from tests.test_full_market import _activated_plan, _source_headers

    _, _, first = await _activated_plan(test_session)
    headers = await _source_headers(test_session)
    body = {
        "version": 1,
        "provider": "taifex",
        "dataset_key": "tw_futures_eod",
        "trade_date": str(DAY + timedelta(days=1)),
        "release_id": str(first.release_id),
    }
    response = await client.post("/api/v1/source/delivery-plans", headers=headers, json=body)
    assert response.status_code == 409
    assert response.json() == {"detail": {"code": "taifex_dated_universe_required"}}
    assert (await client.post("/api/v1/source/delivery-plans", json=body)).status_code == 401
