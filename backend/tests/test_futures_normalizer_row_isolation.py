"""A failing futures row must not leave half of its canonical writes behind."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.canonical import FuturesContract, FuturesContractEOD
from app.models.registry import DQIssue
from app.schemas.ingress import FuturesEODRow
from app.services.ingestion import IngestionService
from app.services.normalize.contracts import FuturesEODContractNormalizer
from tests.test_full_market import DAY, _dataset, _quote


async def _run(db, dataset):
    return await IngestionService(db).create_ingestion_run(
        dataset.dataset_key,
        source="taifex",
        schema_id="futures_eod",
        schema_version=1,
        batch_data_date=DAY,
    )


@pytest.mark.asyncio
async def test_non_sql_failure_after_contract_upsert_rolls_back_only_that_row(
    test_session, monkeypatch
):
    dataset = await _dataset(test_session)
    config = {**dataset.config, "_ingest_source": "taifex"}
    first = await _run(test_session, dataset)
    result = await FuturesEODContractNormalizer(test_session, config).process(
        {"data": [_quote("202608")]}, first.run_id
    )
    assert result.success_records == 1

    original_dump = FuturesEODRow.model_dump

    def failing_dump(self, *args, **kwargs):
        # model_dump runs after the contract upsert and before the quote upsert.
        if self.contract_code == "TX:202608":
            raise ArithmeticError("conversion failed")
        return original_dump(self, *args, **kwargs)

    monkeypatch.setattr(FuturesEODRow, "model_dump", failing_dump)
    second = await _run(test_session, dataset)
    retry = {**_quote("202608"), "close": "99"}
    result = await FuturesEODContractNormalizer(test_session, config).process(
        {"data": [retry, _quote("202609")]}, second.run_id
    )

    assert (result.success_records, result.failed_records) == (1, 1)
    contracts = {
        row.contract_code: row for row in (await test_session.scalars(select(FuturesContract)))
    }
    # The failed row's contract rewrite was rolled back with its savepoint.
    assert contracts["TX:202608"].run_id == first.run_id
    assert contracts["TX:202609"].run_id == second.run_id
    quotes = {
        row.contract_id: row for row in (await test_session.scalars(select(FuturesContractEOD)))
    }
    assert quotes[contracts["TX:202608"].contract_id].close == Decimal("105")
    assert quotes[contracts["TX:202608"].contract_id].run_id == first.run_id
    assert quotes[contracts["TX:202609"].contract_id].run_id == second.run_id
    issue = (
        await test_session.scalars(select(DQIssue).where(DQIssue.run_id == second.run_id))
    ).one()
    assert issue.issue_type == "FUTURES_PROCESSING_ERROR"
    assert issue.instrument_id == contracts["TX:202608"].instrument_id
