"""Split-adjusted durable responses never replay after the as-traded price basis."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from findb_fetcher import provider_work as queue
from findb_fetcher.providers.twelve_data import (
    TWELVE_DATA_RESPONSE_BASIS,
    TwelveDataClient,
    TwelveDataConfig,
)

FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "twelve_data" / "aapl_1day_2024-01-02_2024-01-05.json"
)
DAY = date(2024, 1, 5)


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


def _adjusted_raw() -> bytes:
    payload = json.loads(FIXTURE_PATH.read_text())
    # A pre-basis response priced under adjust=splits for a later split.
    payload["values"] = [{**payload["values"][-1], "close": "1.25"}]
    return json.dumps(payload).encode()


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
    assert _observations(journal) == [
        (legacy_key, "incompatible_basis:" + TWELVE_DATA_RESPONSE_BASIS)
    ]
    assert journal.pre_basis_responses("twelve_daily", TWELVE_DATA_RESPONSE_BASIS) == [
        (legacy_key, legacy)
    ]


def test_current_basis_response_replays_without_provider_io(tmp_path):
    journal = queue.WorkJournal(tmp_path / "journal.sqlite3", "original")
    raw = json.dumps(json.loads(FIXTURE_PATH.read_text())).encode()
    journal.response(
        _base() + ":" + TWELVE_DATA_RESPONSE_BASIS + ":validated-target-v1", _encoded(raw)
    )
    # A re-acquired identity supersedes its pre-basis bytes without deleting them.
    journal.response(_base() + ":validated-target-v1", _encoded(_adjusted_raw()))
    token = queue._current.set(SimpleNamespace(journal=journal))
    try:
        replay = _offline_client().fetch_daily("AAPL", start_date=DAY, end_date=DAY)
    finally:
        queue._current.reset(token)
    assert replay.raw_bytes == raw
    assert journal.pre_basis_responses("twelve_daily", TWELVE_DATA_RESPONSE_BASIS) == []
    assert _observations(journal) == []


def test_historical_prepared_body_from_pre_basis_response_is_not_delivered(tmp_path, contracts_dir):
    from test_twelve_data_scheduler import FakeProvider, FakeSource, _executor, _job

    journal = queue.WorkJournal(tmp_path / "journal.sqlite3", "original")
    source = FakeSource()
    first = _executor(contracts_dir, source=source)
    assert first.execute(_job(), now=_now()).succeeded
    prepared = source.requests[0]
    journal.response(_base(exchange="NASDAQ") + ":validated-target-v1", _encoded(_adjusted_raw()))
    replaying = FakeSource()
    provider = FakeProvider(AssertionError("prepared replay must not refetch"))
    token = queue._current.set(SimpleNamespace(journal=journal))
    try:
        with pytest.raises(queue.ProviderWorkBasisMismatchError):
            _executor(contracts_dir, provider=provider, source=replaying).execute(
                replace(_job(), prepared_request=prepared), now=_now()
            )
        assert replaying.requests == []
        # The same body proceeds once its source response exists under the current basis.
        journal.response(
            _base(exchange="NASDAQ") + ":" + TWELVE_DATA_RESPONSE_BASIS + ":validated-target-v1",
            _encoded(FIXTURE_PATH.read_bytes()),
        )
        assert (
            _executor(contracts_dir, provider=provider, source=replaying)
            .execute(replace(_job(), prepared_request=prepared), now=_now())
            .succeeded
        )
    finally:
        queue._current.reset(token)


@pytest.mark.parametrize(("provider", "blocked"), [("twelve_data", True), ("finlab", False)])
def test_prepared_drain_refuses_twelve_data_work_with_pre_basis_responses(
    tmp_path, monkeypatch, provider, blocked
):
    import findb_fetcher.client as client_module

    class OfflineDelivery:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

    monkeypatch.setattr(client_module, "SourceAPIClient", OfflineDelivery)
    monkeypatch.setattr("findb_fetcher.contracts.ContractRegistry", lambda *args, **kwargs: None)
    delivered = []

    class Source:
        config = SimpleNamespace(contracts_dir=tmp_path)
        client = None

        def request(self, method, route, body):
            return {"bodies": [], "member_keys": []}

    journal = queue.WorkJournal(tmp_path / "journal.sqlite3", "original")
    journal.response(_base() + ":validated-target-v1", _encoded(_adjusted_raw()))
    session = queue.ProviderWorkSession(
        Source(), provider, {"work_id": "original", "generation": 1, "token": "t"}, journal
    )
    session._preparation_request = lambda *args, **kwargs: (
        delivered.append(kwargs) or {"bodies": [], "member_keys": []}
    )
    if blocked:
        with pytest.raises(queue.ProviderWorkBasisMismatchError):
            session.drain_prepared(pending_only=True)
        assert delivered == []
    else:
        assert session.drain_prepared(pending_only=True) == []


def test_full_daily_prepared_replay_refuses_pre_basis_work(tmp_path):
    from provider_work_support import owned_unit_work
    from test_full_market import _member, _plan, _runtime

    runtime, source, _ = _runtime(tmp_path)
    runtime._acquisition_allowed = lambda: True
    runtime._acquire = lambda *args: pytest.fail("prepared replay must not reacquire")
    plan = _plan()
    runtime.state.record_plan(plan, runtime.provider)
    runtime.state.prepare("fp1:123:AAPL", b'[{"idempotency_key":"full:original"}]')
    try:
        with owned_unit_work(runtime.provider, tmp_path) as owner:
            owner.journal.response(_base() + ":validated-target-v1", _encoded(_adjusted_raw()))
            with pytest.raises(queue.ProviderWorkBasisMismatchError):
                runtime._deliver_plan(plan, {"members": [_member()]}, runtime.feeds[0])
        assert source.outcomes == []
        assert runtime.state.prepared("fp1:123:AAPL") == b'[{"idempotency_key":"full:original"}]'
    finally:
        runtime.close()


def _now():
    from test_twelve_data_scheduler import NOW

    return NOW
