"""Governed starts fail closed while retaining the revision/audit stop protocol."""

import asyncio
from datetime import date

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.deps import AdminPrincipal
from app.models.canonical import CalendarYearRevision
from app.models.registry import AdminAuditEvent, DatasetRegistry, SchedulerControl, UniverseRelease
from app.schemas.full_market import UniversePublishRequest
from app.services.full_market import deactivate_feed, publish_universe, submit_universe
from app.services.scheduler_control import (
    SchedulerControlRevisionConflictError,
    SchedulerControlStartBlockedError,
    get_scheduler_control,
    update_scheduler_desired_state,
)
from app.services.source_clients import PROVIDER_DATASET_SCOPE
from scripts.seed_data import seed_datasets
from tests.test_full_market import DAY, _baseline, _calendar, _universe
from tests.test_full_market_governance import (
    RUNTIME_FEEDS,
    change_runtime_identity,
    runtime_dataset,
)
from tests.test_scheduler_control import _admin_headers

KEY = "full_market_finlab_v1"
PRINCIPAL = AdminPrincipal("user", None, "test-owner", "owner")


async def _assert_runtime_mismatch_blocks_api(client, db, provider, key, enabled, invalid):
    approval_key = (
        key
        if enabled
        else next((sibling for sibling in PROVIDER_DATASET_SCOPE[provider] if sibling != key), key)
    )
    await _approved_partial(db, approval_key)
    dataset = await db.get(DatasetRegistry, key)
    dataset.is_active = enabled
    dataset.config = runtime_dataset(key, enabled).config
    # Disabled invalid siblings must still block an otherwise approved partial scope.
    change_runtime_identity(dataset, invalid)
    await db.flush()
    headers = await _admin_headers(db)
    scheduler_key = f"full_market_{provider}_v1"
    listed = await client.get("/api/v1/admin/schedulers", headers=headers)
    metadata = next(row for row in listed.json()["data"] if row["scheduler_key"] == scheduler_key)
    assert metadata["start_allowed"] is False
    assert metadata["start_blockers"] == ["full_market_configuration_invalid"]
    freshness = await client.get(
        "/api/v1/admin/market-freshness?include_feeds=false", headers=headers
    )
    projection = next(
        row for row in freshness.json()["data"] if row["scheduler_key"] == scheduler_key
    )
    assert projection["configuration_status"] == "error"
    code = "runtime_contract_mismatch" if invalid == "contract" else "runtime_feed_scope_mismatch"
    assert f"{key}:{code}" in projection["configuration_errors"]
    rejected = await client.patch(
        f"/api/v1/admin/schedulers/{scheduler_key}",
        headers=headers,
        json={"desired_state": "running", "expected_revision": 1},
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == {
        "code": "scheduler_start_blocked",
        "start_blockers": ["full_market_configuration_invalid"],
    }
    row = await db.get(SchedulerControl, scheduler_key)
    assert (row.desired_state, row.revision) == ("stopped", 1)
    assert await db.scalar(select(func.count()).select_from(AdminAuditEvent)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,key,schema,market,asset,frequency", RUNTIME_FEEDS)
@pytest.mark.parametrize("enabled", [False, True])
async def test_registered_wrong_runtime_contract_blocks_start_and_projection(
    client, test_session, provider, key, schema, market, asset, frequency, enabled
):
    await _assert_runtime_mismatch_blocks_api(
        client, test_session, provider, key, enabled, "contract"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["market", "asset_class", "frequency"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_self_consistent_wrong_runtime_scope_blocks_start_without_mutation(
    client, test_session, enabled, invalid
):
    await _assert_runtime_mismatch_blocks_api(
        client, test_session, "finlab", "tw_equity_eod", enabled, invalid
    )


async def _approved_partial(db, dataset_key="tw_equity_eod"):
    await seed_datasets(db)
    dataset = await db.get(DatasetRegistry, dataset_key)
    await _calendar(db, "TAIFEX" if dataset_key == "tw_futures_eod" else dataset.market)
    if dataset_key == "tw_etf_minute":
        body = _universe(dataset_key)
        body = body.model_copy(
            update={
                "members": [
                    member.model_copy(update={"asset_class": "etf"}) for member in body.members
                ]
            }
        )
        release = await submit_universe(db, body, allowed_datasets=[dataset_key])
        await publish_universe(
            db,
            release.release_id,
            UniversePublishRequest(
                evidence_note="Verified official first baseline.", first_baseline_approved=True
            ),
            actor="test-owner",
        )
    else:
        await _baseline(db, dataset_key)
    dataset.is_active = True
    dataset.config = {
        **dataset.config,
        "full_market": {
            **dataset.config["full_market"],
            "enabled": True,
            "readiness_approved": True,
            "mode": "acceptance",
            "activation_date": date(DAY.year, 12, 31).isoformat(),
        },
    }
    await db.commit()


@pytest.mark.asyncio
async def test_zero_enabled_start_rejects_without_mutation_and_stop_repairs(client, test_session):
    await seed_datasets(test_session)
    headers = await _admin_headers(test_session)
    listed = await client.get("/api/v1/admin/schedulers", headers=headers)
    data = next(row for row in listed.json()["data"] if row["scheduler_key"] == KEY)
    assert data["start_allowed"] is False
    assert data["start_blockers"] == ["full_market_no_enabled_datasets"]
    rejected = await client.patch(
        f"/api/v1/admin/schedulers/{KEY}",
        headers=headers,
        json={"desired_state": "running", "expected_revision": 1},
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"] == {
        "code": "scheduler_start_blocked",
        "start_blockers": ["full_market_no_enabled_datasets"],
    }
    row = await test_session.get(SchedulerControl, KEY)
    assert (row.desired_state, row.revision) == ("stopped", 1)
    assert await test_session.scalar(select(func.count()).select_from(AdminAuditEvent)) == 0
    # Persisted invalid running controls can always be stopped through the normal API.
    row.desired_state = "running"
    dataset = await test_session.get(DatasetRegistry, "tw_etf_eod")
    dataset.config = {"full_market": "malformed"}
    await test_session.flush()
    stopped = await client.patch(
        f"/api/v1/admin/schedulers/{KEY}",
        headers=headers,
        json={"desired_state": "stopped", "expected_revision": 1},
    )
    assert stopped.status_code == 200
    assert stopped.json()["data"]["revision"] == 2
    assert stopped.json()["data"]["start_allowed"] is False
    assert await test_session.scalar(select(func.count()).select_from(AdminAuditEvent)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,key,schema,market,asset,frequency", RUNTIME_FEEDS)
async def test_partial_acceptance_start_allows_future_activation_and_disabled_sibling(
    client, test_session, provider, key, schema, market, asset, frequency
):
    await _approved_partial(test_session, key)
    for sibling_key in PROVIDER_DATASET_SCOPE[provider]:
        if sibling_key != key:
            sibling = await test_session.get(DatasetRegistry, sibling_key)
            assert sibling.config["full_market"]["enabled"] is False
    headers = await _admin_headers(test_session)
    changed = await client.patch(
        f"/api/v1/admin/schedulers/full_market_{provider}_v1",
        headers=headers,
        json={"desired_state": "running", "expected_revision": 1},
    )
    assert changed.status_code == 200
    assert changed.json()["data"]["start_allowed"] is True
    assert changed.json()["data"]["start_blockers"] == []
    assert changed.json()["data"]["revision"] == 2
    assert changed.json()["data"]["last_heartbeat_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("enabled", "true"),
        ("readiness_approved", "false"),
        ("activation_date", "bad-date"),
        ("mode", "invalid"),
        ("allowed_sources", ["twelve_data"]),
        ("defaults", {"market": "US", "asset_class": "etf", "currency": "TWD"}),
        ("schema_id", "unknown"),
    ],
)
async def test_malformed_disabled_sibling_blocks_start(test_session, field, value):
    await _approved_partial(test_session)
    sibling = await test_session.get(DatasetRegistry, "tw_etf_eod")
    config = dict(sibling.config)
    if field in {"enabled", "readiness_approved", "activation_date", "mode"}:
        config["full_market"] = {**config["full_market"], field: value}
    else:
        config[field] = value
    sibling.config = config
    await test_session.flush()
    with pytest.raises(SchedulerControlStartBlockedError):
        await update_scheduler_desired_state(
            test_session,
            scheduler_key=KEY,
            desired_state="running",
            expected_revision=1,
            principal=PRINCIPAL,
        )
    row = await test_session.get(SchedulerControl, KEY)
    assert (row.desired_state, row.revision) == ("stopped", 1)


@pytest.mark.asyncio
async def test_full_market_start_preserves_auth_revision_and_scope(client, test_session):
    await seed_datasets(test_session)
    anonymous = await client.patch(
        f"/api/v1/admin/schedulers/{KEY}",
        json={"desired_state": "running", "expected_revision": 1},
    )
    assert anonymous.status_code == 401
    viewer = await _admin_headers(test_session, role="viewer")
    denied = await client.patch(
        f"/api/v1/admin/schedulers/{KEY}",
        headers=viewer,
        json={"desired_state": "running", "expected_revision": 1},
    )
    assert denied.status_code == 403
    headers = await _admin_headers(test_session)
    conflict = await client.patch(
        f"/api/v1/admin/schedulers/{KEY}",
        headers=headers,
        json={"desired_state": "running", "expected_revision": 0},
    )
    assert conflict.status_code == 409
    assert "revision" in conflict.json()["detail"]
    row = await test_session.get(SchedulerControl, KEY)
    row.provider = "twelve_data"
    await test_session.flush()
    denied_scope = await client.patch(
        f"/api/v1/admin/schedulers/{KEY}",
        headers=headers,
        json={"desired_state": "running", "expected_revision": 1},
    )
    assert denied_scope.json()["detail"]["start_blockers"] == ["full_market_scope_invalid"]


@pytest.mark.asyncio
async def test_start_waits_for_deactivation_and_refreshes_cached_rows(test_engine, test_session):
    await _approved_partial(test_session)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as start_db, factory() as deactivate_db:
        # Cache both rows before another transaction changes their governance/revision.
        cached_dataset = await start_db.get(DatasetRegistry, "tw_equity_eod")
        cached_control = await get_scheduler_control(start_db, KEY)
        await deactivate_db.scalar(
            select(DatasetRegistry)
            .where(DatasetRegistry.dataset_key == "tw_equity_eod")
            .with_for_update()
        )
        started = asyncio.Event()

        async def start():
            started.set()
            return await update_scheduler_desired_state(
                start_db,
                scheduler_key=KEY,
                desired_state="running",
                expected_revision=1,
                principal=PRINCIPAL,
            )

        task = asyncio.create_task(start())
        try:
            await started.wait()
            # The second transaction must wait on the dataset lock, not acquire control first.
            await asyncio.sleep(0.05)
            assert not task.done()
            await asyncio.wait_for(deactivate_feed(deactivate_db, "tw_equity_eod"), timeout=5)
            await deactivate_db.commit()
            with pytest.raises(SchedulerControlRevisionConflictError):
                await asyncio.wait_for(task, timeout=5)
            with pytest.raises(SchedulerControlStartBlockedError):
                await update_scheduler_desired_state(
                    start_db,
                    scheduler_key=KEY,
                    desired_state="running",
                    expected_revision=2,
                    principal=PRINCIPAL,
                )
            assert cached_control.revision == 2
            assert cached_dataset.config["full_market"]["enabled"] is False
            await start_db.rollback()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        row = await test_session.get(SchedulerControl, KEY, populate_existing=True)
        assert (row.desired_state, row.revision) == ("stopped", 2)


@pytest.mark.asyncio
async def test_start_refreshes_governance_after_concurrent_registry_update(
    test_engine, test_session
):
    await _approved_partial(test_session)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as start_db, factory() as registry_db:
        cached_dataset = await start_db.get(DatasetRegistry, "tw_equity_eod")
        dataset = await registry_db.scalar(
            select(DatasetRegistry)
            .where(DatasetRegistry.dataset_key == "tw_equity_eod")
            .with_for_update()
        )
        dataset.config = {
            **dataset.config,
            "full_market": {**dataset.config["full_market"], "enabled": False},
        }
        await registry_db.flush()
        task = asyncio.create_task(
            update_scheduler_desired_state(
                start_db,
                scheduler_key=KEY,
                desired_state="running",
                expected_revision=1,
                principal=PRINCIPAL,
            )
        )
        try:
            await asyncio.sleep(0.05)
            assert not task.done()
            await registry_db.commit()
            with pytest.raises(SchedulerControlStartBlockedError):
                await asyncio.wait_for(task, timeout=5)
            assert cached_dataset.config["full_market"]["enabled"] is False
            row = await start_db.get(SchedulerControl, KEY)
            assert (row.desired_state, row.revision) == ("stopped", 1)
            assert await start_db.scalar(select(func.count()).select_from(AdminAuditEvent)) == 0
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["baseline", "calendar"])
async def test_enabled_feed_requires_baseline_and_calendar(test_session, missing):
    await _approved_partial(test_session)
    if missing == "baseline":
        release = await test_session.scalar(select(UniverseRelease))
        release.status = "candidate"
        await test_session.flush()
    else:
        await test_session.execute(delete(CalendarYearRevision))
    with pytest.raises(SchedulerControlStartBlockedError) as blocked:
        await update_scheduler_desired_state(
            test_session,
            scheduler_key=KEY,
            desired_state="running",
            expected_revision=1,
            principal=PRINCIPAL,
        )
    assert blocked.value.blockers == [f"full_market_{missing}_missing"]


@pytest.mark.asyncio
async def test_partial_scope_ignores_disabled_sibling_calendar(test_session):
    await _approved_partial(test_session, "hk_equity_eod")
    sibling = await test_session.get(DatasetRegistry, "us_equity_eod")
    sibling.is_active = False
    await test_session.flush()
    assert (
        await test_session.scalar(
            select(CalendarYearRevision.id).where(CalendarYearRevision.market == "US")
        )
        is None
    )
    row = await update_scheduler_desired_state(
        test_session,
        scheduler_key="full_market_twelve_data_v1",
        desired_state="running",
        expected_revision=1,
        principal=PRINCIPAL,
    )
    assert (row.desired_state, row.revision) == ("running", 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("is_active", False),
        ("readiness_approved", False),
        ("mode", "off"),
        ("activation_date", None),
    ],
)
async def test_enabled_feed_requires_all_activation_prerequisites(test_session, field, value):
    await _approved_partial(test_session)
    dataset = await test_session.get(DatasetRegistry, "tw_equity_eod")
    if field == "is_active":
        dataset.is_active = value
    else:
        dataset.config = {
            **dataset.config,
            "full_market": {**dataset.config["full_market"], field: value},
        }
    await test_session.flush()
    with pytest.raises(SchedulerControlStartBlockedError) as blocked:
        await update_scheduler_desired_state(
            test_session,
            scheduler_key=KEY,
            desired_state="running",
            expected_revision=1,
            principal=PRINCIPAL,
        )
    assert blocked.value.blockers == ["full_market_configuration_invalid"]
    row = await test_session.get(SchedulerControl, KEY)
    assert (row.desired_state, row.revision) == ("stopped", 1)
