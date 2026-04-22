"""
SQLAlchemy base configuration and database connection.
"""

from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()

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


# Create async engine
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_size=settings.DATABASE_POOL_SIZE,
    max_overflow=settings.DATABASE_MAX_OVERFLOW,
)

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
        await _verify_required_objects(conn)

    if not current_heads:
        raise RuntimeError(
            "Database has no Alembic revision stamp. Run `uv run alembic upgrade head` (or stamp baseline first for existing schema)."
        )

    if set(current_heads) != set(expected_heads):
        raise RuntimeError(
            "Database schema version mismatch: "
            f"current={','.join(current_heads)} expected={','.join(expected_heads)}. "
            "Run `uv run alembic upgrade head`."
        )


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
            raise RuntimeError(
                f"Missing required relation '{relation}'. "
                "Ensure bootstrap migrations are applied with `uv run alembic upgrade head`."
            )


async def get_session() -> AsyncSession:
    """Get a new database session."""
    async with async_session_maker() as session:
        return session
