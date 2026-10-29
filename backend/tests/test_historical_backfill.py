from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, func, null, select, update

from app.models.canonical import CalendarRevisionDay, CalendarYearRevision
from app.models.registry import (
    DatasetRegistry,
    HistoricalBackfillItem,
    HistoricalBackfillRequest,
    IngestionRun,
    MissingDeliveryAlert,
    SchedulerControl,
    SchedulerDataset,
    SourceClient,
)
from app.services import historical_backfill
from app.services.historical_backfill import (
    MAX_BACKFILL_DAYS,
    HistoricalBackfillConflictError,
    HistoricalBackfillError,
    HistoricalBackfillValidationError,
    cancel_request,
    complete_item,
    create_request,
    list_requests,
    preview_request,
)
from app.services.normalize.base import BaseNormalizer
from app.services.provider_work import ProviderWorkError
from app.services.source_clients import create_source_client
from app.utils import uuid7
from tests.owned_history import claim_next_item


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope",
    [
        None,
        [],
        "tw_equity_minute",
        [None],
        [1],
        [""],
        ["tw_etf_minute"],
        ["us_equity_eod"],
        ["tw_equity_minute", "tw_equity_minute"],
    ],
)
async def test_live_historical_acquisition_denies_invalid_db_credential_scope(
    client, test_session, historical_backfill_clock, scope
):
    today = historical_backfill_clock.date()
    await _seed_scope(test_session, today)
    await create_request(
        test_session,
        provider="shioaji",
        dataset_key="tw_equity_minute",
        start_date=today,
        end_date=today,
        request_key="invalid-historical-probe",
        created_by="operator",
        today=today,
    )
    source, key = await create_source_client(
        test_session,
        name="invalid-historical-probe",
        source_name="shioaji",
        allowed_datasets=["tw_equity_minute"],
        rate_limit_requests=100,
        rate_limit_window=60,
        commit=False,
    )
    item = await claim_next_item(
        test_session, provider="shioaji", allowed_datasets=["tw_equity_minute"]
    )
    assert item is not None
    path = f"/api/v1/source/historical-backfills/{item.item_id}/authorization"
    body, headers = {"lease_token": str(item.lease_token)}, {"X-API-Key": key}
    token, expiry = item.lease_token, item.lease_expires_at
    assert (await client.post(path, json=body, headers=headers)).json()[
        "acquisition_allowed"
    ] is True
    await test_session.execute(
        update(SourceClient)
        .where(SourceClient.client_id == source.client_id)
        .values(allowed_datasets=null() if scope is None else scope)
        .execution_options(synchronize_session=False)
    )
    denied = await client.post(path, json=body, headers=headers)
    assert denied.status_code in {403, 404}
    assert "acquisition_allowed" not in denied.json()
    with pytest.raises(HistoricalBackfillError):
        await historical_backfill.authorize_item(
            test_session,
            item.item_id,
            provider="shioaji",
            allowed_datasets=scope,
            lease_token=token,
        )
    await test_session.refresh(item)
    assert (
        item.status == "running" and item.lease_token == token and item.lease_expires_at == expiry
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["staging", "production"])
@pytest.mark.parametrize("deny", [None, "cancel", "expired", "token", "revoked", "scope"])
async def test_historical_acquisition_probe_is_current_scoped_and_never_renews(
    client, test_session, historical_backfill_clock, deny, monkeypatch, environment
):
    monkeypatch.setenv("APP_ENVIRONMENT", environment)
    from app.services.source_clients import revoke_source_client

    today = historical_backfill_clock.date()
    await _seed_scope(test_session, today)
    request, _ = await create_request(
        test_session,
        provider="shioaji",
        dataset_key="tw_equity_minute",
        start_date=today,
        end_date=today,
        request_key="current-acquisition-probe",
        created_by="operator",
        today=today,
    )
    source, key = await create_source_client(
        test_session,
        name="probe-worker",
        owner="tests",
        source_name="shioaji",
        allowed_datasets=["tw_equity_minute"],
        rate_limit_requests=100,
        rate_limit_window=60,
        commit=False,
    )
    item = await claim_next_item(
        test_session, provider="shioaji", allowed_datasets=["tw_equity_minute"]
    )
    assert item is not None
    expires, token = item.lease_expires_at, item.lease_token
    if deny == "cancel":
        await cancel_request(test_session, request.request_id, actor="operator")
    elif deny == "expired":
        item.lease_expires_at = historical_backfill_clock
    elif deny == "token":
        item.lease_token = uuid7()
    elif deny == "revoked":
        await revoke_source_client(test_session, source.client_id, commit=False)
    elif deny == "scope":
        source.allowed_datasets = ["tw_etf_minute"]
    await test_session.flush()
    actual_expiry, actual_token = item.lease_expires_at, item.lease_token
    response = await client.post(
        f"/api/v1/source/historical-backfills/{item.item_id}/authorization",
        json={"lease_token": str(token)},
        headers={"X-API-Key": key},
    )
    if deny in {"revoked", "scope"}:
        assert response.status_code in {401, 403, 404}
    else:
        assert response.status_code == 200
        assert response.json()["acquisition_allowed"] is (deny in {None, "expired", "token"})
        assert response.json()["max_age_seconds"] == 5
        assert datetime.fromisoformat(response.json()["checked_at"]) == datetime.fromisoformat(
            response.headers["X-FinDB-Acquisition-Checked-At"]
        )
        assert response.headers["Cache-Control"] == "no-store"
    await test_session.refresh(item)
    assert item.lease_expires_at == actual_expiry and item.lease_token == actual_token
    if deny is None:
        assert item.lease_expires_at == expires and item.lease_token == token


class _StatusOnlyNormalizer(BaseNormalizer):
    def map_fields(self, raw_data: dict) -> list:
        return []


@pytest.fixture
def historical_backfill_clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    now = datetime(2026, 9, 1, 6, tzinfo=timezone.utc)
    monkeypatch.setattr(historical_backfill, "utc_now", lambda: now)
    return now


def test_production_keeps_one_year_eod_and_31_day_shioaji_minute_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENVIRONMENT", "production")

    assert historical_backfill._max_backfill_days() == 366
    assert historical_backfill._max_backfill_days("shioaji") == 31
    assert historical_backfill.expiry_cutoff(
        "shioaji", datetime(2026, 8, 31, 16, 1, tzinfo=timezone.utc)
    ) == date(2026, 8, 1)
    assert historical_backfill._supports_backfill("twelve_data", "us_equity_eod") is True
    assert historical_backfill._supports_backfill("finlab", "tw_equity_eod") is True
    assert historical_backfill._supports_backfill("shioaji", "tw_equity_minute") is True
    assert historical_backfill._supports_backfill("shioaji", "tw_etf_minute") is True
    assert historical_backfill._supports_backfill("shioaji", "tw_equity_eod") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("dataset_key", ["tw_equity_minute", "tw_etf_minute"])
async def test_production_shioaji_date_window_uses_taipei_date_and_rejects_invalid_scopes(
    test_session, monkeypatch, dataset_key
):
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    # UTC still says August 31 while the TW exchange has entered September 1.
    now = datetime(2026, 8, 31, 16, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(historical_backfill, "utc_now", lambda: now)
    today = date(2026, 9, 1)
    await _seed_scope(test_session, today, dataset_key=dataset_key)
    oldest = today - timedelta(days=30)
    _, reason, days = await preview_request(
        test_session,
        provider="shioaji",
        dataset_key=dataset_key,
        start_date=oldest,
        end_date=today,
    )
    assert reason is None and len(days) == 31
    request, duplicate = await create_request(
        test_session,
        provider="shioaji",
        dataset_key=dataset_key,
        start_date=oldest,
        end_date=today,
        request_key="production-shioaji-month",
        created_by="operator",
    )
    assert not duplicate and len(request.items) == 31
    for start, end, expected in (
        (oldest - timedelta(days=1), oldest, "outside_latest_31_days"),
        (today, today + timedelta(days=1), "outside_latest_31_days"),
    ):
        _, reason, _ = await preview_request(
            test_session,
            provider="shioaji",
            dataset_key=dataset_key,
            start_date=start,
            end_date=end,
        )
        assert reason == expected
        with pytest.raises(HistoricalBackfillError, match="latest 31"):
            await create_request(
                test_session,
                provider="shioaji",
                dataset_key=dataset_key,
                start_date=start,
                end_date=end,
                request_key="outside",
                created_by="operator",
            )
    _, reason, _ = await preview_request(
        test_session,
        provider="finlab",
        dataset_key=dataset_key,
        start_date=oldest,
        end_date=oldest,
    )
    assert reason == "provider_dataset_unsupported"
    with pytest.raises(HistoricalBackfillError, match="does not support"):
        await create_request(
            test_session,
            provider="finlab",
            dataset_key=dataset_key,
            start_date=oldest,
            end_date=oldest,
            request_key="wrong-provider",
            created_by="operator",
        )


async def _seed_scope(session, today: date, *, dataset_key: str = "tw_equity_minute") -> None:
    session.add(
        DatasetRegistry(
            dataset_key=dataset_key,
            name="TW",
            asset_class="etf" if dataset_key == "tw_etf_minute" else "equity",
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
    session.add(SchedulerDataset(scheduler_key="shioaji-tw", dataset_key=dataset_key))
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
async def test_calendar_gap_defaults_without_blocking_request(
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
        (missing, True, None),
        (today, True, None),
    ]
    request, _ = await create_request(
        test_session,
        provider="shioaji",
        dataset_key="tw_equity_minute",
        start_date=missing,
        end_date=today,
        request_key="historic-calendar-gap",
        created_by="operator",
        today=today,
    )
    assert [item.trade_date for item in request.items] == [missing, today]
    assert request.items[0].calendar_snapshot["calendar_source"] == "weekday_default"


@pytest.mark.asyncio
async def test_closed_days_default_to_acquisition(
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
    assert [item.trade_date for item in request.items] == [closed, today]

    closed_request, _ = await create_request(
        test_session,
        provider="shioaji",
        dataset_key="tw_equity_minute",
        start_date=closed,
        end_date=closed,
        request_key="historic-calendar-all-closed",
        created_by="operator",
        today=today,
    )
    assert closed_request.items[0].status == "pending"
    assert closed_request.items[0].calendar_snapshot["is_open"] is False


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
    monkeypatch,
) -> None:
    today = date(2026, 9, 1)
    monkeypatch.setattr(
        historical_backfill, "utc_now", lambda: datetime(2026, 9, 1, 0, tzinfo=timezone.utc)
    )
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
        test_session,
        provider="shioaji",
        allowed_datasets=["tw_equity_minute"],
        now=datetime(2026, 9, 1, 0, tzinfo=timezone.utc),
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
    test_session, historical_backfill_clock
) -> None:
    today = historical_backfill_clock.date()
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
    with pytest.raises(ProviderWorkError, match="complete expected scope"):
        async with test_session.begin_nested():
            await complete_item(
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
    await test_session.refresh(item)
    await test_session.refresh(alert)
    assert request.status == item.status == "running"
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
async def test_expired_legacy_projection_never_reclaims_whole_work_owner(
    test_session, historical_backfill_clock
) -> None:
    today = historical_backfill_clock.date()
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
    first.lease_expires_at = historical_backfill_clock - timedelta(seconds=1)
    await test_session.flush()
    from app.models.provider_work import ProviderWorkOwner
    from app.services import provider_work

    owner = await test_session.get(ProviderWorkOwner, (provider_work.environment(), "shioaji"))
    owner.expires_at = historical_backfill_clock - timedelta(seconds=1)
    owner_identity = (owner.attempt_id, owner.generation, owner.token)
    assert (
        await claim_next_item(
            test_session, provider="shioaji", allowed_datasets=["tw_equity_minute"]
        )
        is None
    )
    assert (owner.attempt_id, owner.generation, owner.token) == owner_identity
    assert first.lease_token == first_token
    assert owner.recovery_reason == "heartbeat_missing"


@pytest.mark.asyncio
async def test_cancelled_request_rejects_inflight_completion(
    test_session, historical_backfill_clock
) -> None:
    today = historical_backfill_clock.date()
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
    with pytest.raises((HistoricalBackfillConflictError, ProviderWorkError)):
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
    with pytest.raises((HistoricalBackfillConflictError, ProviderWorkError)):
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
    test_session, historical_backfill_clock
) -> None:
    today = historical_backfill_clock.date()
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
        select(HistoricalBackfillItem)
        .where(
            HistoricalBackfillItem.request_id == request.request_id,
            HistoricalBackfillItem.trade_date < today - timedelta(days=MAX_BACKFILL_DAYS),
        )
        .order_by(HistoricalBackfillItem.trade_date)
        .limit(1)
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
    valid_request.items[0].calendar_snapshot = {
        **valid_request.items[0].calendar_snapshot,
        "trade_date": valid_request.items[0].trade_date.isoformat(),
    }
    from app.models.provider_work import ProviderWork, ProviderWorkAttempt

    queued = await test_session.scalar(
        select(ProviderWork).where(
            ProviderWork.stable_key == f"historical:{valid_request.items[0].item_id}"
        )
    )
    queued.trade_date = valid_request.items[0].trade_date
    attempt = await test_session.scalar(
        select(ProviderWorkAttempt).where(ProviderWorkAttempt.work_id == queued.work_id)
    )
    attempt.not_before = utc_previous_day
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
    client, test_session, historical_backfill_clock
) -> None:
    today = historical_backfill_clock.date()
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
    assert item_id is not None
    assert lease_token
    assert claim.json()["trade_date"] == (today - timedelta(days=1)).isoformat()
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
    ).status_code == 409


@pytest.mark.asyncio
async def test_source_complete_requires_matching_terminal_run_and_lease_token(
    client, test_session, historical_backfill_clock
) -> None:
    today = historical_backfill_clock.date()
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
    assert item_id is not None
    assert lease_token
    assert claim.json()["trade_date"] == today.isoformat()
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
    assert complete.status_code == 409
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
@pytest.mark.parametrize("environment", ["staging", "production"])
@pytest.mark.parametrize("dataset_key", ["tw_equity_minute", "tw_etf_minute"])
async def test_operator_can_preview_create_list_scope_and_cancel_historical_backfill(
    client,
    test_session,
    admin_headers,
    historical_backfill_clock,
    monkeypatch,
    environment,
    dataset_key,
) -> None:
    monkeypatch.setenv("APP_ENVIRONMENT", environment)
    today = historical_backfill_clock.date()
    await _seed_scope(test_session, today, dataset_key=dataset_key)
    body = {
        "provider": "shioaji",
        "dataset_key": dataset_key,
        "start_date": today.isoformat(),
        "end_date": today.isoformat(),
        "request_key": "historic-operator-flow",
    }
    scopes = await client.get("/api/v1/admin/historical-backfills/scopes", headers=admin_headers)
    assert scopes.status_code == 200
    assert any(
        scope["provider"] == "shioaji" and scope["dataset_key"] == dataset_key
        for scope in scopes.json()["data"]
    )
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
    replay = await client.post(
        "/api/v1/admin/historical-backfills", headers=admin_headers, json=body
    )
    assert replay.status_code == 202
    assert replay.json()["request_id"] == created.json()["request_id"]
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
async def test_create_endpoint_acquires_closed_dates_by_default(
    client, test_session, admin_headers, monkeypatch
) -> None:
    today = date(2026, 9, 1)
    monkeypatch.setattr(
        historical_backfill, "utc_now", lambda: datetime(2026, 9, 1, 0, tzinfo=timezone.utc)
    )
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
    assert [item["trade_date"] for item in response.json()["items"]] == [
        closed.isoformat(),
        today.isoformat(),
    ]
    assert (
        await test_session.scalar(select(func.count()).select_from(HistoricalBackfillRequest)) == 1
    )
    assert await test_session.scalar(select(func.count()).select_from(HistoricalBackfillItem)) == 2


@pytest.mark.asyncio
async def test_create_endpoint_calendar_gap_uses_default(
    client, test_session, admin_headers, historical_backfill_clock
) -> None:
    today = historical_backfill_clock.date()
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
    assert response.status_code == 202
    assert (
        await test_session.scalar(select(func.count()).select_from(HistoricalBackfillRequest))
    ) == 1
    assert await test_session.scalar(select(func.count()).select_from(HistoricalBackfillItem)) == 2


@pytest.fixture(autouse=True)
def queue_clock(monkeypatch):
    from app.services import historical_backfill, provider_work

    monkeypatch.setattr(provider_work, "utc_now", lambda: historical_backfill.utc_now())


@pytest.fixture
def client(client, test_session):
    from tests.owned_history import OwnedHistoricalClient

    return OwnedHistoricalClient(client, test_session)
