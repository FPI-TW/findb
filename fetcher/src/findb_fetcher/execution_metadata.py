"""Packaged execution metadata, independent of daily clocks and Source schedules."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from findb_fetcher.config import app_environment

if TYPE_CHECKING:
    from findb_fetcher.scheduler_state import ScheduledJob

METADATA_PATH = Path(__file__).parent / "data/execution_descriptors.v1.json"
CONFIGS_DIR = Path("/app/configs")


class ExecutionMetadataError(ValueError):
    """The installed descriptor cannot support the requested historical operation."""


@dataclass(frozen=True, slots=True)
class ExecutionDescriptor:
    # Kept solely as an existing journal identity, not a schedule or authorization.
    schedule_id: str
    provider: str
    dataset_key: str
    market: str
    slot_id: str
    universe_file: Path
    outputsize: int
    max_attempts: int
    retry_base_seconds: int
    retry_max_seconds: int
    lease_seconds: int
    poll_interval_seconds: int
    wait_timeout_seconds: int
    max_credits_per_run: int
    max_records_per_run: int


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ExecutionMetadataError("Duplicate execution metadata key")
        result[key] = value
    return result


def historical_feeds(provider: str) -> tuple[dict[str, Any], ...]:
    """Return only static adapter metadata; Backend still authorizes every work item."""
    app_environment()  # Reject invalid/conflicting deployment identity even without a clock.
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
        if provider not in providers:
            raise ExecutionMetadataError("Unknown historical adapter")
        feeds = tuple(
            row
            for row in value["feeds"]
            if row["provider"] == provider
            and row["lifecycle"] == "active"
            and row["capabilities"]["source"] is True
            and row["capabilities"]["historical"] in {"declared", "executable"}
        )
        if not feeds:
            raise ExecutionMetadataError("Unsupported historical adapter scope")
        return feeds
    except (OSError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ExecutionMetadataError("Invalid installed execution metadata") from exc


def historical_feed(provider: str, dataset_key: str) -> dict[str, Any]:
    feed = next(
        (row for row in historical_feeds(provider) if row["dataset_key"] == dataset_key), None
    )
    if feed is None:
        raise ExecutionMetadataError("Unsupported historical adapter scope")
    return feed


def historical_execution(
    provider: str, dataset_key: str, *, universe_file: Path | None = None
) -> ExecutionDescriptor:
    feed = historical_feed(provider, dataset_key)
    profile = feed["historical_execution"]
    if not isinstance(profile, dict):
        raise ExecutionMetadataError("Historical adapter has no bundle execution descriptor")
    settings = dict(profile[app_environment()])
    reference = settings.pop("universe_file")
    if not isinstance(reference, str) or Path(reference).name != reference:
        raise ExecutionMetadataError("Invalid historical universe reference")
    if any(type(v) is not int or v <= 0 for v in settings.values()):
        raise ExecutionMetadataError("Invalid historical execution limits")
    if universe_file is None:
        # Existing FinLab universe override remains shared by runner and estimator.
        override = os.getenv("FETCHER_FINLAB_UNIVERSE_FILE") if provider == "finlab" else None
        universe_file = Path(override) if override else CONFIGS_DIR / reference
    slot = feed["delivery"]["slot_id"]
    return ExecutionDescriptor(
        schedule_id=f"{provider}_{slot}_{dataset_key}",
        provider=provider,
        dataset_key=dataset_key,
        market=feed["market"],
        slot_id=slot,
        universe_file=universe_file,
        **settings,
    )


def historical_delivery_metadata(
    execution: ExecutionDescriptor, job: ScheduledJob
) -> dict[str, str]:
    # This contract field anchors the requested date, never a daily trigger/gate.
    # Already-prepared deliveries bypass this function and keep their original bytes.
    target = job.target_data_date
    anchor = datetime.combine(target, time.min, tzinfo=timezone.utc)
    return {
        "slot_id": execution.slot_id,
        "scheduled_for": anchor.isoformat().replace("+00:00", "Z"),
        "target_data_date": target.isoformat(),
        "work_item_id": job.work_item or job.symbol,
    }
