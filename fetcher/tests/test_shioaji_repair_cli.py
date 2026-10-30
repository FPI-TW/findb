"""Diagnostics use existing SQLite files in read-only mode; approvals use HTTP."""

import hashlib
import json
from types import SimpleNamespace
from uuid import UUID

import pytest

from findb_fetcher.full_market_state import FullMarketState
from findb_fetcher.provider_work import WorkJournal
from findb_fetcher.shioaji_repair_cli import read_checkpoint, run
from test_full_market import _plan


def setup(tmp_path):
    state = FullMarketState(tmp_path / "daily.sqlite3")
    state.bind_profile("full_market")
    state.record_plan(_plan(), "shioaji")
    work_id = str(UUID(int=1))
    journal = WorkJournal(tmp_path / "provider-work.sqlite3", work_id)
    journal.prepare("unchanged", b"immutable original delivery")
    inventory = {
        "work_id": work_id,
        "execution_profile": "full_market",
        "plan_id": "p",
        "trade_date": "2026-07-29",
        "members": [
            {
                "member_key": "AAPL",
                "provider_symbol": "AAPL",
                "mapping_status": "mapped",
                "has_prepared": False,
                "reason": "source_error",
            }
        ],
        "approved_request_ids": [],
    }
    return state, journal, inventory


def test_diagnostics_do_not_create_or_modify_checkpoints(tmp_path):
    state, journal, inventory = setup(tmp_path)
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (state.path, journal.path)
    }
    report = read_checkpoint(state.path, journal.path, inventory)
    assert report["members"][0]["local"]["attempts"] == 0
    assert report["members"][0]["proposed_action"] == "review_observation_grant"
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before} == before
    with pytest.raises(ValueError, match="existing"):
        read_checkpoint(tmp_path / "missing.sqlite3", journal.path, inventory)
    assert not (tmp_path / "missing.sqlite3").exists()
    wrong = {**inventory, "execution_profile": "pilot"}
    with pytest.raises(ValueError, match="profile"):
        read_checkpoint(state.path, journal.path, wrong)


def test_apply_requires_reviewed_digest_and_replay_never_creates_local_grants(
    tmp_path, monkeypatch
):
    state, journal, inventory = setup(tmp_path)
    report = read_checkpoint(state.path, journal.path, inventory)
    proposal = tmp_path / "proposal.json"
    proposal.write_text(
        json.dumps(
            {"request_id": "approved-request", "members": [], "checkpoint_sha256": report["sha256"]}
        )
    )
    args = SimpleNamespace(
        provider="shioaji",
        work_id=inventory["work_id"],
        execution_profile="full_market",
        state_path=state.path,
        diagnose_shioaji=False,
        repair_proposal=proposal,
        apply=True,
        expected_sha256="b" * 64,
    )
    config = SimpleNamespace(source_api_url="https://offline.example", request_timeout_seconds=1)
    monkeypatch.setattr("findb_fetcher.shioaji_repair_cli.FetcherConfig.from_env", lambda: config)

    class Source:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, *args):
            return inventory

    calls = []

    class Admin(Source):
        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return SimpleNamespace(
                status_code=200, json=lambda: {"repair_ids": ["approved"], "replayed": False}
            )

    monkeypatch.setattr("findb_fetcher.shioaji_repair_cli.FullMarketSourceClient", Source)
    monkeypatch.setattr("findb_fetcher.shioaji_repair_cli.httpx.Client", Admin)
    monkeypatch.setenv("FINDB_ADMIN_API_KEY", "offline-owner")
    run(args, tmp_path / "governor.sqlite3")
    assert calls[0][1]["json"]["expected_sha256"] == "b" * 64
    inventory["approved_request_ids"] = ["approved-request"]
    run(args, tmp_path / "governor.sqlite3")
    assert len(calls) == 2
    with journal.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM work_shioaji_grant").fetchone()[0] == 0
    inventory["approved_request_ids"] = []
    inventory["state"] = "changed"
    with pytest.raises(ValueError, match="stale"):
        run(args, tmp_path / "governor.sqlite3")
    assert len(calls) == 2
