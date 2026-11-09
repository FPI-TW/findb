"""Exchange-local schedule interpretation shared by Source and Admin projections."""

import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.execution_profiles import ExecutionProfile
from app.provider_catalog import CompletionDeadline, FeedDefinition
from app.schemas.execution_config import ExecutionConfigResponse, FeedSchedule, FeedScheduleView
from app.services.provider_metadata import acquisition_feed, acquisition_feeds


def describe_feed(feed: FeedDefinition) -> FeedSchedule:
    if (
        not feed.capabilities.acquisition
        or feed.schedule is None
        or feed.completion_deadline is None
        or feed.publication_retry_window_seconds is None
    ):
        raise ValueError("Feed has no acquisition schedule")
    return FeedSchedule(
        provider=feed.provider,
        dataset_key=feed.dataset_key,
        market=feed.market,
        calendar_market=feed.calendar_market,
        asset_class=feed.asset_class,
        frequency=feed.frequency,
        contract=feed.contract,
        scheduled_time=feed.schedule.local_time,
        source_timezone=feed.schedule.timezone,
        target_date_lag_days=feed.schedule.target_date_lag_days,
        publication_retry_window_seconds=feed.publication_retry_window_seconds,
        completion_deadline=feed.completion_deadline,
        bounded_monitor=feed.bounded_monitor,
    )


def next_scheduled_at(schedule: FeedSchedule, now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("Schedule clock must include timezone")
    zone = ZoneInfo(schedule.source_timezone)
    local = now.astimezone(zone)
    candidate = datetime.combine(local.date(), time.fromisoformat(schedule.scheduled_time), zone)
    if candidate <= local:
        candidate = datetime.combine(
            local.date() + timedelta(days=1), time.fromisoformat(schedule.scheduled_time), zone
        )
    return candidate.astimezone(timezone.utc)


def deadline_at(policy: CompletionDeadline, trade_date: date) -> datetime:
    return datetime.combine(
        trade_date + timedelta(days=policy.trade_date_day_offset),
        time.fromisoformat(policy.local_time),
        ZoneInfo(policy.timezone),
    ).astimezone(timezone.utc)


def feed_schedule_view(provider: str, dataset_key: str, now: datetime) -> FeedScheduleView | None:
    feed = acquisition_feed(provider, dataset_key)
    if feed is None:
        return None  # Historical / retired DB rows do not become new acquisition options.
    schedule = describe_feed(feed)
    return FeedScheduleView(
        **schedule.model_dump(), next_scheduled_at=next_scheduled_at(schedule, now)
    )


def execution_config(
    provider: str, dataset_keys: list[str], profile: ExecutionProfile
) -> ExecutionConfigResponse:
    # This list is the entire authorized snapshot, not a partial update.
    feeds = [
        describe_feed(feed)
        for feed in acquisition_feeds()
        if feed.provider == provider and feed.dataset_key in dataset_keys
    ]
    feeds.sort(key=lambda feed: feed.dataset_key)
    if not feeds or {f.dataset_key for f in feeds} != set(dataset_keys):
        raise ValueError("Source acquisition scope has no complete schedule")
    body = {
        "schema_version": 1,
        "environment": get_settings().APP_ENVIRONMENT,
        "execution_profile": profile,
        "provider": provider,
        "feeds": [feed.model_dump(mode="json") for feed in feeds],
    }
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ExecutionConfigResponse.model_validate({**body, "schedule_digest": digest})
