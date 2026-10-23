"""Expected HK sessions count toward escalation; proved no-data resets a member."""

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.registry import DatasetRegistry, MissingDeliveryAlert, UniverseRelease
from app.schemas.full_market import DeliveryOutcomeRequest, DeliveryPlanCreateRequest
from app.services.delivery_monitor import _scan_full_market_feed
from app.services.full_market import create_plan, record_outcome
from app.utils import uuid7
from tests.full_market_fixtures import historical_admission
from tests.test_factset_historical import setup
from tests.test_full_market import _calendar


@pytest.mark.asyncio
async def test_five_expected_days_exclude_weekend_and_no_data_resets_streak(
    test_session, monkeypatch
):
    await setup(test_session, monkeypatch)
    days = [date(2026, 10, day) for day in (20, 21, 22, 23, 26)]
    now = datetime(2026, 10, 26, 16, tzinfo=timezone.utc)
    monkeypatch.setattr("app.services.full_market.utc_now", lambda: now)
    monkeypatch.setattr("app.services.full_market_admission.utc_now", lambda: now)
    await _calendar(test_session, "HK", open_dates=days)
    dataset = await test_session.get(DatasetRegistry, "hk_equity_eod")
    release = await test_session.scalar(
        select(UniverseRelease).where(UniverseRelease.provider == "factset")
    )
    await historical_admission(test_session, dataset, release, days[0], None)
    plans = []
    for day in days:
        plans.append(
            await create_plan(
                test_session,
                DeliveryPlanCreateRequest(
                    version=1,
                    provider="factset",
                    dataset_key=dataset.dataset_key,
                    trade_date=day,
                    release_id=release.release_id,
                ),
                allowed_datasets=[dataset.dataset_key],
            )
        )
    reset_plan = plans[2]
    await record_outcome(
        test_session,
        reset_plan.plan_id,
        DeliveryOutcomeRequest(
            version=1,
            work_item_id=reset_plan.parts[0].work_item_id,
            member_key="00002",
            outcome="no_data",
            reason="no_trade",
            evidence={
                "url": "https://example.org/official/no-trade",
                "sha256": "a" * 64,
                "observed_at": now,
                "source_symbol": "BBBBBB-L",
                "trade_date": days[2],
                "source_status": "no_trade",
                "source_excerpt": "Official no-trade test fixture.",
            },
        ),
        provider="factset",
        allowed_datasets=[dataset.dataset_key],
    )
    await _scan_full_market_feed(
        test_session,
        dataset=dataset,
        source="factset",
        schema_id="market_eod",
        schema_version=2,
        evaluated_at=now,
    )
    alerts = list(
        await test_session.scalars(
            select(MissingDeliveryAlert).order_by(MissingDeliveryAlert.expected_data_date)
        )
    )
    assert [row.expected_data_date for row in alerts] == days
    assert alerts[-2].details["severity"] == "warning"
    assert alerts[-1].details["severity"] == "error"
    assert alerts[-1].details["long_gap_members"] == [
        {"member_key": "00001", "consecutive_trading_days": 5}
    ]
    monkeypatch.setattr(get_settings(), "EOD_LONG_GAP_TRADING_DAYS", 6)
    await _scan_full_market_feed(
        test_session,
        dataset=dataset,
        source="factset",
        schema_id="market_eod",
        schema_version=2,
        evaluated_at=now,
    )
    await test_session.refresh(alerts[-1])
    assert alerts[-1].details["severity"] == "warning"


@pytest.mark.asyncio
async def test_cutover_retires_twelve_data_hk_gap_alerts(test_session, monkeypatch):
    await setup(test_session, monkeypatch)
    now = datetime(2026, 10, 23, 16, tzinfo=timezone.utc)
    dataset = await test_session.get(DatasetRegistry, "hk_equity_eod")
    release = await test_session.scalar(
        select(UniverseRelease).where(UniverseRelease.provider == "factset")
    )
    await historical_admission(test_session, dataset, release, date(2026, 10, 20), None)

    def alert(dataset_key, profile, day, status):
        return MissingDeliveryAlert(
            alert_id=uuid7(),
            dataset_key=dataset_key,
            source="twelve_data",
            schema_id="market_eod",
            schema_version=1,
            execution_profile=profile,
            expected_data_date=day,
            status=status,
            first_detected_at=now,
            last_detected_at=now,
            details={"code": "FULL_MARKET_INCOMPLETE", "plan_id": None},
        )

    retired = alert("hk_equity_eod", "full_market", date(2026, 10, 15), "open")
    already = alert("hk_equity_eod", "full_market", date(2026, 10, 14), "resolved")
    other_profile = alert("hk_equity_eod", "pilot", date(2026, 10, 15), "open")
    other_dataset = alert("us_equity_eod", "full_market", date(2026, 10, 15), "open")
    test_session.add_all([retired, already, other_profile, other_dataset])
    await test_session.flush()

    async def scan():
        return await _scan_full_market_feed(
            test_session,
            dataset=dataset,
            source="twelve_data",
            schema_id="market_eod",
            schema_version=1,
            evaluated_at=now,
        )

    assert await scan() == (0, 1)
    # Later scans neither reopen nor recreate retired-provider gaps.
    assert await scan() == (0, 0)
    for row in (retired, already, other_profile, other_dataset):
        await test_session.refresh(row)
    assert retired.status == "resolved" and retired.resolved_at == now
    assert retired.details == {
        "code": "FULL_MARKET_INCOMPLETE",
        "plan_id": None,
        "resolution": {
            "code": "PROVIDER_RETIRED",
            "provider": "factset",
            "effective_date": dataset.config["hk_provider_cutover"]["effective_date"],
        },
    }
    assert "resolution" not in already.details
    assert other_profile.status == other_dataset.status == "open"
