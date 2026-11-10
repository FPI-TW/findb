"""One-round Backend schedule snapshots; no disk cache or local clock fallback."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from findb_fetcher.execution_metadata import acquisition_feeds

# Implemented dispatch paths, not configuration. Adding metadata cannot install
# a provider adapter or silently fall through to another provider's SDK.
_IMPLEMENTED_ADAPTERS = frozenset(
    {
        ("twelve_data", "us_equity_eod"),
        ("factset", "hk_equity_eod"),
        ("finlab", "tw_equity_eod"),
        ("finlab", "tw_etf_eod"),
        ("taifex", "tw_futures_eod"),
    }
)


@dataclass(frozen=True)
class ExecutionSnapshot:
    digest: str
    feeds: tuple[dict[str, Any], ...]


def _clock(value: object) -> None:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value) is None
    ):
        raise ValueError("Invalid published clock")


def _integer(value: object, minimum: int, maximum: int | None = None) -> None:
    if type(value) is not int or value < minimum or maximum is not None and value > maximum:
        raise ValueError("Invalid published execution bound")


def _deadline(value: dict) -> None:
    if set(value) != {"timezone", "local_time", "trade_date_day_offset"}:
        raise ValueError("Invalid published deadline")
    ZoneInfo(value["timezone"])
    _clock(value["local_time"])
    _integer(value["trade_date_day_offset"], 0, 366)


def resolve_execution_config(
    value: dict[str, Any],
    *,
    provider: str,
    environment: str,
    profile: str,
    complete_scope: bool = True,
) -> ExecutionSnapshot:
    """Validate the complete snapshot before exposing any feed to the planner.

    Maintenance may hold a narrower credential scope; it still validates every
    published feed but does not require the full installed provider scope.
    """
    body = deepcopy(value)
    if set(body) != {
        "schema_version",
        "environment",
        "execution_profile",
        "provider",
        "schedule_digest",
        "feeds",
    }:
        raise ValueError("Invalid execution config shape")
    if (
        type(body["schema_version"]) is not int
        or body["schema_version"] != 1
        or body["environment"] != environment
        or body["provider"] != provider
        or profile != "full_market"
        or body["execution_profile"] != profile
    ):
        raise ValueError("Execution config identity or schema differs")
    digest = body.pop("schedule_digest")
    if (
        digest
        != hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    ):
        raise ValueError("Execution config digest differs")
    expected = {f["dataset_key"]: f for f in acquisition_feeds(provider)}
    feeds = body["feeds"]
    if not isinstance(feeds, list) or not feeds:
        raise ValueError("Empty execution config scope")
    seen = set()
    resolved = []
    for feed in feeds:
        if set(feed) != {
            "provider",
            "dataset_key",
            "market",
            "calendar_market",
            "asset_class",
            "frequency",
            "contract",
            "scheduled_time",
            "source_timezone",
            "target_date_lag_days",
            "publication_retry_window_seconds",
            "completion_deadline",
            "bounded_monitor",
        }:
            raise ValueError("Invalid execution feed shape")
        key = feed["dataset_key"]
        if key in seen or key not in expected or (provider, key) not in _IMPLEMENTED_ADAPTERS:
            raise ValueError("Unknown or duplicate execution feed")
        seen.add(key)
        installed = expected[key]
        if any(
            json.dumps(feed[k], sort_keys=True) != json.dumps(installed[k], sort_keys=True)
            for k in (
                "provider",
                "market",
                "calendar_market",
                "asset_class",
                "frequency",
                "contract",
            )
        ):
            raise ValueError("Unknown adapter scope or contract")
        ZoneInfo(feed["source_timezone"])
        _clock(feed["scheduled_time"])
        _integer(feed["target_date_lag_days"], 0, 366)
        _integer(feed["publication_retry_window_seconds"], 1)
        _deadline(feed["completion_deadline"])
        monitor = feed["bounded_monitor"]
        if (
            set(monitor) != {"enabled", "deadline"}
            or type(monitor["enabled"]) is not bool
            or monitor["enabled"]
            and monitor["deadline"] is None
        ):
            raise ValueError("Invalid bounded monitor")
        if monitor["deadline"] is not None:
            _deadline(monitor["deadline"])
        resolved.append(
            {
                **feed,
                # Existing frozen work uses this name for the publication retry window.
                "completion_window_seconds": feed["publication_retry_window_seconds"],
                # Never used for snapshot comparison, clocks or due dates.
                "slot_id": installed["delivery"]["slot_id"],
            }
        )
    if complete_scope and seen != set(expected):
        raise ValueError("Incomplete execution config scope")
    return ExecutionSnapshot(digest=digest, feeds=tuple(resolved))
