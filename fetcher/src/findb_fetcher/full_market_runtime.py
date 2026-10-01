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
from datetime import time as clock_time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx

from findb_fetcher.client import SourceAPIClient, SourceAPIResponseError
from findb_fetcher.full_market_client import FullMarketProtocolError, FullMarketSourceClient
from findb_fetcher.full_market_state import FullMarketState, QuotaBlockedError
from findb_fetcher.full_market_universe import (
    HKEX_URL,
    MAX_SNAPSHOT_BYTES,
    NASDAQ_URLS,
    TAIFEX_URL,
    TW_COMPANY_URLS,
    TW_ISIN_URLS,
    canonical_bytes,
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
from findb_fetcher.http_response import BoundedResponseError, read_identity_response
from findb_fetcher.market_calendar import PublishedCalendarClient
from findb_fetcher.providers.finlab import (
    FinLabDatasetConfig,
    FinLabDatasetTable,
    FinLabSdkGateway,
    FinLabSymbol,
    build_finlab_dataset_bundle,
)
from findb_fetcher.providers.finlab import (
    build_market_eod_request as finlab_request,
)
from findb_fetcher.providers.shioaji import build_market_minute_request
from findb_fetcher.providers.shioaji_session import PersistentIsolatedShioajiGateway
from findb_fetcher.providers.taifex import fetch_report, parse_report, report_members
from findb_fetcher.providers.twelve_data import (
    TwelveDataClient,
    TwelveDataConfig,
    TwelveDataResponseError,
)
from findb_fetcher.providers.twelve_data import (
    build_market_eod_request as td_request,
)
from findb_fetcher.raw_storage import RawObject, RawPayloadStore, attach_raw_object


class ReadinessBlockedError(RuntimeError):
    pass


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
            or set(value["datasets"]) != set(datasets)
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
        if value["max_response_bytes"] > MAX_SNAPSHOT_BYTES:
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


class FullMarketRuntime:
    def __init__(
        self,
        *,
        provider: str,
        config: dict[str, Any],
        state: FullMarketState,
        source: FullMarketSourceClient,
        delivery: SourceAPIClient,
        calendar: PublishedCalendarClient,
        raw_store: RawPayloadStore,
        readiness_file: Path | None,
        client: httpx.Client | None = None,
        can_acquire: Callable[[], bool] | None = None,
    ) -> None:
        self.provider, self.config, self.state = provider, config, state
        self.can_acquire = can_acquire or (lambda: True)
        self.source, self.delivery, self.calendar, self.raw_store = (
            source,
            delivery,
            calendar,
            raw_store,
        )
        self.readiness_file = readiness_file
        self.feeds = [feed for feed in config["feeds"] if feed["provider"] == provider]
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

    def _shioaji_request(self, operation: str, *args: Any) -> Any:
        if self.sdk is not None and not getattr(self.sdk, "is_alive", lambda: True)():
            self.sdk.close()
            self.sdk = None
        try:
            if self.sdk is None:
                self.sdk = PersistentIsolatedShioajiGateway(
                    os.getenv("SHIOAJI_API_KEY", ""), os.getenv("SHIOAJI_SECRET_KEY", "")
                )
            return getattr(self.sdk, operation)(*args)
        except Exception:
            failed, self.sdk = self.sdk, None
            if failed is not None:
                failed.close()
            raise

    def _reserve(self, *, requests: int = 1, byte_count: int | None = None) -> tuple[int, int]:
        while True:
            if not self.can_acquire():
                raise ReadinessBlockedError("scheduler was stopped by Source control")
            now = datetime.now(timezone.utc)
            if datetime.fromisoformat(self.proof["expires_at"]) <= now:
                raise ReadinessBlockedError("current account readiness evidence expired")
            size = int(self.proof["max_response_bytes"]) if byte_count is None else byte_count
            wait, before, after, window = self.state.reserve_acquisition(
                account=self.provider,
                interval=1 / self.proof["requests_per_second"],
                requests=requests,
                byte_count=size,
                request_limit=int(self.proof["requests_per_day"]),
                byte_limit=int(self.proof["bytes_per_day"]),
                minute_limit=int(self.proof["requests_per_minute"]),
            )
            if wait <= 0:
                self.reservation_window = window
                # A contended SQLite transaction may outlive the pre-permit guard.
                # Retain its conservative reservation when control/expiry changes.
                if not self.can_acquire():
                    raise ReadinessBlockedError("scheduler was stopped by Source control")
                if datetime.fromisoformat(self.proof["expires_at"]) <= datetime.now(timezone.utc):
                    raise ReadinessBlockedError("current account readiness evidence expired")
                return before, after
            # Recheck control and expiry, including clock rollback and very low rates.
            time.sleep(min(wait, 1))

    def _observed_response(self, size: int, *, usage_bytes: int | None = None) -> None:
        """Detached SDK bytes are an observable lower bound, not network measurement."""
        bound = int(self.proof["max_response_bytes"])
        observed = max(size, usage_bytes or 0)
        if observed > bound:
            self.state.record_byte_overage(
                account=self.provider,
                window=self.reservation_window,
                byte_count=observed - bound,
            )
            raise ReadinessBlockedError("provider response exceeds verified size bound")

    def _rate_limited(self) -> None:
        self.state.rate_limited(
            account=self.provider,
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
            if provider_response and exc.observed_bytes:
                self._observed_response(exc.observed_bytes)
            raise ReadinessBlockedError(str(exc)) from exc

    def _snapshot(self, url: str, *, provider_response: bool = False) -> bytes:
        bound = int(self.proof["max_response_bytes"]) if provider_response else MAX_SNAPSHOT_BYTES
        with self.http.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
            if response.status_code != 200:
                raise ReadinessBlockedError("official exchange snapshot unavailable")
            body = self._read_response(response, bound=bound, provider_response=provider_response)
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
            self.snapshot_objects[url] = self._persist(body, dataset, "official_universe")
            return body

    def _ensure_proof(self) -> None:
        self.proof = load_readiness(
            self.readiness_file,
            self.provider,
            [feed["dataset_key"] for feed in self.feeds],
            now=datetime.now(timezone.utc),
        )

    def _finlab_gateway(self) -> FinLabSdkGateway:
        try:
            if importlib.metadata.version("finlab") != "1.5.7":
                raise ReadinessBlockedError("FinLab SDK version is unsupported")
        except importlib.metadata.PackageNotFoundError:
            raise ReadinessBlockedError("FinLab pinned SDK is unavailable") from None
        return FinLabSdkGateway.from_env()

    def _catalogue(self) -> list[dict[str, Any]]:
        self._ensure_proof()
        if self.provider == "shioaji":
            self._reserve()
            catalogue = self._shioaji_request("catalogue")
            detached = canonical_bytes(
                {"sdk_version": "1.7.1", "simulation": False, "catalogue": catalogue}
            )
            self._observed_response(len(detached))
            self._persist(detached, "tw_equity_minute", "sdk_catalogue")
            return catalogue
        if self.provider == "finlab":
            return []
        self._reserve()
        config = TwelveDataConfig.from_env()
        # Catalogue and daily acquisition use the exact same durable account ledger.
        with self.http.stream(
            "GET",
            config.base_url + "/stocks",
            params={"apikey": config.api_key},
            headers={"Accept-Encoding": "identity"},
        ) as response:
            if response.status_code == 429:
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
                    raise QuotaBlockedError("provider catalogue rate limited") from exc
                raise
            if response.status_code == 429:
                raise QuotaBlockedError("provider catalogue rate limited")
            if response.status_code != 200:
                raise ReadinessBlockedError("provider catalogue unavailable")
            value = json.loads(raw)
            if not isinstance(value, dict) or not isinstance(value.get("data"), list):
                raise ReadinessBlockedError("provider catalogue shape invalid")
            catalogue_raw = bytes(raw)
            self._persist(catalogue_raw, "us_equity_eod", "provider_catalogue")
            self.catalogue_evidence = {config.base_url + "/stocks": catalogue_raw}
            return value["data"]

    def sync_universes(self) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        snapshots: dict[str, bytes] = {}
        if self.provider == "twelve_data":
            snapshots = {url: self._snapshot(url) for url in (*NASDAQ_URLS, HKEX_URL)}
            us = parse_nasdaq(snapshots[NASDAQ_URLS[0]]) + parse_nasdaq(
                snapshots[NASDAQ_URLS[1]], other=True
            )
            hk_date, hk = parse_hkex(
                snapshots[HKEX_URL], observed_date=now.astimezone(ZoneInfo("Asia/Hong_Kong")).date()
            )
            catalogue = self._catalogue()
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
            groups = {
                "us_equity_eod": (
                    next(iter(us_dates)),
                    map_catalogue(us, catalogue),
                    {**{url: snapshots[url] for url in NASDAQ_URLS}, **self.catalogue_evidence},
                ),
                "hk_equity_eod": (
                    hk_date,
                    map_catalogue(hk, catalogue),
                    {HKEX_URL: snapshots[HKEX_URL], **self.catalogue_evidence},
                ),
            }
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
                # Provider column presence is verified independently from official share classification.
                try:
                    self._ensure_proof()
                    gateway = self._finlab_gateway()
                    for members in (equity, etfs):
                        self._reserve()
                        try:
                            table = gateway.fetch_dataset(
                                "price:收盤價",
                                target_date=now.astimezone(ZoneInfo("Asia/Taipei")).date(),
                                symbols=tuple(member["provider_symbol"] for member in members),
                            )
                        finally:
                            self.state.pace_from_completion(
                                account=self.provider,
                                now=datetime.now(timezone.utc).timestamp(),
                                interval=1 / self.proof["requests_per_second"],
                            )
                        detached = self._finlab_table_bytes(table)
                        self._observed_response(len(detached))
                        self._persist(
                            detached,
                            "tw_equity_eod" if members is equity else "tw_etf_eod",
                            "provider_catalogue",
                        )
                        for member in members:
                            if (
                                member["classification"] != "classification_gap"
                                and member["provider_symbol"] in table.symbols
                            ):
                                member["mapping_status"] = "mapped"
                except Exception:
                    for member in equity + etfs:
                        member["mapping_status"] = "gap"
            suffix = "minute" if self.provider == "shioaji" else "eod"
            groups = {
                f"tw_equity_{suffix}": (
                    now.date(),
                    equity,
                    {url: snapshots[url] for url in (*TW_COMPANY_URLS, *TW_ISIN_URLS)},
                ),
                f"tw_etf_{suffix}": (
                    now.date(),
                    etfs,
                    {url: snapshots[url] for url in TW_ISIN_URLS},
                ),
            }
        else:
            self._ensure_proof()
            self._reserve()
            raw = self._snapshot(TAIFEX_URL, provider_response=True)
            values = json.loads(raw)
            report_date = str(values[0]["Date"]).replace("/", "").replace("-", "")
            day = datetime.strptime(report_date, "%Y%m%d").date()
            if day > now.astimezone(ZoneInfo("Asia/Taipei")).date():
                raise ReadinessBlockedError("TAIFEX report is future-dated")
            rows = parse_report(raw, target_date=day)
            groups = {"tw_futures_eod": (day, report_members(rows), {TAIFEX_URL: raw})}
        result = []
        for feed in self.feeds:
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

    def cycle(self) -> dict[str, Any]:
        self._ensure_proof()
        now = datetime.now(timezone.utc)
        if self.state.refresh_due(self.provider, now.timestamp()):
            try:
                refreshed = self.sync_universes()
                next_refresh = []
                for feed in self.feeds:
                    local_now = now.astimezone(ZoneInfo(feed["source_timezone"]))
                    trigger = datetime.combine(
                        local_now.date(),
                        clock_time.fromisoformat(feed["scheduled_time"]),
                        local_now.tzinfo,
                    )
                    if trigger <= local_now:
                        trigger += timedelta(days=1)
                    next_refresh.append(trigger.timestamp())
                self.state.refresh(
                    self.provider, now=now.timestamp(), result=refreshed, next_at=min(next_refresh)
                )
            except Exception:
                self.state.refresh(
                    self.provider,
                    now=now.timestamp(),
                    result=[{"status": "blocked", "reason": "official_universe_refresh_failed"}],
                )
        releases = []
        total_calls = 0
        for feed in self.feeds:
            listed = self.source.universes(feed["dataset_key"], self.provider)
            if listed.get("activation", {}).get("enabled") is not True or not listed.get(
                "published_release_id"
            ):
                continue
            release = self.source.release(listed["published_release_id"])
            if (
                release["provider"] != self.provider
                or release["dataset_key"] != feed["dataset_key"]
                or release["status"] != "published"
            ):
                raise ReadinessBlockedError("published universe identity invalid")
            total_calls += 5 if self.provider == "finlab" else len(release["members"])
            releases.append((feed, listed, release))
        if not releases:
            return self.state.health(self.provider)
        validate_capacity(
            self.proof,
            total_calls,
            seconds_available=min(feed["completion_window_seconds"] for feed, _, _ in releases),
        )
        due_work: list[tuple[date, dict[str, Any]]] = []
        for feed, listed, release in releases:
            activation = date.fromisoformat(listed["activation"]["activation_date"])
            local = now.astimezone(ZoneInfo(feed["source_timezone"]))
            through = local.date() - timedelta(days=feed["target_date_lag_days"])
            if local.strftime("%H:%M") < feed["scheduled_time"]:
                through -= timedelta(days=1)
            due_dates: list[date] = []
            for year in range(activation.year, through.year + 1):
                calendar_market = (
                    "TAIFEX" if feed["dataset_key"] == "tw_futures_eod" else feed["market"]
                )
                calendar = self.calendar.get_year(calendar_market, year)
                due_dates.extend(
                    day.trade_date
                    for day in calendar.days
                    if activation <= day.trade_date <= through and day.is_open
                )
            due_work.extend((day, feed) for day in due_dates)
        for day, feed in sorted(due_work, key=lambda work: work[0], reverse=True):
            if not self.can_acquire():
                self.pause()
                return self.state.health(self.provider)
            # Current deadline comes first; catchup retains every unresolved open date.
            plan = self._plan_for_date(feed, day)
            self.state.record_plan(plan, self.provider)
            can_continue = True
            if plan["status"] != "complete":
                pinned = self.source.release(plan["release_id"])
                remaining = (
                    datetime.fromisoformat(plan["deadline_at"].replace("Z", "+00:00"))
                    - datetime.now(timezone.utc)
                ).total_seconds()
                if remaining > 0:
                    eligible = self.state.acquisition_members(plan["plan_id"], now=time.time())
                    pending_calls = sum(
                        member["member_key"] in eligible and member["mapping_status"] == "mapped"
                        for member in pinned["members"]
                    )
                    calls = (
                        (5 if pending_calls else 0) if self.provider == "finlab" else pending_calls
                    )
                    validate_capacity(self.proof, calls, seconds_available=remaining)
                can_continue = self._deliver_plan(plan, pinned, feed)
            evaluated = self.source.evaluate(plan["plan_id"])
            self.state.evaluate(evaluated)
            if not can_continue:
                return self.state.health(self.provider)
            if evaluated["status"] == "complete":
                self.bundles.pop((feed["dataset_key"], day), None)
                self.futures.pop(day.isoformat(), None)
        return self.state.health(self.provider)

    def _existing_plan(self, feed: dict[str, Any], day: date) -> dict[str, Any] | None:
        plans = self.source.plans_for_date(dataset=feed["dataset_key"], trade_date=day.isoformat())[
            "data"
        ]
        if not isinstance(plans, list) or len(plans) > 1:
            raise ReadinessBlockedError("Source frozen plan lookup invalid")
        if not plans:
            return None
        plan = plans[0]
        if (
            plan["dataset_key"] != feed["dataset_key"]
            or plan["trade_date"] != day.isoformat()
            or plan["provider"] != self.provider
        ):
            raise ReadinessBlockedError("Source frozen plan identity invalid")
        return plan

    def _plan_for_date(self, feed: dict[str, Any], day: date) -> dict[str, Any]:
        existing = self._existing_plan(feed, day)
        if existing is not None:
            return existing
        try:
            return self.source.plan(
                dataset=feed["dataset_key"],
                provider=self.provider,
                trade_date=day.isoformat(),
                release_id=self._release_for_date(feed, day)["release_id"],
            )
        except FullMarketProtocolError as exc:
            if exc.status_code == 409:
                # Another scheduler may freeze a plan between the read and POST.
                existing = self._existing_plan(feed, day)
                if existing is not None:
                    return existing
            raise

    def _release_for_date(self, feed: dict[str, Any], day: date) -> dict[str, Any]:
        listed = self.source.universes(feed["dataset_key"], self.provider, as_of=day.isoformat())
        if not listed.get("published_release_id"):
            raise ReadinessBlockedError("no published universe applies to catchup date")
        return self.source.release(listed["published_release_id"])

    def _deliver_plan(
        self, plan: dict[str, Any], release: dict[str, Any], feed: dict[str, Any]
    ) -> bool:
        members = {member["member_key"]: member for member in release["members"]}
        for part in plan["parts"]:
            for member_key in part["member_keys"]:
                if not self.can_acquire():
                    self.pause()
                    return False
                member = members[member_key]
                key = part["work_item_id"] + ":" + member_key
                if not self.state.claim(key, now=time.time()):
                    continue
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
                        requests = self._acquire(member, plan, part, release, feed)
                        if not requests:
                            self.state.finish(
                                key, status="complete", reason="source_proven_no_trade"
                            )
                            continue
                        prepared = canonical_bytes(requests)
                        self.state.prepare(key, prepared)
                    normalizing = False
                    for request in json.loads(prepared):
                        immutable = self.delivery.prepare(request)
                        saved_run = self.state.receipt(key)
                        if saved_run is None:
                            receipt = self.delivery.deliver(immutable)
                            saved_run = str(receipt.run_id)
                            self.state.finish(key, status="prepared", receipt=saved_run)
                        run = self.delivery.get_run_status(UUID(saved_run), expected=immutable)
                        if run.status in {"queued", "pending", "processing", "retrying"}:
                            normalizing = True
                            break
                        if run.status != "completed" or run.failed_records:
                            self.state.finish(key, status="manual", reason="normalization_failed")
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
                except (QuotaBlockedError, TwelveDataResponseError) as exc:
                    limited = isinstance(exc, QuotaBlockedError) or exc.is_rate_limited
                    reason = "rate_limited" if limited else "source_error"
                    if limited:
                        self.state.rate_limited(
                            account=self.provider,
                            window=datetime.now(timezone.utc).date().isoformat(),
                            until=time.time() + 60,
                        )
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
                    if limited:
                        return False
                except SourceAPIResponseError:
                    self.state.finish(
                        key,
                        status="prepared" if self.state.prepared(key) else "pending",
                        reason="source_rejected",
                        now=time.time(),
                    )
                except Exception:
                    # Provider/SDK diagnostics never enter durable state or logs.
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

        return True

    def _persist(self, raw: bytes, dataset: str, symbol: str) -> RawObject:
        stored = self.raw_store.persist(
            raw, dataset_key=dataset, source_symbol=symbol.replace("|", ":"), provider=self.provider
        )
        if stored.sha256 != checksum(raw) or stored.size_bytes != len(raw):
            raise ReadinessBlockedError("R2 raw persistence identity differs from acquired bytes")
        return stored

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
            self._reserve()
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
                if exc.observed_bytes:
                    try:
                        self._observed_response(exc.observed_bytes)
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
                response["meta"]["mic_code"] != member["exchange"]
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
                    self._reserve()
                    try:
                        tables[field] = gateway.fetch_dataset(
                            name,
                            target_date=day,
                            symbols=tuple(item.source_symbol for item in config.symbols),
                        )
                    finally:
                        # Slow or failed SDK calls extend the durable account permit.
                        self.state.pace_from_completion(
                            account=self.provider,
                            now=datetime.now(timezone.utc).timestamp(),
                            interval=1 / self.proof["requests_per_second"],
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
            before, after = self._reserve()
            snapshot = self._shioaji_request("fetch_kbars", member["provider_symbol"], day)
            raw = canonical_bytes(
                {
                    "kbars": dict(snapshot.kbars),
                    "bytes_before": snapshot.usage_bytes_before,
                    "bytes_after": snapshot.usage_bytes_after,
                }
            )
            self._observed_response(len(raw), usage_bytes=snapshot.usage_bytes_delta)
            stored = self._persist(raw, dataset, member["provider_symbol"])
            if snapshot.usage_bytes_delta is None:
                raise ReadinessBlockedError(
                    "SDK account bytes unavailable or exceed reserved bound"
                )
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
            request["payload"]["batch"]["provider_usage_before"]["requests_limit"] = int(
                self.proof["requests_per_day"]
            )
            request["payload"]["batch"]["provider_usage_after"]["requests_limit"] = int(
                self.proof["requests_per_day"]
            )
            raw = canonical_bytes(
                {
                    "kbars": dict(snapshot.kbars),
                    "bytes_before": snapshot.usage_bytes_before,
                    "bytes_after": snapshot.usage_bytes_after,
                }
            )
        else:
            if day.isoformat() not in self.futures:
                self._reserve()
                latest_raw = self._snapshot(TAIFEX_URL, provider_response=True)
                self._persist(latest_raw, dataset, "exchange_report")
                try:
                    rows = parse_report(latest_raw, target_date=day)
                    raw, historical = latest_raw, False
                except ValueError:
                    self._reserve()
                    reports = {}
                    rows = []
                    for product in ("TX", "MTX", "TMF", "TE", "TF"):
                        self._reserve()
                        product_raw = fetch_report(
                            self.http,
                            target_date=day,
                            latest=False,
                            product=product,
                            max_response_bytes=int(self.proof["max_response_bytes"]),
                            on_observed_bytes=self._observed_response,
                        )
                        self._persist(product_raw, dataset, product)
                        parsed = parse_report(product_raw, target_date=day, historical=True)
                        if any(row["product_code"] != product for row in parsed):
                            raise ReadinessBlockedError(
                                "TAIFEX per-product report identity changed"
                            )
                        reports[product] = product_raw.decode("cp950")
                        rows.extend(parsed)
                    raw, historical = canonical_bytes(reports), True
                self.futures[day.isoformat()] = (raw, rows, historical)
            raw, rows, historical = self.futures[day.isoformat()]
            stored = self._persist(raw, dataset, "exchange_report")
            selected = [
                row
                for row in rows
                if row["contract_code"] == member["contract_code"]
                and row["session"] == member["session"]
            ]
            if len(selected) != 1:
                raise ReadinessBlockedError("actual TAIFEX contract/session is missing")
            has_observation = any(
                selected[0][field] is not None
                for field in ("open", "high", "low", "close", "settlement_price", "open_interest")
            )
            if not has_observation and selected[0]["volume"] == 0:
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
            if not has_observation and selected[0]["volume"] is None:
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
