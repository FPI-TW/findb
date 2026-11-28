"""Write evidence reads only the trade dates a run's rows can hold."""

from datetime import date
from uuid import UUID

import pytest
from sqlalchemy import select, text

from app.models.registry import DeliveryMemberEvidence, IngestionRun
from app.services.delivery_evidence import record_delivery_evidence, run_row_dates
from tests.query_budget import count_statements
from tests.test_canonical_ingest_api import _canonical_request, _execute_run, _seed_dataset


def _run(**dates) -> IngestionRun:
    return IngestionRun(dataset_key="tw_equity_eod", **dates)


def test_run_row_dates_cover_data_date_and_declared_coverage():
    day = date(2026, 1, 2)
    assert run_row_dates(_run(batch_data_date=day)) == (day, day)
    assert run_row_dates(
        _run(
            batch_data_date=day,
            coverage_start_date=date(2025, 12, 31),
            coverage_end_date=day,
        )
    ) == (date(2025, 12, 31), day)
    # Rows on data_date need no coverage, so a coverage that omits it still bounds it.
    assert run_row_dates(
        _run(
            batch_data_date=day,
            coverage_start_date=date(2025, 12, 1),
            coverage_end_date=date(2025, 12, 5),
        )
    ) == (date(2025, 12, 1), day)
    assert run_row_dates(_run()) is None


@pytest.mark.asyncio
async def test_evidence_spans_year_partitions_and_reads_only_run_dates(
    owned_client, source_headers, test_session, test_engine
):
    await _seed_dataset(test_session)
    value = _canonical_request(idempotency_key="evidence-across-year-partitions")
    batch = value["payload"]["batch"]
    batch.update(
        data_date="2026-01-02",
        delivery_mode="backfill",
        coverage_start_date="2025-12-31",
        coverage_end_date="2026-01-02",
        declared_record_count=2,
    )
    first = value["payload"]["data"][0]
    first["trade_date"] = "2025-12-31"
    second = {**first, "symbol": "2317", "source_symbol": "2317 TT Equity", "name": "鴻海"}
    second["trade_date"] = "2026-01-02"
    value["payload"]["data"].append(second)
    accepted = await owned_client.post("/api/v1/source/ingest", headers=source_headers, json=value)
    assert accepted.status_code == 202, accepted.text
    run_id = UUID(accepted.json()["run_id"])
    await _execute_run(test_session, test_engine, run_id)

    run = await test_session.get(IngestionRun, run_id)
    assert run.status == "completed"
    evidence_query = select(DeliveryMemberEvidence.member_key).where(
        DeliveryMemberEvidence.run_id == run_id
    )
    assert set(await test_session.scalars(evidence_query)) == {"2330", "2317"}

    with count_statements(test_session) as statements:
        await record_delivery_evidence(test_session, run)
    lookup = next(statement for statement in statements if "market_data_eod" in statement)
    assert "market_data_eod.trade_date BETWEEN" in lookup
    assert set(await test_session.scalars(evidence_query)) == {"2330", "2317"}

    # PostgreSQL prunes to the run's year partitions instead of scanning all of them.
    plan = "\n".join(
        await test_session.scalars(
            text(
                "EXPLAIN SELECT DISTINCT instruments.symbol FROM instruments "
                "JOIN market_data_eod ON market_data_eod.instrument_id = instruments.instrument_id "
                "WHERE market_data_eod.run_id = :run_id "
                "AND market_data_eod.trade_date BETWEEN :start AND :end"
            ),
            {"run_id": run_id, "start": date(2025, 12, 31), "end": date(2026, 1, 2)},
        )
    )
    scanned = {
        line.split(" on ")[1].split()[0]
        for line in plan.splitlines()
        if " on market_data_eod_" in line
    }
    assert scanned == {"market_data_eod_y2025", "market_data_eod_y2026"}
