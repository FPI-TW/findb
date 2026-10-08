"""Historical admitted fixtures preserve delivery tests without legacy activation."""

from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.models.registry import (
    FullMarketAdmission,
    FullMarketAdmissionFeed,
    FullMarketDatasetState,
    FullMarketEnrollment,
    FullMarketEnvironment,
    SchedulerControl,
    SourceClient,
)
from app.schemas.full_market_readiness import ReadinessDeclaration
from app.services.full_market_admission import declaration_digest
from app.utils import uuid7


def declaration(provider, client_id, datasets, *, environment="local", now=None):
    now = now or datetime.now(timezone.utc)
    return ReadinessDeclaration.model_validate(
        {
            "provider": provider,
            "status": "verified",
            "environment": environment,
            "source_client_id": str(client_id),
            "runtime_id": "fixture-installed",
            "artifact_sha256": "a" * 64,
            "config_sha256": "c" * 64,
            "datasets": datasets,
            "verified_at": (now - timedelta(minutes=1)).isoformat(),
            "expires_at": (now + timedelta(days=10)).isoformat(),
            "evidence_url": "https://example.org/provider-capability",
            "evidence_sha256": "b" * 64,
            "requests_per_day": 1000000,
            "requests_per_minute": 100000,
            "requests_per_second": 1000,
            "bytes_per_day": 10**13,
            "max_response_bytes": 100000,
            "provider_call_seconds": 0.01,
            "source_check_seconds": 0.01,
            "source_requests_per_minute": 100000,
            "completion_window_seconds": 86400,
            "account": {
                "account_id": provider,
                "usage_day": now.date().isoformat(),
                "used_requests": 0,
                "used_bytes": 0,
                "environment_exclusive": True,
                "governor_identity": "/fixture/governor.sqlite3",
                "consumers": ["pilot", "full_market", "historical", "maintenance"],
                "other_requests_per_day": 10,
                "other_bytes_per_day": 1000000,
            },
        }
    )


async def historical_admission(db, dataset, release, day, calendar_id):
    settings = get_settings()
    settings.FULL_MARKET_ENABLED = True
    settings.APP_ENVIRONMENT = "local"
    lifecycle = await db.get(FullMarketEnvironment, "local")
    if lifecycle is None:
        db.add(FullMarketEnvironment(environment="local", enabled=True))
    else:
        lifecycle.enabled = True
    client = SourceClient(
        client_id=uuid7(),
        name="fixture-admitted-source",
        source_name=release.provider,
        key_hash=uuid7().hex + uuid7().hex,
        allowed_datasets=[dataset.dataset_key],
        rate_limit_requests=100000,
        rate_limit_window=60,
    )
    db.add(client)
    await db.flush()
    proof = declaration(release.provider, client.client_id, [dataset.dataset_key]).model_dump(
        mode="json"
    )
    enrollment = FullMarketEnrollment(
        enrollment_id=uuid7(),
        environment="local",
        provider=release.provider,
        source_client_id=client.client_id,
        runtime_id=proof["runtime_id"],
        declaration_sha256=declaration_digest(proof),
        declaration=proof,
        installation_sha256="d" * 64,
        expires_at=datetime.fromisoformat(proof["expires_at"]),
        reported_at=datetime.now(timezone.utc),
    )
    db.add(enrollment)
    key = f"full_market_{release.provider}_v1"
    control = await db.get(SchedulerControl, key)
    if control is None:
        control = SchedulerControl(
            scheduler_key=key,
            provider=release.provider,
            slot_id="taiwan_market_window",
            desired_state="running",
            observed_state="stopped",
            revision=2,
        )
        db.add(control)
    else:
        control.desired_state = "running"
    await db.flush()
    admission = FullMarketAdmission(
        admission_id=uuid7(),
        scheduler_key=key,
        control_revision=control.revision,
        enrollment_id=enrollment.enrollment_id,
        capacity={},
    )
    db.add(admission)
    await db.flush()
    db.add(
        FullMarketAdmissionFeed(
            admission_id=admission.admission_id,
            dataset_key=dataset.dataset_key,
            baseline_id=release.release_id,
            calendar_revision_id=calendar_id,
        )
    )
    db.add(FullMarketDatasetState(dataset_key=dataset.dataset_key, first_start_date=day))
    dataset.is_active = True
    config = dict(dataset.config)
    config["full_market"] = {**config["full_market"], "activation_date": day.isoformat()}
    dataset.config = config
    db.info.update(
        source_client_id=client.client_id,
        source_name=release.provider,
        allowed_datasets=[dataset.dataset_key],
    )
    await db.commit()
