"""HKEX daily quotations: the exchange's own report of every listed security's day.

The Main Board and GEM each publish one report per trading day, around 21:00
Hong Kong time.  The CDN appends a varying script to ordinary downloads, and
occasionally to range responses too, so only a 206 response whose length equals
its ``Content-Range`` total is the exchange's file.  The file is kept gzip
compressed (``mtime=0``, reproducible): that is what Raw R2 and the response
journal hold.
"""

from __future__ import annotations

import gzip
import hashlib
import html
import os
import re
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Literal

import httpx

from findb_fetcher.http_response import BoundedResponseError, read_identity_response
from findb_fetcher.provider_work import durable_response

Board = Literal["main", "gem"]
BOARDS: tuple[Board, ...] = ("main", "gem")
REPORT_BASE_URL = "https://www.hkex.com.hk/eng/stat/smstat/dayquot/"
# The exchange's own ceilings, mirrored by the exchange_daily_quote contract.
MAX_REPORT_BYTES = 64 * 1024 * 1024
MAX_GZIP_BYTES = 16 * 1024 * 1024
# A range response the CDN altered is fetched again, at most this many times in all.
REPORT_ATTEMPTS = 3
HALT_STATUSES = frozenset({"TRADING SUSPENDED", "TRADING HALTED"})
_HEADER = "CODE  NAME OF STOCK    CUR PRV.CLO./"
_NUM = r"([\d,]+(?:\.\d+)?|-|N/A)"
_FIRST = re.compile(r"^[ *]*(\d{1,5})[ #](.{16}) ([A-Z]{3})\s+" + r"\s+".join([_NUM] * 4) + r"\s*$")
_SECOND = re.compile(r"^\s+" + r"\s+".join([_NUM] * 4) + r"\s*$")
_STATUS = re.compile(r"^[ *]*(\d{1,5})[ #](.{16}) ([A-Z]{3}) +([A-Z][A-Z ]*?)\s*$")
_DATE = re.compile(r"DATE:\s+(\d{1,2}) ([A-Z]{3}) (\d{4})")
_RANGE = re.compile(r"^bytes 0-(\d+)/(\d+)$")


class HkexReportNotPublishedError(RuntimeError):
    """HTTP 404: the date's report is not downloadable (yet, or any more)."""


class HkexOperationalError(RuntimeError):
    """The report could not be obtained intact; ``status_code`` None is not HTTP.

    ``retryable`` is False for a failure another request cannot change, such as
    a report larger than the published response bound.
    """

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


class HkexReportFormatError(ValueError):
    """The exchange's file is not a daily quotation of the requested date."""


def report_url(board: Board, trade_date: date) -> str:
    if board == "main":
        return f"{REPORT_BASE_URL}d{trade_date:%y%m%d}e.htm"
    return f"{REPORT_BASE_URL}GEM/e_G{trade_date:%y%m%d}.htm"


def compress(original: bytes) -> bytes:
    """Reproducible gzip of the exchange's file: equal input, equal bytes."""
    return gzip.compress(original, compresslevel=9, mtime=0)


def decompress(body: bytes, *, max_bytes: int = MAX_REPORT_BYTES) -> bytes:
    """One gzip member, nothing after it, within ``max_bytes``."""
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    original = inflater.decompress(body, max_bytes + 1)
    if len(original) > max_bytes or not inflater.eof or inflater.unused_data:
        raise HkexReportFormatError("HKEX report gzip is not one bounded member")
    return original


@dataclass(frozen=True)
class ParsedReport:
    """Every security of one report: quoted rows, and halted lines by symbol."""

    holiday: bool
    rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    halted: dict[str, tuple[str, str]] = field(default_factory=dict)


def _value(text: str) -> str | None:
    return None if text in {"-", "N/A"} else text.replace(",", "")


def parse_report(original: bytes, *, trade_date: date) -> ParsedReport:
    """Read a report strictly: any line that is not a known shape rejects it.

    A quoted security is two lines (previous close, ask, high, shares traded;
    then close, bid, low, turnover).  A suspended or halted one is a single line
    with its status.  The shared holiday page carries no date; only the request
    and the exchange calendar say which day it stands for.
    """
    text = html.unescape(re.sub(r"<[^>]*>", "", original.decode("latin-1")))
    start = text.find(_HEADER)
    if start < 0:
        if "NO TRADING" in text:
            return ParsedReport(holiday=True)
        raise HkexReportFormatError("HKEX report has no quotations")
    published = _DATE.search(text[:start])
    if published is None:
        raise HkexReportFormatError("HKEX report date is absent")
    if datetime.strptime(" ".join(published.groups()), "%d %b %Y").date() != trade_date:
        raise HkexReportFormatError("HKEX report is of another date")
    end = text.find("\n-------", start)
    if end < 0:
        raise HkexReportFormatError("HKEX quotations do not end")
    # The header and its continuation line precede the first security.
    lines = text[start:end].splitlines()[2:]
    rows: dict[str, dict[str, Any]] = {}
    halted: dict[str, tuple[str, str]] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        first = _FIRST.match(line)
        second = _SECOND.match(lines[index + 1]) if index + 1 < len(lines) else None
        if first is not None and second is not None:
            code, name, currency, previous, ask, high, shares = first.groups()
            close, bid, low, turnover = second.groups()
            symbol = code.zfill(5)
            volume = int(_value(shares) or 0)
            row = {
                "symbol": symbol,
                "trade_date": trade_date.isoformat(),
                "name": name.strip() or None,
                "currency": currency,
                "trading_status": "traded" if volume else "no_trade",
                "previous_close": _value(previous),
                "close": _value(close),
                "ask": _value(ask),
                "bid": _value(bid),
                "high": _value(high),
                "low": _value(low),
                "volume": volume,
                "turnover": _value(turnover) or "0",
            }
            if symbol in rows or symbol in halted:
                raise HkexReportFormatError("HKEX report lists a security twice")
            rows[symbol] = row
            index += 2
            continue
        status = _STATUS.match(line)
        if status is not None and status.group(4) in HALT_STATUSES:
            symbol = status.group(1).zfill(5)
            if symbol in rows or symbol in halted:
                raise HkexReportFormatError("HKEX report lists a security twice")
            halted[symbol] = (status.group(4), line.strip())
            index += 1
            continue
        raise HkexReportFormatError("HKEX report has an unknown line")
    if not rows:
        raise HkexReportFormatError("HKEX report quotes no security")
    return ParsedReport(holiday=False, rows=rows, halted=halted)


def _report_for_target(value: Any, arguments: dict[str, Any]) -> bool:
    """A journaled response replays only if it still reads as the target date."""
    try:
        parse_report(decompress(value["gzip"]), trade_date=arguments["trade_date"])
    except (HkexReportFormatError, KeyError, TypeError, UnicodeError, zlib.error):
        return False
    return True


def _retry_after(value: str | None) -> float:
    if value is None:
        return 0
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return 0
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, retry_at.timestamp() - time.time())


def _intact(response: httpx.Response, body: bytes) -> bool:
    """Only a full 206 range whose total is the body's length is the exchange's file."""
    matched = _RANGE.fullmatch(response.headers.get("content-range", "").strip())
    if response.status_code != 206 or matched is None:
        return False
    last, total = int(matched.group(1)), int(matched.group(2))
    return total == len(body) and last == total - 1


@durable_response("hkex_report", ("board", "trade_date"), target_valid=_report_for_target)
def fetch_report(
    client: httpx.Client,
    *,
    board: Board,
    trade_date: date,
    max_response_bytes: int,
    on_observed_bytes: Callable[[int], None] | None = None,
    on_bounded_response: Callable[[int, int], None] | None = None,
    on_http_rate_limited: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """The exchange's file for one board and date, gzip compressed, with its headers.

    Each request carries a fresh query so no cache answers it.  A response the
    CDN altered is requested again; 404 means the report is not downloadable.
    """
    if type(max_response_bytes) is not int or max_response_bytes < 1:
        raise ValueError("HKEX response byte bound invalid")
    from findb_fetcher.acquisition import http_acquisition

    bound = min(max_response_bytes, MAX_REPORT_BYTES)
    url = report_url(board, trade_date)
    for _ in range(REPORT_ATTEMPTS):
        request_url = f"{url}?_={os.urandom(8).hex()}"
        headers = {
            "Range": "bytes=0-",
            "Cache-Control": "no-cache",
            "Accept-Encoding": "identity",
        }
        with (
            http_acquisition("hkex", client, url=request_url) as (operation, extensions),
            client.stream("GET", request_url, headers=headers, extensions=extensions) as response,
        ):
            if response.status_code == 429:
                operation.rate_limited(response.headers.get("Retry-After"))
                if on_http_rate_limited is not None:
                    on_http_rate_limited()
            try:
                body = read_identity_response(response, bound=bound)
            except BoundedResponseError as exc:
                if on_bounded_response is not None:
                    on_bounded_response(exc.observed_bytes, exc.declared_bytes)
                elif on_observed_bytes is not None and exc.observed_bytes:
                    on_observed_bytes(exc.observed_bytes)
                raise HkexOperationalError(
                    "HKEX report response bound failed", retryable=False
                ) from exc
            except httpx.HTTPError as exc:
                observed = getattr(exc, "observed_bytes", 0)
                if on_observed_bytes is not None and observed:
                    on_observed_bytes(observed)
                raise
            if on_observed_bytes is not None:
                on_observed_bytes(len(body))
            if response.status_code == 404:
                raise HkexReportNotPublishedError("HKEX report is not downloadable")
            if response.status_code not in {200, 206}:
                raise HkexOperationalError(
                    "HKEX report request failed",
                    status_code=response.status_code,
                    retry_after=_retry_after(response.headers.get("Retry-After")),
                )
            if not _intact(response, body):
                continue
            last_modified = response.headers.get("last-modified")
            etag = response.headers.get("etag", "").strip()
            if not last_modified or not etag:
                raise HkexOperationalError("HKEX report lacks Last-Modified or ETag")
            try:
                modified = parsedate_to_datetime(last_modified)
            except (TypeError, ValueError):
                raise HkexOperationalError("HKEX report Last-Modified is invalid") from None
            if modified.tzinfo is None:
                raise HkexOperationalError("HKEX report Last-Modified is invalid")
            packed = compress(body)
            if len(packed) > MAX_GZIP_BYTES:
                raise HkexOperationalError("HKEX report gzip exceeds its bound", retryable=False)
            return {
                "board": board,
                "url": url,
                "gzip": packed,
                "original_sha256": hashlib.sha256(body).hexdigest(),
                "original_bytes": len(body),
                "last_modified": modified.astimezone(timezone.utc).isoformat(),
                "etag": etag,
            }
    raise HkexOperationalError("HKEX range responses were altered")
