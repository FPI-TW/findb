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
FETCHER_CALENDAR = REPO_ROOT / "fetcher" / "configs" / "calendars" / "tw_equity_2025_2026.v1.json"


def test_reviewed_twse_sources_are_complete_and_match_fetcher_gate() -> None:
    reviewed = load_reviewed_calendars(CALENDAR_DIRECTORY)

    assert [item.year for item in reviewed] == [2025, 2026]
    assert [len(item.rows) for item in reviewed] == [365, 365]
    assert [sum(row["status"] == "settlement_only" for row in item.rows) for item in reviewed] == [
        2,
        2,
    ]
    status_2026 = {row["trade_date"]: row["status"] for row in reviewed[1].rows}
    assert status_2026["2026-02-12"] == "settlement_only"
    assert status_2026["2026-02-13"] == "settlement_only"

    configured = json.loads(FETCHER_CALENDAR.read_text())
    expected_non_trading_weekdays = {
        row["trade_date"]
        for item in reviewed
        for row in item.rows
        if date.fromisoformat(row["trade_date"]).weekday() < 5 and row["status"] != "open"
    }
    assert set(configured["non_trading_dates"]) == expected_non_trading_weekdays


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
        {"year": 2025, "action": "created", "revision": 1},
        {"year": 2026, "action": "created", "revision": 1},
    ]

    for item in reviewed:
        revision = await latest_revision(test_session, "TW", item.year)
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
        {"year": 2025, "action": "unchanged", "revision": 1},
        {"year": 2026, "action": "unchanged", "revision": 1},
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
