"""An intraday delivery inside a frozen daily plan: bound, checked and counted by the index."""

from __future__ import annotations

import hashlib
from typing import get_args
from uuid import UUID

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import select

from app.models.canonical import CalendarYearRevision, IntradayPartition
from app.models.registry import DatasetRegistry, IngestionRun
from app.provider_catalog import FeedDefinition
from app.schemas.execution_config import FeedSchedule
from app.schemas.full_market import DeliveryPlanCreateRequest
from app.services.full_market import create_plan, get_plan
from app.services.normalize import intraday
from app.services.normalize.intraday import IntradayPartitionContractNormalizer
from app.services.provider_metadata import acquisition_feeds
from app.services.source_clients import create_source_client
from app.utils import utc_now
from tests.full_market_fixtures import historical_admission
from tests.intraday_support import (
    ACCOUNT,
    DAY,
    RAW_BUCKET,
    Stores,
    bars,
    dataset_config,
    parquet_bytes,
)
from tests.test_full_market import _baseline, _calendar

DATASET = "tw_equity_bar_1m"


@pytest.fixture
def stores(monkeypatch) -> Stores:
    stores = Stores()
    monkeypatch.setattr(intraday, "raw_reader", lambda: stores.raw)
    monkeypatch.setattr(intraday, "canonical_publisher", lambda: stores.canonical)
    return stores


async def _plan(db, count: int = 2):
    dataset = DatasetRegistry(
        dataset_key=DATASET,
        name="TW equity 1-minute bars",
        asset_class="equity",
        market="TW",
        frequency="minute",
        is_active=True,
        config={
            **dataset_config("bar_1m"),
            "full_market": {"enabled": False, "required": False, "activation_date": None},
        },
    )
    db.add(dataset)
    await db.commit()
    await _calendar(db, "TW", [DAY])
    body, release = await _baseline(db, DATASET, count)
    revision = await db.scalar(
        select(CalendarYearRevision).where(CalendarYearRevision.market == "TW")
    )
    await historical_admission(db, dataset, release, DAY, revision.id)
    plan = await create_plan(
        db,
        DeliveryPlanCreateRequest(
            version=1,
            dataset_key=DATASET,
            provider="finlab",
            trade_date=DAY,
            release_id=release.release_id,
        ),
        allowed_datasets=[DATASET],
    )
    _, key = await create_source_client(
        db,
        name="intraday-plan-source",
        owner="tests",
        source_name="finlab",
        allowed_datasets=[DATASET],
        rate_limit_requests=100,
        rate_limit_window=60,
        commit=False,
    )
    await db.commit()
    return dataset, plan, {"X-API-Key": key}


def _delivery(plan, stores: Stores, symbol: str, **partition) -> dict:
    rows = parquet_bytes(bars(symbol=symbol))
    digest = hashlib.sha256(rows).hexdigest()
    key = f"finlab/{DATASET}/0123456789abcdef/{digest}.rows.parquet"
    stores.r2.objects[(RAW_BUCKET, key)] = rows
    return {
        "dataset_key": DATASET,
        "schema_id": "intraday_partition",
        "schema_version": 1,
        "source": "finlab",
        "request_key": f"intraday-plan-{symbol}",
        "idempotency_key": f"intraday-plan-{symbol}-{digest[:12]}",
        "fetched_at": utc_now().isoformat(),
        "delivery": {
            "slot_id": "taiwan_market_window",
            "scheduled_for": utc_now().isoformat(),
            "target_data_date": plan.trade_date.isoformat(),
            "work_item_id": plan.parts[0].work_item_id,
        },
        "payload": {
            "batch": {
                "data_date": DAY.isoformat(),
                "delivery_mode": "incremental",
                "declared_record_count": 1,
                "source_raw_ref": f"r2://{ACCOUNT}/{RAW_BUCKET}/finlab/original/{digest}.parquet",
                "source_raw_sha256": "d" * 64,
            },
            "data": [
                {
                    "symbol": symbol,
                    "source_symbol": symbol,
                    "trade_date": DAY.isoformat(),
                    "record_kind": "bar_1m",
                    "row_count": 3,
                    "rows_ref": f"r2://{ACCOUNT}/{RAW_BUCKET}/{key}",
                    "rows_sha256": digest,
                    "rows_bytes": len(rows),
                    "rows_format": "parquet",
                    "price_adjustment": "none",
                    "volume_unit": "lot",
                    **partition,
                }
            ],
        },
    }


@pytest.mark.asyncio
async def test_a_plan_member_counts_as_data_once_its_day_is_stored_and_indexed(
    owned_client: AsyncClient, test_session, stores
) -> None:
    dataset, plan, headers = await _plan(test_session)
    request = _delivery(plan, stores, "0001")

    response = await owned_client.post("/api/v1/source/ingest", headers=headers, json=request)

    assert response.status_code == 202, response.text
    run_id = UUID(response.json()["run_id"])
    run = await test_session.get(IngestionRun, run_id)
    assert run.delivery_part_id == plan.parts[0].part_id
    # Accepted is not delivered: nothing is stored yet, so the member is still missing.
    summary = (
        await get_plan(test_session, plan.plan_id, provider="finlab", allowed_datasets=[DATASET])
    ).summary
    assert (summary.expected, summary.data, summary.missing) == (2, 0, 2)

    normalizer = IntradayPartitionContractNormalizer(
        test_session,
        {**dataset.config, "_ingest_source": "finlab", "_ingest_fetched_at": utc_now()},
    )
    result = await normalizer.process(request["payload"], run_id)

    assert result.success_records == 1
    partition = await test_session.scalar(select(IntradayPartition))
    assert (partition.symbol, partition.run_id) == ("0001", run_id)
    summary = (
        await get_plan(test_session, plan.plan_id, provider="finlab", allowed_datasets=[DATASET])
    ).summary
    assert (summary.expected, summary.data, summary.missing) == (2, 1, 1)


@pytest.mark.asyncio
async def test_a_rejected_day_leaves_its_plan_member_missing(
    owned_client: AsyncClient, test_session, stores
) -> None:
    dataset, plan, headers = await _plan(test_session)
    request = _delivery(plan, stores, "0001", row_count=4)

    response = await owned_client.post("/api/v1/source/ingest", headers=headers, json=request)
    assert response.status_code == 202, response.text
    run_id = UUID(response.json()["run_id"])
    normalizer = IntradayPartitionContractNormalizer(
        test_session,
        {**dataset.config, "_ingest_source": "finlab", "_ingest_fetched_at": utc_now()},
    )
    result = await normalizer.process(request["payload"], run_id)

    assert result.failed_records == 1
    summary = (
        await get_plan(test_session, plan.plan_id, provider="finlab", allowed_datasets=[DATASET])
    ).summary
    assert (summary.expected, summary.data, summary.missing) == (2, 0, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "reason"),
    [
        # Not a member of the frozen part.
        ({"symbol": "9999", "source_symbol": "9999"}, "outside the declared plan part"),
        # The provider symbol must be the one frozen with the universe.
        ({"source_symbol": "0001.TW"}, "provider symbol differs"),
    ],
)
async def test_a_partition_outside_the_frozen_plan_is_refused_before_it_is_kept(
    owned_client: AsyncClient, test_session, stores, change, reason
) -> None:
    _, plan, headers = await _plan(test_session)
    request = _delivery(plan, stores, "0001")
    request["payload"]["data"][0].update(change)

    response = await owned_client.post("/api/v1/source/ingest", headers=headers, json=request)

    assert response.status_code == 409, response.text
    assert reason in response.text
    assert (await test_session.scalars(select(IngestionRun))).all() == []


def test_the_catalog_and_the_published_schedule_accept_intraday_frequencies() -> None:
    template = next(feed for feed in acquisition_feeds() if feed.dataset_key == "tw_equity_eod")
    daily = template.model_dump(mode="json")
    intraday_contract = {
        "schema_id": "intraday_partition",
        "current_version": 1,
        "accepted_versions": [1],
    }

    for frequency in ("minute", "tick"):
        feed = FeedDefinition.model_validate(
            {
                **daily,
                "dataset_key": DATASET,
                "frequency": frequency,
                "contract": intraday_contract,
            },
            strict=False,
        )
        assert feed.frequency == frequency
        assert frequency in get_args(FeedSchedule.model_fields["frequency"].annotation)

    # The record granularity and the contract cannot disagree.
    with pytest.raises(ValidationError, match="only intraday_partition feeds"):
        FeedDefinition.model_validate({**daily, "frequency": "minute"}, strict=False)
    with pytest.raises(ValidationError, match="only intraday_partition feeds"):
        FeedDefinition.model_validate({**daily, "contract": intraday_contract}, strict=False)
