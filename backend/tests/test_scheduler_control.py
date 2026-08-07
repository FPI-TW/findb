"""Scheduler control-plane model and API coverage."""

from datetime import datetime, time, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.registry import (
    AdminAuditEvent,
    DatasetRegistry,
    SchedulerControl,
    SchedulerDataset,
)
from app.services.api_keys import create_api_key
from app.services.source_clients import create_source_client
from app.utils import utc_now

SCHEDULER_KEY = "finlab_tw_equity_eod_v1"


async def _add_scheduler(
    db: AsyncSession,
    *,
    scheduler_key: str = SCHEDULER_KEY,
    provider: str = "finlab",
    dataset_keys: list[str] | None = None,
) -> SchedulerControl:
    normalized_dataset_keys = dataset_keys or ["tw_equity_eod"]
    for dataset_key in normalized_dataset_keys:
        db.add(
            DatasetRegistry(
                dataset_key=dataset_key,
                name=dataset_key,
                asset_class="equity",
                market="TW",
                frequency="daily",
                is_active=True,
                config={},
            )
        )
    row = SchedulerControl(
        scheduler_key=scheduler_key,
        provider=provider,
        slot_id="taiwan_market_window",
        scheduled_local_time=time(14, 30),
        timezone="Asia/Taipei",
        dataset_keys=normalized_dataset_keys,
        desired_state="stopped",
        observed_state="stopped",
        revision=1,
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    db.add(row)
    await db.flush()
    db.add_all(
        [
            SchedulerDataset(scheduler_key=scheduler_key, dataset_key=dataset_key)
            for dataset_key in normalized_dataset_keys
        ]
    )
    await db.flush()
    return row


async def _admin_headers(db: AsyncSession, *, role: str = "owner") -> dict[str, str]:
    settings = get_settings()
    _, api_key = await create_api_key(
        db,
        owner="scheduler-tests",
        tier="standard",
        scopes=["admin"],
        rate_limit_requests=100,
        rate_limit_window=60,
        page_size_limit=1000,
        kind="admin",
        name=f"scheduler-{role}",
        role=role,
        commit=False,
    )
    return {settings.API_KEY_HEADER: api_key}


async def _source_headers(
    db: AsyncSession,
    *,
    provider: str,
    allowed_datasets: list[str] | None,
) -> dict[str, str]:
    settings = get_settings()
    _, api_key = await create_source_client(
        db,
        name=f"scheduler-{provider}",
        owner="scheduler-tests",
        source_name=provider,
        allowed_datasets=allowed_datasets,
        rate_limit_requests=100,
        rate_limit_window=60,
        commit=False,
    )
    return {settings.API_KEY_HEADER: api_key}


@pytest.mark.asyncio
async def test_scheduler_model_exposes_constraints_and_indexes(test_engine):
    constraint_names = {constraint.name for constraint in SchedulerControl.__table__.constraints}
    index_names = {index.name for index in SchedulerControl.__table__.indexes}

    assert "ck_scheduler_control_desired_state_valid" in constraint_names
    assert "ck_scheduler_control_observed_state_valid" in constraint_names
    assert "ck_scheduler_control_revision_positive" in constraint_names
    assert "idx_scheduler_control_provider" in index_names
    assert "idx_scheduler_control_heartbeat" in index_names


@pytest.mark.asyncio
async def test_scheduler_admin_get_viewer_and_patch_revision_audit(
    client: AsyncClient,
    test_session: AsyncSession,
):
    await _add_scheduler(test_session)
    viewer_headers = await _admin_headers(test_session, role="viewer")
    response = await client.get("/api/v1/admin/schedulers", headers=viewer_headers)
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert response.json()["data"][0]["scheduler_key"] == SCHEDULER_KEY
    assert response.json()["data"][0]["heartbeat_age_seconds"] is None

    owner_headers = await _admin_headers(test_session, role="owner")
    changed = await client.patch(
        f"/api/v1/admin/schedulers/{SCHEDULER_KEY}",
        headers=owner_headers,
        json={"desired_state": "running", "expected_revision": 1},
    )
    assert changed.status_code == 200
    assert changed.json()["success"] is True
    assert changed.json()["data"]["desired_state"] == "running"
    assert changed.json()["data"]["revision"] == 2

    conflict = await client.patch(
        f"/api/v1/admin/schedulers/{SCHEDULER_KEY}",
        headers=owner_headers,
        json={"desired_state": "stopped", "expected_revision": 1},
    )
    assert conflict.status_code == 409

    audit = (
        await test_session.execute(
            select(AdminAuditEvent).where(
                AdminAuditEvent.resource_type == "scheduler_control",
                AdminAuditEvent.resource_id == SCHEDULER_KEY,
            )
        )
    ).scalar_one()
    assert audit.action == "update"


@pytest.mark.asyncio
async def test_scheduler_admin_patch_is_owner_only(
    client: AsyncClient,
    test_session: AsyncSession,
):
    await _add_scheduler(test_session)
    viewer_headers = await _admin_headers(test_session, role="viewer")
    response = await client.patch(
        f"/api/v1/admin/schedulers/{SCHEDULER_KEY}",
        headers=viewer_headers,
        json={"desired_state": "running", "expected_revision": 1},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_scheduler_source_scope_and_heartbeat_update(
    client: AsyncClient,
    test_session: AsyncSession,
):
    await _add_scheduler(test_session)
    source_headers = await _source_headers(
        test_session,
        provider="finlab",
        allowed_datasets=["tw_equity_eod"],
    )
    started = datetime(2026, 8, 4, 1, 30, tzinfo=timezone.utc)
    completed = started + timedelta(minutes=2)
    response = await client.post(
        f"/api/v1/source/scheduler-controls/{SCHEDULER_KEY}/poll",
        headers=source_headers,
        json={
            "observed_state": "running",
            "cycle_started_at": started.isoformat(),
            "cycle_completed_at": completed.isoformat(),
            "last_error": "temporary retry",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["desired_state"] == "stopped"
    assert payload["revision"] == 1
    assert payload["scheduler_key"] == SCHEDULER_KEY

    legacy_alias = await client.post(
        "/api/v1/source/scheduler-controls/finlab_tw_1430_tw_equity_eod/poll",
        headers=source_headers,
        json={"observed_state": "running"},
    )
    assert legacy_alias.status_code == 200
    assert legacy_alias.json()["scheduler_key"] == SCHEDULER_KEY
    assert legacy_alias.json()["slot_id"] == "taiwan_market_window"

    row = await test_session.get(SchedulerControl, SCHEDULER_KEY)
    assert row is not None
    assert row.observed_state == "running"
    assert row.last_heartbeat_at is not None
    assert row.last_cycle_started_at == started
    assert row.last_cycle_completed_at == completed
    assert row.last_error == "temporary retry"

    idle = await client.post(
        f"/api/v1/source/scheduler-controls/{SCHEDULER_KEY}/poll",
        headers=source_headers,
        json={"observed_state": "stopped"},
    )
    assert idle.status_code == 200
    await test_session.refresh(row)
    assert row.last_cycle_started_at == started
    assert row.last_cycle_completed_at == completed
    assert row.last_error == "temporary retry"

    recovered = await client.post(
        f"/api/v1/source/scheduler-controls/{SCHEDULER_KEY}/poll",
        headers=source_headers,
        json={
            "observed_state": "stopped",
            "cycle_started_at": (completed + timedelta(minutes=1)).isoformat(),
            "cycle_completed_at": (completed + timedelta(minutes=2)).isoformat(),
            "last_error": None,
        },
    )
    assert recovered.status_code == 200
    await test_session.refresh(row)
    assert row.last_error is None


@pytest.mark.asyncio
async def test_scheduler_source_scope_denies_wrong_provider_or_dataset(
    client: AsyncClient,
    test_session: AsyncSession,
):
    await _add_scheduler(test_session)
    wrong_provider = await _source_headers(
        test_session,
        provider="twelve_data",
        allowed_datasets=None,
    )
    path = f"/api/v1/source/scheduler-controls/{SCHEDULER_KEY}/poll"
    response = await client.post(
        path,
        headers=wrong_provider,
        json={"observed_state": "stopped"},
    )
    assert response.status_code == 403

    wrong_dataset = await _source_headers(
        test_session,
        provider="finlab",
        allowed_datasets=["other_dataset"],
    )
    response = await client.post(
        path,
        headers=wrong_dataset,
        json={"observed_state": "stopped"},
    )
    assert response.status_code == 403

    unknown = await client.post(
        "/api/v1/source/scheduler-controls/unknown/poll",
        headers=wrong_provider,
        json={"observed_state": "stopped"},
    )
    assert unknown.status_code == 404
