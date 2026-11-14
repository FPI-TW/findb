"""One durable acknowledgement per preparation stage, and bounded RPC timing."""

from datetime import datetime, timezone

import pytest

from findb_fetcher import rpc_timing
from findb_fetcher.client import PreparedDelivery, delivered_snapshot

REQUEST = {
    "idempotency_key": "full:stable",
    "dataset_key": "us_equity_eod",
    "schema_id": "market_eod",
    "schema_version": 1,
    "payload": {"data": [{"symbol": "AAPL"}]},
}


def _session(tmp_path, calls):
    from findb_fetcher import provider_work as queue

    class Source:
        def request(self, method, route, body):
            calls.append((route.rsplit("/", 1)[1], body.get("stage"), body.get("job_key")))
            return {}

    journal = queue.WorkJournal(tmp_path / "work.sqlite3", "work")
    return queue.ProviderWorkSession(
        Source(),
        "twelve_data",
        {
            "work_id": "work",
            "provider": "twelve_data",
            "generation": 1,
            "token": "fence",
            "kind": "daily",
            "state": "open",
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
        journal,
    )


def test_each_stage_is_acknowledged_once_for_identical_bytes(tmp_path):
    from findb_fetcher.provider_work import ProviderWorkRecoveryBlockedError

    calls: list[tuple] = []
    owner = _session(tmp_path, calls)
    try:
        prepared = delivered_snapshot(REQUEST)
        # Acquisition reserve, delivery prepare (reserve + confirm), then the
        # delivery headers: the old path sent intent and confirm twice each.
        owner.reserve(REQUEST)
        owner.reserve(REQUEST)
        owner.acknowledge(prepared.body.decode(), "confirm")
        headers = owner.headers(prepared)
        assert calls == [("prepare", "intent", None), ("prepare", "confirm", None)]
        assert headers["X-FinDB-Work-Member"] == "full:stable"
        # A different job binding or different bytes are new evidence.
        owner.reserve(REQUEST, job_key="job")
        owner.reserve({**REQUEST, "idempotency_key": "full:other"})
        assert calls[2:] == [("prepare", "intent", "job"), ("prepare", "intent", None)]
        # A skipped stage still checks the local fence.
        owner.failed.set()
        with pytest.raises(ProviderWorkRecoveryBlockedError):
            owner.reserve(REQUEST)
        assert len(calls) == 4
    finally:
        owner.stop()


def test_failed_stage_is_not_remembered(tmp_path):
    from findb_fetcher.full_market_client import FullMarketProtocolError

    calls: list[tuple] = []
    owner = _session(tmp_path, calls)
    original = owner.source.request

    def reject_once(method, route, body):
        original(method, route, body)
        if len(calls) == 1:
            raise FullMarketProtocolError("rejected", status_code=409)
        return {}

    owner.source.request = reject_once
    try:
        with pytest.raises(FullMarketProtocolError):
            owner.reserve(REQUEST)
        owner.reserve(REQUEST)
        assert calls == [("prepare", "intent", None)] * 2
    finally:
        owner.stop()


def test_delivered_snapshot_matches_prepare_bytes():
    snapshot = delivered_snapshot(REQUEST)
    assert isinstance(snapshot, PreparedDelivery)
    assert snapshot.body == (
        b'{"dataset_key":"us_equity_eod","idempotency_key":"full:stable",'
        b'"payload":{"data":[{"symbol":"AAPL"}]},"schema_id":"market_eod","schema_version":1}'
    )
    assert (snapshot.dataset_key, snapshot.schema_id, snapshot.schema_version) == (
        "us_equity_eod",
        "market_eod",
        1,
    )


def test_rpc_timing_keeps_route_templates_only():
    rpc_timing.drain()
    uuid = "01a1c9f4-d5b0-7b2f-8e5b-1cf19fbc0c7d"
    assert (
        rpc_timing.route("POST", f"provider-work/{uuid}/prepare")
        == "POST provider-work/{id}/prepare"
    )
    assert (
        rpc_timing.route("GET", f"https://source.example/api/v1/source/runs/{uuid}?x=secret")
        == "GET runs/{id}"
    )
    rpc_timing.record("GET runs/{id}", 12.34)
    with rpc_timing.timed("step raw_persist"):
        pass
    totals = rpc_timing.drain()
    assert totals["GET runs/{id}"] == [1, 12.3]
    assert totals["step raw_persist"][0] == 1
    assert rpc_timing.drain() == {}
