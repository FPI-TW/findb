from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canonical import CalendarMarket
from app.services.calendar_management import (
    RevisionConflictError,
    latest_revision,
    revision_days,
)
from scripts import seed_production_calendars as calendar_seed
from scripts.seed_production_calendars import (
    CALENDAR_DIRECTORY,
    ProductionCalendarSeedError,
    _load_reviewed_us_calendars,
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
        ("HK", 2026),
        ("CN", 2026),
        ("TAIFEX", 2026),
    ]
    assert [len(item.rows) for item in reviewed] == [365, 365, 365, 365, 365, 366, 365, 365, 365]
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
        for item in reviewed
        if item.market == "US"
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
        {"market": "HK", "year": 2026, "action": "created", "revision": 1},
        {"market": "CN", "year": 2026, "action": "created", "revision": 1},
        {"market": "TAIFEX", "year": 2026, "action": "created", "revision": 1},
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
        {"market": "HK", "year": 2026, "action": "unchanged", "revision": 1},
        {"market": "CN", "year": 2026, "action": "unchanged", "revision": 1},
        {"market": "TAIFEX", "year": 2026, "action": "unchanged", "revision": 1},
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


def test_reviewed_2026_exchange_dates_and_sessions() -> None:
    calendars = {item.market: item for item in load_reviewed_calendars() if item.year == 2026}
    counts = {"US": 251, "HK": 247, "CN": 242, "TAIFEX": 243}
    rows = {
        market: {row["trade_date"]: row for row in item.rows} for market, item in calendars.items()
    }
    for market, count in counts.items():
        assert sum(row["status"] == "open" for row in rows[market].values()) == count
        for row in rows[market].values():
            if row["status"] == "closed":
                assert row["session_open"] is row["session_close"] is None
            if date.fromisoformat(row["trade_date"]).weekday() >= 5:
                assert row["status"] == "closed"
    for day in ("2026-02-16", "2026-12-24", "2026-12-31"):
        assert rows["HK"][day]["status"] == "open"
        assert rows["HK"][day]["session_close"] == "12:10:00"
    assert rows["HK"]["2026-06-19"]["status"] == "closed"
    assert rows["HK"]["2026-06-18"]["session_close"] == "16:10:00"
    for day in ("2026-10-12", "2026-11-11"):
        assert rows["US"][day]["status"] == "open"
        assert rows["US"][day]["session_close"] == "16:00:00"
    for day in ("2026-11-27", "2026-12-24"):
        assert rows["US"][day]["session_open"] == "09:30:00"
        assert rows["US"][day]["session_close"] == "13:00:00"
    for day in ("2026-04-03", "2026-04-07", "2026-07-01", "2026-10-19"):
        assert rows["CN"][day]["status"] == "open"
        assert rows["CN"][day]["session_close"] == "15:00:00"
    for day in ("2026-02-12", "2026-02-13"):
        assert rows["TAIFEX"][day]["status"] == "closed"
        assert rows["TW"][day]["status"] == "settlement_only"
    assert rows["TAIFEX"]["2026-07-22"]["session_close"] == "13:45:00"


@pytest.mark.parametrize("invalid", ["incomplete", "duplicate", "weekend", "session"])
def test_reviewed_2026_calendar_rejects_invalid_dates_and_sessions(tmp_path, invalid) -> None:
    from shutil import copytree

    copytree(CALENDAR_DIRECTORY, tmp_path / "calendars")
    directory = tmp_path / "calendars"
    path = directory / "hk_equity_2026.v1.json"
    payload = json.loads(path.read_text())
    if invalid == "incomplete":
        payload["days"].pop()
    elif invalid == "duplicate":
        payload["days"][1]["trade_date"] = "2026-01-01"
    elif invalid == "weekend":
        payload["days"][2]["status"] = "open"
    else:
        payload["days"][1]["session_close"] = "invalid"
    path.write_text(json.dumps(payload))
    with pytest.raises(ProductionCalendarSeedError):
        load_reviewed_calendars(directory)


async def _seed_known_legacy_us_2026(session):
    reviewed = load_reviewed_calendars()
    legacy = next(
        item for item in _load_reviewed_us_calendars(CALENDAR_DIRECTORY) if item.year == 2026
    )
    old_reviewed = [
        legacy if item.market == "US" and item.year == 2026 else item for item in reviewed
    ]
    await seed_reviewed_calendars(session, reviewed=old_reviewed, actor="legacy-bootstrap-test")
    return reviewed


@pytest.mark.asyncio
async def test_known_legacy_us_2026_is_preserved_until_explicit_upgrade(test_session) -> None:
    reviewed = await _seed_known_legacy_us_2026(test_session)
    legacy_revision = await latest_revision(test_session, "US", 2026)
    old_id = legacy_revision.id
    preserved = await seed_reviewed_calendars(
        test_session, reviewed=reviewed, actor="bootstrap-test"
    )
    assert next(
        item for item in preserved["results"] if item["market"] == "US" and item["year"] == 2026
    ) == {
        "market": "US",
        "year": 2026,
        "action": "pending_reviewed_update",
        "revision": 1,
        "upgrade_required": True,
    }
    assert (await latest_revision(test_session, "US", 2026)).id == old_id
    upgraded = await seed_reviewed_calendars(
        test_session, reviewed=reviewed, actor="reviewed-update-test", upgrade_reviewed_2026=True
    )
    assert next(
        item for item in upgraded["results"] if item["market"] == "US" and item["year"] == 2026
    ) == {"market": "US", "year": 2026, "action": "upgraded", "revision": 2}
    await test_session.refresh(legacy_revision)
    assert legacy_revision.status == "superseded"
    assert len(await revision_days(test_session, old_id)) == 365
    current = await latest_revision(test_session, "US", 2026)
    days = {
        day.trade_date.isoformat(): day for day in await revision_days(test_session, current.id)
    }
    assert days["2026-11-27"].session_close.isoformat() == "13:00:00"
    assert (await test_session.get(CalendarMarket, "US")).default_session_close is None
    repeated = await seed_reviewed_calendars(
        test_session, reviewed=reviewed, actor="reviewed-update-test", upgrade_reviewed_2026=True
    )
    assert all(item["action"] == "unchanged" for item in repeated["results"])
    assert (await latest_revision(test_session, "US", 2026)).revision == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["row", "draft", "source"])
async def test_reviewed_upgrade_rejects_operator_modified_or_draft_legacy(
    test_session, invalid
) -> None:
    reviewed = await _seed_known_legacy_us_2026(test_session)
    legacy = await latest_revision(test_session, "US", 2026)
    if invalid == "row":
        days = await revision_days(test_session, legacy.id)
        days[1].description = "Owner override"
    elif invalid == "draft":
        legacy.status = "draft"
    else:
        legacy.source_sha256 = "0" * 64
    await test_session.commit()
    with pytest.raises(ProductionCalendarSeedError, match="existing US 2026 calendar differs"):
        await seed_reviewed_calendars(
            test_session,
            reviewed=reviewed,
            actor="reviewed-update-test",
            upgrade_reviewed_2026=True,
        )
    assert (await latest_revision(test_session, "US", 2026)).revision == 1


@pytest.mark.asyncio
async def test_reviewed_upgrade_uses_preview_compare_and_swap(test_session, monkeypatch) -> None:
    reviewed = await _seed_known_legacy_us_2026(test_session)
    original_preview = calendar_seed.create_preview

    async def conflicted_preview(*args, **kwargs):
        batch = await original_preview(*args, **kwargs)
        batch.base_revision += 1
        await test_session.flush()
        return batch

    monkeypatch.setattr(calendar_seed, "create_preview", conflicted_preview)
    with pytest.raises(RevisionConflictError, match="calendar changed since preview"):
        await seed_reviewed_calendars(
            test_session,
            reviewed=reviewed,
            actor="reviewed-update-test",
            upgrade_reviewed_2026=True,
        )
    await test_session.rollback()
    assert (await latest_revision(test_session, "US", 2026)).revision == 1
