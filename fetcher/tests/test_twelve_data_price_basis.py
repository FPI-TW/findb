"""Split-adjusted durable responses never replay; affected work still settles for review."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from findb_fetcher import provider_work as queue
from findb_fetcher.full_market_client import FullMarketProtocolError
from findb_fetcher.providers.twelve_data import (
    TWELVE_DATA_RESPONSE_BASIS,
    TwelveDataClient,
    TwelveDataConfig,
)

FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "twelve_data" / "aapl_1day_2024-01-02_2024-01-05.json"
)
DAY = date(2024, 1, 5)
ORIGIN = "incompatible_basis:" + TWELVE_DATA_RESPONSE_BASIS


def _encoded(raw: bytes) -> bytes:
    return json.dumps(
        {
            "type": "TwelveDataResponse",
            "data": {"payload": json.loads(raw), "raw_bytes": base64.b64encode(raw).decode()},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _base(symbol: str = "AAPL", exchange: str | None = None) -> str:
    identity = {
        "symbol": symbol,
        "start_date": DAY,
        "end_date": DAY,
        "outputsize": None,
        "exchange": exchange,
    }
    encoded = json.dumps(identity, sort_keys=True, default=str, separators=(",", ":"))
    return "twelve_daily:" + hashlib.sha256(encoded.encode()).hexdigest()


def _adjusted_raw(close: str = "1.25") -> bytes:
    payload = json.loads(FIXTURE_PATH.read_text())
    # A pre-basis response priced under adjust=splits for a later split.
    payload["values"] = [{**payload["values"][-1], "close": close}]
    return json.dumps(payload).encode()


def _body(raw: bytes, key: str = "twelve_data:old") -> dict:
    """A prepared ingress body whose provenance names the native raw bytes."""
    return {
        "source": "twelve_data",
        "idempotency_key": key,
        "dataset_key": "us_equity_eod",
        "schema_id": "market_eod",
        "schema_version": 1,
        "payload": {"batch": {"source_raw_sha256": hashlib.sha256(raw).hexdigest()}},
    }


def _offline_client() -> TwelveDataClient:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("pre-basis replay must not refetch the provider")

    return TwelveDataClient(
        TwelveDataConfig(api_key="offline-secret"),
        client=httpx.Client(transport=httpx.MockTransport(refuse)),
    )


def _observations(journal: queue.WorkJournal) -> list[tuple[str, str]]:
    with journal.connection() as db:
        return [
            tuple(row)
            for row in db.execute(
                "SELECT response_key,origin FROM work_response_observation ORDER BY rowid"
            )
        ]


class _Ledger:
    """Source preparation ledger plus settlement replies for one owned work."""

    def __init__(self, bodies: dict[str, dict], *, kind: str = "historical") -> None:
        self.bodies = {key: json.dumps(body) for key, body in bodies.items()}
        self.kind = kind
        self.closed = False
        self.settles: list[dict] = []
        self.delivered: list[str] = []

    def preparation(self, action, *, keys_only=False, delivery_key=None, pending_only=False, **_):
        if keys_only:
            keys = set(self.bodies) - (set(self.delivered) if pending_only else set())
            return {"member_keys": sorted(keys)}
        return {"bodies": [self.bodies[delivery_key]]}

    def request(self, action, **body):
        if action == "close":
            self.closed = True
        if action != "settle":
            return {}
        self.settles.append(body)
        # Mirrors backend/app/services/provider_work.py settle(); a bare 409 is a
        # ProviderWorkError, which the session escalates to recovery.
        if not self.closed:
            raise FullMarketProtocolError("close acquisition first", status_code=409)
        manual = set(body["proof"].get("manual_pending_prepared", []))
        pending = set(self.bodies) - set(self.delivered)
        if (
            pending
            and body["outcome"] != "retry"
            and not (
                body["outcome"] == "failed"
                and body["failure_code"]
                and pending <= manual <= set(self.bodies)
            )
        ):
            raise FullMarketProtocolError(
                "drain", status_code=409, code="PROVIDER_WORK_PREPARED_DRAINING"
            )
        if "pending_source_errors" in body["proof"] and (
            body["outcome"] != "retry" or self.kind != "daily"
        ):
            raise FullMarketProtocolError("pending source errors require retry", status_code=409)
        return {}


def _session(tmp_path, monkeypatch, ledger: _Ledger, *, provider="twelve_data", **work):
    work.setdefault("kind", ledger.kind)
    import findb_fetcher.client as client_module

    class Delivery:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def deliver(self, prepared):
            ledger.delivered.append(json.loads(prepared.body)["idempotency_key"])
            return SimpleNamespace(run_id="run")

    monkeypatch.setattr(client_module, "SourceAPIClient", Delivery)
    monkeypatch.setattr("findb_fetcher.contracts.ContractRegistry", lambda *a, **kw: None)
    journal = queue.WorkJournal(tmp_path / "journal.sqlite3", "original")
    session = queue.ProviderWorkSession(
        SimpleNamespace(config=SimpleNamespace(contracts_dir=tmp_path), client=None),
        provider,
        {"work_id": "original", "generation": 1, "token": "t", **work},
        journal,
    )
    session._preparation_request = ledger.preparation
    session.request = ledger.request
    session.publish_cooldown = lambda: None
    return session, journal


@pytest.mark.parametrize("suffix", [":validated-target-v1", ""])
def test_pre_basis_daily_response_blocks_without_replay_or_refetch(tmp_path, suffix):
    journal = queue.WorkJournal(tmp_path / "journal.sqlite3", "original")
    legacy_key = _base() + suffix
    legacy = _encoded(_adjusted_raw())
    journal.response(legacy_key, legacy)
    token = queue._current.set(SimpleNamespace(journal=journal))
    try:
        client = _offline_client()
        for _ in range(2):
            with pytest.raises(queue.ProviderWorkBasisMismatchError, match="basis"):
                client.fetch_daily("AAPL", start_date=DAY, end_date=DAY)
    finally:
        queue._current.reset(token)
    # Original bytes stay authoritative evidence; one observation marks them incompatible.
    assert journal.response(legacy_key) == legacy
    assert _observations(journal) == [(legacy_key, ORIGIN)]
    # An ordinary failure that callers settle, never an owner-quarantining recovery block.
    assert not issubclass(
        queue.ProviderWorkBasisMismatchError, queue.ProviderWorkRecoveryBlockedError
    )


def test_current_basis_response_replays_without_provider_io(tmp_path):
    journal = queue.WorkJournal(tmp_path / "journal.sqlite3", "original")
    raw = json.dumps(json.loads(FIXTURE_PATH.read_text())).encode()
    journal.response(
        _base() + ":" + TWELVE_DATA_RESPONSE_BASIS + ":validated-target-v1", _encoded(raw)
    )
    token = queue._current.set(SimpleNamespace(journal=journal))
    try:
        replay = _offline_client().fetch_daily("AAPL", start_date=DAY, end_date=DAY)
    finally:
        queue._current.reset(token)
    assert replay.raw_bytes == raw
    assert _observations(journal) == []


def test_pre_basis_scan_reads_only_unscoped_daily_keys(tmp_path):
    journal = queue.WorkJournal(tmp_path / "journal.sqlite3", "original")
    old, new = _adjusted_raw(), _adjusted_raw("9.99")
    journal.response(_base() + ":validated-target-v1", _encoded(old))
    journal.response(
        _base("MSFT") + ":" + TWELVE_DATA_RESPONSE_BASIS + ":validated-target-v1", _encoded(new)
    )
    journal.response("twelve_catalogue:" + "0" * 64, b"not a daily response")
    assert set(journal.pre_basis_raw_sha256("twelve_daily")) == {hashlib.sha256(old).hexdigest()}


def test_cancelled_work_with_pre_basis_prepared_settles_failed_and_releases_owner(
    tmp_path, monkeypatch
):
    old, fresh = _adjusted_raw(), _adjusted_raw("2.00")
    ledger = _Ledger({"old": _body(old, "old"), "fresh": _body(fresh, "fresh")})
    session, journal = _session(tmp_path, monkeypatch, ledger)
    journal.response(_base() + ":validated-target-v1", _encoded(old))

    session.settle("cancelled", proof={"reason": "historical_request_stopped"})

    assert session.settled is True
    # Only the current-basis body is replayed; the pre-basis body stays retained.
    assert ledger.delivered == ["fresh"]
    first, final = ledger.settles
    assert first["outcome"] == "cancelled"
    assert final["outcome"] == "failed"
    assert final["failure_code"] == "PRICE_BASIS_MANUAL_REVIEW"
    assert final["proof"]["manual_pending_prepared"] == ["fresh", "old"]
    assert final["proof"]["price_basis_blocked"] == ["old"]
    assert final["proof"]["reason"] == "historical_request_stopped"
    assert _observations(journal) == [(_base() + ":validated-target-v1", ORIGIN)]
    assert journal.response(_base() + ":validated-target-v1") == _encoded(old)


def test_drain_only_retry_with_pre_basis_prepared_becomes_manual_failure(tmp_path, monkeypatch):
    old = _adjusted_raw()
    ledger = _Ledger({"old": _body(old, "old")})
    session, journal = _session(tmp_path, monkeypatch, ledger, drain_only=True)
    journal.response(_base() + ":validated-target-v1", _encoded(old))
    session.drain_prepared()
    session.settle("retry", failure_code="PROVIDER_ACCOUNT_COOLDOWN", delay=60)
    assert [body["outcome"] for body in ledger.settles] == ["failed"]
    assert "retry_at" not in ledger.settles[0]
    assert ledger.settles[0]["failure_code"] == "PRICE_BASIS_MANUAL_REVIEW"


def test_existing_failure_code_keeps_identity_and_gains_waiver(tmp_path, monkeypatch):
    old = _adjusted_raw()
    ledger = _Ledger({"old": _body(old, "old")})
    session, journal = _session(tmp_path, monkeypatch, ledger)
    journal.response(_base() + ":validated-target-v1", _encoded(old))
    session.settle(
        "failed",
        failure_code="RETIRED_FEED_MANUAL_REVIEW",
        proof={"manual_reason": "retired_feed_requires_manual_review"},
    )
    final = ledger.settles[-1]
    assert final["failure_code"] == "RETIRED_FEED_MANUAL_REVIEW"
    assert final["proof"]["manual_reason"] == "retired_feed_requires_manual_review"
    assert final["proof"]["manual_pending_prepared"] == ["old"]


@pytest.mark.parametrize("provider", ["twelve_data", "finlab"])
def test_drain_replays_bodies_without_pre_basis_provenance(tmp_path, monkeypatch, provider):
    # A pre-basis response exists, but no pending body was mapped from it.
    ledger = _Ledger({"fresh": _body(_adjusted_raw("2.00"), "fresh")})
    session, journal = _session(tmp_path, monkeypatch, ledger, provider=provider)
    journal.response(_base() + ":validated-target-v1", _encoded(_adjusted_raw()))
    assert len(session.drain_prepared(pending_only=True)) == 1
    assert ledger.delivered == ["fresh"] and session.basis_blocked == set()
    session.settle("completed")
    assert [body["outcome"] for body in ledger.settles] == ["completed"]


def test_empty_pending_set_never_blocks_a_work_with_pre_basis_history(tmp_path, monkeypatch):
    ledger = _Ledger({})
    session, journal = _session(tmp_path, monkeypatch, ledger)
    journal.response(_base() + ":validated-target-v1", _encoded(_adjusted_raw()))
    assert session.drain_prepared(pending_only=True) == []
    session.settle("completed")
    assert ledger.settles[0]["outcome"] == "completed"
    assert _observations(journal) == []


def test_historical_prepared_body_from_pre_basis_response_is_not_delivered(tmp_path, contracts_dir):
    from test_twelve_data_scheduler import NOW, FakeProvider, FakeSource, _executor, _job

    source = FakeSource()
    assert _executor(contracts_dir, source=source).execute(_job(), now=NOW).succeeded
    prepared = source.requests[0]
    old = _adjusted_raw()
    prepared["payload"]["batch"]["source_raw_sha256"] = hashlib.sha256(old).hexdigest()
    journal = queue.WorkJournal(tmp_path / "journal.sqlite3", "original")
    journal.response(_base(exchange="NASDAQ") + ":validated-target-v1", _encoded(old))
    owner = queue.ProviderWorkSession(
        None, "twelve_data", {"work_id": "original", "generation": 1, "token": "t"}, journal
    )
    replaying = FakeSource()
    provider = FakeProvider(AssertionError("prepared replay must not refetch"))
    token = queue._current.set(owner)
    try:
        with pytest.raises(queue.ProviderWorkBasisMismatchError):
            _executor(contracts_dir, provider=provider, source=replaying).execute(
                replace(_job(), prepared_request=prepared), now=NOW
            )
        assert replaying.requests == []
        # A body whose raw bytes were acquired under the current basis still replays.
        prepared["payload"]["batch"]["source_raw_sha256"] = "f" * 64
        assert (
            _executor(contracts_dir, provider=provider, source=replaying)
            .execute(replace(_job(), prepared_request=prepared), now=NOW)
            .succeeded
        )
    finally:
        queue._current.reset(token)


def test_historical_basis_failure_ends_item_through_manual_waiver(monkeypatch):
    from findb_fetcher.historical_backfill import _run_once

    events = []
    monkeypatch.setattr(
        queue, "require_owner", lambda provider: SimpleNamespace(work={"state": "open"})
    )
    item = SimpleNamespace(provider="twelve_data")
    control = SimpleNamespace(
        provider="twelve_data",
        claim=lambda: item,
        authorize=lambda item: None,
        fail=lambda item, code: events.append(("fail", code)),
    )

    def run(item):
        raise queue.ProviderWorkBasisMismatchError("prepared body predates basis")

    assert _run_once(control, SimpleNamespace(run=run))
    assert events == [("fail", "PREPARED_DELIVERY_MANUAL_REVIEW")]


@pytest.mark.parametrize("prepared", [False, True])
def test_full_daily_pre_basis_member_is_manual_and_others_continue(tmp_path, prepared):
    from provider_work_support import owned_unit_work
    from test_full_market import _member, _plan, _runtime

    runtime, source, _ = _runtime(tmp_path)
    runtime._acquisition_allowed = lambda: True
    acquired = []
    old = _adjusted_raw()

    def acquire(member, *args):
        acquired.append(member["member_key"])
        if member["member_key"] == "AAPL":
            raise queue.ProviderWorkBasisMismatchError("response predates basis")
        raise RuntimeError("stop after the next member")

    runtime._acquire = acquire
    plan = _plan()
    runtime.state.record_plan(plan, runtime.provider)
    body = json.dumps([_body(old, "full:original")]).encode()
    if prepared:
        runtime.state.prepare("fp1:123:AAPL", body)
    try:
        with owned_unit_work(runtime.provider, tmp_path) as owner:
            owner.journal.response(_base() + ":validated-target-v1", _encoded(old))
            assert not runtime._deliver_plan(
                plan,
                {"members": [_member(), {**_member(), "member_key": "MSFT"}]},
                runtime.feeds[0],
            )
        # The pre-basis member is retained for review; the next member is still processed.
        assert acquired == (["MSFT"] if prepared else ["AAPL", "MSFT"])
        assert source.outcomes[0] == {
            "work_item_id": "fp1:123",
            "member_key": "AAPL",
            "reason": "source_error",
        }
        with runtime.state.connection() as db:
            row = db.execute(
                "SELECT status,reason FROM full_work WHERE member_key='AAPL'"
            ).fetchone()
        assert tuple(row) == ("manual", "price_basis_manual_review")
        assert runtime.state.prepared("fp1:123:AAPL") == (body if prepared else None)
        # A retained ledger body arms the drain-only conversion so the work can terminate.
        assert owner.basis_blocked == ({"full:original"} if prepared else set())
        assert [d["category"] for d in runtime.state.diagnostics("fp1:123:AAPL")] == [
            "price_basis_mismatch"
        ]
    finally:
        runtime.close()


def test_diagnostic_write_failure_never_suppresses_the_source_report(tmp_path, monkeypatch):
    from provider_work_support import owned_unit_work
    from test_full_market import _member, _plan, _runtime

    runtime, source, _ = _runtime(tmp_path)
    runtime._acquisition_allowed = lambda: True

    def acquire(*args):
        raise RuntimeError("private provider detail")

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    runtime._acquire = acquire
    monkeypatch.setattr(runtime.state, "record_diagnostic", locked)
    plan = _plan()
    runtime.state.record_plan(plan, runtime.provider)
    try:
        with owned_unit_work(runtime.provider, tmp_path):
            assert not runtime._deliver_plan(plan, {"members": [_member()]}, runtime.feeds[0])
        assert source.outcomes == [
            {"work_item_id": "fp1:123", "member_key": "AAPL", "reason": "source_error"}
        ]
        assert not runtime.state.pending_source_error_keys("p")
    finally:
        runtime.close()


@pytest.mark.parametrize("path", ["/historical-backfills/item/complete", "/other"])
def test_historical_drain_conflict_with_blocked_body_ends_item_instead_of_looping(path):
    from findb_fetcher.historical_backfill import HistoricalControlClient

    sent = []
    drains = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        if len(sent) == 1:
            return httpx.Response(409, json={"detail": {"code": "PROVIDER_WORK_PREPARED_DRAINING"}})
        return httpx.Response(200, json={"success": True})

    owner = SimpleNamespace(
        work={"work_id": "w", "generation": 1, "token": "t"},
        basis_blocked={"old"},
        drain_prepared=lambda: drains.append("drain") or [],
    )
    config = SimpleNamespace(source_client_key="key", source_api_url="https://findb.example.test")
    token = queue._current.set(owner)
    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            control = HistoricalControlClient(config, client=http, provider="twelve_data")
            body = {"status": "completed", "run_id": "r", "lease_token": "lease"}
            if path.endswith("/complete"):
                assert control._request("POST", path, body) == {"success": True}
                # The completion is resent as the Source manual-review waiver.
                assert sent[1] == {
                    "status": "failed",
                    "lease_token": "lease",
                    "failure_code": "PREPARED_DELIVERY_MANUAL_REVIEW",
                    "failure_message": "prepared Twelve Data body predates the price basis",
                }
            else:
                with pytest.raises(queue.ProviderWorkBasisMismatchError):
                    control._request("POST", path, body)
                assert len(sent) == 1
    finally:
        queue._current.reset(token)
    assert drains == ["drain"]


@pytest.mark.parametrize(
    "work",
    [
        {"state": "open", "scope_stopped": True},
        {"state": "open", "drain_only": True},
        {"state": "closed"},
        {"state": "draining"},
    ],
)
def test_historical_drain_branches_end_blocked_item_through_domain_fail(monkeypatch, work):
    from findb_fetcher.historical_backfill import _run_once

    events = []
    owner = SimpleNamespace(work=work, basis_blocked=set())

    def drain():
        events.append("drain")
        owner.basis_blocked.add("old")
        return []

    owner.drain_prepared = drain
    owner.settle = lambda *args, **kwargs: events.append(("settle", args))
    monkeypatch.setattr(queue, "require_owner", lambda provider: owner)
    item = SimpleNamespace(provider="twelve_data")
    control = SimpleNamespace(
        provider="twelve_data",
        claim=lambda: item,
        complete=lambda *args: events.append("complete"),
        fail=lambda item, code: events.append(("fail", code)),
    )
    assert _run_once(control, SimpleNamespace(run=lambda item: pytest.fail("no acquisition")))
    # The historical item/request reach their domain terminal state; no raw settle.
    assert events == ["drain", ("fail", "PREPARED_DELIVERY_MANUAL_REVIEW")]


def test_accepted_pre_basis_body_replays_its_existing_receipt(tmp_path, monkeypatch):
    old = _adjusted_raw()
    ledger = _Ledger({"old": _body(old, "old")})
    ledger.delivered.append("old")
    session, journal = _session(tmp_path, monkeypatch, ledger)
    journal.response(_base() + ":validated-target-v1", _encoded(old))
    # Source already accepted it, so replay only returns the original run.
    assert len(session.drain_prepared()) == 1
    assert session.basis_blocked == set()


def test_unreported_member_errors_keep_retry_until_reported(tmp_path, monkeypatch):
    old = _adjusted_raw()
    ledger = _Ledger({"old": _body(old, "old")}, kind="daily")
    session, journal = _session(tmp_path, monkeypatch, ledger, drain_only=True)
    journal.response(_base() + ":validated-target-v1", _encoded(old))
    session.drain_prepared()
    session.settle("retry", proof={"pending_source_errors": ["AAPL"]})
    assert [body["outcome"] for body in ledger.settles] == ["retry"]
    assert ledger.settles[0]["proof"] == {"pending_source_errors": ["AAPL"]}


def test_daily_drain_only_round_with_blocked_member_settles_through_queued_round(
    tmp_path, monkeypatch
):
    from provider_work_support import owned_unit_work
    from test_full_market import _member, _plan, _runtime

    runtime, source, _ = _runtime(tmp_path)
    runtime._acquisition_allowed = lambda: True
    runtime._acquire = lambda *args: pytest.fail("drain-only rounds never acquire")
    plan = _plan()
    runtime.state.record_plan(plan, runtime.provider)
    old = _adjusted_raw()
    runtime.state.prepare("fp1:123:AAPL", json.dumps([_body(old, "full:original")]).encode())
    # Source evaluation: MSFT was never acquired in this drain-only round.
    source.evaluate = lambda plan_id: {**plan, "status": "incomplete"}
    ledger = _Ledger({"full:original": _body(old, "full:original")}, kind="daily")
    try:
        with owned_unit_work(runtime.provider, tmp_path) as owner:
            owner.journal.freeze({"plan_id": plan["plan_id"]})
            owner.journal.response(_base() + ":validated-target-v1", _encoded(old))
            owner.work.update(kind="daily", drain_only=True)
            runtime._deliver_plan(
                plan,
                {"members": [_member(), {**_member(), "member_key": "MSFT"}]},
                runtime.feeds[0],
                drain_only=True,
            )
            owner.publish_cooldown = lambda: None
            owner._preparation_request = ledger.preparation
            owner.request = ledger.request
            runtime._settle_queued_round(owner)
        # Twelve Data has no daily policy: a manual member settles the round failed,
        # and the retained pre-basis body travels under the explicit waiver.
        final = ledger.settles[-1]
        assert final["outcome"] == "failed"
        assert final["failure_code"] == "MANUAL_INTERVENTION_REQUIRED"
        assert final["proof"]["manual_pending_prepared"] == ["full:original"]
        assert final["proof"]["price_basis_blocked"] == ["full:original"]
        assert ledger.delivered == []
    finally:
        runtime.close()
