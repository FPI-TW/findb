"""HSIL reference documents inside a frozen HKEX daily plan: bound, stored and counted."""

from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.models.canonical import CalendarYearRevision
from app.models.registry import DatasetRegistry, IngestionRun
from app.schemas.full_market import (
    DeliveryPlanCreateRequest,
    UniversePublishRequest,
    UniverseSubmitRequest,
)
from app.services.full_market import create_plan, get_plan, publish_universe, submit_universe
from app.services.normalize import index_reference
from app.services.normalize.index_reference import (
    IndexChangesNormalizer,
    IndexConstituentsNormalizer,
)
from app.services.source_clients import create_source_client
from app.utils import utc_now
from scripts.seed_data import DATASETS
from tests.full_market_fixtures import historical_admission
from tests.index_reference_support import (
    BODIES,
    HSCI,
    change,
    changes_payload,
    constituent,
    constituents_payload,
)
from tests.intraday_support import RAW_BUCKET, Stores
from tests.test_full_market import DAY, EVIDENCE, _calendar

CONSTITUENTS = "hk_index_constituents"
CHANGES = "hk_index_changes"
HSI = {"code": "00001.00", "name_en": "Hang Seng Index", "name_zh": "恒生指數"}
HSI_FINANCE = {
    "code": "00001.01",
    "name_en": "Hang Seng Index - Finance",
    "name_zh": "恒生金融分類指數",
    "parent_code": "00001.00",
}


@pytest.fixture
def stores(monkeypatch) -> Stores:
    stores = Stores()
    monkeypatch.setattr(index_reference, "raw_reader", lambda: stores.raw)
    monkeypatch.setattr(index_reference, "canonical_publisher", lambda: stores.canonical)
    return stores


def _members(dataset: str) -> list[dict]:
    keys = (
        [("HSI", "hsi"), ("HSCI", "hsci")]
        if dataset == CONSTITUENTS
        else [("00011.00", "00011.00"), ("00011.07", "00011.07")]
    )
    return [
        {
            "symbol": symbol,
            "provider_symbol": provider_symbol,
            "exchange": "XHKG",
            "currency": "HKD",
            "asset_class": "index",
        }
        for symbol, provider_symbol in keys
    ]


async def _plan(db, key: str):
    definition = next(item for item in DATASETS if item["dataset_key"] == key)
    dataset = DatasetRegistry(
        **{
            field: definition[field]
            for field in ("dataset_key", "name", "asset_class", "market", "frequency", "config")
        },
        is_active=True,
    )
    db.add(dataset)
    await db.commit()
    await _calendar(db, "HK", [DAY])
    release = await submit_universe(
        db,
        UniverseSubmitRequest(
            version=1,
            dataset_key=key,
            provider="hkex",
            effective_date=DAY,
            observed_at=utc_now(),
            source_timezone="Asia/Hong_Kong",
            evidence=[EVIDENCE],
            members=_members(key),
        ),
        allowed_datasets=[key],
    )
    await publish_universe(
        db,
        release.release_id,
        UniversePublishRequest(
            evidence_note="Verified official first baseline.", first_baseline_approved=True
        ),
        actor="test-owner",
    )
    await db.commit()
    revision = await db.scalar(
        select(CalendarYearRevision).where(CalendarYearRevision.market == "HK")
    )
    await historical_admission(db, dataset, release, DAY, revision.id)
    plan = await create_plan(
        db,
        DeliveryPlanCreateRequest(
            version=1,
            dataset_key=key,
            provider="hkex",
            trade_date=DAY,
            release_id=release.release_id,
        ),
        allowed_datasets=[key],
    )
    _, secret = await create_source_client(
        db,
        name=f"{key}-plan-source",
        owner="tests",
        source_name="hkex",
        allowed_datasets=[key],
        rate_limit_requests=100,
        rate_limit_window=60,
        commit=False,
    )
    await db.commit()
    return dataset, plan, {"X-API-Key": secret}


def _hsi_report() -> dict:
    day = DAY.isoformat()
    payload = constituents_payload(
        [
            constituent("00001.00", "00005", "60", trade_date=day),
            constituent("00001.00", "00011", "40", trade_date=day),
            constituent("00001.01", "00005", "100", trade_date=day),
            constituent("00001.01", "00011", "0", trade_date=day),
        ],
        [{**HSI, "constituent_count": 2}, {**HSI_FINANCE, "constituent_count": 2}],
        trade_date=DAY,
    )
    payload["batch"]["data_date"] = day
    return payload


def _hsci_sheet() -> dict:
    payload = changes_payload(
        [
            change(1, "2026-07-02", "delete", "02555", 1, count_change=-1),
            change(2, "2008-10-08", "add", "00005", 2),
        ],
        index=HSCI,
        document_date=date(2026, 7, 2),
    )
    payload["batch"]["data_date"] = DAY.isoformat()
    return payload


def _delivery(plan, stores: Stores, dataset: str, payload: dict) -> dict:
    digest = payload["batch"]["source_raw_sha256"]
    key = payload["batch"]["source_raw_ref"].split(f"{RAW_BUCKET}/", 1)[1]
    stores.r2.objects[(RAW_BUCKET, key)] = BODIES[digest]
    schema_id = "index_constituents" if dataset == CONSTITUENTS else "index_constituent_changes"
    return {
        "dataset_key": dataset,
        "schema_id": schema_id,
        "schema_version": 1,
        "source": "hkex",
        "request_key": f"{dataset}-plan",
        "idempotency_key": f"{dataset}-plan-{digest[:12]}",
        "fetched_at": utc_now().isoformat(),
        "delivery": {
            "slot_id": "asia_pacific_markets_window",
            "scheduled_for": utc_now().isoformat(),
            "target_data_date": DAY.isoformat(),
            "work_item_id": plan.parts[0].work_item_id,
        },
        "payload": payload,
    }


async def _summary(db, plan, dataset: str):
    return (await get_plan(db, plan.plan_id, provider="hkex", allowed_datasets=[dataset])).summary


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dataset", "payload", "normalizer_type"),
    [
        (CONSTITUENTS, _hsi_report, IndexConstituentsNormalizer),
        (CHANGES, _hsci_sheet, IndexChangesNormalizer),
    ],
)
async def test_a_document_part_counts_as_its_member_once_it_is_kept(
    owned_client: AsyncClient, test_session, stores, dataset, payload, normalizer_type
) -> None:
    registry, plan, headers = await _plan(test_session, dataset)
    request = _delivery(plan, stores, dataset, payload())

    response = await owned_client.post("/api/v1/source/ingest", headers=headers, json=request)

    assert response.status_code == 202, response.text
    run_id = UUID(response.json()["run_id"])
    run = await test_session.get(IngestionRun, run_id)
    assert run.delivery_part_id == plan.parts[0].part_id
    summary = await _summary(test_session, plan, dataset)
    assert (summary.expected, summary.data, summary.missing) == (2, 0, 2)

    normalizer = normalizer_type(
        test_session,
        {**registry.config, "_ingest_source": "hkex", "_ingest_fetched_at": utc_now()},
    )
    result = await normalizer.process(request["payload"], run_id)

    assert result.failed_records == 0
    # One member stands for the whole document, whichever constituents it lists.
    summary = await _summary(test_session, plan, dataset)
    assert (summary.expected, summary.data, summary.missing) == (2, 1, 1)


@pytest.mark.asyncio
async def test_a_document_of_another_member_is_refused_before_it_is_kept(
    owned_client: AsyncClient, test_session, stores
) -> None:
    _, plan, headers = await _plan(test_session, CHANGES)
    other = changes_payload(
        index={"code": "00011.14", "name_en": "Healthcare", "parent_code": "00011.00"},
        document_date=date(2026, 7, 2),
        rows=[change(1, "2020-03-09", "add", "00005", 1)],
        history_start="2020-03-09",
    )
    other["batch"]["data_date"] = DAY.isoformat()
    request = _delivery(plan, stores, CHANGES, other)

    response = await owned_client.post("/api/v1/source/ingest", headers=headers, json=request)

    assert response.status_code == 409, response.text
    assert "outside the declared plan part" in response.text
    assert (await test_session.scalars(select(IngestionRun))).all() == []
