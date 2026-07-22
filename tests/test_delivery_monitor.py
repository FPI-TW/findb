"""Missing-delivery monitor persistence and resolution behavior."""

import asyncio
from datetime import date, datetime, time, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.canonical import TradingCalendar
from app.models.registry import DatasetRegistry, IngestionRun, MissingDeliveryAlert
from app.services.delivery_monitor import scan_missing_deliveries
from app.services.delivery_policy import (
    DeliveryExpectation,
    LatestDatePolicy,
    resolve_expected_data_date,
)

NOW = datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc)


def _config(*, action: str = "warn", sources: list[str] | None = None) -> dict:
    return {
        "schema_id": "market_eod",
        "accepted_schema_versions": [1],
        "current_schema_version": 1,
        "schema_enforcement": "enforce",
        "defaults": {"market": "TW", "asset_class": "equity", "currency": "TWD"},
        "delivery_expectation": {
            "latest_date": {
                "calendar_market": "TW",
                "timezone": "Asia/Taipei",
                "market_close_time": "13:30:00",
                "availability_grace_minutes": 60,
                "action": "warn",
            },
            "missing_delivery": {
                "action": action,
                "expected_sources": sources or [],
            },
        },
    }


async def _seed_dataset(session, *, active: bool = True, config: dict | None = None) -> None:
    session.add(
        DatasetRegistry(
            dataset_key="tw_equity_eod",
            name="TW equity",
            asset_class="equity",
            market="TW",
            is_active=active,
            config=config or _config(sources=["finlab"]),
        )
    )
    session.add(
        TradingCalendar(
            market="TW",
            trade_date=date(2026, 7, 22),
            is_open=True,
            session_close=time(13, 30),
        )
    )
    await session.commit()


def test_missing_delivery_config_is_opt_in_and_validated() -> None:
    assert DeliveryExpectation.model_validate({}).missing_delivery.action == "disabled"
    parsed = DeliveryExpectation.model_validate(
        {"missing_delivery": {"action": "warn", "expected_sources": ["FinLab"]}}
    )
    assert parsed.missing_delivery.expected_sources == ["finlab"]
    with pytest.raises(ValidationError):
        DeliveryExpectation.model_validate(
            {"missing_delivery": {"action": "warn", "expected_sources": []}}
        )
    with pytest.raises(ValidationError):
        DeliveryExpectation.model_validate(
            {"missing_delivery": {"action": "reject", "expected_sources": ["finlab"]}}
        )


@pytest.mark.asyncio
async def test_public_calendar_resolver_honors_dst_and_grace(test_session) -> None:
    test_session.add(TradingCalendar(market="US", trade_date=date(2026, 3, 9), is_open=True))
    await test_session.commit()
    policy = LatestDatePolicy(
        calendar_market="US",
        timezone="America/New_York",
        market_close_time=time(16),
        availability_grace_minutes=60,
        action="warn",
    )

    before, before_reason = await resolve_expected_data_date(
        test_session,
        policy,
        datetime(2026, 3, 9, 20, 59, tzinfo=timezone.utc),
    )
    after, after_reason = await resolve_expected_data_date(
        test_session,
        policy,
        datetime(2026, 3, 9, 21, 0, tzinfo=timezone.utc),
    )
    assert before is None
    assert before_reason == "no_closed_open_session_in_calendar_window"
    assert after == date(2026, 3, 9)
    assert after_reason is None


@pytest.mark.asyncio
async def test_scan_creates_once_refreshes_and_late_delivery_resolves(test_session) -> None:
    await _seed_dataset(test_session)

    first = await scan_missing_deliveries(test_session, now=NOW)
    second = await scan_missing_deliveries(test_session, now=NOW)
    assert first.created_or_refreshed == second.created_or_refreshed == 1
    assert await test_session.scalar(select(func.count()).select_from(MissingDeliveryAlert)) == 1

    test_session.add(
        TradingCalendar(
            market="TW",
            trade_date=date(2026, 7, 23),
            is_open=True,
            session_close=time(13, 30),
        )
    )
    await test_session.commit()
    await scan_missing_deliveries(
        test_session,
        now=datetime(2026, 7, 23, 8, 0, tzinfo=timezone.utc),
    )
    assert await test_session.scalar(select(func.count()).select_from(MissingDeliveryAlert)) == 2

    test_session.add(
        IngestionRun(
            dataset_key="tw_equity_eod",
            source="finlab",
            schema_id="market_eod",
            schema_version=1,
            batch_data_date=date(2026, 7, 22),
            delivery_mode="full_snapshot",
            is_rerun=False,
            status="failed",
        )
    )
    await test_session.commit()
    result = await scan_missing_deliveries(test_session, now=NOW)
    alert = (
        await test_session.execute(
            select(MissingDeliveryAlert).where(
                MissingDeliveryAlert.expected_data_date == date(2026, 7, 22)
            )
        )
    ).scalar_one()
    assert result.resolved == 1
    assert alert.status == "resolved"
    assert alert.resolved_at == NOW
    still_open = await test_session.scalar(
        select(func.count())
        .select_from(MissingDeliveryAlert)
        .where(MissingDeliveryAlert.status == "open")
    )
    assert still_open == 1


@pytest.mark.asyncio
async def test_presence_requires_exact_non_rerun_full_snapshot_identity(test_session) -> None:
    await _seed_dataset(test_session)
    for source, version, data_date, mode, rerun in [
        ("other", 1, date(2026, 7, 22), "full_snapshot", False),
        ("finlab", 2, date(2026, 7, 22), "full_snapshot", False),
        ("finlab", 1, date(2026, 7, 21), "full_snapshot", False),
        ("finlab", 1, date(2026, 7, 22), "incremental", False),
        ("finlab", 1, date(2026, 7, 22), "full_snapshot", True),
    ]:
        test_session.add(
            IngestionRun(
                dataset_key="tw_equity_eod",
                source=source,
                schema_id="market_eod",
                schema_version=version,
                batch_data_date=data_date,
                delivery_mode=mode,
                is_rerun=rerun,
                status="completed",
            )
        )
    test_session.add(
        IngestionRun(
            dataset_key="tw_equity_eod",
            source="finlab",
            schema_id=None,
            schema_version=None,
            batch_data_date=date(2026, 7, 22),
            delivery_mode="full_snapshot",
            is_rerun=False,
            status="completed",
        )
    )
    await test_session.commit()
    await scan_missing_deliveries(test_session, now=NOW)
    assert await test_session.scalar(select(func.count()).select_from(MissingDeliveryAlert)) == 1


@pytest.mark.asyncio
async def test_disabled_inactive_and_calendar_unavailable_do_not_alert(test_session) -> None:
    await _seed_dataset(test_session, config=_config(action="disabled"))
    result = await scan_missing_deliveries(test_session, now=NOW)
    assert result.created_or_refreshed == 0

    dataset = await test_session.get(DatasetRegistry, "tw_equity_eod")
    assert dataset is not None
    dataset.config = _config(sources=["finlab"])
    dataset.is_active = False
    await test_session.commit()
    assert (await scan_missing_deliveries(test_session, now=NOW)).created_or_refreshed == 0

    dataset.is_active = True
    await test_session.delete((await test_session.execute(select(TradingCalendar))).scalar_one())
    await test_session.commit()
    unavailable = await scan_missing_deliveries(test_session, now=NOW)
    assert unavailable.created_or_refreshed == 0
    assert unavailable.diagnostics[0]["reason"] == "calendar_does_not_cover_evaluation_date"


@pytest.mark.asyncio
async def test_admin_list_filters_paginates_and_health_summarizes(
    client, test_session, admin_headers
) -> None:
    await _seed_dataset(test_session)
    await scan_missing_deliveries(test_session, now=NOW)

    unauthorized = await client.get("/api/v1/admin/missing-deliveries")
    assert unauthorized.status_code == 401
    response = await client.get(
        "/api/v1/admin/missing-deliveries",
        params={"status": "open", "dataset_key": "tw_equity_eod", "source": "FINLAB"},
        headers=admin_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["pagination"]["total_records"] == 1
    assert body["data"][0]["source"] == "finlab"

    health = await client.get("/api/v1/admin/queue/health", headers=admin_headers)
    assert health.status_code == 200
    assert health.json()["missing_deliveries"] == 1
    assert health.json()["oldest_missing_delivery_at"] == NOW.isoformat().replace("+00:00", "Z")


@pytest.mark.asyncio
async def test_concurrent_scans_cannot_create_duplicate_alerts(test_engine) -> None:
    session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        await _seed_dataset(session)

    async def scan() -> None:
        async with session_factory() as session:
            await scan_missing_deliveries(session, now=NOW)

    await asyncio.gather(scan(), scan())
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(MissingDeliveryAlert)) == 1
