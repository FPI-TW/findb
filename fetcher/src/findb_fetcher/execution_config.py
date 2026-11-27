"""One-round Backend schedule snapshots; no disk cache or local clock fallback."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from findb_fetcher.execution_metadata import DEFAULT_LANE, acquisition_feeds, feed_lane

# Implemented dispatch paths, not configuration. Adding metadata cannot install
# a provider adapter or silently fall through to another provider's SDK.
_IMPLEMENTED_ADAPTERS = frozenset(
    {
        ("twelve_data", "us_equity_eod"),
        ("factset", "hk_equity_eod"),
        ("finlab", "tw_equity_eod"),
        ("finlab", "tw_etf_eod"),
        ("finlab", "tw_equity_bar_1m"),
        ("finlab", "tw_etf_bar_1m"),
        ("finlab", "tw_equity_tick"),
        ("finlab", "tw_etf_tick"),
        ("taifex", "tw_futures_eod"),
        ("hkex", "hk_equity_official_eod"),
        ("hkex", "hk_index_constituents"),
        ("hkex", "hk_index_changes"),
        ("fmp", "hk_index_eod"),
        ("fmp", "us_index_eod"),
        ("fmp", "us_dollar_index_eod"),
        ("twse", "tw_twse_index_eod"),
        ("tpex", "tw_tpex_index_eod"),
        ("twse", "tw_twse_institutional_summary"),
        ("twse", "tw_twse_institutional_stock"),
        ("tpex", "tw_tpex_institutional_summary"),
        ("tpex", "tw_tpex_institutional_stock"),
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
    lane: str = DEFAULT_LANE,
) -> ExecutionSnapshot:
    """Validate the snapshot, then expose the feeds this image installs and Source publishes.

    Backend and Fetcher deploy separately, so their catalogs may differ for a while:
    a feed published by a newer Backend is skipped here (this image cannot run it),
    and an installed feed an older Backend does not publish yet is simply not
    planned. Neither stops the feeds both sides know. A maintenance credential may
    also publish a narrower scope.

    Only ``lane``'s feeds are exposed: the packaged descriptors, never the
    published config, say which lane a feed runs in, and another lane's consumer
    validates and plans the rest.
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
        if (
            not isinstance(feed, dict)
            or feed.get("provider") != provider
            or not isinstance(feed.get("dataset_key"), str)
        ):
            raise ValueError("Invalid execution feed shape")
        key = feed["dataset_key"]
        if key in seen:
            raise ValueError("Unknown or duplicate execution feed")
        seen.add(key)
        if key not in expected:
            # A newer Backend's feed: not executable by this image, and its shape
            # may be newer too. It waits for the Fetcher release that installs it.
            continue
        if feed_lane(expected[key]) != lane:
            # Another lane's consumer owns this feed.
            continue
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
        if (provider, key) not in _IMPLEMENTED_ADAPTERS:
            raise ValueError("Unknown or duplicate execution feed")
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
    if not resolved:
        raise ValueError("Execution config publishes no installed feed")
    return ExecutionSnapshot(digest=digest, feeds=tuple(resolved))
