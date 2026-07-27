"""Twelve Data daily time-series fetch and ``market_eod.v1`` mapping."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any

import httpx

TWELVE_DATA_SOURCE = "twelve_data"
TWELVE_DATA_DEFAULT_BASE_URL = "https://api.twelvedata.com"
_DATASET_KEY_PATTERN = re.compile(r"^[a-z0-9_]{1,50}$")
_CURRENCY_PATTERN = re.compile(r"^[A-Z0-9]{3,10}$")
_INTEGER_PATTERN = re.compile(r"^[0-9]+$")
_EXCHANGE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._&/+:-]{0,99}$")
_MIC_CODE_PATTERN = re.compile(r"^[A-Z0-9]{4}$")
_DEFAULT_ALLOWED_TYPES = frozenset({"Common Stock"})
_MAX_RESPONSE_BYTES_HARD = 16 * 1024 * 1024


class TwelveDataError(RuntimeError):
    """Base error for Twelve Data integration failures."""


class TwelveDataConfigError(TwelveDataError):
    """Twelve Data runtime configuration is absent or invalid."""


class TwelveDataResponseError(TwelveDataError):
    """The Twelve Data API returned an unsuccessful or malformed response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        provider_code: int | None = None,
    ) -> None:
        self.status_code = status_code
        self.provider_code = provider_code
        super().__init__(message)

    @property
    def is_rate_limited(self) -> bool:
        return self.status_code == 429 or self.provider_code == 429


class TwelveDataPayloadError(TwelveDataError):
    """A successful provider response cannot be mapped safely."""


class TwelveDataNoNewDataError(TwelveDataError):
    """A valid provider response contains no rows after the durable checkpoint."""


class TwelveDataResponse(dict[str, Any]):
    """Parsed provider response retaining the exact bounded HTTP body."""

    def __init__(self, payload: Mapping[str, Any], *, raw_bytes: bytes) -> None:
        super().__init__(payload)
        self.raw_bytes = raw_bytes


@dataclass(frozen=True, slots=True)
class TwelveDataConfig:
    """Provider-owned configuration that never enters FinDB backend settings."""

    api_key: str = field(repr=False)
    base_url: str = TWELVE_DATA_DEFAULT_BASE_URL
    timeout_seconds: float = 30.0
    max_response_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        api_key = self.api_key.strip()
        if not api_key:
            raise TwelveDataConfigError("TWELVE_DATA_API_KEY is required")
        if len(api_key) > 512:
            raise TwelveDataConfigError("TWELVE_DATA_API_KEY exceeds 512 characters")

        base_url = self.base_url.strip().rstrip("/")
        try:
            parsed_url = httpx.URL(base_url)
        except httpx.InvalidURL as exc:
            raise TwelveDataConfigError(
                "TWELVE_DATA_BASE_URL must be a valid HTTPS origin"
            ) from exc
        if (
            parsed_url.scheme != "https"
            or not parsed_url.host
            or bool(parsed_url.username)
            or bool(parsed_url.password)
            or parsed_url.query
            or parsed_url.fragment
            or parsed_url.path not in ("", "/")
        ):
            raise TwelveDataConfigError("TWELVE_DATA_BASE_URL must be a valid HTTPS origin")
        if not isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise TwelveDataConfigError(
                "TWELVE_DATA_TIMEOUT_SECONDS must be a finite, positive number"
            )
        if not 1 <= self.max_response_bytes <= _MAX_RESPONSE_BYTES_HARD:
            raise TwelveDataConfigError(
                f"TWELVE_DATA_MAX_RESPONSE_BYTES must be between 1 and {_MAX_RESPONSE_BYTES_HARD}"
            )

        object.__setattr__(self, "api_key", api_key)
        object.__setattr__(self, "base_url", base_url)

    @classmethod
    def from_env(cls) -> "TwelveDataConfig":
        api_key = os.getenv("TWELVE_DATA_API_KEY", "")
        base_url = os.getenv("TWELVE_DATA_BASE_URL", TWELVE_DATA_DEFAULT_BASE_URL)
        timeout_seconds = _positive_float_env("TWELVE_DATA_TIMEOUT_SECONDS", 30.0)
        max_response_bytes = _positive_int_env("TWELVE_DATA_MAX_RESPONSE_BYTES", 8 * 1024 * 1024)
        return cls(
            api_key=api_key,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
        )


class TwelveDataClient:
    """Small synchronous client for the provider's daily time-series endpoint."""

    def __init__(
        self,
        config: TwelveDataConfig,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._config = config
        self._client = client or httpx.Client(timeout=config.timeout_seconds)
        self._owns_client = client is None

    def fetch_daily(
        self,
        symbol: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        outputsize: int | None = None,
        exchange: str | None = None,
    ) -> TwelveDataResponse:
        normalized_symbol = symbol.strip()
        if not normalized_symbol or len(normalized_symbol) > 100:
            raise ValueError("symbol must contain between 1 and 100 characters")
        if start_date is not None and end_date is not None and start_date > end_date:
            raise ValueError("start_date must not be after end_date")
        if outputsize is not None and not 1 <= outputsize <= 5000:
            raise ValueError("outputsize must be between 1 and 5000")
        if start_date is not None and end_date is not None and outputsize is not None:
            raise ValueError("outputsize must be omitted when both date bounds are set")

        params: dict[str, str | int] = {
            "symbol": normalized_symbol,
            "interval": "1day",
            "order": "asc",
            "format": "JSON",
            "adjust": "splits",
            "apikey": self._config.api_key,
        }
        if start_date is not None:
            params["start_date"] = start_date.isoformat()
        if end_date is not None:
            params["end_date"] = end_date.isoformat()
        if outputsize is not None:
            params["outputsize"] = outputsize
        if exchange is not None:
            normalized_exchange = exchange.strip()
            if _EXCHANGE_PATTERN.fullmatch(normalized_exchange) is None:
                raise ValueError("exchange must contain 1 to 100 supported ASCII characters")
            params["exchange"] = normalized_exchange

        try:
            with self._client.stream(
                "GET",
                f"{self._config.base_url}/time_series",
                params=params,
                headers={"Accept-Encoding": "identity"},
            ) as response:
                content_encoding = response.headers.get("content-encoding")
                if content_encoding is not None and content_encoding.strip().lower() != "identity":
                    raise TwelveDataResponseError(
                        "Twelve Data returned an unsupported Content-Encoding"
                    )
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_length = int(content_length)
                    except ValueError as exc:
                        raise TwelveDataResponseError(
                            "Twelve Data returned an invalid Content-Length"
                        ) from exc
                    if declared_length < 0:
                        raise TwelveDataResponseError(
                            "Twelve Data returned an invalid Content-Length"
                        )
                    if declared_length > self._config.max_response_bytes:
                        raise TwelveDataResponseError("Twelve Data response exceeds the size limit")
                raw_buffer = bytearray()
                chunks = (response.content,) if response.is_stream_consumed else response.iter_raw()
                for chunk in chunks:
                    if len(raw_buffer) + len(chunk) > self._config.max_response_bytes:
                        raise TwelveDataResponseError("Twelve Data response exceeds the size limit")
                    raw_buffer.extend(chunk)
                raw_bytes = bytes(raw_buffer)
                response_status_code = response.status_code
        except httpx.TransportError as exc:
            raise TwelveDataResponseError("Twelve Data transport request failed") from exc

        try:
            payload = json.loads(raw_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise TwelveDataResponseError(
                f"Twelve Data returned non-JSON HTTP {response_status_code}"
            ) from exc
        if not isinstance(payload, dict):
            raise TwelveDataResponseError("Twelve Data response must be a JSON object")

        provider_status = payload.get("status")
        if response_status_code >= 400 or provider_status == "error":
            raw_provider_code = payload.get("code", response_status_code)
            provider_code = _provider_code(raw_provider_code)
            provider_message = _redacted_message(payload.get("message"), self._config.api_key)
            raise TwelveDataResponseError(
                f"Twelve Data request failed ({raw_provider_code}): {provider_message}",
                status_code=response_status_code,
                provider_code=provider_code,
            )
        if provider_status != "ok":
            raise TwelveDataResponseError("Twelve Data response status must be 'ok'")
        return TwelveDataResponse(payload, raw_bytes=raw_bytes)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "TwelveDataClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def build_market_eod_request(
    response: Mapping[str, Any],
    *,
    dataset_key: str,
    fetched_at: datetime,
    requested_symbol: str,
    requested_exchange: str | None = None,
    canonical_symbol: str | None = None,
    allowed_instrument_types: Sequence[str] = tuple(_DEFAULT_ALLOWED_TYPES),
    after_trade_date: date | None = None,
) -> dict[str, Any]:
    """Map a successful daily response into a deterministic ``market_eod.v1`` request."""

    if _DATASET_KEY_PATTERN.fullmatch(dataset_key) is None:
        raise TwelveDataPayloadError("dataset_key must be a stable lowercase identifier")
    if fetched_at.tzinfo is None or fetched_at.utcoffset() is None:
        raise TwelveDataPayloadError("fetched_at must be timezone-aware")
    if response.get("status") != "ok":
        raise TwelveDataPayloadError("provider response status must be 'ok'")

    meta = _required_mapping(response, "meta", "response")
    if _required_string(meta, "interval", "meta") != "1day":
        raise TwelveDataPayloadError("Twelve Data interval must be '1day'")
    source_symbol = _required_string(meta, "symbol", "meta")
    if len(source_symbol) > 100:
        raise TwelveDataPayloadError("Twelve Data symbol exceeds 100 characters")
    normalized_requested_symbol = requested_symbol.strip()
    if (
        not normalized_requested_symbol
        or len(normalized_requested_symbol) > 100
        or source_symbol.casefold() != normalized_requested_symbol.casefold()
    ):
        raise TwelveDataPayloadError("Twelve Data symbol does not match the requested symbol")
    instrument_type = _required_string(meta, "type", "meta")
    if instrument_type not in set(allowed_instrument_types):
        raise TwelveDataPayloadError(f"unsupported Twelve Data instrument type: {instrument_type}")
    currency = _required_string(meta, "currency", "meta").upper()
    if _CURRENCY_PATTERN.fullmatch(currency) is None:
        raise TwelveDataPayloadError("Twelve Data currency is invalid")
    exchange = _required_string(meta, "exchange", "meta")
    mic_code = _required_string(meta, "mic_code", "meta")
    if _EXCHANGE_PATTERN.fullmatch(exchange) is None:
        raise TwelveDataPayloadError("Twelve Data exchange is invalid")
    if _MIC_CODE_PATTERN.fullmatch(mic_code) is None:
        raise TwelveDataPayloadError("Twelve Data MIC code is invalid")
    if requested_exchange is not None:
        normalized_requested_exchange = requested_exchange.strip()
        if (
            _EXCHANGE_PATTERN.fullmatch(normalized_requested_exchange) is None
            or exchange.casefold() != normalized_requested_exchange.casefold()
        ):
            raise TwelveDataPayloadError(
                "Twelve Data exchange does not match the requested exchange"
            )

    target_symbol = (canonical_symbol or source_symbol).strip()
    if not target_symbol or len(target_symbol) > 50:
        raise TwelveDataPayloadError("canonical symbol must contain between 1 and 50 characters")

    values = response.get("values")
    if not isinstance(values, list) or not values:
        raise TwelveDataPayloadError("Twelve Data values must be a non-empty list")
    if len(values) > 5000:
        raise TwelveDataPayloadError("Twelve Data values exceed the 5000-row endpoint limit")

    dated_rows: list[tuple[date, dict[str, Any]]] = []
    seen_dates: set[date] = set()
    for index, value in enumerate(values):
        if not isinstance(value, Mapping):
            raise TwelveDataPayloadError(f"values[{index}] must be an object")
        trade_date = _daily_date(value, index)
        if trade_date in seen_dates:
            raise TwelveDataPayloadError(f"duplicate Twelve Data date: {trade_date.isoformat()}")
        seen_dates.add(trade_date)
        open_value = _non_negative_decimal(value, "open", index)
        high_value = _non_negative_decimal(value, "high", index)
        low_value = _non_negative_decimal(value, "low", index)
        close_value = _non_negative_decimal(value, "close", index)
        if high_value < max(open_value, low_value, close_value):
            raise TwelveDataPayloadError(f"values[{index}].high violates OHLC bounds")
        if low_value > min(open_value, high_value, close_value):
            raise TwelveDataPayloadError(f"values[{index}].low violates OHLC bounds")

        row: dict[str, Any] = {
            "symbol": target_symbol,
            "source_symbol": source_symbol,
            "trade_date": trade_date.isoformat(),
            "currency": currency,
            "open": _decimal_text(open_value),
            "high": _decimal_text(high_value),
            "low": _decimal_text(low_value),
            "close": _decimal_text(close_value),
        }
        volume = value.get("volume")
        if volume not in (None, ""):
            if not isinstance(volume, str) or _INTEGER_PATTERN.fullmatch(volume.strip()) is None:
                raise TwelveDataPayloadError(f"values[{index}].volume must be an integer string")
            row["volume"] = int(volume)
        dated_rows.append((trade_date, row))

    dated_rows.sort(key=lambda item: item[0])
    if after_trade_date is not None:
        dated_rows = [item for item in dated_rows if item[0] > after_trade_date]
        if not dated_rows:
            raise TwelveDataNoNewDataError("Twelve Data response has no rows after checkpoint")
    first_date = dated_rows[0][0]
    last_date = dated_rows[-1][0]
    rows = [row for _, row in dated_rows]
    batch: dict[str, Any] = {
        "data_date": last_date.isoformat(),
        "delivery_mode": "incremental" if first_date == last_date else "backfill",
        "declared_record_count": len(rows),
    }
    if first_date != last_date:
        batch["coverage_start_date"] = first_date.isoformat()
        batch["coverage_end_date"] = last_date.isoformat()

    identity = json.dumps(
        {
            "dataset_key": dataset_key,
            "source": TWELVE_DATA_SOURCE,
            "source_symbol": source_symbol,
            "exchange": exchange,
            "mic_code": mic_code,
            "first_date": first_date.isoformat(),
            "last_date": last_date.isoformat(),
            "schema": "market_eod.v1",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    identity_digest = hashlib.sha256(identity).hexdigest()
    normalized_fetched_at = fetched_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    return {
        "dataset_key": dataset_key,
        "schema_id": "market_eod",
        "schema_version": 1,
        "source": TWELVE_DATA_SOURCE,
        "request_key": f"twelve_data:{dataset_key}:{identity_digest[:32]}",
        "idempotency_key": f"twelve_data:{identity_digest}",
        "fetched_at": normalized_fetched_at,
        "payload": {
            "batch": batch,
            "data": rows,
        },
    }


def _positive_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise TwelveDataConfigError(f"{name} must be a number") from exc
    if not isfinite(value) or value <= 0:
        raise TwelveDataConfigError(f"{name} must be a finite, positive number")
    return value


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    if not raw.isascii() or not raw.isdigit():
        raise TwelveDataConfigError(f"{name} must be a positive integer")
    value = int(raw)
    if value <= 0:
        raise TwelveDataConfigError(f"{name} must be a positive integer")
    return value


def _provider_code(value: object) -> int | None:
    if type(value) is int:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _redacted_message(value: object, api_key: str) -> str:
    message = value if isinstance(value, str) and value.strip() else "provider rejected the request"
    return message.replace(api_key, "[redacted]")[:300]


def _required_mapping(parent: Mapping[str, Any], key: str, scope: str) -> Mapping[str, Any]:
    value = parent.get(key)
    if not isinstance(value, Mapping):
        raise TwelveDataPayloadError(f"{scope}.{key} must be an object")
    return value


def _required_string(parent: Mapping[str, Any], key: str, scope: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TwelveDataPayloadError(f"{scope}.{key} must be a non-empty string")
    return value.strip()


def _daily_date(value: Mapping[str, Any], index: int) -> date:
    raw = value.get("datetime")
    if not isinstance(raw, str):
        raise TwelveDataPayloadError(f"values[{index}].datetime must be a date string")
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise TwelveDataPayloadError(
            f"values[{index}].datetime must use YYYY-MM-DD for daily data"
        ) from exc
    if raw != parsed.isoformat():
        raise TwelveDataPayloadError(f"values[{index}].datetime must use canonical YYYY-MM-DD")
    return parsed


def _non_negative_decimal(value: Mapping[str, Any], key: str, index: int) -> Decimal:
    raw = value.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise TwelveDataPayloadError(f"values[{index}].{key} must be a decimal string")
    try:
        parsed = Decimal(raw)
    except InvalidOperation as exc:
        raise TwelveDataPayloadError(f"values[{index}].{key} must be numeric") from exc
    if not parsed.is_finite() or parsed < 0:
        raise TwelveDataPayloadError(f"values[{index}].{key} must be finite and non-negative")
    return parsed


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")
