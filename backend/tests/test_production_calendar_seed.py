from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.calendar_management import latest_revision, revision_days
from scripts.seed_production_calendars import (
    CALENDAR_DIRECTORY,
    ProductionCalendarSeedError,
    load_reviewed_calendars,
    seed_reviewed_calendars,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FETCHER_TW_CALENDAR = (
    REPO_ROOT / "fetcher" / "configs" / "calendars" / "tw_equity_2025_2026.v1.json"
)
FETCHER_US_CALENDAR = (
    REPO_ROOT / "fetcher" / "configs" / "calendars" / "us_equity_2025_2028.v1.json"
)


def test_reviewed_sources_are_complete_and_match_fetcher_gates() -> None:
    reviewed = load_reviewed_calendars(CALENDAR_DIRECTORY)

    assert [(item.market, item.year) for item in reviewed] == [
        ("TW", 2025),
        ("TW", 2026),
        ("US", 2025),
        ("US", 2026),
        ("US", 2027),
        ("US", 2028),
    ]
    assert [len(item.rows) for item in reviewed] == [365, 365, 365, 365, 365, 366]
    assert [
        sum(row["status"] == "settlement_only" for row in item.rows) for item in reviewed[:2]
    ] == [2, 2]
    status_2026 = {row["trade_date"]: row["status"] for row in reviewed[1].rows}
    assert status_2026["2026-02-12"] == "settlement_only"
    assert status_2026["2026-02-13"] == "settlement_only"

    configured_tw = json.loads(FETCHER_TW_CALENDAR.read_text())
    expected_tw_non_trading_weekdays = {
        row["trade_date"]
        for item in reviewed[:2]
        for row in item.rows
        if date.fromisoformat(row["trade_date"]).weekday() < 5 and row["status"] != "open"
    }
    assert set(configured_tw["non_trading_dates"]) == expected_tw_non_trading_weekdays

    configured_us = json.loads(FETCHER_US_CALENDAR.read_text())
    expected_us_non_trading_weekdays = {
        row["trade_date"]
        for item in reviewed[2:]
        for row in item.rows
        if date.fromisoformat(row["trade_date"]).weekday() < 5 and row["status"] != "open"
    }
    assert set(configured_us["non_trading_dates"]) == expected_us_non_trading_weekdays


@pytest.mark.asyncio
async def test_seed_reviewed_calendars_is_published_and_idempotent(
    test_session: AsyncSession,
) -> None:
    reviewed = load_reviewed_calendars()

    first = await seed_reviewed_calendars(
        test_session,
        reviewed=reviewed,
        actor="production-bootstrap-test",
    )
    assert first["future_years_required_for_initial_deploy"] is False
    assert first["results"] == [
        {"market": "TW", "year": 2025, "action": "created", "revision": 1},
        {"market": "TW", "year": 2026, "action": "created", "revision": 1},
        {"market": "US", "year": 2025, "action": "created", "revision": 1},
        {"market": "US", "year": 2026, "action": "created", "revision": 1},
        {"market": "US", "year": 2027, "action": "created", "revision": 1},
        {"market": "US", "year": 2028, "action": "created", "revision": 1},
    ]

    for item in reviewed:
        revision = await latest_revision(test_session, item.market, item.year)
        assert revision is not None
        assert revision.status == "published"
        assert revision.source_sha256 == item.source_sha256
        assert len(await revision_days(test_session, revision.id)) == len(item.rows)

    repeated = await seed_reviewed_calendars(
        test_session,
        reviewed=reviewed,
        actor="production-bootstrap-test",
    )
    assert repeated["results"] == [
        {"market": "TW", "year": 2025, "action": "unchanged", "revision": 1},
        {"market": "TW", "year": 2026, "action": "unchanged", "revision": 1},
        {"market": "US", "year": 2025, "action": "unchanged", "revision": 1},
        {"market": "US", "year": 2026, "action": "unchanged", "revision": 1},
        {"market": "US", "year": 2027, "action": "unchanged", "revision": 1},
        {"market": "US", "year": 2028, "action": "unchanged", "revision": 1},
    ]


@pytest.mark.asyncio
async def test_seed_reviewed_calendars_rejects_existing_different_revision(
    test_session: AsyncSession,
) -> None:
    reviewed = load_reviewed_calendars()
    await seed_reviewed_calendars(
        test_session,
        reviewed=reviewed,
        actor="production-bootstrap-test",
    )
    revision = await latest_revision(test_session, "TW", 2025)
    assert revision is not None
    revision.source_sha256 = "0" * 64
    await test_session.commit()

    with pytest.raises(
        ProductionCalendarSeedError,
        match="existing TW 2025 calendar differs",
    ):
        await seed_reviewed_calendars(
            test_session,
            reviewed=reviewed,
            actor="production-bootstrap-test",
        )
