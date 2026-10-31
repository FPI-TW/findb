"""Fence, claim, enqueue and preparation never move retained bodies out of PostgreSQL."""

import json
import re
from contextlib import contextmanager
from datetime import date, timedelta

import pytest
from sqlalchemy import event

from app.services import provider_work as queue
from app.utils import utc_now
from tests.test_canonical_ingest_api import _canonical_request
from tests.test_provider_work import identity, seed

# Only Source prepared lookup selects bodies; control paths read keys/descriptors.
BODY_TRANSFER = re.compile(r"^\s*SELECT\b.*\bprovider_work_prepared\.body\b", re.I | re.S)


@contextmanager
def statements(db):
    seen: list[str] = []

    def capture(conn, cursor, statement, params, context, many):
        seen.append(statement)

    engine = db.bind.sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def _retained(body: str) -> dict:
    return {
        "body": body,
        "body_sha256": "a" * 64,
        "payload_sha256": "b" * 64,
        "canonical_members": ["RETAINED"],
        "job_key": "retained-job",
        "confirmed": True,
    }


async def _claimed(db, stable_key="bodies"):
    key = await seed(db, "finlab")
    body = _canonical_request()
    values = {
        "provider": "finlab",
        "stable_key": stable_key,
        "kind": "maintenance",
        "dataset_key": key,
        "trade_date": date.fromisoformat(body["payload"]["batch"]["data_date"]),
    }
    work = await queue.enqueue(db, **values)
    claimed = await queue.claim(
        db, provider="finlab", datasets=[key], host_lock_identity="host:1:22"
    )
    await queue.freeze_manifest(
        db,
        **identity(claimed),
        manifest={
            "mode": "canonical",
            "members": [{"key": row["symbol"]} for row in body["payload"]["data"]],
        },
    )
    return work, claimed, body, values


@pytest.mark.asyncio
async def test_control_path_never_transports_retained_bodies(test_session):
    db = test_session
    work, claimed, body, values = await _claimed(db)
    large = "retained-body-" * 40_000
    await queue.store_prepared(db, work.work_id, "retained", _retained(large))
    fence = identity(claimed)
    with statements(db) as seen:
        assert (await queue.enqueue(db, **values)).work_id == work.work_id
        await queue.heartbeat(db, **fence)
        for stage in ("intent", "confirm"):
            await queue.register_prepared(
                db, **fence, body=json.dumps(body), stage=stage, job_key="original"
            )
        await queue.close(db, **fence)
        await queue.settle(
            db, **fence, outcome="retry", retry_at=utc_now() + timedelta(seconds=60), proof={}
        )
        await queue.claim(
            db, provider="finlab", datasets=[values["dataset_key"]], host_lock_identity="host:1:22"
        )
    assert seen and not [statement for statement in seen if BODY_TRANSFER.search(statement)]
    saved = await queue.prepared_map(db, work.work_id)
    assert saved["retained"]["body"] == large
    assert saved[body["idempotency_key"]]["body"] == json.dumps(body)
    assert saved[body["idempotency_key"]]["confirmed"] is True
    descriptors = await queue.prepared_descriptors(db, work.work_id)
    assert not [row for row in descriptors.values() if "body" in row]
    assert set(descriptors) == {"retained", body["idempotency_key"]}


@pytest.mark.asyncio
async def test_idempotent_enqueue_replay_takes_no_provider_lock(test_session):
    db = test_session
    work, _, _, values = await _claimed(db)
    with statements(db) as seen:
        assert (await queue.enqueue(db, **values)).work_id == work.work_id
    assert not [statement for statement in seen if "pg_advisory_xact_lock" in statement]
    with pytest.raises(queue.ProviderWorkError, match="another scope"):
        await queue.enqueue(db, **{**values, "scope": {"other": True}})


@pytest.mark.asyncio
async def test_prepared_lookup_returns_bodies_matching_any_identity(test_session):
    db = test_session
    work, _, _, _ = await _claimed(db)
    rows = {
        "by-key": {**_retained("key-body"), "canonical_members": ["A"], "job_key": "j1"},
        "by-job": {**_retained("job-body"), "canonical_members": ["B"], "job_key": "j2"},
        "by-member": {**_retained("member-body"), "canonical_members": ["C"], "job_key": None},
    }
    for key, row in rows.items():
        await queue.store_prepared(db, work.work_id, key, row)

    async def bodies(**kwargs):
        return sorted(await queue.prepared_bodies(db, work.work_id, **kwargs))

    assert await bodies() == ["job-body", "key-body", "member-body"]
    assert await bodies(delivery_key="by-key") == ["key-body"]
    assert await bodies(job_key="j2", member_key="C") == ["job-body", "member-body"]
    assert await bodies(member_key="missing") == []
    assert await queue.prepared_keys(db, work.work_id) == list(
        await queue.prepared_map(db, work.work_id)
    )
    assert await queue.has_prepared(db, work.work_id)
    assert set(await queue.prepared_descriptors(db, work.work_id, job_key="j1")) == {"by-key"}
