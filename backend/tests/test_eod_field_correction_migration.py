"""Field corrections are additive; recorded corrections block their removal."""

import subprocess

import pytest
from sqlalchemy import text

from tests.migration_database import get_active_migration_database_factory, run_alembic

BEFORE = "a4c6e8f0b2d3"
REVISION = "b7d9f1a3c5e8"
SHA = "a" * 64


async def _shape(engine) -> tuple[bool, bool]:
    async with engine.connect() as db:
        column = await db.scalar(
            text(
                "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' "
                "AND table_name = 'market_data_eod' AND column_name = 'field_sources'"
            )
        )
        table = await db.scalar(text("SELECT to_regclass('public.eod_field_correction')"))
        return bool(column), table is not None


@pytest.mark.asyncio
async def test_column_and_table_are_removed_only_while_no_correction_is_recorded():
    async with get_active_migration_database_factory().clone(
        "3553ed95cfbf", "eod_field_correction"
    ) as (url, engine):
        await run_alembic(url, REVISION)
        assert await _shape(engine) == (True, True)
        await run_alembic(url, BEFORE, command="downgrade")
        assert await _shape(engine) == (False, False)

        await run_alembic(url, REVISION)
        async with engine.begin() as db:
            instrument = await db.scalar(
                text(
                    "INSERT INTO instruments (instrument_id, asset_class, market, symbol, status, "
                    "created_at, updated_at) "
                    "VALUES (gen_random_uuid(), 'equity', 'HK', '00700', 'active', now(), now()) "
                    "RETURNING instrument_id"
                )
            )
            archive = await db.scalar(
                text(
                    "INSERT INTO exchange_report_archive (archive_id, source, board, trade_date, "
                    "gzip_sha256, gzip_bytes, original_sha256, original_bytes, source_url, "
                    "source_last_modified, source_etag, object_key, object_etag, created_at) "
                    "VALUES (gen_random_uuid(), 'hkex', 'main', '2026-11-20', :sha, 1, :sha, 1, "
                    "'https://www.hkex.com.hk/eng/stat/smstat/dayquot/d261120e.htm', now(), "
                    "'\"etag\"', 'archive/v1/hkex/report.htm.gz', '\"etag\"', now()) "
                    "RETURNING archive_id"
                ),
                {"sha": SHA},
            )
            await db.execute(
                text(
                    "INSERT INTO eod_field_correction (correction_id, instrument_id, trade_date, "
                    "source, replaced_source, before, after, archive_id, created_at) "
                    "VALUES (gen_random_uuid(), :instrument, '2026-11-20', 'hkex', 'factset', "
                    '\'{"close": "343.8"}\', \'{"close": "344"}\', :archive, now())'
                ),
                {"instrument": instrument, "archive": archive},
            )
        with pytest.raises(subprocess.CalledProcessError) as blocked:
            await run_alembic(url, BEFORE, command="downgrade")
        assert "downgrade is blocked" in blocked.value.stderr
        assert await _shape(engine) == (True, True)
