"""Target provisioning and Source → queue → actual-contract Serve pilot acceptance."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
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
    SchedulerDataset,
)
from app.services.normalization_queue import execute_normalization, reconcile_stale_jobs
from app.utils import utc_now
from scripts.provision_registry import (
    RegistryProvisioningError,
    provision_registry,
    provision_taifex_pilot_config,
)
from scripts.reconcile_fetcher_credentials import source_specs_for_profile
from scripts.seed_data import DATASETS, seed_datasets
from scripts.seed_staging_pilot_calendar import seed_staging_pilot_calendar
from tests.test_full_market import _calendar, _ingress, _quote, _source_headers
from tests.test_scheduler_control import _admin_headers


@pytest.mark.parametrize("target", ["staging", "production"])
def test_pilot_catalog_matches_target_source_scope_and_supported_contracts(target: str) -> None:
    catalog = json.loads(
        (
            Path(__file__).resolve().parents[2] / "fetcher/configs/staging_provider_pilots.v1.json"
        ).read_bytes()
    )
    specs = source_specs_for_profile("bounded", target)
    assert {spec.source_name for spec in specs} == (
        set(catalog["providers"]) if target == "staging" else {"twelve_data", "finlab", "shioaji"}
    )
    declarations = {item["dataset_key"]: item["config"] for item in DATASETS}
    for spec in specs:
        assert set(spec.allowed_datasets) == {
            feed["dataset_key"] for feed in catalog["providers"][spec.source_name]["feeds"]
        }
        for feed in catalog["providers"][spec.source_name]["feeds"]:
            config = declarations[feed["dataset_key"]]
            assert config["schema_id"] == feed["schema_id"]
            assert config["allowed_sources"] == [spec.source_name]


def test_stage_futures_governance_exact_reversal_and_operator_fail_closed() -> None:
    neutral = deepcopy(
        next(item["config"] for item in DATASETS if item["dataset_key"] == "tw_futures_eod")
    )
    neutral["operator_note"] = "keep"
    stage = provision_taifex_pilot_config(neutral, deployment_target="staging")
    assert stage["full_market"]["required"] is False
    assert stage["full_market"]["enabled"] is False
    assert provision_taifex_pilot_config(stage, deployment_target="production") == neutral
    custom = deepcopy(neutral)
    custom["full_market"]["enabled"] = True
    assert provision_taifex_pilot_config(custom, deployment_target="production") == custom
    with pytest.raises(RegistryProvisioningError):
        provision_taifex_pilot_config(custom, deployment_target="staging")
    stage["full_market"]["required"] = "false"
    with pytest.raises(RegistryProvisioningError):
        provision_taifex_pilot_config(stage, deployment_target="production")


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
    assert dataset.config["full_market"]["required"] is False
    expectations = dataset.config["delivery_expectation"]
    assert expectations["schedule"]["enabled"] is True
    assert expectations["schedule"]["local_time"] == "18:00:00"
    assert expectations["record_count"]["minimum_record_count"] == 4
    control = await test_session.get(SchedulerControl, "taifex_tw_futures_pilot_v1")
    assert control.desired_state == control.observed_state == "stopped"
    assert (
        await test_session.scalars(
            select(SchedulerDataset.dataset_key).where(
                SchedulerDataset.scheduler_key == control.scheduler_key
            )
        )
    ).all() == ["tw_futures_eod"]
    owner_headers = await _admin_headers(test_session, role="owner")
    started = await client.patch(
        "/api/v1/admin/schedulers/taifex_tw_futures_pilot_v1",
        headers=owner_headers,
        json={"desired_state": "running", "expected_revision": 1},
    )
    assert started.status_code == 200, started.text
    assert started.json()["data"]["desired_state"] == "running"
    assert all(
        row.desired_state == "stopped"
        for row in await test_session.scalars(
            select(SchedulerControl).where(SchedulerControl.scheduler_key.like("%full_market%"))
        )
    )
    for market in ("US", "TW", "HK"):
        await _calendar(test_session, market)
    operator_ids = set(await test_session.scalars(select(CalendarYearRevision.id)))
    calendar = await seed_staging_pilot_calendar(test_session)
    assert calendar["market"] == "TAIFEX"
    assert set(await test_session.scalars(select(CalendarYearRevision.market))) == {
        "US",
        "TW",
        "HK",
        "TAIFEX",
    }
    assert operator_ids <= set(await test_session.scalars(select(CalendarYearRevision.id)))
    assert (await seed_staging_pilot_calendar(test_session))[
        "action"
    ] == "operator_revision_preserved"
    day = date(2026, 10, 2)
    plan = SimpleNamespace(
        dataset_key="tw_futures_eod",
        trade_date=day,
        parts=[SimpleNamespace(work_item_id="taifex_tw_staging_pilot_v1")],
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
    from tests.test_staging_feed_evidence import _load

    collector = _load(
        "taifex_actual_assessment",
        Path(__file__).resolve().parents[2] / "infra/acceptance/collect_staging_feed_evidence.py",
    )
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
    catalog = json.loads(
        (
            Path(__file__).resolve().parents[2] / "fetcher/configs/staging_provider_pilots.v1.json"
        ).read_bytes()
    )
    fetcher = {
        "taifex": {
            "provider": "taifex",
            "recent_trade_dates": [
                {"target_date": day.isoformat(), "status": "completed", "run_id": str(run_id)}
            ],
            "config": {"manifest_sha256": catalog["files"]["taifex_tw_staging_pilot.v1.json"]},
        }
    }
    assert (
        collector._assessment(package, fetcher, observed_at=observed_at)[
            "pilot_functional_acceptance"
        ]["taifex/tw_futures_eod"]
        is True
    )
    assert (
        collector._assessment(
            package, fetcher, observed_at=datetime(2026, 10, 5, 10, tzinfo=timezone.utc)
        )["pilot_functional_acceptance"]["taifex/tw_futures_eod"]
        is False
    )
    await provision_registry(database_url, deployment_target="production")
    await test_session.refresh(dataset)
    assert dataset.is_active is False and dataset.config["full_market"]["required"] is True


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
