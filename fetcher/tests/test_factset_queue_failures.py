"""Known daily failures settle under the real owner; uncertain fences do not."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from findb_fetcher import provider_work as queue
from findb_fetcher.client import SourceAPIResponseError
from findb_fetcher.full_market_client import FullMarketProtocolError
from findb_fetcher.historical_backfill import HistoricalWorkDeferredError
from findb_fetcher.providers.factset import FactSetOperationalError
from findb_fetcher.raw_storage import RawStorageUploadError
from test_full_market import _plan, _runtime


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (FactSetOperationalError("FACTSET_QUERY_REJECTED", retryable=False), "failed"),
        (FactSetOperationalError("FACTSET_QUERY_UNAVAILABLE", retryable=True), "retry"),
        (RawStorageUploadError("rejected", retryable=False), "failed"),
        (RawStorageUploadError("unavailable", retryable=True), "retry"),
        (SourceAPIResponseError(422), "failed"),
        (SourceAPIResponseError(503), "retry"),
        (SourceAPIResponseError(429), "retry"),
        (HistoricalWorkDeferredError("deferred"), "retry"),
        (queue.ProviderWorkAcquisitionStopped("stopped"), "retry"),
    ],
)
def test_daily_known_failure_drains_settles_and_releases_owner(
    tmp_path, monkeypatch, error, outcome
):
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(tmp_path / "account.sqlite3"))
    runtime, source, _ = _runtime(tmp_path, provider="factset")
    runtime._ensure_proof = lambda: None
    plan = {**_plan(), "status": "pending", "release_id": "original-release"}
    runtime._plan_for_date = lambda *args: plan
    source.release = lambda *args: {}
    events, settlements = [], []

    def request(method, route, body=None):
        events.append(route.rsplit("/", 1)[-1])
        if route.startswith("full-market/authorization"):
            return {"acquisition_allowed": False}
        if route == "provider-work/claim":
            return {
                "work": {
                    "work_id": "original",
                    "generation": 1,
                    "token": "fence",
                    "kind": "daily",
                    "dataset_key": "us_equity_eod",
                    "state": "open",
                    "trade_date": "2026-10-15",
                    "scope": {"feed": runtime.feeds[0]},
                }
            }
        if route.endswith("/prepared"):
            return {"member_keys": ["original-prepared"]}
        if route.endswith("/settle"):
            settlements.append(body)
            if len(settlements) == 1:
                raise FullMarketProtocolError(
                    "accepted runs pending", status_code=409, code="PROVIDER_WORK_DRAINING"
                )
        return {}

    source.request = request

    def fail(*args):
        assert queue.inherited_fds()
        raise error

    runtime._deliver_factset_plan = fail
    monkeypatch.setattr(queue.time, "sleep", lambda _: None)
    try:
        runtime._queued_cycle()
        assert len(settlements) == 2
        assert all(s["outcome"] == outcome for s in settlements)
        assert events.index("close") < events.index("settle")
        if outcome == "failed":
            assert settlements[-1]["proof"]["manual_pending_prepared"] == ["original-prepared"]
        else:
            assert settlements[-1]["retry_at"]
        assert queue.active() is None
        # The next consumer can obtain the same shared inode after settlement.
        with queue.host_lock("factset", root=tmp_path):
            pass
    finally:
        runtime.close()


def test_daily_source_fence_is_never_settled_as_known_failure(tmp_path, monkeypatch):
    runtime, source, _ = _runtime(tmp_path, provider="factset")
    runtime._ensure_proof = lambda: None
    source.request = lambda *args: {"acquisition_allowed": False}
    source.release = lambda *args: {}
    runtime._plan_for_date = lambda *args: {
        **_plan(),
        "status": "pending",
        "release_id": "original-release",
    }
    owner = SimpleNamespace(
        work={
            "kind": "daily",
            "dataset_key": "us_equity_eod",
            "state": "open",
            "trade_date": "2026-10-15",
            "scope": {"feed": runtime.feeds[0]},
        }
    )

    @contextmanager
    def claimed(*args, **kwargs):
        yield owner

    monkeypatch.setattr(queue, "claimed_work", claimed)
    runtime._settle_queued_round = lambda *args, **kwargs: pytest.fail("unsafe settlement")

    def fail(*args):
        raise SourceAPIResponseError(409, "PROVIDER_WORK_FENCE")

    runtime._deliver_factset_plan = fail
    try:
        with pytest.raises(queue.ProviderWorkRecoveryBlockedError):
            runtime._queued_cycle()
    finally:
        runtime.close()


@pytest.mark.parametrize("dispatched", [False, True])
def test_maintenance_suspension_preserves_dispatch_budget(tmp_path, dispatched):
    runtime, _, _ = _runtime(tmp_path, provider="factset")
    runtime.installed_feeds[0]["dataset_key"] = "hk_equity_eod"
    owner = SimpleNamespace(
        work={"state": "open", "dataset_key": "hk_equity_eod"},
        journal=queue.WorkJournal(tmp_path / "work.sqlite3", "original"),
        dispatch_observations=3,
    )
    settlements = []
    runtime._settle_queued_round = lambda *args, **kwargs: settlements.append(kwargs)

    def stop(**kwargs):
        owner.dispatch_observations += int(dispatched)
        raise queue.ProviderWorkAcquisitionStopped("control stopped")

    runtime.sync_universes = stop
    try:
        assert runtime._run_queued_maintenance(owner)[0]["status"] == "queued"
        assert settlements[0]["outcome"] == "retry"
        with owner.journal.connection() as db:
            assert db.execute("SELECT attempts FROM work_budget").fetchone()[0] == int(dispatched)
    finally:
        runtime.close()
