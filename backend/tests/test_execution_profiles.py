"""Profile membership, canonical evidence and monitoring must never cross scopes."""

from uuid import UUID

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.execution_profiles import scheduler_key
from app.models.canonical import CalendarYearRevision, FuturesContractEOD
from app.models.registry import DeliveryMemberEvidence, MissingDeliveryAlert, SchedulerControl
from app.schemas.full_market import DeliveryPlanCreateRequest
from app.services.full_market import create_plan, get_plan
from app.services.normalize.contracts import FuturesEODContractNormalizer
from tests.full_market_fixtures import historical_admission
from tests.test_full_market import DAY, _activated_plan, _ingress, _quote, _source_headers


async def _pilot(db, dataset, body, full):
    revision = await db.scalar(
        select(CalendarYearRevision).where(
            CalendarYearRevision.market
            == ("TAIFEX" if dataset.dataset_key == "tw_futures_eod" else dataset.market)
        )
    )
    from app.models.registry import UniverseRelease

    release = await db.get(UniverseRelease, full.release_id)
    await historical_admission(db, dataset, release, DAY, revision.id, profile="pilot")
    return await create_plan(
        db,
        DeliveryPlanCreateRequest(
            version=1,
            execution_profile="pilot",
            dataset_key=dataset.dataset_key,
            provider=body.provider,
            trade_date=DAY,
            release_id=full.release_id,
        ),
        allowed_datasets=[dataset.dataset_key],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    [
        "us_equity_eod",
        "hk_equity_eod",
        "tw_equity_eod",
        "tw_etf_eod",
        "tw_equity_minute",
        "tw_etf_minute",
        "tw_futures_eod",
    ],
)
@pytest.mark.parametrize("count", [1, 3])
async def test_seven_feed_profiles_freeze_distinct_plans(test_session, key, count):
    dataset, body, full = await _activated_plan(test_session, key, count=count)
    pilot = await _pilot(test_session, dataset, body, full)
    multiplier = 2 if key == "tw_futures_eod" else 1
    assert full.summary.expected == count * multiplier
    assert pilot.summary.expected == min(count, 2) * multiplier
    assert pilot.plan_id != full.plan_id
    members = [member for part in pilot.parts for member in part.member_keys]
    assert (
        members
        == sorted(member for part in full.parts for member in part.member_keys)[
            : min(count, 2) * multiplier
        ]
    )
    assert pilot.execution_profile == pilot.summary.execution_profile == "pilot"
    get_settings().FULL_MARKET_ENABLED = False
    # The flag cannot disable pilot nor change an already frozen plan on retry.
    repeated = await create_plan(
        test_session,
        DeliveryPlanCreateRequest(
            version=1,
            execution_profile="pilot",
            dataset_key=key,
            provider=body.provider,
            trade_date=DAY,
            release_id=full.release_id,
        ),
        allowed_datasets=[key],
    )
    assert repeated == pilot


@pytest.mark.asyncio
async def test_canonical_overwrite_preserves_only_own_plan_evidence(
    test_session, client, admin_headers
):
    dataset, body, full = await _activated_plan(test_session, count=1)
    pilot = await _pilot(test_session, dataset, body, full)
    headers = await _source_headers(test_session)
    run_ids = []
    for plan in (pilot, full):
        request = _ingress(plan, [_quote(), _quote(session="after_hours")])
        request["idempotency_key"] = request["request_key"] = str(plan.plan_id)
        response = await client.post("/api/v1/source/ingest", headers=headers, json=request)
        assert response.status_code == 202, response.text
        run_id = UUID(response.json()["run_id"])
        run_ids.append(run_id)
        normalizer = FuturesEODContractNormalizer(
            test_session,
            {
                **dataset.config,
                "_ingest_source": "taifex",
                "_ingest_fetched_at": request["fetched_at"],
            },
        )
        assert (await normalizer.process(request["payload"], run_id)).success_records == 2
        await test_session.commit()
        result = await get_plan(
            test_session, pilot.plan_id, provider="taifex", allowed_datasets=[dataset.dataset_key]
        )
        assert result.status == "complete"
        other = await get_plan(
            test_session, full.plan_id, provider="taifex", allowed_datasets=[dataset.dataset_key]
        )
        assert other.status == ("incomplete" if plan == pilot else "complete")
    assert set(await test_session.scalars(select(FuturesContractEOD.run_id))) == {run_ids[1]}
    assert set(await test_session.scalars(select(DeliveryMemberEvidence.run_id))) == set(run_ids)
    for profile, expected in (("pilot", pilot), ("full_market", full)):
        response = await client.get(
            "/api/v1/admin/delivery-plans",
            params={"execution_profile": profile},
            headers=admin_headers,
        )
        assert response.status_code == 200, response.text
        assert [item["plan_id"] for item in response.json()["data"]] == [str(expected.plan_id)]
    default = await client.get("/api/v1/admin/delivery-plans", headers=admin_headers)
    assert [item["plan_id"] for item in default.json()["data"]] == [str(full.plan_id)]


@pytest.mark.asyncio
async def test_outcomes_and_missing_alerts_are_profile_isolated(
    test_session, client, admin_headers
):
    from datetime import timedelta

    from app.models.registry import DailyDeliveryMember
    from app.schemas.full_market import DeliveryOutcomeRequest
    from app.services.delivery_monitor import scan_missing_deliveries
    from app.services.full_market import record_outcome
    from app.utils import utc_now
    from tests.test_full_market import EVIDENCE

    dataset, body, full = await _activated_plan(test_session, count=1)
    pilot = await _pilot(test_session, dataset, body, full)
    for member in await test_session.scalars(
        select(DailyDeliveryMember).where(DailyDeliveryMember.plan_id == pilot.plan_id)
    ):
        await record_outcome(
            test_session,
            pilot.plan_id,
            DeliveryOutcomeRequest(
                version=1,
                work_item_id=pilot.parts[0].work_item_id,
                member_key=member.member_key,
                outcome="no_data",
                reason="no_trade",
                evidence={
                    **EVIDENCE,
                    "observed_at": utc_now(),
                    "source_symbol": member.provider_symbol,
                    "trade_date": DAY,
                    "session": member.session,
                    "source_status": "no_trade",
                    "source_excerpt": "Official session report: no trades.",
                },
            ),
            provider="taifex",
            allowed_datasets=[dataset.dataset_key],
        )
    await scan_missing_deliveries(test_session, now=full.deadline_at + timedelta(minutes=1))
    await test_session.commit()
    for profile in ("pilot", "full_market"):
        result = await client.get(
            "/api/v1/admin/missing-deliveries",
            params={"execution_profile": profile, "status": "open"},
            headers=admin_headers,
        )
        assert result.status_code == 200, result.text
        rows = result.json()["data"]
        assert len(rows) == (0 if profile == "pilot" else 1)
        assert all(row["execution_profile"] == profile for row in rows)
    full_after = await get_plan(
        test_session, full.plan_id, provider="taifex", allowed_datasets=[dataset.dataset_key]
    )
    assert full_after.summary.no_data == 0 and full_after.summary.missing == 2

    full_control = await test_session.get(SchedulerControl, scheduler_key("taifex", "full_market"))
    assert full_control is not None
    full_control.desired_state = "stopped"
    await test_session.commit()
    await scan_missing_deliveries(test_session, now=full.deadline_at + timedelta(minutes=2))
    alert = await test_session.scalar(
        select(MissingDeliveryAlert).where(MissingDeliveryAlert.execution_profile == "full_market")
    )
    assert alert.status == "resolved"
    assert alert.details["resolution"] == {
        "code": "SCHEDULER_STOPPED",
        "execution_profile": "full_market",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["staging", "production"])
@pytest.mark.parametrize(
    "key",
    [
        "us_equity_eod",
        "hk_equity_eod",
        "tw_equity_eod",
        "tw_etf_eod",
        "tw_equity_minute",
        "tw_etf_minute",
        "tw_futures_eod",
    ],
)
async def test_shared_seven_feed_source_queue_canonical_serve(
    test_session, test_engine, client, monkeypatch, environment, key
):
    """Both profiles traverse the real durable pipeline in either environment."""
    import hashlib
    from copy import deepcopy

    from app.models.canonical import Instrument
    from app.models.raw import RawMarketPayload
    from app.models.registry import NormalizationJob, NormalizationOutbox
    from app.schemas.ingress import minute_sequence_key_digest
    from app.services.normalization_queue import execute_normalization
    from app.utils import utc_now
    from tests.test_minute_normalize import _minute_request

    monkeypatch.setattr(get_settings(), "APP_ENVIRONMENT", environment)
    dataset, body, full = await _activated_plan(test_session, key, count=3)
    pilot = await _pilot(test_session, dataset, body, full)
    headers = await _source_headers(test_session, body.provider, [key])
    await test_session.commit()
    for plan in (pilot, full):
        members = [member for part in plan.parts for member in part.member_keys]
        request = _ingress(plan, [])
        request.update(
            source=body.provider, request_key=str(plan.plan_id), idempotency_key=str(plan.plan_id)
        )
        if key == "tw_futures_eod":
            rows = [
                _quote(month=member.split("|")[1].split(":")[1], session=member.split("|")[2])
                for member in members
            ]
        elif key.endswith("minute"):
            template = _minute_request(key, symbol=members[0], source_symbol=members[0])
            request["schema_id"] = "market_minute"
            request["payload"] = template["payload"]
            batch = request["payload"]["batch"]
            for field in ("data_date", "coverage_start_date", "coverage_end_date"):
                batch[field] = DAY.isoformat()
            batch.update(
                snapshot_id=str(plan.plan_id),
                daily_update_id=str(plan.plan_id),
                universe_id=str(plan.release_id),
                symbols_sha256=hashlib.sha256("\n".join(members).encode()).hexdigest(),
            )
            digest = minute_sequence_key_digest(
                dataset_key=key, data_date=DAY, snapshot_id=str(plan.plan_id), sequence=1
            )
            request.update(request_key=f"mmr:{digest}", idempotency_key=f"mms:{digest}")
            rows = []
            for member in members:
                row = deepcopy(template["payload"]["data"][0])
                row.update(symbol=member, source_symbol=member)
                for field in ("trade_date", "bar_start_time", "bar_end_time", "signal_time"):
                    row[field] = row[field].replace("2026-07-30", DAY.isoformat())
                rows.append(row)
            request["payload"]["symbol_statuses"] = [
                {"symbol": member, "outcome": "data"} for member in members
            ]
        else:
            request["schema_id"] = "market_eod"
            rows = [
                {
                    "symbol": member,
                    "source_symbol": member,
                    "trade_date": DAY.isoformat(),
                    "currency": "HKD"
                    if key.startswith("hk_")
                    else "USD"
                    if key.startswith("us_")
                    else "TWD",
                    "open": "100",
                    "high": "110",
                    "low": "90",
                    "close": "105",
                    "volume": 5,
                }
                for member in members
            ]
        request["fetched_at"] = utc_now().isoformat()
        request["payload"]["data"] = rows
        request["payload"]["batch"]["declared_record_count"] = len(rows)
        accepted = await client.post("/api/v1/source/ingest", headers=headers, json=request)
        assert accepted.status_code == 202, accepted.text
        run_id = UUID(accepted.json()["run_id"])
        job = (
            await test_session.scalars(
                select(NormalizationJob).where(NormalizationJob.run_id == run_id)
            )
        ).one()
        assert await test_session.scalar(
            select(RawMarketPayload.raw_payload_id).where(RawMarketPayload.run_id == run_id)
        )
        assert await test_session.scalar(
            select(NormalizationOutbox.run_id).where(NormalizationOutbox.run_id == run_id)
        )
        await execute_normalization(
            run_id,
            job.delivery_id,
            database_url=test_engine.url.render_as_string(hide_password=False),
        )
        duplicate = await client.post("/api/v1/source/ingest", headers=headers, json=request)
        assert duplicate.json()["run_id"] == str(run_id)
        for frozen in (pilot, full):
            result = await get_plan(
                test_session, frozen.plan_id, provider=body.provider, allowed_datasets=[key]
            )
            completed = frozen == pilot or plan == full
            assert result.summary.data == (frozen.summary.expected if completed else 0)
            assert result.status == ("complete" if completed else "incomplete")
        if key == "tw_futures_eod":
            endpoint = "/api/v1/serve/futures/eod"
            params = {
                "product_code": "TX",
                "start_date": DAY.isoformat(),
                "end_date": DAY.isoformat(),
            }
        else:
            instrument = await test_session.scalar(
                select(Instrument).where(Instrument.symbol == members[0])
            )
            endpoint = "/api/v1/serve/minute" if key.endswith("minute") else "/api/v1/serve/eod"
            params = {
                "instrument_id": str(instrument.instrument_id),
                "start_date": DAY.isoformat(),
                "end_date": DAY.isoformat(),
            }
        served = await client.get(endpoint, params=params)
        assert served.status_code == 200, served.text
        assert served.json()["data"]
        assert all(row["source"] == body.provider for row in served.json()["data"])
