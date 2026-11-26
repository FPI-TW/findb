"""Backfill dates settle complete when only unavailable members are absent (#593).

A mapping gap is a member the provider cannot supply. A current-release cohort can
also hold a member that no release in effect on the backfilled date listed yet.
Neither keeps a date partial; any other absent member still does.
"""

from datetime import date, datetime, timezone
from uuid import UUID

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.provider_work import ProviderWork, ProviderWorkAttempt
from app.models.registry import HistoricalBackfillItem, HistoricalBackfillRequest
from app.schemas.eod_completion import EODCompletionEvidence
from app.schemas.full_market import UniversePublishRequest
from app.services import historical_backfill
from app.services.eod_completion import not_yet_listed, validate_eod_completion
from app.services.full_market import publish_universe, submit_universe
from app.services.source_clients import create_source_client
from scripts.seed_data import seed_datasets
from tests.test_eod_completion import scope
from tests.test_full_market import _universe
from tests.test_provider_work import attest_cutover

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
LISTED, NEW = "00001", "00002"


def _raw(key: str) -> dict:
    digest = f"{int(key):064x}"
    return {
        "ref": f"r2://{'a' * 32}/raw/factset/hk_equity_eod/{key}/{digest}.json",
        "sha256": digest,
    }


async def _publish(db, count: int, effective: date, *, first: bool) -> None:
    body = _universe("hk_equity_eod", count, effective)
    for member in body.members:
        member.provider_symbol = f"{member.symbol}-L"
    release = await submit_universe(db, body, allowed_datasets=["hk_equity_eod"])
    await publish_universe(
        db,
        release.release_id,
        UniversePublishRequest(
            evidence_note="Verified official listing.",
            **(
                {"first_baseline_approved": True}
                if first
                else {"threshold_exception_approved": True}
            ),
        ),
        actor="test-owner",
    )


async def _setup(db, monkeypatch) -> dict:
    """10-14 lists only 00001; the current 10-20 list adds 00002."""
    monkeypatch.setattr(get_settings(), "PROVIDER_WORK_REQUIRED", True)
    monkeypatch.setattr("app.services.provider_work.utc_now", lambda: NOW)
    monkeypatch.setattr("app.services.full_market.utc_now", lambda: NOW)
    monkeypatch.setattr(historical_backfill, "utc_now", lambda: NOW)
    await seed_datasets(db)
    await attest_cutover(db)
    await _publish(db, 1, date(2026, 10, 14), first=True)
    await _publish(db, 2, date(2026, 10, 20), first=False)
    _, key = await create_source_client(
        db,
        name="FactSet",
        source_name="factset",
        allowed_datasets=["hk_equity_eod"],
        rate_limit_requests=100000,
        rate_limit_window=60,
        commit=False,
    )
    await db.commit()
    return {"X-API-Key": key}


async def _complete(client, headers: dict, outcomes: dict[str, str]) -> tuple[dict, dict]:
    """Claim the next date and report ``outcomes`` as a partial date, as a Fetcher does."""
    claimed = await client.post(
        "/api/v1/source/provider-work/claim",
        headers=headers,
        json={"protocol": "provider-work-v1", "host_lock_identity": "local:1:22", "recover": True},
    )
    assert claimed.status_code == 200, claimed.text
    owner = claimed.json()["work"]
    work_headers = {
        **headers,
        "X-FinDB-Work-Id": owner["work_id"],
        "X-FinDB-Work-Generation": str(owner["generation"]),
        "X-FinDB-Work-Token": owner["token"],
    }
    lease = await client.post("/api/v1/source/historical-backfills/claim", headers=work_headers)
    assert lease.status_code == 200, lease.text
    item = lease.json()
    frozen = item["universe_snapshot"]
    manifest = {
        "mode": "canonical",
        "contract": {"schema_id": "market_eod", "schema_version": 2},
        "universe_release_id": frozen["release_id"],
        "members": [
            {
                "key": m["member_key"],
                "source_symbol": m["provider_symbol"],
                "mapping_status": m["mapping_status"],
            }
            for m in frozen["members"]
        ],
    }
    stored = await client.post(
        f"/api/v1/source/provider-work/{owner['work_id']}/manifest",
        headers=headers,
        json={"generation": owner["generation"], "token": owner["token"], "manifest": manifest},
    )
    assert stored.status_code == 200, stored.text
    evidence = {
        "version": 2,
        "schema_id": "market_eod",
        "schema_version": 2,
        "trade_date": owner["trade_date"],
        "item_id": item["item_id"],
        "request_id": item["request_id"],
        "members": [
            {"member_key": key, "outcome": outcome}
            | ({"raw": _raw(key)} if outcome == "no_data" else {})
            for key, outcome in sorted(outcomes.items())
        ],
    }
    body = {"status": "partial", "lease_token": owner["token"], "result_evidence": evidence}
    url = f"/api/v1/source/historical-backfills/{item['item_id']}/complete"
    complete = await client.post(url, headers=work_headers, json=body)
    assert complete.status_code == 200, complete.text
    # The Fetcher's retry of the same report replays the settled date.
    replay = await client.post(url, headers=work_headers, json=body)
    assert replay.status_code == 200 and replay.json() == complete.json(), replay.text
    return item, owner


async def _settled(db, item: dict, work_id: str) -> tuple[str, str, dict]:
    """The date's item status, its work's state and the settlement proof."""
    db.expire_all()
    stored = await db.get(HistoricalBackfillItem, UUID(item["item_id"]))
    work = await db.get(ProviderWork, UUID(work_id))
    attempt = await db.scalar(
        select(ProviderWorkAttempt).where(ProviderWorkAttempt.work_id == work.work_id)
    )
    return stored.status, work.state, attempt.proof


@pytest.mark.asyncio
async def test_a_member_listed_after_the_date_does_not_keep_it_partial(
    client, test_session, test_engine, monkeypatch
):
    headers = await _setup(test_session, monkeypatch)
    request, _ = await historical_backfill.create_request(
        test_session,
        provider="factset",
        dataset_key="hk_equity_eod",
        start_date=date(2026, 10, 15),
        end_date=date(2026, 10, 16),
        request_key="factset-listed-later",
        created_by="owner",
    )
    request_id = request.request_id
    await test_session.commit()

    # 00002 joined the list after both dates; only 00001's absence is a gap.
    item, owner = await _complete(client, headers, {LISTED: "no_data", NEW: "missing"})
    status, state, proof = await _settled(test_session, item, owner["work_id"])
    assert status == state == "completed"
    assert proof["unavailable"] == {NEW: "not_yet_trading"}
    assert proof["historical_item_status"] == "completed"
    # The Fetcher's own report is kept as sent.
    assert proof["historical_completion"]["outcome"] == "partial"

    item, owner = await _complete(client, headers, {LISTED: "missing", NEW: "missing"})
    status, state, proof = await _settled(test_session, item, owner["work_id"])
    assert status == state == "partial"
    assert proof["unavailable"] == {NEW: "not_yet_trading"}

    test_session.expire_all()
    loaded = await test_session.get(HistoricalBackfillRequest, request_id)
    assert loaded.status == "partial"
    items = await test_session.scalars(
        select(HistoricalBackfillItem)
        .where(HistoricalBackfillItem.request_id == request_id)
        .order_by(HistoricalBackfillItem.trade_date)
    )
    assert [item.status for item in items] == ["completed", "partial"]


@pytest.mark.asyncio
async def test_only_a_release_in_effect_on_the_date_can_prove_a_later_listing(
    test_session, monkeypatch
):
    await _setup(test_session, monkeypatch)
    kwargs = {"provider": "factset", "dataset_key": "hk_equity_eod", "keys": {LISTED, NEW}}
    assert await not_yet_listed(test_session, trade_date=date(2026, 10, 15), **kwargs) == {NEW}
    # From the release that lists it, the member is no longer new.
    assert await not_yet_listed(test_session, trade_date=date(2026, 10, 20), **kwargs) == set()
    # Before the feed's first release nothing is known about the date.
    assert await not_yet_listed(test_session, trade_date=date(2026, 10, 13), **kwargs) == set()
    # Another provider's releases say nothing about this one's listings.
    kwargs["provider"] = "hkex"
    assert await not_yet_listed(test_session, trade_date=date(2026, 10, 15), **kwargs) == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "partial"])
async def test_mapping_gaps_alone_complete_a_date(test_session, outcome):
    # ``partial`` is what a Fetcher released before this change still reports.
    work = await scope(test_session)
    evidence = EODCompletionEvidence.model_validate(
        {
            "version": 2,
            "schema_id": "market_eod",
            "schema_version": 2,
            "trade_date": work.trade_date,
            "members": [
                {"member_key": "00700", "outcome": "no_data", "raw": _raw("700")},
                {"member_key": "00005", "outcome": "no_data", "raw": _raw("5")},
                {"member_key": "00001", "outcome": "mapping_gap"},
            ],
        }
    )
    result = await validate_eod_completion(test_session, work, [], evidence, outcome=outcome)
    assert result["gaps"] == [] and result["unavailable"] == {"00001": "mapping_gap"}

    evidence.members[0].outcome, evidence.members[0].raw = "missing", None
    with pytest.raises(ValueError, match="full coverage"):
        await validate_eod_completion(test_session, work, [], evidence, outcome="completed")
    # Nothing absent at all is never partial.
    evidence.members[0].outcome, evidence.members[0].raw = "no_data", evidence.members[1].raw
    evidence.members[2].outcome = "no_data"
    evidence.members[2].raw = evidence.members[1].raw
    with pytest.raises(ValueError, match="partial requires gaps"):
        await validate_eod_completion(test_session, work, [], evidence, outcome="partial")
