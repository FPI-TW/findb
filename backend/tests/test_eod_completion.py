"""Whole-work completion reconciles every receipt, never just the last run."""

from datetime import date, datetime, timezone

import pytest

from app.models.provider_work import ProviderWork, ProviderWorkReceipt
from app.models.registry import DatasetRegistry, DeliveryMemberEvidence, DQIssue, IngestionRun
from app.schemas.eod_completion import EODCompletionEvidence, EODRawEvidence
from app.services import provider_work as queue
from app.services.eod_completion import validate_eod_completion
from app.utils import uuid7
from tests.test_provider_work import identity

DAY = date(2026, 10, 15)


def proof(*outcomes):
    return EODCompletionEvidence(
        version=2,
        schema_id="market_eod",
        schema_version=2,
        trade_date=DAY,
        members=[{"member_key": key, "outcome": outcome} for key, outcome in outcomes],
    )


async def scope(db):
    db.add(
        DatasetRegistry(dataset_key="hk_equity_eod", name="HK", market="HK", asset_class="equity")
    )
    await db.flush()
    work = ProviderWork(
        environment=queue.environment(),
        provider="factset",
        dataset_key="hk_equity_eod",
        kind="historical",
        stable_key="test",
        trade_date=DAY,
        manifest={
            "contract": {"schema_id": "market_eod", "schema_version": 2},
            "members": [
                {"key": "00700", "mapping_status": "mapped"},
                {"key": "00005", "mapping_status": "mapped"},
                {"key": "00001", "mapping_status": "gap"},
            ],
        },
    )
    db.add(work)
    await db.flush()
    return work


async def run(db, **changes):
    value = IngestionRun(
        **{
            "run_id": uuid7(),
            "dataset_key": "hk_equity_eod",
            "source": "factset",
            "schema_id": "market_eod",
            "schema_version": 2,
            "batch_data_date": DAY,
            "status": "completed_with_errors",
            "total_records": 2,
            "success_records": 1,
            "failed_records": 1,
            **changes,
        }
    )
    db.add(value)
    await db.flush()
    return value


@pytest.mark.asyncio
async def test_actual_writes_and_dq_errors_cover_partial_without_false_success(test_session):
    work = await scope(test_session)
    accepted = await run(test_session)
    test_session.add(DeliveryMemberEvidence(run_id=accepted.run_id, member_key="00700"))
    test_session.add(
        DQIssue(
            run_id=accepted.run_id,
            issue_type="OHLC_HIGH_CHECK",
            severity="error",
            trade_date=datetime(2026, 10, 15, tzinfo=timezone.utc),
            raw_data={"symbol": "00005"},
        )
    )
    await test_session.flush()
    evidence = proof(("00700", "data"), ("00005", "invalid"), ("00001", "mapping_gap"))
    result = await validate_eod_completion(
        test_session, work, [accepted], evidence, outcome="partial"
    )
    assert result["data"] == ["00700"] and result["gaps"] == ["00001", "00005"]
    with pytest.raises(ValueError, match="partial requires"):
        await validate_eod_completion(test_session, work, [accepted], evidence, outcome="completed")
    # A later successful receipt cannot hide a non-data normalization failure.
    broken = await run(test_session, status="failed")
    with pytest.raises(ValueError, match="restricted"):
        await validate_eod_completion(
            test_session, work, [accepted, broken], evidence, outcome="partial"
        )
    test_session.add(
        DQIssue(run_id=accepted.run_id, severity="error", issue_type="processing_error")
    )
    await test_session.flush()
    with pytest.raises(ValueError, match="non-data"):
        await validate_eod_completion(test_session, work, [accepted], evidence, outcome="partial")


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["data", "no_data", "invalid", "missing", "mapping_gap"])
async def test_unproven_claim_never_completes_a_mapped_member(test_session, outcome):
    work = await scope(test_session)
    evidence = proof(("00700", outcome), ("00005", "missing"), ("00001", "mapping_gap"))
    with pytest.raises(ValueError):
        await validate_eod_completion(test_session, work, [], evidence, outcome="partial")


@pytest.mark.asyncio
async def test_raw_gaps_require_scoped_content_address_and_complete_member_accounting(test_session):
    work = await scope(test_session)
    raw = {
        "ref": f"r2://{'a' * 32}/raw/factset/hk_equity_eod/member/{'b' * 64}.json",
        "sha256": "b" * 64,
    }
    evidence = proof(("00700", "missing"), ("00005", "invalid"), ("00001", "mapping_gap"))
    for member, reason in zip(evidence.members[:2], ("no_source_row", "row_structure_invalid")):
        member.raw = EODRawEvidence.model_validate(raw)
        member.reason = reason
    evidence = EODCompletionEvidence.model_validate(evidence.model_dump())
    result = await validate_eod_completion(test_session, work, [], evidence, outcome="partial")
    assert result["data"] == [] and len(result["gaps"]) == 3
    evidence.members.pop()
    with pytest.raises(ValueError, match="every frozen"):
        await validate_eod_completion(test_session, work, [], evidence, outcome="partial")


@pytest.mark.asyncio
async def test_partial_keeps_owner_until_all_receipts_are_terminal(test_session):
    work = await scope(test_session)
    # A queue claim is independent from the domain completion gate below.
    work.state = "queued"
    from app.models.provider_work import ProviderWorkAttempt, ProviderWorkOwner

    test_session.add(ProviderWorkAttempt(work_id=work.work_id))
    await test_session.flush()
    claimed = await queue.claim(
        test_session, provider="factset", datasets=["hk_equity_eod"], host_lock_identity="host:1:22"
    )
    assert claimed
    accepted = await run(test_session, status="processing")
    test_session.add(
        ProviderWorkReceipt(
            run_id=accepted.run_id,
            work_id=work.work_id,
            attempt_id=claimed[1].attempt_id,
            member_key="body",
            payload_sha256="c" * 64,
        )
    )
    await test_session.flush()
    await queue.close(test_session, **identity(claimed))
    with pytest.raises(queue.ProviderWorkDrainingError):
        await queue.settle(test_session, **identity(claimed), outcome="partial", proof={})
    owner = await test_session.get(ProviderWorkOwner, (queue.environment(), "factset"))
    assert owner.attempt_id == claimed[1].attempt_id
