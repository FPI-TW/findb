"""The index membership history shape allows unknown starts and blocks removal once used."""

import subprocess

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.migration_database import get_active_migration_database_factory, run_alembic

BEFORE_SHAPE = "37ed18886cab"
SHA = "a" * 64
SHAPE = "ebb187773e30"


async def _shape(engine) -> dict[str, str]:
    async with engine.connect() as db:
        rows = await db.execute(
            text(
                "SELECT table_name || '.' || column_name, is_nullable FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name IN "
                "('index_definition', 'index_membership', 'index_membership_build') "
                "AND column_name IN ('series', 'valid_from', 'starts_at_history_start', "
                "'observed_from', 'findings')"
            )
        )
        return dict(rows.all())


@pytest.mark.asyncio
async def test_history_shape_allows_unknown_starts_and_blocks_removal_once_used():
    async with get_active_migration_database_factory().clone(
        "1587769858c0", "index_history_shape"
    ) as (url, engine):
        await run_alembic(url, SHAPE)
        async with engine.connect() as db:
            assert (
                await db.scalar(text("SELECT to_regclass('public.reference_delivery_evidence')"))
                is None
            )
        assert await _shape(engine) == {
            "index_definition.series": "NO",
            "index_membership.valid_from": "YES",
            "index_membership_build.observed_from": "YES",
            "index_membership_build.findings": "NO",
        }
        await run_alembic(url, BEFORE_SHAPE, command="downgrade")
        async with engine.connect() as db:
            assert (
                await db.scalar(text("SELECT to_regclass('public.reference_delivery_evidence')"))
                is not None
            )
        assert await _shape(engine) == {
            "index_membership.valid_from": "NO",
            "index_membership.starts_at_history_start": "NO",
        }
        await run_alembic(url, SHAPE)
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO index_definition (index_id, publisher, code, series, name_en, "
                    "created_at, updated_at) VALUES (gen_random_uuid(), 'hsil', '00001.00', 'hsi', "
                    "'Hang Seng Index', now(), now())"
                )
            )
            index = await db.scalar(text("SELECT index_id FROM index_definition"))
            build = await db.scalar(
                text(
                    "INSERT INTO index_membership_build (build_id, publisher, family, "
                    "anchor_trade_date, observed_from, findings, inputs_sha256, status, created_at) "
                    "VALUES (gen_random_uuid(), 'hsil', 'hsi', '2026-11-23', '2026-10-02', 0, :sha, "
                    "'consistent', now()) RETURNING build_id"
                ),
                {"sha": SHA},
            )
        membership = (
            "INSERT INTO index_membership (membership_id, index_id, symbol, build_id) "
            "VALUES (gen_random_uuid(), :index, '00005', :build)"
        )
        async with engine.begin() as db:
            await db.execute(text(membership), {"index": index, "build": build})
        # One membership before history per code: an unknown start is not distinct.
        with pytest.raises(IntegrityError):
            async with engine.begin() as db:
                await db.execute(text(membership), {"index": index, "build": build})
        with pytest.raises(subprocess.CalledProcessError) as blocked:
            await run_alembic(url, BEFORE_SHAPE, command="downgrade")
        assert "downgrade is blocked" in blocked.value.stderr
