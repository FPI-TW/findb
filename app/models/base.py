"""
SQLAlchemy base configuration and database connection.
"""

import logging
import os
from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, event, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

DB_INIT_COMMAND = "docker compose run --rm app uv run alembic upgrade head"
ALEMBIC_UPGRADE_COMMAND = "uv run alembic upgrade head"

# Naming convention for constraints
convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=convention)


class Base(DeclarativeBase):
    """Base class for all database models."""

    metadata = metadata


# WORKER_MODE=true is set by docker-compose / the Celery entrypoint command.
# Relying on sys.argv is fragile because base.py is imported at module load time,
# before Celery always populates argv correctly.
# NullPool is mandatory for workers: each task calls asyncio.run() which creates a
# new event loop. asyncpg connections are bound to the event loop they were created
# on, so a shared pool would accumulate un-closeable connections across event loops,
# exhausting PostgreSQL's max_connections.
_is_worker = os.environ.get("WORKER_MODE", "").lower() in ("1", "true", "yes")

if _is_worker:
    engine_kwargs = {
        "echo": settings.DEBUG,
        "poolclass": NullPool,  # one connection per asyncio.run() call, closed on exit
    }
else:
    engine_kwargs = {
        "echo": settings.DEBUG,
        "pool_size": settings.DATABASE_POOL_SIZE,
        "max_overflow": settings.DATABASE_MAX_OVERFLOW,
        "pool_pre_ping": True,  # detect stale connections before checkout
        "pool_recycle": 1800,  # recycle connections idle for 30 min (prevents PG timeout)
    }


# Create async engine
engine = create_async_engine(settings.DATABASE_URL, **engine_kwargs)

# Session factory
async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db():
    """Validate database migration state before serving traffic."""
    expected_heads = _expected_alembic_heads()
    async with engine.connect() as conn:
        current_heads = await conn.run_sync(_current_db_heads)

    if not current_heads:
        message = (
            "Database is not initialized: no Alembic revision stamp was found. "
            f"Run `{ALEMBIC_UPGRADE_COMMAND}` before starting the app. "
            f"For local Docker, run `{DB_INIT_COMMAND}`."
        )
        logger.warning(message)
        raise RuntimeError(message)

    async with engine.connect() as conn:
        await _verify_required_objects(conn)

    if set(current_heads) != set(expected_heads):
        message = (
            "Database schema version mismatch: "
            f"current={','.join(current_heads)} expected={','.join(expected_heads)}. "
            f"Run `{ALEMBIC_UPGRADE_COMMAND}`."
        )
        logger.warning(message)
        raise RuntimeError(message)


def _expected_alembic_heads() -> tuple[str, ...]:
    project_root = Path(__file__).resolve().parents[2]
    alembic_config = AlembicConfig(str(project_root / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(project_root / "migrations"))
    script_dir = ScriptDirectory.from_config(alembic_config)
    heads = tuple(script_dir.get_heads())
    if not heads:
        raise RuntimeError("Alembic head revision is not configured.")
    return heads


def _current_db_heads(connection: Connection) -> tuple[str, ...]:
    context = MigrationContext.configure(connection)
    return tuple(context.get_current_heads())


async def _verify_required_objects(conn) -> None:
    required_objects = ("public.dataset_registry", "public.instruments", "raw.market_payload")
    for relation in required_objects:
        exists = await conn.scalar(
            text("SELECT to_regclass(:relation) IS NOT NULL"), {"relation": relation}
        )
        if not exists:
            message = (
                f"Missing required relation '{relation}'. "
                "Database schema is incomplete. "
                f"Ensure bootstrap migrations are applied with `{ALEMBIC_UPGRADE_COMMAND}`. "
                f"For local Docker, run `{DB_INIT_COMMAND}`."
            )
            logger.warning(message)
            raise RuntimeError(message)


async def get_session() -> AsyncSession:
    """Get a new database session."""
    async with async_session_maker() as session:
        return session


@event.listens_for(engine.sync_engine, "connect")
def receive_connect(dbapi_connection, connection_record):
    """確保連線在 fork 後是安全的"""
    pass  #
