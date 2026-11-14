"""Full market is the only daily profile; retired Pilot parameters fail closed."""

from uuid import UUID

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.services.full_market import get_plan
from tests.test_full_market import DAY, _activated_plan, _ingress, _quote, _source_headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/admin/delivery-plans",
        "/api/v1/admin/missing-deliveries",
        "/api/v1/admin/schedulers",
    ],
)
async def test_retired_pilot_profile_is_rejected_by_admin_reads(client, admin_headers, path):
    response = await client.get(path, params={"execution_profile": "pilot"}, headers=admin_headers)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_retired_pilot_consumer_is_rejected_by_source_authorization(test_session, client):
    await _activated_plan(test_session, count=1)
    headers = await _source_headers(test_session)
    await test_session.commit()
    for params in ({"consumer": "pilot"}, {"execution_profile": "pilot"}):
        response = await client.get(
            "/api/v1/source/full-market/authorization", params=params, headers=headers
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_default_and_explicit_plan_reads_return_the_daily_plan(
    test_session, owned_client, admin_headers
):
    _, _, full = await _activated_plan(test_session, count=1)
    await test_session.commit()
    for params in ({}, {"execution_profile": "full_market"}):
        response = await owned_client.get(
            "/api/v1/admin/delivery-plans", params=params, headers=admin_headers
        )
        assert response.status_code == 200, response.text
        assert [item["plan_id"] for item in response.json()["data"]] == [str(full.plan_id)]


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["staging", "production"])
@pytest.mark.parametrize(
    "key",
    [
        "us_equity_eod",
        "hk_equity_eod",
        "tw_equity_eod",
        "tw_etf_eod",
        "tw_futures_eod",
    ],
)
async def test_shared_five_feed_source_queue_canonical_serve(
    test_session, test_engine, owned_client, monkeypatch, environment, key
):
    """Daily plans traverse the real durable pipeline in either environment."""
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
    headers = await _source_headers(test_session, body.provider, [key])
    await test_session.commit()
    for plan in (full,):
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
        from app.services import full_market as full_market_service

        reconciles = []
        classify = full_market_service.classify_plan

        async def counted(db, plan_row, members=None):
            reconciles.append(plan_row.plan_id if members is None else "part")
            return await classify(db, plan_row, members)

        monkeypatch.setattr(full_market_service, "classify_plan", counted)
        accepted = await owned_client.post("/api/v1/source/ingest", headers=headers, json=request)
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
        # A daily run reclassifies only its part (alert check, then summary); a
        # sequenced snapshot still classifies the whole cohort.
        assert reconciles == ([plan.plan_id] * 2 if key.endswith("minute") else ["part"] * 2)
        monkeypatch.setattr(full_market_service, "classify_plan", classify)
        duplicate = await owned_client.post("/api/v1/source/ingest", headers=headers, json=request)
        assert duplicate.json()["run_id"] == str(run_id)
        result = await get_plan(
            test_session, plan.plan_id, provider=body.provider, allowed_datasets=[key]
        )
        assert result.summary.data == plan.summary.expected
        assert result.status == "complete"
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
        served = await owned_client.get(endpoint, params=params)
        assert served.status_code == 200, served.text
        assert served.json()["data"]
        assert all(row["source"] == body.provider for row in served.json()["data"])
