"""Runtime configuration limited to Fetcher-owned concerns."""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from math import isfinite
from pathlib import Path

import httpx


class ConfigError(ValueError):
    """Fetcher environment configuration is missing or invalid."""


_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


@dataclass(frozen=True, slots=True)
class FetcherConfig:
    source_api_url: str
    source_client_key: str
    contracts_dir: Path = Path("/app/contracts")
    request_timeout_seconds: float = 30.0
    max_attempts: int = 3
    max_retry_after_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> "FetcherConfig":
        source_api_url = _https_origin_env("SOURCE_API_URL")
        source_client_key = _required_env("SOURCE_CLIENT_KEY")
        contracts_dir = Path(os.getenv("FETCHER_CONTRACTS_DIR", "/app/contracts"))
        request_timeout_seconds = _positive_float_env("FETCHER_REQUEST_TIMEOUT_SECONDS", 30.0)
        max_attempts = _positive_int_env("FETCHER_MAX_ATTEMPTS", 3)
        max_retry_after_seconds = _non_negative_float_env("FETCHER_MAX_RETRY_AFTER_SECONDS", 30.0)
        return cls(
            source_api_url=source_api_url,
            source_client_key=source_client_key,
            contracts_dir=contracts_dir,
            request_timeout_seconds=request_timeout_seconds,
            max_attempts=max_attempts,
            max_retry_after_seconds=max_retry_after_seconds,
        )


@dataclass(frozen=True, slots=True)
class MarketCalendarConfig:
    """Required read-only access to FinDB's published market calendar."""

    serve_base_url: str
    api_key: str
    request_timeout_seconds: float = 10.0
    cache_ttl_seconds: float = 300.0

    @classmethod
    def from_env(cls) -> "MarketCalendarConfig":
        timeout = _positive_float_env("FETCHER_CALENDAR_TIMEOUT_SECONDS", 10.0)
        cache_ttl = _positive_float_env("FETCHER_CALENDAR_CACHE_TTL_SECONDS", 300.0)
        return cls(
            serve_base_url=_https_origin_env("FINDB_SERVE_BASE_URL"),
            api_key=_required_env("FETCHER_CALENDAR_SERVE_API_KEY"),
            request_timeout_seconds=timeout,
            cache_ttl_seconds=cache_ttl,
        )


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required")
    return value


def _https_origin_env(name: str) -> str:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        raise ConfigError(f"{name} is required")
    if (
        raw != raw.strip()
        or "\\" in raw
        or "%" in raw
        or "@" in raw
        or "?" in raw
        or "#" in raw
        or raw.rstrip("/").endswith(":")
        or any(character.isspace() or ord(character) == 127 for character in raw)
    ):
        raise ConfigError(f"{name} must be a valid HTTPS origin")
    try:
        parsed = httpx.URL(raw)
        port = parsed.port
    except (httpx.InvalidURL, ValueError) as exc:
        raise ConfigError(f"{name} must be a valid HTTPS origin") from exc
    if (
        parsed.scheme != "https"
        or not parsed.host
        or bool(parsed.username)
        or bool(parsed.password)
        or parsed.path not in ("", "/")
        or bool(parsed.query)
        or bool(parsed.fragment)
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ConfigError(f"{name} must be a valid HTTPS origin")
    host = _canonical_host(parsed.host, raw)
    authority = f"{host}:{port}" if port is not None else host
    return f"https://{authority}"


def _canonical_host(host: str, raw_url: str) -> str:
    authority = raw_url.split("://", 1)[1].rstrip("/")
    if ":" in host:
        if not authority.startswith("["):
            raise ConfigError("SOURCE_API_URL must be a valid HTTPS origin")
        try:
            address = ipaddress.IPv6Address(host)
        except ValueError as exc:
            raise ConfigError("SOURCE_API_URL must be a valid HTTPS origin") from exc
        return f"[{address.compressed}]"

    if host.replace(".", "").isdigit():
        try:
            return str(ipaddress.IPv4Address(host))
        except ValueError as exc:
            raise ConfigError("SOURCE_API_URL must be a valid HTTPS origin") from exc

    if len(host) > 253:
        raise ConfigError("SOURCE_API_URL must be a valid HTTPS origin")
    labels = host.split(".")
    if any(_DNS_LABEL.fullmatch(label) is None for label in labels):
        raise ConfigError("SOURCE_API_URL must be a valid HTTPS origin")
    return host


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value < 1:
        raise ConfigError(f"{name} must be at least 1")
    return value


def _positive_float_env(name: str, default: float) -> float:
    value = _non_negative_float_env(name, default)
    if value == 0:
        raise ConfigError(f"{name} must be greater than 0")
    return value


def _non_negative_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if not isfinite(value) or value < 0:
        raise ConfigError(f"{name} must be a finite, non-negative number")
    return value
