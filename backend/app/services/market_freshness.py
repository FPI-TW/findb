"""Read-only market freshness projection from registry delivery expectations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.raw import RawMarketPayload
from app.models.registry import DatasetRegistry, IngestionRun, MissingDeliveryAlert
from app.services.delivery_policy import DeliveryExpectation, resolve_expected_data_date
from app.utils import utc_now

FreshnessStatus = Literal["not_due", "fresh", "partial", "late", "failed", "never_received"]


@dataclass(frozen=True)
class FreshnessFeed:
    dataset_key: str
    source: str
    schema_id: str
    schema_version: int
    expected_data_date: date | None
    latest_successful_data_date: date | None
    last_fetched_at: datetime | None
    last_completed_at: datetime | None
    last_run_id: UUID | None
    total_records: int | None
    success_records: int | None
    failed_records: int | None
    policy_outcome: str | None
    open_missing_delivery_alert: bool
    last_failure_code: str | None
    status: FreshnessStatus


@dataclass(frozen=True)
class MarketFreshness:
    market: str
    slot_id: str
    scheduled_local_time: time
    timezone: str
    status: FreshnessStatus
    expected_data_date: date | None
    coverage_data_date: date | None
    last_successful_update_at: datetime | None
    last_complete_at: datetime | None
    next_scheduled_at: datetime
    feed_count: int
    fresh_feed_count: int
    late_feed_count: int
    feeds: tuple[FreshnessFeed, ...]


@dataclass(frozen=True)
class _ConfiguredFeed:
    dataset: DatasetRegistry
    source: str
    schema_id: str
    schema_version: int
    expectation: DeliveryExpectation


def _next_scheduled_at(now: datetime, local_time: time, timezone_name: str) -> datetime:
    zone = ZoneInfo(timezone_name)
    local_now = now.astimezone(zone)
    candidate = datetime.combine(local_now.date(), local_time, tzinfo=zone)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


async def _runs_for_feed(db: AsyncSession, feed: _ConfiguredFeed):
    criteria = (
        IngestionRun.dataset_key == feed.dataset.dataset_key,
        IngestionRun.source == feed.source,
        IngestionRun.schema_id == feed.schema_id,
        IngestionRun.schema_version == feed.schema_version,
        IngestionRun.delivery_mode == feed.expectation.delivery_mode.value,
        IngestionRun.is_rerun.is_(False),
    )
    last_row = (
        await db.execute(
            select(IngestionRun, RawMarketPayload.fetched_at)
            .outerjoin(
                RawMarketPayload,
                RawMarketPayload.raw_payload_id == IngestionRun.raw_payload_id,
            )
            .where(*criteria)
            .order_by(IngestionRun.created_at.desc(), IngestionRun.run_id.desc())
            .limit(1)
        )
    ).first()
    latest_success = await db.scalar(
        select(IngestionRun)
        .where(
            *criteria,
            IngestionRun.status == "completed",
            IngestionRun.batch_data_date.is_not(None),
        )
        .order_by(
            IngestionRun.batch_data_date.desc(),
            IngestionRun.completed_at.desc(),
            IngestionRun.created_at.desc(),
        )
        .limit(1)
    )
    latest_failure = await db.scalar(
        select(IngestionRun)
        .where(*criteria, IngestionRun.status == "failed")
        .order_by(IngestionRun.created_at.desc(), IngestionRun.run_id.desc())
        .limit(1)
    )
    last_run, last_fetched_at = last_row if last_row else (None, None)
    return last_run, last_fetched_at, latest_success, latest_failure


async def _open_alert_exists(
    db: AsyncSession, feed: _ConfiguredFeed, expected: date | None
) -> bool:
    if expected is None:
        return False
    return bool(
        await db.scalar(
            select(MissingDeliveryAlert.alert_id)
            .where(
                MissingDeliveryAlert.dataset_key == feed.dataset.dataset_key,
                MissingDeliveryAlert.source == feed.source,
                MissingDeliveryAlert.schema_id == feed.schema_id,
                MissingDeliveryAlert.schema_version == feed.schema_version,
                MissingDeliveryAlert.expected_data_date == expected,
                MissingDeliveryAlert.status == "open",
            )
            .limit(1)
        )
    )


async def _feed_freshness(
    db: AsyncSession, feed: _ConfiguredFeed, expected: date | None
) -> FreshnessFeed:
    last_run, last_fetched_at, latest, failure = await _runs_for_feed(db, feed)
    alert_open = await _open_alert_exists(db, feed, expected)
    if last_run is None:
        state: FreshnessStatus = "never_received"
    elif expected is None:
        state = "not_due"
    elif latest is not None and latest.batch_data_date >= expected:
        state = "fresh"
    elif (
        last_run is not None
        and last_run.status == "failed"
        and (last_run.batch_data_date is None or last_run.batch_data_date >= expected)
    ):
        state = "failed"
    else:
        state = "late"
    return FreshnessFeed(
        dataset_key=feed.dataset.dataset_key,
        source=feed.source,
        schema_id=feed.schema_id,
        schema_version=feed.schema_version,
        expected_data_date=expected,
        latest_successful_data_date=latest.batch_data_date if latest else None,
        last_fetched_at=last_fetched_at,
        last_completed_at=latest.completed_at if latest else None,
        last_run_id=last_run.run_id if last_run else None,
        total_records=last_run.total_records if last_run else None,
        success_records=last_run.success_records if last_run else None,
        failed_records=last_run.failed_records if last_run else None,
        policy_outcome=last_run.policy_outcome if last_run else None,
        open_missing_delivery_alert=alert_open,
        last_failure_code=failure.failure_code if failure else None,
        status=state,
    )


async def list_market_freshness(
    db: AsyncSession,
    *,
    market: str | None = None,
    slot_id: str | None = None,
    status: FreshnessStatus | None = None,
    now: datetime | None = None,
) -> list[MarketFreshness]:
    """Build a projection without altering registry, run, or alert state."""
    evaluated_at = (now or utc_now()).astimezone(timezone.utc)
    statement = select(DatasetRegistry).where(DatasetRegistry.is_active.is_(True))
    if market:
        statement = statement.where(DatasetRegistry.market == market.upper())
    datasets = list((await db.execute(statement)).scalars().all())
    groups: dict[
        tuple[str, str], tuple[DatasetRegistry, DeliveryExpectation, list[_ConfiguredFeed]]
    ] = {}
    for dataset in datasets:
        config = dataset.config or {}
        try:
            expectation = DeliveryExpectation.model_validate(
                config.get("delivery_expectation") or {}
            )
        except ValueError:
            continue
        schedule = expectation.schedule
        if schedule is None or not schedule.enabled or (slot_id and schedule.slot_id != slot_id):
            continue
        declaration_schema = config.get("schema_id")
        schema_version = config.get("current_schema_version")
        if not isinstance(declaration_schema, str) or type(schema_version) is not int:
            continue
        key = (dataset.market, schedule.slot_id)
        entry = groups.get(key)
        if entry is None:
            entry = (dataset, expectation, [])
            groups[key] = entry
        configured_feeds = entry[2]
        configured_feeds.extend(
            _ConfiguredFeed(dataset, source, declaration_schema, schema_version, expectation)
            for source in schedule.expected_sources
        )

    results: list[MarketFreshness] = []
    for (group_market, group_slot), (
        first_dataset,
        first_expectation,
        configured,
    ) in groups.items():
        schedule = first_expectation.schedule
        assert schedule is not None
        expected, _ = (
            await resolve_expected_data_date(db, first_expectation.latest_date, evaluated_at)
            if first_expectation.latest_date is not None
            else (None, "latest_date_policy_not_configured")
        )
        feed_rows = tuple([await _feed_freshness(db, feed, expected) for feed in configured])
        states = [feed.status for feed in feed_rows]
        fresh_count = states.count("fresh")
        late_count = sum(value in {"late", "never_received"} for value in states)
        if states and all(value == "never_received" for value in states):
            group_status: FreshnessStatus = "never_received"
        elif expected is None:
            group_status = "not_due"
        elif "failed" in states:
            group_status = "failed"
        elif fresh_count == len(feed_rows):
            group_status = "fresh"
        elif fresh_count:
            group_status = "partial"
        else:
            group_status = "late"
        coverage_dates: list[date] = [
            value for feed in feed_rows if (value := feed.latest_successful_data_date) is not None
        ]
        coverage = min(coverage_dates) if len(coverage_dates) == len(feed_rows) else None
        last_updates = [feed.last_completed_at for feed in feed_rows if feed.last_completed_at]
        all_current = bool(
            expected
            and feed_rows
            and all(
                feed.latest_successful_data_date is not None
                and feed.latest_successful_data_date >= expected
                for feed in feed_rows
            )
        )
        results.append(
            MarketFreshness(
                market=group_market,
                slot_id=group_slot,
                scheduled_local_time=schedule.local_time,
                timezone=schedule.timezone,
                status=group_status,
                expected_data_date=expected,
                coverage_data_date=coverage,
                last_successful_update_at=max(last_updates, default=None),
                last_complete_at=max(last_updates, default=None) if all_current else None,
                next_scheduled_at=_next_scheduled_at(
                    evaluated_at, schedule.local_time, schedule.timezone
                ),
                feed_count=len(feed_rows),
                fresh_feed_count=fresh_count,
                late_feed_count=late_count,
                feeds=feed_rows,
            )
        )
    return [
        item
        for item in sorted(results, key=lambda item: (item.market, item.slot_id))
        if not status or item.status == status
    ]
