"""Hang Seng Indexes (HSIL) reference documents: constituent reports and change workbooks.

HSIL publishes, for the HSI and HSCI series:

- a constituent report PDF for every trading day, kept for about 36 trading
  days and listed by a JSON index of the series' daily reports;
- one change workbook per series, a sheet per index, listing every effective
  addition and deletion back to 2008 (or the index's launch).

Both are downloaded whole and kept gzip compressed (``mtime=0``), as Raw R2 and
the response journal hold them.  The download service sends neither ETag nor
Last-Modified, and answers 404 for a file it does not have.

Only HSI with its four sub-indexes and HSCI with its twelve HSICS industry
indexes are read; the frozen ``INDEXES`` table names them as the publisher does,
and a document that names them otherwise is rejected, not guessed at.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any, Literal
from urllib.parse import quote

import httpx

from findb_fetcher import xlsx
from findb_fetcher.http_response import BoundedResponseError, read_identity_response
from findb_fetcher.provider_work import durable_response
from findb_fetcher.providers.hkex import _retry_after, compress
from findb_fetcher.providers.hsil_pdf import normalize, squeeze

Series = Literal["hsi", "hsci"]
SERIES: tuple[Series, ...] = ("hsi", "hsci")
PUBLISHER = "hsil"
SITE = "https://www.hsi.com.hk"
API = f"{SITE}/api/wsit-hsi-ddoc-ea-public-website-proxy/v1"
# The published bounds of an index reference document, mirrored by its contracts.
MAX_DOCUMENT_BYTES = 64 * 1024 * 1024
MAX_GZIP_BYTES = 16 * 1024 * 1024
MAX_LISTING_BYTES = 1024 * 1024
# A report the parser cannot finish in this time is treated as unreadable.
PARSE_TIMEOUT_SECONDS = 120
_REPORT_RESOURCE = re.compile(
    r"/public-website/webapp/contents/en/indexes/report/(hsi|hsci)/con_[0-9]{1,2}[A-Z][a-z]{2}[0-9]{2}\.pdf"
)
_WIDE = re.compile(r"[\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef]")


class HsilNotPublishedError(RuntimeError):
    """The document of the date is not listed or not downloadable (yet, or any more)."""


class HsilOperationalError(RuntimeError):
    """The document could not be obtained intact; ``status_code`` None is not HTTP."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: float = 0,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.retryable = retryable and (
            status_code is None or status_code in {408, 429} or status_code >= 500
        )


class HsilFormatError(ValueError):
    """The document is not one of the layout this reader knows."""


@dataclass(frozen=True)
class Industry:
    """An HSICS industry, as the constituent reports print it."""

    code: str
    name_en: str
    name_zh: str

    def contract(self) -> dict[str, str]:
        return {
            "scheme": "hsics",
            "code": self.code,
            "name_en": self.name_en,
            "name_zh": self.name_zh,
        }


HSICS: Mapping[str, Industry] = {
    item.code: item
    for item in (
        Industry("00", "Energy", "能源業"),
        Industry("05", "Materials", "原材料業"),
        Industry("10", "Industrials", "工業"),
        Industry("23", "Consumer Discretionary", "非必需性消費"),
        Industry("25", "Consumer Staples", "必需性消費"),
        Industry("28", "Healthcare", "醫療保健業"),
        Industry("35", "Telecommunications", "電訊業"),
        Industry("40", "Utilities", "公用事業"),
        Industry("50", "Financials", "金融業"),
        Industry("60", "Properties and Construction", "地產建築業"),
        Industry("70", "Information Technology", "資訊科技業"),
        Industry("80", "Conglomerates", "綜合企業"),
    )
}


@dataclass(frozen=True)
class Index:
    """One index the reference feeds deliver, named as its workbook sheet names it."""

    code: str
    series: Series
    sheet: str
    name_en: str
    name_zh: str
    parent: str | None = None
    industry: Industry | None = None
    # The HSI report's weighting column of a sub-index.
    weight_column: str | None = None

    def descriptor(self) -> dict[str, Any]:
        value: dict[str, Any] = {"code": self.code, "name_en": self.name_en}
        value["name_zh"] = self.name_zh
        if self.parent is not None:
            value["parent_code"] = self.parent
        if self.industry is not None:
            value["classification"] = self.industry.contract()
        return value


def _industry_index(code: str, sheet: str, industry: str, name_en: str, name_zh: str) -> Index:
    return Index(
        code,
        "hsci",
        sheet,
        f"Hang Seng Composite Industry Index - {name_en}",
        f"恒生綜合行業指數 - {name_zh}",
        parent="00011.00",
        industry=HSICS[industry],
    )


INDEXES: tuple[Index, ...] = (
    Index("00001.00", "hsi", "HSI", "Hang Seng Index", "恒生指數"),
    Index(
        "00001.01",
        "hsi",
        "HSI-FIN",
        "Hang Seng Index - Finance",
        "恒生金融分類指數",
        parent="00001.00",
        weight_column="weight_fin",
    ),
    Index(
        "00001.02",
        "hsi",
        "HSI-UTI",
        "Hang Seng Index - Utilities",
        "恒生公用事業分類指數",
        parent="00001.00",
        weight_column="weight_uti",
    ),
    Index(
        "00001.03",
        "hsi",
        "HSI-PROP",
        "Hang Seng Index - Properties",
        "恒生地產分類指數",
        parent="00001.00",
        weight_column="weight_prop",
    ),
    Index(
        "00001.04",
        "hsi",
        "HSI-C&I",
        "Hang Seng Index - Commerce & Industry",
        "恒生工商業分類指數",
        parent="00001.00",
        weight_column="weight_ci",
    ),
    Index("00011.00", "hsci", "HSCI", "Hang Seng Composite Index", "恒生綜合指數"),
    _industry_index("00011.01", "HSCI-ENG", "00", "Energy", "能源業"),
    _industry_index("00011.02", "HSCI-MAT", "05", "Materials", "原材料業"),
    _industry_index("00011.03", "HSCI-IND", "10", "Industrials", "工業"),
    _industry_index("00011.06", "HSCI-TEL", "35", "Telecommunications", "電訊業"),
    _industry_index("00011.07", "HSCI-UTI", "40", "Utilities", "公用事業"),
    _industry_index("00011.08", "HSCI-FIN", "50", "Financials", "金融業"),
    _industry_index("00011.09", "HSCI-PROP", "60", "Properties & Construction", "地產建築業"),
    _industry_index("00011.10", "HSCI-IT", "70", "Information Technology", "資訊科技業"),
    _industry_index("00011.11", "HSCI-CONG", "80", "Conglomerates", "綜合企業"),
    _industry_index("00011.12", "HSCI-CD", "23", "Consumer Discretionary", "非必需性消費"),
    _industry_index("00011.13", "HSCI-CS", "25", "Consumer Staples", "必需性消費"),
    _industry_index("00011.14", "HSCI-H", "28", "Healthcare", "醫療保健業"),
)
INDEX_BY_CODE: Mapping[str, Index] = {index.code: index for index in INDEXES}
ROOTS: Mapping[Series, Index] = {
    "hsi": INDEX_BY_CODE["00001.00"],
    "hsci": INDEX_BY_CODE["00011.00"],
}
# Indexes the HSCI report also lists, which the feeds do not deliver.
_UNREAD_REPORT_INDEXES = frozenset(
    squeeze(name)
    for name in (
        "Hang Seng Composite LargeCap Index 恒生綜合大型股指數",
        "Hang Seng Composite MidCap Index 恒生綜合中型股指數",
        "Hang Seng Composite SmallCap Index 恒生綜合小型股指數",
        "Hang Seng Composite LargeCap & MidCap Index 恒生綜合大中型股指數",
        "Hang Seng Composite MidCap & SmallCap Index 恒生綜合中小型股指數",
    )
)
_REPORT_INDEXES: Mapping[Series, Mapping[str, Index]] = {
    "hsi": {squeeze(ROOTS["hsi"].name_en + ROOTS["hsi"].name_zh): ROOTS["hsi"]},
    "hsci": {
        squeeze(index.name_en + index.name_zh): index for index in INDEXES if index.series == "hsci"
    },
}
LISTING_PLACES = {squeeze("Hong Kong 香港"): "Hong Kong"}


def listing_url(series: Series) -> str:
    return f"{API}/product-data/pub/series/metadata/v2?data=dailyReport&seriesCode={series}"


def download_url(resource: str) -> str:
    return f"{API}/content/gcs/download?resource={quote(resource, safe='/')}"


def workbook_url(series: Series) -> str:
    return download_url(
        f"/public-website/webapp/contents/en/indexes/hisConstituent/{series}/hist_{series}.xlsx"
    )


def decompress(body: bytes, *, max_bytes: int = MAX_DOCUMENT_BYTES) -> bytes:
    """One gzip member, nothing after it, within ``max_bytes``."""
    try:
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        original = inflater.decompress(body, max_bytes + 1)
    except zlib.error as exc:
        raise HsilFormatError("HSIL document gzip is invalid") from exc
    if len(original) > max_bytes or not inflater.eof or inflater.unused_data:
        raise HsilFormatError("HSIL document gzip is not one bounded member")
    return original


# --- download ---------------------------------------------------------------


def _get(
    client: httpx.Client,
    url: str,
    *,
    bound: int,
    on_observed_bytes: Callable[..., None] | None,
    on_http_rate_limited: Callable[[], None] | None,
) -> tuple[httpx.Response, bytes]:
    from findb_fetcher.acquisition import http_acquisition

    headers = {"Accept-Encoding": "identity", "Cache-Control": "no-cache"}
    with (
        http_acquisition("hkex", client, url=url) as (operation, extensions),
        client.stream("GET", url, headers=headers, extensions=extensions) as response,
    ):
        if response.status_code == 429:
            operation.rate_limited(response.headers.get("Retry-After"))
            if on_http_rate_limited is not None:
                on_http_rate_limited()
        try:
            body = read_identity_response(response, bound=bound)
        except BoundedResponseError as exc:
            if on_observed_bytes is not None:
                on_observed_bytes(exc.observed_bytes, declared=exc.declared_bytes)
            raise HsilOperationalError("HSIL response bound failed", retryable=False) from exc
        except httpx.HTTPError as exc:
            observed = getattr(exc, "observed_bytes", 0)
            if on_observed_bytes is not None and observed:
                on_observed_bytes(observed)
            raise
        if on_observed_bytes is not None:
            on_observed_bytes(len(body))
        if response.status_code == 404:
            raise HsilNotPublishedError("HSIL document is not downloadable")
        if response.status_code != 200:
            raise HsilOperationalError(
                "HSIL request failed",
                status_code=response.status_code,
                retry_after=_retry_after(response.headers.get("Retry-After")),
            )
        return response, body


def _last_modified(response: httpx.Response) -> str | None:
    value = response.headers.get("last-modified")
    if not value:
        return None
    try:
        modified = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if modified.tzinfo is None:
        return None
    return modified.astimezone(timezone.utc).isoformat()


def _document(response: httpx.Response, body: bytes, *, url: str, magic: bytes) -> dict[str, Any]:
    if not body.startswith(magic):
        # A page the CDN answered in its place; another request may succeed.
        raise HsilOperationalError("HSIL response is not the document")
    packed = compress(body)
    if len(packed) > MAX_GZIP_BYTES:
        raise HsilOperationalError("HSIL document gzip exceeds its bound", retryable=False)
    etag = response.headers.get("etag", "").strip()
    return {
        "url": url,
        "gzip": packed,
        "original_sha256": hashlib.sha256(body).hexdigest(),
        "original_bytes": len(body),
        "last_modified": _last_modified(response),
        "etag": etag[:100] or None,
    }


def report_resource(listing: bytes, *, series: Series, trade_date: date) -> str | None:
    """The listed report of ``trade_date``, or None while it is not listed."""
    try:
        groups = json.loads(listing)
        entries = [
            entry
            for group in groups
            if group.get("seriesCode") == series
            for report in group["reportList"]
            if report.get("reportType") == "con-pdf-1"
            for entry in report["reportDate"]
        ]
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise HsilOperationalError("HSIL report listing is malformed") from exc
    found = [entry for entry in entries if entry.get("date") == trade_date.isoformat()]
    if not found:
        return None
    if len(found) > 1:
        raise HsilFormatError("HSIL lists two reports of one date")
    resource = found[0].get("url")
    matched = _REPORT_RESOURCE.fullmatch(resource) if isinstance(resource, str) else None
    if matched is None or matched.group(1) != series:
        raise HsilFormatError("HSIL report location differs from the known layout")
    return str(resource)


def _report_for_target(value: Any, arguments: dict[str, Any]) -> bool:
    """A journaled report replays only if it is still the target date's PDF."""
    try:
        return (
            value["series"] == arguments["series"]
            and value["document_date"] == arguments["trade_date"].isoformat()
            and decompress(value["gzip"]).startswith(b"%PDF-")
        )
    except (HsilFormatError, KeyError, TypeError):
        return False


@durable_response("hsil_report", ("series", "trade_date"), target_valid=_report_for_target)
def fetch_report(
    client: httpx.Client,
    *,
    series: Series,
    trade_date: date,
    max_response_bytes: int,
    on_observed_bytes: Callable[..., None] | None = None,
    on_http_rate_limited: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """The series' constituent report of ``trade_date``, gzip compressed, with its listing."""
    if type(max_response_bytes) is not int or max_response_bytes < 1:
        raise ValueError("HSIL response byte bound invalid")
    _, listing = _get(
        client,
        listing_url(series),
        bound=min(max_response_bytes, MAX_LISTING_BYTES),
        on_observed_bytes=on_observed_bytes,
        on_http_rate_limited=on_http_rate_limited,
    )
    resource = report_resource(listing, series=series, trade_date=trade_date)
    if resource is None:
        raise HsilNotPublishedError("HSIL report of the date is not listed")
    url = download_url(resource)
    response, body = _get(
        client,
        url,
        bound=min(max_response_bytes, MAX_DOCUMENT_BYTES),
        on_observed_bytes=on_observed_bytes,
        on_http_rate_limited=on_http_rate_limited,
    )
    document = _document(response, body, url=url, magic=b"%PDF-")
    return {**document, "series": series, "document_date": trade_date.isoformat()}


def _workbook_valid(value: Any, arguments: dict[str, Any]) -> bool:
    try:
        return value["series"] == arguments["series"] and decompress(value["gzip"]).startswith(
            b"PK"
        )
    except (HsilFormatError, KeyError, TypeError):
        return False


@durable_response("hsil_workbook", ("series", "trade_date"), target_valid=_workbook_valid)
def fetch_workbook(
    client: httpx.Client,
    *,
    series: Series,
    trade_date: date,
    max_response_bytes: int,
    on_observed_bytes: Callable[..., None] | None = None,
    on_http_rate_limited: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """The series' change workbook as observed for ``trade_date``, gzip compressed."""
    if type(max_response_bytes) is not int or max_response_bytes < 1:
        raise ValueError("HSIL response byte bound invalid")
    url = workbook_url(series)
    response, body = _get(
        client,
        url,
        bound=min(max_response_bytes, MAX_DOCUMENT_BYTES),
        on_observed_bytes=on_observed_bytes,
        on_http_rate_limited=on_http_rate_limited,
    )
    return {**_document(response, body, url=url, magic=b"PK"), "series": series}


# --- constituent reports ----------------------------------------------------


def read_report_records(original: bytes, series: Series) -> list[dict[str, str]]:
    """The report's records, read in a child process bounded in time."""
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-m", "findb_fetcher.providers.hsil_pdf", series],
            input=original,
            capture_output=True,
            timeout=PARSE_TIMEOUT_SECONDS,
            check=False,
            env={},
        )
    except subprocess.TimeoutExpired as exc:
        raise HsilFormatError("HSIL report parse timed out") from exc
    if completed.returncode != 0:
        raise HsilFormatError("HSIL report parser failed")
    try:
        result = json.loads(completed.stdout)
    except ValueError as exc:
        raise HsilFormatError("HSIL report parser answered malformed output") from exc
    if not isinstance(result, dict) or "error" in result:
        raise HsilFormatError("HSIL report layout differs from the known one")
    records = result.get("records")
    if not isinstance(records, list):
        raise HsilFormatError("HSIL report parser answered malformed output")
    return records


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", normalize(text)).strip()


def split_name(text: str) -> tuple[str | None, str | None]:
    """The English and Chinese short names of a report's name cell.

    The cell prints the English name, a space, then the Chinese name.  The
    Chinese name starts at the last space before its first wide character
    (``TCL ELECTRONICS TCL電子``); a cell without one is split only when it
    prints one name twice.  Anything else is left unnamed, not guessed.
    """
    plain = _plain(text)
    wide = _WIDE.search(plain)
    if wide is not None:
        cut = plain.rfind(" ", 0, wide.start())
        english, chinese = (plain[:cut], plain[cut + 1 :]) if cut > 0 else ("", plain)
        if english.isascii() and english.strip():
            return english.strip(), chinese.strip()
        return None, chinese.strip() or None
    words = plain.split(" ")
    half = len(words) // 2
    if words and len(words) % 2 == 0 and words[:half] == words[half:]:
        name = " ".join(words[:half])
        return name, name
    return None, None


_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?")


def _decimal(text: str | None, *, places: int) -> str:
    plain = _plain(text or "")
    if _NUMBER.fullmatch(plain) is None:
        raise HsilFormatError("HSIL report prints an unexpected number")
    try:
        value = Decimal(plain)
    except InvalidOperation as exc:  # pragma: no cover - the pattern admits only decimals
        raise HsilFormatError("HSIL report prints an unexpected number") from exc
    if -value.as_tuple().exponent > places:  # type: ignore[operator]
        raise HsilFormatError("HSIL report prints more decimals than published")
    return plain


def _industry(text: str | None) -> Industry:
    matched = re.fullmatch(r"([0-9]{2})(.+)", squeeze(text or ""))
    industry = HSICS.get(matched.group(1)) if matched else None
    if industry is None or squeeze(industry.name_en + industry.name_zh) != matched.group(2):  # type: ignore[union-attr]
        raise HsilFormatError("HSIL report prints an unknown industry")
    return industry


@dataclass(frozen=True)
class ParsedReport:
    """One report's lists as contract values: the indexes it lists and their rows."""

    indexes: list[dict[str, Any]]
    rows: list[dict[str, Any]]


def report_contents(
    records: Sequence[Mapping[str, str]], *, series: Series, trade_date: date
) -> ParsedReport:
    """Map a report's records to the lists of the indexes the feed delivers.

    The HSCI report lists each industry index on its own rows.  The HSI report
    lists only HSI rows, each with its weight in the one sub-index it belongs to.
    """
    known = _REPORT_INDEXES[series]
    printed_date = f"{trade_date:%Y%m%d}"
    rows: list[dict[str, Any]] = []
    for record in records:
        if record.get("date") != printed_date:
            raise HsilFormatError("HSIL report lists another trade date")
        name = squeeze(record.get("index", ""))
        index = known.get(name)
        if index is None:
            if series == "hsci" and name in _UNREAD_REPORT_INDEXES:
                continue
            raise HsilFormatError("HSIL report lists an unknown index")
        code = re.fullmatch(r"([0-9]{4,5})\.HK", _plain(record.get("code", "")))
        place = LISTING_PLACES.get(squeeze(record.get("listed", "")))
        currency = _plain(record.get("currency", ""))
        if code is None or place is None or re.fullmatch(r"[A-Z]{3}", currency) is None:
            raise HsilFormatError("HSIL report prints an unknown constituent shape")
        name_en, name_zh = split_name(record.get("name", ""))
        industry = _industry(record.get("industry"))
        row: dict[str, Any] = {
            "index_code": index.code,
            "trade_date": trade_date.isoformat(),
            "symbol": code.group(1).zfill(5),
            "listing_place": place,
            "currency": currency,
            "close": _decimal(record.get("close"), places=6),
            "weight_pct": _decimal(record.get("weight"), places=4),
            "industry": industry.contract(),
        }
        if name_en is not None:
            row["name_en"] = name_en
        if name_zh is not None:
            row["name_zh"] = name_zh
        rows.append(row)
        if series != "hsi":
            continue
        subs = [item for item in INDEXES if item.weight_column and record.get(item.weight_column)]
        if len(subs) != 1:
            raise HsilFormatError("HSI report constituent is not in exactly one sub-index")
        (sub,) = subs
        rows.append(
            {
                **row,
                "index_code": sub.code,
                "weight_pct": _decimal(record.get(sub.weight_column), places=4),  # type: ignore[arg-type]
            }
        )
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["index_code"]] = counts.get(row["index_code"], 0) + 1
    root = ROOTS[series]
    if root.code not in counts:
        raise HsilFormatError("HSIL report lists no constituent of its series")
    indexes = [
        {**index.descriptor(), "constituent_count": counts[index.code]}
        for index in INDEXES
        if index.series == series and index.code in counts
    ]
    return ParsedReport(indexes, rows)


# --- change workbooks -------------------------------------------------------

_SHEET_HEADER = (
    "EffectiveDate生效日期",
    "No.ofConstituents",
    "Change變動",
    "Count數目",
    "StockCode股份代號",
    "ListingPlace上市地點",
    "StockName",
    "股份名稱",
)
_ACTIONS = {"Add加入": "add", "Delete刪除": "delete"}
_EFFECTIVE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


@dataclass(frozen=True)
class IndexHistory:
    """One sheet: an index's effective changes, newest first as published."""

    index: Index
    rows: list[dict[str, Any]]

    @property
    def history_start(self) -> date:
        return min(date.fromisoformat(row["effective_date"]) for row in self.rows)

    @property
    def latest(self) -> date:
        return max(date.fromisoformat(row["effective_date"]) for row in self.rows)


def _change_row(cells: Mapping[str, str], ordinal: int) -> dict[str, Any]:
    try:
        effective = date.fromisoformat(cells.get("A", ""))
    except ValueError as exc:
        raise HsilFormatError("HSIL workbook prints an unknown effective date") from exc
    action = _ACTIONS.get(squeeze(cells.get("C", "")))
    place = LISTING_PLACES.get(squeeze(cells.get("F", "")))
    code = cells.get("E", "")
    after = cells.get("B", "")
    count = cells.get("D", "")
    if (
        action is None
        or place is None
        or re.fullmatch(r"[0-9]{1,5}", code) is None
        or re.fullmatch(r"[0-9]+(?:\.0+)?", after) is None
        or re.fullmatch(r"[+-][0-9]+", count) is None
    ):
        raise HsilFormatError("HSIL workbook prints an unknown change shape")
    row: dict[str, Any] = {
        "ordinal": ordinal,
        "effective_date": effective.isoformat(),
        "action": action,
        "symbol": code.zfill(5),
        "listing_place": place,
        "constituents_after": int(Decimal(after)),
        "count_change": int(count),
    }
    if name_en := _plain(cells.get("G", "")):
        row["name_en"] = name_en
    if name_zh := _plain(cells.get("H", "")):
        row["name_zh"] = name_zh
    return row


def _sheet_history(index: Index, rows: Sequence[Mapping[str, str]]) -> IndexHistory:
    title = [_plain(row.get("A", "")) for row in rows[:2]]
    if title != [f"Constituent Changes in {index.name_en}", f"{index.name_zh}之成份股變動"]:
        raise HsilFormatError("HSIL workbook sheet names another index")
    header = next(
        (
            number
            for number, row in enumerate(rows)
            if squeeze(row.get("A", "")) == _SHEET_HEADER[0]
        ),
        None,
    )
    if (
        header is None
        or tuple(squeeze(rows[header].get(column, "")) for column in "ABCDEFGH") != _SHEET_HEADER
    ):
        raise HsilFormatError("HSIL workbook sheet header differs")
    changes: list[dict[str, Any]] = []
    # The header's second line, then one change per row; notes follow the last one.
    body = rows[header + 2 :]
    count = next(
        (number for number, row in enumerate(body) if not _EFFECTIVE.fullmatch(row.get("A", ""))),
        len(body),
    )
    if any(_EFFECTIVE.fullmatch(row.get("A", "")) for row in body[count:]):
        raise HsilFormatError("HSIL workbook sheet interleaves notes and changes")
    for row in body[:count]:
        changes.append(_change_row(row, len(changes) + 1))
    if not changes:
        raise HsilFormatError("HSIL workbook sheet lists no change")
    return IndexHistory(index, changes)


def workbook_histories(original: bytes, series: Series) -> dict[str, IndexHistory]:
    """Every index of the series the workbook has a sheet for, by index code."""
    try:
        sheets = xlsx.sheets(original)
    except xlsx.XlsxError as exc:
        raise HsilFormatError("HSIL workbook is not readable") from exc
    histories: dict[str, IndexHistory] = {}
    for index in INDEXES:
        if index.series == series and index.sheet in sheets:
            histories[index.code] = _sheet_history(index, sheets[index.sheet])
    if ROOTS[series].code not in histories:
        raise HsilFormatError("HSIL workbook has no sheet of its series")
    return histories
