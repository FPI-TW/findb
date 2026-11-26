"""Unlisted write counts are additive; downgrade refuses once any are recorded."""

import subprocess
import uuid

import pytest
from sqlalchemy import text

from tests.migration_database import get_active_migration_database_factory, run_alembic

BEFORE = "d7f9b1c3e5a8"
HEAD = "f3a5c7e9b1d2"


async def _columns(engine) -> set[tuple[str, str]]:
    async with engine.connect() as db:
        rows = await db.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE (table_name, column_name) IN "
                "(('daily_delivery_plan_summary', 'unlisted_data_count'),"
                " ('historical_backfill_item', 'unlisted_symbols'))"
            )
        )
        return {tuple(row) for row in rows.all()}


@pytest.mark.asyncio
async def test_columns_are_added_and_downgrade_keeps_recorded_unlisted_writes():
    async with get_active_migration_database_factory().clone(
        "e9f4a1c6b3d5", "unlisted_symbol_counts"
    ) as (url, engine):
        await run_alembic(url, HEAD)
        assert len(await _columns(engine)) == 2
        # Nothing recorded yet: downgrade drops the additive columns (no test shims).
        await run_alembic(url, BEFORE, command="downgrade", forward_columns=False)
        assert await _columns(engine) == set()

        await run_alembic(url, HEAD)
        request_id, item_id = uuid.uuid4(), uuid.uuid4()
        async with engine.begin() as db:
            await db.execute(
                text(
                    "INSERT INTO historical_backfill_request (request_id, request_key, provider,"
                    " dataset_key, market, start_date, end_date, created_by, status,"
                    " created_at, updated_at) VALUES (:id, 'unlisted', 'finlab',"
                    " 'tw_equity_eod', 'TW', '2026-11-20', '2026-11-20', 'owner', 'completed',"
                    " now(), now())"
                ),
                {"id": request_id},
            )
            await db.execute(
                text(
                    "INSERT INTO historical_backfill_item (item_id, request_id, trade_date,"
                    " status, unlisted_symbols) VALUES (:item, :request, '2026-11-20',"
                    " 'completed', '[\"7799\"]')"
                ),
                {"item": item_id, "request": request_id},
            )
        with pytest.raises(subprocess.CalledProcessError) as refused:
            await run_alembic(url, BEFORE, command="downgrade", forward_columns=False)
        assert "roll forward" in refused.value.stderr
        assert len(await _columns(engine)) == 2
