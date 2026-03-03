"""
Application configuration management.
"""

from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Application
    APP_NAME: str = "FinDB"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://findb:findb@localhost:5435/findb"
    DATABASE_POOL_SIZE: int = 5
    DATABASE_MAX_OVERFLOW: int = 10

    # API
    API_V1_PREFIX: str = "/api/v1"
    API_KEY_HEADER: str = "X-API-Key"

    # Source API Keys (comma-separated)
    SOURCE_API_KEYS: str = ""
    SOURCE_ALLOWLIST_CIDRS: str = ""
    SOURCE_TRUST_PROXY_HEADERS: bool = False

    # Serve API Keys (comma-separated, optional)
    SERVE_API_KEYS: str = ""
    SERVE_REQUIRE_AUTH: bool = False

    # Admin API Keys (comma-separated)
    ADMIN_API_KEYS: str = ""

    # Rate Limiting
    RATE_LIMIT_REQUESTS: int = 100
    RATE_LIMIT_WINDOW: int = 60  # seconds

    # Raw Data Retention
    RAW_RETENTION_DAYS: int = 14

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True)


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
