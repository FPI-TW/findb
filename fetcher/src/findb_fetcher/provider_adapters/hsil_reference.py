"""HSIL reference documents inside the HKEX runtime: two datasets of fixed members.

- ``hk_index_constituents``: one member per series (``HSI``, ``HSCI``), each the
  series' constituent report of the trade date, delivered whole;
- ``hk_index_changes``: one member per index (its code, such as ``00011.08``),
  each its sheet of the series' change workbook as observed that day.

A workbook is the publisher's current one, not a dated document: it stands for
its latest effective date, so a workbook that already lists a later date is not
delivered for an earlier trade date.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from datetime import date, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

from findb_fetcher.full_market_errors import MemberUnavailableError, ReadinessBlockedError
from findb_fetcher.provider_adapters.base import (
    PASSTHROUGH,
    AcquisitionContext,
    AcquisitionResult,
    FailureKind,
    ProviderFailure,
    UniverseContext,
    UniverseSnapshot,
)
from findb_fetcher.providers.hsil import (
    INDEX_BY_CODE,
    INDEXES,
    PUBLISHER,
    SERIES,
    HsilFormatError,
    HsilNotPublishedError,
    HsilOperationalError,
    IndexHistory,
    ParsedReport,
    Series,
    compress,
    decompress,
    download_url,
    fetch_report,
    fetch_workbook,
    listing_url,
    read_report_records,
    report_contents,
    workbook_histories,
)
from findb_fetcher.raw_storage import RawObject

CONSTITUENTS = "hk_index_constituents"
CHANGES = "hk_index_changes"
# A local copy of HSIL's reports, for dates HSIL no longer serves: the folder the
# archive job keeps, with ``manifest.json`` and ``constituent_reports/<series>/``.
ARCHIVE_ENV = "HSIL_DOCUMENT_ARCHIVE_DIR"
DATASETS = (CONSTITUENTS, CHANGES)


def member(symbol: str, provider_symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "provider_symbol": provider_symbol,
        "exchange": "XHKG",
        "currency": "HKD",
        "asset_class": "index",
        "mapping_status": "mapped",
    }


def fixed_members(dataset: str) -> list[dict[str, Any]]:
    """The dataset's members: a report per series, a change history per index."""
    if dataset == CONSTITUENTS:
        return [member(series.upper(), series) for series in SERIES]
    if dataset == CHANGES:
        return [member(index.code, index.code) for index in INDEXES]
    raise ReadinessBlockedError(f"hkex has no reference dataset {dataset}")


def _outage(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, HsilOperationalError) and exc.retryable


class HsilReference:
    """Per-round documents of the two datasets, each fetched and read once."""

    def __init__(self, archive: Path | None = None) -> None:
        configured = os.getenv(ARCHIVE_ENV)
        self.archive = archive or (Path(configured) if configured else None)
        self.reports: dict[tuple[Series, str], tuple[dict[str, Any], ParsedReport, RawObject]] = {}
        self.workbooks: dict[
            tuple[Series, str], tuple[dict[str, Any], dict[str, IndexHistory], RawObject]
        ] = {}

    def reset(self) -> None:
        self.reports.clear()
        self.workbooks.clear()

    def pending_shared_fetch(self, dataset_key: str, trade_date: date) -> bool:
        # Each member reads its own series' document; nothing is shared across the cohort.
        return False

    # --- universe -------------------------------------------------------------

    def universe(self, dataset: str, context: UniverseContext) -> UniverseSnapshot:
        """The fixed members, evidenced by the publisher's current report listings."""
        lag = (context.target_date_lags or {}).get(dataset)
        if type(lag) is not int or lag < 0:
            raise ReadinessBlockedError("hkex reference universe needs the published target lag")
        local = context.observed_at.astimezone(context.zones[dataset]).date()
        evidence = {listing_url(series): context.snapshot(listing_url(series)) for series in SERIES}
        return UniverseSnapshot(local - timedelta(days=lag), fixed_members(dataset), evidence)

    # --- acquisition ------------------------------------------------------------

    def _report(
        self, series: Series, context: AcquisitionContext
    ) -> tuple[dict[str, Any], ParsedReport, RawObject]:
        key = (series, context.trade_date.isoformat())
        if key not in self.reports:
            fetched = self._archived(series, context.trade_date) or fetch_report(
                context.http,
                series=series,
                trade_date=context.trade_date,
                max_response_bytes=context.max_response_bytes,
                on_observed_bytes=context.observe_response,
                on_http_rate_limited=context.rate_limited,
            )
            # Kept before reading, so an unreadable report still cites its bytes.
            stored = context.persist(
                fetched["gzip"], f"constituent_report_{series}", payload_format="pdf_gzip"
            )
            records = read_report_records(decompress(fetched["gzip"]), series)
            parsed = report_contents(records, series=series, trade_date=context.trade_date)
            self.reports[key] = (fetched, parsed, stored)
        return self.reports[key]

    def _archived(self, series: Series, day: date) -> dict[str, Any] | None:
        """The archived report of the date, verified against its manifest; None if absent.

        A date the archive does not hold is read from HSIL as usual.
        """
        if self.archive is None:
            return None
        manifest = json.loads((self.archive / "manifest.json").read_text())
        prefix = f"constituent_reports/{series}/{day.isoformat()}_con_"
        found = [
            (name, entry)
            for name, entry in manifest.items()
            if name.startswith(prefix) and name.endswith(".pdf")
        ]
        if not found:
            return None
        if len(found) > 1:
            raise HsilFormatError("the HSIL archive holds two reports of one date")
        name, entry = found[0]
        original = (self.archive / name).read_bytes()
        if hashlib.sha256(original).hexdigest() != entry.get("sha256"):
            raise HsilFormatError("archived HSIL report differs from its manifest")
        resource = f"/public-website/webapp/contents/en/indexes/report/{series}/{Path(name).name.split('_', 1)[1]}"
        if entry.get("url") != download_url(resource):
            raise HsilFormatError("archived HSIL report names another location")
        modified = None
        if entry.get("last_modified"):
            try:
                parsed = parsedate_to_datetime(entry["last_modified"])
                modified = parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else None
            except (TypeError, ValueError):
                modified = None
        return {
            "url": entry["url"],
            "gzip": compress(original),
            "original_sha256": entry["sha256"],
            "original_bytes": len(original),
            "last_modified": modified,
            "etag": entry.get("etag") or None,
            "series": series,
            "document_date": day.isoformat(),
        }

    def _workbook(
        self, series: Series, context: AcquisitionContext
    ) -> tuple[dict[str, Any], dict[str, IndexHistory], RawObject]:
        key = (series, context.trade_date.isoformat())
        if key not in self.workbooks:
            fetched = fetch_workbook(
                context.http,
                series=series,
                trade_date=context.trade_date,
                max_response_bytes=context.max_response_bytes,
                on_observed_bytes=context.observe_response,
                on_http_rate_limited=context.rate_limited,
            )
            stored = context.persist(
                fetched["gzip"], f"change_workbook_{series}", payload_format="xlsx_gzip"
            )
            histories = workbook_histories(decompress(fetched["gzip"]), series)
            self.workbooks[key] = (fetched, histories, stored)
        return self.workbooks[key]

    @staticmethod
    def _document(
        fetched: Mapping[str, Any], *, kind: str, series: Series, day: date, part: str
    ) -> dict[str, Any]:
        document: dict[str, Any] = {
            "publisher": PUBLISHER,
            "kind": kind,
            "series": series,
            "url": fetched["url"],
            "document_date": day.isoformat(),
            "part_key": part,
            "original_sha256": fetched["original_sha256"],
            "original_bytes": fetched["original_bytes"],
            "gzip_bytes": len(fetched["gzip"]),
        }
        if fetched.get("last_modified"):
            document["last_modified"] = fetched["last_modified"]
        if fetched.get("etag"):
            document["etag"] = fetched["etag"]
        return document

    @staticmethod
    def _request(
        context: AcquisitionContext, batch: dict[str, Any], rows: list[dict[str, Any]]
    ) -> dict[str, Any]:
        assert context.contract is not None
        return {
            "dataset_key": context.dataset_key,
            "schema_id": context.contract["schema_id"],
            "schema_version": context.contract["current_version"],
            "source": "hkex",
            "fetched_at": context.fetched_at.isoformat(),
            "payload": {
                "batch": {
                    "data_date": context.trade_date.isoformat(),
                    "delivery_mode": "full_snapshot",
                    "declared_record_count": len(rows),
                    **batch,
                },
                "data": rows,
            },
            "delivery": dict(context.delivery),
        }

    def acquire(self, member: Mapping[str, Any], context: AcquisitionContext) -> AcquisitionResult:
        if context.contract is None:
            raise ReadinessBlockedError("hkex reference contract metadata is unavailable")
        symbol = member["symbol"]
        if context.dataset_key == CONSTITUENTS:
            series = next((item for item in SERIES if item.upper() == symbol), None)
            if series is None or member["provider_symbol"] != series:
                raise ReadinessBlockedError("hkex constituent reports serve only HSI and HSCI")
            fetched, parsed, stored = self._report(series, context)
            batch = {
                "document": self._document(
                    fetched,
                    kind="constituent_report",
                    series=series,
                    day=context.trade_date,
                    part="document",
                ),
                "indexes": parsed.indexes,
            }
            return AcquisitionResult.data(self._request(context, batch, parsed.rows), stored)
        if context.dataset_key != CHANGES:
            raise ReadinessBlockedError(f"hkex does not serve {context.dataset_key}")
        index = INDEX_BY_CODE.get(symbol)
        if index is None or member["provider_symbol"] != symbol:
            raise ReadinessBlockedError("hkex change histories serve only the known indexes")
        fetched, histories, stored = self._workbook(index.series, context)
        history = histories.get(index.code)
        if history is None:
            # The workbook has no sheet for this index (any more).
            raise MemberUnavailableError("missing_symbol")
        latest = max(item.latest for item in histories.values())
        if latest > context.trade_date:
            # The publisher's current workbook already speaks for a later day.
            raise MemberUnavailableError("document_later_than_date")
        batch = {
            "document": self._document(
                fetched, kind="change_workbook", series=index.series, day=latest, part=index.code
            ),
            "index": index.descriptor(),
            "history_start": history.history_start.isoformat(),
        }
        return AcquisitionResult.data(self._request(context, batch, list(history.rows)), stored)

    # --- failures -----------------------------------------------------------------

    @staticmethod
    def classify(exc: BaseException) -> ProviderFailure:
        if isinstance(exc, HsilNotPublishedError):
            return ProviderFailure(
                FailureKind.ROUND_RETRY, wait="publication", code="hsil_document_not_published"
            )
        if _outage(exc):
            return ProviderFailure(
                FailureKind.ROUND_RETRY,
                wait="outage",
                code="hsil_unavailable",
                retry_after=getattr(exc, "retry_after", 0) or 0,
            )
        if isinstance(exc, (HsilOperationalError, HsilFormatError)):
            # The publisher changed its documents: retrying cannot help, a code change can.
            return ProviderFailure(FailureKind.WORK_FAILED, code="hsil_document_invalid")
        return PASSTHROUGH

    @staticmethod
    def member_outcome(exc: BaseException) -> tuple[str, str | None] | None:
        # A past report the publisher no longer keeps will not return by retrying.
        if isinstance(exc, HsilNotPublishedError):
            return ("missing", "no_source_row")
        return None

    @staticmethod
    def diagnostic(exc: BaseException) -> dict[str, Any] | None:
        if isinstance(exc, HsilOperationalError):
            return {"category": "hsil_unavailable", "http_status": exc.status_code}
        if isinstance(exc, HsilFormatError):
            return {"category": "format_changed"}
        if isinstance(exc, HsilNotPublishedError):
            return {"category": "not_published"}
        return None


def universe_groups(
    reference: HsilReference, feeds: Sequence[Mapping[str, Any]], context: UniverseContext
) -> dict[str, UniverseSnapshot]:
    return {
        feed["dataset_key"]: reference.universe(feed["dataset_key"], context)
        for feed in feeds
        if feed["dataset_key"] in DATASETS
    }
