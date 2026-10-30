"""Activation-forward full-market delivery with durable provider/account boundaries."""

from __future__ import annotations

import importlib.metadata
import json
import math
import os
import re
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx

from findb_fetcher.acquisition import authorization_scope, http_acquisition
from findb_fetcher.client import (
    SourceAPIClient,
    SourceAPIResponseError,
    SourceAPITransportError,
    permanent_source_failure,
)
from findb_fetcher.daily_runtime import window
from findb_fetcher.full_market_client import FullMarketProtocolError, FullMarketSourceClient
from findb_fetcher.full_market_state import (
    FULL_WORK_MAX_ATTEMPTS,
    FullMarketState,
    QuotaBlockedError,
)
from findb_fetcher.full_market_universe import (
    HKEX_URL,
    MAX_SNAPSHOT_BYTES,
    NASDAQ_URLS,
    TAIFEX_HISTORY_URL,
    TAIFEX_URL,
    TW_COMPANY_URLS,
    TW_ISIN_URLS,
    canonical_bytes,
    canonical_twelve_data_mic,
    checksum,
    map_catalogue,
    nasdaq_effective_date,
    parse_hkex,
    parse_nasdaq,
    parse_tw_companies,
    parse_tw_etfs,
    parse_tw_ordinary,
    universe_request,
)
from findb_fetcher.historical_backfill import HistoricalWorkDeferredError
from findb_fetcher.http_response import BoundedResponseError, read_identity_response
from findb_fetcher.provider_process import ProviderProcessError
from findb_fetcher.provider_wait import (
    CalendarDecisionRepairRequiredError,
    ProviderPublicationPendingError,
)
from findb_fetcher.provider_work import ProviderWorkAcquisitionStopped, durable_response
from findb_fetcher.providers.factset import (
    FactSetConfig,
    FactSetGateway,
    FactSetOperationalError,
    map_current_cohort,
)
from findb_fetcher.providers.finlab import (
    FinLabConfigError,
    FinLabDatasetConfig,
    FinLabDatasetTable,
    FinLabOperationalError,
    FinLabSdkError,
    FinLabSdkGateway,
    FinLabSymbol,
    FinLabTargetNotReadyError,
    build_finlab_dataset_bundle,
)
from findb_fetcher.providers.finlab import (
    build_market_eod_request as finlab_request,
)
from findb_fetcher.providers.shioaji import (
    SHIOAJI_SDK_VERSION,
    ShioajiEmptyResponseError,
    ShioajiSdkError,
    _simulation_from_env,
    build_market_minute_request,
)
from findb_fetcher.providers.shioaji_session import PersistentIsolatedShioajiGateway
from findb_fetcher.providers.taifex import (
    TaifexOperationalError,
    fetch_report,
    is_no_trade,
    official_report_date,
    parse_report,
    persist_dated_report,
    report_members,
)
from findb_fetcher.providers.twelve_data import (
    TwelveDataClient,
    TwelveDataConfig,
    TwelveDataResponseError,
)
from findb_fetcher.providers.twelve_data import (
    build_market_eod_request as td_request,
)
from findb_fetcher.raw_storage import (
    RawObject,
    RawPayloadFormat,
    RawPayloadStore,
    attach_raw_object,
)

_TD_CATALOGUE_COUNTRIES = {"us_equity_eod": "United States", "hk_equity_eod": "Hong Kong"}


class ReadinessBlockedError(RuntimeError):
    pass


class DailyWindowClosedError(ProviderWorkAcquisitionStopped):
    """A proven daily-window wait, including authoritative Source denial."""


class ProviderHTTPError(ReadinessBlockedError):
    """Preserve the public universe error while retaining a dispatched HTTP status."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.dispatched = True


class DatedUniversePendingError(ReadinessBlockedError):
    """A date-local prerequisite; other frozen dates may still make progress."""

    def __init__(self, reason: str, *, retry_after_seconds: int = 3600) -> None:
        super().__init__(reason)
        self.retry_after_seconds = max(60, retry_after_seconds)


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
        if value["max_response_bytes"] > (
            2 * 1024**3 if provider == "finlab" else MAX_SNAPSHOT_BYTES
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ReadinessBlockedError(
            "existing account readiness evidence is invalid or expired"
        ) from None
    return value


def validate_capacity(
    proof: dict[str, Any], request_count: int, *, seconds_available: float
) -> None:
    # Reserve retry headroom; insufficient coverage blocks instead of selecting a pilot subset.
    needed = math.ceil(request_count * 1.2)
    rate = min(proof["requests_per_second"], proof["requests_per_minute"] / 60)
    if (
        needed > proof["requests_per_day"]
        or needed * proof["max_response_bytes"] > proof["bytes_per_day"]
        or needed / rate > seconds_available
    ):
        raise ReadinessBlockedError("current account cannot finish full coverage before deadline")


def split_rows(
    rows: list[dict[str, Any]], *, max_rows: int = 15000, max_bytes: int = 900000
) -> list[list[dict[str, Any]]]:
    """Bound both contract row count and serialized request headroom."""
    if max_rows < 1 or max_bytes < 1:
        raise ValueError("batch bounds invalid")
    result: list[list[dict[str, Any]]] = []
    chunk: list[dict[str, Any]] = []
    size = 2
    for row in rows:
        row_size = len(canonical_bytes(row)) + 1
        if row_size + 2 > max_bytes:
            raise ReadinessBlockedError("one provider row exceeds request byte bound")
        if chunk and (len(chunk) >= max_rows or size + row_size > max_bytes):
            result.append(chunk)
            chunk, size = [], 2
        chunk.append(row)
        size += row_size
    if chunk:
        result.append(chunk)
    return result


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
                        if method.__name__ == "_dated_taifex_universe"
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
        if execution_profile not in {"pilot", "full_market"}:
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
        self.feeds = [feed for feed in config["feeds"] if feed["provider"] == provider]
        self.installed_feeds = list(self.feeds)
        self.http = client or httpx.Client(timeout=30)
        self.owns_http = client is None
        self.sdk: PersistentIsolatedShioajiGateway | None = None
        self.td: TwelveDataClient | None = None
        self.proof: dict[str, Any] = {}
        self.reservation_window = ""
        self.snapshot_objects: dict[str, RawObject] = {}
        self.catalogue_evidence: dict[str, bytes] = {}
        self.futures: dict[str, tuple[bytes, list[dict[str, Any]], bool]] = {}
        self.bundles: dict[tuple[str, date], Any] = {}
        self.daily_policy = config.get("daily_runtime_policies", {}).get(provider)
        self.last_batch: dict[str, Any] = {}

    def close(self) -> None:
        if self.sdk is not None:
            self.sdk.close()
        if self.td is not None:
            self.td.close()
        if self.owns_http:
            self.http.close()

    def pause(self) -> None:
        if self.sdk is not None:
            self.sdk.close()
            self.sdk = None
        self.bundles.clear()
        self.futures.clear()

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

    def _shioaji_request(self, operation: str, *args: Any) -> Any:
        from findb_fetcher.provider_work import active

        owner = active()
        maintenance = (owner and owner.work["kind"] == "maintenance") or os.getenv(
            "FETCHER_CONSUMER_PROFILE"
        ) == "maintenance"
        if not maintenance and not self.runtime_window()["open"]:
            raise DailyWindowClosedError("daily acquisition window closed")
        if self.sdk is not None and (not getattr(self.sdk, "is_alive", lambda: True)()):
            self.sdk.close()
            self.sdk = None
        if self.sdk is None:
            self.sdk = PersistentIsolatedShioajiGateway(
                os.getenv("SHIOAJI_API_KEY", ""),
                os.getenv("SHIOAJI_SECRET_KEY", ""),
                simulation=_simulation_from_env(),
            )
        # The gateway owns transport/child failure cleanup. Local observation
        # waits and authorization errors must not discard a healthy login.
        try:
            return getattr(self.sdk, operation)(*args)
        except Exception:
            if not getattr(self.sdk, "is_alive", lambda: True)():
                failed, self.sdk = self.sdk, None
                failed.close()
            raise

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

    def _reserve(self, *, requests: int = 1, byte_count: int | None = None) -> tuple[int, int]:
        from findb_fetcher.account_governor import enabled as governor_enabled
        from findb_fetcher.account_governor import reserve
        from findb_fetcher.platform_limits import interval

        if governor_enabled():
            permit = reserve(self.provider)
            if not self._acquisition_allowed() or permit is None:
                raise ReadinessBlockedError("scheduler acquisition stopped")
            self.reservation_window = permit.window
            self.account_permit = permit
            return permit.requests_before, permit.requests_after
        while True:
            if not self._acquisition_allowed():
                raise ReadinessBlockedError("scheduler was stopped by Source control")
            wait, before, after, window = self.state.reserve_acquisition(
                account=self.provider,
                interval=interval(self.proof),
                requests=requests,
                byte_count=(
                    0 if self.proof.get("platform_limits") else self.proof["max_response_bytes"]
                )
                if byte_count is None
                else byte_count,
                request_limit=self.proof["requests_per_day"],
                byte_limit=self.proof["bytes_per_day"],
                minute_limit=self.proof["requests_per_minute"],
                enforce_capacity=False,
            )
            if wait <= 0:
                self.reservation_window = window
                if not self._acquisition_allowed():
                    raise ReadinessBlockedError("scheduler was stopped by Source control")
                return before, after
            time.sleep(min(wait, 1))

    def _observed_response(
        self, size: int, *, usage_bytes: int | None = None, declared: int = 0
    ) -> None:
        bound = int(self.proof["max_response_bytes"])
        # SDK account usage is authoritative; detached JSON is only a transport bound.
        counted = usage_bytes if self.provider == "shioaji" else size
        permit = getattr(self, "account_permit", None)
        if permit is not None:
            if counted is not None:
                permit.observe(counted, declared=declared)
        elif self.proof.get("bytes_per_day") is not None and counted is not None:
            reserved = 0 if self.proof.get("platform_limits") else bound
            if counted > reserved and max(size, declared) <= bound:
                self.state.record_byte_overage(
                    account=self.provider,
                    window=self.reservation_window,
                    byte_count=counted - reserved,
                )
        if max(size, declared) > bound:
            if permit is None:
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
        permit = getattr(self, "account_permit", None)
        state = permit.state if permit is not None else self.state
        state.rate_limited(
            account=permit.account if permit is not None else self.provider,
            window=datetime.now(timezone.utc).date().isoformat(),
            until=time.time() + 60,
        )

    def _finlab_table_bytes(self, table: FinLabDatasetTable) -> bytes:
        return canonical_bytes(
            {
                "dates": table.dates,
                "symbols": table.symbols,
                "values": [[str(value) for value in row] for row in table.values],
            }
        )

    def _read_response(
        self, response: httpx.Response, *, bound: int, provider_response: bool
    ) -> bytes:
        try:
            return read_identity_response(response, bound=bound)
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
                body = self._read_response(response, bound=bound, provider_response=True)
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
            if url in NASDAQ_URLS:
                dataset = "us_equity_eod"
            elif url == HKEX_URL:
                dataset = "hk_equity_eod"
            elif url == TAIFEX_URL:
                dataset = "tw_futures_eod"
            else:
                suffix = "minute" if self.provider == "shioaji" else "eod"
                asset = "etf" if url in TW_ISIN_URLS else "equity"
                dataset = f"tw_{asset}_{suffix}"
            # An empty TAIFEX HTTP 200 report has no raw object; the dated
            # report resolver handles fallback/retry without fabricating evidence.
            if url == TAIFEX_URL and not body:
                return body
            self.snapshot_objects[url] = self._persist(body, dataset, "official_universe")
            return body

    def _ensure_proof(self) -> None:
        from findb_fetcher.platform_limits import limits

        self.proof = limits(self.provider, [feed["dataset_key"] for feed in self.installed_feeds])

    def _finlab_gateway(self) -> FinLabSdkGateway:
        try:
            if importlib.metadata.version("finlab") != "1.5.7":
                raise ReadinessBlockedError("FinLab SDK version is unsupported")
        except importlib.metadata.PackageNotFoundError:
            raise ReadinessBlockedError("FinLab pinned SDK is unavailable") from None
        return FinLabSdkGateway.from_env(on_response_bytes=self._observed_response)

    @_governed
    def _catalogue(self, dataset_keys: list[str] | None = None) -> list[dict[str, Any]]:
        self._ensure_proof()
        if self.provider == "shioaji":
            catalogue = self._shioaji_request("catalogue")
            detached = canonical_bytes(
                {"sdk_version": SHIOAJI_SDK_VERSION, "simulation": True, "catalogue": catalogue}
            )
            self._observed_response(len(detached))
            self._persist(detached, "tw_equity_minute", "sdk_catalogue")
            return catalogue
        if self.provider == "finlab":
            return []
        installed = {feed["dataset_key"] for feed in self.installed_feeds}
        selected = installed if dataset_keys is None else set(dataset_keys)
        if (
            not selected
            or not selected <= installed
            or not selected <= _TD_CATALOGUE_COUNTRIES.keys()
        ):
            raise ReadinessBlockedError("provider catalogue dataset scope invalid")
        combined: list[dict[str, Any]] = []
        evidence: dict[str, bytes] = {}
        for dataset in _TD_CATALOGUE_COUNTRIES:
            if dataset not in selected:
                continue
            rows, url, raw = self._catalogue_market(dataset)
            combined.extend(rows)
            evidence[url] = raw
        self.catalogue_evidence = evidence
        return combined

    def _catalogue_evidence_for(self, dataset_key: str) -> dict[str, bytes]:
        country = _TD_CATALOGUE_COUNTRIES[dataset_key]
        return {
            url: raw
            for url, raw in self.catalogue_evidence.items()
            if dict(parse_qsl(urlsplit(url).query)).get("country") == country
        }

    @_governed
    @durable_response("twelve_catalogue", ("dataset_key",))
    def _catalogue_market(self, dataset_key: str) -> tuple[list[dict[str, Any]], str, bytes]:
        config = TwelveDataConfig.from_env()
        country = _TD_CATALOGUE_COUNTRIES[dataset_key]
        evidence_url = config.base_url + "/stocks?" + urlencode({"country": country})
        # Each country request uses a fresh dataset-bound authorization and account permit.
        with (
            http_acquisition(self.provider, self.http, url=config.base_url + "/stocks") as (
                operation,
                extensions,
            ),
            self.http.stream(
                "GET",
                config.base_url + "/stocks",
                params={"apikey": config.api_key, "country": country},
                headers={"Accept-Encoding": "identity"},
                extensions=extensions,
            ) as response,
        ):
            if response.status_code == 429:
                operation.rate_limited(response.headers.get("Retry-After"))
                # Commit known account cooldown before body validation/byte accounting.
                self._rate_limited()
            try:
                raw = self._read_response(
                    response,
                    bound=min(config.max_response_bytes, int(self.proof["max_response_bytes"])),
                    provider_response=True,
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
            value = json.loads(raw)
            if not isinstance(value, dict) or not isinstance(value.get("data"), list):
                raise ReadinessBlockedError("provider catalogue shape invalid")
            catalogue_raw = bytes(raw)
            self._persist(catalogue_raw, dataset_key, "provider_catalogue")
            return value["data"], evidence_url, catalogue_raw

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
        now = datetime.now(timezone.utc)
        snapshots: dict[str, bytes] = {}
        if self.provider == "twelve_data":
            groups = {}
            catalogue = self._catalogue(dataset_keys=[feed["dataset_key"] for feed in feeds])
            if "us_equity_eod" in dataset_keys:
                snapshots.update({url: self._snapshot(url) for url in NASDAQ_URLS})
                us = parse_nasdaq(snapshots[NASDAQ_URLS[0]]) + parse_nasdaq(
                    snapshots[NASDAQ_URLS[1]], other=True
                )
                us_dates = {
                    nasdaq_effective_date(
                        snapshots[url],
                        observed_date=now.astimezone(ZoneInfo("America/New_York")).date(),
                    )
                    for url in NASDAQ_URLS
                }
                if len(us_dates) != 1:
                    raise ReadinessBlockedError(
                        "Nasdaq official directories have different effective dates"
                    )
                groups["us_equity_eod"] = (
                    next(iter(us_dates)),
                    map_catalogue(us, catalogue),
                    {
                        **{url: snapshots[url] for url in NASDAQ_URLS},
                        **self._catalogue_evidence_for("us_equity_eod"),
                    },
                )
            if "hk_equity_eod" in dataset_keys:
                snapshots[HKEX_URL] = self._snapshot(HKEX_URL)
                hk_date, hk = parse_hkex(
                    snapshots[HKEX_URL],
                    observed_date=now.astimezone(ZoneInfo("Asia/Hong_Kong")).date(),
                )
                groups["hk_equity_eod"] = (
                    hk_date,
                    map_catalogue(hk, catalogue),
                    {
                        HKEX_URL: snapshots[HKEX_URL],
                        **self._catalogue_evidence_for("hk_equity_eod"),
                    },
                )
        elif self.provider == "factset":
            snapshots[HKEX_URL] = self._snapshot(HKEX_URL)
            hk_date, hk = parse_hkex(
                snapshots[HKEX_URL], observed_date=now.astimezone(ZoneInfo("Asia/Hong_Kong")).date()
            )
            native = FactSetGateway(FactSetConfig.from_env()).catalogue(
                tuple(sorted({member["symbol"] for member in hk}))
            )
            self._observed_response(len(native))
            stored = self._persist(native, "hk_equity_eod", "provider_catalogue")
            members = map_current_cohort(hk, native)
            for member in members:
                member["raw_evidence_ref"] = stored.ref
            groups = {"hk_equity_eod": (hk_date, members, {HKEX_URL: snapshots[HKEX_URL]})}
        elif self.provider in {"finlab", "shioaji"}:
            snapshots = {url: self._snapshot(url) for url in (*TW_COMPANY_URLS, *TW_ISIN_URLS)}
            companies = {
                (member["symbol"], member["exchange"])
                for index, url in enumerate(TW_COMPANY_URLS)
                for member in parse_tw_companies(snapshots[url], otc=bool(index))
            }
            equity = [
                member
                for index, url in enumerate(TW_ISIN_URLS)
                for member in parse_tw_ordinary(snapshots[url], otc=bool(index))
            ]
            for member in equity:
                if (member["symbol"], member["exchange"]) not in companies:
                    member["classification"] = "classification_gap"
            etfs = [
                member
                for index, url in enumerate(TW_ISIN_URLS)
                for member in parse_tw_etfs(snapshots[url], otc=bool(index))
            ]
            if self.provider == "shioaji":
                catalogue = self._catalogue()
                equity, etfs = (
                    map_catalogue(equity, catalogue, shioaji=True),
                    map_catalogue(etfs, catalogue, shioaji=True),
                )
            else:
                # Catalogue presence is independent of daily OHLCV completeness.
                catalogue_snapshots: dict[str, dict[str, bytes]] = {}
                self._ensure_proof()
                gateway = self._finlab_gateway()
                for dataset, members in (("tw_equity_eod", equity), ("tw_etf_eod", etfs)):
                    if dataset not in dataset_keys:
                        continue
                    try:
                        try:
                            table = gateway.fetch_catalogue(
                                symbols=tuple(member["provider_symbol"] for member in members),
                            )
                        finally:
                            self.state.pace_from_completion(
                                account=self.provider,
                                now=datetime.now(timezone.utc).timestamp(),
                                interval=(
                                    1 / self.proof["requests_per_second"]
                                    if self.proof["requests_per_second"]
                                    else 0
                                ),
                            )
                    except FinLabOperationalError:
                        raise
                    except FinLabSdkError:
                        for member in members:
                            member["mapping_status"] = "gap"
                        continue
                    detached = self._finlab_table_bytes(table)
                    self._observed_response(len(detached))
                    url = f"https://www.finlab.tw/data/price-close?dataset={dataset}"
                    self.snapshot_objects[url] = self._persist(
                        detached, dataset, "provider_catalogue"
                    )
                    catalogue_snapshots[dataset] = {url: detached}
                    for member in members:
                        member["mapping_status"] = (
                            "mapped"
                            if member["classification"] != "classification_gap"
                            and member["provider_symbol"] in table.symbols
                            else "gap"
                        )
            suffix = "minute" if self.provider == "shioaji" else "eod"
            groups = {
                f"tw_equity_{suffix}": (
                    now.astimezone(ZoneInfo("Asia/Taipei")).date(),
                    equity,
                    {url: snapshots[url] for url in (*TW_COMPANY_URLS, *TW_ISIN_URLS)},
                ),
                f"tw_etf_{suffix}": (
                    now.astimezone(ZoneInfo("Asia/Taipei")).date(),
                    etfs,
                    {url: snapshots[url] for url in TW_ISIN_URLS},
                ),
            }
            if self.provider == "finlab":
                for dataset, evidence in catalogue_snapshots.items():
                    groups[dataset][2].update(evidence)
        else:
            self._ensure_proof()
            raw = self._snapshot(TAIFEX_URL, provider_response=True)
            day = official_report_date(raw)
            if day > now.astimezone(ZoneInfo("Asia/Taipei")).date():
                raise ReadinessBlockedError("TAIFEX report is future-dated")
            rows = parse_report(raw, target_date=day)
            groups = {"tw_futures_eod": (day, report_members(rows), {TAIFEX_URL: raw})}
        result = []
        for feed in feeds:
            dataset = feed["dataset_key"]
            effective, members, evidence = groups[dataset]
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
                source_timezone=feed["source_timezone"],
                snapshots=evidence,
                members=members,
            )
            result.append(self.source.submit(body))
        return result

    def drain(self) -> None:
        """Drain retained deliveries only through the original fenced owner."""
        from findb_fetcher.config import require_provider_work_protocol

        require_provider_work_protocol()
        self._queued_cycle(include_health=False)

    def cycle(self) -> dict[str, Any]:
        """All providers use the same durable queue, including retained work."""
        from findb_fetcher.config import require_provider_work_protocol

        require_provider_work_protocol()
        return self._queued_cycle()

    def sync_queued_universes(self) -> list[dict[str, Any]]:
        from findb_fetcher.provider_work import ProviderWorkWaitingError, claimed_work

        self._ensure_proof()
        for feed in self.installed_feeds:
            self.source.request(
                "POST",
                "provider-work/enqueue",
                {
                    "stable_key": f"maintenance:{feed['dataset_key']}:{datetime.now(timezone.utc).date()}",
                    "kind": "maintenance",
                    "dataset_key": feed["dataset_key"],
                    "scope": {"dataset_key": feed["dataset_key"]},
                },
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
        from findb_fetcher.providers.shioaji import ShioajiSdkError
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
        except (TwelveDataResponseError, ProviderHTTPError, httpx.HTTPStatusError) as exc:
            status = (
                exc.status_code
                if isinstance(exc, (TwelveDataResponseError, ProviderHTTPError))
                else exc.response.status_code
            )
            retryable = status is None or status in {408, 429} or status >= 500
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
        except FactSetOperationalError as exc:
            retryable, code = exc.retryable, exc.code
        except ProviderProcessError:
            retryable, code = True, "MAINTENANCE_PROVIDER_PROCESS"
        except FinLabOperationalError:
            retryable, code = True, "MAINTENANCE_FINLAB_OPERATIONAL"
        except FinLabConfigError:
            code = "MAINTENANCE_FINLAB_CONFIG"
        except ShioajiSdkError as exc:
            retryable = exc.code not in {"CREDENTIALS", "LOGIN", "SDK", "CONTRACT", "PAYLOAD"}
            code = "MAINTENANCE_SDK_" + exc.code
        except (ReadinessBlockedError, ValueError):
            pass
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
            now = datetime.now(timezone.utc).timestamp()
            self.state.refresh(self.provider, now=now, result=results, next_at=now + 86400)
            return results
        self._settle_queued_round(
            owner, outcome="retry" if retryable else "failed", failure_code=code
        )
        return [{"status": "queued" if retryable else "manual", "reason": code}]

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
        # Missing/manual frozen members remain missing/manual, never no_data.
        owner.settle(
            "failed",
            failure_code="RETIRED_FEED_MANUAL_REVIEW",
            proof={"manual_reason": "retired_feed_requires_manual_review"},
        )

    def _queued_cycle(self, *, include_health: bool = True) -> dict[str, Any]:
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
        due: list[tuple[date, dict[str, Any]]] = []
        for feed in self.installed_feeds:
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
        for saved in self.state.pending_plans(self.provider):
            plan = json.loads(saved["body"])
            feed = next(
                (
                    feed
                    for feed in self.installed_feeds
                    if feed["dataset_key"] == plan["dataset_key"]
                ),
                None,
            )
            if feed:
                due.append((date.fromisoformat(plan["trade_date"]), feed))
        for day, feed in sorted(due, key=lambda value: (value[0], value[1]["dataset_key"])):
            self.source.request(
                "POST",
                "provider-work/enqueue",
                {
                    "stable_key": f"daily:{self.execution_profile}:{feed['dataset_key']}:{day}",
                    "kind": "daily",
                    "dataset_key": feed["dataset_key"],
                    "trade_date": day.isoformat(),
                    "execution_profile": self.execution_profile,
                    "scope": {"feed": feed},
                },
            )
        if (
            admitted
            and self.provider != "taifex"
            and self.state.refresh_due(self.provider, now.timestamp())
        ):
            for feed in self.installed_feeds:
                if feed["dataset_key"] in admitted:
                    self.source.request(
                        "POST",
                        "provider-work/enqueue",
                        {
                            "stable_key": f"maintenance:{feed['dataset_key']}:{now.date()}",
                            "kind": "maintenance",
                            "dataset_key": feed["dataset_key"],
                            "scope": {"dataset_key": feed["dataset_key"]},
                        },
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
                            if self.provider == "factset":
                                self._deliver_factset_plan(owner, plan, release, feed)
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
                                if self.provider == "shioaji":
                                    release = self._install_shioaji_repairs(owner, plan, release)
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
                except (ProviderHTTPError, TwelveDataResponseError, TaifexOperationalError) as exc:
                    status = exc.status_code
                    retryable = (
                        (exc.retryable or status == 408)
                        if isinstance(exc, TaifexOperationalError)
                        else (status is None or status in {408, 429} or status >= 500)
                    )
                    self._settle_queued_round(
                        owner,
                        outcome="retry" if retryable else "failed",
                        failure_code="DAILY_PROVIDER_HTTP_" + str(status or "UNKNOWN"),
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
                except (FactSetOperationalError, RawStorageUploadError) as exc:
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
                finally:
                    # Persistent SDK sessions must be stopped and reaped before
                    # ownership can leave this process. HTTP calls are bounded.
                    self.pause()
        except ProviderWorkWaitingError:
            pass
        return health()

    def _deliver_factset_plan(self, owner, plan: dict, release: dict, feed: dict) -> None:
        from findb_fetcher.factset_eod import FactSetEOD, completion_status

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
                "contract": {"schema_id": "market_eod", "schema_version": 2},
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
            owner.journal.has_response_observation("factset-eod:snapshot")
            and owner.journal.response("factset-eod:snapshot") is None
            and self._calendar_repair_required(owner)
        ):
            for key, delivery in deliveries.items():
                self.state.finish(
                    delivery["work_item_id"] + ":" + key,
                    status="manual",
                    reason="calendar_repair_required",
                )
            raise CalendarDecisionRepairRequiredError("published calendar is now closed") from None
        try:
            with authorization_scope(
                lambda: self.source.authorization(
                    "GET",
                    "full-market/authorization?consumer=" + self.execution_profile,
                    required_dataset=plan["dataset_key"],
                ),
                state_path=self.state.path,
            ):
                evidence = FactSetEOD(
                    FactSetGateway(FactSetConfig.from_env()),
                    self.delivery,
                    self.raw_store,
                    on_receipt=checkpoint_receipt,
                ).run(
                    day=date.fromisoformat(plan["trade_date"]),
                    members=members,
                    dataset=plan["dataset_key"],
                    deliveries=deliveries,
                    historical=False,
                    defer_missing=retry_delay > 0,
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
                "factset_no_source_row", retry_after_seconds=retry_delay
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
        owner.settle(completion_status(evidence), proof={"eod": evidence})

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
            if desired is None:
                plan = self.source.evaluate(owner.journal.manifest()["plan_id"])
                self.state.evaluate(plan)
                with self.state.connection() as db:
                    manual = db.execute(
                        "SELECT 1 FROM full_work WHERE plan_id=? AND status='manual' LIMIT 1",
                        (plan["plan_id"],),
                    ).fetchone()
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
                if self.provider == "taifex" and exc.code == "taifex_dated_universe_required":
                    dataset = feed["dataset_key"]
                    if not self.state.date_prerequisite_due(self.provider, dataset, day):
                        raise DatedUniversePendingError(
                            "dated universe retry is pending",
                            retry_after_seconds=self.state.date_prerequisite_delay(
                                self.provider, dataset, day
                            ),
                        ) from None
                    try:
                        release = self._dated_taifex_universe(feed, day)
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

    def _taifex_report(self, day: date, dataset: str) -> tuple[bytes, list[dict[str, Any]], bool]:
        if day.isoformat() not in self.futures:
            latest_raw = self._snapshot(TAIFEX_URL, provider_response=True)
            if latest_raw:
                self._persist(latest_raw, dataset, "exchange_report")
            try:
                rows = parse_report(latest_raw, target_date=day)
                raw, historical = latest_raw, False
            except (ValueError, UnicodeError):
                reports = {}
                rows = []
                for product in ("TX", "MTX", "TMF", "TE", "TF"):
                    product_raw = fetch_report(
                        self.http,
                        target_date=day,
                        latest=False,
                        product=product,
                        max_response_bytes=int(self.proof["max_response_bytes"]),
                        on_observed_bytes=self._observed_response,
                        on_bounded_response=lambda size, declared: self._observed_response(
                            size, declared=declared
                        ),
                        on_http_rate_limited=self._rate_limited,
                    )
                    if not product_raw:
                        raise DatedUniversePendingError("dated_report_unavailable")
                    try:
                        _, parsed = persist_dated_report(
                            product_raw,
                            target_date=day,
                            product=product,
                            persist=lambda raw: self._persist(
                                raw,
                                dataset,
                                product,
                                payload_format="csv_cp950",
                            ),
                        )
                    except (ValueError, UnicodeError):
                        raise DatedUniversePendingError("dated_report_unavailable") from None
                    if any(row["product_code"] != product for row in parsed):
                        raise DatedUniversePendingError("dated_report_unavailable")
                    reports[product] = product_raw.decode("cp950")
                    rows.extend(parsed)
                raw, historical = canonical_bytes(reports), True
            self.futures[day.isoformat()] = (raw, rows, historical)
        return self.futures[day.isoformat()]

    @_governed
    def _dated_taifex_universe(self, feed: dict[str, Any], day: date) -> dict[str, Any]:
        dataset = feed["dataset_key"]
        raw, rows, historical = self._taifex_report(day, dataset)
        stored = self._persist(raw, dataset, "dated_universe")
        members = report_members(rows)
        for member in members:
            member["raw_evidence_ref"] = stored.ref
        url = (TAIFEX_HISTORY_URL if historical else TAIFEX_URL) + "?trade_date=" + day.isoformat()
        result = self.source.submit(
            universe_request(
                dataset_key=dataset,
                provider=self.provider,
                effective_date=day,
                observed_at=datetime.now(timezone.utc),
                source_timezone=feed["source_timezone"],
                snapshots={url: raw},
                members=members,
            )
        )
        if (
            result.get("provider") != self.provider
            or result.get("dataset_key") != dataset
            or result.get("effective_date") != day.isoformat()
        ):
            raise ReadinessBlockedError("dated universe response identity invalid")
        if result.get("status") != "published":
            raise DatedUniversePendingError("dated_universe_pending")
        return result

    def _release_for_date(self, feed: dict[str, Any], day: date) -> dict[str, Any]:
        listed = self.source.universes(feed["dataset_key"], self.provider, as_of=day.isoformat())
        if not listed.get("published_release_id"):
            raise ReadinessBlockedError("no published universe applies to catchup date")
        return self.source.release(listed["published_release_id"])

    def _install_shioaji_repairs(self, owner, plan: dict, release: dict) -> dict:
        from findb_fetcher.provider_work import ProviderWorkRecoveryBlockedError
        from findb_fetcher.shioaji_recovery import install_grant

        response = owner._preparation_request("shioaji-repairs")
        if (
            response.get("work_id") != owner.work["work_id"]
            or response.get("plan_id") != plan["plan_id"]
        ):
            raise ProviderWorkRecoveryBlockedError("repair scope differs from frozen work")
        repairs = response["repairs"]
        if not repairs:
            return release
        pending_reports = self.state.pending_source_error_keys(plan["plan_id"])
        for part in plan["parts"]:
            for member_key in part["member_keys"]:
                key = part["work_item_id"] + ":" + member_key
                if key in pending_reports:
                    self._report_source_error(plan, part, member_key, key)
        inventory = self.source.request(
            "GET", f"provider-work/{owner.work['work_id']}/shioaji-diagnostics"
        )
        if any(
            inventory.get(field) != expected
            for field, expected in {
                "work_id": owner.work["work_id"],
                "plan_id": plan["plan_id"],
                "execution_profile": self.execution_profile,
                "dataset_key": plan["dataset_key"],
                "trade_date": plan["trade_date"],
            }.items()
        ):
            raise ProviderWorkRecoveryBlockedError("repair inventory differs from frozen work")
        resolved = {
            row["member_key"] for row in inventory["members"] if row["status"] == "resolved"
        }
        keys = {
            member: part["work_item_id"] + ":" + member
            for part in plan["parts"]
            for member in part["member_keys"]
        }
        members = {row["member_key"]: dict(row) for row in release["members"]}
        installed = []
        for repair in repairs:
            member_key = repair["member_key"]
            if member_key not in keys or member_key not in members:
                raise ProviderWorkRecoveryBlockedError("repair member differs from frozen work")
            if repair["mapping_repaired"]:
                members[member_key].update(
                    mapping_status="mapped", provider_symbol=repair["provider_symbol"]
                )
            if member_key in resolved:
                if not self.state.member_complete(keys[member_key]):
                    self.state.finish(keys[member_key], status="complete")
                continue
            # Original prepared descriptors always win over any repair grant.
            if self.state.prepared(keys[member_key]) is not None or owner.restore(
                member_key=member_key
            ):
                continue
            if self.state.apply_member_repair(keys[member_key], repair["repair_id"]):
                install_grant(owner.journal, repair, date.fromisoformat(plan["trade_date"]))
                installed.append(repair["repair_id"])
        if installed:
            owner._preparation_request("shioaji-repair-ack", repair_ids=installed)
        return {**release, "members": list(members.values())}

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
            if self.state.prepared(key) is None:
                restored = owner.restore(member_key=member_key)
                if restored:
                    self.state.prepare(
                        key, canonical_bytes([json.loads(body) for body in restored])
                    )
            if self.state.prepared(key) is None and (drain_only or not self._acquisition_allowed()):
                continue
            if self.daily_policy and (
                self.last_batch["processed"] >= self.daily_policy["max_members"]
                or time.monotonic() - self._batch_started >= self.daily_policy["max_seconds"]
            ):
                self.last_batch["reason"] = "batch_yield"
                return True
            if not self.state.claim(
                key,
                now=time.time(),
                next_cursor=(index + 1) % len(entries) if self.daily_policy else None,
            ):
                continue
            if self.daily_policy:
                self.last_batch["processed"] += 1
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
                    if (
                        self.provider == "finlab"
                        and getattr(owner, "journal", None) is not None
                        and owner.journal.has_response_observation("finlab_dataset:", prefix=True)
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
                    requests = self._acquire(member, plan, part, release, feed)
                    if not requests:
                        self.state.finish(key, status="complete", reason="source_proven_no_trade")
                        continue
                    for request in requests:
                        owner.reserve(request)
                    prepared = canonical_bytes(requests)
                    self.state.prepare(key, prepared)
                normalizing = False
                prepared_requests = json.loads(prepared)
                for request in prepared_requests:
                    immutable = self.delivery.prepare(request)
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
                    if saved_run is None:
                        receipt = self.delivery.deliver(immutable)
                        saved_run = str(receipt.run_id)
                        self.state.save_sequence_receipt(key, request_identity, saved_run)
                        self.state.finish(key, status="prepared", receipt=saved_run)
                    run = self.delivery.get_run_status(UUID(saved_run), expected=immutable)
                    if run.status in {"queued", "pending", "processing", "retrying"}:
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
                if self.provider == "shioaji":
                    self.state.defer_wait(key, now=time.time(), reason="calendar_repair_required")
                    self.state.finish(key, status="manual", reason="calendar_repair_required")
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
            except ShioajiEmptyResponseError as exc:
                from findb_fetcher.shioaji_recovery import (
                    RETRY_SECONDS,
                    ShioajiObservationWaitError,
                    observation_status,
                )

                count, maximum = observation_status(
                    owner.journal, member["provider_symbol"], date.fromisoformat(plan["trade_date"])
                )
                exhausted = (
                    exc.exhausted
                    if isinstance(exc, ShioajiObservationWaitError)
                    else count >= maximum
                )
                delay = (
                    exc.delay
                    if isinstance(exc, ShioajiObservationWaitError) and exc.delay
                    else RETRY_SECONDS
                )
                reason = (
                    "unknown_dispatch"
                    if isinstance(exc, ShioajiObservationWaitError) and not exc.observed_empty
                    else "empty_response"
                )
                # Persist classification/refund before any Source RPC so a
                # failed calendar probe cannot lose the report continuation.
                self.state.stage_empty_response(
                    key, now=time.time(), delay=delay, exhausted=exhausted, reason=reason
                )
                if self._calendar_repair_required(owner):
                    self._report_source_error(plan, part, member_key, key)
                    self.state.finish(key, status="manual", reason="calendar_repair_required")
                    raise CalendarDecisionRepairRequiredError(
                        "published calendar is now closed"
                    ) from None
                self._report_source_error(plan, part, member_key, key)
                if self.daily_policy:
                    self.last_batch["reason"] = reason
                return False
            except FinLabTargetNotReadyError:
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
                        "finlab_target_not_ready",
                        retry_after_seconds=retry_delay,
                    ) from None
                self.state.finish(
                    key,
                    status="manual",
                    reason="target_not_ready",
                )
                return False
            except (QuotaBlockedError, TwelveDataResponseError) as exc:
                limited = (
                    exc.quota_wait and not exc.dispatched
                    if isinstance(exc, QuotaBlockedError)
                    else exc.is_rate_limited
                )
                reason = "rate_limited" if limited else "source_error"
                if limited:
                    self.state.rate_limited(
                        account=self.provider,
                        window=datetime.now(timezone.utc).date().isoformat(),
                        until=time.time() + 60,
                    )

                predispatch_wait = (
                    isinstance(exc, QuotaBlockedError)
                    and limited
                    and (self.daily_policy or owner.dispatch_observations == dispatch_before)
                )
                if predispatch_wait:
                    self.state.defer_wait(key, now=time.time())
                    if self.daily_policy:
                        self.last_batch["reason"] = "quota_wait"
                    return False
                self.source.outcome(
                    plan["plan_id"],
                    work_item_id=part["work_item_id"],
                    member_key=member_key,
                    reason=reason,
                )
                self.state.finish(
                    key,
                    status="prepared" if self.state.prepared(key) else "pending",
                    reason=reason,
                    now=time.time(),
                )
                return False
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
                if self.daily_policy and isinstance(exc, ShioajiSdkError) and exc.quota_wait:
                    self.state.defer_wait(key, now=time.time())
                    self.last_batch["reason"] = "quota_wait"
                    return False
                # Provider/SDK diagnostics never enter durable state or logs.
                self.state.stage_source_error(key)
                self._report_source_error(plan, part, member_key, key)
                return False

        return True

    def _report_source_error(
        self, plan: dict[str, Any], part: dict[str, Any], member_key: str, key: str
    ) -> None:
        observation = self.state.observation_outcome_reason(key)
        self.source.outcome(
            plan["plan_id"],
            work_item_id=part["work_item_id"],
            member_key=member_key,
            reason="empty_response" if observation == "empty_response" else "source_error",
        )
        if observation is not None:
            self.state.acknowledge_empty(key)
            return
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

    @_governed
    def _acquire(
        self,
        member: dict[str, Any],
        plan: dict[str, Any],
        part: dict[str, Any],
        release: dict[str, Any],
        feed: dict[str, Any],
    ) -> list[dict[str, Any]]:
        day = date.fromisoformat(plan["trade_date"])
        now = datetime.now(timezone.utc)
        delivery = {
            "slot_id": feed["slot_id"],
            "scheduled_for": now.isoformat(),
            "target_data_date": day.isoformat(),
            "work_item_id": part["work_item_id"],
        }
        dataset = plan["dataset_key"]
        if self.provider == "twelve_data":
            if self.td is None:
                td_config = TwelveDataConfig.from_env()
                self.td = TwelveDataClient(
                    replace(
                        td_config,
                        max_response_bytes=min(
                            td_config.max_response_bytes, int(self.proof["max_response_bytes"])
                        ),
                    )
                )
            try:
                response = self.td.fetch_daily(
                    member["provider_symbol"],
                    start_date=day,
                    end_date=day,
                    max_response_bytes=int(self.proof["max_response_bytes"]),
                    on_http_rate_limited=self._rate_limited,
                )
            except TwelveDataResponseError as exc:
                if exc.is_rate_limited:
                    self._rate_limited()
                if exc.observed_bytes or exc.declared_bytes:
                    try:
                        self._observed_response(exc.observed_bytes, declared=exc.declared_bytes)
                    except ReadinessBlockedError:
                        # Overage debt is retained without erasing known HTTP/API status.
                        raise exc
                raise
            raw = response.raw_bytes
            self._observed_response(len(raw))
            stored = self._persist(raw, dataset, member["provider_symbol"])
            request = td_request(
                response,
                dataset_key=dataset,
                fetched_at=now,
                requested_symbol=member["provider_symbol"],
                canonical_symbol=member["symbol"],
                allowed_instrument_types=(
                    "Common Stock",
                    "American Depositary Receipt",
                    "Depositary Receipt",
                ),
                after_trade_date=day - timedelta(days=1),
                through_trade_date=day,
                delivery=delivery,
            )
            if (
                canonical_twelve_data_mic(response["meta"]["mic_code"]) != member["exchange"]
                or response["meta"]["currency"] != member["currency"]
            ):
                raise ReadinessBlockedError("provider identity differs from official universe")
            raw = response.raw_bytes
        elif self.provider == "finlab":
            cache_key = (dataset, day)
            if cache_key not in self.bundles:
                config = FinLabDatasetConfig(
                    dataset_key=dataset,
                    market="TW",
                    asset_class=feed["asset_class"],
                    currency="TWD",
                    field_datasets={
                        "open": "price:開盤價",
                        "high": "price:最高價",
                        "low": "price:最低價",
                        "close": "price:收盤價",
                        "volume": "price:成交股數",
                    },
                    symbols=tuple(
                        FinLabSymbol(item["provider_symbol"], item["symbol"])
                        for item in release["members"]
                    ),
                    production_scope=True,
                )
                gateway = self._finlab_gateway()
                tables = {}
                for field, name in config.field_datasets.items():
                    tables[field] = gateway.fetch_dataset(
                        name,
                        target_date=day,
                        symbols=tuple(item.source_symbol for item in config.symbols),
                    )
                    self._observed_response(len(self._finlab_table_bytes(tables[field])))
                table_raw = canonical_bytes(
                    {
                        field: {
                            "dates": table.dates,
                            "symbols": table.symbols,
                            "values": [[str(value) for value in row] for row in table.values],
                        }
                        for field, table in tables.items()
                    }
                )
                # Five independent field responses share a bounded derived bundle.
                if len(table_raw) > 5 * int(self.proof["max_response_bytes"]) + 64:
                    raise ReadinessBlockedError("SDK derived bundle exceeds aggregate bound")
                table_stored = self._persist(table_raw, dataset, "dataset_bundle")
                self.bundles[cache_key] = (config, tables, table_raw, table_stored)
            full_config, tables, raw, stored = self.bundles[cache_key]
            single_config = FinLabDatasetConfig(
                dataset_key=dataset,
                market="TW",
                asset_class=feed["asset_class"],
                currency="TWD",
                field_datasets=full_config.field_datasets,
                symbols=(FinLabSymbol(member["provider_symbol"], member["symbol"]),),
                production_scope=True,
            )
            single_tables = {
                field: FinLabDatasetTable(
                    dates=table.dates,
                    symbols=(member["provider_symbol"],),
                    values=tuple(
                        (values[table.symbols.index(member["provider_symbol"])],)
                        for values in table.values
                    ),
                )
                for field, table in tables.items()
            }
            bundle = build_finlab_dataset_bundle(
                config=single_config, target_date=day, tables=single_tables
            )
            request = finlab_request(bundle, fetched_at=now, delivery=delivery)
            request["payload"]["data"] = [
                row for row in request["payload"]["data"] if row["symbol"] == member["symbol"]
            ]
            request["payload"]["batch"]["declared_record_count"] = 1
            request["payload"]["batch"]["delivery_mode"] = "incremental"
        elif self.provider == "shioaji":
            before, after = 0, 1
            snapshot = self._shioaji_request("fetch_kbars", member["provider_symbol"], day)
            raw = canonical_bytes(
                {
                    "kbars": dict(snapshot.kbars),
                    "bytes_before": snapshot.usage_bytes_before,
                    "bytes_after": snapshot.usage_bytes_after,
                }
            )
            self._observed_response(len(raw), usage_bytes=0)
            before, after = snapshot.requests_before, snapshot.requests_after
            stored = self._persist(raw, dataset, member["provider_symbol"])
            identity = checksum(
                canonical_bytes({"plan": plan["plan_id"], "member": member["member_key"]})
            )
            request = build_market_minute_request(
                snapshot.kbars,
                dataset_key=dataset,
                target_date=day,
                symbols=(member["symbol"],),
                fetched_at=now,
                usage_before_requests=before,
                usage_after_requests=after,
                snapshot_id=identity,
                daily_update_id=plan["plan_id"],
                universe_id=release["release_id"],
            )
            request["delivery"] = delivery
            for row in request["payload"]["data"]:
                row["source_symbol"] = member["provider_symbol"]
            request["payload"]["batch"]["provider_usage_before"]["requests_limit"] = self.proof[
                "requests_per_day"
            ]
            request["payload"]["batch"]["provider_usage_after"]["requests_limit"] = self.proof[
                "requests_per_day"
            ]
            raw = canonical_bytes(
                {
                    "kbars": dict(snapshot.kbars),
                    "bytes_before": snapshot.usage_bytes_before,
                    "bytes_after": snapshot.usage_bytes_after,
                }
            )
        else:
            raw, rows, historical = self._taifex_report(day, dataset)
            stored = self._persist(raw, dataset, "exchange_report")
            selected = [
                row
                for row in rows
                if row["contract_code"] == member["contract_code"]
                and row["session"] == member["session"]
            ]
            if len(selected) != 1:
                raise ReadinessBlockedError("actual TAIFEX contract/session is missing")
            if is_no_trade(selected[0]):
                from findb_fetcher.full_market_universe import TAIFEX_HISTORY_URL

                evidence = {
                    "url": TAIFEX_HISTORY_URL if historical else TAIFEX_URL,
                    "sha256": checksum(raw),
                    "observed_at": now.isoformat(),
                    "source_symbol": member["provider_symbol"],
                    "trade_date": day.isoformat(),
                    "session": member["session"],
                    "source_status": "no_trade",
                    "source_excerpt": canonical_bytes(selected[0]).decode()[:500],
                }
                self.raw_store.persist(
                    raw,
                    dataset_key=dataset,
                    source_symbol="exchange_report",
                    provider=self.provider,
                )
                self.source.outcome(
                    plan["plan_id"],
                    work_item_id=part["work_item_id"],
                    member_key=member["member_key"],
                    reason="no_trade",
                    evidence=evidence,
                )
                return []
            if (
                not any(
                    selected[0][field] is not None
                    for field in (
                        "open",
                        "high",
                        "low",
                        "close",
                        "settlement_price",
                        "open_interest",
                    )
                )
                and selected[0]["volume"] is None
            ):
                raise ReadinessBlockedError("TAIFEX quote has no source-provided observation")
            request = {
                "dataset_key": dataset,
                "schema_id": "futures_eod",
                "schema_version": 1,
                "source": "taifex",
                "fetched_at": now.isoformat(),
                "payload": {
                    "batch": {
                        "data_date": day.isoformat(),
                        "delivery_mode": "incremental",
                        "declared_record_count": 1,
                    },
                    "data": selected,
                },
                "delivery": delivery,
            }
        identity = checksum(
            canonical_bytes(
                {
                    "plan": plan["plan_id"],
                    "member": member["member_key"],
                    "rows": request["payload"]["data"],
                }
            )
        )
        if self.provider != "shioaji":
            request["request_key"] = f"full:{identity}"
            request["idempotency_key"] = f"full:{identity}"
        attach_raw_object(request, stored, preserve_identity=self.provider == "shioaji")
        if len(canonical_bytes(request)) >= 1024 * 1024:
            raise ReadinessBlockedError("prepared Source body exceeds byte bound")
        return [request]
