"""Canonical visibility, five-day rollout and safe registry reseeding."""

import json
import subprocess
import sys
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select, text

from app.models.registry import DailyDeliveryMember, DatasetRegistry, IngestionRun, SchedulerControl
from app.schemas.full_market import (
    DeliveryOutcomeRequest,
    DeliveryPlanCreateRequest,
    FeedActivateRequest,
)
from app.schemas.ingress import MarketMinuteIngressRequest
from app.services.full_market import (
    FullMarketError,
    activate_feed,
    create_plan,
    get_plan,
    record_outcome,
)
from app.services.normalize.contracts import (
    FuturesEODContractNormalizer,
    MarketMinuteContractNormalizer,
)
from scripts.seed_data import seed_datasets
from tests.test_canonical_ingest_api import _execute_run
from tests.test_full_market import DAY, EVIDENCE, _activated_plan, _ingress, _quote, _source_headers
from tests.test_minute_normalize import _minute_request


def _fetcher_member_minute_request(plan, symbol, *, sequence_count=1):
    """Use the frozen Fetcher adapter in its own process; no production coupling."""
    source = Path(__file__).resolve().parents[2] / "fetcher" / "src"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import json, sys, runpy
from datetime import date, datetime, timedelta, timezone
args = json.load(sys.stdin)
adapter = runpy.run_path(args['source'] + '/findb_fetcher/providers/shioaji.py')
universe = runpy.run_path(args['source'] + '/findb_fetcher/full_market_universe.py')
build_market_minute_request = adapter['build_market_minute_request']
canonical_bytes, checksum = universe['canonical_bytes'], universe['checksum']
day = date.fromisoformat(args['day'])
end = datetime.combine(day, datetime.min.time()) + timedelta(hours=9, minutes=1)
ns = int((end - datetime(1970, 1, 1)).total_seconds()) * 1_000_000_000
kbars = {'ts': (ns,), 'Open': (100,), 'High': (101,), 'Low': (99,),
         'Close': (100.5,), 'Volume': (2,), 'Amount': (200000,)}
identity = checksum(canonical_bytes({'plan': args['plan'], 'member': args['symbol']}))
request = build_market_minute_request(
    kbars, dataset_key='tw_equity_minute', target_date=day, symbols=(args['symbol'],),
    fetched_at=datetime.combine(day, datetime.min.time(), timezone.utc) + timedelta(hours=10),
    usage_before_requests=1, usage_after_requests=2, snapshot_id=identity,
    daily_update_id=args['plan'], universe_id=args['release'], sequence_count=args['count'])
print(json.dumps(request))
""",
        ],
        input=json.dumps(
            {
                "day": str(DAY),
                "plan": str(plan.plan_id),
                "release": str(plan.release_id),
                "symbol": symbol,
                "count": sequence_count,
                "source": str(source),
            }
        ),
        capture_output=True,
        text=True,
        check=True,
    )
    request = json.loads(result.stdout)
    request["delivery"] = _ingress(plan, [])["delivery"]
    return request


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_member_snapshot_completeness_is_independent_within_one_plan_part(
    client, test_session, test_engine, partial
):
    _, _, plan = await _activated_plan(test_session, "tw_equity_minute", count=2)
    assert len(plan.parts) == 1
    headers = await _source_headers(test_session, "shioaji", ["tw_equity_minute"])
    # Process a partial member first, then a complete member. A later complete
    # snapshot must never promote all symbols that happen to share its part.
    for symbol in ("0002", "0001"):
        request = _fetcher_member_minute_request(
            plan, symbol, sequence_count=2 if partial and symbol == "0002" else 1
        )
        validated = MarketMinuteIngressRequest.model_validate(request)
        assert validated.payload.data[0].volume == 2000
        assert validated.payload.batch.daily_update_id == str(plan.plan_id)
        response = await client.post("/api/v1/source/ingest", headers=headers, json=request)
        assert response.status_code == 202, response.text
        run_id = UUID(response.json()["run_id"])
        await _execute_run(test_session, test_engine, run_id)
    final = await get_plan(
        test_session, plan.plan_id, provider="shioaji", allowed_datasets=["tw_equity_minute"]
    )
    assert final.summary.expected == 2
    assert await test_session.scalar(text("SELECT count(*) FROM market_data_minute")) == 2
    assert final.summary.data == (1 if partial else 2)
    assert final.summary.missing == (1 if partial else 0)
    assert final.status == ("incomplete" if partial else "complete")


@pytest.mark.asyncio
async def test_futures_serve_cursor_filters_and_product_coverage(client, test_session):
    dataset, _, plan = await _activated_plan(test_session)
    run = IngestionRun(
        dataset_key=dataset.dataset_key, source="taifex", delivery_part_id=plan.parts[0].part_id
    )
    test_session.add(run)
    await test_session.flush()
    normalizer = FuturesEODContractNormalizer(
        test_session, {**dataset.config, "_ingest_source": "taifex"}
    )
    await normalizer.process(
        {"data": [_quote(), _quote(session="after_hours"), _quote("202609")]}, run.run_id
    )
    response = await client.get("/api/v1/serve/futures/eod?product_code=TX&page_size=1")
    assert response.status_code == 200, response.text
    seen = [response.json()["data"][0]]
    cursor = response.json()["pagination"]["next_cursor"]
    while cursor:
        response = await client.get(
            "/api/v1/serve/futures/eod",
            params={"product_code": "TX", "page_size": 1, "cursor": cursor},
        )
        assert response.status_code == 200
        seen.extend(response.json()["data"])
        cursor = response.json()["pagination"]["next_cursor"]
    assert len(seen) == 3 and len({(row["contract_code"], row["session"]) for row in seen}) == 3
    first = await client.get("/api/v1/serve/futures/eod?page_size=1")
    wrong = await client.get(
        "/api/v1/serve/futures/eod",
        params={"session": "regular", "cursor": first.json()["pagination"]["next_cursor"]},
    )
    assert wrong.status_code == 422
    coverage = (await client.get("/api/v1/serve/instruments")).json()["data"][0]["coverage"]
    assert coverage["futures"]["latest_close"] is None


@pytest.mark.asyncio
async def test_minute_available_before_closed_group_but_plan_remains_missing(test_session):
    dataset, _, plan = await _activated_plan(test_session, "tw_equity_minute", count=1)
    await test_session.execute(
        text(
            "CREATE TABLE market_data_minute_fm PARTITION OF market_data_minute FOR VALUES FROM ('2026-07-01') TO ('2026-08-01')"
        )
    )
    payload = _minute_request("tw_equity_minute", symbol="0001", source_symbol="0001")["payload"]
    payload["batch"]["data_date"] = DAY.isoformat()
    payload["batch"]["sequence_count"] = 2
    for key in ("trade_date", "bar_start_time", "bar_end_time", "signal_time"):
        payload["data"][0][key] = payload["data"][0][key].replace("2026-07-30", DAY.isoformat())
    normalizer = MarketMinuteContractNormalizer(
        test_session, {**dataset.config, "_ingest_source": "shioaji"}
    )
    for sequence in (1, 2):
        run = IngestionRun(
            dataset_key="tw_equity_minute",
            source="shioaji",
            delivery_part_id=plan.parts[0].part_id,
            schema_id="market_minute",
            schema_version=1,
            batch_data_date=DAY,
            delivery_mode="sequenced_snapshot",
            snapshot_id="one-part-snapshot",
            daily_update_id="one-update",
            sequence=sequence,
            sequence_count=2,
        )
        test_session.add(run)
        await test_session.flush()
        result = await normalizer.process(payload, run.run_id)
        assert result.success_records == 1
        assert await test_session.scalar(text("SELECT count(*) FROM market_data_minute")) == 1
        summary = (
            await get_plan(
                test_session,
                plan.plan_id,
                provider="shioaji",
                allowed_datasets=["tw_equity_minute"],
            )
        ).summary
        assert summary.data == (1 if sequence == 2 else 0)
        assert summary.missing == (0 if sequence == 2 else 1)


@pytest.mark.asyncio
async def test_acceptance_requires_five_distinct_consecutive_open_days_on_time(test_session):
    days = [DAY + timedelta(days=offset) for offset in (0, 1, 3, 7, 8, 9)]
    dataset, body, first = await _activated_plan(test_session, count=1, open_dates=days)
    plans = [first]
    for day in days[1:]:
        plans.append(
            await create_plan(
                test_session,
                DeliveryPlanCreateRequest(
                    version=1,
                    dataset_key=dataset.dataset_key,
                    provider=body.provider,
                    trade_date=day,
                    release_id=first.release_id,
                ),
                allowed_datasets=[dataset.dataset_key],
            )
        )
    active = FeedActivateRequest(
        activation_date=DAY,
        readiness_evidence_note="Five day complete official audit reviewed.",
        mode="active",
    )
    with pytest.raises(FullMarketError, match="incomplete"):
        await activate_feed(test_session, dataset.dataset_key, active)
    for plan in plans:
        members = (
            await test_session.scalars(
                select(DailyDeliveryMember).where(DailyDeliveryMember.plan_id == plan.plan_id)
            )
        ).all()
        for member in members:
            evidence = {
                **EVIDENCE,
                "observed_at": plan.deadline_at - timedelta(minutes=2),
                "source_symbol": member.provider_symbol,
                "trade_date": plan.trade_date,
                "session": member.session,
                "source_status": "no_trade",
                "source_excerpt": "Official report confirms no trades for this contract/session.",
            }
            await record_outcome(
                test_session,
                plan.plan_id,
                DeliveryOutcomeRequest(
                    version=1,
                    work_item_id=plan.parts[0].work_item_id,
                    member_key=member.member_key,
                    outcome="no_data",
                    reason="no_trade",
                    evidence=evidence,
                ),
                provider=body.provider,
                allowed_datasets=[dataset.dataset_key],
            )
            member.outcome_at = plan.deadline_at - timedelta(minutes=1)
        await test_session.commit()
    removed = plans[-3]
    await test_session.execute(
        text("UPDATE daily_delivery_member SET outcome=NULL WHERE plan_id=:id"),
        {"id": removed.plan_id},
    )
    await test_session.commit()
    with pytest.raises(FullMarketError, match="incomplete"):
        await activate_feed(test_session, dataset.dataset_key, active)
    await test_session.execute(
        text("UPDATE daily_delivery_member SET outcome='no_data' WHERE plan_id=:id"),
        {"id": removed.plan_id},
    )
    await test_session.commit()
    response = await activate_feed(test_session, dataset.dataset_key, active)
    assert response["mode"] == "active"


@pytest.mark.asyncio
async def test_reseed_preserves_approved_feed_and_running_scheduler(test_session):
    dataset, _, _ = await _activated_plan(test_session, "hk_equity_eod", count=1)
    previous = deepcopy(dataset.config["full_market"])
    await seed_datasets(test_session)
    control = await test_session.get(SchedulerControl, "full_market_twelve_data_v1")
    assert control.desired_state == "stopped"
    control.desired_state = "running"
    await test_session.commit()
    await seed_datasets(test_session)
    assert dataset.is_active and dataset.config["full_market"] == previous
    assert control.desired_state == "running"


@pytest.mark.asyncio
async def test_audited_deactivation_retains_plan_and_restores_bounded_feed(
    client, test_session, admin_headers
):
    from app.models.registry import DailyDeliveryPlan, UniverseRelease
    from app.services.full_market import deactivate_feed

    staged, _, plan = await _activated_plan(test_session, "hk_equity_eod", count=1)
    await seed_datasets(test_session)
    control = await test_session.get(SchedulerControl, "full_market_twelve_data_v1")
    control.desired_state = "running"
    await test_session.commit()
    response = await client.post(
        "/api/v1/admin/feeds/hk_equity_eod/deactivate",
        headers=admin_headers,
        json={"evidence_note": "Operator reviewed rollback without deleting history."},
    )
    assert response.status_code == 200, response.text
    assert not staged.is_active and not staged.config["full_market"]["enabled"]
    assert control.desired_state == "stopped"
    assert await test_session.get(DailyDeliveryPlan, plan.plan_id) is not None
    assert await test_session.get(UniverseRelease, plan.release_id) is not None
    bounded = await test_session.get(DatasetRegistry, "us_equity_eod")
    config = deepcopy(bounded.config)
    config["full_market"]["enabled"] = True
    bounded.config = config
    await deactivate_feed(test_session, "us_equity_eod")
    assert bounded.is_active and not bounded.config["full_market"]["enabled"]


@pytest.mark.asyncio
async def test_acceptance_cannot_skip_an_actual_exchange_open_day(test_session):
    days = [DAY + timedelta(days=offset) for offset in (0, 1, 3, 7, 8, 9)]
    dataset, body, first = await _activated_plan(test_session, count=1, open_dates=days)
    for day in (days[1], days[2], days[4], days[5]):
        await create_plan(
            test_session,
            DeliveryPlanCreateRequest(
                version=1,
                dataset_key=dataset.dataset_key,
                provider=body.provider,
                trade_date=day,
                release_id=first.release_id,
            ),
            allowed_datasets=[dataset.dataset_key],
        )
    with pytest.raises(FullMarketError, match="consecutive"):
        await activate_feed(
            test_session,
            dataset.dataset_key,
            FeedActivateRequest(
                activation_date=DAY,
                mode="active",
                readiness_evidence_note="Five scheduled days reviewed by owner.",
            ),
        )


@pytest.mark.asyncio
async def test_monitor_only_emits_post_activation_real_exchange_gaps(test_session):
    from app.models.registry import MissingDeliveryAlert
    from app.services.delivery_monitor import _scan_full_market_feed

    dataset, body, plan = await _activated_plan(
        test_session, count=1, open_dates=[DAY - timedelta(days=1), DAY]
    )
    created, resolved = await _scan_full_market_feed(
        test_session,
        dataset=dataset,
        source=body.provider,
        schema_id="futures_eod",
        schema_version=1,
        evaluated_at=plan.deadline_at + timedelta(minutes=1),
    )
    assert (created, resolved) == (1, 0)
    alerts = (await test_session.scalars(select(MissingDeliveryAlert))).all()
    assert [alert.expected_data_date for alert in alerts] == [DAY]
    assert alerts[0].details["expected"] == 2 and alerts[0].details["missing"] == 2
