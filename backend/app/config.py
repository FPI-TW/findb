"""
Application configuration management.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Application
    APP_NAME: str = "FinDB"
    APP_VERSION: str = "0.1.0"
    APP_ROLE: str = "all"
    DEBUG: bool = False
    PORT: int = 8080

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://findb:findb@localhost:5435/findb"
    FINDB_REMOTE_DATABASE_URL: str = "postgresql+asyncpg://findb:findb@localhost:5435/findb"
    DATABASE_POOL_SIZE: int = 5
    DATABASE_MAX_OVERFLOW: int = 10

    # API
    API_V1_PREFIX: str = "/api/v1"
    API_KEY_HEADER: str = "X-API-Key"

    # Source API clients are DB-backed; no shared runtime credential exists.
    SOURCE_TRUST_PROXY_HEADERS: bool = False
    SOURCE_MAX_PAYLOAD_BYTES: int = 1_000_000
    SOURCE_MAX_DATA_ITEMS: int = 5_000

    # Durable normalization queue
    CELERY_BROKER_URL: str = "amqp://findb:findb@localhost:5672/%2Ffindb"
    NORMALIZATION_EXCHANGE: str = "findb.ingestion.v1"
    NORMALIZATION_QUEUE: str = "findb.normalize.v1"
    NORMALIZATION_DLQ: str = "findb.normalize.dlq.v1"
    NORMALIZATION_MAX_ATTEMPTS: int = 5
    NORMALIZATION_RETRY_BASE_SECONDS: int = 30
    NORMALIZATION_RETRY_MAX_SECONDS: int = 900
    NORMALIZATION_TASK_SOFT_TIME_LIMIT: int = 1_800
    NORMALIZATION_TASK_TIME_LIMIT: int = 2_100
    NORMALIZATION_LEASE_SECONDS: int = 2_400
    NORMALIZATION_CONSUMER_TIMEOUT_MS: int = 3_600_000
    OUTBOX_POLL_SECONDS: float = 1.0
    OUTBOX_BATCH_SIZE: int = 100
    OUTBOX_CLAIM_SECONDS: int = 60
    OUTBOX_RECONCILE_SECONDS: int = 30
    INGESTION_ATTEMPT_STALE_SECONDS: int = Field(default=300, gt=0)
    INGESTION_ATTEMPT_RECONCILE_BATCH_SIZE: int = Field(default=100, gt=0, le=1_000)
    DELIVERY_MONITOR_SECONDS: float = Field(default=60, gt=0)
    DELIVERY_MONITOR_TIMEOUT_SECONDS: float = Field(default=30, gt=0)

    # Serve API access is provided only by DB-backed keys when enabled.
    SERVE_REQUIRE_AUTH: bool = False

    # Admin machine access is DB-backed. This key is bootstrap/recovery only.
    ADMIN_BREAK_GLASS_API_KEY: str = ""
    ADMIN_SESSION_HOURS: int = Field(default=8, ge=1, le=168)
    ADMIN_LOGIN_RATE_LIMIT_REQUESTS: int = Field(default=5, ge=1)
    ADMIN_LOGIN_RATE_LIMIT_WINDOW: int = Field(default=300, ge=1)
    CREDENTIAL_USAGE_FLUSH_SECONDS: int = Field(default=60, ge=1)

    # Rate Limiting
    RATE_LIMIT_REQUESTS: int = 100
    RATE_LIMIT_WINDOW: int = 60  # seconds

    # Raw Data Retention
    RAW_RETENTION_ENABLED: bool = True
    RAW_RETENTION_DAYS: int = 30

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        case_sensitive=True,
        extra="ignore",
    )


@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
