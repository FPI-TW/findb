from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, func, select

from app.models.canonical import CalendarRevisionDay, CalendarYearRevision
from app.models.registry import (
    DatasetRegistry,
    HistoricalBackfillItem,
    HistoricalBackfillRequest,
    IngestionRun,
    MissingDeliveryAlert,
    SchedulerControl,
    SchedulerDataset,
)
from app.services.historical_backfill import (
    MAX_BACKFILL_DAYS,
    HistoricalBackfillConflictError,
    HistoricalBackfillError,
    HistoricalBackfillValidationError,
    cancel_request,
    claim_next_item,
    complete_item,
    create_request,
    list_requests,
    preview_request,
)
from app.services.normalize.base import BaseNormalizer
from app.services.source_clients import create_source_client
from app.utils import utc_now, uuid7


class _StatusOnlyNormalizer(BaseNormalizer):
    def map_fields(self, raw_data: dict) -> list:
        return []


async def _seed_scope(session, today: date) -> None:
    session.add(
        DatasetRegistry(
            dataset_key="tw_equity_minute",
            name="TW",
            asset_class="equity",
            market="TW",
            is_active=True,
        )
    )
    session.add(
        SchedulerControl(
            scheduler_key="shioaji-tw",
            provider="shioaji",
            slot_id="taiwan_market_window",
            timezone="Asia/Taipei",
        )
    )
    session.add(SchedulerDataset(scheduler_key="shioaji-tw", dataset_key="tw_equity_minute"))
    await session.flush()
    revision = CalendarYearRevision(
        id=uuid7(),
        market="TW",
        year=today.year,
        revision=1,
        status="published",
        expected_days=366 if today.year % 4 == 0 else 365,
        actual_days=366 if today.year % 4 == 0 else 365,
        timezone="Asia/Taipei",
        source_kind="test",
    )
    session.add(revision)
    await session.flush()
    for offset in range(31):
        session.add(
            CalendarRevisionDay(
                id=uuid7(),
                calendar_revision_id=revision.id,
                trade_date=today - timedelta(days=offset),
                day_status="open",
                is_open=True,
                source_kind="test",
            )
        )
    await session.flush()


async def _seed_twelve_scope(session, today: date) -> None:
    session.add(
        DatasetRegistry(
            dataset_key="us_equity_eod",
            name="US",
            asset_class="equity",
            market="US",
            is_active=True,
        )
    )
    session.add(
        SchedulerControl(
            scheduler_key="twelve-us",
            provider="twelve_data",
            slot_id="western_markets_window",
            timezone="America/New_York",
        )
    )
    session.add(SchedulerDataset(scheduler_key="twelve-us", dataset_key="us_equity_eod"))
    await session.flush()
    revision = CalendarYearRevision(
        id=uuid7(),
        market="US",
        year=today.year,
        revision=1,
        status="published",
        expected_days=365,
        actual_days=365,
        timezone="America/New_York",
        source_kind="test",
    )
    session.add(revision)
    await session.flush()
    session.add(
        CalendarRevisionDay(
            id=uuid7(),
            calendar_revision_id=revision.id,
            trade_date=today,
            day_status="open",
            is_open=True,
            source_kind="test",
        )
    )
    await session.flush()


@pytest.mark.asyncio
async def test_historical_backfill_rejects_out_of_window_and_is_idempotent(test_session) -> None:
    today = date(2026, 9, 1)
    await _seed_scope(test_session, today)
    with pytest.raises(HistoricalBackfillError, match="latest 31"):
        await create_request(
            test_session,
            provider="shioaji",
            dataset_key="tw_equity_minute",
            start_date=today - timedelta(days=31),
            end_date=today,
            request_key="historic-window",
            created_by="operator",
            today=today,
        )
    first, duplicate = await create_request(
        test_session,
        provider="shioaji",
        dataset_key="tw_equity_minute",
        start_date=today - timedelta(days=1),
        end_date=today,
        request_key="historic-duplicate",
        created_by="operator",
        today=today,
    )
    repeated, repeated_duplicate = await create_request(
        test_session,
        provider="shioaji",
        dataset_key="tw_equity_minute",
        start_date=today - timedelta(days=1),
        end_date=today,
        request_key="historic-duplicate",
        created_by="operator",
        today=today,
    )
    assert duplicate is False
    assert repeated_duplicate is True
    assert repeated.request_id == first.request_id


@pytest.mark.asyncio
async def test_calendar_gap_rejects_whole_request_without_persistence(
    test_session,
) -> None:
    today = date(2026, 9, 1)
    await _seed_scope(test_session, today)
    missing = today - timedelta(days=1)
    await test_session.execute(
        delete(CalendarRevisionDay).where(CalendarRevisionDay.trade_date == missing)
    )
    market, reason, days = await preview_request(
        test_session,
        provider="shioaji",
        dataset_key="tw_equity_minute",
        start_date=missing,
        end_date=today,
        today=today,
    )
    assert market == "TW" and reason is None
    assert [(value, valid, day_reason) for value, valid, day_reason in days] == [
        (missing, False, "calendar_unpublished"),
        (today, True, None),
    ]
    with pytest.raises(HistoricalBackfillError, match="unpublished calendar days"):
        await create_request(
            test_session,
            provider="shioaji",
            dataset_key="tw_equity_minute",
            start_date=missing,
            end_date=today,
            request_key="historic-calendar-gap",
            created_by="operator",
            today=today,
        )
    assert (
        await test_session.scalar(select(func.count()).select_from(HistoricalBackfillRequest))
    ) == 0
    assert await test_session.scalar(select(func.count()).select_from(HistoricalBackfillItem)) == 0


@pytest.mark.asyncio
async def test_closed_days_are_skipped_but_all_closed_range_is_rejected_without_persistence(
    test_session,
) -> None:
    today = date(2026, 9, 1)
    await _seed_scope(test_session, today)
    closed = today - timedelta(days=1)
    await test_session.execute(
        delete(CalendarRevisionDay).where(CalendarRevisionDay.trade_date == closed)
    )
    revision = await test_session.scalar(
        select(CalendarYearRevision).where(CalendarYearRevision.market == "TW")
    )
    assert revision is not None
    test_session.add(
        CalendarRevisionDay(
            id=uuid7(),
            calendar_revision_id=revision.id,
            trade_date=closed,
            day_status="closed",
            is_open=False,
            source_kind="test",
        )
    )
    await test_session.flush()

    request, duplicate = await create_request(
        test_session,
        provider="shioaji",
        dataset_key="tw_equity_minute",
        start_date=closed,
        end_date=today,
        request_key="historic-calendar-mixed",
        created_by="operator",
        today=today,
    )
    assert duplicate is False
    assert (request.start_date, request.end_date) == (closed, today)
    assert [item.trade_date for item in request.items] == [today]

    with pytest.raises(HistoricalBackfillError, match="no open market dates"):
        await create_request(
            test_session,
            provider="shioaji",
            dataset_key="tw_equity_minute",
            start_date=closed,
            end_date=closed,
            request_key="historic-calendar-all-closed",
            created_by="operator",
            today=today,
        )
    assert (
        await test_session.scalar(select(func.count()).select_from(HistoricalBackfillRequest))
    ) == 1


@pytest.mark.asyncio
async def test_historical_backfill_requests_are_newest_first_and_paginated(test_session) -> None:
    today = date(2026, 9, 1)
    await _seed_scope(test_session, today)
    for index in range(3):
        await create_request(
            test_session,
            provider="shioaji",
            dataset_key="tw_equity_minute",
            start_date=today,
            end_date=today,
            request_key=f"historic-pagination-{index}",
            created_by="operator",
            today=today,
        )
    first, total = await list_requests(test_session, page=1, page_size=2)
    second, second_total = await list_requests(test_session, page=2, page_size=2)
    assert total == second_total == 3
    assert len(first) == 2 and len(second) == 1
    assert [row.request_key for row in first + second] == [
        "historic-pagination-2",
        "historic-pagination-1",
        "historic-pagination-0",
    ]


@pytest.mark.asyncio
async def test_failure_stops_later_dates_without_manually_resolving_delivery_alerts(
    test_session,
) -> None:
    today = date(2026, 9, 1)
    await _seed_scope(test_session, today)
    request, _ = await create_request(
        test_session,
        provider="shioaji",
        dataset_key="tw_equity_minute",
        start_date=today - timedelta(days=2),
        end_date=today,
        request_key="historic-stop",
        created_by="operator",
        today=today,
    )
    alert = MissingDeliveryAlert(
        dataset_key="tw_equity_minute",
        source="shioaji",
        schema_id="market_minute",
        schema_version=1,
        expected_data_date=today,
        status="open",
    )
    test_session.add(alert)
    await test_session.flush()
    item = await claim_next_item(
        test_session, provider="shioaji", allowed_datasets=["tw_equity_minute"]
    )
    assert item is not None and item.trade_date == today - timedelta(days=2)
    assert item.lease_token is not None
    await complete_item(
        test_session,
        item.item_id,
        provider="shioaji",
        outcome="failed",
        lease_token=item.lease_token,
        run_id=None,
        failure_code="PROVIDER_FAILED",
        failure_message="safe",
    )
    await test_session.refresh(request)
    await test_session.refresh(alert)
    assert request.status == "failed"
    assert alert.status == "open"
    statuses = (
        (
            await test_session.execute(
                select(HistoricalBackfillItem.status).where(
                    HistoricalBackfillItem.request_id == request.request_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert all(value != "pending" for value in statuses)


@pytest.mark.asyncio
async def test_completed_run_must_match_terminal_delivery_before_backfill_item_completes(
    test_session,
) -> None:
    today = date(2026, 9, 1)
    await _seed_scope(test_session, today)
    request, _ = await create_request(
        test_session,
        provider="shioaji",
        dataset_key="tw_equity_minute",
        start_date=today,
        end_date=today,
        request_key="historic-source-run-validation",
        created_by="operator",
        today=today,
    )
    item = await claim_next_item(
        test_session, provider="shioaji", allowed_datasets=["tw_equity_minute"]
    )
    assert item is not None and item.lease_token is not None
    with pytest.raises(HistoricalBackfillValidationError, match="requires a Source run"):
        await complete_item(
            test_session,
            item.item_id,
            provider="shioaji",
            outcome="completed",
            lease_token=item.lease_token,
            run_id=None,
            failure_code=None,
            failure_message=None,
        )
    foreign_run = IngestionRun(
        run_id=uuid7(),
        dataset_key="tw_equity_minute",
        source="other_provider",
        schema_id="market_minute",
        schema_version=1,
        batch_data_date=today,
        delivery_mode="sequenced_snapshot",
        snapshot_id="snapshot",
        daily_update_id="daily",
        sequence=1,
        sequence_count=1,
        status="completed",
        total_records=1,
        success_records=1,
        failed_records=0,
    )
    test_session.add(foreign_run)
    await test_session.flush()
    with pytest.raises(HistoricalBackfillValidationError, match="does not match"):
        await complete_item(
            test_session,
            item.item_id,
            provider="shioaji",
            outcome="completed",
            lease_token=item.lease_token,
            run_id=foreign_run.run_id,
            failure_code=None,
            failure_message=None,
        )
    successful_run = IngestionRun(
        run_id=uuid7(),
        dataset_key="tw_equity_minute",
        source="shioaji",
        schema_id="market_minute",
        schema_version=1,
        batch_data_date=today,
        delivery_mode="sequenced_snapshot",
        snapshot_id="snapshot",
        daily_update_id="daily",
        sequence=1,
        sequence_count=1,
        status="completed",
        total_records=1,
        success_records=1,
        failed_records=0,
    )
    alert = MissingDeliveryAlert(
        dataset_key="tw_equity_minute",
        source="shioaji",
        schema_id="market_minute",
        schema_version=1,
        expected_data_date=today,
        status="open",
    )
    test_session.add_all([successful_run, alert])
    await test_session.flush()
    completed = await complete_item(
        test_session,
        item.item_id,
        provider="shioaji",
        outcome="completed",
        lease_token=item.lease_token,
        run_id=successful_run.run_id,
        failure_code=None,
        failure_message=None,
    )
    await test_session.refresh(request)
    await test_session.refresh(completed)
    await test_session.refresh(alert)
    assert completed.status == request.status == "completed"
    # Control-plane completion is not the normalization terminal transition.
    # A Source-accepted run only resolves delivery alerts after canonical work
    # has completed successfully.
    assert alert.status == "open"


@pytest.mark.asyncio
async def test_full_snapshot_alert_waits_for_normalizer_terminal_success(test_session) -> None:
    today = date(2026, 9, 1)
    await _seed_scope(test_session, today)
    alert = MissingDeliveryAlert(
        dataset_key="tw_equity_minute",
        source="shioaji",
        schema_id="market_minute",
        schema_version=1,
        expected_data_date=today,
        status="open",
    )
    failed_run = IngestionRun(
        run_id=uuid7(),
        dataset_key="tw_equity_minute",
        source="shioaji",
        schema_id="market_minute",
        schema_version=1,
        batch_data_date=today,
        delivery_mode="full_snapshot",
        status="pending",
    )
    test_session.add_all([alert, failed_run])
    await test_session.flush()
    normalizer = _StatusOnlyNormalizer(test_session)

    # Source acceptance left the run pending; a terminal normalization failure
    # must leave the missing-delivery alert open.
    await normalizer.update_run_status(failed_run.run_id, "failed", 1, 0, 1, "safe")
    await test_session.refresh(alert)
    assert alert.status == "open"

    successful_run = IngestionRun(
        run_id=uuid7(),
        dataset_key="tw_equity_minute",
        source="shioaji",
        schema_id="market_minute",
        schema_version=1,
        batch_data_date=today,
        delivery_mode="full_snapshot",
        status="pending",
    )
    test_session.add(successful_run)
    await test_session.flush()
    await normalizer.update_run_status(successful_run.run_id, "completed", 1, 1, 0)
    await test_session.refresh(alert)
    assert alert.status == "resolved"


@pytest.mark.asyncio
async def test_expired_lease_is_reclaimed_and_late_completion_cannot_overwrite(
    test_session,
) -> None:
    today = date(2026, 9, 1)
    await _seed_scope(test_session, today)
    await create_request(
        test_session,
        provider="shioaji",
        dataset_key="tw_equity_minute",
        start_date=today,
        end_date=today,
        request_key="historic-lease-reclaim",
        created_by="operator",
        today=today,
    )
    first = await claim_next_item(
        test_session, provider="shioaji", allowed_datasets=["tw_equity_minute"]
    )
    assert first is not None and first.lease_token is not None
    first_token = first.lease_token
    first.lease_expires_at = utc_now() - timedelta(seconds=1)
    await test_session.flush()
    reclaimed = await claim_next_item(
        test_session, provider="shioaji", allowed_datasets=["tw_equity_minute"]
    )
    assert reclaimed is not None and reclaimed.lease_token is not None
    assert reclaimed.item_id == first.item_id
    assert reclaimed.lease_token != first_token and reclaimed.lease_attempt == 2
    with pytest.raises(HistoricalBackfillConflictError, match="not leased"):
        await complete_item(
            test_session,
            first.item_id,
            provider="shioaji",
            outcome="failed",
            lease_token=first_token,
            run_id=None,
            failure_code="OLD_WORKER",
            failure_message="safe",
        )
    await complete_item(
        test_session,
        reclaimed.item_id,
        provider="shioaji",
        outcome="failed",
        lease_token=reclaimed.lease_token,
        run_id=None,
        failure_code="PROVIDER_FAILED",
        failure_message="safe",
    )


@pytest.mark.asyncio
async def test_cancelled_request_rejects_inflight_completion(test_session) -> None:
    today = date(2026, 9, 1)
    await _seed_scope(test_session, today)
    request, _ = await create_request(
        test_session,
        provider="shioaji",
        dataset_key="tw_equity_minute",
        start_date=today,
        end_date=today,
        request_key="historic-cancel-race",
        created_by="operator",
        today=today,
    )
    item = await claim_next_item(
        test_session, provider="shioaji", allowed_datasets=["tw_equity_minute"]
    )
    assert item is not None and item.lease_token is not None
    await cancel_request(test_session, request.request_id, actor="operator")
    with pytest.raises(HistoricalBackfillConflictError, match="not leased"):
        await complete_item(
            test_session,
            item.item_id,
            provider="shioaji",
            outcome="completed",
            lease_token=item.lease_token,
            run_id=uuid7(),
            failure_code=None,
            failure_message=None,
        )
    with pytest.raises(HistoricalBackfillConflictError, match="not leased"):
        await complete_item(
            test_session,
            item.item_id,
            provider="shioaji",
            outcome="failed",
            lease_token=item.lease_token,
            run_id=None,
            failure_code="PROVIDER_FAILED",
            failure_message="safe",
        )


@pytest.mark.asyncio
async def test_claim_fails_expired_historical_item_and_cancels_following_dates(
    test_session,
) -> None:
    today = date(2026, 9, 1)
    await _seed_scope(test_session, today)
    request, _ = await create_request(
        test_session,
        provider="shioaji",
        dataset_key="tw_equity_minute",
        start_date=today - timedelta(days=1),
        end_date=today,
        request_key="historic-claim-expired",
        created_by="operator",
        today=today,
    )
    request.items[0].trade_date = today - timedelta(days=MAX_BACKFILL_DAYS + 1)
    request.items[1].trade_date = today - timedelta(days=MAX_BACKFILL_DAYS + 2)
    await test_session.flush()
    assert await claim_next_item(test_session, provider="shioaji", allowed_datasets=None) is None
    await test_session.refresh(request)
    expired_item = await test_session.scalar(
        select(HistoricalBackfillItem).where(
            HistoricalBackfillItem.request_id == request.request_id,
            HistoricalBackfillItem.trade_date < today - timedelta(days=MAX_BACKFILL_DAYS),
        )
    )
    assert request.status == "failed"
    assert expired_item is not None and expired_item.failure_code == "BACKFILL_EXPIRED"
    statuses = (
        await test_session.scalars(
            select(HistoricalBackfillItem.status)
            .where(HistoricalBackfillItem.request_id == request.request_id)
            .order_by(HistoricalBackfillItem.trade_date)
        )
    ).all()
    assert statuses == ["failed", "cancelled"]


@pytest.mark.asyncio
async def test_claim_uses_shioaji_market_date_at_utc_taipei_boundary(test_session) -> None:
    taipei_today = date(2026, 9, 2)
    # This is still 2026-09-01 in UTC, but already 2026-09-02 in Taiwan.
    utc_previous_day = datetime(2026, 9, 1, 16, 30, tzinfo=timezone.utc)
    await _seed_scope(test_session, taipei_today)
    expired_request, _ = await create_request(
        test_session,
        provider="shioaji",
        dataset_key="tw_equity_minute",
        start_date=taipei_today,
        end_date=taipei_today,
        request_key="historic-taipei-expired",
        created_by="operator",
        today=taipei_today,
    )
    valid_request, _ = await create_request(
        test_session,
        provider="shioaji",
        dataset_key="tw_equity_minute",
        start_date=taipei_today,
        end_date=taipei_today,
        request_key="historic-taipei-valid",
        created_by="operator",
        today=taipei_today,
    )
    expired_item = expired_request.items[0]
    expired_item.trade_date = taipei_today - timedelta(days=MAX_BACKFILL_DAYS + 1)
    # Inclusive: Shioaji's own target-date gate permits local-today minus 31 days.
    valid_request.items[0].trade_date = taipei_today - timedelta(days=MAX_BACKFILL_DAYS)
    await test_session.flush()

    claimed = await claim_next_item(
        test_session,
        provider="shioaji",
        allowed_datasets=["tw_equity_minute"],
        now=utc_previous_day,
    )
    await test_session.refresh(expired_request)
    await test_session.refresh(expired_item)
    assert expired_request.status == "failed"
    assert expired_item.failure_code == "BACKFILL_EXPIRED"
    assert claimed is not None
    assert claimed.request_id == valid_request.request_id
    assert claimed.trade_date == taipei_today - timedelta(days=MAX_BACKFILL_DAYS)


@pytest.mark.asyncio
async def test_shioaji_claim_does_not_expire_still_valid_twelve_request(test_session) -> None:
    taipei_today = date(2026, 9, 2)
    new_york_today = date(2026, 9, 1)
    utc_previous_day = datetime(2026, 9, 1, 16, 30, tzinfo=timezone.utc)
    await _seed_scope(test_session, taipei_today)
    await _seed_twelve_scope(test_session, new_york_today)
    twelve_request, _ = await create_request(
        test_session,
        provider="twelve_data",
        dataset_key="us_equity_eod",
        start_date=new_york_today,
        end_date=new_york_today,
        request_key="historic-twelve-ny-still-valid",
        created_by="operator",
        today=new_york_today,
    )
    twelve_item = twelve_request.items[0]
    # New York's inclusive day-31 is valid, even though Taipei has advanced
    # to the next date and would otherwise use a stricter expiry boundary.
    twelve_item.trade_date = new_york_today - timedelta(days=MAX_BACKFILL_DAYS)
    await test_session.flush()

    assert (
        await claim_next_item(
            test_session,
            provider="shioaji",
            allowed_datasets=["tw_equity_minute"],
            now=utc_previous_day,
        )
    ) is None
    await test_session.refresh(twelve_request)
    await test_session.refresh(twelve_item)
    assert twelve_request.status == "queued"
    assert twelve_item.status == "pending"
    assert twelve_item.failure_code is None


@pytest.mark.asyncio
async def test_preview_explains_open_and_unpublished_dates_before_create(test_session) -> None:
    today = date(2026, 9, 1)
    await _seed_scope(test_session, today)
    market, reason, days = await preview_request(
        test_session,
        provider="shioaji",
        dataset_key="tw_equity_minute",
        start_date=today - timedelta(days=1),
        end_date=today + timedelta(days=1),
        today=today,
    )
    assert market == "TW"
    assert reason == "outside_latest_31_days"
    assert [item[2] for item in days] == ["outside_latest_31_days"] * 3


@pytest.mark.asyncio
async def test_source_claim_and_terminal_failure_are_authenticated_and_serial(
    client, test_session
) -> None:
    today = date(2026, 9, 1)
    await _seed_scope(test_session, today)
    await create_request(
        test_session,
        provider="shioaji",
        dataset_key="tw_equity_minute",
        start_date=today - timedelta(days=1),
        end_date=today,
        request_key="historic-source-lease",
        created_by="operator",
        today=today,
    )
    _, key = await create_source_client(
        test_session,
        name="shioaji-worker",
        owner="tests",
        source_name="shioaji",
        allowed_datasets=["tw_equity_minute"],
        rate_limit_requests=20,
        rate_limit_window=60,
        commit=False,
    )
    assert (await client.post("/api/v1/source/historical-backfills/claim")).status_code == 401
    claim = await client.post(
        "/api/v1/source/historical-backfills/claim", headers={"X-API-Key": key}
    )
    assert claim.status_code == 200
    item_id = claim.json()["item_id"]
    lease_token = claim.json()["lease_token"]
    complete = await client.post(
        f"/api/v1/source/historical-backfills/{item_id}/complete",
        headers={"X-API-Key": key},
        json={
            "status": "failed",
            "lease_token": lease_token,
            "failure_code": "PROVIDER_FAILED",
            "failure_message": "safe",
        },
    )
    assert complete.status_code == 200
    assert (
        await client.post("/api/v1/source/historical-backfills/claim", headers={"X-API-Key": key})
    ).json()["item_id"] is None


@pytest.mark.asyncio
async def test_source_complete_requires_matching_terminal_run_and_lease_token(
    client, test_session
) -> None:
    today = date(2026, 9, 1)
    await _seed_scope(test_session, today)
    await create_request(
        test_session,
        provider="shioaji",
        dataset_key="tw_equity_minute",
        start_date=today,
        end_date=today,
        request_key="historic-source-complete-validation",
        created_by="operator",
        today=today,
    )
    _, key = await create_source_client(
        test_session,
        name="shioaji-worker-complete",
        owner="tests",
        source_name="shioaji",
        allowed_datasets=["tw_equity_minute"],
        rate_limit_requests=20,
        rate_limit_window=60,
        commit=False,
    )
    headers = {"X-API-Key": key}
    claim = await client.post("/api/v1/source/historical-backfills/claim", headers=headers)
    assert claim.status_code == 200
    item_id, lease_token = claim.json()["item_id"], claim.json()["lease_token"]
    missing_run = await client.post(
        f"/api/v1/source/historical-backfills/{item_id}/complete",
        headers=headers,
        json={"status": "completed", "lease_token": lease_token},
    )
    assert missing_run.status_code == 422
    run = IngestionRun(
        run_id=uuid7(),
        dataset_key="tw_equity_minute",
        source="shioaji",
        schema_id="market_minute",
        schema_version=1,
        batch_data_date=today,
        delivery_mode="sequenced_snapshot",
        snapshot_id="source-route-snapshot",
        daily_update_id="source-route-daily",
        sequence=1,
        sequence_count=1,
        status="completed",
        total_records=1,
        success_records=1,
        failed_records=0,
    )
    test_session.add(run)
    await test_session.flush()
    complete = await client.post(
        f"/api/v1/source/historical-backfills/{item_id}/complete",
        headers=headers,
        json={"status": "completed", "lease_token": lease_token, "run_id": str(run.run_id)},
    )
    assert complete.status_code == 200
    assert (
        await client.post(
            f"/api/v1/source/historical-backfills/{item_id}/complete",
            headers=headers,
            json={"status": "completed", "lease_token": lease_token, "run_id": str(run.run_id)},
        )
    ).status_code == 409


@pytest.mark.asyncio
async def test_operator_historical_endpoints_reject_viewer(client, test_session) -> None:
    from app.config import get_settings
    from app.services.api_keys import create_api_key

    _, viewer_key = await create_api_key(
        test_session,
        owner="viewer",
        tier="standard",
        scopes=["admin"],
        rate_limit_requests=20,
        rate_limit_window=60,
        page_size_limit=100,
        kind="admin",
        name="historical-viewer",
        role="viewer",
        commit=False,
    )
    headers = {get_settings().API_KEY_HEADER: viewer_key}
    body = {
        "provider": "shioaji",
        "dataset_key": "tw_equity_minute",
        "start_date": "2026-09-01",
        "end_date": "2026-09-01",
        "request_key": "historic-viewer-denied",
    }
    assert (
        await client.get("/api/v1/admin/historical-backfills", headers=headers)
    ).status_code == 403
    assert (
        await client.get("/api/v1/admin/historical-backfills/scopes", headers=headers)
    ).status_code == 403
    assert (
        await client.post("/api/v1/admin/historical-backfills/preview", headers=headers, json=body)
    ).status_code == 403
    assert (
        await client.post("/api/v1/admin/historical-backfills", headers=headers, json=body)
    ).status_code == 403
    assert (
        await client.post(f"/api/v1/admin/historical-backfills/{uuid7()}/cancel", headers=headers)
    ).status_code == 403


@pytest.mark.asyncio
async def test_operator_can_preview_create_list_scope_and_cancel_historical_backfill(
    client, test_session, admin_headers
) -> None:
    today = date(2026, 9, 1)
    await _seed_scope(test_session, today)
    body = {
        "provider": "shioaji",
        "dataset_key": "tw_equity_minute",
        "start_date": today.isoformat(),
        "end_date": today.isoformat(),
        "request_key": "historic-operator-flow",
    }
    assert (
        await client.get("/api/v1/admin/historical-backfills/scopes", headers=admin_headers)
    ).status_code == 200
    preview = await client.post(
        "/api/v1/admin/historical-backfills/preview", headers=admin_headers, json=body
    )
    assert preview.status_code == 200 and preview.json()["days"] == [
        {"trade_date": today.isoformat(), "valid": True, "reason": None}
    ]
    created = await client.post(
        "/api/v1/admin/historical-backfills", headers=admin_headers, json=body
    )
    assert created.status_code == 202
    assert (
        await client.get("/api/v1/admin/historical-backfills", headers=admin_headers)
    ).status_code == 200
    assert (
        await client.post(
            f"/api/v1/admin/historical-backfills/{created.json()['request_id']}/cancel",
            headers=admin_headers,
        )
    ).status_code == 200


@pytest.mark.asyncio
async def test_operator_lists_historical_backfills_with_requested_pagination(
    client, test_session, admin_headers
) -> None:
    today = date(2026, 9, 1)
    await _seed_scope(test_session, today)
    for index in range(3):
        await create_request(
            test_session,
            provider="shioaji",
            dataset_key="tw_equity_minute",
            start_date=today,
            end_date=today,
            request_key=f"historic-api-pagination-{index}",
            created_by="operator",
            today=today,
        )
    response = await client.get(
        "/api/v1/admin/historical-backfills?page=2&page_size=2",
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["pagination"] | {"next_cursor": None} == {
        "page": 2,
        "page_size": 2,
        "total_records": 3,
        "total_pages": 2,
        "next_cursor": None,
    }
    assert [row["request_key"] for row in response.json()["data"]] == ["historic-api-pagination-0"]


@pytest.mark.asyncio
async def test_create_endpoint_skips_published_closed_dates_but_preserves_requested_range(
    client, test_session, admin_headers
) -> None:
    today = date(2026, 9, 1)
    closed = today - timedelta(days=1)
    await _seed_scope(test_session, today)
    await test_session.execute(
        delete(CalendarRevisionDay).where(CalendarRevisionDay.trade_date == closed)
    )
    revision = await test_session.scalar(
        select(CalendarYearRevision).where(CalendarYearRevision.market == "TW")
    )
    assert revision is not None
    test_session.add(
        CalendarRevisionDay(
            id=uuid7(),
            calendar_revision_id=revision.id,
            trade_date=closed,
            day_status="closed",
            is_open=False,
            source_kind="test",
        )
    )
    await test_session.flush()

    response = await client.post(
        "/api/v1/admin/historical-backfills",
        headers=admin_headers,
        json={
            "provider": "shioaji",
            "dataset_key": "tw_equity_minute",
            "start_date": closed.isoformat(),
            "end_date": today.isoformat(),
            "request_key": "historic-api-mixed-closed-open",
        },
    )
    assert response.status_code == 202
    assert response.json()["start_date"] == closed.isoformat()
    assert response.json()["end_date"] == today.isoformat()
    assert [item["trade_date"] for item in response.json()["items"]] == [today.isoformat()]
    assert (
        await test_session.scalar(select(func.count()).select_from(HistoricalBackfillRequest)) == 1
    )
    assert await test_session.scalar(select(func.count()).select_from(HistoricalBackfillItem)) == 1


@pytest.mark.asyncio
async def test_create_endpoint_calendar_gap_rejects_without_persisting_scope(
    client, test_session, admin_headers
) -> None:
    today = date(2026, 9, 1)
    await _seed_scope(test_session, today)
    await test_session.execute(
        delete(CalendarRevisionDay).where(
            CalendarRevisionDay.trade_date == today - timedelta(days=1)
        )
    )
    response = await client.post(
        "/api/v1/admin/historical-backfills",
        headers=admin_headers,
        json={
            "provider": "shioaji",
            "dataset_key": "tw_equity_minute",
            "start_date": (today - timedelta(days=1)).isoformat(),
            "end_date": today.isoformat(),
            "request_key": "historic-api-calendar-gap",
        },
    )
    assert response.status_code == 422
    assert (
        await test_session.scalar(select(func.count()).select_from(HistoricalBackfillRequest))
    ) == 0
    assert await test_session.scalar(select(func.count()).select_from(HistoricalBackfillItem)) == 0
