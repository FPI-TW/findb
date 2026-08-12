"""Read-only market freshness projection.

SchedulerControl is the source of truth for scheduler definitions.  Dataset
delivery blocks are used only to evaluate a mapped feed's own date policy; an
invalid or missing block never removes the scheduler card from this view.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Literal, cast
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.raw import RawMarketPayload
from app.models.registry import (
    DatasetRegistry,
    IngestionRun,
    MissingDeliveryAlert,
    SchedulerControl,
)
from app.services.delivery_policy import DeliveryExpectation, resolve_expected_data_date
from app.services.scheduler_control import (
    list_scheduler_controls,
    normalize_scheduler_key,
    scheduler_dataset_keys,
)
from app.services.sequenced_snapshots import list_sequenced_snapshot_groups
from app.services.slot_identity import normalize_slot_id
from app.utils import ensure_utc, utc_now

FreshnessStatus = Literal["not_due", "fresh", "partial", "late", "failed", "never_received"]


@dataclass(frozen=True)
class FreshnessFeed:
    dataset_key: str
    source: str
    schema_id: str | None
    schema_version: int | None
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
    configuration_error: str | None
    status: FreshnessStatus


@dataclass(frozen=True)
class MarketFreshness:
    market: str
    scheduler_key: str
    provider: str
    dataset_keys: tuple[str, ...]
    slot_id: str
    scheduled_local_time: time
    timezone: str
    desired_state: Literal["running", "stopped"]
    observed_state: Literal["running", "stopped"]
    revision: int
    last_heartbeat_at: datetime | None
    last_cycle_started_at: datetime | None
    last_cycle_completed_at: datetime | None
    last_error: str | None
    heartbeat_age_seconds: float | None
    configuration_status: Literal["ready", "error"]
    configuration_errors: tuple[str, ...]
    status: FreshnessStatus
    expected_data_date: date | None
    coverage_data_date: date | None
    last_fetched_at: datetime | None
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
    schema_id: str | None
    schema_version: int | None
    expectation: DeliveryExpectation | None
    configuration_errors: tuple[str, ...]


@dataclass(frozen=True)
class _SchedulerDefinition:
    scheduler_key: str
    provider: str
    providers: tuple[str, ...]
    dataset_keys: tuple[str, ...]
    slot_id: str
    scheduled_local_time: time
    timezone: str
    desired_state: Literal["running", "stopped"]
    observed_state: Literal["running", "stopped"]
    revision: int
    last_heartbeat_at: datetime | None
    last_cycle_started_at: datetime | None
    last_cycle_completed_at: datetime | None
    last_error: str | None
    created_at: datetime | None
    updated_at: datetime | None


def _next_scheduled_at(now: datetime, local_time: time, timezone_name: str) -> datetime:
    """Calculate the next slot while failing closed on a bad timezone."""
    try:
        zone = ZoneInfo(timezone_name)
    except (TypeError, ZoneInfoNotFoundError, ValueError):
        return ensure_utc(now)
    local_now = now.astimezone(zone)
    candidate = datetime.combine(local_now.date(), local_time, tzinfo=zone)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def _heartbeat_age(now: datetime, heartbeat: datetime | None) -> float | None:
    if heartbeat is None:
        return None
    return max(0.0, (now - ensure_utc(heartbeat)).total_seconds())


async def _runs_for_feed(
    db: AsyncSession,
    feed: _ConfiguredFeed,
    expected: date | None = None,
):
    criteria = [
        IngestionRun.dataset_key == feed.dataset.dataset_key,
        IngestionRun.source == feed.source,
        IngestionRun.is_rerun.is_(False),
    ]
    if feed.schema_id is not None:
        criteria.append(IngestionRun.schema_id == feed.schema_id)
    if feed.schema_version is not None:
        criteria.append(IngestionRun.schema_version == feed.schema_version)
    delivery_mode = feed.expectation.delivery_mode.value if feed.expectation else None
    if delivery_mode is not None:
        criteria.append(IngestionRun.delivery_mode == delivery_mode)

    latest_group = None
    last_criteria = list(criteria)
    if delivery_mode == "sequenced_snapshot" and expected is not None:
        # A strict minute session may evaluate the prior trading date before
        # today's 17:00 cutoff; future current-session attempts must not alter
        # that status.
        last_criteria.append(IngestionRun.batch_data_date <= expected)
    last_row = (
        await db.execute(
            select(IngestionRun, RawMarketPayload.fetched_at)
            .outerjoin(
                RawMarketPayload,
                (RawMarketPayload.raw_payload_id == IngestionRun.raw_payload_id)
                | (RawMarketPayload.run_id == IngestionRun.run_id),
            )
            .where(*last_criteria)
            .order_by(IngestionRun.created_at.desc(), IngestionRun.run_id.desc())
            .limit(1)
        )
    ).first()
    if delivery_mode == "sequenced_snapshot":
        latest_success = None
        latest_group = None
        if feed.schema_id is not None and feed.schema_version is not None:
            groups = await list_sequenced_snapshot_groups(
                db,
                dataset_key=feed.dataset.dataset_key,
                source=feed.source,
                schema_id=feed.schema_id,
                schema_version=feed.schema_version,
            )
            eligible_groups = (
                [group for group in groups if group.data_date <= expected]
                if expected is not None
                else groups
            )
            latest_group = eligible_groups[0] if eligible_groups else None
            latest_complete = next((group for group in eligible_groups if group.complete), None)
            if latest_complete is not None:
                latest_success = await db.scalar(
                    select(IngestionRun)
                    .where(
                        *criteria,
                        IngestionRun.batch_data_date == latest_complete.data_date,
                        IngestionRun.snapshot_id == latest_complete.snapshot_id,
                        IngestionRun.daily_update_id == latest_complete.daily_update_id,
                        IngestionRun.status == "completed",
                    )
                    .order_by(
                        IngestionRun.completed_at.desc().nullslast(),
                        IngestionRun.created_at.desc(),
                        IngestionRun.run_id.desc(),
                    )
                    .limit(1)
                )
    else:
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
    failure_criteria = list(criteria)
    if delivery_mode == "sequenced_snapshot" and expected is not None:
        failure_criteria.append(IngestionRun.batch_data_date <= expected)
    if latest_group is not None and latest_group.failed:
        failure_criteria.extend(
            [
                IngestionRun.batch_data_date == latest_group.data_date,
                IngestionRun.snapshot_id == latest_group.snapshot_id,
                IngestionRun.daily_update_id == latest_group.daily_update_id,
            ]
        )
    latest_failure = await db.scalar(
        select(IngestionRun)
        .where(*failure_criteria, IngestionRun.status == "failed")
        .order_by(IngestionRun.created_at.desc(), IngestionRun.run_id.desc())
        .limit(1)
    )
    last_run, last_fetched_at = last_row if last_row else (None, None)
    partial_group = bool(
        latest_group is not None and not latest_group.complete and not latest_group.failed
    )
    failed_group = bool(latest_group is not None and latest_group.failed)
    return last_run, last_fetched_at, latest_success, latest_failure, partial_group, failed_group


async def _open_alert_exists(
    db: AsyncSession, feed: _ConfiguredFeed, expected: date | None
) -> bool:
    if expected is None or feed.schema_id is None or feed.schema_version is None:
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
    db: AsyncSession,
    feed: _ConfiguredFeed,
    expected: date | None,
) -> FreshnessFeed:
    last_run, last_fetched_at, latest, failure, partial_group, failed_group = await _runs_for_feed(
        db, feed, expected
    )
    alert_open = await _open_alert_exists(db, feed, expected)
    config_error = "; ".join(feed.configuration_errors) if feed.configuration_errors else None

    if config_error:
        # A malformed policy is not evidence that a feed is late or fresh.
        state: FreshnessStatus = "not_due"
    elif last_run is None:
        state = "never_received"
    elif expected is None:
        state = "not_due"
    elif failed_group or (
        last_run is not None
        and last_run.status == "failed"
        and (last_run.batch_data_date is None or last_run.batch_data_date >= expected)
    ):
        state = "failed"
    elif latest is not None and latest.batch_data_date >= expected:
        state = "fresh"
    elif partial_group:
        state = "partial"
    else:
        state = "late"

    return FreshnessFeed(
        dataset_key=feed.dataset.dataset_key,
        source=feed.source,
        schema_id=feed.schema_id,
        schema_version=feed.schema_version,
        expected_data_date=expected,
        latest_successful_data_date=latest.batch_data_date if latest else None,
        last_fetched_at=ensure_utc(last_fetched_at) if last_fetched_at else None,
        last_completed_at=ensure_utc(latest.completed_at)
        if latest and latest.completed_at
        else None,
        last_run_id=last_run.run_id if last_run else None,
        total_records=last_run.total_records if last_run else None,
        success_records=last_run.success_records if last_run else None,
        failed_records=last_run.failed_records if last_run else None,
        policy_outcome=last_run.policy_outcome if last_run else None,
        open_missing_delivery_alert=alert_open,
        last_failure_code=failure.failure_code if failure else None,
        configuration_error=config_error,
        status=state,
    )


def _definition_from_control(row: SchedulerControl) -> _SchedulerDefinition:
    keys = tuple(scheduler_dataset_keys(row))
    try:
        slot_id = normalize_slot_id(row.slot_id)
    except ValueError:
        slot_id = row.slot_id
    return _SchedulerDefinition(
        scheduler_key=normalize_scheduler_key(row.scheduler_key),
        provider=row.provider,
        providers=(row.provider,),
        dataset_keys=keys,
        slot_id=slot_id,
        scheduled_local_time=row.scheduled_local_time,
        timezone=row.timezone,
        desired_state=cast(Literal["running", "stopped"], row.desired_state),
        observed_state=cast(Literal["running", "stopped"], row.observed_state),
        revision=row.revision,
        last_heartbeat_at=row.last_heartbeat_at,
        last_cycle_started_at=row.last_cycle_started_at,
        last_cycle_completed_at=row.last_cycle_completed_at,
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _feed_configuration(
    dataset: DatasetRegistry, providers: tuple[str, ...]
) -> list[_ConfiguredFeed]:
    config = dataset.config if isinstance(dataset.config, dict) else {}
    errors: list[str] = []
    schema_id_value = config.get("schema_id")
    schema_version_value = config.get("current_schema_version")
    schema_id = schema_id_value if isinstance(schema_id_value, str) and schema_id_value else None
    schema_version = schema_version_value if type(schema_version_value) is int else None
    if schema_id is None:
        errors.append("schema_id_missing")
    if schema_version is None:
        errors.append("schema_version_missing")

    expectation: DeliveryExpectation | None = None
    raw_expectation = config.get("delivery_expectation")
    if raw_expectation is None:
        errors.append("delivery_expectation_missing")
    elif not isinstance(raw_expectation, dict):
        errors.append("delivery_expectation_invalid")
    else:
        try:
            expectation = DeliveryExpectation.model_validate(raw_expectation)
        except ValueError:
            errors.append("delivery_expectation_invalid")
    feeds: list[_ConfiguredFeed] = []
    for source in providers:
        source_errors = list(errors)
        effective = expectation.for_source(source) if expectation is not None else None
        if effective is not None and effective.latest_date is None:
            source_errors.append("latest_date_policy_not_configured")
        feeds.append(
            _ConfiguredFeed(
                dataset=dataset,
                source=source,
                schema_id=schema_id,
                schema_version=schema_version,
                expectation=effective,
                configuration_errors=tuple(source_errors),
            )
        )
    return feeds


async def _build_scheduler_freshness(
    db: AsyncSession,
    definition: _SchedulerDefinition,
    datasets: dict[str, DatasetRegistry],
    evaluated_at: datetime,
) -> MarketFreshness:
    configuration_errors: list[str] = []
    if not definition.dataset_keys:
        configuration_errors.append("scheduler_dataset_mapping_missing")
    try:
        ZoneInfo(definition.timezone)
    except (TypeError, ZoneInfoNotFoundError, ValueError):
        configuration_errors.append("scheduler_timezone_invalid")

    feed_configs: list[_ConfiguredFeed] = []
    markets: list[str] = []
    for dataset_key in definition.dataset_keys:
        dataset = datasets.get(dataset_key)
        if dataset is None:
            configuration_errors.append(f"dataset_not_found:{dataset_key}")
            continue
        markets.append(dataset.market)
        if not dataset.is_active:
            configuration_errors.append(f"dataset_inactive:{dataset_key}")
        feed_configs.extend(_feed_configuration(dataset, definition.providers))

    expected_by_feed: list[date | None] = []
    feed_rows: list[FreshnessFeed] = []
    for feed in feed_configs:
        expected: date | None = None
        feed_errors = list(feed.configuration_errors)
        effective_expectation = (
            feed.expectation.for_source(feed.source) if feed.expectation is not None else None
        )
        if effective_expectation is not None and effective_expectation.latest_date is not None:
            try:
                deadline = effective_expectation.missing_delivery.deadline_local_time
                resolver_kwargs: dict[str, Any] = {
                    "strict_current_session": (
                        effective_expectation.delivery_mode.value == "sequenced_snapshot"
                        or deadline is not None
                    )
                }
                if deadline is not None:
                    resolver_kwargs["current_session_deadline"] = deadline
                    if effective_expectation.schedule is not None:
                        resolver_kwargs["operational_deadline_timezone"] = (
                            effective_expectation.schedule.timezone
                        )
                        resolver_kwargs["target_date_lag_days"] = (
                            effective_expectation.schedule.target_date_lag_days
                        )
                expected, reason = await resolve_expected_data_date(
                    db,
                    effective_expectation.latest_date,
                    evaluated_at,
                    **resolver_kwargs,
                )
            except (ValueError, ZoneInfoNotFoundError):
                reason = "latest_date_policy_invalid"
            if expected is None:
                feed_errors.append(reason or "latest_date_policy_unavailable")
        configured = _ConfiguredFeed(
            dataset=feed.dataset,
            source=feed.source,
            schema_id=feed.schema_id,
            schema_version=feed.schema_version,
            expectation=effective_expectation,
            configuration_errors=tuple(feed_errors),
        )
        expected_by_feed.append(expected)
        feed_rows.append(await _feed_freshness(db, configured, expected))
        configuration_errors.extend(f"{feed.dataset.dataset_key}:{error}" for error in feed_errors)

    states = [feed.status for feed in feed_rows]
    fresh_count = states.count("fresh")
    late_count = sum(value in {"late", "partial", "never_received"} for value in states)
    configured_errors = [feed.configuration_error for feed in feed_rows if feed.configuration_error]
    if not feed_rows:
        group_status: FreshnessStatus = "not_due"
    elif configured_errors and not any(
        feed.status in {"fresh", "late", "failed"} for feed in feed_rows
    ):
        group_status = "not_due"
    elif states and all(value == "never_received" for value in states):
        group_status = "never_received"
    elif all(value == "not_due" for value in states):
        group_status = "not_due"
    elif "failed" in states:
        group_status = "failed"
    elif "partial" in states:
        group_status = "partial"
    elif fresh_count == len(feed_rows):
        group_status = "fresh"
    elif fresh_count:
        group_status = "partial"
    else:
        group_status = "late"

    coverage_dates = [
        feed.latest_successful_data_date
        for feed in feed_rows
        if feed.latest_successful_data_date is not None
    ]
    coverage = min(coverage_dates) if len(coverage_dates) == len(feed_rows) and feed_rows else None
    expected_dates = [value for value in expected_by_feed if value is not None]
    expected_data_date = min(expected_dates) if expected_dates else None
    last_completed = [feed.last_completed_at for feed in feed_rows if feed.last_completed_at]
    last_fetched = [feed.last_fetched_at for feed in feed_rows if feed.last_fetched_at]
    all_current = bool(
        feed_rows
        and expected_dates
        and all(
            feed.configuration_error is None
            and feed.expected_data_date is not None
            and feed.latest_successful_data_date is not None
            and feed.latest_successful_data_date >= feed.expected_data_date
            for feed in feed_rows
        )
    )
    card_errors = tuple(dict.fromkeys(configuration_errors))
    market = (
        markets[0] if markets and len(set(markets)) == 1 else (markets[0] if markets else "UNKNOWN")
    )
    return MarketFreshness(
        market=market,
        scheduler_key=definition.scheduler_key,
        provider=definition.provider,
        dataset_keys=definition.dataset_keys,
        slot_id=definition.slot_id,
        scheduled_local_time=definition.scheduled_local_time,
        timezone=definition.timezone,
        desired_state=definition.desired_state,
        observed_state=definition.observed_state,
        revision=definition.revision,
        last_heartbeat_at=ensure_utc(definition.last_heartbeat_at)
        if definition.last_heartbeat_at
        else None,
        last_cycle_started_at=ensure_utc(definition.last_cycle_started_at)
        if definition.last_cycle_started_at
        else None,
        last_cycle_completed_at=ensure_utc(definition.last_cycle_completed_at)
        if definition.last_cycle_completed_at
        else None,
        last_error=definition.last_error,
        heartbeat_age_seconds=_heartbeat_age(evaluated_at, definition.last_heartbeat_at),
        configuration_status="error" if card_errors else "ready",
        configuration_errors=card_errors,
        status=group_status,
        expected_data_date=expected_data_date,
        coverage_data_date=coverage,
        last_fetched_at=max(last_fetched, default=None),
        last_successful_update_at=max(last_completed, default=None),
        last_complete_at=max(last_completed, default=None) if all_current else None,
        next_scheduled_at=_next_scheduled_at(
            evaluated_at,
            definition.scheduled_local_time,
            definition.timezone,
        ),
        feed_count=len(feed_rows),
        fresh_feed_count=fresh_count,
        late_feed_count=late_count,
        feeds=tuple(feed_rows),
    )


async def list_market_freshness(
    db: AsyncSession,
    *,
    market: str | None = None,
    slot_id: str | None = None,
    status: FreshnessStatus | None = None,
    now: datetime | None = None,
) -> list[MarketFreshness]:
    """Build a scheduler-backed projection without mutating registry state."""
    evaluated_at = ensure_utc(now or utc_now())
    normalized_slot_filter = normalize_slot_id(slot_id) if slot_id else None
    controls = await list_scheduler_controls(db)
    if not controls:
        return []
    definitions = [_definition_from_control(row) for row in controls]
    all_keys = sorted({key for definition in definitions for key in definition.dataset_keys})
    datasets: dict[str, DatasetRegistry] = {}
    if all_keys:
        datasets = {
            row.dataset_key: row
            for row in (
                await db.execute(
                    select(DatasetRegistry).where(DatasetRegistry.dataset_key.in_(all_keys))
                )
            )
            .scalars()
            .all()
        }

    results: list[MarketFreshness] = []
    for definition in definitions:
        if normalized_slot_filter and definition.slot_id != normalized_slot_filter:
            continue
        mapped_markets = {
            datasets[key].market.upper() for key in definition.dataset_keys if key in datasets
        }
        if market and mapped_markets and market.upper() not in mapped_markets:
            continue
        if market and not mapped_markets and controls:
            # An invalid scheduler still appears for an unfiltered view, but a
            # market filter cannot safely infer its market.
            continue
        row = await _build_scheduler_freshness(db, definition, datasets, evaluated_at)
        if status and row.status != status:
            continue
        results.append(row)
    return sorted(
        results,
        key=lambda item: (
            item.market,
            item.timezone,
            item.scheduled_local_time,
            item.slot_id,
            item.scheduler_key,
        ),
    )
