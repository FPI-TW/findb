"""Shared reading of the TWSE and TPEx public JSON endpoints.

Both exchanges serve their statistics as JSON without credentials. Each body
carries a ``stat`` flag, a ``fields`` header and rows of display strings: dates
in the Minguo calendar (``115/11/24``) or with slashes (``2026/11/24``) and
numbers with thousands separators. A header that differs from the documented
one is a format change and stops the work; a body that is not the documented
JSON (an exchange block page, for example) is retried like an outage.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from findb_fetcher.http_response import BoundedResponseError, read_identity_response

# Both exchanges' statistics pages are small; a month of index rows is about 1 KB.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
TIMEOUT_SECONDS = 30.0
# Bounded diagnostic vocabulary; exchange messages never enter durable state.
ERROR_CATEGORIES = frozenset(
    {
        "rate_limited",
        "blocked",
        "transport",
        "server_error",
        "http_error",
        "response_too_large",
        "malformed_response",
    }
)
RETRYABLE_CATEGORIES = frozenset({"rate_limited", "blocked", "transport", "server_error"})
_ROC_DATE = re.compile(r"^(\d{2,3})/(\d{2})/(\d{2})$")
_ISO_SLASH_DATE = re.compile(r"^(\d{4})/(\d{2})/(\d{2})$")
_NUMBER = re.compile(r"^-?[\d,]+(?:\.\d+)?$")


class TwOfficialError(RuntimeError):
    """Base error of the TWSE and TPEx endpoints."""


class TwOfficialResponseError(TwOfficialError):
    """A request failed; ``category`` is the only durable diagnostic."""

    def __init__(
        self,
        message: str,
        *,
        category: str,
        status_code: int | None = None,
        retry_after: float = 0,
        observed_bytes: int = 0,
        declared_bytes: int = 0,
    ) -> None:
        super().__init__(message)
        if category not in ERROR_CATEGORIES:
            raise ValueError("unknown TWSE/TPEx error category")
        self.category = category
        self.status_code = status_code
        self.retry_after = retry_after
        self.observed_bytes = observed_bytes
        self.declared_bytes = declared_bytes

    @property
    def retryable(self) -> bool:
        return self.category in RETRYABLE_CATEGORIES


class TwOfficialPayloadError(TwOfficialError):
    """An HTTP 200 body that is not the documented JSON; another request may differ."""


class TwOfficialFormatError(TwOfficialError):
    """The documented JSON with another header or shape: the exchange changed the format."""


class TwOfficialNotPublishedError(TwOfficialError):
    """The exchange has not published the target date (yet)."""


def _category(status_code: int) -> str:
    if status_code == 429:
        return "rate_limited"
    if status_code == 403:
        # Both exchanges answer bursts and unwelcome clients with 403 for a while.
        return "blocked"
    if status_code >= 500:
        return "server_error"
    return "http_error"


def _retry_after(value: str | None) -> float:
    try:
        return max(0.0, float(value)) if value is not None else 0.0
    except ValueError:
        return 0.0


def get(
    provider: str,
    client: httpx.Client,
    url: str,
    params: dict[str, str],
    *,
    max_response_bytes: int,
    on_http_rate_limited: Callable[[], None] | None = None,
    on_observed_bytes: Callable[[int], None] | None = None,
) -> bytes:
    """One governed GET without credentials; only an HTTP 200 body is returned."""
    from findb_fetcher.acquisition import http_acquisition

    if type(max_response_bytes) is not int or max_response_bytes < 1:
        raise ValueError("response byte bound must be a positive integer")
    bound = min(max_response_bytes, MAX_RESPONSE_BYTES)
    status_code = None
    try:
        with (
            http_acquisition(provider, client, url=url) as (operation, extensions),
            client.stream(
                "GET",
                url,
                params=params,
                headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                extensions=extensions,
                timeout=TIMEOUT_SECONDS,
            ) as response,
        ):
            status_code = response.status_code
            if status_code == 429:
                operation.rate_limited(response.headers.get("Retry-After"))
                if on_http_rate_limited is not None:
                    on_http_rate_limited()
            try:
                body = read_identity_response(response, bound=bound)
            except BoundedResponseError as exc:
                raise TwOfficialResponseError(
                    f"{provider} {exc}",
                    status_code=status_code,
                    observed_bytes=exc.observed_bytes,
                    declared_bytes=exc.declared_bytes,
                    category=(
                        "response_too_large"
                        if max(exc.observed_bytes, exc.declared_bytes) > bound
                        else "malformed_response"
                    ),
                ) from exc
            if on_observed_bytes is not None:
                on_observed_bytes(len(body))
            if status_code != 200:
                raise TwOfficialResponseError(
                    f"{provider} request failed (HTTP {status_code})",
                    status_code=status_code,
                    retry_after=_retry_after(response.headers.get("Retry-After")),
                    category=_category(status_code),
                )
            return body
    except httpx.TransportError as exc:
        raise TwOfficialResponseError(
            f"{provider} transport request failed", status_code=status_code, category="transport"
        ) from exc


def document(raw: bytes) -> dict[str, Any]:
    """The JSON object of one response."""
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TwOfficialPayloadError("response is not JSON") from exc
    if not isinstance(value, dict):
        raise TwOfficialPayloadError("response is not a JSON object")
    return value


def require_fields(actual: object, expected: Sequence[str]) -> None:
    """The documented header, exactly: a renamed or moved column is a format change."""
    if not isinstance(actual, list) or [str(name).strip() for name in actual] != list(expected):
        raise TwOfficialFormatError("response header differs from the documented one")


def roc_date(text: object) -> date:
    """A Minguo date ``115/11/24`` (year + 1911)."""
    matched = _ROC_DATE.fullmatch(str(text).strip())
    if matched is None:
        raise TwOfficialFormatError("date is not a Minguo date")
    year, month, day = (int(part) for part in matched.groups())
    try:
        return date(year + 1911, month, day)
    except ValueError:
        raise TwOfficialFormatError("date is not a Minguo date") from None


def slash_date(text: object) -> date:
    """A Gregorian date written ``2026/11/24``."""
    matched = _ISO_SLASH_DATE.fullmatch(str(text).strip())
    if matched is None:
        raise TwOfficialFormatError("date is not YYYY/MM/DD")
    year, month, day = (int(part) for part in matched.groups())
    try:
        return date(year, month, day)
    except ValueError:
        raise TwOfficialFormatError("date is not YYYY/MM/DD") from None


def integer(text: object) -> int:
    """A displayed whole number such as ``-1,234``."""
    value = str(text).strip()
    if not re.fullmatch(r"-?[\d,]+", value):
        raise TwOfficialFormatError("value is not a whole number")
    return int(value.replace(",", ""))


def label(text: object) -> str:
    """A row label without the full-width and ordinary spaces used for indenting."""
    return str(text).replace("\u3000", "").strip()


# The per-security layout of ``institutional_flow_stock`` rows objects, in order.
STOCK_COLUMNS = (
    "symbol",
    "name",
    *(
        f"{prefix}_{side}"
        for prefix in ("foreign_ex_dealer", "foreign_dealer", "foreign_total", "investment_trust")
        for side in ("buy", "sell", "net")
    ),
    *(
        f"{prefix}_{side}"
        for prefix in ("dealer_proprietary", "dealer_hedge")
        for side in ("buy", "sell", "net")
    ),
    "dealer_total_buy",
    "dealer_total_sell",
    "dealer_total_net",
    "total_net",
)
_SECURITY = re.compile(r"^[0-9A-Z]{4,6}$")


def security_code(text: object) -> str:
    code = str(text).strip()
    if not _SECURITY.fullmatch(code):
        raise TwOfficialFormatError("security code has another form")
    return code


def rows_object(rows: list[dict[str, Any]]) -> bytes:
    """The delivered rows object: the published column order, one list per security."""
    return json.dumps(
        {
            "columns": list(STOCK_COLUMNS),
            "rows": [[row[name] for name in STOCK_COLUMNS] for row in rows],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def number(text: object) -> Decimal:
    """A displayed number such as ``39,148.91`` or ``-1,234``."""
    value = str(text).strip()
    if not _NUMBER.fullmatch(value):
        raise TwOfficialFormatError("value is not a number")
    try:
        return Decimal(value.replace(",", ""))
    except InvalidOperation:
        raise TwOfficialFormatError("value is not a number") from None
