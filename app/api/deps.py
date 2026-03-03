"""
API dependencies including authentication.
"""

import ipaddress
from threading import Lock
from time import time

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader
from app.config import get_settings

settings = get_settings()

# API Key header security
api_key_header = APIKeyHeader(name=settings.API_KEY_HEADER, auto_error=False)
_source_rate_limit_lock = Lock()
_source_rate_limit_buckets: dict[str, list[float]] = {}


def get_source_api_keys() -> list[str]:
    """Get list of valid source API keys."""
    if not settings.SOURCE_API_KEYS:
        return []
    return [key.strip() for key in settings.SOURCE_API_KEYS.split(",") if key.strip()]


def get_serve_api_keys() -> list[str]:
    """Get list of valid serve API keys."""
    if not settings.SERVE_API_KEYS:
        return []
    return [key.strip() for key in settings.SERVE_API_KEYS.split(",") if key.strip()]


def get_admin_api_keys() -> list[str]:
    """Get list of valid admin API keys."""
    if not settings.ADMIN_API_KEYS:
        return []
    return [key.strip() for key in settings.ADMIN_API_KEYS.split(",") if key.strip()]


def get_source_allowlist_networks() -> list[ipaddress._BaseNetwork]:
    if not settings.SOURCE_ALLOWLIST_CIDRS:
        return []

    networks: list[ipaddress._BaseNetwork] = []
    for raw_value in settings.SOURCE_ALLOWLIST_CIDRS.split(","):
        value = raw_value.strip()
        if not value:
            continue
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Invalid SOURCE_ALLOWLIST_CIDRS entry: {value}",
            ) from exc
        networks.append(network)
    return networks


def _extract_client_ip(request: Request) -> str | None:
    if settings.SOURCE_TRUST_PROXY_HEADERS:
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            candidate = forwarded_for.split(",")[0].strip()
            if candidate:
                return candidate

    if request.client:
        return request.client.host
    return None


def _enforce_source_allowlist(request: Request) -> str:
    client_ip = _extract_client_ip(request)
    if not client_ip:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Client IP unavailable",
        )

    networks = get_source_allowlist_networks()
    if not networks:
        if not settings.DEBUG:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="SOURCE_ALLOWLIST_CIDRS must be configured when DEBUG is false",
            )
        return client_ip

    try:
        parsed_ip = ipaddress.ip_address(client_ip)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid client IP address",
        ) from exc

    if not any(parsed_ip in network for network in networks):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Client IP not allowlisted",
        )

    return client_ip


def _enforce_source_rate_limit(api_key: str, client_ip: str) -> None:
    limit = max(1, int(settings.RATE_LIMIT_REQUESTS))
    window_seconds = max(1, int(settings.RATE_LIMIT_WINDOW))
    bucket_key = f"{api_key}:{client_ip}"
    now = time()
    threshold = now - window_seconds

    with _source_rate_limit_lock:
        bucket = _source_rate_limit_buckets.get(bucket_key, [])
        bucket = [ts for ts in bucket if ts > threshold]

        if len(bucket) >= limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded",
            )

        bucket.append(now)
        _source_rate_limit_buckets[bucket_key] = bucket


def reset_source_rate_limit_state() -> None:
    with _source_rate_limit_lock:
        _source_rate_limit_buckets.clear()


async def verify_source_api_key(
    request: Request,
    api_key: str = Security(api_key_header),
) -> str:
    """Verify API key for Source API endpoints."""
    valid_keys = get_source_api_keys()

    if not valid_keys:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No API keys configured",
        )

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key",
        )

    if api_key not in valid_keys:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key",
        )

    client_ip = _enforce_source_allowlist(request)
    _enforce_source_rate_limit(api_key, client_ip)

    return api_key


async def verify_admin_api_key(api_key: str = Security(api_key_header)) -> str:
    """Verify API key for Admin API endpoints. Always required — no bypass."""
    valid_keys = get_admin_api_keys()

    if not valid_keys:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No admin API keys configured",
        )

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key",
        )

    if api_key not in valid_keys:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key",
        )

    return api_key


async def verify_serve_api_key(api_key: str = Security(api_key_header)) -> str | None:
    """Verify API key for Serve API endpoints (optional based on config)."""
    if not settings.SERVE_REQUIRE_AUTH:
        return None

    valid_keys = get_serve_api_keys()

    if not valid_keys:
        # If auth is required but no keys configured, deny access
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication required but no API keys configured",
        )

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key",
        )

    if api_key not in valid_keys:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key",
        )

    return api_key
