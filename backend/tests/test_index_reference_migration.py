"""Index reference storage is additive; archived documents block its removal."""

import subprocess

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.migration_database import get_active_migration_database_factory, run_alembic

BEFORE = "b6d8f0a2c4e7"
REVISION = "2bbe07b98293"
BEFORE_SHAPE = "37ed18886cab"
SHA = "a" * 64
TABLES = (
    "reference_document_archive",
    "reference_delivery_evidence",
    "index_definition",
    "index_change_event",
    "index_snapshot",
    "index_snapshot_constituent",
    "index_membership_build",
    "index_membership",
)


async def _tables(engine) -> set[str]:
    async with engine.connect() as db:
        found = set()
        for table in TABLES:
            if await db.scalar(text(f"SELECT to_regclass('public.{table}')")) is not None:
                found.add(table)
        return found


async def _archive(db) -> object:
    return await db.scalar(
        text(
            "INSERT INTO reference_document_archive (archive_id, source, publisher, kind, series, "
            "document_date, gzip_sha256, gzip_bytes, original_sha256, original_bytes, source_url, "
            "object_key, object_etag, created_at) VALUES (gen_random_uuid(), 'hkex', 'hsil', "
            "'constituent_report', 'hsi', '2026-11-23', :sha, 1, :sha, 1, "
            "'https://www.hsi.com.hk/report.pdf', 'archive/v1/hsil/report.pdf.gz', '\"etag\"', "
            "now()) RETURNING archive_id"
        ),
        {"sha": SHA},
    )


@pytest.mark.asyncio
async def test_tables_are_removed_only_while_no_document_is_archived():
    async with get_active_migration_database_factory().clone("3553ed95cfbf", "index_reference") as (
        url,
        engine,
    ):
        await run_alembic(url, REVISION)
        assert await _tables(engine) == set(TABLES)
        await run_alembic(url, BEFORE, command="downgrade")
        assert await _tables(engine) == set()

        await run_alembic(url, REVISION)
        async with engine.begin() as db:
            await _archive(db)
        with pytest.raises(subprocess.CalledProcessError) as blocked:
            await run_alembic(url, BEFORE, command="downgrade")
        assert "downgrade is blocked" in blocked.value.stderr
        assert await _tables(engine) == set(TABLES)


@pytest.mark.asyncio
async def test_constraints_keep_membership_intervals_and_definitions_coherent():
    async with get_active_migration_database_factory().clone(
        "3553ed95cfbf", "index_reference_checks"
    ) as (url, engine):
        await run_alembic(url, REVISION)
        async with engine.begin() as db:
            archive = await _archive(db)
            index = await db.scalar(
                text(
                    "INSERT INTO index_definition (index_id, publisher, code, name_en, created_at, "
                    "updated_at) VALUES (gen_random_uuid(), 'hsil', '00001.00', 'Hang Seng Index', "
                    "now(), now()) RETURNING index_id"
                )
            )
            build = await db.scalar(
                text(
                    "INSERT INTO index_membership_build (build_id, publisher, family, "
                    "anchor_trade_date, inputs_sha256, status, created_at) VALUES "
                    "(gen_random_uuid(), 'hsil', 'hsi', '2026-11-23', :sha, 'consistent', now()) "
                    "RETURNING build_id"
                ),
                {"sha": SHA},
            )
        rejected = {
            "membership ends before it starts": (
                "INSERT INTO index_membership (membership_id, index_id, symbol, valid_from, "
                "valid_to, starts_at_history_start, build_id) VALUES (gen_random_uuid(), :index, "
                "'00005', '2026-11-23', '2026-11-23', false, :build)"
            ),
            "consistent build without anchor": (
                "INSERT INTO index_membership_build (build_id, publisher, family, inputs_sha256, "
                "status, created_at) VALUES (gen_random_uuid(), 'hsil', 'hsci', :sha, "
                "'consistent', now())"
            ),
            "classification without its scheme": (
                "INSERT INTO index_definition (index_id, publisher, code, name_en, "
                "classification_code, created_at, updated_at) VALUES (gen_random_uuid(), 'hsil', "
                "'00011.07', 'Utilities', '40', now(), now())"
            ),
            "symbol is not an exchange code": (
                "INSERT INTO index_change_event (event_id, index_id, effective_date, action, "
                "symbol, listing_place, constituents_after, source_ordinal, status, archive_id, "
                "created_at, updated_at) VALUES (gen_random_uuid(), :index, '2026-10-23', "
                "'delete', '2555', 'Hong Kong', 580, 1, 'effective', :archive, now(), now())"
            ),
        }
        for reason, statement in rejected.items():
            with pytest.raises(IntegrityError):
                async with engine.begin() as db:
                    await db.execute(
                        text(statement),
                        {"index": index, "build": build, "archive": archive, "sha": SHA},
                    )
            assert reason


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
        "3553ed95cfbf", "index_history_shape"
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
