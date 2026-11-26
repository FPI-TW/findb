"""Whole-work completion reconciles every receipt, never just the last run."""

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

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
    assert result["data"] == ["00700"] and result["gaps"] == ["00005"]
    # A mapping gap is unavailable: it never keeps a date partial on its own (#593).
    assert result["unavailable"] == {"00001": "mapping_gap"}
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
@pytest.mark.parametrize("outcome", ["data", "no_data", "invalid", "mapping_gap"])
async def test_unproven_claim_never_completes_a_mapped_member(test_session, outcome):
    work = await scope(test_session)
    evidence = proof(("00700", outcome), ("00005", "missing"), ("00001", "mapping_gap"))
    with pytest.raises(ValueError):
        await validate_eod_completion(test_session, work, [], evidence, outcome="partial")


@pytest.mark.asyncio
async def test_a_member_no_attempt_fetched_leaves_a_current_release_date_partial(test_session):
    # Retries exhausted without a response: nothing to cite, yet the date must settle.
    work = await scope(test_session)
    evidence = proof(("00700", "missing"), ("00005", "missing"), ("00001", "mapping_gap"))
    result = await validate_eod_completion(test_session, work, [], evidence, outcome="partial")
    assert result["data"] == [] and result["gaps"] == ["00005", "00700"]
    with pytest.raises(ValueError, match="full coverage"):
        await validate_eod_completion(test_session, work, [], evidence, outcome="completed")
    # A daily plan's member is never settled this way.
    work.kind = "daily"
    await test_session.flush()
    with pytest.raises(ValueError, match="gap claim"):
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
    assert result["data"] == [] and len(result["gaps"]) == 2
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


@pytest.mark.asyncio
async def test_unlisted_writes_settle_only_where_the_feed_delivers_them(test_session):
    """A member-scoped feed's extra write is a scope breach; FinLab's is recorded apart."""
    work = await scope(test_session)
    accepted = await run(test_session, status="completed", success_records=2, failed_records=0)
    for key in ("00700", "00005", "09999"):
        test_session.add(DeliveryMemberEvidence(run_id=accepted.run_id, member_key=key))
    await test_session.flush()
    evidence = proof(("00700", "data"), ("00005", "data"), ("00001", "mapping_gap"))
    with pytest.raises(ValueError, match="exceeds the frozen scope"):
        await validate_eod_completion(test_session, work, [accepted], evidence, outcome="partial")

    test_session.add(
        DatasetRegistry(dataset_key="tw_equity_eod", name="TW", market="TW", asset_class="equity")
    )
    await test_session.flush()
    finlab = ProviderWork(
        environment=queue.environment(),
        provider="finlab",
        dataset_key="tw_equity_eod",
        kind="historical",
        stable_key="finlab",
        trade_date=DAY,
        manifest={
            "contract": {"schema_id": "market_eod", "schema_version": 1},
            "members": [{"key": "2330", "mapping_status": "mapped"}],
        },
    )
    test_session.add(finlab)
    written = await run(
        test_session,
        dataset_key="tw_equity_eod",
        source="finlab",
        schema_version=1,
        status="completed",
        success_records=2,
        failed_records=0,
    )
    for key in ("2330", "7799"):
        test_session.add(DeliveryMemberEvidence(run_id=written.run_id, member_key=key))
    await test_session.flush()
    result = await validate_eod_completion(
        test_session,
        finlab,
        [written],
        EODCompletionEvidence(
            version=2,
            schema_id="market_eod",
            schema_version=1,
            trade_date=DAY,
            members=[{"member_key": "2330", "outcome": "data"}],
        ),
        outcome="completed",
    )
    assert result["data"] == ["2330"] and result["unlisted"] == ["7799"]


async def _outside_run(db, work, key, members, **changes):
    """A run whose prepared body names only ``members`` (unlisted or supplement rows)."""
    from app.models.provider_work import ProviderWorkAttempt

    attempt = await db.scalar(
        select(ProviderWorkAttempt).where(ProviderWorkAttempt.work_id == work.work_id)
    )
    if attempt is None:
        attempt = ProviderWorkAttempt(work_id=work.work_id, state="closed", generation=1)
        db.add(attempt)
        await db.flush()
    value = await run(
        db,
        **{
            "dataset_key": "tw_equity_eod",
            "source": "finlab",
            "schema_version": 1,
            "total_records": 1,
            "success_records": 0,
            "failed_records": 1,
            **changes,
        },
    )
    await queue.store_prepared(
        db,
        work.work_id,
        key,
        {
            "body": "{}",
            "body_sha256": "a" * 64,
            "payload_sha256": "b" * 64,
            "canonical_members": members,
            "job_key": None,
            "confirmed": True,
        },
    )
    db.add(
        ProviderWorkReceipt(
            run_id=value.run_id,
            work_id=work.work_id,
            attempt_id=attempt.attempt_id,
            member_key=key,
            payload_sha256="b" * 64,
        )
    )
    await db.flush()
    return value


@pytest.mark.asyncio
async def test_a_failed_unlisted_run_never_decides_the_members(test_session):
    test_session.add(
        DatasetRegistry(dataset_key="tw_equity_eod", name="TW", market="TW", asset_class="equity")
    )
    await test_session.flush()
    work = ProviderWork(
        environment=queue.environment(),
        provider="finlab",
        dataset_key="tw_equity_eod",
        kind="historical",
        stable_key="outside",
        trade_date=DAY,
        manifest={
            "contract": {"schema_id": "market_eod", "schema_version": 1},
            "members": [{"key": "2330", "mapping_status": "mapped"}],
        },
    )
    test_session.add(work)
    await test_session.flush()
    member = await _outside_run(
        test_session,
        work,
        "member",
        ["2330"],
        status="completed",
        success_records=1,
        failed_records=0,
    )
    test_session.add(DeliveryMemberEvidence(run_id=member.run_id, member_key="2330"))
    failed = await _outside_run(test_session, work, "unlisted", ["7799"], status="failed")
    test_session.add(DQIssue(run_id=failed.run_id, severity="error", issue_type="processing_error"))
    await test_session.flush()
    evidence = EODCompletionEvidence(
        version=2,
        schema_id="market_eod",
        schema_version=1,
        trade_date=DAY,
        members=[{"member_key": "2330", "outcome": "data"}],
    )
    result = await validate_eod_completion(
        test_session, work, [member, failed], evidence, outcome="completed"
    )
    assert result["data"] == ["2330"] and result["unlisted"] == []
    # The same failure on a member's own run still refuses the completion.
    test_session.add(DQIssue(run_id=member.run_id, severity="error", issue_type="processing_error"))
    await test_session.flush()
    with pytest.raises(ValueError, match="non-data"):
        await validate_eod_completion(
            test_session, work, [member, failed], evidence, outcome="completed"
        )
