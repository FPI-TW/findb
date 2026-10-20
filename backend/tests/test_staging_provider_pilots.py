"""Target provisioning and Source → queue → actual-contract Serve pilot acceptance."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.models.canonical import CalendarYearRevision, FuturesContractEOD
from app.models.registry import (
    DatasetRegistry,
    IngestionRun,
    NormalizationJob,
    NormalizationOutbox,
    SchedulerControl,
)
from app.services.normalization_queue import execute_normalization, reconcile_stale_jobs
from app.utils import utc_now
from scripts.provision_registry import (
    provision_registry,
    provision_taifex_pilot_config,
)
from scripts.reconcile_fetcher_credentials import source_specs_for_profile
from scripts.seed_data import DATASETS, seed_datasets
from tests.test_full_market import _calendar, _ingress, _quote, _source_headers


@pytest.mark.parametrize("target", ["staging", "production"])
def test_shared_feed_catalog_matches_source_scope_and_supported_contracts(target):
    catalog = json.loads(
        (Path(__file__).resolve().parents[1] / "configs/execution_feeds.v1.json").read_text()
    )
    specs = source_specs_for_profile("unified", target)
    declarations = {item["dataset_key"]: item["config"] for item in DATASETS}
    assert len(catalog["feeds"]) == 7
    for spec in specs:
        acquisition = {
            feed["dataset_key"] for feed in catalog["feeds"] if feed["provider"] == spec.source_name
        }
        retained = {"hk_equity_eod"} if spec.source_name == "twelve_data" else set()
        assert set(spec.allowed_datasets) == acquisition | retained
        for key in spec.allowed_datasets:
            assert spec.source_name in declarations[key]["allowed_sources"]


def test_futures_provisioning_has_same_policy_in_both_environments():
    neutral = deepcopy(
        next(item["config"] for item in DATASETS if item["dataset_key"] == "tw_futures_eod")
    )
    neutral["operator_note"] = "keep"
    for target in ("staging", "production"):
        assert provision_taifex_pilot_config(neutral, deployment_target=target) == neutral
        custom = deepcopy(neutral)
        custom["full_market"]["enabled"] = True
        assert provision_taifex_pilot_config(custom, deployment_target=target) == custom


@pytest.mark.parametrize("recovery", ["none", "published_lost", "expired_lease"])
@pytest.mark.asyncio
async def test_provisioned_stage_taifex_source_queue_canonical_serve_and_duplicate(
    client, test_session, test_engine, monkeypatch, recovery
) -> None:
    await seed_datasets(test_session)
    database_url = test_engine.url.render_as_string(hide_password=False)
    await provision_registry(database_url, deployment_target="staging")
    dataset = await test_session.get(DatasetRegistry, "tw_futures_eod")
    await test_session.refresh(dataset)
    assert dataset.is_active is True
    assert dataset.config["full_market"]["enabled"] is False
    assert dataset.config["full_market"]["required"] is True
    control = await test_session.get(SchedulerControl, "pilot_taifex_v1")
    assert control.desired_state == control.observed_state == "stopped"
    day = date(2026, 10, 2)
    await _calendar(test_session, "TAIFEX", [day])
    from app.schemas.full_market import (
        DeliveryPlanCreateRequest,
        UniversePublishRequest,
        UniverseSubmitRequest,
    )
    from app.services.full_market import create_plan, publish_universe, submit_universe
    from tests.full_market_fixtures import historical_admission
    from tests.test_full_market import _universe

    body = _universe(effective_date=day).model_dump(mode="json")
    body["members"] = [
        dict(
            symbol=product,
            provider_symbol=product + "202610",
            exchange="TAIFEX",
            currency="TWD",
            asset_class="future",
            product_code=product,
            contract_code=product + ":202610",
            contract_month="202610",
            sessions=["regular", "after_hours"],
        )
        for product in ("TX", "MTX")
    ]
    release = await submit_universe(
        test_session,
        UniverseSubmitRequest.model_validate(body),
        allowed_datasets=[dataset.dataset_key],
    )
    await publish_universe(
        test_session,
        release.release_id,
        UniversePublishRequest(
            evidence_note="Reviewed fixture baseline", first_baseline_approved=True
        ),
        actor="test-owner",
    )
    revision = await test_session.scalar(select(CalendarYearRevision))
    from app.models.registry import UniverseRelease

    release_row = await test_session.get(UniverseRelease, release.release_id)
    await historical_admission(
        test_session, dataset, release_row, day, revision.id, profile="pilot"
    )
    plan = await create_plan(
        test_session,
        DeliveryPlanCreateRequest(
            version=1,
            execution_profile="pilot",
            dataset_key=dataset.dataset_key,
            provider="taifex",
            trade_date=day,
            release_id=release.release_id,
        ),
        allowed_datasets=[dataset.dataset_key],
    )
    rows = [
        {
            **_quote(month="202610", session=session, trade_date=day),
            "product_code": product,
            "contract_code": f"{product}:202610",
        }
        for product in ("TX", "MTX")
        for session in ("regular", "after_hours")
    ]
    request = _ingress(plan, rows)
    headers = await _source_headers(test_session)
    response = await client.post("/api/v1/source/ingest", headers=headers, json=request)
    assert response.status_code == 202, response.text
    run_id = UUID(response.json()["run_id"])
    job = (
        await test_session.scalars(
            select(NormalizationJob).where(NormalizationJob.run_id == run_id)
        )
    ).one()
    original_delivery = job.delivery_id
    if recovery != "none":
        outbox = (
            await test_session.scalars(
                select(NormalizationOutbox).where(NormalizationOutbox.run_id == run_id)
            )
        ).one()
        outbox.status = "published"
        outbox.published_at = utc_now() - timedelta(days=1)
        if recovery == "expired_lease":
            job.status = "processing"
            job.lease_expires_at = utc_now() - timedelta(seconds=1)
        await test_session.commit()
        assert await reconcile_stale_jobs(test_session) == 1
        await test_session.refresh(job)
        assert job.delivery_id != original_delivery
    await execute_normalization(run_id, job.delivery_id, database_url=database_url)
    run = await test_session.get(IngestionRun, run_id)
    await test_session.refresh(run)
    assert run.status == "completed" and run.total_records == run.success_records == 4
    assert await test_session.scalar(select(func.count()).select_from(FuturesContractEOD)) == 4
    from scripts.export_staging_feed_evidence import FUTURES_SAMPLE, RUNS

    evidence = (await test_session.execute(RUNS)).mappings().one()
    assert len(evidence["outbox_receipts"]) == (1 if recovery == "none" else 2)
    assert evidence["job_delivery_id"] == str(job.delivery_id)
    assert {item["delivery_id"] for item in evidence["outbox_receipts"]} == {
        str(original_delivery),
        str(job.delivery_id),
    }
    assert len(evidence["canonical_coverage"]) == 4
    assert evidence["canonical_rows"] == 4 and evidence["raw_persisted"] is True
    sample = (await test_session.execute(FUTURES_SAMPLE)).mappings().one()
    assert sample["sample_run_id"] == str(run_id)
    assert sample["sample_contract_code"] in {"TX:202610", "MTX:202610"}
    for product in ("TX", "MTX"):
        served = await client.get(
            "/api/v1/serve/futures/eod", params={"contract_code": f"{product}:202610"}
        )
        assert served.status_code == 200
        assert {item["session"] for item in served.json()["data"]} == {"regular", "after_hours"}
    duplicate = await client.post("/api/v1/source/ingest", headers=headers, json=request)
    assert duplicate.status_code == 202 and duplicate.json()["run_id"] == str(run_id)
    # A durable prepared retry / restart may POST the same payload after acceptance.
    # The exporter must keep both receipts while projecting canonical coverage once.
    evidence = dict((await test_session.execute(RUNS)).mappings().one())
    assert len(evidence["canonical_coverage"]) == 4
    assert {item["status"] for item in evidence["attempt_receipts"]} == {"accepted", "duplicate"}
    assert {item["run_id"] for item in evidence["attempt_receipts"]} == {str(run_id)}
    assert len({item["request_sha256"] for item in evidence["attempt_receipts"]}) == 1
    from contextlib import asynccontextmanager
    from io import BytesIO

    from scripts import export_staging_feed_evidence as exporter

    observed_at = datetime(2026, 10, 3, 10, tzinfo=timezone.utc)
    sample = dict(sample)
    served = await client.get(
        "/api/v1/serve/futures/eod",
        params={
            "instrument_id": sample["sample_instrument_id"],
            "contract_code": sample["sample_contract_code"],
            "session": sample["sample_session"],
            "start_date": day.isoformat(),
            "end_date": day.isoformat(),
            "page_size": 1,
        },
    )
    assert served.status_code == 200

    class HTTPResponse(BytesIO):
        status = 200

    def urlopen(request, timeout):
        assert sample["sample_contract_code"].replace(":", "%3A") in request.full_url
        return HTTPResponse(served.content)

    @asynccontextmanager
    async def session_maker():
        yield test_session

    monkeypatch.setattr(exporter, "async_session_maker", session_maker)
    monkeypatch.setattr(exporter, "urlopen", urlopen)
    monkeypatch.setenv("FINDB_STATIC_CACHE_SERVE_API_KEY", "fixture-key")
    package = await exporter.export(observed_at=observed_at)
    feed = next(item for item in package["feeds"] if item["source"] == "taifex")
    assert len(feed["runs"]) == 1 and feed["serve_probe"]["success"] is True
    assert feed["runs"][0]["canonical_rows"] == 4
    assert (
        feed["serve_probe"]["returned_fingerprint"] == feed["serve_probe"]["canonical_fingerprint"]
    )
    from app.services.full_market import get_plan

    completed = await get_plan(
        test_session, plan.plan_id, provider="taifex", allowed_datasets=[dataset.dataset_key]
    )
    assert completed.execution_profile == "pilot" and completed.status == "complete"
    assert completed.summary.data == 4
    await provision_registry(database_url, deployment_target="production")
    await test_session.refresh(dataset)
    assert dataset.is_active is True and dataset.config["full_market"]["required"] is True


@pytest.mark.parametrize("schema", ["market_eod", "market_minute"])
@pytest.mark.asyncio
async def test_actual_market_pipeline_serve_provenance(
    client, test_session, test_engine, monkeypatch, schema
):
    from io import BytesIO

    from scripts import export_staging_feed_evidence as exporter
    from tests.test_canonical_ingest_api import _canonical_request, _dataset_config
    from tests.test_minute_normalize import _contract_config, _minute_request
    from tests.test_staging_feed_evidence import _load

    is_minute = schema == "market_minute"
    dataset_key = "tw_equity_minute" if is_minute else "tw_equity_eod"
    provider = "shioaji" if is_minute else "finlab"
    config = _contract_config("equity") if is_minute else _dataset_config()
    config["schema_enforcement"] = "enforce"
    test_session.add(
        DatasetRegistry(
            dataset_key=dataset_key,
            name=dataset_key,
            asset_class="equity",
            market="TW",
            frequency="minute" if is_minute else "daily",
            is_active=True,
            config=config,
        )
    )
    headers = await _source_headers(test_session, provider=provider, datasets=[dataset_key])
    await test_session.commit()
    request = _minute_request(dataset_key) if is_minute else _canonical_request()
    if is_minute:
        second = {
            **request["payload"]["data"][0],
            "bar_start_time": "2026-07-30T09:01:00+08:00",
            "bar_end_time": "2026-07-30T09:02:00+08:00",
            "signal_time": "2026-07-30T09:02:00+08:00",
        }
        request["payload"]["data"].append(second)
        request["payload"]["batch"]["declared_record_count"] = 2
    accepted = await client.post("/api/v1/source/ingest", headers=headers, json=request)
    assert accepted.status_code == 202, accepted.text
    run_id = UUID(accepted.json()["run_id"])
    job = (
        await test_session.scalars(
            select(NormalizationJob).where(NormalizationJob.run_id == run_id)
        )
    ).one()
    await execute_normalization(
        run_id, job.delivery_id, database_url=test_engine.url.render_as_string(hide_password=False)
    )
    sample = next(
        dict(item)
        for item in (await test_session.execute(exporter.SAMPLES)).mappings()
        if item["dataset_key"] == dataset_key
    )
    assert sample["sample_run_id"] == str(run_id)
    if is_minute:
        assert "01:01:00" in sample["sample_record"]["bar_start_time"]
    served = await client.get(
        "/api/v1/serve/minute" if is_minute else "/api/v1/serve/eod",
        params={
            "instrument_id": sample["sample_instrument_id"],
            "start_date": sample["sample_trade_date"],
            "end_date": sample["sample_trade_date"],
            "page_size": 1000 if is_minute else 1,
        },
    )
    assert served.status_code == 200, served.text

    class Response(BytesIO):
        status = 200

    monkeypatch.setenv("FINDB_STATIC_CACHE_SERVE_API_KEY", "fixture-key")
    monkeypatch.setattr(exporter, "urlopen", lambda *args, **kwargs: Response(served.content))
    proof = exporter._serve_probe(schema, sample)
    assert proof["success"] is True and proof["run_id"] == str(run_id)
    collector = _load(
        "actual_market_proof",
        Path(__file__).resolve().parents[2] / "infra/acceptance/collect_staging_feed_evidence.py",
    )
    feed = {"schema_id": schema, "canonical_sample": sample, "serve_probe": proof}
    assert (
        collector._valid_serve_proof(feed, provider, sample["sample_trade_date"], {str(run_id)})
        is True
    )
    # Real HTTP responses with the same key cannot borrow the DB run binding.
    for field, invalid in [
        ("source", "other_provider"),
        ("source_fetched_at", "2026-07-30T02:00:01Z"),
        ("close", "999"),
    ]:
        altered = served.json()
        for item in altered["data"]:
            item[field] = invalid
        monkeypatch.setattr(
            exporter, "urlopen", lambda *args, **kwargs: Response(json.dumps(altered).encode())
        )
        rejected = exporter._serve_probe(schema, sample)
        assert rejected["success"] is False and rejected["run_id"] is None


def test_provision_preserves_first_start_and_registry_migration_marker():
    config = deepcopy(
        next(item["config"] for item in DATASETS if item["dataset_key"] == "tw_futures_eod")
    )
    config["full_market"].update(
        activation_date="2026-10-01", _registry_active_before_2442dc84beae=False
    )
    staged = provision_taifex_pilot_config(config, deployment_target="staging")
    restored = provision_taifex_pilot_config(staged, deployment_target="production")
    assert restored == config
    assert "readiness_approved" not in restored["full_market"]
