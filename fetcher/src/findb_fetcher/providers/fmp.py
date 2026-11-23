"""Financial Modeling Prep: market index daily rows and their ``market_eod.v2`` mapping.

One ``historical-price-eod/full`` request per index and date (``from`` = ``to`` =
the target date). Indices have no splits, so these values are the index levels
themselves. FMP serves the current date before the close and never marks a row
final: the feed is scheduled after the measured settle time, and only a row dated
the target date counts. The key travels only in the ``apikey`` header and never
enters URLs, errors or durable state.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any

import httpx

from findb_fetcher.http_response import BoundedResponseError, read_identity_response
from findb_fetcher.provider_work import durable_response

FMP_SOURCE = "fmp"
FMP_DEFAULT_BASE_URL = "https://financialmodelingprep.com"
FMP_DAILY_PATH = "/stable/historical-price-eod/full"
FMP_INDEX_LIST_PATH = "/stable/index-list"
# Journal identity of one index's daily response; never rename.
FMP_DAILY_RESPONSE = "fmp_index_daily"
FMP_INDEX_LIST_RESPONSE = "fmp_index_list"
_MAX_RESPONSE_BYTES_HARD = 16 * 1024 * 1024
# FMP index symbols: a caret and the provider's code, e.g. ``^GSPC``.
INDEX_SYMBOL = re.compile(r"^\^[A-Z0-9][A-Z0-9.]{0,19}$")
# Index levels above this do not fit the canonical NUMERIC(20, 8) column.
_MAX_LEVEL = Decimal("1000000000000")
# Bounded diagnostic vocabulary; provider messages never enter durable state.
FMP_ERROR_CATEGORIES = frozenset(
    {
        "rate_limited",
        "transport",
        "server_error",
        "auth_failed",
        "plan_restricted",
        "http_error",
        "response_too_large",
        "malformed_response",
    }
)
RETRYABLE_CATEGORIES = frozenset({"rate_limited", "transport", "server_error"})


class FMPError(RuntimeError):
    """Base error for FMP integration failures."""


class FMPConfigError(FMPError):
    """Raised when FMP settings are missing or invalid."""


class FMPResponseError(FMPError):
    """An FMP request failed; ``category`` is the only durable diagnostic."""

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
        if category not in FMP_ERROR_CATEGORIES:
            raise ValueError("unknown FMP error category")
        self.category = category
        self.status_code = status_code
        self.retry_after = retry_after
        self.observed_bytes = observed_bytes
        self.declared_bytes = declared_bytes


class FMPPayloadError(FMPError):
    """A 200 response is not the documented JSON shape; the provider's, not a member's."""


class FMPRowNotPublishedError(FMPError):
    """The response has no row dated the target date yet."""


class FMPRowError(FMPError):
    """The target date's row cannot become a contract row; ``code`` names why."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise FMPConfigError(f"{name} must be a number") from exc


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise FMPConfigError(f"{name} must be an integer") from exc


@dataclass(frozen=True, slots=True)
class FMPConfig:
    """Provider-owned configuration that never enters FinDB backend settings."""

    api_key: str = field(repr=False)
    base_url: str = FMP_DEFAULT_BASE_URL
    timeout_seconds: float = 60.0
    max_response_bytes: int = _MAX_RESPONSE_BYTES_HARD

    def __post_init__(self) -> None:
        api_key = self.api_key.strip()
        if not api_key:
            raise FMPConfigError("FMP_API_KEY is required")
        if len(api_key) > 512:
            raise FMPConfigError("FMP_API_KEY exceeds 512 characters")
        base_url = self.base_url.strip().rstrip("/")
        try:
            parsed = httpx.URL(base_url)
        except httpx.InvalidURL as exc:
            raise FMPConfigError("FMP_BASE_URL must be a valid HTTPS origin") from exc
        if (
            parsed.scheme != "https"
            or not parsed.host
            or bool(parsed.username)
            or bool(parsed.password)
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise FMPConfigError("FMP_BASE_URL must be a valid HTTPS origin")
        if not isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise FMPConfigError("FMP_TIMEOUT_SECONDS must be a finite, positive number")
        if not 1 <= self.max_response_bytes <= _MAX_RESPONSE_BYTES_HARD:
            raise FMPConfigError(
                f"FMP_MAX_RESPONSE_BYTES must be between 1 and {_MAX_RESPONSE_BYTES_HARD}"
            )
        object.__setattr__(self, "api_key", api_key)
        object.__setattr__(self, "base_url", base_url)

    @classmethod
    def from_env(cls) -> FMPConfig:
        return cls(
            api_key=os.getenv("FMP_API_KEY", ""),
            base_url=os.getenv("FMP_BASE_URL", FMP_DEFAULT_BASE_URL),
            timeout_seconds=_float_env("FMP_TIMEOUT_SECONDS", 60.0),
            max_response_bytes=_int_env("FMP_MAX_RESPONSE_BYTES", _MAX_RESPONSE_BYTES_HARD),
        )


def authorization_headers(api_key: str) -> dict[str, str]:
    # Header credentials keep the key out of URLs, tracebacks and proxies.
    return {"apikey": api_key, "Accept-Encoding": "identity"}


def redacted_message(message: object, api_key: str) -> str:
    text = message.decode("utf-8", "replace") if isinstance(message, bytes) else str(message or "")
    if api_key:
        text = text.replace(api_key, "[redacted]")
    return text[:300]


def _category(status_code: int) -> str:
    if status_code == 429:
        return "rate_limited"
    if status_code == 401:
        return "auth_failed"
    if status_code in {402, 403}:
        return "plan_restricted"
    if status_code >= 500:
        return "server_error"
    return "http_error"


def _retry_after(value: str | None) -> float:
    try:
        return max(0.0, float(value)) if value is not None else 0.0
    except ValueError:
        return 0.0


def daily_rows(raw: bytes) -> list[dict[str, Any]]:
    """The rows of one ``historical-price-eod/full`` response, newest first as served."""
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FMPPayloadError("malformed FMP daily JSON") from exc
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise FMPPayloadError("FMP daily response must be a JSON list of objects")
    return value


def target_row(raw: bytes, *, symbol: str, day: date) -> dict[str, Any]:
    """The one row of ``symbol`` dated ``day``; its absence means not yet published."""
    rows = [
        row
        for row in daily_rows(raw)
        if row.get("date") == day.isoformat() and row.get("symbol") == symbol
    ]
    if len(rows) > 1:
        raise FMPPayloadError("FMP daily response repeats the target date")
    if not rows:
        raise FMPRowNotPublishedError("fmp_row_not_published")
    return rows[0]


def index_list(raw: bytes) -> dict[str, dict[str, Any]]:
    """``/stable/index-list`` keyed by symbol."""
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise FMPPayloadError("malformed FMP index list JSON") from exc
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise FMPPayloadError("FMP index list must be a JSON list of objects")
    listed: dict[str, dict[str, Any]] = {}
    for row in value:
        symbol = row.get("symbol")
        if isinstance(symbol, str):
            if symbol in listed:
                raise FMPPayloadError("FMP index list repeats a symbol")
            listed[symbol] = row
    return listed


def _level(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise FMPRowError("FMP_VALUE_INVALID")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise FMPRowError("FMP_VALUE_INVALID") from None
    if not number.is_finite() or number <= 0 or number >= _MAX_LEVEL:
        raise FMPRowError("FMP_VALUE_INVALID")
    return number


def contract_row(native: dict[str, Any], *, symbol: str, currency: str) -> dict[str, Any]:
    """One ``market_eod.v2`` row of an index level.

    Open, high, low and close are all required: FMP serves all four for every index
    it covers, so a missing one is a malformed row, not a partial bar. Their order
    is left to Backend DQ, the one authority on bar consistency.

    Volume is FMP's figure unchanged, zero included: no other source serves index
    volume. It is not one consistent series (``^RUT`` and ``^NYA`` mostly repeat
    ``^GSPC``'s; ``^HSCE`` counts thousands of shares since 2024-10-08), which
    ``docs/dev/fmp-provider.md`` records for readers of the data.
    """
    levels = {name: _level(native.get(name)) for name in ("open", "high", "low", "close")}
    volume = native.get("volume")
    if isinstance(volume, float) and volume.is_integer():
        volume = int(volume)
    if volume is not None and (
        isinstance(volume, bool) or not isinstance(volume, int) or not 0 <= volume <= 2**63 - 1
    ):
        raise FMPRowError("FMP_VOLUME_INVALID")
    return {
        "symbol": symbol,
        "source_symbol": native["symbol"],
        "trade_date": native["date"],
        "currency": currency,
        **{name: str(number) for name, number in levels.items()},
        "volume": volume,
    }


def _target_present(value: Any, arguments: dict[str, Any]) -> bool:
    """Replay a journaled response only when it already carried the target row."""
    try:
        target_row(value, symbol=arguments["symbol"], day=arguments["day"])
    except (FMPError, TypeError, KeyError):
        return False
    return True


class FMPClient:
    """Small synchronous client for the index daily and index list endpoints."""

    def __init__(self, config: FMPConfig, *, client: httpx.Client | None = None) -> None:
        self._config = config
        self._client = client or httpx.Client(timeout=config.timeout_seconds)
        self._owns_client = client is None

    @property
    def base_url(self) -> str:
        return self._config.base_url

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _bound(self, max_response_bytes: int | None) -> int:
        bound = self._config.max_response_bytes
        if max_response_bytes is not None:
            if type(max_response_bytes) is not int or max_response_bytes < 1:
                raise ValueError("response byte bound must be a positive integer")
            bound = min(bound, max_response_bytes)
        return bound

    def _get(
        self,
        path: str,
        params: dict[str, str],
        *,
        max_response_bytes: int | None,
        on_http_rate_limited: Callable[[], None] | None,
        on_observed_bytes: Callable[[int], None] | None,
    ) -> bytes:
        """One governed GET; only an HTTP 200 body is returned."""
        from findb_fetcher.acquisition import http_acquisition

        bound = self._bound(max_response_bytes)
        url = f"{self._config.base_url}{path}"
        status_code = None
        try:
            with (
                http_acquisition(
                    FMP_SOURCE, self._client, url=url, credential=self._config.api_key, units=1
                ) as (operation, extensions),
                self._client.stream(
                    "GET",
                    url,
                    params=params,
                    headers=authorization_headers(self._config.api_key),
                    extensions=extensions,
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
                    raise FMPResponseError(
                        f"FMP {exc}",
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
                    raise FMPResponseError(
                        f"FMP request failed (HTTP {status_code}): "
                        + redacted_message(body[:300], self._config.api_key),
                        status_code=status_code,
                        retry_after=_retry_after(response.headers.get("Retry-After")),
                        category=_category(status_code),
                    )
                return body
        except httpx.TransportError as exc:
            raise FMPResponseError(
                "FMP transport request failed", status_code=status_code, category="transport"
            ) from exc

    @durable_response(FMP_DAILY_RESPONSE, ("symbol", "day"), target_valid=_target_present)
    def index_daily(
        self,
        symbol: str,
        day: date,
        *,
        max_response_bytes: int | None = None,
        on_http_rate_limited: Callable[[], None] | None = None,
        on_observed_bytes: Callable[[int], None] | None = None,
    ) -> bytes:
        """The exact response bytes of one index for one date."""
        if not INDEX_SYMBOL.fullmatch(symbol):
            raise ValueError("FMP index symbol is invalid")
        return self._get(
            FMP_DAILY_PATH,
            {"symbol": symbol, "from": day.isoformat(), "to": day.isoformat()},
            max_response_bytes=max_response_bytes,
            on_http_rate_limited=on_http_rate_limited,
            on_observed_bytes=on_observed_bytes,
        )

    @durable_response(FMP_INDEX_LIST_RESPONSE, ())
    def index_list_raw(
        self,
        *,
        max_response_bytes: int | None = None,
        on_http_rate_limited: Callable[[], None] | None = None,
        on_observed_bytes: Callable[[int], None] | None = None,
    ) -> bytes:
        """The exact bytes of FMP's index list: symbol, name, exchange and currency."""
        return self._get(
            FMP_INDEX_LIST_PATH,
            {},
            max_response_bytes=max_response_bytes,
            on_http_rate_limited=on_http_rate_limited,
            on_observed_bytes=on_observed_bytes,
        )
