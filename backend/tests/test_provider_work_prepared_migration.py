"""Prepared JSONB objects move to rows and back without changing a byte or order."""

import json

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.services import provider_work as queue
from app.utils import uuid7
from tests.migration_database import get_active_migration_database_factory, run_alembic

PREVIOUS = "3a80c2e1d5f4"


def _row(body, members, job_key, confirmed=True):
    return {
        "body": body,
        "body_sha256": "a" * 64,
        "payload_sha256": "b" * 64,
        "canonical_members": members,
        "job_key": job_key,
        "confirmed": confirmed,
    }


@pytest.mark.asyncio
async def test_prepared_rows_round_trip_original_objects_and_key_order():
    retained = {
        # JSONB orders keys by length then bytes; rows must preserve that order.
        "zz": _row('{"z":"\\u00e9"}', ["2330"], "job-z"),
        "a": _row("a-body " * 4096, ["2317", "2454"], None, confirmed=False),
        "bbb": _row("b-body", [], "job-b"),
        "descriptor-only": {"canonical_members": ["0050"], "body_sha256": "c" * 64},
    }
    async with get_active_migration_database_factory().clone("3553ed95cfbf", "prepared_rows") as (
        url,
        engine,
    ):
        await run_alembic(url, PREVIOUS)
        work_ids = [uuid7(), uuid7()]
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO dataset_registry (dataset_key, name, asset_class, market, "
                    "frequency, is_active, config, created_at, updated_at) VALUES "
                    "('prepared_rows', 'Prepared', 'equity', 'TW', 'daily', true, '{}', "
                    "now(), now())"
                )
            )
            for number, (work_id, prepared) in enumerate(zip(work_ids, [retained, {}])):
                await db.execute(
                    text(
                        "INSERT INTO provider_work (work_id, environment, provider, stable_key, "
                        "kind, dataset_key, scope, state, created_at, prepared_deliveries) "
                        "VALUES (:id, 'local', 'shioaji', :key, 'daily', 'prepared_rows', '{}', "
                        "'queued', now(), CAST(:prepared AS jsonb))"
                    ),
                    {"id": work_id, "key": f"prepared-{number}", "prepared": json.dumps(prepared)},
                )

        async def originals():
            async with engine.connect() as db:
                return {
                    row.work_id: (row.value, list(row.keys))
                    for row in await db.execute(
                        text(
                            "SELECT work_id, to_jsonb(t) AS value, ARRAY(SELECT jsonb_object_keys("
                            "t.prepared_deliveries)) AS keys FROM provider_work t"
                        )
                    )
                }

        before = await originals()
        assert before[work_ids[0]][1] == ["a", "zz", "bbb", "descriptor-only"]
        await run_alembic(url, "head")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            assert await queue.prepared_keys(db, work_ids[0]) == before[work_ids[0]][1]
            assert await queue.prepared_map(db, work_ids[0]) == retained
            assert await queue.prepared_map(db, work_ids[1]) == {}
            assert not await queue.has_prepared(db, work_ids[1])
            assert await queue.prepared_bodies(db, work_ids[0], member_key="2454") == [
                retained["a"]["body"]
            ]
            stored = await db.scalar(
                text("SELECT descriptor FROM provider_work_prepared WHERE delivery_key='zz'")
            )
            assert "body" not in stored
        await run_alembic(url, PREVIOUS, command="downgrade")
        assert await originals() == before


@pytest.mark.asyncio
async def test_prepared_rows_upgrade_fails_closed_when_an_object_cannot_round_trip():
    import subprocess

    async with get_active_migration_database_factory().clone(
        "3553ed95cfbf", "prepared_rows_closed"
    ) as (url, engine):
        await run_alembic(url, PREVIOUS)
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO dataset_registry (dataset_key, name, asset_class, market, "
                    "frequency, is_active, config, created_at, updated_at) VALUES "
                    "('prepared_rows', 'Prepared', 'equity', 'TW', 'daily', true, '{}', "
                    "now(), now())"
                )
            )
            # A JSON null body has no text column equivalent; never drop it silently.
            await db.execute(
                text(
                    "INSERT INTO provider_work (work_id, environment, provider, stable_key, "
                    "kind, dataset_key, scope, state, created_at, prepared_deliveries) "
                    "VALUES (:id, 'local', 'shioaji', 'null-body', 'daily', 'prepared_rows', "
                    """'{}', 'queued', now(), '{"k": {"body": null}}')"""
                ),
                {"id": uuid7()},
            )
        with pytest.raises(subprocess.CalledProcessError) as failed:
            await run_alembic(url, "head")
        assert "did not round-trip" in failed.value.stderr
        async with engine.connect() as db:
            assert await db.scalar(text("SELECT version_num FROM alembic_version")) == PREVIOUS
            assert await db.scalar(text("SELECT to_regclass('provider_work_prepared')")) is None
