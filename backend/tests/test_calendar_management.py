"""Focused tests for independently managed calendar year lifecycle."""

from base64 import b64encode
from datetime import date, time
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.models.canonical import CalendarMarket, CalendarYearRevision, TradingCalendar
from app.services.calendar_management import (
    CalendarError,
    apply_preview,
    clone_with_day_change,
    create_preview,
    latest_revision,
    parse_twse_csv,
    publish_revision,
    published_year,
    revision_days,
    rollback_revision,
)
from app.utils import utc_now

TWSE_115_FIXTURE = (
    Path(__file__).parent / "fixtures" / "calendar" / "holiday_schedule_115.csv"
).read_text(encoding="utf-8")


@pytest.fixture
def tw_market() -> CalendarMarket:
    return CalendarMarket(
        market="TW",
        display_name="臺灣",
        timezone="Asia/Taipei",
        weekend_days=[5, 6],
        default_session_open=time(9),
        default_session_close=time(13, 30),
        active=True,
        created_at=utc_now(),
        updated_at=utc_now(),
    )


def test_twse_cp950_fixture_expands_and_sanitizes(tw_market):
    rows, encoding = parse_twse_csv(TWSE_115_FIXTURE.encode("cp950"), year=2026, market=tw_market)

    assert encoding == "cp950"
    assert len(rows) == 365
    assert {
        status: sum(row["status"] == status for row in rows)
        for status in ("open", "closed", "settlement_only")
    } == {"open": 243, "closed": 120, "settlement_only": 2}
    by_day = {row["trade_date"]: row for row in rows}
    assert by_day["2026-01-01"]["status"] == "closed"
    assert by_day["2026-01-02"]["status"] == "open"
    assert by_day["2026-02-12"]["status"] == "settlement_only"
    assert by_day["2026-02-13"]["status"] == "settlement_only"
    assert "<br" not in (by_day["2026-02-17"]["description"] or "")


def test_twse_rejects_unknown_marker_and_bad_weekday(tw_market):
    bad_marker = TWSE_115_FIXTURE.replace('"*"\n', '"?"\n', 1).encode("cp950")
    with pytest.raises(CalendarError, match="unknown marker"):
        parse_twse_csv(bad_marker, year=2026, market=tw_market)
    bad_weekday = TWSE_115_FIXTURE.replace("1月1日 (四)", "1月1日 (五)").encode("cp950")
    with pytest.raises(CalendarError, match="weekday"):
        parse_twse_csv(bad_weekday, year=2026, market=tw_market)


@pytest.mark.asyncio
async def test_preview_apply_publish_isolated_from_observed_calendar(test_session, tw_market):
    test_session.add(tw_market)
    # Existing normalization-derived row is deliberately not used as managed coverage.
    test_session.add(TradingCalendar(market="TW", trade_date=date(2026, 1, 5), is_open=True))
    await test_session.commit()
    rows, _ = parse_twse_csv(TWSE_115_FIXTURE.encode("cp950"), year=2026, market=tw_market)
    batch = await create_preview(
        test_session,
        market=tw_market,
        year=2026,
        input_format="twse_csv",
        rows=rows,
        source_bytes=TWSE_115_FIXTURE.encode("cp950"),
        source_filename="twse.csv",
        detected_encoding="cp950",
        created_by="tester",
    )
    assert (await test_session.execute(select(CalendarYearRevision))).scalars().all() == []
    revision = await apply_preview(
        test_session, batch_id=batch.id, expected_revision=0, actor="tester"
    )
    repeated = await apply_preview(
        test_session, batch_id=batch.id, expected_revision=0, actor="tester"
    )
    assert repeated.id == revision.id
    assert revision.status == "draft"
    assert await published_year(test_session, "TW", 2026) is None
    published = await publish_revision(
        test_session,
        market="TW",
        year=2026,
        expected_revision=revision.revision,
        published_by="owner",
    )
    resolved = await published_year(test_session, "TW", 2026)
    assert published.status == "published"
    assert resolved is not None
    assert len(resolved[2]) == 365

    changed = await clone_with_day_change(
        test_session,
        market="TW",
        trade_date=date(2026, 1, 2),
        expected_revision=1,
        replacement={
            "status": "closed",
            "holiday_name": "測試休市",
            "description": "revision 2",
            "session_open": None,
            "session_close": None,
        },
    )
    await publish_revision(
        test_session,
        market="TW",
        year=2026,
        expected_revision=changed.revision,
        published_by="owner",
    )
    rolled_back = await rollback_revision(
        test_session,
        market="TW",
        year=2026,
        target_revision=1,
        published_by="owner",
    )
    assert rolled_back.revision == 3
    assert rolled_back.status == "published"
    assert (await latest_revision(test_session, "TW", 2026)).id == rolled_back.id
    resolved_after_rollback = await published_year(test_session, "TW", 2026)
    assert resolved_after_rollback is not None
    restored = {day.trade_date.isoformat(): day for day in resolved_after_rollback[2]}
    assert restored["2026-01-02"].day_status == "open"


@pytest.mark.asyncio
async def test_manual_edit_can_create_first_complete_revision(test_session, tw_market):
    test_session.add(tw_market)
    await test_session.commit()

    revision = await clone_with_day_change(
        test_session,
        market="TW",
        trade_date=date(2027, 1, 1),
        expected_revision=0,
        replacement={
            "status": "closed",
            "holiday_name": "元旦",
            "description": "手動建立年度日曆",
            "session_open": "09:00",
            "session_close": "13:30",
        },
    )

    assert revision.revision == 1
    assert revision.status == "draft"
    assert revision.actual_days == 365
    days = await revision_days(test_session, revision.id)
    assert len(days) == 365
    assert days[0].trade_date == date(2027, 1, 1)
    assert days[0].day_status == "closed"
    assert days[0].holiday_name == "元旦"
    assert days[0].session_open is None
    assert days[0].session_close is None
    assert days[1].day_status == "closed"
    assert days[4].day_status == "open"


@pytest.mark.asyncio
async def test_admin_preview_lifecycle_and_scheduler_contract(
    client: AsyncClient, admin_headers: dict
):
    market_response = await client.put(
        "/api/v1/admin/calendars/markets/TW",
        headers=admin_headers,
        json={
            "market": "TW",
            "display_name": "臺灣",
            "timezone": "Asia/Taipei",
            "weekend_days": [5, 6],
            "default_session_open": "09:00:00",
            "default_session_close": "13:30:00",
            "active": True,
        },
    )
    assert market_response.status_code == 200
    preview = await client.post(
        "/api/v1/admin/calendars/imports/preview",
        headers=admin_headers,
        json={
            "market": "TW",
            "year": 2026,
            "filename": "holidaySchedule_115.csv",
            "content_base64": b64encode(TWSE_115_FIXTURE.encode("cp950")).decode(),
        },
    )
    assert preview.status_code == 200
    preview_body = preview.json()
    assert preview_body["detected_encoding"] == "cp950"
    assert preview_body["summary"]["total"] == 365
    batch_id = preview_body["batch_id"]
    imports = await client.get(
        "/api/v1/admin/calendars/imports?market=TW&year=2026", headers=admin_headers
    )
    assert imports.status_code == 200
    assert imports.json()[0]["id"] == batch_id
    apply = await client.post(
        f"/api/v1/admin/calendars/imports/{batch_id}/apply",
        headers=admin_headers,
        json={"expected_revision": 0},
    )
    assert apply.status_code == 200
    assert apply.json()["status"] == "draft"
    assert (await client.get("/api/v1/serve/calendar/years/TW/2026")).status_code == 404
    publish = await client.post(
        "/api/v1/admin/calendars/TW/2026/publish",
        headers=admin_headers,
        json={"expected_revision": 1},
    )
    assert publish.status_code == 200
    scheduler = await client.get("/api/v1/serve/calendar/years/TW/2026")
    assert scheduler.status_code == 200
    body = scheduler.json()
    assert body["data"]["coverage_complete"] is True
    assert len(body["data"]["days"]) == 365
