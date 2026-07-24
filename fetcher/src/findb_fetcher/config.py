"""Runtime configuration limited to Fetcher-owned concerns."""

from __future__ import annotations

import os
from dataclasses import dataclass
from math import isfinite
from pathlib import Path


class ConfigError(ValueError):
    """Fetcher environment configuration is missing or invalid."""


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
        source_api_url = _required_env("SOURCE_API_URL").rstrip("/")
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


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required")
    return value


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
