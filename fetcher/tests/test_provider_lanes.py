"""Per-lane consumer identity: files, wire compatibility and skew guards (#691).

The packaged catalog runs FinLab's four intraday feeds in an ``intraday`` lane
(#691 step 3b); every other feed stays in the default lane.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from execution_config_support import publication
from findb_fetcher import execution_metadata as metadata
from findb_fetcher import full_market_cli
from findb_fetcher import provider_work as queue
from findb_fetcher.config import FetcherConfig
from findb_fetcher.execution_config import resolve_execution_config
from findb_fetcher.scheduler_control import SchedulerControlClient
from test_full_market import _runtime

INTRADAY = ("tw_equity_bar_1m", "tw_etf_bar_1m", "tw_equity_tick", "tw_etf_tick")
EOD = ("tw_equity_eod", "tw_etf_eod")
NOW = datetime(2026, 11, 20, tzinfo=timezone.utc).timestamp()
OLD = "2026-11-12T00:00:00+00:00"
DEFAULT_WORK = "019a0000-0000-7000-8000-000000000021"
LANE_WORK = "019a0000-0000-7000-8000-000000000022"


@pytest.fixture
def intraday_lane():
    """The packaged descriptors, where FinLab's intraday feeds run in their own lane."""
    assert full_market_cli._LANES["finlab"] == {"default": set(EOD), "intraday": set(INTRADAY)}


def test_packaged_descriptors_run_only_finlab_intraday_outside_the_default_lane():
    for provider in metadata.provider_ids():
        if provider == "finlab":
            assert metadata.provider_lanes(provider) == ("default", "intraday")
            continue
        assert metadata.provider_lanes(provider) == ("default",)
        assert metadata.lane_feeds(provider) == metadata.acquisition_feeds(provider)
    assert {
        (feed["provider"], feed["dataset_key"])
        for feed in metadata.provider_feeds()
        if feed["lane"] != "default"
    } == {("finlab", key) for key in INTRADAY}


def test_lane_feeds_split_one_provider(intraday_lane):
    assert metadata.provider_lanes("finlab") == ("default", "intraday")
    assert {f["dataset_key"] for f in metadata.lane_feeds("finlab")} == set(EOD)
    assert {f["dataset_key"] for f in metadata.lane_feeds("finlab", "intraday")} == set(INTRADAY)
    assert metadata.provider_lanes("twelve_data") == ("default",)
    with pytest.raises(metadata.ExecutionMetadataError):
        metadata.lane_feeds("twelve_data", "intraday")


def test_default_lane_keeps_every_pre_lane_file_name(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENVIRONMENT", "staging")
    assert queue.host_lock_path("finlab", root=tmp_path) == (
        tmp_path / "provider-work-staging-finlab.lock"
    )
    assert queue.host_lock_path("finlab", "intraday", root=tmp_path) == (
        tmp_path / "provider-work-staging-finlab-intraday.lock"
    )
    assert queue.journal_path(tmp_path, "finlab") == tmp_path / "provider-work-finlab.sqlite3"
    assert queue.journal_path(tmp_path, "finlab", "intraday") == (
        tmp_path / "provider-work-finlab-intraday.sqlite3"
    )
    assert full_market_cli.default_state_path("finlab") == Path(
        "/var/lib/findb-full-market/state-finlab.sqlite3"
    )
    assert full_market_cli.default_state_path("finlab", "intraday") == Path(
        "/var/lib/findb-full-market/state-finlab-intraday.sqlite3"
    )


@pytest.mark.parametrize("lane", ["", "Intraday", "intra-day", "../x", "a" * 31])
def test_lane_file_names_are_bounded(tmp_path, lane):
    with pytest.raises(ValueError):
        queue.host_lock_path("finlab", lane, root=tmp_path)
    with pytest.raises(ValueError):
        queue.journal_path(tmp_path, "finlab", lane)


def test_each_lane_owns_a_distinct_host_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENVIRONMENT", "local")
    with queue.host_lock("finlab", root=tmp_path) as (_, default_identity):
        # Another lane of the same provider runs beside the default owner.
        with queue.host_lock("finlab", "intraday", root=tmp_path) as (_, lane_identity):
            assert lane_identity != default_identity
            with pytest.raises(queue.ProviderWorkWaitingError):
                with queue.host_lock("finlab", "intraday", root=tmp_path):
                    pytest.fail("one lane has one owner")
        with pytest.raises(queue.ProviderWorkWaitingError):
            with queue.host_lock("finlab", root=tmp_path):
                pytest.fail("one lane has one owner")
    queue.initialize_host_lock("finlab", "intraday", root=tmp_path)
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "provider-work-local-finlab-intraday.lock",
        "provider-work-local-finlab.lock",
    ]


def _journal_work(path: Path, key: str) -> None:
    journal = queue.WorkJournal(path, key)
    journal.freeze({"members": [{"key": "m"}]})
    with journal.connection() as db:
        db.execute(
            "INSERT INTO work_delivery VALUES(?,?,?,?,?,?)",
            (key, "m", b"{}", "0" * 64, "run", None),
        )


def test_lane_journal_lookup_keeps_works_where_they_were_journaled(tmp_path):
    lane_file = queue.journal_path(tmp_path, "finlab", "intraday")
    default_file = queue.journal_path(tmp_path, "finlab")
    shared = queue.journal_path(tmp_path)
    # A new work of the lane goes to the lane's own file.
    assert queue.work_journal_path("finlab", LANE_WORK, lane="intraday", root=tmp_path) == (
        lane_file
    )
    # A work journaled by the default consumer before the lane existed stays there.
    _journal_work(default_file, DEFAULT_WORK)
    assert queue.work_journal_path("finlab", DEFAULT_WORK, lane="intraday", root=tmp_path) == (
        default_file
    )
    # The host-wide file comes first, as for the default lane.
    _journal_work(shared, LANE_WORK)
    assert queue.work_journal_path("finlab", LANE_WORK, lane="intraday", root=tmp_path) == shared
    # The default lane never reads a lane's file.
    _journal_work(lane_file, "019a0000-0000-7000-8000-000000000023")
    assert queue.work_journal_path(
        "finlab", "019a0000-0000-7000-8000-000000000023", root=tmp_path
    ) == (default_file)


class _ClosedSource:
    def __init__(self, closed: dict[str, str]) -> None:
        self.closed = closed

    def request(self, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        assert (method, path) == ("POST", "provider-work/closed")
        return {
            "data": [
                {"work_id": key, "stable_key": key, "state": "completed", "completed_at": at}
                for key, at in self.closed.items()
                if key in body["work_ids"]
            ]
        }


def _keys(path: Path) -> set[str]:
    """Works whose replay state is still journaled (FinLab keeps its manifests)."""
    with sqlite3.connect(path) as db:
        return {row[0] for row in db.execute("SELECT work_key FROM work_delivery")}


def test_each_lane_prunes_only_its_own_files(tmp_path):
    lane_file = queue.journal_path(tmp_path, "finlab", "intraday")
    default_file = queue.journal_path(tmp_path, "finlab")
    _journal_work(lane_file, LANE_WORK)
    _journal_work(default_file, DEFAULT_WORK)
    source = _ClosedSource({LANE_WORK: OLD, DEFAULT_WORK: OLD})

    removed = queue.prune_closed_journal(source, "finlab", lane="intraday", root=tmp_path, now=NOW)
    assert removed is not None and removed["works"] == 1
    assert _keys(lane_file) == set()
    assert _keys(default_file) == {DEFAULT_WORK}

    removed = queue.prune_closed_journal(source, "finlab", root=tmp_path, now=NOW)
    assert removed is not None and removed["works"] == 1
    assert _keys(default_file) == set()
    # The default lane never touched the lane's file; its claim is per file.
    with sqlite3.connect(lane_file) as db:
        assert db.execute("SELECT provider FROM work_retention").fetchall() == [("finlab",)]


class _ClaimSource:
    def __init__(self, work: dict[str, Any] | None = None) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.work = work

    def request(self, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        assert (method, path) == ("POST", "provider-work/claim")
        self.bodies.append(body)
        return {"work": self.work}


def test_default_claim_sends_the_pre_lane_request(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENVIRONMENT", "local")
    source = _ClaimSource()
    with queue.claimed_work(source, "finlab", accepted_kinds=["daily"], root=tmp_path) as owner:
        assert owner is None
    assert set(source.bodies[0]) == {
        "protocol",
        "host_lock_identity",
        "recover",
        "requested_work_id",
        "accepted_kinds",
        "execution_profile",
    }


def test_lane_claim_names_its_lane_and_journals_in_its_file(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENVIRONMENT", "local")
    work = {
        "work_id": LANE_WORK,
        "provider": "finlab",
        "generation": 1,
        "token": "fence",
        "kind": "daily",
        "state": "open",
        "lane": "intraday",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    source = _ClaimSource(work)
    monkeypatch.setattr(queue.ProviderWorkSession, "publish_cooldown", lambda self: None)
    monkeypatch.setattr(queue.ProviderWorkSession, "_beat", lambda self: None)
    with pytest.raises(queue.ProviderWorkRecoveryBlockedError):
        with queue.claimed_work(source, "finlab", lane="intraday", root=tmp_path) as owner:
            assert owner is not None
            assert queue.work_lane(owner.work) == "intraday"
            assert owner.journal.path == queue.journal_path(tmp_path, "finlab", "intraday")
    body = source.bodies[0]
    assert body["lane"] == "intraday"
    lock = queue.host_lock_path("finlab", "intraday", root=tmp_path).stat()
    assert body["host_lock_identity"] == f"local:{lock.st_dev}:{lock.st_ino}"


def test_work_lane_defaults_when_source_names_none():
    assert queue.work_lane({"work_id": "x"}) == "default"
    assert queue.work_lane({"lane": "intraday"}) == "intraday"


class _SentError(Exception):
    pass


@pytest.mark.parametrize("lane", ["default", "intraday"])
def test_scheduler_poll_names_only_a_non_default_lane(lane):
    config = FetcherConfig("https://source.example", "private", max_attempts=1)
    sent: list[bytes] = []
    with httpx.Client() as http:
        control = SchedulerControlClient(config, "full_market_finlab_v1", client=http, lane=lane)

        def capture(body: bytes) -> dict[str, Any]:
            sent.append(body)
            raise _SentError

        control._request = capture  # type: ignore[method-assign]
        with pytest.raises(_SentError):
            control.poll(observed_state="stopped")
    body = json.loads(sent[0])
    if lane == "default":
        assert sent[0] == (
            b'{"cycle_completed_at":null,"cycle_started_at":null,'
            b'"last_error":null,"observed_state":"stopped"}'
        )
    else:
        assert body["lane"] == "intraday"


def test_scheduler_control_rejects_an_invalid_lane():
    config = FetcherConfig("https://source.example", "private", max_attempts=1)
    with pytest.raises(ValueError):
        SchedulerControlClient(config, "full_market_finlab_v1", lane="Intra-day")


def _cli(*args: str) -> int:
    return full_market_cli.main(["--config", "/nonexistent.json", *args])


def test_cli_lane_must_be_one_of_the_providers_lanes(intraday_lane, capsys):
    with pytest.raises(SystemExit) as raised:
        _cli("--provider", "twelve_data", "--lane", "intraday", "--check")
    assert raised.value.code == 2
    assert "not a lane of provider 'twelve_data'" in capsys.readouterr().err
    with pytest.raises(SystemExit) as raised:
        _cli("--provider", "finlab", "--lane", "unknown", "--check")
    assert raised.value.code == 2


def test_cli_lane_initializes_its_own_lock_and_checkpoint(intraday_lane, tmp_path, monkeypatch):
    config = Path(__file__).resolve().parents[1] / "configs/bootstrap.local.v1.json"
    monkeypatch.setenv("APP_ENVIRONMENT", "local")
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(tmp_path / "account" / "governor.sqlite3"))
    checkpoint = tmp_path / "state-finlab-intraday.sqlite3"
    result = full_market_cli.main(
        [
            "--config",
            str(config),
            "--provider",
            "finlab",
            "--lane",
            "intraday",
            "--state-path",
            str(checkpoint),
            "--initialize-state",
        ]
    )
    assert result == 0
    assert checkpoint.is_file()
    assert sorted(os.listdir(tmp_path / "account")) == ["provider-work-local-finlab-intraday.lock"]


def _finlab_publication() -> dict[str, Any]:
    return publication("finlab", profile="full_market", environment="local")


def test_execution_config_exposes_only_the_lanes_feeds(intraday_lane):
    value = _finlab_publication()
    default = resolve_execution_config(
        value, provider="finlab", environment="local", profile="full_market"
    )
    lane = resolve_execution_config(
        value, provider="finlab", environment="local", profile="full_market", lane="intraday"
    )
    assert {feed["dataset_key"] for feed in default.feeds} == set(EOD)
    assert {feed["dataset_key"] for feed in lane.feeds} == set(INTRADAY)
    assert default.digest == lane.digest


def test_execution_config_without_the_lanes_feeds_plans_nothing(intraday_lane):
    from execution_config_support import signed

    value = _finlab_publication()
    value["feeds"] = [feed for feed in value["feeds"] if feed["dataset_key"] in INTRADAY]
    signed(value)
    with pytest.raises(ValueError, match="no installed feed"):
        resolve_execution_config(
            value, provider="finlab", environment="local", profile="full_market"
        )


def test_runtime_installs_only_its_lanes_feeds(intraday_lane, tmp_path, monkeypatch):
    from findb_fetcher.full_market_runtime import FullMarketRuntime
    from findb_fetcher.full_market_state import FullMarketState

    monkeypatch.setenv("APP_ENVIRONMENT", "local")
    for lane, own, other in (("default", EOD, INTRADAY), ("intraday", INTRADAY, EOD)):
        runtime = FullMarketRuntime(
            provider="finlab",
            config={},
            state=FullMarketState(tmp_path / f"state-{lane}.sqlite3"),
            source=None,  # type: ignore[arg-type]
            delivery=None,  # type: ignore[arg-type]
            raw_store=None,  # type: ignore[arg-type]
            lane=lane,
        )
        try:
            assert {feed["dataset_key"] for feed in runtime.installed_feeds} == set(own)
            assert runtime.other_lane_datasets == frozenset(other)
        finally:
            runtime.close()


def _mismatch_runtime(tmp_path, monkeypatch, *, lane, work):
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(tmp_path / "account.sqlite3"))
    runtime, source, _ = _runtime(tmp_path, provider="finlab", lane=lane)
    runtime._ensure_proof = lambda: None
    runtime._plan_for_date = lambda *args: pytest.fail("another lane's work cannot be planned")
    runtime._acquire = lambda *args: pytest.fail("another lane's work cannot acquire")
    runtime.sync_universes = lambda **kwargs: pytest.fail("another lane's work cannot refresh")
    runtime._settle_retired_work = lambda owner: pytest.fail("another lane's feed is not retired")
    settlements: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []

    def request(method, route, body=None):
        if route.startswith("full-market/authorization"):
            return {"acquisition_allowed": False}
        if route == "provider-work/claim":
            claims.append(body)
            return {"work": work}
        if route.endswith("/settle"):
            settlements.append(body)
        return {}

    source.request = request
    return runtime, settlements, claims


def _work(dataset: str, *, kind: str = "daily", **extra: Any) -> dict[str, Any]:
    return {
        "work_id": "019a0000-0000-7000-8000-000000000031",
        "stable_key": f"daily:full_market:{dataset}:2026-11-20",
        "generation": 1,
        "token": "fence",
        "kind": kind,
        "dataset_key": dataset,
        "state": "open",
        "trade_date": "2026-11-20",
        "scope": {},
        **extra,
    }


@pytest.mark.parametrize(
    ("lane", "work"),
    [
        # A Backend still running the feed in the default lane hands it to the
        # default consumer of an image that runs it in the intraday lane.
        ("default", _work("tw_equity_bar_1m")),
        ("default", _work("tw_etf_tick", kind="maintenance")),
        ("default", _work("tw_equity_tick", kind="rerun")),
        # Source claimed a work in another lane than the one asked for.
        ("intraday", _work("tw_equity_bar_1m", lane="default")),
        # This lane's request, but a feed this image runs in the default lane.
        ("intraday", _work("tw_equity_eod", lane="intraday")),
    ],
)
def test_lane_mismatch_waits_for_the_owning_lane(intraday_lane, tmp_path, monkeypatch, lane, work):
    runtime, settlements, claims = _mismatch_runtime(tmp_path, monkeypatch, lane=lane, work=work)
    try:
        before = datetime.now(timezone.utc)
        runtime._queued_cycle(plan_new_dates=False)
    finally:
        runtime.close()
    assert ("lane" in claims[0]) is (lane != "default")
    assert len(settlements) == 1
    settled = settlements[0]
    assert settled["outcome"] == "retry"
    assert settled["failure_code"] == "LANE_MISMATCH"
    assert settled["proof"]["lane_mismatch"]["consumer_lane"] == lane
    delay = (datetime.fromisoformat(settled["retry_at"]) - before).total_seconds()
    assert 590 <= delay <= 610


def test_matching_lane_work_is_not_a_mismatch(intraday_lane, tmp_path, monkeypatch):
    runtime, settlements, _ = _mismatch_runtime(
        tmp_path, monkeypatch, lane="intraday", work=_work("tw_equity_bar_1m", lane="intraday")
    )
    try:
        owner = type("Owner", (), {"work": _work("tw_equity_bar_1m", lane="intraday")})()
        assert runtime._settle_lane_mismatch(owner) is False
    finally:
        runtime.close()
    assert settlements == []
