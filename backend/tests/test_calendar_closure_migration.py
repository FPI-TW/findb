"""Expand-only closure schema and rollback floor preserve legacy work."""

import subprocess

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from tests.migration_database import get_active_migration_database_factory, run_alembic
from tests.test_shioaji_repair_migration import current_dataset_fixture

# Later revisions (prepared rows) reshape provider_work; isolate this expansion.
CLOSURE_REVISION = "3a80c2e1d5f4"


@pytest.mark.asyncio
async def test_calendar_closure_migration_preserves_originals_and_blocks_evidence_downgrade(
    monkeypatch,
):
    current_dataset_fixture(monkeypatch)
    async with get_active_migration_database_factory().clone(
        "3553ed95cfbf", "calendar_closure"
    ) as (url, engine):
        await run_alembic(url, "2f79b1d0c4e3")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            from tests.test_shioaji_repair import setup

            work, _, _, plan, _ = await setup(db, gap=False)
            wid, pid = work.work_id, plan.plan_id
        tables = [
            "provider_work",
            "provider_work_attempt",
            "daily_delivery_plan",
            "daily_delivery_part",
            "daily_delivery_member",
        ]

        async def snapshot():
            async with engine.connect() as db:
                return {
                    t: list(
                        (
                            await db.execute(text(f"SELECT to_jsonb(t) FROM {t} t ORDER BY 1"))
                        ).scalars()
                    )
                    for t in tables
                }

        before = await snapshot()
        await run_alembic(url, CLOSURE_REVISION)
        assert await snapshot() == before
        await run_alembic(url, "2f79b1d0c4e3", command="downgrade")
        assert await snapshot() == before
        await run_alembic(url, CLOSURE_REVISION)
        async with engine.begin() as db:
            from app.utils import uuid7

            await db.execute(
                text(
                    "INSERT INTO provider_work_calendar_closure(work_id,closure_id,plan_id,request_id,request_sha256,environment,execution_profile,actor,reason,evidence,created_at) VALUES(:wid,:cid,:pid,'reviewed',:sha,'local','full_market','owner','retained calendar closure','{}',now())"
                ),
                {"wid": wid, "cid": uuid7(), "pid": pid, "sha": "a" * 64},
            )
        with pytest.raises(subprocess.CalledProcessError) as blocked:
            await run_alembic(url, "2f79b1d0c4e3", command="downgrade")
        assert "Calendar closure evidence must be retained" in blocked.value.stderr
        assert await snapshot() == before
