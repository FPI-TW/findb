"""Packaged execution metadata, independent of daily clocks and Source schedules."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from findb_fetcher.config import app_environment

METADATA_PATH = Path(__file__).parent / "data/execution_descriptors.v1.json"
# A provider's execution lane: its own Source FIFO, owner, consumer and local files.
# The default lane keeps every pre-lane name; historical work always runs there.
DEFAULT_LANE = "default"


_LANE_NAME = re.compile(r"[a-z][a-z0-9_]{0,29}")


class ExecutionMetadataError(ValueError):
    """The installed descriptor cannot support the requested historical operation."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ExecutionMetadataError("Duplicate execution metadata key")
        result[key] = value
    return result


def _installed_metadata() -> dict[str, Any]:
    """Load and verify the packaged projection; never reads environment or clocks."""
    try:
        value = json.loads(METADATA_PATH.read_text(), object_pairs_hook=_unique_object)
        if set(value) != {"schema_version", "projection_sha256", "providers", "feeds"}:
            raise ExecutionMetadataError("Invalid execution metadata root")
        digest = value.pop("projection_sha256")
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ExecutionMetadataError("Unsupported execution metadata schema")
        actual = hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if digest != actual:
            raise ExecutionMetadataError("Execution metadata digest differs")
        providers = [row["provider"] for row in value["providers"]]
        identities = [(row["provider"], row["dataset_key"]) for row in value["feeds"]]
        if len(set(providers)) != len(providers) or len(set(identities)) != len(identities):
            raise ExecutionMetadataError("Duplicate execution metadata identity")
        if not providers or any(type(p) is not str or not p for p in providers):
            raise ExecutionMetadataError("Invalid execution metadata provider")
        return value
    except (OSError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ExecutionMetadataError("Invalid installed execution metadata") from exc


def provider_ids() -> tuple[str, ...]:
    """Every provider this package carries adapter metadata for, in published order."""
    return tuple(row["provider"] for row in _installed_metadata()["providers"])


def historical_provider_ids() -> tuple[str, ...]:
    """Providers with at least one active historical feed; adapter choice only."""
    value = _installed_metadata()
    try:
        return tuple(
            provider["provider"]
            for provider in value["providers"]
            if any(
                row["provider"] == provider["provider"]
                and row["lifecycle"] == "active"
                and row["capabilities"]["source"] is True
                and row["capabilities"]["historical"] in {"declared", "executable"}
                for row in value["feeds"]
            )
        )
    except (TypeError, KeyError) as exc:
        raise ExecutionMetadataError("Invalid installed execution metadata") from exc


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def provider_descriptor(provider: str) -> dict[str, Any]:
    """Return one provider's static, secret-free settings (names and limits only)."""
    row = next(
        (row for row in _installed_metadata()["providers"] if row["provider"] == provider), None
    )
    if row is None:
        raise ExecutionMetadataError("unsupported provider")
    try:
        names = row["credential_env_names"]
        limits = row["platform_limits"]
        rate = limits["requests_per_minute"]
        if (
            set(row) != {"provider", "credential_env_names", "platform_limits"}
            or set(limits) != {"requests_per_minute", "max_response_bytes"}
            or not isinstance(names, list)
            or any(type(name) is not str or not name for name in names)
            or len(set(names)) != len(names)
            or not (rate is None or _positive_int(rate))
            or not _positive_int(limits["max_response_bytes"])
        ):
            raise ExecutionMetadataError("Invalid provider descriptor")
    except (TypeError, KeyError) as exc:
        raise ExecutionMetadataError("Invalid provider descriptor") from exc
    return row


def provider_platform_limits(provider: str) -> dict[str, Any]:
    return dict(provider_descriptor(provider)["platform_limits"])


def provider_feeds(provider: str | None = None) -> tuple[dict[str, Any], ...]:
    """Return only static adapter metadata; Backend still authorizes every work item."""
    app_environment()  # Reject invalid/conflicting deployment identity even without a clock.
    value = _installed_metadata()
    try:
        providers = [row["provider"] for row in value["providers"]]
        if provider is not None and provider not in providers:
            raise ExecutionMetadataError("Unknown historical adapter")
        feeds = tuple(
            row
            for row in value["feeds"]
            if (provider is None or row["provider"] == provider)
            and row["lifecycle"] == "active"
            and row["capabilities"]["source"] is True
        )
        if not feeds:
            raise ExecutionMetadataError("Unsupported historical adapter scope")
        return feeds
    except (TypeError, KeyError) as exc:
        raise ExecutionMetadataError("Invalid installed execution metadata") from exc


def dated_universe(provider: str, dataset_key: str) -> bool:
    """Whether a plan needs a universe built from its trade date's own observation.

    A ``refreshed`` feed's universe comes from scheduled maintenance instead.
    """
    feed = next(
        (row for row in provider_feeds(provider) if row["dataset_key"] == dataset_key), None
    )
    if feed is None or feed.get("universe_mode") not in {"refreshed", "dated"}:
        raise ExecutionMetadataError("Unknown feed universe mode")
    return feed["universe_mode"] == "dated"


def delivers_unlisted(provider: str, dataset_key: str) -> bool:
    """Whether the feed also delivers served symbols outside its frozen list."""
    feed = next(
        (row for row in provider_feeds(provider) if row["dataset_key"] == dataset_key), None
    )
    return feed is not None and feed.get("unlisted_symbols") == "deliver"


def acquisition_feeds(provider: str | None = None) -> tuple[dict[str, Any], ...]:
    return tuple(
        row for row in provider_feeds(provider) if row["capabilities"]["acquisition"] is True
    )


def feed_lane(feed: dict[str, Any]) -> str:
    """The packaged lane of one descriptor feed; a feed naming none runs in the default lane."""
    lane = feed.get("lane", DEFAULT_LANE)
    if type(lane) is not str or _LANE_NAME.fullmatch(lane) is None:
        raise ExecutionMetadataError("Invalid execution lane")
    return lane


def provider_lanes(provider: str) -> tuple[str, ...]:
    """The lanes this image runs a provider's daily acquisition in, default first."""
    lanes = {feed_lane(feed) for feed in acquisition_feeds(provider)} | {DEFAULT_LANE}
    return tuple(sorted(lanes, key=lambda lane: (lane != DEFAULT_LANE, lane)))


def lane_feeds(provider: str, lane: str = DEFAULT_LANE) -> tuple[dict[str, Any], ...]:
    """The provider's acquiring feeds that run in ``lane``; other lanes' consumers own the rest."""
    if lane not in provider_lanes(provider):
        raise ExecutionMetadataError("Unknown execution lane")
    return tuple(feed for feed in acquisition_feeds(provider) if feed_lane(feed) == lane)


def historical_feeds(provider: str) -> tuple[dict[str, Any], ...]:
    feeds = tuple(
        row
        for row in provider_feeds(provider)
        if row["capabilities"]["historical"] in {"declared", "executable"}
    )
    if not feeds:
        raise ExecutionMetadataError("Unsupported historical adapter scope")
    return feeds


def historical_feed(provider: str, dataset_key: str) -> dict[str, Any]:
    feed = next(
        (row for row in historical_feeds(provider) if row["dataset_key"] == dataset_key), None
    )
    if feed is None:
        raise ExecutionMetadataError("Unsupported historical adapter scope")
    return feed
