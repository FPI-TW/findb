"""Frozen rows continue after data errors and replay across raw/Source failures."""

import json
import threading
from datetime import date
from types import SimpleNamespace
from uuid import UUID

import pytest

from findb_fetcher.factset_eod import FactSetEOD, completion_status
from findb_fetcher.full_market_universe import checksum
from findb_fetcher.historical_backfill import HistoricalWorkDeferredError
from findb_fetcher.provider_wait import ProviderPublicationPendingError
from findb_fetcher.provider_work import (
    ProviderWorkAcquisitionStopped,
    ProviderWorkRecoveryBlockedError,
    WorkJournal,
)
from findb_fetcher.providers.factset import FactSetGateway, FactSetOperationalError, encode_native
from findb_fetcher.raw_storage import RawObject, RawStorageUploadError


def harness(tmp_path, monkeypatch):
    members = [
        {
            "member_key": f"{i:05}",
            "symbol": f"{i:05}",
            "provider_symbol": f"AAAAA{i}-L",
            "mapping_status": "gap" if i == 5 else "mapped",
            "currency": "HKD",
        }
        for i in range(1, 6)
    ]
    owner = SimpleNamespace(
        provider="factset",
        failed=threading.Event(),
        work={
            "work_id": "original-work",
            "manifest": {"members": [{"key": m["member_key"]} for m in members]},
        },
        journal=WorkJournal(tmp_path / "journal.sqlite3", "original-work"),
    )
    prepared = {}
    owner.restore = lambda job_key: [prepared[job_key]] if job_key in prepared else []

    def reserve(request, *, job_key):
        body = json.dumps(request, sort_keys=True)
        assert prepared.get(job_key, body) == body
        prepared[job_key] = body

    owner.reserve = reserve
    monkeypatch.setattr("findb_fetcher.factset_eod.active", lambda: owner)
    monkeypatch.setattr("findb_fetcher.provider_work.active", lambda: owner)
    queries, deliveries = [], []

    class Gateway(FactSetGateway):
        failure = False
        include_missing = False

        def __init__(self):
            pass

        def _query(self, sql, parameters, *, scope):
            day, identities = parameters[0], parameters[1:]
            queries.append((day, identities))
            if self.failure:
                raise FactSetOperationalError("FACTSET_QUERY_UNAVAILABLE", retryable=True)
            return encode_native(
                [
                    {
                        "fsym_id": f"AAAAA{i}-L",
                        "price_date": day.isoformat(),
                        "currency": "HKD",
                        "price": 10,
                        "price_high": 1 if i == 2 else 11,
                        "price_low": 9,
                        "volume": -1 if i == 3 else 100,
                    }
                    for i in ((1, 2, 3, 4) if self.include_missing else (1, 2, 3))
                ],
                scope={"trade_date": day, "listing_ids": identities},
            )

    class Raw:
        failure = False

        def persist(self, raw, **scope):
            if self.failure:
                raise RawStorageUploadError("temporarily unavailable", retryable=True)
            digest = checksum(raw)
            return RawObject(
                f"r2://{'a' * 32}/raw/factset/hk_equity_eod/member/{digest}.json", digest, len(raw)
            )

    class Delivery:
        def prepare(self, request, *, job_key=None):
            owner.reserve(request, job_key=job_key)
            return request

        def deliver(self, request):
            deliveries.append(request)
            return SimpleNamespace(run_id=UUID(int=int(request["payload"]["data"][0]["symbol"])))

        def get_run_status(self, run_id, *, expected):
            invalid = expected["payload"]["data"][0]["high"] == "1"
            return SimpleNamespace(
                status="completed_with_errors" if invalid else "completed",
                total_records=1,
                success_records=int(not invalid),
                failed_records=int(invalid),
            )

    gateway, raw = Gateway(), Raw()
    runner = FactSetEOD(gateway, Delivery(), raw)
    arguments = {
        "day": date(2026, 10, 15),
        "members": members,
        "dataset": "hk_equity_eod",
        "historical": True,
        "deliveries": {
            m["member_key"]: {
                "slot_id": "asia_pacific_markets_window",
                "work_item_id": "historical:item",
            }
            for m in members
        },
    }
    return runner, arguments, owner, gateway, raw, queries, deliveries


def test_data_errors_continue_and_replay_without_requery_or_new_delivery(tmp_path, monkeypatch):
    runner, arguments, owner, gateway, raw, queries, deliveries = harness(tmp_path, monkeypatch)
    first = runner.run(**arguments)
    assert [r["outcome"] for r in first["members"]] == [
        "data",
        "invalid",
        "invalid",
        "missing",
        "mapping_gap",
    ]
    assert completion_status(first) == "partial"
    assert len(queries) == 1 and len(deliveries) == 2
    assert len(queries[0][1]) == 4
    original = json.dumps(deliveries, sort_keys=True)
    gateway.failure = raw.failure = True
    assert runner.run(**arguments) == first
    assert len(queries) == 1 and len(deliveries) == 2
    assert json.dumps(deliveries, sort_keys=True) == original
    assert owner.journal.manifest() is None  # Runner cannot rewrite the owner's frozen scope.


def test_raw_failure_preserves_snapshot_and_budget_before_tail_retry(tmp_path, monkeypatch):
    runner, arguments, owner, gateway, raw, queries, deliveries = harness(tmp_path, monkeypatch)
    raw.failure = True
    with pytest.raises(HistoricalWorkDeferredError):
        runner.run(**arguments)
    assert len(queries) == 1 and deliveries == []
    raw.failure = False
    gateway.failure = True
    assert completion_status(runner.run(**arguments)) == "partial"
    assert len(queries) == 1 and len(deliveries) == 2
    with owner.journal.connection() as db:
        assert (
            db.execute(
                "SELECT attempts FROM work_budget WHERE operation_key='factset-eod:raw'"
            ).fetchone()[0]
            == 2
        )


def test_transient_query_does_not_become_missing_or_partial(tmp_path, monkeypatch):
    runner, arguments, owner, gateway, raw, queries, deliveries = harness(tmp_path, monkeypatch)
    gateway.failure = True
    with pytest.raises(HistoricalWorkDeferredError):
        runner.run(**arguments)
    assert deliveries == [] and len(queries) == 1
    assert owner.journal.response("factset-eod:snapshot") is None


def test_daily_missing_rows_are_observed_but_requeried_before_completion_window(
    tmp_path, monkeypatch
):
    runner, arguments, owner, gateway, _, queries, deliveries = harness(tmp_path, monkeypatch)
    arguments["historical"] = False
    arguments["defer_missing"] = True

    with pytest.raises(ProviderPublicationPendingError, match="factset_no_source_row"):
        runner.run(**arguments)

    assert len(queries) == 1 and deliveries == []
    assert owner.journal.response("factset-eod:snapshot") is None
    with owner.journal.connection() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM work_response_observation WHERE response_key=?",
                ("factset-eod:snapshot",),
            ).fetchone()[0]
            == 1
        )
        assert (
            db.execute(
                "SELECT attempts FROM work_budget WHERE operation_key=?",
                ("factset-eod:snapshot",),
            ).fetchone()[0]
            == 0
        )

    gateway.include_missing = True
    evidence = runner.run(**arguments)
    assert len(queries) == 2
    assert [row["outcome"] for row in evidence["members"]] == [
        "data",
        "invalid",
        "invalid",
        "data",
        "mapping_gap",
    ]


@pytest.mark.parametrize("dispatched", [False, True])
def test_suspension_refunds_only_undispatched_attempt(tmp_path, monkeypatch, dispatched):
    runner, _, owner, *_ = harness(tmp_path, monkeypatch)
    owner.dispatch_observations = 7

    def action():
        owner.dispatch_observations += int(dispatched)
        raise ProviderWorkAcquisitionStopped("window closed")

    with pytest.raises(HistoricalWorkDeferredError):
        runner._saved("snapshot", action)
    with owner.journal.connection() as db:
        assert db.execute("SELECT attempts FROM work_budget").fetchone()[0] == int(dispatched)
    assert owner.journal.response("factset-eod:snapshot") is None


def test_recovery_uncertainty_is_not_converted_to_safe_retry(tmp_path, monkeypatch):
    runner, _, _, *_ = harness(tmp_path, monkeypatch)

    def action():
        raise ProviderWorkRecoveryBlockedError("fence differs")

    with pytest.raises(ProviderWorkRecoveryBlockedError):
        runner._saved("snapshot", action)


def test_preparation_suspension_preserves_native_snapshot_for_retry(tmp_path, monkeypatch):
    runner, arguments, _, _, _, queries, deliveries = harness(tmp_path, monkeypatch)
    prepare = runner.delivery.prepare

    def stopped(*args, **kwargs):
        raise ProviderWorkAcquisitionStopped("stopped before preparation")

    runner.delivery.prepare = stopped
    with pytest.raises(HistoricalWorkDeferredError):
        runner.run(**arguments)
    assert len(queries) == 1 and not deliveries
    runner.delivery.prepare = prepare
    assert completion_status(runner.run(**arguments)) == "partial"
    assert len(queries) == 1 and len(deliveries) == 2
