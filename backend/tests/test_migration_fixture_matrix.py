"""CI matrix for supported historical database revisions."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import create_async_engine

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "migration_revisions.json"
FIXTURES = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"
BASE_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://findb:findb@localhost:5435/findb_test",
)


def _migration_scripts() -> ScriptDirectory:
    config = Config(str(REPO_ROOT / "backend" / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "backend" / "migrations"))
    return ScriptDirectory.from_config(config)


async def _run_alembic(database_url: str, revision: str) -> None:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=BACKEND_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "fixture",
    FIXTURES["supported_revisions"],
    ids=lambda item: str(item["revision"]),
)
def test_supported_migration_fixture_is_versioned_and_covered(fixture: dict[str, str]) -> None:
    assert FIXTURES["schema_version"] == 1
    assert len(fixture["revision"]) == 12
    assert fixture["revision"].isalnum()
    assert _migration_scripts().get_revision(fixture["revision"]) is not None
    coverage = REPO_ROOT / fixture["coverage"]
    assert coverage.is_file(), fixture


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fixture",
    FIXTURES["supported_revisions"],
    ids=lambda item: f"upgrade-{item['revision']}",
)
async def test_supported_migration_fixture_upgrades_to_current_head(
    fixture: dict[str, str],
) -> None:
    base_url = make_url(BASE_DATABASE_URL)
    database_name = f"findb_fixture_{uuid4().hex[:12]}"
    database_url = base_url.set(database=database_name).render_as_string(hide_password=False)
    admin_engine = create_async_engine(
        base_url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
    )
    database_created = False
    target_engine = None

    try:
        async with admin_engine.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        database_created = True

        await _run_alembic(database_url, fixture["revision"])
        await _run_alembic(database_url, "head")

        target_engine = create_async_engine(database_url)
        async with target_engine.connect() as connection:
            current_revision = await connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
        assert current_revision == _migration_scripts().get_current_head()
    finally:
        if target_engine is not None:
            await target_engine.dispose()
        if database_created:
            async with admin_engine.connect() as connection:
                await connection.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) "
                        "FROM pg_stat_activity WHERE datname = :name"
                    ),
                    {"name": database_name},
                )
                await connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
        await admin_engine.dispose()


def test_supported_migration_fixture_revisions_are_unique() -> None:
    revisions = [fixture["revision"] for fixture in FIXTURES["supported_revisions"]]
    assert len(revisions) == len(set(revisions))
