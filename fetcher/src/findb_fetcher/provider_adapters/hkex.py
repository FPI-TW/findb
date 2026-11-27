"""HKEX: the exchange's two daily reports serve every listed security of the date."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from findb_fetcher.full_market_errors import MemberUnavailableError, ReadinessBlockedError
from findb_fetcher.full_market_universe import HKEX_URL, parse_hkex
from findb_fetcher.provider_adapters import hsil_reference
from findb_fetcher.provider_adapters.base import (
    PASSTHROUGH,
    AcquisitionContext,
    AcquisitionResult,
    FailureKind,
    Phase,
    ProviderAdapter,
    ProviderFailure,
    UniverseContext,
    UniverseSnapshot,
)
from findb_fetcher.providers.hkex import (
    BOARDS,
    Board,
    HkexOperationalError,
    HkexReportFormatError,
    HkexReportNotPublishedError,
    ParsedReport,
    compress,
    decompress,
    fetch_report,
    parse_report,
    report_url,
)
from findb_fetcher.providers.hsil import (
    HsilFormatError,
    HsilNotPublishedError,
    HsilOperationalError,
)
from findb_fetcher.raw_storage import RawObject

DATASET = "hk_equity_official_eod"
# A local copy of downloaded reports, for importing dates HKEX no longer serves:
# ``reports/`` and ``gem_reports/`` with their ``manifest.json``/``gem_manifest.json``.
ARCHIVE_ENV = "HKEX_REPORT_ARCHIVE_DIR"
_ARCHIVE_LAYOUT: dict[Board, tuple[str, str]] = {
    "main": ("reports", "manifest.json"),
    "gem": ("gem_reports", "gem_manifest.json"),
}
HOLIDAY_EXCERPT = "Hong Kong Public Holiday, NO TRADING."
# HSIL's index reference documents, read by the same runtime (see ``hsil_reference``).
REFERENCE_DATASETS = frozenset(hsil_reference.DATASETS)
_HSIL_ERRORS = (HsilFormatError, HsilNotPublishedError, HsilOperationalError)


def _outage(exc: BaseException) -> bool:
    """Another request may succeed: transport errors, 408/429/5xx, an altered file."""
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, HkexOperationalError) and exc.retryable


class HkexAdapter(ProviderAdapter):
    """The exchange's daily quotations, and HSIL's index reference documents."""

    provider = "hkex"
    shared_response_prefix = "hkex_report:"
    # Both reports serve the whole cohort: an outage holds the plan, never a member.
    outage_holds_plan = True

    def __init__(self, archive: Path | None = None) -> None:
        configured = os.getenv(ARCHIVE_ENV)
        self.archive = archive or (Path(configured) if configured else None)
        self.reports: dict[tuple[str, Board], tuple[dict[str, Any], ParsedReport]] = {}
        # One content-addressed raw object per report, cited by every member.
        self.report_objects: dict[tuple[str, Board], RawObject] = {}
        self.reference = hsil_reference.HsilReference()

    def reset(self) -> None:
        self.reports.clear()
        self.report_objects.clear()
        self.reference.reset()

    def pending_shared_fetch(self, dataset_key: str, trade_date: date) -> bool:
        if dataset_key in REFERENCE_DATASETS:
            return self.reference.pending_shared_fetch(dataset_key, trade_date)
        return (trade_date.isoformat(), "main") not in self.reports

    def _archived(self, board: Board, day: date) -> dict[str, Any]:
        """A downloaded report, verified against the manifest written when it was fetched."""
        assert self.archive is not None
        folder, manifest = _ARCHIVE_LAYOUT[board]
        name = report_url(board, day).rsplit("/", 1)[1]
        entries = json.loads((self.archive / manifest).read_text())
        entry = next((item for item in entries if item.get("file") == name), None)
        path = self.archive / folder / f"{name}.gz"
        if entry is None or not path.is_file():
            raise HkexReportNotPublishedError("HKEX report is not in the archive")
        original = decompress(path.read_bytes())
        if hashlib.sha256(original).hexdigest() != entry["sha256"]:
            raise HkexReportFormatError("archived HKEX report differs from its manifest")
        modified = datetime.fromisoformat(entry["last_modified_hkt"])
        return {
            "board": board,
            "url": report_url(board, day),
            "gzip": compress(original),
            "original_sha256": entry["sha256"],
            "original_bytes": len(original),
            "last_modified": modified.astimezone(timezone.utc).isoformat(),
            "etag": entry["etag"],
        }

    def report(
        self, board: Board, context: AcquisitionContext
    ) -> tuple[dict[str, Any], ParsedReport, RawObject]:
        """One board's report of the date: persisted first, then read."""
        day = context.trade_date
        key = (day.isoformat(), board)
        if key not in self.reports:
            if self.archive is not None:
                fetched = self._archived(board, day)
            else:
                fetched = fetch_report(
                    context.http,
                    board=board,
                    trade_date=day,
                    max_response_bytes=context.max_response_bytes,
                    on_observed_bytes=context.observe_response,
                    on_bounded_response=lambda size, declared: context.observe_response(
                        size, declared=declared
                    ),
                    on_http_rate_limited=context.rate_limited,
                )
            stored = context.persist(
                fetched["gzip"], f"daily_quotation_{board}", payload_format="html_gzip"
            )
            if stored.sha256 != hashlib.sha256(fetched["gzip"]).hexdigest():
                raise ReadinessBlockedError("HKEX report raw object differs from the report")
            parsed = parse_report(decompress(fetched["gzip"]), trade_date=day)
            self.report_objects[key] = stored
            self.reports[key] = (fetched, parsed)
        fetched, parsed = self.reports[key]
        return fetched, parsed, self.report_objects[key]

    def build_universe(
        self, feeds: Sequence[Mapping[str, Any]], context: UniverseContext
    ) -> dict[str, UniverseSnapshot]:
        """The exchange's own list; its codes are the report's, so every member is mapped.

        The reference datasets have fixed members (see ``hsil_reference``).
        """
        keys = {feed["dataset_key"] for feed in feeds}
        if unknown := sorted(keys - {DATASET, *REFERENCE_DATASETS}):
            raise ReadinessBlockedError(f"hkex has no universe for {unknown}")
        groups = hsil_reference.universe_groups(self.reference, feeds, context)
        if keys and DATASET not in keys:
            return groups
        listing = context.snapshot(HKEX_URL)
        effective, members = parse_hkex(
            listing, observed_date=context.observed_at.astimezone(context.zones[DATASET]).date()
        )
        for member in members:
            member["mapping_status"] = "mapped"
        groups[DATASET] = UniverseSnapshot(effective, members, {HKEX_URL: listing})
        return groups

    def _evidence(
        self,
        member: Mapping[str, Any],
        context: AcquisitionContext,
        url: str,
        stored: RawObject,
        status: str,
        excerpt: str,
    ) -> dict[str, Any]:
        return {
            "url": url,
            # The raw object's digest: the report as kept in Raw R2 and the archive.
            "sha256": stored.sha256,
            "observed_at": context.fetched_at.isoformat(),
            "source_symbol": member["provider_symbol"],
            "trade_date": context.trade_date.isoformat(),
            "source_status": status,
            "source_excerpt": excerpt[:500],
        }

    def acquire(self, member: Mapping[str, Any], context: AcquisitionContext) -> AcquisitionResult:
        if context.dataset_key in REFERENCE_DATASETS:
            return self.reference.acquire(member, context)
        symbol = member["provider_symbol"]
        gem = "GEM" in str(member.get("classification", ""))
        for board in ("gem", "main") if gem else BOARDS:
            fetched, parsed, stored = self.report(board, context)
            if parsed.holiday:
                # The shared holiday page has no date: the request names the day.
                evidence = self._evidence(
                    member, context, fetched["url"], stored, "no_trade", HOLIDAY_EXCERPT
                )
                return AcquisitionResult.no_trade_evidence(evidence, stored)
            if symbol in parsed.halted:
                _, line = parsed.halted[symbol]
                evidence = self._evidence(member, context, fetched["url"], stored, "halted", line)
                return AcquisitionResult.no_trade_evidence(evidence, stored, reason="halted")
            row = parsed.rows.get(symbol)
            if row is None:
                continue
            if context.contract is None:
                raise ReadinessBlockedError("HKEX contract metadata is unavailable")
            report = {
                "board": board,
                "url": fetched["url"],
                "original_sha256": fetched["original_sha256"],
                "original_bytes": fetched["original_bytes"],
                "gzip_bytes": len(fetched["gzip"]),
                "last_modified": fetched["last_modified"],
                "etag": fetched["etag"],
            }
            request = {
                "dataset_key": context.dataset_key,
                "schema_id": context.contract["schema_id"],
                "schema_version": context.contract["current_version"],
                "source": "hkex",
                "fetched_at": context.fetched_at.isoformat(),
                "payload": {
                    "batch": {
                        "data_date": context.trade_date.isoformat(),
                        "delivery_mode": "incremental",
                        "declared_record_count": 1,
                        "report": report,
                    },
                    "data": [dict(row)],
                },
                "delivery": dict(context.delivery),
            }
            return AcquisitionResult.data(request, stored)
        # Neither report lists it: a listed security the exchange did not quote.
        raise MemberUnavailableError("missing_symbol")

    def classify(self, exc: BaseException, phase: Phase) -> ProviderFailure:
        if phase not in {"member", "round"}:
            return PASSTHROUGH
        if isinstance(exc, _HSIL_ERRORS):
            return self.reference.classify(exc)
        if isinstance(exc, HkexReportNotPublishedError):
            # Downloadable around 21:00; a 404 before that is the exchange's own.
            return ProviderFailure(
                FailureKind.ROUND_RETRY, wait="publication", code="hkex_report_not_published"
            )
        if _outage(exc):
            return ProviderFailure(
                FailureKind.ROUND_RETRY,
                wait="outage",
                retry_after=getattr(exc, "retry_after", 0) or 0,
            )
        if isinstance(exc, (HkexOperationalError, HkexReportFormatError)):
            return ProviderFailure(FailureKind.WORK_FAILED, code="hkex_report_invalid")
        return PASSTHROUGH

    def member_outcome(self, exc: BaseException) -> tuple[str, str | None] | None:
        outcome = self.reference.member_outcome(exc)
        return outcome if outcome is not None else super().member_outcome(exc)

    def diagnostic(self, exc: BaseException) -> dict[str, Any] | None:
        return self.reference.diagnostic(exc)
