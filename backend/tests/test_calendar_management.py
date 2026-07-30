"""Focused tests for independently managed calendar year lifecycle."""

from base64 import b64encode
from datetime import date, time
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.canonical import CalendarMarket, CalendarYearRevision, TradingCalendar
from app.services.calendar_management import (
    CalendarError,
    RevisionConflictError,
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


async def _calendar_serve_headers(
    test_session, *, kind: str = "serve", scopes: list[str] | None = None
) -> dict[str, str]:
    from app.config import get_settings
    from app.services.api_keys import create_api_key

    _, api_key = await create_api_key(
        test_session,
        owner=f"calendar-{kind}",
        tier="standard",
        scopes=["serve"] if scopes is None else scopes,
        rate_limit_requests=100,
        rate_limit_window=60,
        page_size_limit=1000,
        kind=kind,
        name=f"calendar-{kind}",
        role="viewer" if kind == "admin" else None,
        commit=False,
    )
    return {get_settings().API_KEY_HEADER: api_key}


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
    assert resolved[0].timezone == "Asia/Taipei"
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
        expected_revision=changed.revision,
        published_by="owner",
    )
    assert rolled_back.revision == 3
    assert rolled_back.status == "published"
    assert rolled_back.timezone == "Asia/Taipei"
    assert (await latest_revision(test_session, "TW", 2026)).id == rolled_back.id
    resolved_after_rollback = await published_year(test_session, "TW", 2026)
    assert resolved_after_rollback is not None
    restored = {day.trade_date.isoformat(): day for day in resolved_after_rollback[2]}
    assert restored["2026-01-02"].day_status == "open"


@pytest.mark.asyncio
async def test_rollback_rejects_current_or_unpublished_revision(
    client: AsyncClient, test_session, admin_headers: dict, tw_market
):
    test_session.add(tw_market)
    await test_session.commit()
    first = await clone_with_day_change(
        test_session,
        market="TW",
        trade_date=date(2026, 1, 1),
        expected_revision=0,
        replacement={
            "status": "closed",
            "holiday_name": "元旦",
            "description": None,
            "session_open": None,
            "session_close": None,
        },
    )
    await publish_revision(
        test_session,
        market="TW",
        year=2026,
        expected_revision=first.revision,
        published_by="owner",
    )
    second = await clone_with_day_change(
        test_session,
        market="TW",
        trade_date=date(2026, 1, 2),
        expected_revision=first.revision,
        replacement={
            "status": "closed",
            "holiday_name": "測試休市",
            "description": None,
            "session_open": None,
            "session_close": None,
        },
    )
    await publish_revision(
        test_session,
        market="TW",
        year=2026,
        expected_revision=second.revision,
        published_by="owner",
    )
    draft = await clone_with_day_change(
        test_session,
        market="TW",
        trade_date=date(2026, 1, 3),
        expected_revision=second.revision,
        replacement={
            "status": "closed",
            "holiday_name": "草稿休市",
            "description": None,
            "session_open": None,
            "session_close": None,
        },
    )

    for target_revision in (second.revision, draft.revision):
        with pytest.raises(CalendarError, match="older published"):
            await rollback_revision(
                test_session,
                market="TW",
                year=2026,
                target_revision=target_revision,
                expected_revision=draft.revision,
                published_by="owner",
            )
    stale_response = await client.post(
        "/api/v1/admin/calendars/TW/2026/rollback",
        headers=admin_headers,
        json={"target_revision": first.revision, "expected_revision": second.revision},
    )
    assert stale_response.status_code == 409


@pytest.mark.asyncio
async def test_admin_year_exposes_active_published_revision_with_newer_draft(
    client: AsyncClient, test_session, admin_headers: dict, tw_market
):
    test_session.add(tw_market)
    await test_session.commit()
    published = await clone_with_day_change(
        test_session,
        market="TW",
        trade_date=date(2026, 1, 1),
        expected_revision=0,
        replacement={
            "status": "closed",
            "holiday_name": "元旦",
            "description": None,
            "session_open": None,
            "session_close": None,
        },
    )
    await publish_revision(
        test_session,
        market="TW",
        year=2026,
        expected_revision=published.revision,
        published_by="owner",
    )
    draft = await clone_with_day_change(
        test_session,
        market="TW",
        trade_date=date(2026, 1, 2),
        expected_revision=published.revision,
        replacement={
            "status": "closed",
            "holiday_name": "草稿休市",
            "description": None,
            "session_open": None,
            "session_close": None,
        },
    )

    response = await client.get(
        "/api/v1/admin/calendars/days?market=TW&year=2026", headers=admin_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["revision"]["revision"] == draft.revision
    assert body["revision"]["status"] == "draft"
    assert body["days"][0]["revision"] == draft.revision
    assert body["published_revision"]["revision"] == published.revision
    assert body["published_revision"]["status"] == "published"
    assert body["published_revision"]["timezone"] == "Asia/Taipei"


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
async def test_published_calendar_revision_is_unique_per_market_year(test_session):
    """Base.metadata.create_all() must retain the migration's partial unique index."""
    test_session.add(
        CalendarYearRevision(
            market="TW",
            year=2026,
            revision=1,
            status="published",
            expected_days=365,
            actual_days=365,
            timezone="Asia/Taipei",
            source_kind="manual",
        )
    )
    await test_session.flush()
    test_session.add(
        CalendarYearRevision(
            market="TW",
            year=2026,
            revision=2,
            status="published",
            expected_days=365,
            actual_days=365,
            timezone="Asia/Taipei",
            source_kind="manual",
        )
    )
    with pytest.raises(IntegrityError):
        await test_session.flush()
    await test_session.rollback()


@pytest.mark.asyncio
async def test_publish_rejects_incomplete_calendar_revision(test_session, tw_market):
    test_session.add(tw_market)
    test_session.add(
        CalendarYearRevision(
            market="TW",
            year=2026,
            revision=1,
            status="draft",
            expected_days=365,
            actual_days=364,
            timezone="Asia/Taipei",
            source_kind="manual",
        )
    )
    await test_session.commit()

    with pytest.raises(CalendarError, match="incomplete"):
        await publish_revision(
            test_session,
            market="TW",
            year=2026,
            expected_revision=1,
            published_by="owner",
        )

    revision = await latest_revision(test_session, "TW", 2026)
    assert revision is not None
    assert revision.status == "draft"


@pytest.mark.asyncio
async def test_calendar_mutation_rejects_stale_revision(test_session, tw_market):
    test_session.add(tw_market)
    await test_session.commit()
    created = await clone_with_day_change(
        test_session,
        market="TW",
        trade_date=date(2026, 1, 1),
        expected_revision=0,
        replacement={
            "status": "closed",
            "holiday_name": "元旦",
            "description": None,
            "session_open": None,
            "session_close": None,
        },
    )

    with pytest.raises(RevisionConflictError, match="calendar changed"):
        await clone_with_day_change(
            test_session,
            market="TW",
            trade_date=date(2026, 1, 2),
            expected_revision=0,
            replacement={
                "status": "closed",
                "holiday_name": None,
                "description": None,
                "session_open": None,
                "session_close": None,
            },
        )

    assert (await latest_revision(test_session, "TW", 2026)).id == created.id


async def _calendar_role_headers(test_session, role: str) -> dict[str, str]:
    from app.config import get_settings
    from app.services.api_keys import create_api_key

    _, api_key = await create_api_key(
        test_session,
        owner=f"calendar-{role}",
        tier="standard",
        scopes=["admin"],
        rate_limit_requests=100,
        rate_limit_window=60,
        page_size_limit=1000,
        kind="admin",
        name=f"calendar-{role}",
        role=role,
        commit=False,
    )
    return {get_settings().API_KEY_HEADER: api_key}


@pytest.mark.asyncio
async def test_calendar_routes_enforce_viewer_operator_owner_roles(
    client: AsyncClient, test_session, admin_headers: dict
):
    viewer_headers = await _calendar_role_headers(test_session, "viewer")
    operator_headers = await _calendar_role_headers(test_session, "operator")
    market_payload = {
        "market": "TW",
        "display_name": "臺灣",
        "timezone": "Asia/Taipei",
        "weekend_days": [5, 6],
        "default_session_open": "09:00:00",
        "default_session_close": "13:30:00",
        "active": True,
    }

    assert (
        await client.get("/api/v1/admin/calendars/markets", headers=viewer_headers)
    ).status_code == 200
    assert (
        await client.post(
            "/api/v1/admin/calendars/imports/json/preview",
            headers=viewer_headers,
            json={"market": "TW", "year": 2026, "coverage_mode": "exceptions", "days": []},
        )
    ).status_code == 403
    assert (
        await client.put(
            "/api/v1/admin/calendars/markets/TW",
            headers=operator_headers,
            json=market_payload,
        )
    ).status_code == 403
    assert (
        await client.put(
            "/api/v1/admin/calendars/markets/TW",
            headers=admin_headers,
            json=market_payload,
        )
    ).status_code == 200
    assert (
        await client.post(
            "/api/v1/admin/calendars/imports/json/preview",
            headers=operator_headers,
            json={"market": "TW", "year": 2026, "coverage_mode": "exceptions", "days": []},
        )
    ).status_code == 200


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_calendar_rollback_mutation(
    client: AsyncClient, test_session, admin_headers: dict, monkeypatch, tw_market
):
    from app.api.v1 import admin as admin_api

    test_session.add(tw_market)
    await test_session.commit()
    first = await clone_with_day_change(
        test_session,
        market="TW",
        trade_date=date(2026, 1, 1),
        expected_revision=0,
        replacement={
            "status": "closed",
            "holiday_name": "元旦",
            "description": None,
            "session_open": None,
            "session_close": None,
        },
    )
    await publish_revision(
        test_session,
        market="TW",
        year=2026,
        expected_revision=first.revision,
        published_by="owner",
    )
    second = await clone_with_day_change(
        test_session,
        market="TW",
        trade_date=date(2026, 1, 2),
        expected_revision=first.revision,
        replacement={
            "status": "closed",
            "holiday_name": "測試休市",
            "description": None,
            "session_open": None,
            "session_close": None,
        },
    )
    await publish_revision(
        test_session,
        market="TW",
        year=2026,
        expected_revision=second.revision,
        published_by="owner",
    )

    async def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(admin_api, "record_admin_audit", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await client.post(
            "/api/v1/admin/calendars/TW/2026/rollback",
            headers=admin_headers,
            json={"target_revision": first.revision, "expected_revision": second.revision},
        )
    await test_session.rollback()

    revisions = list(
        (
            await test_session.execute(
                select(CalendarYearRevision)
                .where(CalendarYearRevision.market == "TW", CalendarYearRevision.year == 2026)
                .order_by(CalendarYearRevision.revision)
            )
        )
        .scalars()
        .all()
    )
    assert [(row.revision, row.status) for row in revisions] == [
        (first.revision, "superseded"),
        (second.revision, "published"),
    ]


@pytest.mark.asyncio
async def test_admin_preview_lifecycle_and_scheduler_contract(
    client: AsyncClient, test_session, admin_headers: dict
):
    serve_headers = await _calendar_serve_headers(test_session)
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
    assert (
        await client.get("/api/v1/serve/calendar/years/TW/2026", headers=serve_headers)
    ).status_code == 404
    publish = await client.post(
        "/api/v1/admin/calendars/TW/2026/publish",
        headers=admin_headers,
        json={"expected_revision": 1},
    )
    assert publish.status_code == 200
    scheduler = await client.get("/api/v1/serve/calendar/years/TW/2026", headers=serve_headers)
    assert scheduler.status_code == 200
    body = scheduler.json()
    assert body["data"]["coverage_complete"] is True
    assert len(body["data"]["days"]) == 365
    changed_market = await client.put(
        "/api/v1/admin/calendars/markets/TW",
        headers=admin_headers,
        json={
            "market": "TW",
            "display_name": "臺灣",
            "timezone": "UTC",
            "weekend_days": [5, 6],
            "default_session_open": "09:00:00",
            "default_session_close": "13:30:00",
            "active": True,
        },
    )
    assert changed_market.status_code == 200
    scheduler_after_market_change = await client.get(
        "/api/v1/serve/calendar/years/TW/2026", headers=serve_headers
    )
    assert scheduler_after_market_change.status_code == 200
    assert scheduler_after_market_change.json()["data"]["timezone"] == "Asia/Taipei"


@pytest.mark.asyncio
async def test_calendar_year_route_requires_db_backed_serve_key_when_auth_optional(
    client: AsyncClient, test_session
):
    from app.config import get_settings

    settings = get_settings()
    original_require_auth = settings.SERVE_REQUIRE_AUTH
    settings.SERVE_REQUIRE_AUTH = False
    try:
        valid_headers = await _calendar_serve_headers(test_session)
        wrong_scope_headers = await _calendar_serve_headers(test_session, scopes=[])
        wrong_kind_headers = await _calendar_serve_headers(test_session, kind="admin")
        path = "/api/v1/serve/calendar/years/TW/2026"

        assert (await client.get(path)).status_code == 401
        assert (
            await client.get(path, headers={settings.API_KEY_HEADER: "invalid"})
        ).status_code == 403
        assert (await client.get(path, headers=wrong_scope_headers)).status_code == 403
        assert (await client.get(path, headers=wrong_kind_headers)).status_code == 403
        assert (await client.get(path, headers=valid_headers)).status_code == 404
    finally:
        settings.SERVE_REQUIRE_AUTH = original_require_auth
