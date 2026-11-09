"""Schedule publication is scoped configuration, independent of acquisition gates."""

import hashlib
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.registry import SchedulerControl
from app.services import feed_schedule, provider_metadata
from app.services.feed_schedule import deadline_at, execution_config, feed_schedule_view
from app.services.source_clients import create_source_client
from scripts.seed_data import seed_datasets

URL = "/api/v1/source/execution-config"


async def credential(db, provider="finlab", datasets=None):
    return await create_source_client(
        db,
        name="config-reader",
        source_name=provider,
        allowed_datasets=datasets,
        rate_limit_requests=1000,
        rate_limit_window=60,
        commit=True,
    )


@pytest.mark.parametrize("profile", ["pilot", "full_market"])
@pytest.mark.parametrize(
    "provider,count", [("finlab", 2), ("twelve_data", 1), ("factset", 1), ("taifex", 1)]
)
async def test_publication_is_credential_scoped_and_independent_of_running(
    client, test_session, monkeypatch, provider, count, profile
):
    await seed_datasets(test_session)
    before = [
        (r.scheduler_key, r.desired_state, r.revision)
        for r in (await test_session.scalars(select(SchedulerControl))).all()
    ]
    _, key = await credential(test_session, provider)
    monkeypatch.setattr(
        feed_schedule, "get_settings", lambda: SimpleNamespace(APP_ENVIRONMENT="staging")
    )
    response = await client.get(
        URL, params={"execution_profile": profile, "schema_version": 1}, headers={"X-API-Key": key}
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert (body["provider"], body["environment"], body["execution_profile"]) == (
        provider,
        "staging",
        profile,
    )
    assert len(body["feeds"]) == count
    assert all(f["provider"] == provider for f in body["feeds"])
    assert all("slot_id" not in f for f in body["feeds"])
    digest = body.pop("schedule_digest")
    assert (
        hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        == digest
    )
    after = [
        (r.scheduler_key, r.desired_state, r.revision)
        for r in (await test_session.scalars(select(SchedulerControl))).all()
    ]
    assert after == before


async def test_subset_does_not_publish_sibling_and_has_complete_snapshot(client, test_session):
    row, key = await credential(test_session, datasets=["tw_etf_eod"])
    first = await client.get(URL, params={"execution_profile": "pilot"}, headers={"X-API-Key": key})
    assert first.status_code == 200
    assert [f["dataset_key"] for f in first.json()["feeds"]] == ["tw_etf_eod"]
    row.allowed_datasets = ["tw_equity_eod"]
    await test_session.commit()
    second = await client.get(
        URL, params={"execution_profile": "pilot"}, headers={"X-API-Key": key}
    )
    assert second.status_code == 200
    assert [f["dataset_key"] for f in second.json()["feeds"]] == ["tw_equity_eod"]
    assert second.json()["schedule_digest"] != first.json()["schedule_digest"]


@pytest.mark.parametrize(
    "scope", [None, [], ["tw_etf_eod", "tw_etf_eod"], ["us_equity_eod"], ["tw_etf_eod", "unknown"]]
)
async def test_malformed_or_stale_db_scope_fails_closed(client, test_session, scope):
    row, key = await credential(test_session)
    row.allowed_datasets = scope
    await test_session.commit()
    response = await client.get(
        URL, params={"execution_profile": "pilot"}, headers={"X-API-Key": key}
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"execution_profile": "historical"},
        {"execution_profile": "pilot", "schema_version": 2},
        {"execution_profile": "pilot", "provider": "factset"},
        {"execution_profile": "pilot", "environment": "production"},
    ],
)
async def test_query_cannot_override_identity_or_version(client, test_session, params):
    _, key = await credential(test_session)
    response = await client.get(URL, params=params, headers={"X-API-Key": key})
    assert response.status_code == 422


async def test_auth_required_and_revoked_key_rejected(client, test_session):
    assert (await client.get(URL, params={"execution_profile": "pilot"})).status_code == 401
    row, key = await credential(test_session)
    row.revoked_at = datetime.now(timezone.utc)
    await test_session.commit()
    assert (
        await client.get(URL, params={"execution_profile": "pilot"}, headers={"X-API-Key": key})
    ).status_code == 403


@pytest.mark.parametrize("environment", ["local", "staging", "production"])
def test_digest_binds_environment_and_profile(monkeypatch, environment):
    monkeypatch.setattr(
        feed_schedule, "get_settings", lambda: SimpleNamespace(APP_ENVIRONMENT=environment)
    )
    first = execution_config("twelve_data", ["us_equity_eod"], "pilot")
    assert first.environment == environment
    assert (
        first.schedule_digest
        == execution_config("twelve_data", ["us_equity_eod"], "pilot").schedule_digest
    )
    assert (
        first.schedule_digest
        != execution_config("twelve_data", ["us_equity_eod"], "full_market").schedule_digest
    )


@pytest.mark.parametrize(
    "clock,expected",
    [
        ("2026-03-07T22:00:00+00:00", "2026-03-08T21:00:00+00:00"),
        ("2026-10-31T21:00:00+00:00", "2026-11-01T22:00:00+00:00"),
        ("2026-12-31T22:00:00+00:00", "2027-01-01T22:00:00+00:00"),
        ("2026-11-09T21:59:59+00:00", "2026-11-09T22:00:00+00:00"),
    ],
)
def test_exchange_clock_dst_boundaries_and_year_rollover(clock, expected):
    view = feed_schedule_view("twelve_data", "us_equity_eod", datetime.fromisoformat(clock))
    assert view.next_scheduled_at == datetime.fromisoformat(expected)
    assert view.scheduled_time == "17:00" and view.source_timezone == "America/New_York"


def test_completion_and_bounded_deadlines_are_independent():
    view = feed_schedule_view("twelve_data", "us_equity_eod", datetime.now(timezone.utc))
    assert (
        deadline_at(view.completion_deadline, date(2026, 12, 31)).isoformat()
        == "2027-01-01T01:00:00+00:00"
    )
    assert (
        deadline_at(view.bounded_monitor.deadline, date(2026, 12, 31)).isoformat()
        == "2027-01-01T01:15:00+00:00"
    )
    for provider, key in [
        ("finlab", "tw_equity_eod"),
        ("factset", "hk_equity_eod"),
        ("taifex", "tw_futures_eod"),
    ]:
        view = feed_schedule_view(provider, key, datetime.now(timezone.utc))
        assert (
            deadline_at(view.completion_deadline, date(2026, 11, 9)).isoformat()
            == "2026-11-09T15:00:00+00:00"
        )
    with pytest.raises(ValueError, match="timezone"):
        feed_schedule_view("finlab", "tw_equity_eod", datetime(2026, 11, 9))
    assert feed_schedule_view("shioaji", "tw_equity_minute", datetime.now(timezone.utc)) is None


def test_schedule_change_changes_snapshot_but_slot_does_not(monkeypatch):
    catalog = provider_metadata.provider_catalog().model_copy(deep=True)
    monkeypatch.setattr(provider_metadata, "provider_catalog", lambda: catalog)
    before = execution_config("twelve_data", ["us_equity_eod"], "pilot")
    feed = next(f for f in catalog.feeds if f.provider == "twelve_data")
    feed.delivery.slot_id = "taiwan_market_window"
    assert execution_config("twelve_data", ["us_equity_eod"], "pilot") == before
    feed.schedule.local_time = "18:00"
    after = execution_config("twelve_data", ["us_equity_eod"], "pilot")
    assert after.schedule_digest != before.schedule_digest
    assert after.feeds[0].scheduled_time == "18:00"


def test_generated_metadata_rejects_corruption(monkeypatch, tmp_path):
    path = tmp_path / "metadata.json"
    data = json.loads(provider_metadata.METADATA_PATH.read_text())
    data["source_sha256"] = "0" * 64
    path.write_text(json.dumps(data))
    monkeypatch.setattr(provider_metadata, "METADATA_PATH", path)
    provider_metadata.provider_catalog.cache_clear()
    try:
        with pytest.raises(ValueError, match="Invalid generated"):
            provider_metadata.provider_catalog()
    finally:
        provider_metadata.provider_catalog.cache_clear()


async def test_admin_feed_projection_uses_same_resolver(client, admin_headers, test_session):
    await seed_datasets(test_session)
    response = await client.get("/api/v1/admin/schedulers", headers=admin_headers)
    assert response.status_code == 200
    rows = response.json()["data"]
    for provider in ("finlab", "factset", "twelve_data", "taifex"):
        row = next(r for r in rows if r["scheduler_key"] == f"pilot_{provider}_v1")
        expected = execution_config(provider, row["dataset_keys"], "pilot")
        assert [
            {k: v for k, v in f.items() if k != "next_scheduled_at"} for f in row["feed_schedules"]
        ] == [f.model_dump(mode="json") for f in expected.feeds]

    freshness = await client.get("/api/v1/admin/market-freshness", headers=admin_headers)
    assert freshness.status_code == 200
    published = {
        (f["provider"], f["dataset_key"]): {k: v for k, v in f.items() if k != "next_scheduled_at"}
        for row in rows
        for f in row["feed_schedules"]
    }
    for row in freshness.json()["data"]:
        for feed in row["feeds"]:
            if (feed["source"], feed["dataset_key"]) in published:
                assert {
                    k: v for k, v in feed["schedule"].items() if k != "next_scheduled_at"
                } == published[(feed["source"], feed["dataset_key"])]


async def test_unavailable_schedule_does_not_return_partial_or_stale_snapshot(
    client, test_session, monkeypatch
):
    _, key = await credential(test_session)
    headers = {"X-API-Key": key}
    assert (
        await client.get(URL, params={"execution_profile": "pilot"}, headers=headers)
    ).status_code == 200
    catalog = provider_metadata.provider_catalog().model_copy(deep=True)
    catalog.feeds = [f for f in catalog.feeds if f.dataset_key != "tw_etf_eod"]
    monkeypatch.setattr(provider_metadata, "provider_catalog", lambda: catalog)
    response = await client.get(URL, params={"execution_profile": "pilot"}, headers=headers)
    assert response.status_code == 503
    assert response.json() == {"detail": "Execution configuration unavailable"}
