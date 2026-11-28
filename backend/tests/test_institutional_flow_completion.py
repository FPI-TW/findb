"""An exchange's institutional table settles by its board member's completion evidence."""

import typing
from datetime import date
from uuid import uuid4

import pytest

from app.models.provider_work import ProviderWork
from app.models.registry import DatasetRegistry, DeliveryMemberEvidence, IngestionRun
from app.schemas.eod_completion import EODCompletionEvidence
from app.schemas.source import HistoricalBackfillItemUpdateRequest
from app.services import provider_work as queue
from app.services.eod_completion import MEMBER_EOD_SCHEMAS, eod_contract, validate_eod_completion
from app.utils import uuid7

DAY = date(2026, 11, 23)
FEEDS = [
    ("twse", "tw_twse_institutional_summary", "institutional_flow_summary"),
    ("twse", "tw_twse_institutional_stock", "institutional_flow_stock"),
    ("tpex", "tw_tpex_institutional_summary", "institutional_flow_summary"),
    ("tpex", "tw_tpex_institutional_stock", "institutional_flow_stock"),
]


def test_completion_evidence_accepts_every_member_eod_contract():
    """Settlement routes by MEMBER_EOD_SCHEMAS, so its evidence must parse each one."""
    accepted = typing.get_args(EODCompletionEvidence.model_fields["schema_id"].annotation)
    assert set(accepted) == MEMBER_EOD_SCHEMAS


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["daily", "historical"])
@pytest.mark.parametrize(("provider", "dataset_key", "schema_id"), FEEDS)
async def test_a_written_table_settles_its_board_member(
    test_session, provider, dataset_key, schema_id, kind
):
    board = provider.upper()
    test_session.add(
        DatasetRegistry(
            dataset_key=dataset_key, name=dataset_key, market="TW", asset_class="equity"
        )
    )
    await test_session.flush()
    work = ProviderWork(
        environment=queue.environment(),
        provider=provider,
        dataset_key=dataset_key,
        kind=kind,
        stable_key=f"test:{kind}",
        trade_date=DAY,
    )
    contract = eod_contract(work)
    assert contract == {"schema_id": schema_id, "schema_version": 1}
    work.manifest = {"contract": contract, "members": [{"key": board, "mapping_status": "mapped"}]}
    run = IngestionRun(
        run_id=uuid7(),
        dataset_key=dataset_key,
        source=provider,
        schema_id=schema_id,
        schema_version=1,
        batch_data_date=DAY,
        status="completed",
        total_records=1,
        success_records=1,
        failed_records=0,
    )
    test_session.add_all([work, run])
    await test_session.flush()
    test_session.add(DeliveryMemberEvidence(run_id=run.run_id, member_key=board))
    await test_session.flush()

    # The Fetcher's completion body parses at the Source route, not only in the service.
    body = HistoricalBackfillItemUpdateRequest.model_validate(
        {
            "status": "completed",
            "lease_token": str(uuid4()),
            "run_id": str(run.run_id),
            "result_evidence": {
                "version": 2,
                "schema_id": schema_id,
                "schema_version": 1,
                "trade_date": DAY.isoformat(),
                "members": [{"member_key": board, "outcome": "data"}],
            },
        }
    )
    evidence = body.result_evidence
    assert isinstance(evidence, EODCompletionEvidence)

    result = await validate_eod_completion(test_session, work, [run], evidence, outcome="completed")

    assert result["data"] == [board]
    assert not result["gaps"]
