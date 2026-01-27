"""
API dependencies including authentication.
"""

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader
from app.config import get_settings

settings = get_settings()

# API Key header security
api_key_header = APIKeyHeader(name=settings.API_KEY_HEADER, auto_error=False)


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


async def verify_source_api_key(api_key: str = Security(api_key_header)) -> str:
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
