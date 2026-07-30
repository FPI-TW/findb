"""API dependencies including credential authentication and Admin RBAC."""

from dataclasses import dataclass
from hashlib import sha256
from hmac import compare_digest
from threading import Lock
from time import time

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.dependencies import get_db
from app.models.registry import APIKey
from app.services.admin_identity import find_active_session
from app.services.api_keys import find_active_api_key, has_active_admin_keys, has_active_api_keys
from app.services.security_events import record_invalid_credential
from app.services.source_clients import find_active_source_client

settings = get_settings()

# API Key header security
api_key_header = APIKeyHeader(name=settings.API_KEY_HEADER, auto_error=False)
bearer_scheme = HTTPBearer(auto_error=False)
_source_rate_limit_lock = Lock()
_source_rate_limit_buckets: dict[str, list[float]] = {}
_serve_rate_limit_lock = Lock()
_serve_rate_limit_buckets: dict[str, list[float]] = {}
_admin_login_rate_limit_buckets: dict[str, list[float]] = {}
DEFAULT_SERVE_PAGE_SIZE = 100
ROLE_LEVEL = {"viewer": 1, "operator": 2, "owner": 3}


@dataclass(frozen=True)
class AdminPrincipal:
    actor_type: str
    actor_id: str | None
    display_name: str
    role: str
    session_id: str | None = None
    must_change_password: bool = False

    def __str__(self) -> str:
        return f"{self.actor_type}:{self.actor_id or self.display_name}:{self.display_name}"


def get_admin_break_glass_api_key() -> str:
    return settings.ADMIN_BREAK_GLASS_API_KEY.strip()


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


def _enforce_source_rate_limit(
    bucket_identity: str,
    client_ip: str,
    *,
    limit: int | None = None,
    window_seconds: int | None = None,
) -> None:
    limit = max(1, int(limit or settings.RATE_LIMIT_REQUESTS))
    window_seconds = max(1, int(window_seconds or settings.RATE_LIMIT_WINDOW))
    bucket_key = f"{bucket_identity}:{client_ip}"
    now = time()
    threshold = now - window_seconds

    with _source_rate_limit_lock:
        bucket = _source_rate_limit_buckets.get(bucket_key, [])
        bucket = [ts for ts in bucket if ts > threshold]

        if len(bucket) >= limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded",
                headers={"Retry-After": str(window_seconds)},
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
    with _source_rate_limit_lock:
        _admin_login_rate_limit_buckets.clear()


def enforce_admin_login_rate_limit(request: Request, username: str) -> None:
    now = time()
    window = settings.ADMIN_LOGIN_RATE_LIMIT_WINDOW
    key = f"{_extract_client_ip(request) or 'unknown'}:{username.strip().lower()}"
    with _source_rate_limit_lock:
        bucket = [t for t in _admin_login_rate_limit_buckets.get(key, []) if t > now - window]
        if len(bucket) >= settings.ADMIN_LOGIN_RATE_LIMIT_REQUESTS:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many login attempts",
                headers={"Retry-After": str(window)},
            )
        bucket.append(now)
        _admin_login_rate_limit_buckets[key] = bucket


async def verify_source_api_key(
    request: Request,
    api_key: str = Security(api_key_header),
    db: AsyncSession = Depends(get_db),
) -> str:
    """Verify API key for Source API endpoints."""
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key",
        )

    try:
        source_client = await find_active_source_client(db, api_key)
    except DBAPIError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ingestion is temporarily unavailable",
            headers={"Retry-After": "30"},
        )
    if source_client is None:
        record_invalid_credential(
            "source",
            endpoint=request.url.path,
            client_ip=_extract_client_ip(request),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key",
        )

    client_ip = _get_source_client_ip(request)
    if source_client is not None:
        request.state.credential_ref = ("source", source_client.client_id)
        db.info["source_client_id"] = source_client.client_id
        db.info["source_name"] = source_client.source_name
        db.info["allowed_datasets"] = source_client.allowed_datasets
        _enforce_source_rate_limit(
            str(source_client.client_id),
            client_ip,
            limit=source_client.rate_limit_requests,
            window_seconds=source_client.rate_limit_window,
        )
    return api_key


async def verify_admin_api_key(
    request: Request,
    api_key: str | None = Security(api_key_header),
    bearer: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> AdminPrincipal:
    """Resolve exactly one Admin credential into a named principal."""
    if api_key and bearer:
        raise HTTPException(status_code=400, detail="Provide exactly one admin credential")
    if bearer:
        pair = await find_active_session(db, bearer.credentials)
        if pair is None:
            raise HTTPException(status_code=403, detail="Invalid admin credential")
        session, user = pair
        request.state.credential_ref = None
        password_allowed_paths = {
            f"{settings.API_V1_PREFIX}/admin/auth/me",
            f"{settings.API_V1_PREFIX}/admin/auth/logout",
            f"{settings.API_V1_PREFIX}/admin/auth/change-password",
        }
        if user.must_change_password and request.url.path not in password_allowed_paths:
            raise HTTPException(status_code=403, detail="Password change required")
        return AdminPrincipal(
            "user",
            str(user.user_id),
            user.display_name,
            user.role,
            str(session.session_id),
            user.must_change_password,
        )
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing admin credential")

    break_glass = get_admin_break_glass_api_key()
    if break_glass and compare_digest(api_key, break_glass):
        return AdminPrincipal(
            "break_glass",
            sha256(api_key.encode("utf-8")).hexdigest()[:12],
            "break-glass",
            "owner",
        )
    db_key = await find_active_api_key(db, api_key)
    if db_key is not None and db_key.kind == "admin":
        request.state.credential_ref = ("admin", db_key.key_id)
        return AdminPrincipal(
            "machine", str(db_key.key_id), db_key.name or db_key.owner, db_key.role or "viewer"
        )
    if not break_glass and not await has_active_admin_keys(db):
        raise HTTPException(status_code=500, detail="No admin credential configured")
    record_invalid_credential(
        "admin",
        endpoint=request.url.path,
        client_ip=_extract_client_ip(request),
    )
    raise HTTPException(status_code=403, detail="Invalid admin credential")


def require_admin_role(minimum_role: str):
    async def dependency(
        principal: AdminPrincipal = Depends(verify_admin_api_key),
    ) -> AdminPrincipal:
        if principal.must_change_password:
            raise HTTPException(status_code=403, detail="Password change required")
        if ROLE_LEVEL.get(principal.role, 0) < ROLE_LEVEL[minimum_role]:
            raise HTTPException(status_code=403, detail="Insufficient admin role")
        return principal

    return dependency


require_viewer = require_admin_role("viewer")
require_operator = require_admin_role("operator")
require_owner = require_admin_role("owner")


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
    else:
        requested_page_size = DEFAULT_SERVE_PAGE_SIZE

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
        if db_key.kind != "serve":
            record_invalid_credential(
                "serve",
                endpoint=request.url.path,
                client_ip=_extract_client_ip(request),
            )
            raise HTTPException(status_code=403, detail="Invalid API key")
        _enforce_serve_api_key_policy(request, db_key)
        request.state.credential_ref = ("serve", db_key.key_id)
        return str(db_key.key_id)

    record_invalid_credential(
        "serve",
        endpoint=request.url.path,
        client_ip=_extract_client_ip(request),
    )
    if not await has_active_api_keys(db):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Authentication required but no API keys configured",
        )
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid API key")


async def require_serve_api_key(
    request: Request,
    api_key: str = Security(api_key_header),
    db: AsyncSession = Depends(get_db),
) -> str:
    """Require an active DB-backed Serve key, regardless of optional Serve auth."""
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key",
        )
    db_key = await find_active_api_key(db, api_key)
    if db_key is None or db_key.kind != "serve":
        record_invalid_credential(
            "serve",
            endpoint=request.url.path,
            client_ip=_extract_client_ip(request),
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid API key")
    _enforce_serve_api_key_policy(request, db_key)
    request.state.credential_ref = ("serve", db_key.key_id)
    return str(db_key.key_id)
