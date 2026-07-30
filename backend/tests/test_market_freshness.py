"""Read-only market freshness projection and endpoint behavior."""

from datetime import date, datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.config import get_settings
from app.models.canonical import CalendarMarket, CalendarRevisionDay, CalendarYearRevision
from app.models.registry import DatasetRegistry, IngestionRun, MissingDeliveryAlert
from app.schemas.serve import MarketFreshnessSummaryResponse
from app.services.market_freshness import list_market_freshness
from app.utils import uuid7

NOW = datetime(2026, 7, 22, 8, tzinfo=timezone.utc)


def _config(sources: list[str]) -> dict:
    return {
        "schema_id": "market_eod",
        "current_schema_version": 1,
        "delivery_expectation": {
            "latest_date": {
                "calendar_market": "TW",
                "timezone": "Asia/Taipei",
                "market_close_time": "13:30:00",
                "availability_grace_minutes": 60,
                "action": "warn",
            },
            "schedule": {
                "enabled": True,
                "slot_id": "tw_1430",
                "local_time": "14:30:00",
                "timezone": "Asia/Taipei",
                "expected_sources": sources,
            },
        },
    }


async def _setup(
    session,
    sources: list[str] = ["finlab"],
    closed_calendar_days: set[date] | None = None,
) -> None:
    session.add(
        DatasetRegistry(
            dataset_key="tw_equity_eod",
            name="TW",
            asset_class="equity",
            market="TW",
            is_active=True,
            config=_config(sources),
        )
    )
    await _seed_published_calendar_year(
        session,
        market="TW",
        year=2026,
        closed_calendar_days=closed_calendar_days,
    )
    await session.commit()


async def _seed_published_calendar_year(
    session,
    *,
    market: str,
    year: int,
    closed_calendar_days: set[date] | None = None,
) -> None:
    if await session.get(CalendarMarket, market) is None:
        session.add(
            CalendarMarket(
                market=market,
                display_name=market,
                timezone="Asia/Taipei",
                weekend_days=[5, 6],
            )
        )
        await session.flush()
    start = date(year, 1, 1)
    count = (date(year, 12, 31) - start).days + 1
    closed_calendar_days = closed_calendar_days or set()
    revision = CalendarYearRevision(
        market=market,
        year=year,
        revision=1,
        status="published",
        expected_days=count,
        actual_days=count,
        timezone="Asia/Taipei",
        source_kind="test",
    )
    session.add(revision)
    await session.flush()
    session.add_all(
        [
            CalendarRevisionDay(
                calendar_revision_id=revision.id,
                trade_date=start + timedelta(days=offset),
                is_open=(start + timedelta(days=offset)).weekday() < 5
                and start + timedelta(days=offset) not in closed_calendar_days,
                day_status=(
                    "open"
                    if (start + timedelta(days=offset)).weekday() < 5
                    and start + timedelta(days=offset) not in closed_calendar_days
                    else "closed"
                ),
                source_kind="test",
            )
            for offset in range(count)
        ]
    )


def _run(source: str, data_date: date, status: str = "completed") -> IngestionRun:
    return IngestionRun(
        run_id=uuid7(),
        dataset_key="tw_equity_eod",
        source=source,
        schema_id="market_eod",
        schema_version=1,
        batch_data_date=data_date,
        delivery_mode="full_snapshot",
        is_rerun=False,
        status=status,
        completed_at=NOW if status == "completed" else None,
        failure_code="NORMALIZATION_FAILED" if status == "failed" else None,
        total_records=2,
        success_records=2 if status == "completed" else 0,
    )


@pytest.mark.asyncio
async def test_configured_market_without_runs_is_never_received(test_session):
    await _setup(test_session)
    rows = await list_market_freshness(test_session, now=NOW)
    assert len(rows) == 1
    assert rows[0].status == "never_received"
    assert rows[0].feeds[0].status == "never_received"
    assert rows[0].expected_data_date == date(2026, 7, 22)

    before_due = await list_market_freshness(
        test_session, now=datetime(2026, 7, 22, 4, tzinfo=timezone.utc)
    )
    assert before_due[0].status == "never_received"
    assert before_due[0].feeds[0].status == "never_received"


@pytest.mark.asyncio
async def test_freshness_aggregates_partial_late_failed_and_coverage(test_session):
    await _setup(test_session, ["finlab", "bloomberg"])
    test_session.add_all(
        [
            _run("finlab", date(2026, 7, 22)),
            _run("bloomberg", date(2026, 7, 21)),
        ]
    )
    await test_session.commit()
    partial = (await list_market_freshness(test_session, now=NOW))[0]
    assert partial.status == "partial"
    assert partial.coverage_data_date == date(2026, 7, 21)
    assert partial.last_complete_at is None

    test_session.add(_run("bloomberg", date(2026, 7, 22), "failed"))
    await test_session.commit()
    failed = (await list_market_freshness(test_session, now=NOW))[0]
    assert failed.status == "failed"
    assert failed.feeds[1].last_failure_code == "NORMALIZATION_FAILED"

    test_session.add(_run("bloomberg", date(2026, 7, 22)))
    await test_session.commit()
    recovered = (await list_market_freshness(test_session, now=NOW))[0]
    assert recovered.status == "fresh"
    assert recovered.last_complete_at == NOW


@pytest.mark.asyncio
async def test_not_due_and_open_alert_are_read_only(test_session):
    await _setup(
        test_session,
        closed_calendar_days={
            date(2026, 7, 22) - timedelta(days=offset) for offset in range(1, 32)
        },
    )
    test_session.add_all(
        [
            _run("finlab", date(2026, 7, 21)),
            MissingDeliveryAlert(
                dataset_key="tw_equity_eod",
                source="finlab",
                schema_id="market_eod",
                schema_version=1,
                expected_data_date=date(2026, 7, 22),
                status="open",
                first_detected_at=NOW,
                last_detected_at=NOW,
            ),
        ]
    )
    await test_session.commit()
    rows = await list_market_freshness(
        test_session, now=datetime(2026, 7, 22, 4, tzinfo=timezone.utc)
    )
    assert rows[0].status == "not_due"
    assert rows[0].feeds[0].open_missing_delivery_alert is False
    alert = (await test_session.execute(select(MissingDeliveryAlert))).scalar_one()
    assert alert.status == "open"


@pytest.mark.asyncio
async def test_admin_filters_and_serve_redacts_feed_internals(
    client: AsyncClient, test_session, admin_headers
):
    await _setup(test_session)
    unauthorized = await client.get("/api/v1/admin/market-freshness")
    assert unauthorized.status_code == 401
    admin = await client.get(
        "/api/v1/admin/market-freshness?market=TW&slot_id=tw_1430&status=never_received&include_feeds=false",
        headers=admin_headers,
    )
    assert admin.status_code == 200
    assert admin.json()["data"][0]["feeds"] == []

    settings = get_settings()
    original = settings.SERVE_REQUIRE_AUTH
    settings.SERVE_REQUIRE_AUTH = True
    try:
        assert (await client.get("/api/v1/serve/market-freshness")).status_code == 401
    finally:
        settings.SERVE_REQUIRE_AUTH = original
    served = await client.get("/api/v1/serve/market-freshness?market=TW")
    assert served.status_code == 200
    assert "feeds" not in served.json()["data"][0]
    assert "dataset_key" not in str(served.json())


def test_serve_freshness_schema_has_a_closed_status_vocabulary():
    status_schema = MarketFreshnessSummaryResponse.model_json_schema()["properties"]["status"]
    assert status_schema["enum"] == [
        "not_due",
        "fresh",
        "partial",
        "late",
        "failed",
        "never_received",
    ]
