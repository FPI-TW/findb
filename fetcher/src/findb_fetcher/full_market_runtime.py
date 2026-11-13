"""Activation-forward full-market delivery with durable provider/account boundaries."""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from datetime import time as clock_time
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx

from findb_fetcher import rpc_timing
from findb_fetcher.acquisition import authorization_scope, http_acquisition
from findb_fetcher.client import (
    SourceAPIClient,
    SourceAPIResponseError,
    SourceAPITransportError,
    delivered_snapshot,
    permanent_source_failure,
)
from findb_fetcher.config import app_environment
from findb_fetcher.daily_runtime import window
from findb_fetcher.execution_config import ExecutionSnapshot, resolve_execution_config
from findb_fetcher.execution_metadata import acquisition_feeds, provider_platform_limits
from findb_fetcher.full_market_client import FullMarketProtocolError, FullMarketSourceClient
from findb_fetcher.full_market_errors import (
    DatedUniversePendingError,
    MemberUnavailableError,
    ProviderHTTPError,
    ReadinessBlockedError,
)
from findb_fetcher.full_market_errors import retryable_http_status as _retryable_http_status
from findb_fetcher.full_market_state import (
    FULL_WORK_MAX_ATTEMPTS,
    FULL_WORK_NO_DATA_BREAKER,
    FULL_WORK_NO_DATA_BREAKER_HOLD_SECONDS,
    FullMarketState,
    FullMarketStateError,
    QuotaBlockedError,
)
from findb_fetcher.full_market_universe import (
    EMPTY_OFFICIAL_SNAPSHOT_URLS,
    MAX_SNAPSHOT_BYTES,
    canonical_bytes,
    checksum,
    official_snapshot_dataset,
    universe_request,
)
from findb_fetcher.historical_backfill import HistoricalWorkDeferredError
from findb_fetcher.http_response import BoundedResponseError, read_identity_response
from findb_fetcher.provider_adapters import (
    AcquisitionContext,
    BatchContext,
    FailureKind,
    UniverseContext,
    adapter_for,
)
from findb_fetcher.provider_process import ProviderProcessError
from findb_fetcher.provider_wait import (
    CalendarDecisionRepairRequiredError,
    ProviderOutagePendingError,
    ProviderPublicationPendingError,
    ProviderWorkRejectedError,
)
from findb_fetcher.provider_work import (
    ProviderWorkAcquisitionStopped,
    ProviderWorkBasisMismatchError,
    closed_work_times,
    durable_response,
    prune_closed_journal,
)
from findb_fetcher.raw_storage import (
    RawObject,
    RawPayloadFormat,
    RawPayloadStore,
    attach_raw_object,
)

# Fetcher logs one delivery timing line per interval during a long plan delivery.
DELIVERY_TIMING_INTERVAL_SECONDS = 60
# One official listing download, start to finish. The client's read timeout only
# bounds each chunk; the slowest page observed took just under nine minutes.
OFFICIAL_SNAPSHOT_TIME_LIMIT_SECONDS = 20 * 60
# Maintenance works of one provider run back to back and read the same listings;
# a later one reuses what an earlier one downloaded this recently.
OFFICIAL_SNAPSHOT_REUSE_SECONDS = 30 * 60
# Settlement waits at most this long for just-accepted runs before scheduling.
NORMALIZING_SETTLE_WAIT_SECONDS = 60
NONTERMINAL_RUN_STATUSES = frozenset({"queued", "pending", "processing", "retrying"})


class DailyWindowClosedError(ProviderWorkAcquisitionStopped):
    """A proven daily-window wait, including authoritative Source denial."""


def refresh_slots(
    feeds: Iterable[Mapping[str, Any]], now: datetime
) -> tuple[datetime, datetime] | None:
    """The daily scheduled time at or before ``now`` and the next one after it.

    A universe refresh belongs to a scheduled time rather than to its own
    completion, so its hour neither drifts nor follows the last control start.
    """
    latest: datetime | None = None
    upcoming: datetime | None = None
    for feed in feeds:
        local = now.astimezone(ZoneInfo(feed["source_timezone"]))
        today = datetime.combine(
            local.date(), clock_time.fromisoformat(feed["scheduled_time"]), local.tzinfo
        )
        passed = today if today <= local else today - timedelta(days=1)
        following = passed + timedelta(days=1)
        latest = passed if latest is None or passed > latest else latest
        upcoming = following if upcoming is None or following < upcoming else upcoming
    return None if latest is None or upcoming is None else (latest, upcoming)


def load_readiness(
    path: Path | None, provider: str, datasets: list[str], *, now: datetime
) -> dict[str, Any]:
    if path is None or not path.is_file() or path.stat().st_size > 65536:
        raise ReadinessBlockedError("existing account capability is unknown")
    try:
        value = json.loads(path.read_bytes())
        if (
            value["provider"] != provider
            or value["status"] != "verified"
            or not set(datasets) <= set(value["datasets"])
        ):
            raise ValueError
        verified = datetime.fromisoformat(value["verified_at"])
        expires = datetime.fromisoformat(value["expires_at"])
        if verified.tzinfo is None or expires.tzinfo is None or not verified <= now < expires:
            raise ValueError
        evidence_url = urlsplit(value["evidence_url"])
        if (
            evidence_url.scheme != "https"
            or not evidence_url.hostname
            or evidence_url.username
            or evidence_url.password
            or any(
                key.lower()
                in {"apikey", "api_key", "key", "token", "secret", "password", "signature"}
                for key, _ in parse_qsl(evidence_url.query)
            )
            or not re.fullmatch(r"[0-9a-f]{64}", value["evidence_sha256"])
        ):
            raise ValueError
        for field in (
            "requests_per_day",
            "requests_per_minute",
            "bytes_per_day",
            "max_response_bytes",
            "requests_per_second",
        ):
            if (
                isinstance(value[field], bool)
                or not isinstance(value[field], (float, int))
                or not math.isfinite(value[field])
                or value[field] <= 0
            ):
                raise ValueError
        if value["max_response_bytes"] > provider_platform_limits(provider)["max_response_bytes"]:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ReadinessBlockedError(
            "existing account readiness evidence is invalid or expired"
        ) from None
    return value


def _governed(method):
    @wraps(method)
    def run(self, *args, **kwargs):
        from findb_fetcher.provider_work import ProviderWorkSourceUnavailableError, require_owner
        from findb_fetcher.source_recovery import transient_source_error

        owner = require_owner(self.provider)

        def acquisition_allowed():
            try:
                return self._acquisition_allowed()
            except Exception as exc:
                if transient_source_error(exc):
                    raise ProviderWorkSourceUnavailableError(
                        "Source acquisition gate unavailable"
                    ) from exc
                raise

        if not acquisition_allowed():
            if not self.runtime_window()["open"]:
                raise DailyWindowClosedError("daily acquisition window closed")
            raise ProviderWorkAcquisitionStopped("scheduler acquisition stopped")

        def authorize():
            if not acquisition_allowed():
                if not self.runtime_window()["open"]:
                    raise DailyWindowClosedError("daily acquisition window closed")
                raise ProviderWorkAcquisitionStopped("scheduler acquisition stopped")
            consumer = os.getenv("FETCHER_CONSUMER_PROFILE", "full_market")

            consumer = (
                "maintenance"
                if consumer == "maintenance"
                or method.__name__ == "sync_universes"
                or owner.work["kind"] == "maintenance"
                else self.execution_profile
            )
            plan = kwargs.get("plan") or (
                args[1] if method.__name__ == "_acquire" and len(args) > 1 else None
            )
            try:
                grant = self.source.authorization(
                    "GET",
                    "full-market/authorization?consumer="
                    + consumer
                    + "&execution_profile="
                    + self.execution_profile,
                    required_dataset=(
                        kwargs.get("dataset_key") or args[0]
                        if method.__name__ == "_catalogue_market"
                        else args[0]["dataset_key"]
                        if method.__name__ == "_dated_universe"
                        else plan["dataset_key"]
                        if plan
                        else None
                    ),
                )

            except Exception as exc:
                if (
                    isinstance(exc, FullMarketProtocolError)
                    and exc.code == "DAILY_ACQUISITION_WINDOW_CLOSED"
                ):
                    raise DailyWindowClosedError("daily acquisition window closed") from None
                if transient_source_error(exc):
                    raise ProviderWorkSourceUnavailableError(
                        "Source acquisition authorization unavailable"
                    ) from exc
                raise
            runtime_window = self.runtime_window()
            if consumer != "maintenance" and self.daily_policy:
                if not runtime_window["open"]:
                    raise DailyWindowClosedError("daily acquisition window closed")
                expires = datetime.fromisoformat(runtime_window["closes_at"]).timestamp()
                grant = replace(grant, expires_at=min(grant.expires_at or expires, expires))
            return grant

        with authorization_scope(authorize, state_path=self.state.path):
            return method(self, *args, **kwargs)

    return run


def only_mapping_gaps(plan: dict[str, Any]) -> list[str] | None:
    """The plan's mapping-gap members when they are all it still lacks, else None.

    Source evaluates the same summary to accept a partial settlement, so the
    decision uses its counts and its bounded gap list rather than local state.
    """
    summary = plan.get("summary") or {}
    gaps = summary.get("gaps") or []
    blocked = summary.get("blocked")
    if (
        summary.get("missing") != 0
        or not blocked
        or blocked != summary.get("mapping_gap")
        or len(gaps) != blocked
    ):
        return None
    return sorted(gap["member_key"] for gap in gaps)


class FullMarketRuntime:
    def __init__(
        self,
        *,
        provider: str,
        config: dict[str, Any],
        state: FullMarketState,
        source: FullMarketSourceClient,
        delivery: SourceAPIClient,
        raw_store: RawPayloadStore,
        readiness_file: Path | None = None,
        execution_profile: str = "full_market",
        client: httpx.Client | None = None,
        can_acquire: Callable[[], bool] | None = None,
    ) -> None:
        if execution_profile != "full_market":
            raise ValueError("Unsupported execution profile")
        self.execution_profile = execution_profile
        self.provider, self.config, self.state = provider, config, state
        self.can_acquire = can_acquire or (lambda: True)
        self.source, self.delivery, self.raw_store = (
            source,
            delivery,
            raw_store,
        )
        self.readiness_file = readiness_file
        self.feeds = [
            {**feed, "slot_id": feed["delivery"]["slot_id"]} for feed in acquisition_feeds(provider)
        ]
        # Packaged catalog contracts; frozen work scope never overrides them.
        self.contracts = {
            feed["dataset_key"]: feed["contract"] for feed in acquisition_feeds(provider)
        }
        self.schedule_snapshot: ExecutionSnapshot | None = None
        # The schedule the latest universe refresh resolved its dates from.
        self.refresh_snapshot: ExecutionSnapshot | None = None
        self.installed_feeds = list(self.feeds)
        self.http = client or httpx.Client(timeout=30)
        self.owns_http = client is None
        # Provider-specific acquisition, failure classes and native caches.
        self.adapter = adapter_for(provider)
        self.proof: dict[str, Any] = {}
        self.reservation_window = ""
        self.snapshot_objects: dict[str, RawObject] = {}
        # url -> (downloaded at, body, R2 object), for sibling maintenance works only.
        self._recent_snapshots: dict[str, tuple[float, bytes, RawObject]] = {}
        # Set by the worker around a cycle: reports liveness to the scheduler control.
        self.heartbeat: Callable[[], Any] | None = None
        self.catalogue_evidence: dict[str, bytes] = {}
        # Source stable work identity is immutable, so one acknowledged enqueue
        # per process is enough; every later cycle would only replay it.
        self.enqueued_keys: set[str] = set()
        self.daily_policy = None
        self.last_batch: dict[str, Any] = {}
        self._timing: dict[str, Any] = {"plan_id": None, "since": 0.0, "members": 0}

    def close(self) -> None:
        self.adapter.close()
        if self.owns_http:
            self.http.close()

    def pause(self) -> None:
        self.adapter.reset()

    def _publication_retry_delay(self, owner, feed: dict[str, Any]) -> int:
        created_at = owner.work.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        if not isinstance(created_at, datetime) or created_at.tzinfo is None:
            raise ReadinessBlockedError("provider work publication window is invalid")
        window = feed.get("completion_window_seconds")
        if type(window) is not int or window < 1:
            raise ReadinessBlockedError("provider publication completion window is invalid")
        remaining = math.ceil(
            (created_at + timedelta(seconds=window) - datetime.now(timezone.utc)).total_seconds()
        )
        if remaining <= 0:
            return 0
        from findb_fetcher.provider_wait import PUBLICATION_RETRY_SECONDS

        return min(PUBLICATION_RETRY_SECONDS, remaining)

    def _calendar_repair_required(self, owner) -> bool:
        status = owner.request("calendar-status")
        if (
            not isinstance(status, dict)
            or status.get("work_id") != owner.work.get("work_id")
            or status.get("trade_date") != owner.work.get("trade_date")
            or type(status.get("repair_required")) is not bool
        ):
            raise ReadinessBlockedError("provider work calendar status is invalid")
        return status["repair_required"]

    def runtime_window(self) -> dict[str, Any]:
        return window(self.daily_policy, datetime.now(timezone.utc))

    def health(self) -> dict[str, Any]:
        return {**self.state.health(self.provider), "daily_runtime_window": self.runtime_window()}

    def _acquisition_allowed(self) -> bool:
        from findb_fetcher.provider_work import active

        owner = active()
        if owner and owner.work["kind"] == "maintenance":
            owner.authorize()
            return True
        if os.getenv("FETCHER_CONSUMER_PROFILE") == "maintenance":
            return self.can_acquire()
        return self.runtime_window()["open"] and self.can_acquire()

    def _observed_response(self, size: int, *, declared: int = 0) -> None:
        bound = int(self.proof["max_response_bytes"])
        if self.proof.get("bytes_per_day") is not None and size is not None:
            reserved = 0 if self.proof.get("platform_limits") else bound
            if size > reserved and max(size, declared) <= bound:
                self.state.record_byte_overage(
                    account=self.provider,
                    window=self.reservation_window,
                    byte_count=size - reserved,
                )
        if max(size, declared) > bound:
            self.state.observe_capacity(
                account=self.provider,
                window=self.reservation_window,
                bound=(0 if self.proof.get("platform_limits") else bound),
                observed=size,
                declared=declared,
                allocation_sha256="",
            )
            raise ReadinessBlockedError("provider response exceeds transport size bound")

    def _rate_limited(self) -> None:
        self.state.rate_limited(
            account=self.provider,
            window=datetime.now(timezone.utc).date().isoformat(),
            until=time.time() + 60,
        )

    def _download_progress(self) -> None:
        """Keep the control heartbeat alive while one long download blocks the cycle."""
        if self.heartbeat is None:
            return
        try:
            self.heartbeat()
        except Exception:
            # Liveness reporting never decides the fate of the download itself.
            pass

    def _recent_snapshot(self, url: str) -> tuple[bytes, RawObject] | None:
        """A listing a sibling maintenance work of this process just downloaded."""
        from findb_fetcher.provider_work import active

        now = time.monotonic()
        for stale in [
            key
            for key, (at, _, _) in self._recent_snapshots.items()
            if now - at > OFFICIAL_SNAPSHOT_REUSE_SECONDS
        ]:
            del self._recent_snapshots[stale]
        owner = active()
        # Daily and historical works read dated reports whose content changes as
        # the exchange publishes; only universe refreshes share their listings.
        if owner is None or owner.work.get("kind") != "maintenance":
            return None
        recent = self._recent_snapshots.get(url)
        return None if recent is None else (recent[1], recent[2])

    def _read_response(
        self,
        response: httpx.Response,
        *,
        bound: int,
        provider_response: bool,
        time_limit: float | None = None,
    ) -> bytes:
        try:
            return read_identity_response(
                response,
                bound=bound,
                time_limit=time_limit,
                on_progress=self._download_progress,
            )
        except BoundedResponseError as exc:
            if provider_response and (exc.observed_bytes or exc.declared_bytes):
                self._observed_response(exc.observed_bytes, declared=exc.declared_bytes)
            raise ReadinessBlockedError(str(exc)) from exc
        except httpx.HTTPError as exc:
            if provider_response:
                self._observed_response(getattr(exc, "observed_bytes", 0))
            raise

    @_governed
    @durable_response("official_snapshot", ("url",))
    def _snapshot(self, url: str, *, provider_response: bool = False) -> bytes:
        if not self._acquisition_allowed():
            if not self.runtime_window()["open"]:
                raise DailyWindowClosedError("daily acquisition window closed")
            raise ProviderWorkAcquisitionStopped("scheduler acquisition stopped")
        recent = self._recent_snapshot(url)
        if recent is not None:
            body, self.snapshot_objects[url] = recent
            return body
        # Official listings consume the same enrolled response bound and account
        # bytes as provider data; the parser ceiling is an additional constraint.
        bound = min(int(self.proof["max_response_bytes"]), MAX_SNAPSHOT_BYTES)
        with (
            http_acquisition(self.provider, self.http, url=url) as (operation, extensions),
            self.http.stream(
                "GET", url, headers={"Accept-Encoding": "identity"}, extensions=extensions
            ) as response,
        ):
            if response.status_code == 429:
                operation.rate_limited(response.headers.get("Retry-After"))
                # Status is known before a bounded read can reject its body.
                self._rate_limited()
            try:
                body = self._read_response(
                    response,
                    bound=bound,
                    provider_response=True,
                    time_limit=OFFICIAL_SNAPSHOT_TIME_LIMIT_SECONDS,
                )
            except (ReadinessBlockedError, httpx.HTTPError) as exc:
                if response.status_code != 200:
                    raise ProviderHTTPError(
                        "official exchange snapshot unavailable", response.status_code
                    ) from exc
                raise
            self._observed_response(len(body))
            if response.status_code != 200:
                raise ProviderHTTPError(
                    "official exchange snapshot unavailable", response.status_code
                )
            # An empty unpublished HTTP 200 report has no raw object; the dated
            # report resolver handles fallback/retry without fabricating evidence.
            if url in EMPTY_OFFICIAL_SNAPSHOT_URLS and not body:
                return body
            stored = self._persist(body, official_snapshot_dataset(url), "official_universe")
            self.snapshot_objects[url] = stored
            self._recent_snapshots[url] = (time.monotonic(), body, stored)
            return body

    def _ensure_proof(self) -> None:
        from findb_fetcher.platform_limits import limits

        self.proof = limits(self.provider, [feed["dataset_key"] for feed in self.installed_feeds])

    @_governed
    def _catalogue(self, dataset_keys: list[str] | None = None) -> list[dict[str, Any]]:
        """The provider's HTTP catalogue rows for the selected datasets."""
        self._ensure_proof()
        scope = self.adapter.catalogue_datasets
        if scope is None:
            return []
        installed = {feed["dataset_key"] for feed in self.installed_feeds}
        selected = installed if dataset_keys is None else set(dataset_keys)
        if not selected or not selected <= installed or not selected <= set(scope):
            raise ReadinessBlockedError("provider catalogue dataset scope invalid")
        combined: list[dict[str, Any]] = []
        evidence: dict[str, bytes] = {}
        for dataset in scope:
            if dataset not in selected:
                continue
            rows, url, raw = self._catalogue_market(dataset)
            combined.extend(rows)
            evidence[url] = raw
        self.catalogue_evidence = evidence
        return combined

    def _catalogue_evidence_for(self, dataset_key: str) -> dict[str, bytes]:
        return {
            url: raw
            for url, raw in self.catalogue_evidence.items()
            if self.adapter.catalogue_evidence_dataset(url) == dataset_key
        }

    @_governed
    def _catalogue_market(self, dataset_key: str) -> tuple[list[dict[str, Any]], str, bytes]:
        # The adapter names the journal identity, so replay keys never change.
        journaled = durable_response(self.adapter.catalogue_journal, ("dataset_key",))(
            self._fetch_catalogue
        )
        return journaled(dataset_key=dataset_key)

    def _fetch_catalogue(self, dataset_key: str) -> tuple[list[dict[str, Any]], str, bytes]:
        request = self.adapter.catalogue_request(dataset_key, int(self.proof["max_response_bytes"]))
        # Each dataset request uses a fresh dataset-bound authorization and account permit.
        with (
            http_acquisition(self.provider, self.http, url=request.url) as (
                operation,
                extensions,
            ),
            self.http.stream(
                "GET",
                request.url,
                params=dict(request.params),
                headers=dict(request.headers),
                extensions=extensions,
            ) as response,
        ):
            if response.status_code == 429:
                operation.rate_limited(response.headers.get("Retry-After"))
                # Commit known account cooldown before body validation/byte accounting.
                self._rate_limited()
            try:
                raw = self._read_response(
                    response, bound=request.max_response_bytes, provider_response=True
                )
            except (ReadinessBlockedError, httpx.HTTPError) as exc:
                if response.status_code == 429:
                    raise QuotaBlockedError(
                        "provider catalogue rate limited", status_code=429
                    ) from exc
                if response.status_code != 200:
                    raise ProviderHTTPError(
                        "provider catalogue unavailable", response.status_code
                    ) from exc
                raise
            if response.status_code == 429:
                raise QuotaBlockedError("provider catalogue rate limited", status_code=429)
            if response.status_code != 200:
                raise ProviderHTTPError("provider catalogue unavailable", response.status_code)
            rows = self.adapter.catalogue_rows(raw)
            catalogue_raw = bytes(raw)
            self._persist(catalogue_raw, dataset_key, "provider_catalogue")
            return rows, request.evidence_url, catalogue_raw

    @_governed
    def sync_universes(self, *, dataset_keys: list[str] | None = None) -> list[dict[str, Any]]:
        self._ensure_proof()
        if dataset_keys is None:
            authorization = self.source.request(
                "GET",
                "full-market/authorization?consumer=maintenance&execution_profile="
                + self.execution_profile,
            )
            if authorization.get("acquisition_allowed") is not True:
                raise ReadinessBlockedError("Source maintenance authorization denied")
            dataset_keys = authorization.get("authorized_dataset_keys")
        if (
            not isinstance(dataset_keys, list)
            or not dataset_keys
            or any(not isinstance(key, str) or not key.strip() for key in dataset_keys)
        ):
            raise ReadinessBlockedError("Source universe scope invalid")
        # Local refresh scope must never overwrite the cycle's admitted feeds,
        # including when snapshot fetching or submission fails partway through.
        feeds = [feed for feed in self.installed_feeds if feed["dataset_key"] in dataset_keys]
        if not feeds:
            return []
        zones = self._universe_zones(feeds)
        now = datetime.now(timezone.utc)
        groups = self.adapter.build_universe(
            feeds,
            UniverseContext(
                dataset_keys=dataset_keys,
                observed_at=now,
                zones=zones,
                snapshot=self._snapshot,
                catalogue=self._catalogue,
                catalogue_evidence=self._catalogue_evidence_for,
                persist=self._persist,
                observe_response=self._observed_response,
                snapshot_objects=self.snapshot_objects,
                refresh_limits=self._ensure_proof,
                pace=self._pace_after_call,
            ),
        )
        result = []
        for feed in feeds:
            dataset = feed["dataset_key"]
            universe = groups[dataset]
            effective, members, evidence = (
                universe.effective_date,
                universe.members,
                universe.evidence,
            )
            for url, raw in evidence.items():
                stored = self.snapshot_objects.get(url) or self._persist(
                    raw, dataset, "official_universe"
                )
                for member in members:
                    member.setdefault("raw_evidence_ref", stored.ref)
            body = universe_request(
                dataset_key=dataset,
                provider=self.provider,
                effective_date=effective,
                observed_at=now,
                source_timezone=zones[dataset].key,
                snapshots=evidence,
                members=members,
            )
            result.append(self.source.submit(body))
        return result

    def _pace_after_call(self) -> None:
        """Space the next account call from the completion of a provider SDK call."""
        self.state.pace_from_completion(
            account=self.provider,
            now=datetime.now(timezone.utc).timestamp(),
            interval=(
                1 / self.proof["requests_per_second"] if self.proof["requests_per_second"] else 0
            ),
        )

    def _universe_zones(self, feeds: list[dict[str, Any]]) -> dict[str, ZoneInfo]:
        """Universe dates use Source-published feed timezones, never local tables."""
        snapshot = self.schedule_snapshot or resolve_execution_config(
            self.source.execution_config(),
            provider=self.provider,
            environment=app_environment(),
            profile=self.execution_profile,
            complete_scope=False,
        )
        self.refresh_snapshot = snapshot
        published = {feed["dataset_key"]: feed["source_timezone"] for feed in snapshot.feeds}
        if any(feed["dataset_key"] not in published for feed in feeds):
            raise ReadinessBlockedError("Source universe timezone unavailable")
        return {feed["dataset_key"]: ZoneInfo(published[feed["dataset_key"]]) for feed in feeds}

    def prune_closed_state(self) -> dict[str, Any] | None:
        """Daily retention of local journal and checkpoint rows for closed works."""
        journal = prune_closed_journal(self.source, self.provider)
        plans = self.state.prune_closed_plans(
            self.provider,
            lambda keys: closed_work_times(self.source, stable_keys=keys),
            execution_profile=self.execution_profile,
        )
        if journal is None and plans is None:
            return None
        return {"journal": journal, "plans": plans}

    def drain(self) -> None:
        """Drain retained deliveries only through the original fenced owner."""
        from findb_fetcher.config import require_provider_work_protocol

        require_provider_work_protocol()
        self._queued_cycle(include_health=False, plan_new_dates=False)

    def cycle(self) -> dict[str, Any]:
        """All providers use the same durable queue, including retained work."""
        from findb_fetcher.config import require_provider_work_protocol

        require_provider_work_protocol()
        # Clear before requesting: a failed round cannot reuse the previous clock.
        self.schedule_snapshot = None
        snapshot = resolve_execution_config(
            self.source.execution_config(),
            provider=self.provider,
            environment=app_environment(),
            profile=self.execution_profile,
        )
        self.schedule_snapshot = snapshot
        return self._queued_cycle(snapshot=snapshot)

    def sync_queued_universes(self) -> list[dict[str, Any]]:
        from findb_fetcher.provider_work import ProviderWorkWaitingError, claimed_work

        self._ensure_proof()
        for feed in self.installed_feeds:
            self._enqueue(
                {
                    "stable_key": f"maintenance:{feed['dataset_key']}:{datetime.now(timezone.utc).date()}",
                    "kind": "maintenance",
                    "dataset_key": feed["dataset_key"],
                    "scope": {"dataset_key": feed["dataset_key"]},
                }
            )
        try:
            with claimed_work(self.source, self.provider, accepted_kinds=["maintenance"]) as owner:
                if owner is None:
                    return [{"status": "queued"}]
                try:
                    return self._run_queued_maintenance(owner)
                finally:
                    self.pause()
        except ProviderWorkWaitingError:
            return [{"status": "queued"}]

    def _run_queued_maintenance(self, owner) -> list[dict[str, Any]]:
        """Known catalogue failures close a safe round; uncertain ownership never does."""
        from findb_fetcher.provider_work import (
            ProviderWorkRecoveryBlockedError,
            ProviderWorkSourceUnavailableError,
        )
        from findb_fetcher.raw_storage import RawStorageUploadError

        work = owner.work
        if not any(feed["dataset_key"] == work["dataset_key"] for feed in self.installed_feeds):
            self._settle_retired_work(owner)
            return [{"status": "manual", "reason": "RETIRED_FEED_MANUAL_REVIEW"}]
        if work["state"] != "open":
            manifest = work.get("manifest")
            if manifest:
                owner.freeze(manifest)
                self._settle_queued_round(owner, outcome="completed")
                # Replay the original completion proof without acquiring again.
                return manifest.get("no_data_evidence", {}).get("universe_refresh", [])
            self._settle_queued_round(owner, outcome="retry", failure_code="RECOVERED_CLOSED_ROUND")
            return [{"status": "queued"}]
        operation_key = "maintenance:universe_refresh"
        try:
            owner.journal.reserve_attempt(operation_key, FULL_WORK_MAX_ATTEMPTS)
        except ValueError:
            self._settle_queued_round(
                owner, outcome="failed", failure_code="MAINTENANCE_RETRY_EXHAUSTED"
            )
            return [{"status": "manual", "reason": "MAINTENANCE_RETRY_EXHAUSTED"}]
        retryable, code = False, "MAINTENANCE_FORMAT_OR_SCOPE"
        dispatch_before = owner.dispatch_observations
        try:
            results = self.sync_universes(dataset_keys=[work["dataset_key"]])
        except ProviderWorkSourceUnavailableError:
            if owner.dispatch_observations == dispatch_before:
                owner.journal.release_wait_attempt(operation_key)
            raise
        except ProviderWorkRecoveryBlockedError:
            raise
        except ProviderWorkAcquisitionStopped:
            retryable, code = True, "MAINTENANCE_ACQUISITION_SUSPENDED"
            if owner.dispatch_observations == dispatch_before:
                owner.journal.release_wait_attempt(operation_key)
        except QuotaBlockedError as exc:
            retryable, code = True, "MAINTENANCE_QUOTA_OR_ACQUISITION"
            if (
                exc.quota_wait
                and not exc.dispatched
                and owner.dispatch_observations == dispatch_before
            ):
                owner.journal.release_wait_attempt(operation_key)
        except (ProviderHTTPError, httpx.HTTPStatusError) as exc:
            status = (
                exc.status_code if isinstance(exc, ProviderHTTPError) else exc.response.status_code
            )
            retryable = _retryable_http_status(status)
            code = "MAINTENANCE_HTTP_" + str(status or "UNKNOWN")
        except (httpx.TransportError, SourceAPITransportError):
            retryable, code = True, "MAINTENANCE_TRANSPORT"
        except FullMarketProtocolError as exc:
            if exc.code == "PROVIDER_WORK_FENCE":
                raise ProviderWorkRecoveryBlockedError("maintenance owner fence differs") from exc
            retryable = (
                exc.retryable
                or exc.status_code in {408, 429}
                or (exc.status_code is not None and exc.status_code >= 500)
            )
            code = "MAINTENANCE_SOURCE_" + str(exc.status_code or "UNKNOWN")
        except RawStorageUploadError as exc:
            retryable, code = exc.retryable, "MAINTENANCE_RAW_UPLOAD"
        except ProviderProcessError:
            retryable, code = True, "MAINTENANCE_PROVIDER_PROCESS"
        except (ReadinessBlockedError, ValueError):
            pass
        except Exception as exc:
            # Provider failures are classified by the adapter; others propagate.
            failure = self.adapter.classify(exc, "maintenance")
            if failure.kind not in {FailureKind.ROUND_RETRY, FailureKind.WORK_FAILED}:
                raise
            retryable = failure.kind is FailureKind.ROUND_RETRY
            code = (
                "MAINTENANCE_HTTP_" + str(failure.status_code or "UNKNOWN")
                if failure.http
                else failure.code or "MAINTENANCE_PROVIDER_FAILURE"
            )
        else:
            owner.freeze(
                {
                    "members": [{"key": "universe_refresh"}],
                    "no_data_evidence": {"universe_refresh": results},
                }
            )
            self.pause()
            # The manifest is already durable; closed-round replay uses it.
            owner.settle("completed", proof={"no_data": ["universe_refresh"]})
            now = datetime.now(timezone.utc)
            self.state.refresh(
                self.provider,
                now=now.timestamp(),
                result=results,
                next_at=self._next_refresh_at(now),
            )
            return results
        self._settle_queued_round(
            owner, outcome="retry" if retryable else "failed", failure_code=code
        )
        return [{"status": "queued" if retryable else "manual", "reason": code}]

    def _next_refresh_at(self, now: datetime) -> float:
        """The provider's next scheduled daily time, whoever asked for this refresh."""
        snapshot = self.schedule_snapshot or self.refresh_snapshot
        slots = refresh_slots(snapshot.feeds if snapshot else (), now)
        # Without a published schedule the refresh keeps its former daily interval.
        return slots[1].timestamp() if slots else now.timestamp() + 86400

    def _settle_retired_work(self, owner) -> None:
        """Drain original deliveries without turning compatibility scope into acquisition."""
        from findb_fetcher.provider_work import ProviderWorkRecoveryBlockedError

        work = owner.work
        # Imported plans can predate the server preparation ledger. Never
        # release their owner on the strength of an empty server ledger alone.
        # Compare every local sequence with the original server-retained bytes;
        # do not recreate a manifest, request, receipt, or provider response.
        try:
            if work["kind"] == "daily":
                originals = {checksum(body.encode()) for body in owner.restore()}
                with self.state.connection() as db:
                    keys = db.execute(
                        "SELECT w.key FROM full_work w JOIN full_plan p ON p.plan_id=w.plan_id "
                        "WHERE p.provider=? AND p.dataset=? AND p.trade_date=? AND w.body IS NOT NULL",
                        (self.provider, work["dataset_key"], work["trade_date"]),
                    ).fetchall()
                for row in keys:
                    prepared = self.state.prepared(row["key"])
                    if prepared is None:
                        raise ValueError("local prepared bytes disappeared")
                    requests = json.loads(prepared)
                    if (
                        not isinstance(requests, list)
                        or not requests
                        or any(
                            not isinstance(body, dict)
                            or checksum(canonical_bytes(body)) not in originals
                            for body in requests
                        )
                    ):
                        raise ProviderWorkRecoveryBlockedError(
                            "retired feed has local prepared bytes requiring explicit recovery"
                        )
            owner.drain_prepared()
        except Exception as exc:
            raise ProviderWorkRecoveryBlockedError(
                "retired feed original delivery drain requires recovery"
            ) from exc
        self.pause()
        # No manual_pending_prepared waiver: Source must see all original
        # deliveries accepted and every normalization terminal before release.
        # The only exception is a Twelve Data body mapped from pre-basis bytes,
        # which settle() retains under an explicit price-basis waiver.
        # Missing/manual frozen members remain missing/manual, never no_data.
        owner.settle(
            "failed",
            failure_code="RETIRED_FEED_MANUAL_REVIEW",
            proof={"manual_reason": "retired_feed_requires_manual_review"},
        )

    def _enqueue(self, body: dict[str, Any]) -> None:
        if body["stable_key"] in self.enqueued_keys:
            return
        try:
            self.source.request("POST", "provider-work/enqueue", body)
        except FullMarketProtocolError as exc:
            if not (
                body["kind"] == "daily"
                and exc.status_code == 409
                and exc.code == "PROVIDER_WORK_FROZEN_FEED"
            ):
                raise
            # Source proved the same daily identity already has frozen metadata.
            # Claim restores its original scope; never overwrite it with this round.
        self.enqueued_keys.add(body["stable_key"])

    def _queued_cycle(
        self,
        *,
        include_health: bool = True,
        plan_new_dates: bool = True,
        snapshot: ExecutionSnapshot | None = None,
    ) -> dict[str, Any]:
        """Enqueue bounded dates before claiming; every provider call is owned."""
        from findb_fetcher.provider_work import ProviderWorkWaitingError, claimed_work
        from findb_fetcher.raw_storage import RawStorageUploadError

        # Idle drain callers discard the result. Never rebuild every historical
        # plan's counts/failures just to report that there was no owned work.
        health = self.health if include_health else lambda: {}
        self._ensure_proof()
        authorization = self.source.request(
            "GET", "full-market/authorization?consumer=" + self.execution_profile
        )
        now = datetime.now(timezone.utc)
        admitted = (
            authorization.get("admitted_dataset_keys", [])
            if authorization.get("acquisition_allowed")
            else []
        )
        if plan_new_dates and snapshot is None:
            raise ValueError("New date planning requires a fresh execution snapshot")
        planning_feeds = snapshot.feeds if snapshot is not None and plan_new_dates else ()
        due: list[tuple[date, dict[str, Any]]] = []
        for feed in planning_feeds:
            if feed["dataset_key"] not in admitted:
                continue
            listed = self.source.universes(feed["dataset_key"], self.provider)
            activation = listed.get("activation", {})
            if activation.get("enabled") is not True or not listed.get("published_release_id"):
                continue
            first = date.fromisoformat(activation["activation_date"])
            if activation.get("acquisition_start_date"):
                first = max(first, date.fromisoformat(activation["acquisition_start_date"]))
            local = now.astimezone(ZoneInfo(feed["source_timezone"]))
            through = local.date() - timedelta(days=feed["target_date_lag_days"])
            if local.strftime("%H:%M") < feed["scheduled_time"]:
                through -= timedelta(days=1)
            due.extend(
                (first + timedelta(days=offset), feed)
                for offset in range((through - first).days + 1)
            )
        # Existing durable plans must drain after stop even when no new dates
        # are admitted. Enqueue idempotency preserves their original position.
        for saved in self.state.pending_plans(self.provider) if plan_new_dates else ():
            plan = json.loads(saved["body"])
            pending_feed = next(
                (feed for feed in planning_feeds if feed["dataset_key"] == plan["dataset_key"]),
                None,
            )
            if pending_feed:
                due.append((date.fromisoformat(plan["trade_date"]), pending_feed))
        slots = refresh_slots(planning_feeds, now)
        if (
            admitted
            and slots
            and self.adapter.scheduled_maintenance
            and self.state.refresh_due(self.provider, now.timestamp())
        ):
            # Enqueued ahead of this round's dates: their plans freeze the list
            # observed at their own scheduled time. One work per scheduled time,
            # so a failed refresh is tried again at the next one.
            slot = slots[0].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
            for feed in planning_feeds:
                if feed["dataset_key"] in admitted:
                    self._enqueue(
                        {
                            "stable_key": f"maintenance:{feed['dataset_key']}:{slot}",
                            "kind": "maintenance",
                            "dataset_key": feed["dataset_key"],
                            "scope": {"dataset_key": feed["dataset_key"]},
                        }
                    )
        for day, feed in sorted(due, key=lambda value: (value[0], value[1]["dataset_key"])):
            self._enqueue(
                {
                    "stable_key": f"daily:{self.execution_profile}:{feed['dataset_key']}:{day}",
                    "kind": "daily",
                    "dataset_key": feed["dataset_key"],
                    "trade_date": day.isoformat(),
                    "execution_profile": self.execution_profile,
                    "scope": {"feed": feed},
                }
            )
        try:
            with claimed_work(
                self.source,
                self.provider,
                accepted_kinds=["daily", "maintenance", "rerun"],
                execution_profile=self.execution_profile,
            ) as owner:
                if owner is None:
                    return health()
                try:
                    work = owner.work
                    if work["kind"] == "rerun":
                        owner.request("execute-rerun")
                        self._settle_queued_round(owner, outcome="completed")
                    elif work["kind"] == "maintenance":
                        self._run_queued_maintenance(owner)
                    else:
                        installed = next(
                            (
                                feed
                                for feed in self.installed_feeds
                                if feed["dataset_key"] == work["dataset_key"]
                            ),
                            None,
                        )
                        if installed is None:
                            self._settle_retired_work(owner)
                            return health()
                        if work["state"] != "open" and not work.get("manifest"):
                            owner.settle("retry", failure_code="RECOVERED_CLOSED_ROUND")
                            return health()
                        feed = work["scope"].get("feed") or installed
                        if feed["dataset_key"] != installed["dataset_key"]:
                            raise ValueError("daily frozen feed differs from work dataset")
                        day = date.fromisoformat(work["trade_date"])
                        if self.daily_policy and admitted and not work.get("drain_only"):
                            # Freeze all same-date admitted siblings before any SDK
                            # dispatch, even though only this work owns acquisition.
                            siblings = {
                                sibling["dataset_key"]: sibling
                                for due_day, sibling in due
                                if due_day == day and sibling["dataset_key"] in admitted
                            }
                            for sibling in siblings.values():
                                sibling_plan = self._plan_for_date(sibling, day)
                                self.state.clear_date(self.provider, sibling["dataset_key"], day)
                                if sibling_plan["status"] != "skipped_calendar":
                                    self.state.record_plan(sibling_plan, self.provider)
                        plan = self._plan_for_date(feed, day)
                        if plan["status"] == "skipped_calendar":
                            owner.freeze(
                                {"members": [{"key": "calendar_skip"}], "calendar_skip": True}
                            )
                            owner.settle("completed", proof={"no_data": ["calendar_skip"]})
                        else:
                            self.state.record_plan(plan, self.provider)
                            release = self.source.release(plan["release_id"])
                            if self.adapter.batch:
                                self._deliver_batch_plan(owner, plan, release, feed)
                            else:
                                owner.freeze(
                                    {
                                        "mode": "canonical",
                                        "plan_id": plan["plan_id"],
                                        "members": [
                                            {"key": key}
                                            for part in plan["parts"]
                                            for key in part["member_keys"]
                                        ],
                                    }
                                )
                                self._deliver_plan(
                                    plan,
                                    release,
                                    feed,
                                    drain_only=bool(work.get("drain_only")) or not bool(admitted),
                                )
                                self._settle_queued_round(owner)
                except (
                    FullMarketProtocolError,
                    SourceAPITransportError,
                    httpx.TransportError,
                ) as exc:
                    from findb_fetcher.provider_work import ProviderWorkRecoveryBlockedError

                    if (
                        isinstance(exc, FullMarketProtocolError)
                        and exc.code == "PROVIDER_WORK_FENCE"
                    ):
                        raise ProviderWorkRecoveryBlockedError("daily owner fence differs") from exc
                    retryable = not isinstance(exc, FullMarketProtocolError) or (
                        exc.retryable
                        or exc.code == "PROVIDER_WORK_SUSPENDED"
                        or exc.status_code in {408, 429}
                        or (exc.status_code is not None and exc.status_code >= 500)
                    )
                    self._settle_queued_round(
                        owner,
                        outcome="retry" if retryable else "failed",
                        failure_code="DAILY_SOURCE_UNAVAILABLE"
                        if retryable
                        else "DAILY_SOURCE_REJECTED",
                    )
                except ProviderHTTPError as exc:
                    self._settle_queued_round(
                        owner,
                        outcome="retry" if _retryable_http_status(exc.status_code) else "failed",
                        failure_code="DAILY_PROVIDER_HTTP_" + str(exc.status_code or "UNKNOWN"),
                    )
                except QuotaBlockedError as exc:
                    self._settle_queued_round(
                        owner,
                        outcome="retry"
                        if exc.quota_wait
                        or exc.status_code in {408, 429}
                        or (exc.status_code is not None and exc.status_code >= 500)
                        else "failed",
                        failure_code="DAILY_PROVIDER_QUOTA_OR_ACQUISITION",
                    )
                except SourceAPIResponseError as exc:
                    permanent = permanent_source_failure(exc)
                    self._settle_queued_round(
                        owner,
                        outcome="failed" if permanent else "retry",
                        failure_code="DAILY_SOURCE_REJECTED"
                        if permanent
                        else "DAILY_SOURCE_UNAVAILABLE",
                    )
                except RawStorageUploadError as exc:
                    self._settle_queued_round(
                        owner,
                        outcome="retry" if exc.retryable else "failed",
                        failure_code="DAILY_PROVIDER_UNAVAILABLE"
                        if exc.retryable
                        else "DAILY_PROVIDER_REJECTED",
                    )
                except (HistoricalWorkDeferredError, ProviderWorkAcquisitionStopped):
                    self._settle_queued_round(
                        owner, outcome="retry", failure_code="DAILY_ACQUISITION_DEFERRED"
                    )
                except DatedUniversePendingError as exc:
                    self.pause()
                    self._settle_queued_round(
                        owner,
                        outcome="retry",
                        failure_code="DATED_UNIVERSE_PENDING",
                        retry_delay_seconds=exc.retry_after_seconds,
                    )
                except ProviderPublicationPendingError as exc:
                    self.pause()
                    self._settle_queued_round(
                        owner,
                        outcome="retry",
                        failure_code="DAILY_PROVIDER_NOT_READY",
                        retry_delay_seconds=exc.retry_after_seconds,
                    )
                except ProviderOutagePendingError as exc:
                    self.pause()
                    self._settle_queued_round(
                        owner,
                        outcome="retry",
                        failure_code="DAILY_PROVIDER_UNAVAILABLE",
                        retry_delay_seconds=exc.retry_after_seconds,
                    )
                except ProviderWorkRejectedError as exc:
                    self.pause()
                    self._settle_queued_round(owner, outcome="failed", failure_code=exc.code)
                except CalendarDecisionRepairRequiredError:
                    self.pause()
                    self._settle_queued_round(
                        owner,
                        outcome="failed",
                        failure_code="CALENDAR_DECISION_REPAIR_REQUIRED",
                    )
                except (ReadinessBlockedError, ValueError):
                    self._settle_queued_round(
                        owner, outcome="failed", failure_code="DAILY_PLANNING_FORMAT_OR_SCOPE"
                    )
                except Exception as exc:
                    # Provider failures are classified by the adapter; others propagate.
                    failure = self.adapter.classify(exc, "round")
                    if failure.kind not in {FailureKind.ROUND_RETRY, FailureKind.WORK_FAILED}:
                        raise
                    retryable = failure.kind is FailureKind.ROUND_RETRY
                    self._settle_queued_round(
                        owner,
                        outcome="retry" if retryable else "failed",
                        failure_code=(
                            "DAILY_PROVIDER_HTTP_" + str(failure.status_code or "UNKNOWN")
                            if failure.http
                            else "DAILY_PROVIDER_UNAVAILABLE"
                            if retryable
                            else "DAILY_PROVIDER_REJECTED"
                        ),
                    )
                finally:
                    # Persistent SDK sessions must be stopped and reaped before
                    # ownership can leave this process. HTTP calls are bounded.
                    self.pause()
        except ProviderWorkWaitingError:
            pass
        return health()

    def _deliver_batch_plan(self, owner, plan: dict, release: dict, feed: dict) -> None:
        """A whole-cohort provider query, delivered and checkpointed member by member."""
        contract = self.contracts[plan["dataset_key"]]
        snapshot_key = self.adapter.batch_snapshot_key or ""
        deliveries = {
            key: {"slot_id": feed["slot_id"], "work_item_id": part["work_item_id"]}
            for part in plan["parts"]
            for key in part["member_keys"]
        }
        members = [row for row in release["members"] if row["member_key"] in deliveries]
        owner.freeze(
            {
                "mode": "canonical",
                "plan_id": plan["plan_id"],
                "contract": {
                    "schema_id": contract["schema_id"],
                    "schema_version": contract["current_version"],
                },
                "members": [
                    {
                        "key": m["member_key"],
                        "source_symbol": m["provider_symbol"],
                        "mapping_status": m["mapping_status"],
                    }
                    for m in members
                ],
            }
        )

        def checkpoint_receipt(key: str, request: dict, run_id: str) -> None:
            work_key = deliveries[key]["work_item_id"] + ":" + key
            self.state.prepare(work_key, canonical_bytes([request]))
            self.state.save_sequence_receipt(work_key, request["idempotency_key"], run_id)
            self.state.finish(work_key, status="prepared", receipt=run_id)

        retry_delay = self._publication_retry_delay(owner, feed)
        if (
            owner.journal.has_response_observation(snapshot_key)
            and owner.journal.response(snapshot_key) is None
            and self._calendar_repair_required(owner)
        ):
            for key, delivery in deliveries.items():
                self.state.finish(
                    delivery["work_item_id"] + ":" + key,
                    status="manual",
                    reason="calendar_repair_required",
                )
            raise CalendarDecisionRepairRequiredError("published calendar is now closed") from None
        if (
            owner.work.get("kind") == "daily"
            and not owner.journal.has_response_observation(snapshot_key)
            and any(member["mapping_status"] == "mapped" for member in members)
            and self._calendar_repair_required(owner)
        ):
            # The first snapshot is new provider I/O. A legacy planned decision
            # that is now a published closure stops here without a request.
            raise CalendarDecisionRepairRequiredError("published calendar is now closed")
        try:
            with authorization_scope(
                lambda: self.source.authorization(
                    "GET",
                    "full-market/authorization?consumer=" + self.execution_profile,
                    required_dataset=plan["dataset_key"],
                ),
                state_path=self.state.path,
            ):
                evidence = self.adapter.acquire_batch(
                    members,
                    BatchContext(
                        dataset_key=plan["dataset_key"],
                        trade_date=date.fromisoformat(plan["trade_date"]),
                        deliveries=deliveries,
                        delivery=self.delivery,
                        raw_store=self.raw_store,
                        on_receipt=checkpoint_receipt,
                        defer_missing=retry_delay > 0,
                    ),
                )
        except ProviderPublicationPendingError:
            if self._calendar_repair_required(owner):
                for key, delivery in deliveries.items():
                    self.state.finish(
                        delivery["work_item_id"] + ":" + key,
                        status="manual",
                        reason="calendar_repair_required",
                    )
                raise CalendarDecisionRepairRequiredError(
                    "published calendar is now closed"
                ) from None
            raise ProviderPublicationPendingError(
                self.adapter.batch_pending_reason or "provider_no_source_row",
                retry_after_seconds=retry_delay,
            ) from None
        if any(row.get("reason") == "no_source_row" for row in evidence["members"]) and (
            self._calendar_repair_required(owner)
        ):
            for key, delivery in deliveries.items():
                self.state.finish(
                    delivery["work_item_id"] + ":" + key,
                    status="manual",
                    reason="calendar_repair_required",
                )
            raise CalendarDecisionRepairRequiredError("published calendar is now closed") from None
        for result in evidence["members"]:
            key = result["member_key"]
            delivery = deliveries[key]
            if result["outcome"] in {"invalid", "mapping_gap"}:
                self.source.outcome(
                    plan["plan_id"],
                    work_item_id=delivery["work_item_id"],
                    member_key=key,
                    reason="mapping_gap" if result["outcome"] == "mapping_gap" else "source_error",
                )
            self.state.finish(
                delivery["work_item_id"] + ":" + key,
                status="complete" if result["outcome"] == "data" else "manual",
                reason=result["outcome"],
            )
        self.state.evaluate(self.source.evaluate(plan["plan_id"]))
        owner.settle(self.adapter.batch_status(evidence), proof={"eod": evidence})

    def _settle_queued_round(
        self,
        owner,
        *,
        outcome: str | None = None,
        failure_code: str | None = None,
        retry_delay_seconds: int = 60,
    ) -> None:
        """Close acceptance, drain every accepted run, then settle this round."""
        self.pause()
        owner.request("close")
        scheduling = None
        pending_errors = []
        plan_id = (owner.journal.manifest() or {}).get("plan_id")
        if owner.work["kind"] == "daily" and plan_id:
            # The next no-data retry round waits from this settlement, not from each member.
            self.state.advance_no_data_round(plan_id, now=time.time())
            self._record_finished_runs(plan_id)
            with self.state.connection() as db:
                pending_errors = sorted(
                    row[0]
                    for row in db.execute(
                        "SELECT member_key FROM full_work WHERE plan_id=? AND reason='source_error_pending'",
                        (plan_id,),
                    )
                )
        if self.daily_policy and owner.work["kind"] == "daily":
            manifest = owner.journal.manifest() or {}
            plan_id = manifest.get("plan_id")
            if plan_id:
                with self.state.connection() as db:
                    manual_bodies = db.execute(
                        "SELECT body FROM full_work WHERE plan_id=? AND status='manual' AND body IS NOT NULL",
                        (plan_id,),
                    ).fetchall()
                manual_keys = {
                    request["idempotency_key"]
                    for row in manual_bodies
                    for request in json.loads(row["body"])
                }
                owner.drain_prepared(pending_only=True, skip=manual_keys)
                progress = self.state.plan_progress(plan_id)
                scheduling = {
                    **(progress["last_batch"] or {}),
                    "counts": progress["counts"],
                    "failures": progress["failures"],
                }
        while True:
            desired = outcome
            mapping_gaps = None
            if desired is None:
                plan = self.source.evaluate(owner.journal.manifest()["plan_id"])
                self.state.evaluate(plan)
                manual = self.state.has_blocking_manual(plan["plan_id"])
                with self.state.connection() as db:
                    pending = db.execute(
                        "SELECT 1 FROM full_work WHERE plan_id=? AND status NOT IN ('complete','manual') LIMIT 1",
                        (plan["plan_id"],),
                    ).fetchone()
                desired = (
                    "completed"
                    if plan["status"] == "complete"
                    else "failed"
                    if (manual and not self.daily_policy) or not pending
                    else "retry"
                )
                if desired == "failed" and not manual and not pending:
                    # Nothing is left to acquire and no retry maps a symbol the
                    # provider lacks: the work is partial, not a failure.
                    mapping_gaps = only_mapping_gaps(plan)
                    if mapping_gaps is not None:
                        desired = "partial"
                no_data_retry_at = self.state.no_data_retry_at(plan["plan_id"])
                if desired == "retry" and no_data_retry_at is not None and not pending_errors:
                    # Every remaining member is backing off, so an earlier round would be empty.
                    retry_delay_seconds = max(
                        retry_delay_seconds, math.ceil(no_data_retry_at - time.time())
                    )
            try:
                proof: dict[str, Any] = (
                    {
                        "no_data": [
                            row["key"]
                            for row in (owner.work.get("manifest") or {}).get("members", [])
                        ]
                    }
                    if desired == "completed" and owner.work["kind"] == "maintenance"
                    else {}
                )
                if scheduling is not None:
                    proof["scheduling"] = scheduling
                    if desired == "retry" and scheduling.get("reason") == "batch_yield":
                        retry_delay_seconds = 1
                    if (
                        desired == "retry"
                        and not pending_errors
                        and not self.runtime_window()["open"]
                    ):
                        retry_delay_seconds = max(
                            retry_delay_seconds,
                            math.ceil(
                                (
                                    datetime.fromisoformat(self.runtime_window()["next_open_at"])
                                    - datetime.now(timezone.utc)
                                ).total_seconds()
                            ),
                        )
                if mapping_gaps is not None:
                    proof["mapping_gaps"] = mapping_gaps
                if desired == "failed":
                    proof = {
                        **proof,
                        "manual_pending_prepared": owner.prepared_keys(),
                        "manual_reason": "frozen_scope_requires_manual_review",
                    }
                if desired == "retry" and pending_errors:
                    proof["pending_source_errors"] = pending_errors
                owner.settle(
                    desired,
                    delay=retry_delay_seconds,
                    failure_code=failure_code
                    or ("MANUAL_INTERVENTION_REQUIRED" if desired == "failed" else None),
                    proof=proof,
                )
                return
            except FullMarketProtocolError as exc:
                if exc.code != "PROVIDER_WORK_DRAINING":
                    from findb_fetcher.provider_work import ProviderWorkRecoveryBlockedError

                    raise ProviderWorkRecoveryBlockedError(
                        "provider settlement requires recovery"
                    ) from exc
                # Source owns the terminal drain decision; waiting neither
                # acquires a new member nor consumes an acquisition attempt.
                time.sleep(2)
                if owner.failed.is_set():
                    raise ReadinessBlockedError("provider ownership recovery required")

    def _existing_plan(self, feed: dict[str, Any], day: date) -> dict[str, Any] | None:
        plans = self.source.plans_for_date(dataset=feed["dataset_key"], trade_date=day.isoformat())[
            "data"
        ]
        if not isinstance(plans, list) or len(plans) > 1:
            raise ReadinessBlockedError("Source frozen plan lookup invalid")
        if not plans:
            return None
        return self._validate_plan_identity(plans[0], feed, day)

    def _validate_plan_identity(
        self, plan: dict[str, Any], feed: dict[str, Any], day: date
    ) -> dict[str, Any]:
        if (
            plan["dataset_key"] != feed["dataset_key"]
            or plan["trade_date"] != day.isoformat()
            or plan["provider"] != self.provider
            or plan.get("execution_profile") != self.execution_profile
        ):
            raise ReadinessBlockedError("Source frozen plan identity invalid")
        return plan

    def _plan_for_date(self, feed: dict[str, Any], day: date) -> dict[str, Any]:
        existing = self._existing_plan(feed, day)
        if existing is not None:
            return existing
        try:
            plan = self.source.plan(
                dataset=feed["dataset_key"],
                provider=self.provider,
                trade_date=day.isoformat(),
                release_id=self._release_for_date(feed, day)["release_id"],
            )
            return self._validate_plan_identity(plan, feed, day)
        except FullMarketProtocolError as exc:
            if exc.status_code == 409:
                # Another scheduler may freeze a plan between the read and POST.
                existing = self._existing_plan(feed, day)
                if existing is not None:
                    return existing
                if exc.code is not None and exc.code == self.adapter.dated_universe_code:
                    dataset = feed["dataset_key"]
                    if not self.state.date_prerequisite_due(self.provider, dataset, day):
                        raise DatedUniversePendingError(
                            "dated universe retry is pending",
                            retry_after_seconds=self.state.date_prerequisite_delay(
                                self.provider, dataset, day
                            ),
                        ) from None
                    try:
                        release = self._dated_universe(feed, day)
                    except DatedUniversePendingError as pending:
                        self.state.block_date(self.provider, dataset, day, str(pending))
                        raise
                    try:
                        plan = self.source.plan(
                            dataset=dataset,
                            provider=self.provider,
                            trade_date=day.isoformat(),
                            release_id=release["release_id"],
                        )
                        return self._validate_plan_identity(plan, feed, day)
                    except FullMarketProtocolError as retry:
                        if retry.status_code == 409:
                            existing = self._existing_plan(feed, day)
                            if existing is not None:
                                return existing
                        raise
            raise

    @_governed
    def _dated_universe(self, feed: dict[str, Any], day: date) -> dict[str, Any]:
        """Submit one date's universe built from a provider observation of that date."""
        dataset = feed["dataset_key"]
        universe = self.adapter.dated_universe(
            day, self._provider_context(dataset, day, feed, delivery={})
        )
        stored = self._persist(universe.raw, dataset, "dated_universe")
        for member in universe.members:
            member["raw_evidence_ref"] = stored.ref
        result = self.source.submit(
            universe_request(
                dataset_key=dataset,
                provider=self.provider,
                effective_date=day,
                observed_at=datetime.now(timezone.utc),
                source_timezone=feed["source_timezone"],
                snapshots={universe.url: universe.raw},
                members=universe.members,
            )
        )
        if (
            result.get("provider") != self.provider
            or result.get("dataset_key") != dataset
            or result.get("effective_date") != day.isoformat()
        ):
            raise ReadinessBlockedError("dated universe response identity invalid")
        # Dated-universe cycles skip catalogue refresh, so health would otherwise go stale.
        self.state.record_dated_release(
            self.provider, now=datetime.now(timezone.utc).timestamp(), release=result
        )
        if result.get("status") != "published":
            raise DatedUniversePendingError("dated_universe_pending")
        return result

    def _release_for_date(self, feed: dict[str, Any], day: date) -> dict[str, Any]:
        listed = self.source.universes(feed["dataset_key"], self.provider, as_of=day.isoformat())
        if not listed.get("published_release_id"):
            raise ReadinessBlockedError("no published universe applies to catchup date")
        return self.source.release(listed["published_release_id"])

    def _deliver_plan(
        self,
        plan: dict[str, Any],
        release: dict[str, Any],
        feed: dict[str, Any],
        *,
        drain_only: bool = False,
        drain_keys: set[str] | None = None,
    ) -> bool:
        self._batch_started = time.monotonic()
        self.last_batch = {"processed": 0, "reason": "scanned", "plan_id": plan["plan_id"]}
        self._timing = {"plan_id": plan["plan_id"], "since": self._batch_started, "members": 0}
        rpc_timing.drain()
        try:
            return self._deliver_members(
                plan, release, feed, drain_only=drain_only, drain_keys=drain_keys
            )
        finally:
            if self.daily_policy:
                self.last_batch["elapsed_seconds"] = round(
                    time.monotonic() - self._batch_started, 3
                )
                self.state.schedule_save(plan["plan_id"], batch=self.last_batch)
            self._log_delivery_timing(final=True)

    def _log_delivery_timing(self, *, final: bool = False) -> None:
        """Log ``{route template or step: [count, total_ms]}`` once a minute and at the end.

        Diagnostics only: a plan delivery can run for hours without a daily
        batch policy, and timing never enters the durable batch or settlement proof.
        """
        elapsed = time.monotonic() - self._timing["since"]
        if not final and elapsed < DELIVERY_TIMING_INTERVAL_SECONDS:
            return
        print(
            json.dumps(
                {
                    "delivery_timing": {
                        "plan_id": self._timing["plan_id"],
                        "members": self._timing["members"],
                        "seconds": round(elapsed, 3),
                        "final": final,
                        "timing": rpc_timing.drain(),
                    }
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
        self._timing.update(since=time.monotonic(), members=0)

    def _deliver_members(
        self,
        plan: dict[str, Any],
        release: dict[str, Any],
        feed: dict[str, Any],
        *,
        drain_only: bool = False,
        drain_keys: set[str] | None = None,
    ) -> bool:
        from findb_fetcher.provider_work import ProviderWorkSourceUnavailableError, require_owner

        owner = require_owner(self.provider)
        members = {member["member_key"]: member for member in release["members"]}
        entries = [(part, key) for part in plan["parts"] for key in part["member_keys"]]
        # Replay reports before eligibility, cursor rotation or any new provider
        # I/O. A failed final attempt must not hide an unacknowledged outcome.
        pending_reports = self.state.pending_source_error_keys(plan["plan_id"])
        if pending_reports:
            for part, member_key in entries:
                key = part["work_item_id"] + ":" + member_key
                if key in pending_reports:
                    self._report_source_error(plan, part, member_key, key)
                    return False
        offset = (
            self.state.schedule_cursor(plan["plan_id"]) % max(1, len(entries))
            if self.daily_policy
            else 0
        )
        ordered = list(enumerate(entries))
        ordered = ordered[offset:] + ordered[:offset]
        calendar_checked = False
        no_data_streak = 0
        # Once this pass sees acquisition closed (stopped, suspended or outside the
        # window) it stays closed: unprepared members are skipped without a restore
        # probe or another gate RPC each. Locally prepared bodies still deliver;
        # server-retained ones stay pending for the next running attempt.
        acquisition_closed = False
        for index, (part, member_key) in ordered:
            key = part["work_item_id"] + ":" + member_key
            if drain_keys is not None and key not in drain_keys:
                continue
            member = members[member_key]
            if self.daily_policy:
                if (
                    self.last_batch["processed"] >= self.daily_policy["max_members"]
                    or time.monotonic() - self._batch_started >= self.daily_policy["max_seconds"]
                ):
                    self.last_batch["reason"] = "batch_yield"
                    return True
            if not self.state.eligible(key, now=time.time()):
                continue
            if acquisition_closed and self.state.prepared(key) is None:
                continue
            if self.state.prepared(key) is None:
                restored = owner.restore(member_key=member_key)
                if restored:
                    self.state.prepare(
                        key, canonical_bytes([json.loads(body) for body in restored])
                    )
            if self.state.prepared(key) is None and (drain_only or not self._acquisition_allowed()):
                acquisition_closed = not drain_only
                continue
            if self.daily_policy and (
                self.last_batch["processed"] >= self.daily_policy["max_members"]
                or time.monotonic() - self._batch_started >= self.daily_policy["max_seconds"]
            ):
                self.last_batch["reason"] = "batch_yield"
                return True
            if (
                not calendar_checked
                and member["mapping_status"] == "mapped"
                and self.state.prepared(key) is None
                and (getattr(owner, "work", None) or {}).get("kind") == "daily"
                and self.state.eligible(key, now=time.time())
            ):
                # A legacy planned decision may now be a published closure. Probe
                # once per round before the first claim that needs provider I/O,
                # so neither a closed date nor an unavailable probe spends native
                # calls or member budgets. Member checkpoints stay untouched: the
                # round settles failed and finished no-trade evidence survives.
                calendar_checked = True
                if self._calendar_repair_required(owner):
                    raise CalendarDecisionRepairRequiredError("published calendar is now closed")
            if not self.state.claim(
                key,
                now=time.time(),
                next_cursor=(index + 1) % len(entries) if self.daily_policy else None,
            ):
                continue
            # A member whose every body already has a run only reads status: it
            # makes no provider call, so it spends none of the batch's budget.
            if self.daily_policy and self._saved_runs(key) is None:
                self.last_batch["processed"] += 1
            self._timing["members"] += 1
            self._log_delivery_timing()
            dispatch_before = owner.dispatch_observations
            try:
                if member["mapping_status"] != "mapped":
                    self.source.outcome(
                        plan["plan_id"],
                        work_item_id=part["work_item_id"],
                        member_key=member_key,
                        reason="mapping_gap",
                    )
                    self.state.finish(key, status="manual", reason="mapping_gap")
                    continue
                prepared = self.state.prepared(key)
                if prepared is None:
                    shared = self.adapter.shared_response_prefix
                    if (
                        shared is not None
                        and getattr(owner, "journal", None) is not None
                        and owner.journal.has_response_observation(shared, prefix=True)
                        and self._calendar_repair_required(owner)
                    ):
                        self.state.finish(
                            key,
                            status="manual",
                            reason="calendar_repair_required",
                        )
                        raise CalendarDecisionRepairRequiredError(
                            "published calendar is now closed"
                        ) from None
                    owner.authorize()
                    candidates = self._batch_candidates(part, member_key, members, drain_keys)
                    # Providers without shared requests keep the original call shape.
                    requests = self._acquire(
                        member, plan, part, release, feed, *([candidates] if candidates else [])
                    )
                    no_data_streak = 0
                    self.state.clear_no_data_trips(plan["plan_id"])
                    if self.adapter.outage_retry_seconds is not None:
                        self.state.clear_provider_outage(plan["plan_id"])
                    if not requests:
                        self.state.finish(key, status="complete", reason="source_proven_no_trade")
                        continue
                    for request in requests:
                        owner.reserve(request)
                    prepared = canonical_bytes(requests)
                    self.state.prepare(key, prepared)
                elif callable(getattr(owner, "prepared_predates_basis", None)) and any(
                    owner.prepared_predates_basis(request) for request in json.loads(prepared)
                ):
                    # Bodies mapped from split-adjusted responses are retained, never replayed.
                    owner.basis_blocked.update(
                        request["idempotency_key"] for request in json.loads(prepared)
                    )
                    self._price_basis_manual(plan, part, member_key, key)
                    continue
                normalizing = False
                prepared_requests = json.loads(prepared)
                for request in prepared_requests:
                    request_identity = request.get("idempotency_key") or checksum(
                        canonical_bytes(request)
                    )
                    saved_run = self.state.sequence_receipt(key, request_identity)
                    if saved_run is None and len(prepared_requests) == 1:
                        # Legacy checkpoints stored one receipt per member.
                        # Import only the unambiguous one-request case; each
                        # multi-sequence body replays its own immutable key.
                        saved_run = self.state.receipt(key)
                        if saved_run is not None:
                            self.state.save_sequence_receipt(key, request_identity, saved_run)
                    # An accepted body never needs another durable preparation
                    # round trip; checking its run only needs its identity.
                    immutable = (
                        delivered_snapshot(request)
                        if saved_run is not None
                        else self.delivery.prepare(request)
                    )
                    if saved_run is None:
                        receipt = self.delivery.deliver(immutable)
                        saved_run = str(receipt.run_id)
                        self.state.save_sequence_receipt(key, request_identity, saved_run)
                        self.state.finish(key, status="prepared", receipt=saved_run)
                    run = self.delivery.get_run_status(UUID(saved_run), expected=immutable)
                    if run.status in NONTERMINAL_RUN_STATUSES:
                        normalizing = True
                        break
                    if (
                        run.status != "completed"
                        or run.failed_records
                        or run.success_records != run.total_records
                    ):
                        self.state.finish(key, status="manual", reason="normalization_failed")
                        if run.status != "completed" or run.failed_records:
                            self.source.outcome(
                                plan["plan_id"],
                                work_item_id=part["work_item_id"],
                                member_key=member_key,
                                reason="source_error",
                            )
                        normalizing = True
                        saved_run = None
                        break
                if normalizing:
                    if saved_run is not None:
                        self.state.finish(
                            key, status="prepared", reason="normalizing", now=time.time()
                        )
                    continue
                self.state.finish(key, status="complete")
            except CalendarDecisionRepairRequiredError:
                raise
            except ProviderWorkSourceUnavailableError:
                if (
                    owner.dispatch_observations == dispatch_before
                    and self.state.prepared(key) is None
                ):
                    self.state.defer_wait(key, now=time.time(), reason="source_unavailable")
                raise
            except ProviderWorkAcquisitionStopped as exc:
                window_closed = (
                    isinstance(exc, DailyWindowClosedError) or not self.runtime_window()["open"]
                )
                if self.daily_policy:
                    self.last_batch["reason"] = (
                        "acquisition_window_closed" if window_closed else "suspended"
                    )
                if self.daily_policy and window_closed:
                    self.state.defer_wait(key, now=time.time(), reason="acquisition_window_closed")
                elif owner.dispatch_observations != dispatch_before:
                    self.state.finish(
                        key,
                        status="prepared" if self.state.prepared(key) else "pending",
                        reason="suspended",
                        now=time.time(),
                    )
                else:
                    self.state.defer_wait(key, now=time.time(), reason="suspended")
                return False
            except QuotaBlockedError as exc:
                limited = exc.quota_wait and not exc.dispatched
                if limited:
                    self._account_cooldown()
                if limited and (
                    self.daily_policy or owner.dispatch_observations == dispatch_before
                ):
                    self.state.defer_wait(key, now=time.time())
                    if self.daily_policy:
                        self.last_batch["reason"] = "quota_wait"
                    return False
                reason = "rate_limited" if limited else "source_error"
                self._report_member_failure(plan, part, member_key, key, exc, reason, reason)
                return False
            except ProviderWorkBasisMismatchError:
                self._price_basis_manual(plan, part, member_key, key)
                continue
            except MemberUnavailableError as exc:
                # Deterministic for this table: block only the member, without retries
                # that would download the shared table again.
                self.source.outcome(
                    plan["plan_id"],
                    work_item_id=part["work_item_id"],
                    member_key=member_key,
                    reason="empty_response" if exc.absent else "source_error",
                )
                self.state.finish(key, status="manual", reason="member_unavailable")
                self._record_diagnostic(key, exc)
                continue
            except SourceAPIResponseError as exc:
                from findb_fetcher.provider_work import ProviderWorkRecoveryBlockedError

                if exc.code == "PROVIDER_WORK_FENCE":
                    raise ProviderWorkRecoveryBlockedError(
                        "Source provider owner fence differs"
                    ) from None
                if exc.status_code == 429:
                    if (
                        self.state.prepared(key) is not None
                        or owner.dispatch_observations == dispatch_before
                    ):
                        self.state.defer_wait(key, now=time.time())
                        return False
                self.state.finish(
                    key,
                    status="manual"
                    if exc.status_code < 500 and exc.status_code != 429
                    else "prepared"
                    if self.state.prepared(key)
                    else "pending",
                    reason="source_rejected",
                    now=time.time(),
                )

                return False
            except Exception as exc:
                if isinstance(exc, FullMarketProtocolError) and exc.code == "PROVIDER_WORK_FENCE":
                    from findb_fetcher.provider_work import ProviderWorkRecoveryBlockedError

                    raise ProviderWorkRecoveryBlockedError(
                        "Source provider owner fence differs"
                    ) from None
                failure = self.adapter.classify(exc, "member")
                if failure.kind is FailureKind.WORK_FAILED:
                    # The provider rejects every member alike (credential,
                    # configuration): the work ends without charging this one.
                    self._record_diagnostic(key, exc)
                    self.state.defer_wait(key, now=time.time(), reason="provider_rejected")
                    raise ProviderWorkRejectedError(
                        failure.code or "provider_rejected", "DAILY_PROVIDER_REJECTED"
                    ) from None
                if failure.wait == "publication":
                    # The provider has not published the target date for the cohort yet.
                    retry_delay = self._publication_retry_delay(owner, feed)
                    if self._calendar_repair_required(owner):
                        self.state.finish(
                            key,
                            status="manual",
                            reason="calendar_repair_required",
                        )
                        raise CalendarDecisionRepairRequiredError(
                            "published calendar is now closed"
                        ) from None
                    if retry_delay > 0:
                        self.state.defer_wait(
                            key,
                            now=time.time(),
                            reason="target_not_ready",
                            delay_seconds=retry_delay,
                        )
                        raise ProviderPublicationPendingError(
                            failure.code or "provider_target_not_ready",
                            retry_after_seconds=retry_delay,
                        ) from None
                    self.state.finish(
                        key,
                        status="manual",
                        reason="target_not_ready",
                    )
                    return False
                if failure.wait is None and failure.kind in {
                    FailureKind.MEMBER,
                    FailureKind.QUOTA_WAIT,
                    FailureKind.ROUND_RETRY,
                }:
                    if failure.kind is FailureKind.QUOTA_WAIT:
                        self._account_cooldown()
                    self._report_member_failure(
                        plan,
                        part,
                        member_key,
                        key,
                        exc,
                        failure.reason,
                        "no_data" if failure.no_data else failure.reason,
                    )
                    # One symbol without data says nothing about the provider or account,
                    # so it keeps its bounded retries without stopping the other members.
                    # A long unbroken run does, so it holds the plan to bound its calls.
                    if failure.no_data:
                        if self.state.counts_toward_no_data_breaker(key):
                            no_data_streak += 1
                        if no_data_streak < FULL_WORK_NO_DATA_BREAKER:
                            continue
                        self.state.record_no_data_trip(
                            plan["plan_id"],
                            until=time.time() + FULL_WORK_NO_DATA_BREAKER_HOLD_SECONDS,
                        )
                    return False
                if failure.wait == "outage":
                    # One shared report serves every member: an outage keeps the
                    # member failure budget and ends this round for a later retry,
                    # until the plan's outage exceeds its bounded retry window.
                    delay = max(60, math.ceil(failure.retry_after))
                    deferred = self.state.defer_provider_outage(
                        key,
                        now=time.time(),
                        delay_seconds=delay,
                        limit_seconds=self.adapter.outage_retry_seconds or 0,
                    )
                    if self.adapter.outage_holds_plan:
                        # Every member is read from the observation that failed, so
                        # the next one would only repeat the failure a minute later.
                        self._record_diagnostic(key, exc)
                        if deferred:
                            raise ProviderOutagePendingError(
                                failure.code or "provider_unavailable",
                                retry_after_seconds=delay,
                            ) from None
                        # Return the claim: the member never had a table to be read from.
                        self.state.defer_wait(key, now=time.time(), reason="provider_unavailable")
                        raise ProviderWorkRejectedError(
                            failure.code or "provider_unavailable",
                            "DAILY_PROVIDER_OUTAGE_EXHAUSTED",
                        ) from None
                    if deferred:
                        return False
                    self.state.stage_source_error(key)
                    self.source.outcome(
                        plan["plan_id"],
                        work_item_id=part["work_item_id"],
                        member_key=member_key,
                        reason="source_error",
                    )
                    self.state.finish(key, status="manual", reason="retry_exhausted")
                    return False
                # Provider/SDK messages never enter durable state or logs; only
                # the bounded failure class is retained for traceability.
                self.state.stage_source_error(key)
                self._record_diagnostic(key, exc)
                self._report_source_error(plan, part, member_key, key)
                return False

        return True

    def _batch_candidates(
        self,
        part: dict[str, Any],
        member_key: str,
        members: dict[str, dict[str, Any]],
        drain_keys: set[str] | None,
    ) -> Callable[[], tuple[str, ...]] | None:
        """Later members of this part that this pass will acquire, for a shared request.

        Only members that would reach a provider call now qualify: mapped, without
        a prepared body, and eligible (no lease, backoff or pending no-data round).
        """
        limit = getattr(self.adapter, "member_batch_limit", 0)
        if limit < 2:
            return None

        def candidates() -> tuple[str, ...]:
            now = time.time()
            symbols = [members[member_key]["provider_symbol"]]
            keys = part["member_keys"]
            for other in keys[keys.index(member_key) + 1 :]:
                key = part["work_item_id"] + ":" + other
                candidate = members[other]
                if (
                    candidate["mapping_status"] != "mapped"
                    or (drain_keys is not None and key not in drain_keys)
                    or self.state.prepared(key) is not None
                    or not self.state.eligible(key, now=now)
                ):
                    continue
                symbols.append(candidate["provider_symbol"])
                if len(symbols) == limit:
                    break
            return tuple(dict.fromkeys(symbols))

        return candidates

    def _record_finished_runs(self, plan_id: str) -> None:
        """Complete members whose accepted runs finished while this round closed.

        Settlement waits for those runs anyway. Left as ``normalizing``, each would
        schedule an attempt a minute later only to read its run status, so every
        no-data retry round would take two attempts. A failed or unreadable run keeps
        its regular revisit, which reports it as before.
        """
        deadline = time.monotonic() + NORMALIZING_SETTLE_WAIT_SECONDS
        while True:
            with self.state.connection() as db:
                keys = [
                    row[0]
                    for row in db.execute(
                        "SELECT key FROM full_work WHERE plan_id=? AND status='prepared' "
                        "AND reason='normalizing'",
                        (plan_id,),
                    )
                ]
            waiting = False
            for key in keys:
                try:
                    runs, prepared = self._saved_runs(key), self.state.prepared(key)
                    if runs is None or prepared is None:
                        continue
                    statuses = [
                        self.delivery.get_run_status(UUID(run), expected=delivered_snapshot(body))
                        for body, run in zip(json.loads(prepared), runs, strict=True)
                    ]
                except (ValueError, KeyError, TypeError):
                    # A body this check cannot read keeps its regular revisit.
                    continue
                except Exception:
                    return
                if any(run.status in NONTERMINAL_RUN_STATUSES for run in statuses):
                    waiting = True
                elif all(
                    run.status == "completed"
                    and not run.failed_records
                    and run.success_records == run.total_records
                    for run in statuses
                ):
                    self.state.finish(key, status="complete")
            if not waiting or time.monotonic() >= deadline:
                return
            time.sleep(2)

    def _saved_runs(self, key: str) -> list[str] | None:
        """Runs for every prepared body of a member, or None while any needs delivery."""
        prepared = self.state.prepared(key)
        if prepared is None:
            return None
        requests = json.loads(prepared)
        runs = []
        for request in requests:
            run = self.state.sequence_receipt(
                key, request.get("idempotency_key") or checksum(canonical_bytes(request))
            )
            if run is None and len(requests) == 1:
                run = self.state.receipt(key)
            if run is None:
                return None
            runs.append(run)
        return runs

    def _price_basis_manual(
        self, plan: dict[str, Any], part: dict[str, Any], member_key: str, key: str
    ) -> None:
        """Retain pre-basis bytes for manual review; never retry, refetch or replay them."""
        self.source.outcome(
            plan["plan_id"],
            work_item_id=part["work_item_id"],
            member_key=member_key,
            reason="source_error",
        )
        self.state.finish(key, status="manual", reason="price_basis_manual_review")
        self._record_diagnostic(key, ProviderWorkBasisMismatchError("price basis"))

    def _account_cooldown(self) -> None:
        self.state.rate_limited(
            account=self.provider,
            window=datetime.now(timezone.utc).date().isoformat(),
            until=time.time() + 60,
        )

    def _report_member_failure(
        self,
        plan: dict[str, Any],
        part: dict[str, Any],
        member_key: str,
        key: str,
        exc: BaseException,
        reason: str,
        finish_reason: str,
    ) -> None:
        """Report one classified member failure; its retry budget stays durable."""
        self.source.outcome(
            plan["plan_id"],
            work_item_id=part["work_item_id"],
            member_key=member_key,
            reason=reason,
        )
        self.state.finish(
            key,
            status="prepared" if self.state.prepared(key) else "pending",
            reason=finish_reason,
            now=time.time(),
        )
        self._record_diagnostic(key, exc)

    def _record_diagnostic(self, key: str, exc: BaseException) -> None:
        """Best effort: a diagnostic write never suppresses the failure's own report."""
        try:
            self._write_diagnostic(key, exc)
        except (sqlite3.Error, FullMarketStateError):
            pass

    def _write_diagnostic(self, key: str, exc: BaseException) -> None:
        if isinstance(exc, ProviderWorkBasisMismatchError):
            self.state.record_diagnostic(
                key, provider=self.provider, category="price_basis_mismatch"
            )
        elif (fields := self.adapter.diagnostic(exc)) is not None:
            self.state.record_diagnostic(key, provider=self.provider, **fields)
        elif isinstance(exc, MemberUnavailableError):
            self.state.record_diagnostic(key, provider=self.provider, category=f"member:{exc.code}")
        elif isinstance(exc, QuotaBlockedError):
            self.state.record_diagnostic(
                key,
                provider=self.provider,
                category="quota_blocked",
                http_status=exc.status_code,
                dispatched=exc.dispatched,
            )
        else:
            name = re.sub(r"[^A-Za-z0-9_]", "", type(exc).__name__).lstrip("_0123456789")
            self.state.record_diagnostic(
                key, provider=self.provider, category="exception:" + (name[:60] or "Unknown")
            )

    def _report_source_error(
        self, plan: dict[str, Any], part: dict[str, Any], member_key: str, key: str
    ) -> None:
        self.source.outcome(
            plan["plan_id"],
            work_item_id=part["work_item_id"],
            member_key=member_key,
            reason="source_error",
        )
        self.state.finish(
            key,
            status="prepared" if self.state.prepared(key) else "pending",
            reason="source_error",
            now=time.time(),
        )

    def _persist(
        self,
        raw: bytes,
        dataset: str,
        symbol: str,
        *,
        payload_format: RawPayloadFormat = "json",
    ) -> RawObject:
        with rpc_timing.timed("step raw_persist"):
            stored = self.raw_store.persist(
                raw,
                dataset_key=dataset,
                source_symbol=symbol.replace("|", ":"),
                provider=self.provider,
                payload_format=payload_format,
            )
        if stored.sha256 != checksum(raw) or stored.size_bytes != len(raw):
            raise ReadinessBlockedError("R2 raw persistence identity differs from acquired bytes")
        return stored

    def _acquisition_context(
        self,
        plan: dict[str, Any],
        part: dict[str, Any],
        release: dict[str, Any],
        feed: dict[str, Any],
    ) -> AcquisitionContext:
        """The narrow view of one owned member acquisition that an adapter may use."""
        day = date.fromisoformat(plan["trade_date"])
        now = datetime.now(timezone.utc)
        delivery = {
            "slot_id": feed["slot_id"],
            "scheduled_for": now.isoformat(),
            "target_data_date": day.isoformat(),
            "work_item_id": part["work_item_id"],
        }
        effective = release.get("effective_date")
        return replace(
            self._provider_context(
                plan["dataset_key"],
                day,
                feed,
                delivery=delivery,
                fetched_at=now,
                cohort=release.get("members"),
            ),
            universe_effective_date=date.fromisoformat(effective)
            if isinstance(effective, str)
            else None,
        )

    def _provider_context(
        self,
        dataset: str,
        day: date,
        feed: dict[str, Any],
        *,
        delivery: dict[str, Any],
        fetched_at: datetime | None = None,
        cohort: list[dict[str, Any]] | None = None,
    ) -> AcquisitionContext:
        return AcquisitionContext(
            dataset_key=dataset,
            trade_date=day,
            fetched_at=fetched_at or datetime.now(timezone.utc),
            feed=feed,
            delivery=delivery,
            contract=self.contracts.get(dataset),
            limits=self.proof,
            http=self.http,
            persist=lambda raw, symbol, **kwargs: self._persist(raw, dataset, symbol, **kwargs),
            observe_response=self._observed_response,
            rate_limited=self._rate_limited,
            snapshot=self._snapshot,
            cohort=cohort,
        )

    @_governed
    def _acquire(
        self,
        member: dict[str, Any],
        plan: dict[str, Any],
        part: dict[str, Any],
        release: dict[str, Any],
        feed: dict[str, Any],
        batch_candidates: Callable[[], tuple[str, ...]] | None = None,
    ) -> list[dict[str, Any]]:
        context = self._acquisition_context(plan, part, release, feed)
        if batch_candidates is not None:
            context = replace(context, batch_candidates=batch_candidates)
        with rpc_timing.timed("step provider_acquire"):
            result = self.adapter.acquire(member, context)
        if result.no_trade is not None:
            self.source.outcome(
                plan["plan_id"],
                work_item_id=part["work_item_id"],
                member_key=member["member_key"],
                reason="no_trade",
                evidence=result.no_trade,
            )
            return []
        request, stored = result.request, result.raw_object
        if request is None or stored is None:
            raise ReadinessBlockedError("provider acquisition produced no request")
        identity = checksum(
            canonical_bytes(
                {
                    "plan": plan["plan_id"],
                    "member": member["member_key"],
                    "rows": request["payload"]["data"],
                }
            )
        )
        request["request_key"] = f"full:{identity}"
        request["idempotency_key"] = f"full:{identity}"
        attach_raw_object(request, stored)
        if len(canonical_bytes(request)) >= 1024 * 1024:
            raise ReadinessBlockedError("prepared Source body exceeds byte bound")
        return [request]
