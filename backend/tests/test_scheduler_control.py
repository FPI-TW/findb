"""Scheduler control-plane model and API coverage."""

from datetime import datetime, time, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, null, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.registry import (
    AdminAuditEvent,
    DatasetRegistry,
    SchedulerControl,
    SchedulerDataset,
    SourceClient,
)
from app.services.api_keys import create_api_key
from app.services.scheduler_control import scheduler_dataset_keys
from app.services.source_clients import PROVIDER_DATASET_SCOPE, create_source_client
from app.utils import utc_now

SCHEDULER_KEY = "finlab_tw_equity_eod_v1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope",
    [
        None,
        [],
        "tw_equity_eod",
        [None],
        [1],
        [""],
        ["tw_etf_eod"],
        ["us_equity_eod"],
        ["tw_equity_eod", "tw_equity_eod"],
    ],
)
async def test_running_pilot_acquisition_denies_invalid_db_credential_scope(
    client, test_session, scope
):
    row = await _add_scheduler(test_session)
    row.desired_state = "running"
    source, key = await create_source_client(
        test_session,
        name="invalid-pilot-probe",
        source_name="finlab",
        allowed_datasets=["tw_equity_eod"],
        rate_limit_requests=100,
        rate_limit_window=60,
        commit=False,
    )
    path = f"/api/v1/source/scheduler-controls/{SCHEDULER_KEY}/authorization"
    headers = {"X-API-Key": key}
    assert (await client.get(path, headers=headers)).json()["acquisition_allowed"] is True
    # Bypass lifecycle validation to exercise persisted legacy/corrupt credentials.
    # Keep the identity-map copy stale: auth must reload the current DB scope.
    await test_session.execute(
        update(SourceClient)
        .where(SourceClient.client_id == source.client_id)
        .values(allowed_datasets=null() if scope is None else scope)
        .execution_options(synchronize_session=False)
    )
    denied = await client.get(path, headers=headers)
    assert denied.status_code == 403
    assert "acquisition_allowed" not in denied.json()
    await test_session.refresh(row)
    assert row.desired_state == "running"


@pytest.mark.asyncio
async def test_exact_scheduler_authorization_is_read_only_and_scoped(client, test_session):
    row = await _add_scheduler(test_session)
    _, key = await create_source_client(
        test_session,
        name="exact-probe",
        owner="tests",
        source_name="finlab",
        allowed_datasets=["tw_equity_eod"],
        rate_limit_requests=100,
        rate_limit_window=60,
        commit=False,
    )
    heartbeat = row.last_heartbeat_at
    path = f"/api/v1/source/scheduler-controls/{SCHEDULER_KEY}/authorization"
    stopped = await client.get(path, headers={"X-API-Key": key})
    assert stopped.status_code == 200 and stopped.json()["acquisition_allowed"] is False
    row.desired_state = "running"
    await test_session.flush()
    running = await client.get(path, headers={"X-API-Key": key})
    assert running.status_code == 200 and running.json()["acquisition_allowed"] is True
    assert running.json()["max_age_seconds"] == 5
    assert datetime.fromisoformat(running.json()["checked_at"]) == datetime.fromisoformat(
        running.headers["X-FinDB-Acquisition-Checked-At"]
    )
    await test_session.refresh(row)
    assert row.last_heartbeat_at == heartbeat
    assert (await client.get(path)).status_code == 401
    assert (
        await client.get(
            "/api/v1/source/scheduler-controls/another/authorization", headers={"X-API-Key": key}
        )
    ).status_code == 404


def test_scheduler_dataset_keys_uses_loaded_association_only() -> None:
    row = SchedulerControl(
        scheduler_key=SCHEDULER_KEY,
        provider="finlab",
        slot_id="taiwan_market_window",
    )
    assert scheduler_dataset_keys(row) == []

    row.scheduler_datasets = [
        SchedulerDataset(scheduler_key=SCHEDULER_KEY, dataset_key="association_only")
    ]
    assert scheduler_dataset_keys(row) == ["association_only"]


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
    row, api_key = await create_source_client(
        db,
        name=f"scheduler-{provider}",
        owner="scheduler-tests",
        source_name=provider,
        # Credential creation itself enforces the exact provider mapping.  A
        # deliberately malformed scope is applied only after creation here to
        # exercise scheduler fail-closed behavior for legacy rows.
        allowed_datasets=list(PROVIDER_DATASET_SCOPE[provider]),
        rate_limit_requests=100,
        rate_limit_window=60,
        commit=False,
    )
    if allowed_datasets is not None and set(allowed_datasets) != set(
        PROVIDER_DATASET_SCOPE[provider]
    ):
        row.allowed_datasets = allowed_datasets
        await db.flush()
    return {settings.API_KEY_HEADER: api_key}


@pytest.mark.asyncio
async def test_scheduler_model_exposes_constraints_and_indexes(test_engine):
    constraint_names = {constraint.name for constraint in SchedulerControl.__table__.constraints}
    index_names = {index.name for index in SchedulerControl.__table__.indexes}

    assert "ck_scheduler_control_desired_state_valid" in constraint_names
    assert "ck_scheduler_control_observed_state_valid" in constraint_names
    assert "ck_scheduler_control_revision_positive" in constraint_names
    assert "ck_scheduler_control_slot_id_canonical" in constraint_names
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
async def test_scheduler_admin_patch_rejects_legacy_key(
    client: AsyncClient,
    test_session: AsyncSession,
):
    await _add_scheduler(test_session)
    owner_headers = await _admin_headers(test_session, role="owner")
    response = await client.patch(
        "/api/v1/admin/schedulers/finlab_tw_1430_tw_equity_eod",
        headers=owner_headers,
        json={"desired_state": "running", "expected_revision": 1},
    )
    assert response.status_code == 404


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
    assert legacy_alias.status_code == 404

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


@pytest.mark.asyncio
async def test_current_authorization_rejects_removed_associations_midcycle(client, test_session):
    row = await _add_scheduler(test_session)
    row.desired_state = "running"
    headers = await _source_headers(
        test_session, provider="finlab", allowed_datasets=["tw_equity_eod"]
    )
    path = f"/api/v1/source/scheduler-controls/{SCHEDULER_KEY}"
    assert (await client.get(path + "/authorization", headers=headers)).json()[
        "acquisition_allowed"
    ] is True
    await test_session.execute(
        delete(SchedulerDataset).where(SchedulerDataset.scheduler_key == SCHEDULER_KEY)
    )
    await test_session.flush()
    assert (await client.get(path + "/authorization", headers=headers)).status_code == 403
    assert (
        await client.post(path + "/poll", headers=headers, json={"observed_state": "running"})
    ).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider,scope,status",
    [
        ("finlab", ["tw_equity_eod", "tw_etf_eod"], 200),
        ("finlab", ["tw_equity_eod"], 403),
        ("shioaji", ["tw_equity_minute"], 403),
    ],
)
async def test_authorization_requires_exact_nonempty_complete_provider_scope(
    client, test_session, provider, scope, status
):
    row = await _add_scheduler(test_session, dataset_keys=["tw_equity_eod", "tw_etf_eod"])
    row.desired_state = "running"
    headers = await _source_headers(test_session, provider=provider, allowed_datasets=scope)
    result = await client.get(
        f"/api/v1/source/scheduler-controls/{SCHEDULER_KEY}/authorization", headers=headers
    )
    assert result.status_code == status
    if status == 200:
        assert result.json()["acquisition_allowed"] is True
        assert set(result.json()["dataset_keys"]) == {"tw_equity_eod", "tw_etf_eod"}
