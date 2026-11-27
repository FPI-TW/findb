"""The deploy's catalog schedule preflight: the inspection it loads stays current.

``scripts/preflight_catalog_schedule.py`` runs one migration module's ``inspect``
against the database before every deployment.  Its frozen inputs must match the
live catalog, and a database at head must be fully compliant with it.
"""

from datetime import time

import pytest

from scripts.preflight_catalog_schedule import migration_module
from tests.migration_database import get_active_migration_database_factory, run_alembic


def test_the_preflight_inspects_the_latest_schedule_revision():
    module = migration_module()
    assert module.revision == "21e8f89c680b"
    assert module.FEEDS["tw_equity_eod"] == (
        "finlab",
        "Asia/Taipei",
        "19:30:00",
        (("Asia/Taipei", "18:00:00", 0, "finlab"),),
        "17:00:00",
        0,
    )
    assert module.CONTROLS["finlab"] == (
        "Asia/Taipei",
        "19:30:00",
        (("Asia/Taipei", "18:00:00"),),
    )
    assert module.FEEDS["tw_twse_index_eod"] == ("twse", "Asia/Taipei", "14:15:00", (), None, 0)
    assert module.FEEDS["tw_tpex_index_eod"] == ("tpex", "Asia/Taipei", "14:15:00", (), None, 0)
    assert module.CONTROLS["twse"] == module.CONTROLS["tpex"] == ("Asia/Taipei", "14:15:00", ())
    assert module.FEEDS["tw_twse_institutional_stock"] == (
        "twse",
        "Asia/Taipei",
        "16:45:00",
        (),
        None,
        0,
    )
    assert module.FEEDS["tw_tpex_institutional_summary"][2] == "17:30:00"
    for key in ("hk_index_constituents", "hk_index_changes"):
        assert module.FEEDS[key] == ("hkex", "Asia/Hong_Kong", "21:05:00", (), None, 0)
    # The earlier feeds keep the rules of the revisions that installed them.
    assert {"us_dollar_index_eod", "hk_index_eod", "us_index_eod", "us_equity_eod"} <= set(
        module.FEEDS
    )


def test_frozen_inputs_match_the_live_catalog():
    from app.services.provider_metadata import acquisition_feeds

    module = migration_module()
    for feed in acquisition_feeds():
        if feed.dataset_key in module.FEEDS:
            provider, zone, clock, *_ = module.FEEDS[feed.dataset_key]
            assert feed.schedule is not None
            assert (feed.provider, feed.schedule.timezone) == (provider, zone)
            assert time.fromisoformat(feed.schedule.local_time).isoformat() == clock


@pytest.mark.asyncio
async def test_a_database_at_head_is_compliant_with_the_preflight():
    module = migration_module()
    async with get_active_migration_database_factory().clone(
        "1587769858c0", "catalog_preflight"
    ) as (url, engine):
        await run_alembic(url, "head")
        async with engine.connect() as db:
            report, _, _ = await db.run_sync(module.inspect)
        assert report
        assert all(row["status"] == "already_compliant" for row in report)
