"""Read-only classification using the exact frozen schedule migration inputs."""

import asyncio
import importlib.util
import json
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings


def migration_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations/versions/e9f4a1c6b3d5_catalog_schedule_projection.py"
    )
    spec = importlib.util.spec_from_file_location("catalog_schedule_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def main() -> int:
    engine = create_async_engine(get_settings().DATABASE_URL)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            # Empty bootstrap databases are populated by migrations and seed.
            exists = await connection.scalar(text("SELECT to_regclass('public.scheduler_control')"))
            if exists is None:
                print(json.dumps({"status": "empty_bootstrap"}))
                return 0
            report, _, _ = await connection.run_sync(migration_module().inspect)
            print(json.dumps({"revision": "e9f4a1c6b3d5", "rows": report}, sort_keys=True))
            return int(any(row["status"] == "unknown_difference" for row in report))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
