"""API dependencies including authentication."""

from hmac import compare_digest
from threading import Lock
from time import time

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.dependencies import get_db
from app.models.registry import APIKey
from app.services.api_keys import find_active_api_key, has_active_api_keys

settings = get_settings()

# API Key header security
api_key_header = APIKeyHeader(name=settings.API_KEY_HEADER, auto_error=False)
_source_rate_limit_lock = Lock()
_source_rate_limit_buckets: dict[str, list[float]] = {}
_serve_rate_limit_lock = Lock()
_serve_rate_limit_buckets: dict[str, list[float]] = {}


def get_source_api_key() -> str:
    """Get the valid source API key."""
    return settings.SOURCE_API_KEY.strip()


def get_serve_api_keys() -> list[str]:
    """Get list of valid serve API keys."""
    if not settings.SERVE_API_KEYS:
        return []
    return [key.strip() for key in settings.SERVE_API_KEYS.split(",") if key.strip()]


def get_admin_api_key() -> str:
    """Get the valid admin API key."""
    return settings.ADMIN_API_KEY.strip()


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


def _get_source_client_ip(request: Request) -> str:
    client_ip = _extract_client_ip(request)
    if not client_ip:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Client IP unavailable",
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


def _enforce_serve_rate_limit(key: APIKey, client_ip: str) -> None:
    limit = max(1, int(key.rate_limit_requests))
    window_seconds = max(1, int(key.rate_limit_window))
    bucket_key = f"{key.key_id}:{client_ip}"
    now = time()
    threshold = now - window_seconds

    with _serve_rate_limit_lock:
        bucket = _serve_rate_limit_buckets.get(bucket_key, [])
        bucket = [ts for ts in bucket if ts > threshold]

        if len(bucket) >= limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded",
            )

        bucket.append(now)
        _serve_rate_limit_buckets[bucket_key] = bucket


def reset_source_rate_limit_state() -> None:
    with _source_rate_limit_lock:
        _source_rate_limit_buckets.clear()
    with _serve_rate_limit_lock:
        _serve_rate_limit_buckets.clear()


async def verify_source_api_key(
    request: Request,
    api_key: str = Security(api_key_header),
) -> str:
    """Verify API key for Source API endpoints."""
    valid_key = get_source_api_key()

    if not valid_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No API key configured",
        )

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key",
        )

    if not compare_digest(api_key, valid_key):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key",
        )

    client_ip = _get_source_client_ip(request)
    _enforce_source_rate_limit(api_key, client_ip)

    return api_key


async def verify_admin_api_key(api_key: str = Security(api_key_header)) -> str:
    """Verify API key for Admin API endpoints. Always required — no bypass."""
    valid_key = get_admin_api_key()

    if not valid_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="No admin API key configured",
        )

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key",
        )

    if not compare_digest(api_key, valid_key):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key",
        )

    return api_key


def _enforce_serve_api_key_policy(request: Request, key: APIKey) -> None:
    scopes = key.scopes or []
    if "serve" not in scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key does not allow Serve API access",
        )

    page_size = request.query_params.get("page_size")
    if page_size:
        try:
            requested_page_size = int(page_size)
        except ValueError:
            requested_page_size = 0
        if requested_page_size > key.page_size_limit:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"page_size exceeds API key limit of {key.page_size_limit}",
            )

    client_ip = _extract_client_ip(request) or "unknown"
    _enforce_serve_rate_limit(key, client_ip)


async def verify_serve_api_key(
    request: Request,
    api_key: str = Security(api_key_header),
    db: AsyncSession = Depends(get_db),
) -> str | None:
    """Verify API key for Serve API endpoints (optional based on config)."""
    if not settings.SERVE_REQUIRE_AUTH:
        return None

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key",
        )

    db_key = await find_active_api_key(db, api_key)
    if db_key is not None:
        _enforce_serve_api_key_policy(request, db_key)
        return str(db_key.key_id)

    valid_keys = get_serve_api_keys()
    if not valid_keys:
        if not await has_active_api_keys(db):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Authentication required but no API keys configured",
            )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key",
        )

    if not any(compare_digest(api_key, valid_key) for valid_key in valid_keys):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key",
        )

    return api_key
