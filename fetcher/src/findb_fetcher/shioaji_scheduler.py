"""Production-pilot recurring scheduler for the reviewed Shioaji universe.

This module deliberately keeps the provider and delivery implementation in the
reviewed staging coordinator.  The production pilot adds only the schedule
gate, published-calendar gate, production state identity, and strict Source
terminal-count acceptance around that coordinator.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as clock_time
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from findb_fetcher.client import PreparedDelivery, SourceAPIDeadlineExceeded
from findb_fetcher.config import ConfigError, FetcherConfig, MarketCalendarConfig
from findb_fetcher.contracts import ContractRegistry
from findb_fetcher.market_calendar import PublishedCalendarClient
from findb_fetcher.providers.shioaji import IsolatedShioajiGateway
from findb_fetcher.raw_storage import R2RawPayloadStore, RawStorageConfig, RawStorageError
from findb_fetcher.shioaji_staging import (
    Coordinator as _StagingCoordinator,
)
from findb_fetcher.shioaji_staging import (
    Result,
)
from findb_fetcher.shioaji_staging_state import (
    EXPECTED_COLUMNS,
    SCHEMA_VERSION,
    ShioajiStagingState,
    ShioajiStagingStateError,
)

TAIPEI = ZoneInfo("Asia/Taipei")
MAX_MANIFEST_BYTES = 16 * 1024
MAX_OUTPUT_BYTES = 4096
PILOT_UNIVERSE_ID = "shioaji_tw_pilot_v1"
PRODUCTION_UNIVERSE_PREFIX = "shioaji_tw50_"
PILOT_MARKET = "TW"
PILOT_POLL_SECONDS = 60
PILOT_DUE = clock_time(14, 30)
PILOT_CUTOFF = clock_time(17, 0)
PRODUCTION_STATE_ENV = "FETCHER_SHIOAJI_STATE_PATH"
DEFAULT_PRODUCTION_STATE_PATH = Path("/var/lib/findb-shioaji-fetcher/state.sqlite3")

# Keep this tuple independent from the staging identity and implementation.
# Sharing a universe ID or SQLite path is forbidden.
EXPECTED = (
    {
        "symbol": "2330",
        "dataset_key": "tw_equity_minute",
        "asset_class": "equity",
        "sequence": 1,
        "sequence_count": 1,
    },
    {
        "symbol": "0050",
        "dataset_key": "tw_etf_minute",
        "asset_class": "etf",
        "sequence": 1,
        "sequence_count": 3,
    },
    {
        "symbol": "0056",
        "dataset_key": "tw_etf_minute",
        "asset_class": "etf",
        "sequence": 2,
        "sequence_count": 3,
    },
    {
        "symbol": "006201",
        "dataset_key": "tw_etf_minute",
        "asset_class": "etf",
        "sequence": 3,
        "sequence_count": 3,
    },
)
GOVERNANCE = {
    "max_requests": 50,
    "rolling_seconds": 60,
    "max_attempts_per_sequence": 3,
    "cutoff": "17:00",
    "timezone": "Asia/Taipei",
}


class ProductionManifestError(ValueError):
    """The production pilot manifest is not the reviewed exact contract."""


class ProductionStateError(ValueError):
    """A production state path is missing or belongs to another run."""


class ProductionRuntimeError(ValueError):
    """Production-only runtime requirements are not satisfied."""


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProductionManifestError("duplicate JSON key")
        result[key] = value
    return result


def _strict_equal(expected: Any, actual: Any) -> bool:
    if type(expected) is not type(actual):
        return False
    if isinstance(expected, dict):
        return list(expected) == list(actual) and all(
            _strict_equal(expected[key], actual[key]) for key in expected
        )
    if isinstance(expected, list):
        return len(expected) == len(actual) and all(
            _strict_equal(left, right) for left, right in zip(expected, actual, strict=True)
        )
    return expected == actual


def load_manifest(path: Path) -> dict[str, Any]:
    """Load a reviewed pilot or production manifest and fail closed."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ProductionManifestError("unable to read production manifest") from exc
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ProductionManifestError("manifest exceeds limit")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ProductionManifestError) as exc:
        raise ProductionManifestError("invalid production manifest") from exc
    if not isinstance(value, dict):
        raise ProductionManifestError("manifest must be an object")
    if value.get("version") == 1:
        expected: dict[str, Any] = {
            "version": 1,
            "universe_id": PILOT_UNIVERSE_ID,
            "governance": GOVERNANCE,
            "sequences": [dict(item) for item in EXPECTED],
        }
        if not _strict_equal(expected, value):
            raise ProductionManifestError("manifest is not the reviewed production pilot")
        return value
    _validate_v2_manifest(value)
    return value


def _validate_v2_manifest(value: dict[str, Any]) -> None:
    expected_keys = {
        "version",
        "universe_id",
        "source_url",
        "effective_date",
        "source_sha256",
        "symbols_sha256",
        "governance",
        "sequences",
    }
    if set(value) != expected_keys or value.get("version") != 2:
        raise ProductionManifestError("production manifest v2 has invalid keys")
    identity = value.get("universe_id")
    if not isinstance(identity, str) or not identity.startswith(PRODUCTION_UNIVERSE_PREFIX):
        raise ProductionManifestError("production manifest v2 has invalid identity")
    if not isinstance(value.get("source_url"), str) or not value["source_url"].startswith(
        "https://"
    ):
        raise ProductionManifestError("production source_url must use HTTPS")
    try:
        date.fromisoformat(value["effective_date"])
    except (TypeError, ValueError) as exc:
        raise ProductionManifestError("production effective_date must be an ISO date") from exc
    for key in ("source_sha256", "symbols_sha256"):
        digest = value.get(key)
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ProductionManifestError(f"{key} must be a lowercase SHA-256 digest")
    if not _strict_equal(GOVERNANCE, value.get("governance")):
        raise ProductionManifestError("production governance does not match reviewed limits")
    sequences = value.get("sequences")
    if not isinstance(sequences, list) or len(sequences) != 53:
        raise ProductionManifestError("production manifest must contain 50 equities and 3 ETFs")
    expected_keys = {"symbol", "dataset_key", "asset_class", "sequence", "sequence_count"}
    if any(not isinstance(item, dict) or set(item) != expected_keys for item in sequences):
        raise ProductionManifestError("production sequence keys are invalid")
    equity = [item for item in sequences if item.get("dataset_key") == "tw_equity_minute"]
    etf = [item for item in sequences if item.get("dataset_key") == "tw_etf_minute"]
    if len(equity) != 50 or len(etf) != 3:
        raise ProductionManifestError("production dataset sequence sizes are invalid")
    if [item.get("symbol") for item in etf] != ["0050", "0056", "006201"]:
        raise ProductionManifestError("production ETF membership is invalid")
    for group, asset_class in ((equity, "equity"), (etf, "etf")):
        count = len(group)
        if any(
            item.get("asset_class") != asset_class
            or item.get("sequence") != index
            or item.get("sequence_count") != count
            or not isinstance(item.get("symbol"), str)
            for index, item in enumerate(group, start=1)
        ):
            raise ProductionManifestError("production sequence ordering is invalid")
    symbols = [f"{item['dataset_key']}:{item['symbol']}" for item in sequences]
    digest = hashlib.sha256(("\n".join(symbols) + "\n").encode()).hexdigest()
    if digest != value["symbols_sha256"]:
        raise ProductionManifestError("symbols_sha256 does not match normalized sequences")


def default_manifest_path() -> Path:
    configured = os.getenv("FETCHER_SHIOAJI_PRODUCTION_MANIFEST")
    if configured:
        return Path(configured)
    target = os.getenv("DEPLOYMENT_TARGET", "staging").strip().lower()
    filename = (
        "shioaji_tw50_2026_09_21.v2.json" if target == "production" else "shioaji_tw_pilot.v1.json"
    )
    container_path = Path("/app/configs") / filename
    if container_path.is_file():
        return container_path
    return Path(__file__).resolve().parents[2] / "configs" / filename


def default_state_path() -> Path:
    configured = os.getenv(PRODUCTION_STATE_ENV)
    return Path(configured) if configured else DEFAULT_PRODUCTION_STATE_PATH


def validate_production_state_path(
    path: Path,
    *,
    read_only: bool = False,
    require_existing: bool = False,
    manifest: dict[str, Any] | None = None,
) -> None:
    """Reject staging SQLite and malformed existing state without migration."""
    if not isinstance(path, Path):
        raise ProductionStateError("production state path is invalid")
    resolved = path.expanduser().resolve()
    if "staging" in resolved.name.lower():
        raise ProductionStateError("production state path must not be a staging path")
    configured_staging = os.getenv("FETCHER_SHIOAJI_STAGING_STATE_PATH")
    if configured_staging:
        try:
            if resolved == Path(configured_staging).expanduser().resolve():
                raise ProductionStateError("production state path must be distinct from staging")
        except OSError:
            raise ProductionStateError("production state path is invalid") from None
    if not resolved.name or len(str(resolved)) > 4096:
        raise ProductionStateError("production state path is invalid")
    if not resolved.exists():
        if require_existing:
            raise ProductionStateError("production state does not exist")
        if read_only:
            parent = resolved.parent
            while not parent.exists() and parent != parent.parent:
                parent = parent.parent
            if not parent.is_dir() or not os.access(parent, os.W_OK):
                raise ProductionStateError("production state parent is not writable")
            return
        if not resolved.parent.exists() or not resolved.parent.is_dir():
            return
        return
    if not resolved.is_file():
        raise ProductionStateError("production state path is not a file")
    _inspect_existing_state(resolved, manifest or load_manifest(default_manifest_path()))


def _inspect_existing_state(path: Path, manifest: dict[str, Any]) -> None:
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            db.execute("PRAGMA query_only = ON")
            quick_check = [row[0] for row in db.execute("PRAGMA quick_check").fetchall()]
            if quick_check != ["ok"]:
                raise ProductionStateError("production state integrity check failed")
            tables = {
                str(row[0])
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            required = set(EXPECTED_COLUMNS)
            if not required.issubset(tables):
                raise ProductionStateError("production state schema is incompatible")
            schema_row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if schema_row != (SCHEMA_VERSION,):
                raise ProductionStateError("production state schema is incompatible")
            identities = {
                str(row[0]) for row in db.execute("SELECT DISTINCT universe_id FROM daily_updates")
            }
            universe_id = str(manifest["universe_id"])
            if any(identity != universe_id for identity in identities):
                raise ProductionStateError("production state identity does not match manifest")
            expected_sequences = {
                (
                    item["symbol"],
                    item["dataset_key"],
                    item["sequence"],
                    item["sequence_count"],
                )
                for item in manifest["sequences"]
            }
            for (daily_id,) in db.execute("SELECT daily_id FROM daily_updates"):
                actual_sequences = set(
                    db.execute(
                        "SELECT symbol,dataset_key,sequence_no,sequence_count "
                        "FROM snapshot_sequences WHERE daily_id=?",
                        (daily_id,),
                    ).fetchall()
                )
                if actual_sequences != expected_sequences:
                    raise ProductionStateError("production state plan identity changed")
    except ProductionStateError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise ProductionStateError("production state cannot be inspected") from exc


def open_production_state(
    path: Path, manifest: dict[str, Any] | None = None
) -> ShioajiStagingState:
    validate_production_state_path(path, manifest=manifest)
    try:
        return ShioajiStagingState(path)
    except ShioajiStagingStateError as exc:
        raise ProductionStateError("production state is incompatible") from exc


def _simulation_is_true() -> bool:
    value = os.getenv("SHIOAJI_SIMULATION")
    return value is None or value == "true"


def validate_production_runtime() -> tuple[FetcherConfig, MarketCalendarConfig, RawStorageConfig]:
    """Validate credentials/configuration without constructing network clients."""
    if not _simulation_is_true():
        raise ProductionRuntimeError("SHIOAJI_SIMULATION=true is required")
    if (
        not os.getenv("SHIOAJI_API_KEY", "").strip()
        or not os.getenv("SHIOAJI_SECRET_KEY", "").strip()
    ):
        raise ProductionRuntimeError("Shioaji production credentials are required")
    try:
        fetcher = FetcherConfig.from_env()
        calendar = MarketCalendarConfig.from_env()
        raw = RawStorageConfig.from_env()
    except (ConfigError, RawStorageError) as exc:
        raise ProductionRuntimeError("production runtime configuration is invalid") from exc
    return fetcher, calendar, raw


def validate_contracts(contracts_dir: Path) -> ContractRegistry:
    try:
        registry = ContractRegistry(contracts_dir)
        registry.get("market_minute", 1)
        return registry
    except Exception as exc:
        raise ProductionRuntimeError("market_minute contract is unavailable") from exc


class Calendar(Protocol):
    def get_day(self, market: str, value: date) -> tuple[Any, int]: ...


@dataclass(frozen=True, slots=True)
class SchedulerRun:
    """Bounded result for one production scheduler poll."""

    target_date: date
    status: str
    results: tuple[Result, ...] = ()
    skip_reason: str | None = None
    calendar_revision: int | None = None

    @property
    def retryable(self) -> bool:
        return any(result.retryable for result in self.results)


class ProductionCoordinator(_StagingCoordinator):
    """Coordinator with production retry exhaustion and Source count gates."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        manifest = kwargs.get("manifest")
        if manifest is None and len(args) > 1:
            manifest = args[1]
        if not isinstance(manifest, dict) or not _coordinator_universe_id(
            manifest.get("universe_id")
        ):
            raise ProductionManifestError("production coordinator requires reviewed identity")
        kwargs["terminal_on_exhaustion"] = True
        super().__init__(*args, **kwargs)

    def _wait_terminal(self, run_id: Any, prepared: PreparedDelivery) -> str | None:
        deadline = self.monotonic() + 15
        expected_count = _prepared_record_count(prepared.body)
        for _ in range(15):
            try:
                status = self.source.get_run_status(
                    run_id,
                    expected=prepared,
                    deadline=deadline,
                    monotonic=self.monotonic,
                )
            except SourceAPIDeadlineExceeded:
                return "SOURCE_TERMINAL_TIMEOUT"
            value = getattr(status, "status", None)
            if value in {"completed", "completed_with_errors", "failed"}:
                if value == "completed" and _source_counts_match(status, expected_count):
                    return "completed"
                if value == "completed" and not _source_counts_match(status, expected_count):
                    return "SOURCE_COUNT_MISMATCH"
                return (
                    getattr(status, "failure_code", None)
                    if getattr(status, "failure_code", None) == "DATASET_INACTIVE"
                    else str(value)
                )
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                break
            self.sleep(min(1.0, remaining))
        return "SOURCE_TERMINAL_TIMEOUT"


def _prepared_record_count(body: bytes) -> int:
    try:
        value = json.loads(body)
        data = value["payload"]["data"]
        if not isinstance(data, list):
            raise ValueError
        return len(data)
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise ProductionRuntimeError("prepared request is invalid") from exc


def _source_counts_match(status: Any, expected: int) -> bool:
    return (
        type(getattr(status, "total_records", None)) is int
        and type(getattr(status, "success_records", None)) is int
        and type(getattr(status, "failed_records", None)) is int
        and status.total_records == expected
        and status.success_records == expected
        and status.failed_records == 0
    )


class ShioajiProductionScheduler:
    """Trigger the pilot only for today's open TW session."""

    def __init__(
        self,
        *,
        state: ShioajiStagingState,
        manifest: dict[str, Any],
        calendar: Calendar,
        coordinator_factory: Callable[[datetime], ProductionCoordinator],
    ) -> None:
        if not _supported_universe_id(manifest.get("universe_id")):
            raise ProductionManifestError("production coordinator requires reviewed identity")
        self.state = state
        self.manifest = manifest
        self.calendar = calendar
        self.coordinator_factory = coordinator_factory

    def run_once(self, *, now: datetime) -> SchedulerRun:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ProductionRuntimeError("scheduler clock must be timezone-aware")
        local = now.astimezone(TAIPEI)
        target = local.date()
        if local.time() < PILOT_DUE:
            return SchedulerRun(target, "skipped", skip_reason="not_due")
        if local.time() >= PILOT_CUTOFF:
            self.state.mark_cutoff(f"{self.manifest['universe_id']}:{target.isoformat()}")
            return SchedulerRun(target, "skipped", skip_reason="cutoff_reached")
        day, revision = self.calendar.get_day(PILOT_MARKET, target)
        day_status = getattr(day, "day_status", None)
        is_open = getattr(day, "is_open", None)
        if day_status is None:
            day_status = "open" if is_open is True else "closed"
        if day_status != "open" or is_open is not True:
            return SchedulerRun(
                target,
                "skipped",
                skip_reason=f"calendar_{day_status or 'closed'}",
                calendar_revision=revision,
            )
        results = tuple(self.coordinator_factory(now).run(target, deliver=True))
        if not results:
            return SchedulerRun(target, "failed", results=results, calendar_revision=revision)
        if any(result.retryable for result in results):
            status = "retry_pending"
        elif all(result.code in {"completed", "DELIVERED"} for result in results):
            status = "completed"
        else:
            status = "failed"
        return SchedulerRun(
            target,
            status,
            results=results,
            calendar_revision=revision,
        )


# Short aliases make the pilot easy to discover without exposing the staging
# module as a production entry point.
PilotCoordinator = ProductionCoordinator
Coordinator = ProductionCoordinator
ShioajiScheduler = ShioajiProductionScheduler
ProductionScheduler = ShioajiProductionScheduler
SchedulerService = ShioajiProductionScheduler
ShioajiSchedulerService = ShioajiProductionScheduler
ShioajiManifestError = ProductionManifestError
load_production_manifest = load_manifest


def build_runtime(
    manifest: dict[str, Any],
    state: ShioajiStagingState,
    *,
    fetcher: FetcherConfig,
    calendar_config: MarketCalendarConfig,
    raw_config: RawStorageConfig,
    contracts: ContractRegistry,
) -> tuple[ShioajiProductionScheduler, Any]:
    """Construct external clients only after all offline checks pass."""
    state.bind_raw_storage(raw_config.account_id, raw_config.bucket)
    gateway = IsolatedShioajiGateway.from_env()
    raw_store = R2RawPayloadStore(raw_config)
    source = _source_client(fetcher, contracts)
    calendar = PublishedCalendarClient(calendar_config)

    def factory(_scheduled_at: datetime) -> ProductionCoordinator:
        return ProductionCoordinator(
            state,
            manifest,
            gateway,
            raw_store=raw_store,
            source=source,
            contracts=contracts,
            # The scheduler's ``now`` selects today's due work. Coordinator
            # execution must keep reading wall time so a long provider run
            # cannot continue past the 17:00 production cutoff.
            now=lambda: datetime.now(TAIPEI),
        )

    return ShioajiProductionScheduler(
        state=state,
        manifest=manifest,
        calendar=calendar,
        coordinator_factory=factory,
    ), (source, calendar, raw_store)


def _source_client(fetcher: FetcherConfig, contracts: ContractRegistry) -> Any:
    from findb_fetcher.client import SourceAPIClient

    return SourceAPIClient(fetcher, contracts)


def _supported_universe_id(value: Any) -> bool:
    return value == PILOT_UNIVERSE_ID or (
        isinstance(value, str) and value.startswith(PRODUCTION_UNIVERSE_PREFIX)
    )


def _coordinator_universe_id(value: Any) -> bool:
    return isinstance(value, str) and (
        value.startswith(PILOT_UNIVERSE_ID) or value.startswith(PRODUCTION_UNIVERSE_PREFIX)
    )


def stable_daily_id(target: date, universe_id: str = PILOT_UNIVERSE_ID) -> str:
    if not _supported_universe_id(universe_id):
        raise ProductionManifestError("daily identity requires reviewed universe")
    return hashlib.sha256(f"{universe_id}:{target.isoformat()}".encode()).hexdigest()[:32]


__all__ = [
    "DEFAULT_PRODUCTION_STATE_PATH",
    "EXPECTED",
    "GOVERNANCE",
    "PILOT_CUTOFF",
    "PILOT_DUE",
    "PILOT_UNIVERSE_ID",
    "ProductionCoordinator",
    "PilotCoordinator",
    "ProductionManifestError",
    "ProductionRuntimeError",
    "ProductionScheduler",
    "ProductionStateError",
    "SchedulerService",
    "ShioajiSchedulerService",
    "ShioajiManifestError",
    "SchedulerRun",
    "ShioajiProductionScheduler",
    "ShioajiScheduler",
    "build_runtime",
    "default_manifest_path",
    "default_state_path",
    "load_manifest",
    "load_production_manifest",
    "open_production_state",
    "validate_contracts",
    "validate_production_runtime",
    "validate_production_state_path",
]
