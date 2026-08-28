"""EOD partition ownership regression tests."""

from datetime import date
from unittest.mock import AsyncMock

import pytest

from app.services.normalize.base import BaseNormalizer, EODPartitionUnavailableError


class _Normalizer(BaseNormalizer):
    def map_fields(self, raw_data: dict) -> list:
        return []


@pytest.mark.asyncio
async def test_eod_partition_check_uses_catalog_read_and_caches_year() -> None:
    session = AsyncMock()
    session.scalar.return_value = True
    normalizer = _Normalizer(session)

    await normalizer.ensure_eod_partition(date(2026, 8, 28))
    await normalizer.ensure_eod_partition(date(2026, 12, 31))

    session.scalar.assert_awaited_once()
    statement = str(session.scalar.await_args.args[0])
    assert "pg_inherits" in statement
    assert "CREATE TABLE" not in statement
    assert session.scalar.await_args.args[1] == {"partition_name": "market_data_eod_y2026"}


@pytest.mark.asyncio
async def test_eod_partition_check_fails_closed_when_partition_is_missing() -> None:
    session = AsyncMock()
    session.scalar.return_value = False
    normalizer = _Normalizer(session)

    with pytest.raises(
        EODPartitionUnavailableError,
        match="migration-owned EOD partition is unavailable for year 2032",
    ):
        await normalizer.ensure_eod_partition(date(2032, 1, 1))
