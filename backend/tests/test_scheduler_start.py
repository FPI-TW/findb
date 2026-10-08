"""Trust-bound manual admission, scope freeze, CAS, and lifecycle stops."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.api.deps import AdminPrincipal
from app.config import get_settings
from app.models.registry import (
    AdminAuditEvent,
    DatasetRegistry,
    FullMarketAdmission,
    FullMarketAdmissionFeed,
    FullMarketDatasetState,
    SchedulerControl,
    UniverseRelease,
)
from app.schemas.full_market_readiness import ReadinessReport
from app.services.full_market_admission import (
    admission_projection,
    enroll,
    local_today,
    reconcile_environment,
    report_readiness,
)
from app.services.scheduler_control import (
    SchedulerControlRevisionConflictError,
    SchedulerControlStartBlockedError,
    update_scheduler_desired_state,
)
from app.services.source_clients import create_source_client
from app.utils import utc_now
from scripts.seed_data import seed_datasets
from tests.full_market_fixtures import declaration
from tests.test_full_market import _baseline, _calendar
from tests.test_full_market_governance import RUNTIME_FEEDS, change_runtime_identity

KEY = "full_market_finlab_v1"
PRINCIPAL = AdminPrincipal("user", None, "test-owner", "owner")


async def ready(db, monkeypatch, *, environment="local", scope=None):
    settings = get_settings()
    monkeypatch.setattr(settings, "FULL_MARKET_ENABLED", True)
    monkeypatch.setattr(settings, "APP_ENVIRONMENT", environment)
    await seed_datasets(db)
    await reconcile_environment(db, actor="test-management")
    await _calendar(db, "TW")
    await _baseline(db, "tw_equity_eod", count=2)
    client, _ = await create_source_client(
        db,
        name="ready-source",
        source_name="finlab",
        allowed_datasets=["tw_equity_eod", "tw_etf_eod"],
        rate_limit_requests=100000,
        rate_limit_window=60,
    )
    proof = declaration(
        "finlab",
        client.client_id,
        scope or ["tw_equity_eod", "tw_etf_eod"],
        environment=environment,
    )
    row = await enroll(db, proof, "d" * 64)
    db.info.update(
        source_client_id=client.client_id,
        source_name="finlab",
        allowed_datasets=client.allowed_datasets,
    )
    await report_readiness(
        db,
        ReadinessReport(
            enrollment_id=row.enrollment_id,
            declaration_sha256=row.declaration_sha256,
            runtime_id=row.runtime_id,
            datasets=proof.datasets,
        ),
    )
    return row, proof


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["local", "staging", "production"])
async def test_manual_start_partial_scope_freeze_repeat_and_restart(
    test_session, monkeypatch, environment
):
    row, _ = await ready(test_session, monkeypatch, environment=environment)
    control = await update_scheduler_desired_state(
        test_session,
        scheduler_key=KEY,
        desired_state="running",
        expected_revision=1,
        principal=PRINCIPAL,
    )
    projection = await admission_projection(test_session, "finlab", source=True)
    assert projection["acquisition_allowed"]
    assert projection["admitted_dataset_keys"] == ["tw_equity_eod"]
    first = await test_session.get(FullMarketDatasetState, "tw_equity_eod")
    dataset = await test_session.get(DatasetRegistry, "tw_equity_eod")
    assert first.first_start_date == local_today(dataset)
    before = projection["admission_id"]
    await _baseline(test_session, "tw_etf_eod")
    repeat = await update_scheduler_desired_state(
        test_session,
        scheduler_key=KEY,
        desired_state="running",
        expected_revision=control.revision,
        principal=PRINCIPAL,
    )
    assert repeat.revision == control.revision
    assert (await admission_projection(test_session, "finlab"))["admission_id"] == before
    assert (await admission_projection(test_session, "finlab"))["admitted_dataset_keys"] == [
        "tw_equity_eod"
    ]
    stop = await update_scheduler_desired_state(
        test_session,
        scheduler_key=KEY,
        desired_state="stopped",
        expected_revision=control.revision,
        principal=PRINCIPAL,
    )
    await update_scheduler_desired_state(
        test_session,
        scheduler_key=KEY,
        desired_state="running",
        expected_revision=stop.revision,
        principal=PRINCIPAL,
    )
    assert (await admission_projection(test_session, "finlab"))["admitted_dataset_keys"] == [
        "tw_equity_eod",
        "tw_etf_eod",
    ]
    assert first.first_start_date == local_today(dataset)
    assert await test_session.scalar(select(func.count()).select_from(FullMarketAdmission)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "block", ["flag", "runtime", "expiry", "calendar", "baseline", "capacity", "credential"]
)
async def test_start_blocks_without_freezing_or_auditing(test_session, monkeypatch, block):
    row, proof = await ready(test_session, monkeypatch)
    if block == "flag":
        monkeypatch.setattr(get_settings(), "FULL_MARKET_ENABLED", False)
    elif block == "runtime":
        row.revoked_at = utc_now()
    elif block == "expiry":
        row.expires_at = utc_now() - timedelta(seconds=1)
    elif block == "calendar":
        from app.models.canonical import CalendarYearRevision

        revision = await test_session.scalar(select(CalendarYearRevision))
        revision.status = "draft"
    elif block == "baseline":
        baseline = await test_session.scalar(select(UniverseRelease))
        baseline.status = "candidate"
    elif block == "capacity":
        value = dict(row.declaration)
        value["requests_per_day"] = 1
        row.declaration = value
    elif block == "credential":
        from app.models.registry import SourceClient

        credential = await test_session.get(SourceClient, row.source_client_id)
        credential.revoked_at = utc_now()
    await test_session.flush()
    before = await test_session.scalar(select(func.count()).select_from(AdminAuditEvent))
    with pytest.raises(SchedulerControlStartBlockedError):
        await update_scheduler_desired_state(
            test_session,
            scheduler_key=KEY,
            desired_state="running",
            expected_revision=1,
            principal=PRINCIPAL,
        )
    assert await test_session.scalar(select(func.count()).select_from(FullMarketAdmissionFeed)) == 0
    assert await test_session.scalar(select(func.count()).select_from(AdminAuditEvent)) == before
    control = await test_session.get(SchedulerControl, KEY)
    assert (control.desired_state, control.revision) == ("stopped", 1)


@pytest.mark.asyncio
async def test_flag_off_audited_stop_on_does_not_restart_and_dates_remain(
    test_session, monkeypatch
):
    await ready(test_session, monkeypatch)
    control = await update_scheduler_desired_state(
        test_session,
        scheduler_key=KEY,
        desired_state="running",
        expected_revision=1,
        principal=PRINCIPAL,
    )
    first = await test_session.get(FullMarketDatasetState, "tw_equity_eod")
    first_date = first.first_start_date
    monkeypatch.setattr(get_settings(), "FULL_MARKET_ENABLED", False)
    await reconcile_environment(test_session, actor="test-management")
    assert control.desired_state == "stopped"
    revision = control.revision
    await reconcile_environment(test_session, actor="test-management")
    assert control.revision == revision
    monkeypatch.setattr(get_settings(), "FULL_MARKET_ENABLED", True)
    await reconcile_environment(test_session, actor="test-management")
    assert control.desired_state == "stopped"
    assert first.first_start_date == first_date
    with pytest.raises(SchedulerControlRevisionConflictError):
        await update_scheduler_desired_state(
            test_session,
            scheduler_key=KEY,
            desired_state="running",
            expected_revision=1,
            principal=PRINCIPAL,
        )


@pytest.mark.asyncio
async def test_source_report_cannot_change_digest_scope_or_identity(test_session, monkeypatch):
    row, proof = await ready(test_session, monkeypatch)
    expiry = row.expires_at
    for change in (
        {"declaration_sha256": "f" * 64},
        {"datasets": ["tw_equity_eod"]},
        {"runtime_id": "other-runtime"},
    ):
        from app.services.full_market_admission import AdmissionError

        body = ReadinessReport(
            enrollment_id=row.enrollment_id,
            declaration_sha256=row.declaration_sha256,
            runtime_id=row.runtime_id,
            datasets=proof.datasets,
        ).model_copy(update=change)
        with pytest.raises(AdmissionError):
            await report_readiness(test_session, body)
    assert row.expires_at == expiry


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,key,schema,market,asset,frequency", RUNTIME_FEEDS)
@pytest.mark.parametrize("invalid", ["contract", "market", "asset_class", "frequency"])
async def test_runtime_identity_mismatch_blocks_that_feed(
    test_session, monkeypatch, provider, key, schema, market, asset, frequency, invalid
):
    from app.services.full_market_admission import eligibility

    await seed_datasets(test_session)
    dataset = await test_session.get(DatasetRegistry, key)
    change_runtime_identity(dataset, invalid)
    result = await eligibility(test_session, provider, [dataset])
    assert not result["ready"]
    assert any(reason.startswith("runtime_") for reason in result["feeds"][0]["blockers"])


@pytest.mark.asyncio
async def test_legacy_actions_are_authenticated_owner_only_and_gone(client, test_session):
    from tests.test_scheduler_control import _admin_headers

    for action in ("activate", "deactivate"):
        url = f"/api/v1/admin/feeds/tw_equity_eod/{action}"
        assert (await client.post(url)).status_code == 401
        assert (
            await client.post(url, headers=await _admin_headers(test_session, role="viewer"))
        ).status_code == 403
        response = await client.post(url, headers=await _admin_headers(test_session))
        assert response.status_code == 410
        assert "PATCH" in response.json()["detail"]["guidance"]


@pytest.mark.asyncio
async def test_management_output_fetcher_partial_scope_digest_roundtrip(
    test_session, monkeypatch, tmp_path
):
    import json
    import subprocess
    from pathlib import Path

    from app.schemas.full_market_readiness import ReadinessDeclaration
    from app.services.full_market_admission import declaration_digest
    from scripts.enroll_full_market_runtime import write_enrollment_files

    _, original = await ready(test_session, monkeypatch, scope=["tw_equity_eod"])
    value = original.model_dump(mode="json")
    value.update(
        requests_per_second=3,
        provider_call_seconds=1,
        source_check_seconds=1,
        account_allocation_sha256=None,
    )
    proof = ReadinessDeclaration.model_validate(value)
    row = await enroll(test_session, proof, "d" * 64)
    write_enrollment_files(row, tmp_path)
    source_root = Path(__file__).resolve().parents[2] / "fetcher" / "src"
    # Execute the real Fetcher reader without a backend runtime dependency.
    result = subprocess.run(
        [
            str(Path(__file__).resolve().parents[2] / "fetcher" / ".venv" / "bin" / "python"),
            "-c",
            """
import json,sys
from datetime import datetime,timezone
sys.path.insert(0,sys.argv[1])
from findb_fetcher.full_market_runtime import load_readiness
from findb_fetcher.full_market_universe import canonical_bytes,checksum
from pathlib import Path
proof=load_readiness(Path(sys.argv[2]), 'finlab', ['tw_equity_eod'], now=datetime.now(timezone.utc))
print(json.dumps({'digest':checksum(canonical_bytes(proof)), 'proof':proof}))
""",
            str(source_root),
            str(tmp_path / "finlab.json"),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    loaded = json.loads(result.stdout)
    assert loaded["digest"] == row.declaration_sha256 == declaration_digest(loaded["proof"])
    assert loaded["proof"]["requests_per_second"] == 3.0
    report = ReadinessReport.model_validate_json((tmp_path / "finlab.enrollment.json").read_bytes())
    await report_readiness(test_session, report)
    assert row.reported_at is not None and report.datasets == ["tw_equity_eod"]


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["local", "staging", "production"])
async def test_partial_credential_can_poll_and_start_its_ready_sibling(
    test_session, monkeypatch, environment
):
    from app.models.registry import SourceClient
    from app.services.scheduler_control import poll_scheduler_control

    row, _ = await ready(
        test_session, monkeypatch, environment=environment, scope=["tw_equity_eod"]
    )
    credential = await test_session.get(SourceClient, row.source_client_id)
    credential.allowed_datasets = ["tw_equity_eod"]
    test_session.info["allowed_datasets"] = ["tw_equity_eod"]
    await test_session.flush()
    await update_scheduler_desired_state(
        test_session,
        scheduler_key=KEY,
        desired_state="running",
        expected_revision=1,
        principal=PRINCIPAL,
    )
    observed, _ = await poll_scheduler_control(
        test_session, scheduler_key=KEY, observed_state="running"
    )
    assert observed.desired_state == "running"
    projection = await admission_projection(test_session, "finlab", source=True)
    assert projection["acquisition_allowed"] and projection["admitted_dataset_keys"] == [
        "tw_equity_eod"
    ]


@pytest.mark.asyncio
async def test_baseline_publish_and_start_share_safe_dataset_lock_order(test_engine, monkeypatch):
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.schemas.full_market import UniversePublishRequest
    from app.services.full_market import publish_universe, submit_universe
    from tests.test_full_market import _universe

    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as setup:
        await ready(setup, monkeypatch, scope=["tw_equity_eod"])
        candidate = await submit_universe(
            setup, _universe("tw_equity_eod", count=3), allowed_datasets=["tw_equity_eod"]
        )
        await setup.commit()

    async def publish():
        async with factory() as db:
            await publish_universe(
                db,
                candidate.release_id,
                UniversePublishRequest(
                    threshold_exception_approved=True,
                    evidence_note="Concurrent official universe review.",
                ),
                actor="test-owner",
            )
            await db.commit()

    async def start():
        async with factory() as db:
            return await update_scheduler_desired_state(
                db,
                scheduler_key=KEY,
                desired_state="running",
                expected_revision=1,
                principal=PRINCIPAL,
            )

    _, control = await asyncio.wait_for(asyncio.gather(publish(), start()), timeout=5)
    assert control.desired_state == "running" and control.revision == 2
    async with factory() as db:
        assert (await db.get(UniverseRelease, candidate.release_id)).status == "published"
        assert await db.scalar(select(func.count()).select_from(FullMarketAdmissionFeed)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["all", "ingest"])
async def test_lifecycle_stop_flag_off_and_reopening_remains_stopped(
    test_session, monkeypatch, role
):
    from contextlib import asynccontextmanager

    from app import main
    from app.models.registry import FullMarketEnvironment

    await ready(test_session, monkeypatch, scope=["tw_equity_eod"])
    await update_scheduler_desired_state(
        test_session,
        scheduler_key=KEY,
        desired_state="running",
        expected_revision=1,
        principal=PRINCIPAL,
    )

    async def nothing(*args):
        return None

    @asynccontextmanager
    async def session():
        yield test_session

    monkeypatch.setattr(main, "APP_ROLE", role)
    monkeypatch.setattr(main, "init_db", nothing)
    monkeypatch.setattr(main, "flush_usage", nothing)
    monkeypatch.setattr(main, "async_session_maker", session)
    monkeypatch.setattr(get_settings(), "FULL_MARKET_ENABLED", False)
    async with main.lifespan(main.app):
        assert (await test_session.get(SchedulerControl, KEY)).desired_state == "stopped"
        assert not (await test_session.get(FullMarketEnvironment, "local")).enabled
    monkeypatch.setattr(get_settings(), "FULL_MARKET_ENABLED", True)
    async with main.lifespan(main.app):
        assert (await test_session.get(SchedulerControl, KEY)).desired_state == "stopped"
        assert (await test_session.get(FullMarketEnvironment, "local")).enabled


@pytest.mark.asyncio
async def test_authorization_get_never_stops_or_refreezes_controls(test_session, monkeypatch):
    await ready(test_session, monkeypatch, scope=["tw_equity_eod"])
    control = await update_scheduler_desired_state(
        test_session,
        scheduler_key=KEY,
        desired_state="running",
        expected_revision=1,
        principal=PRINCIPAL,
    )
    monkeypatch.setattr(get_settings(), "FULL_MARKET_ENABLED", False)
    before = await test_session.scalar(select(func.count()).select_from(AdminAuditEvent))
    projection = await admission_projection(test_session, "finlab", source=True)
    assert not projection["acquisition_allowed"]
    assert control.desired_state == "running" and control.revision == 2
    assert await test_session.scalar(select(func.count()).select_from(AdminAuditEvent)) == before
    assert await test_session.scalar(select(func.count()).select_from(FullMarketAdmissionFeed)) == 1


@pytest.mark.asyncio
async def test_source_budget_is_bound_to_actual_client_and_reported_for_permits(
    test_session, monkeypatch
):
    from app.models.registry import SourceClient
    from app.services.full_market_admission import account_authorization, eligibility

    row, proof = await ready(test_session, monkeypatch, scope=["tw_equity_eod"])
    control = await update_scheduler_desired_state(
        test_session,
        scheduler_key=KEY,
        desired_state="running",
        expected_revision=1,
        principal=PRINCIPAL,
    )
    assert control.desired_state == "running"
    original_digest = row.declaration_sha256
    result = await account_authorization(
        test_session, "finlab", "full_market", original_digest, row.runtime_id
    )
    assert result["acquisition_allowed"]
    assert result["effective_acquisition_interval_seconds"] == 0.006
    client = await test_session.get(SourceClient, row.source_client_id)
    client.rate_limit_requests = 60
    await test_session.flush()
    result = await account_authorization(
        test_session, "finlab", "full_market", original_digest, row.runtime_id
    )
    assert not result["acquisition_allowed"]
    datasets = list(
        (
            await test_session.scalars(
                select(DatasetRegistry).where(DatasetRegistry.dataset_key.in_(proof.datasets))
            )
        ).all()
    )
    assert (
        "full_market_source_rate_binding_invalid"
        in (await eligibility(test_session, "finlab", datasets))["blockers"]
    )
    assert row.declaration_sha256 == original_digest


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["local", "staging", "production"])
@pytest.mark.parametrize("backend_enabled,runtime_enabled", [(True, False), (False, True)])
async def test_environment_flag_mismatch_blocks_manual_start_without_get_mutation(
    test_session, monkeypatch, environment, backend_enabled, runtime_enabled
):
    row, proof = await ready(
        test_session, monkeypatch, environment=environment, scope=["tw_equity_eod"]
    )
    await report_readiness(
        test_session,
        ReadinessReport(
            enrollment_id=row.enrollment_id,
            declaration_sha256=row.declaration_sha256,
            runtime_id=row.runtime_id,
            datasets=proof.datasets,
            full_market_enabled=runtime_enabled,
        ),
    )
    monkeypatch.setattr(get_settings(), "FULL_MARKET_ENABLED", backend_enabled)
    result = await admission_projection(test_session, "finlab", source=True)
    expected = (
        "full_market_runtime_flag_mismatch" if backend_enabled else "full_market_flag_disabled"
    )
    assert expected in result["acquisition_blockers"] and not result["acquisition_allowed"]
    with pytest.raises(SchedulerControlStartBlockedError):
        await update_scheduler_desired_state(
            test_session,
            scheduler_key=KEY,
            desired_state="running",
            expected_revision=1,
            principal=PRINCIPAL,
        )
    control = await test_session.get(SchedulerControl, KEY)
    assert control.desired_state == "stopped" and control.revision == 1
    assert row.expires_at == proof.expires_at


@pytest.mark.asyncio
async def test_stale_installed_ack_cannot_admit_until_exact_stopped_runtime_reports(
    test_session, monkeypatch
):
    row, proof = await ready(test_session, monkeypatch, scope=["tw_equity_eod"])
    row.reported_at = utc_now() - timedelta(seconds=91)
    await test_session.flush()
    assert (
        "full_market_installed_runtime_unavailable"
        in (await admission_projection(test_session, "finlab", source=True))["acquisition_blockers"]
    )
    with pytest.raises(SchedulerControlStartBlockedError):
        await update_scheduler_desired_state(
            test_session,
            scheduler_key=KEY,
            desired_state="running",
            expected_revision=1,
            principal=PRINCIPAL,
        )
    expires = row.expires_at
    await report_readiness(
        test_session,
        ReadinessReport(
            enrollment_id=row.enrollment_id,
            declaration_sha256=row.declaration_sha256,
            runtime_id=row.runtime_id,
            datasets=proof.datasets,
            full_market_enabled=True,
        ),
    )
    control = await update_scheduler_desired_state(
        test_session,
        scheduler_key=KEY,
        desired_state="running",
        expected_revision=1,
        principal=PRINCIPAL,
    )
    assert control.desired_state == "running" and row.expires_at == expires


def test_finlab_capacity_includes_cached_table_member_source_work():
    from uuid import UUID

    from app.services.full_market_admission import capacity_proof

    proof = declaration("finlab", UUID(int=1), ["tw_equity_eod", "tw_etf_eod"])
    one, _ = capacity_proof(proof, "finlab", {"tw_equity_eod": 1})
    all_feeds, _ = capacity_proof(proof, "finlab", {"tw_equity_eod": 3000, "tw_etf_eod": 1000})
    assert all_feeds["source_requests"] > one["source_requests"] + 19000
    assert all_feeds["required_seconds"] > one["required_seconds"]
    assert all_feeds["members"] == {"tw_equity_eod": 3000, "tw_etf_eod": 1000}
    assert all_feeds["completion_window_seconds"] == 14400


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [0, False, [], {}, "invalid", "2026-02-30"])
async def test_malformed_legacy_date_blocks_eligibility_start_without_overwrite(
    test_session, monkeypatch, legacy
):
    from copy import deepcopy

    from app.services.full_market_admission import eligibility

    await ready(test_session, monkeypatch, scope=["tw_equity_eod"])
    dataset = await test_session.get(DatasetRegistry, "tw_equity_eod")
    config = deepcopy(dataset.config)
    config["full_market"]["activation_date"] = legacy
    dataset.config = config
    await test_session.flush()
    result = await eligibility(test_session, "finlab", [dataset])
    assert not result["feeds"][0]["ready"]
    assert "full_market_activation_date_invalid" in result["feeds"][0]["blockers"]
    with pytest.raises(SchedulerControlStartBlockedError):
        await update_scheduler_desired_state(
            test_session,
            scheduler_key=KEY,
            desired_state="running",
            expected_revision=1,
            principal=PRINCIPAL,
        )
    assert dataset.config["full_market"]["activation_date"] == legacy
    assert await test_session.get(FullMarketDatasetState, dataset.dataset_key) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("absent", [False, True])
async def test_uninitialized_legacy_date_permits_first_start(test_session, monkeypatch, absent):
    from copy import deepcopy

    await ready(test_session, monkeypatch, scope=["tw_equity_eod"])
    dataset = await test_session.get(DatasetRegistry, "tw_equity_eod")
    config = deepcopy(dataset.config)
    if absent:
        config["full_market"].pop("activation_date", None)
    else:
        config["full_market"]["activation_date"] = None
    dataset.config = config
    await test_session.flush()
    await update_scheduler_desired_state(
        test_session,
        scheduler_key=KEY,
        desired_state="running",
        expected_revision=1,
        principal=PRINCIPAL,
    )
    assert (
        await test_session.get(FullMarketDatasetState, dataset.dataset_key)
    ).first_start_date == local_today(dataset)


@pytest.mark.asyncio
async def test_null_legacy_metadata_cannot_reset_existing_permanent_first_date(
    test_session, monkeypatch
):
    from copy import deepcopy

    await ready(test_session, monkeypatch, scope=["tw_equity_eod"])
    control = await update_scheduler_desired_state(
        test_session,
        scheduler_key=KEY,
        desired_state="running",
        expected_revision=1,
        principal=PRINCIPAL,
    )
    first = await test_session.get(FullMarketDatasetState, "tw_equity_eod")
    cutoff = first.first_start_date
    stopped = await update_scheduler_desired_state(
        test_session,
        scheduler_key=KEY,
        desired_state="stopped",
        expected_revision=control.revision,
        principal=PRINCIPAL,
    )
    dataset = await test_session.get(DatasetRegistry, "tw_equity_eod")
    config = deepcopy(dataset.config)
    config["full_market"]["activation_date"] = None
    dataset.config = config
    await test_session.flush()
    await update_scheduler_desired_state(
        test_session,
        scheduler_key=KEY,
        desired_state="running",
        expected_revision=stopped.revision,
        principal=PRINCIPAL,
    )
    assert (
        await test_session.get(FullMarketDatasetState, dataset.dataset_key)
    ).first_start_date == cutoff
    assert dataset.config["full_market"]["activation_date"] == cutoff.isoformat()


@pytest.mark.parametrize(
    "provider,datasets",
    [
        ("twelve_data", {"hk_equity_eod": 160}),
        ("finlab", {"tw_equity_eod": 160}),
        ("shioaji", {"tw_equity_minute": 160}),
        ("taifex", {"tw_futures_eod": 160}),
    ],
)
def test_capacity_counts_every_member_source_work_independently_of_provider_cache(
    provider, datasets
):
    from uuid import UUID

    from app.services.full_market_admission import capacity_proof

    proof = declaration(provider, UUID(int=1), list(datasets))
    proof.source_requests_per_minute = 2
    result, blockers = capacity_proof(proof, provider, datasets)
    assert result["member_source_requests"] == 768
    assert result["source_requests_per_member"] == 4
    assert result["source_requests"] >= 768 + result["required_requests"] * 8
    assert "full_market_deadline_capacity_insufficient" in blockers
    proof.source_requests_per_minute = 2400
    result, blockers = capacity_proof(proof, provider, datasets)
    assert blockers == []
    assert result["members"] == datasets


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        None,
        "revoked",
        "replaced",
        "expired",
        "stale",
        "wrong-client",
        "wrong-runtime",
        "scope",
        "noncanonical",
    ],
)
async def test_source_current_enrollment_verification_is_readonly_and_independent_of_acquisition(
    client,
    test_session,
    monkeypatch,
    failure,
):
    from app.services.full_market_admission import declaration_digest

    await ready(test_session, monkeypatch, scope=["tw_equity_eod"])
    source_client, key = await create_source_client(
        test_session,
        name="verification-source",
        source_name="finlab",
        allowed_datasets=["tw_equity_eod"],
        rate_limit_requests=100000,
        rate_limit_window=60,
    )
    proof = declaration("finlab", source_client.client_id, ["tw_equity_eod"])
    row = await enroll(test_session, proof, "d" * 64)
    test_session.info.update(
        source_client_id=source_client.client_id,
        source_name="finlab",
        allowed_datasets=["tw_equity_eod"],
    )
    await report_readiness(
        test_session,
        ReadinessReport(
            enrollment_id=row.enrollment_id,
            declaration_sha256=row.declaration_sha256,
            runtime_id=row.runtime_id,
            datasets=proof.datasets,
            full_market_enabled=False,
        ),
    )
    # Flag off and a stopped control must still allow repair-proof verification.
    monkeypatch.setattr(get_settings(), "FULL_MARKET_ENABLED", False)
    control = await test_session.get(SchedulerControl, KEY)
    before = (control.desired_state, control.revision)
    audit_count = await test_session.scalar(select(func.count()).select_from(AdminAuditEvent))
    params = dict(
        enrollment_id=str(row.enrollment_id),
        source_client_id=str(source_client.client_id),
        declaration_sha256=row.declaration_sha256,
        runtime_id=row.runtime_id,
    )
    if failure == "revoked":
        row.revoked_at = utc_now()
    elif failure == "replaced":
        changed = proof.model_copy(update={"runtime_id": "replacement"})
        await enroll(test_session, changed, "e" * 64)
    elif failure == "expired":
        row.expires_at = utc_now() - timedelta(seconds=1)
    elif failure == "stale":
        row.reported_at = utc_now() - timedelta(seconds=91)
    elif failure == "wrong-client":
        from app.utils import uuid7

        params["source_client_id"] = str(uuid7())
    elif failure == "wrong-runtime":
        params["runtime_id"] = "never-installed-runtime"
    elif failure == "scope":
        source_client.allowed_datasets = ["tw_etf_eod"]
    elif failure == "noncanonical":
        row.declaration = {**row.declaration, "requests_per_second": 1000}
        row.declaration_sha256 = declaration_digest(row.declaration)
        params["declaration_sha256"] = row.declaration_sha256
    await test_session.commit()
    assert (
        await client.get("/api/v1/source/full-market/enrollment-verification", params=params)
    ).status_code == 401
    response = await client.get(
        "/api/v1/source/full-market/enrollment-verification",
        params=params,
        headers={"X-API-Key": key},
    )
    assert response.status_code == (200 if failure is None else 403), response.text
    if failure is None:
        verified = response.json()
        assert verified["enrollment_id"] == str(row.enrollment_id)
        assert verified["declaration"] == proof.model_dump(mode="json")
        assert verified["installation_sha256"] == "d" * 64
        assert "acquisition_allowed" not in verified
    await test_session.refresh(control)
    assert (control.desired_state, control.revision) == before
    assert await test_session.scalar(
        select(func.count()).select_from(AdminAuditEvent)
    ) == audit_count + (1 if failure == "replaced" else 0)
