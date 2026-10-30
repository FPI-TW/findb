"""Calendar closure reads immutable evidence under the existing shared host lock."""

import hashlib
import json
from types import SimpleNamespace
from uuid import UUID

import pytest

from findb_fetcher import calendar_closure_cli as tool
from findb_fetcher.full_market_state import FullMarketState
from findb_fetcher.provider_work import (
    ProviderWorkWaitingError,
    WorkJournal,
    host_lock,
    initialize_host_lock,
)
from test_full_market import _plan


def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENVIRONMENT", "local")
    state = FullMarketState(tmp_path / "state.sqlite3")
    state.bind_profile("full_market")
    state.record_plan(_plan(), "shioaji")
    account = FullMarketState(tmp_path / "governor.sqlite3")
    wid = str(UUID(int=1))
    journal = WorkJournal(tmp_path / "provider-work.sqlite3", wid)
    journal.freeze({"mode": "canonical", "plan_id": "p", "members": [{"key": "AAPL"}]})
    initialize_host_lock("shioaji", root=tmp_path)
    inventory = {
        "work_id": wid,
        "environment": "local",
        "plan_id": "p",
        "execution_profile": "full_market",
        "sha256": "a" * 64,
        "snapshot": {"work": [{"dataset_key": "us_equity_eod", "trade_date": "2026-10-01"}]},
    }
    config = SimpleNamespace(source_api_url="https://offline.example", request_timeout_seconds=1)
    monkeypatch.setattr(tool.FetcherConfig, "from_env", lambda: config)
    monkeypatch.setenv("FINDB_ADMIN_API_KEY", "test-only-owner-key")
    calls = []

    class Source:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def request(self, *a):
            return inventory

    class Admin(Source):
        def post(self, url, **kw):
            calls.append((url, kw))
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"request": kw["json"], "sha256": "c" * 64, "apply_ready": True},
            )

    monkeypatch.setattr(tool, "FullMarketSourceClient", Source)
    monkeypatch.setattr(tool.httpx, "Client", Admin)
    proposal = tmp_path / "proposal.json"
    proposal.write_text(
        json.dumps(
            {
                "request_id": "calendar-1",
                "reason": "Confirmed closed day and retained all original evidence",
            }
        )
    )
    args = SimpleNamespace(
        provider="shioaji",
        work_id=wid,
        closure_proposal=proposal,
        apply=False,
        expected_sha256=None,
        execution_profile="full_market",
        state_path=state.path,
    )
    return state, account, journal, args, inventory, calls


def fingerprints(tmp_path):
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in tmp_path.iterdir()
        if p.suffix in {".sqlite3", ".lock"}
    }


def test_preview_apply_replay_no_sqlite_mutation_or_sdk(tmp_path, monkeypatch):
    _, account, _, args, _, calls = setup(tmp_path, monkeypatch)
    before = fingerprints(tmp_path)
    report = tool.run(args, account.path)
    assert report["request"]["checkpoint"]["facts"]["prepared_count"] == 0
    assert report["request"]["checkpoint"]["facts"]["local"]["members"][0]["attempts"] == 0
    assert calls[-1][0].endswith("/calendar-closure/preview")
    args.closure_proposal.write_text(json.dumps(report))
    args.apply = True
    args.expected_sha256 = report["sha256"]
    tool.run(args, account.path)
    tool.run(args, account.path)
    assert calls[-1][0].endswith("/calendar-closure")
    assert calls[-1][1]["json"]["expected_sha256"] == "c" * 64
    assert fingerprints(tmp_path) == before


def test_missing_lock_and_missing_checkpoint_never_initialize(tmp_path, monkeypatch):
    _, account, _, args, _, calls = setup(tmp_path, monkeypatch)
    lock = tmp_path / "provider-work-local-shioaji.lock"
    lock.unlink()
    with pytest.raises(ValueError, match="existing shared"):
        tool.run(args, account.path)
    assert not lock.exists() and not calls
    initialize_host_lock("shioaji", root=tmp_path)
    args.state_path = tmp_path / "missing.sqlite3"
    with pytest.raises(ValueError, match="existing regular"):
        tool.run(args, account.path)
    assert not args.state_path.exists()


def test_active_host_owner_blocks_preview(tmp_path, monkeypatch):
    _, account, _, args, _, calls = setup(tmp_path, monkeypatch)
    with host_lock("shioaji", root=tmp_path):
        with pytest.raises(ProviderWorkWaitingError):
            tool.run(args, account.path)
    assert not calls


def test_apply_rejects_changed_checkpoint_missing_digest_and_corrupt_cache(tmp_path, monkeypatch):
    state, account, journal, args, _, calls = setup(tmp_path, monkeypatch)
    report = tool.run(args, account.path)
    args.closure_proposal.write_text(json.dumps(report))
    args.apply = True
    with pytest.raises(ValueError, match="expected-sha256"):
        tool.run(args, account.path)
    args.expected_sha256 = "c" * 64
    with state.connection() as db:
        db.execute("UPDATE full_work SET attempts=8")
    with pytest.raises(ValueError, match="stale"):
        tool.run(args, account.path)
    assert len(calls) == 1
    with journal.connection() as db:
        db.execute(
            "INSERT INTO work_response VALUES(?,?,?,?)", (journal.key, "cache", b"{}", "a" * 64)
        )
    with pytest.raises(ValueError, match="checksum"):
        tool.run(args, account.path)


def test_source_outage_prevents_owner_apply(tmp_path, monkeypatch):
    _, account, _, args, _, calls = setup(tmp_path, monkeypatch)

    class Outage:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            raise ValueError("Source outage")

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(tool, "FullMarketSourceClient", Outage)
    with pytest.raises(ValueError, match="Source outage"):
        tool.run(args, account.path)
    assert not calls


def test_ack_loss_replays_durable_original_after_unrelated_local_history(tmp_path, monkeypatch):
    state, account, _, args, inventory, calls = setup(tmp_path, monkeypatch)
    report = tool.run(args, account.path)
    args.closure_proposal.write_text(json.dumps(report))
    args.apply, args.expected_sha256 = True, report["sha256"]
    body = {**report["request"], "expected_sha256": args.expected_sha256}
    inventory["snapshot"]["closure"] = [
        {
            "work_id": args.work_id,
            "request_id": body["request_id"],
            "request_sha256": tool.digest(body),
        }
    ]
    with state.connection() as db:
        db.execute("UPDATE full_work SET attempts=8")
    tool.run(args, account.path)
    assert calls[-1][1]["json"] == body
