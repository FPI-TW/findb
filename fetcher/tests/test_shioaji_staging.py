from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from findb_fetcher.client import (
    PreparedDelivery,
    SourceAPIDeadlineExceeded,
    SourceAPIResponseError,
)
from findb_fetcher.contracts import ContractValidationError
from findb_fetcher.providers.shioaji import (
    ShioajiKbarsSnapshot,
    ShioajiSdkError,
    build_market_minute_request,
)
from findb_fetcher.raw_storage import RawObject
from findb_fetcher.shioaji_staging import (
    EXPECTED,
    Coordinator,
    Result,
    StagingManifestError,
    load_manifest,
)
from findb_fetcher.shioaji_staging_cli import main
from findb_fetcher.shioaji_staging_state import (
    SCHEMA_VERSION,
    ShioajiStagingState,
    ShioajiStagingStateError,
)


def test_manifest_is_exact_and_rejects_reorder_unknown_duplicate_and_oversize(
    tmp_path: Path,
) -> None:
    good = {
        "version": 1,
        "universe_id": "shioaji_tw_staging_v1",
        "governance": {
            "max_requests": 50,
            "rolling_seconds": 60,
            "max_attempts_per_sequence": 3,
            "cutoff": "17:00",
            "timezone": "Asia/Taipei",
        },
        "sequences": list(EXPECTED),
    }
    for changed in (
        lambda v: v.update(extra=True),
        lambda v: v.update(sequences=list(reversed(EXPECTED))),
        lambda v: v.update(sequences=list(EXPECTED[:-1])),
    ):
        value = dict(good)
        changed(value)
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(value))
        with pytest.raises(StagingManifestError):
            load_manifest(path)
    duplicate = tmp_path / "dup.json"
    duplicate.write_text('{"version":1,"version":1}')
    with pytest.raises(StagingManifestError):
        load_manifest(duplicate)
    large = tmp_path / "large.json"
    large.write_bytes(b" " * (17 * 1024))
    with pytest.raises(StagingManifestError):
        load_manifest(large)
    committed = Path(__file__).resolve().parents[1] / "configs" / "shioaji_tw_staging.v1.json"
    assert load_manifest(committed)["sequences"] == list(EXPECTED)


def test_state_persists_snapshot_identity_attempts_limit_and_lease(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    now = datetime(2026, 7, 29, tzinfo=timezone.utc)
    rows = [dict(x) for x in EXPECTED]
    state = ShioajiStagingState(path)
    state.ensure("d", "2026-07-29", "u", "update", rows)
    equity = state.sequence("d", "2330")
    etf1 = state.sequence("d", "0050")
    etf2 = state.sequence("d", "0056")
    assert equity and etf1 and etf2 and etf1[-1] == etf2[-1] and equity[-1] != etf1[-1]
    assert state.acquire_attempt(
        "d", "2330", now, max_requests=50, rolling_seconds=60, max_attempts=3
    )[0]
    assert not state.acquire_attempt(
        "d", "2330", now, max_requests=50, rolling_seconds=60, max_attempts=3
    )[0]
    state.close()
    state = ShioajiStagingState(path)
    assert not state.acquire_attempt(
        "d", "2330", now + timedelta(seconds=1), max_requests=50, rolling_seconds=60, max_attempts=3
    )[0]
    assert (
        state.acquire_attempt(
            "d",
            "2330",
            now + timedelta(seconds=121),
            max_requests=50,
            rolling_seconds=60,
            max_attempts=3,
        )[1]
        == 2
    )
    state.close()


def test_rolling_limiter_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "limiter.sqlite"
    now = datetime(2026, 7, 29, tzinfo=timezone.utc)
    state = ShioajiStagingState(path)
    state.ensure("d", "2026-07-29", "u", "update", [dict(EXPECTED[0]), dict(EXPECTED[1])])
    assert state.acquire_attempt(
        "d", "2330", now, max_requests=1, rolling_seconds=60, max_attempts=3
    )[0]
    state.close()

    state = ShioajiStagingState(path)
    assert not state.acquire_attempt(
        "d",
        "0050",
        now + timedelta(seconds=59),
        max_requests=1,
        rolling_seconds=60,
        max_attempts=3,
    )[0]
    assert state.acquire_attempt(
        "d",
        "0050",
        now + timedelta(seconds=61),
        max_requests=1,
        rolling_seconds=60,
        max_attempts=3,
    )[0]
    state.close()


class _Gateway:
    def __init__(self) -> None:
        self.calls = 0

    def fetch_kbars(self, _symbol: str, _date: date) -> ShioajiKbarsSnapshot:
        self.calls += 1
        return ShioajiKbarsSnapshot(
            {
                "ts": (1785286860000000000,),
                "Open": (1,),
                "High": (1,),
                "Low": (1,),
                "Close": (1,),
                "Volume": (1,),
                "Amount": (1,),
            },
            None,
            None,
        )


def test_validate_only_does_not_construct_delivery_and_prepared_retries_do_not_refetch(
    tmp_path: Path,
) -> None:
    manifest = {
        "governance": {
            "max_requests": 50,
            "rolling_seconds": 60,
            "max_attempts_per_sequence": 3,
            "cutoff": "17:00",
            "timezone": "Asia/Taipei",
        },
        "universe_id": "shioaji_tw_staging_v1",
        "sequences": [dict(EXPECTED[0])],
    }
    state = ShioajiStagingState(tmp_path / "s.db")
    gateway = _Gateway()
    result = Coordinator(
        state, manifest, gateway, now=lambda: datetime(2026, 7, 29, 1, tzinfo=timezone.utc)
    ).run(date(2026, 7, 29))
    assert result[0].code == "VALIDATED" and gateway.calls == 1
    assert state.db.execute(
        "SELECT outcome,code FROM provider_attempts WHERE symbol='2330'"
    ).fetchone() == ("success", "ACQUIRED")
    # Snapshot makes a second validation retry provider-free.
    result = Coordinator(
        state, manifest, gateway, now=lambda: datetime(2026, 7, 29, 1, tzinfo=timezone.utc)
    ).run(date(2026, 7, 29))
    assert result[0].code == "VALIDATED" and gateway.calls == 1


def test_minute_digest_excludes_sequence_count() -> None:
    identity = {
        "data_date": "2026-07-29",
        "dataset_key": "tw_etf_minute",
        "sequence": 2,
        "snapshot_id": "snapshot",
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    kwargs = {
        "kbars": _Gateway().fetch_kbars("0056", date(2026, 7, 29)).kbars,
        "dataset_key": "tw_etf_minute",
        "target_date": date(2026, 7, 29),
        "symbols": ("0056",),
        "fetched_at": datetime(2026, 7, 29, tzinfo=timezone.utc),
        "usage_before_requests": 0,
        "usage_after_requests": 1,
        "snapshot_id": "snapshot",
        "daily_update_id": "daily",
        "universe_id": "universe",
        "sequence": 2,
    }
    first = build_market_minute_request(**kwargs, sequence_count=3)
    second = build_market_minute_request(**kwargs, sequence_count=4)
    assert first["request_key"] == second["request_key"] == f"mmr:{digest}"
    assert first["idempotency_key"] == second["idempotency_key"] == f"mms:{digest}"


def test_state_rejects_schema_drift_and_persists_raw_checkpoint(tmp_path: Path) -> None:
    path = tmp_path / "bad.sqlite"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO meta VALUES ('schema_version', '" + SCHEMA_VERSION + "');"
        "CREATE TABLE daily_updates (daily_id TEXT PRIMARY KEY);"
    )
    db.close()
    with pytest.raises(ShioajiStagingStateError, match="schema"):
        ShioajiStagingState(path)

    state = ShioajiStagingState(tmp_path / "good.sqlite")
    state.ensure("d", "2026-07-29", "u", "update", [dict(EXPECTED[0])])
    state.save_raw("d", "2330", "r2://bucket/raw/object", "a" * 64, 42)
    state.close()
    state = ShioajiStagingState(tmp_path / "good.sqlite")
    assert state.get_raw("d", "2330") == ("r2://bucket/raw/object", "a" * 64, 42)
    state.close()


def test_state_rejects_semantic_corruption_and_prepared_identity_drift(
    tmp_path: Path,
) -> None:
    path = tmp_path / "constraints.sqlite"
    state = ShioajiStagingState(path)
    state.ensure("d", "2026-07-29", "u", "update", [dict(EXPECTED[0])])
    with pytest.raises(sqlite3.IntegrityError):
        state.db.execute(
            "UPDATE snapshot_sequences SET attempts=-1 WHERE daily_id='d' AND symbol='2330'"
        )
    with pytest.raises(sqlite3.IntegrityError):
        state.db.execute(
            "UPDATE snapshot_sequences SET status='impossible' WHERE daily_id='d' AND symbol='2330'"
        )
    state.close()


class _FailingGateway:
    def __init__(self, code: str = "ACQUISITION") -> None:
        self.code = code
        self.calls = 0

    def fetch_kbars(self, _symbol: str, _date: date) -> ShioajiKbarsSnapshot:
        self.calls += 1
        raise ShioajiSdkError("provider detail must not escape", code=self.code)


def _single_manifest() -> dict[str, object]:
    return {
        "governance": {
            "max_requests": 50,
            "rolling_seconds": 60,
            "max_attempts_per_sequence": 3,
            "cutoff": "17:00",
            "timezone": "Asia/Taipei",
        },
        "universe_id": "shioaji_tw_staging_v1",
        "sequences": [dict(EXPECTED[0])],
    }


def test_terminal_stage_is_not_retried_and_retryable_stops_after_three(
    tmp_path: Path,
) -> None:
    def now() -> datetime:
        return datetime(2026, 7, 29, 1, tzinfo=timezone.utc)

    terminal = _FailingGateway("CONTRACT")
    state = ShioajiStagingState(tmp_path / "terminal.sqlite")
    coordinator = Coordinator(state, _single_manifest(), terminal, now=now)  # type: ignore[arg-type]
    assert coordinator.run(date(2026, 7, 29))[0].code == "CONTRACT_UNAVAILABLE"
    assert coordinator.run(date(2026, 7, 29))[0].code == "CONTRACT_UNAVAILABLE"
    assert terminal.calls == 1
    state.close()

    retryable = _FailingGateway()
    state = ShioajiStagingState(tmp_path / "retryable.sqlite")
    coordinator = Coordinator(state, _single_manifest(), retryable, now=now)  # type: ignore[arg-type]
    assert [coordinator.run(date(2026, 7, 29))[0].retryable for _ in range(3)] == [
        True,
        True,
        True,
    ]
    assert coordinator.run(date(2026, 7, 29))[0].code == "ATTEMPT_BLOCKED"
    assert retryable.calls == 3
    state.close()


def test_attempt_is_committed_before_provider_call_and_empty_payload_fails_closed(
    tmp_path: Path,
) -> None:
    state = ShioajiStagingState(tmp_path / "ledger.sqlite")
    daily_id = "shioaji_tw_staging_v1:2026-07-29"

    class LedgerGateway:
        def fetch_kbars(self, _symbol: str, _date: date) -> ShioajiKbarsSnapshot:
            assert state.db.execute(
                "SELECT outcome FROM provider_attempts "
                "WHERE daily_id=? AND symbol='2330' AND attempt_no=1",
                (daily_id,),
            ).fetchone() == ("started",)
            return ShioajiKbarsSnapshot({}, None, None)

    result = Coordinator(
        state,
        _single_manifest(),  # type: ignore[arg-type]
        LedgerGateway(),
        now=lambda: datetime(2026, 7, 29, 1, tzinfo=timezone.utc),
    ).run(date(2026, 7, 29))
    assert result[0].code == "KBARS_PAYLOAD"
    assert state.db.execute(
        "SELECT terminal_status FROM snapshot_sequences WHERE daily_id=? AND symbol='2330'",
        (daily_id,),
    ).fetchone() == ("KBARS_PAYLOAD",)
    state.close()


def test_non_finite_sdk_scalar_is_terminal_payload(tmp_path: Path) -> None:
    state = ShioajiStagingState(tmp_path / "nan.sqlite")

    class NanGateway:
        calls = 0

        def fetch_kbars(self, _symbol: str, _date: date) -> ShioajiKbarsSnapshot:
            self.calls += 1
            return ShioajiKbarsSnapshot(
                {
                    "ts": (1785286860000000000,),
                    "Open": (float("nan"),),
                    "High": (1,),
                    "Low": (1,),
                    "Close": (1,),
                    "Volume": (1,),
                    "Amount": (1,),
                },
                None,
                None,
            )

    gateway = NanGateway()
    coordinator = Coordinator(
        state,
        _single_manifest(),  # type: ignore[arg-type]
        gateway,
        now=lambda: datetime(2026, 7, 29, 1, tzinfo=timezone.utc),
    )
    result = coordinator.run(date(2026, 7, 29))
    assert result[0].code == "KBARS_PAYLOAD" and not result[0].retryable
    assert coordinator.run(date(2026, 7, 29))[0].code == "KBARS_PAYLOAD"
    assert gateway.calls == 1
    state.close()


class _RawStore:
    def __init__(self) -> None:
        self.calls = 0

    def persist(self, raw_bytes: bytes, **_kwargs: object) -> RawObject:
        self.calls += 1
        return RawObject(
            f"r2://{'a' * 32}/findb-fetcher-raw/raw/object.json",
            hashlib.sha256(raw_bytes).hexdigest(),
            len(raw_bytes),
        )


class _CrashAfterRawUpload(BaseException):
    pass


class _CrashRawStore(_RawStore):
    def persist(self, raw_bytes: bytes, **_kwargs: object) -> RawObject:
        self.calls += 1
        raise _CrashAfterRawUpload


class _Source:
    def __init__(
        self,
        *,
        fail_prepare: bool = False,
        fail_delivery: bool = False,
        inactive: bool = False,
    ) -> None:
        self.fail_prepare = fail_prepare
        self.fail_delivery = fail_delivery
        self.inactive = inactive
        self.prepared: list[bytes] = []
        self.delivered: list[bytes] = []

    def prepare(self, request: dict[str, object]) -> PreparedDelivery:
        if self.fail_prepare:
            raise RuntimeError("prepare failed")
        body = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        self.prepared.append(body)
        return PreparedDelivery(
            body,
            str(request["idempotency_key"]),
            str(request["dataset_key"]),
            str(request["schema_id"]),
            int(request["schema_version"]),
        )

    def deliver(self, prepared: PreparedDelivery) -> object:
        self.delivered.append(prepared.body)
        if self.inactive:
            raise SourceAPIResponseError(409, "DATASET_INACTIVE")
        if self.fail_delivery:
            raise RuntimeError("transport failed")
        return SimpleNamespace(attempt_id=uuid4(), run_id=uuid4())

    def get_run_status(self, *_args: object, **_kwargs: object) -> object:
        return SimpleNamespace(status="completed", failure_code=None)


def test_raw_first_restart_reuses_raw_and_exact_prepared_bytes(tmp_path: Path) -> None:
    state_path = tmp_path / "delivery.sqlite"
    raw = _RawStore()
    gateway = _Gateway()

    def clock() -> datetime:
        return datetime(2026, 7, 29, 1, tzinfo=timezone.utc)

    state = ShioajiStagingState(state_path)
    failed = Coordinator(
        state,
        _single_manifest(),  # type: ignore[arg-type]
        gateway,
        raw_store=raw,
        source=_Source(fail_prepare=True),
        now=clock,
    ).run(date(2026, 7, 29), True)
    assert failed[0].code == "DELIVERY_FAILED"
    state.close()

    state = ShioajiStagingState(state_path)
    first_source = _Source(fail_delivery=True)
    failed = Coordinator(
        state,
        _single_manifest(),  # type: ignore[arg-type]
        gateway,
        raw_store=raw,
        source=first_source,
        now=clock,
    ).run(date(2026, 7, 29), True)
    assert failed[0].code == "DELIVERY_FAILED"
    state.close()

    state = ShioajiStagingState(state_path)
    source = _Source()
    completed = Coordinator(
        state,
        _single_manifest(),  # type: ignore[arg-type]
        gateway,
        raw_store=raw,
        source=source,
        now=clock,
    ).run(date(2026, 7, 29), True)
    assert completed[0].code == "completed"
    assert raw.calls == 1 and gateway.calls == 1
    prepared = state.get_prepared("shioaji_tw_staging_v1:2026-07-29", "2330")
    assert source.prepared == []
    assert prepared == first_source.prepared[0] == first_source.delivered[0] == source.delivered[0]
    prepared_value = json.loads(prepared)
    batch = prepared_value["payload"]["batch"]
    canonical_digest = hashlib.sha256(
        json.dumps(
            {
                "data_date": "2026-07-29",
                "dataset_key": "tw_equity_minute",
                "sequence": 1,
                "snapshot_id": batch["snapshot_id"],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert prepared_value["request_key"] == f"mmr:{canonical_digest}"
    assert prepared_value["idempotency_key"] == f"mms:{canonical_digest}"
    state.close()

    assert prepared is not None
    corrupted = json.loads(prepared)
    corrupted["payload"]["data"][0]["volume"] = 2001
    corrupted_body = json.dumps(corrupted, sort_keys=True, separators=(",", ":")).encode()
    db = sqlite3.connect(state_path)
    db.execute(
        "UPDATE snapshot_sequences SET prepared=?,prepared_sha256=? WHERE symbol='2330'",
        (corrupted_body, hashlib.sha256(corrupted_body).hexdigest()),
    )
    db.commit()
    db.close()
    with pytest.raises(ShioajiStagingStateError, match="content"):
        ShioajiStagingState(state_path)


def test_raw_upload_crash_window_fails_closed_without_reupload(tmp_path: Path) -> None:
    state_path = tmp_path / "raw-crash.sqlite"
    raw = _CrashRawStore()
    gateway = _Gateway()

    def clock() -> datetime:
        return datetime(2026, 7, 29, 1, tzinfo=timezone.utc)

    state = ShioajiStagingState(state_path)
    with pytest.raises(_CrashAfterRawUpload):
        Coordinator(
            state,
            _single_manifest(),  # type: ignore[arg-type]
            gateway,
            raw_store=raw,
            source=_Source(),
            now=clock,
        ).run(date(2026, 7, 29), True)
    state.close()

    state = ShioajiStagingState(state_path)
    result = Coordinator(
        state,
        _single_manifest(),  # type: ignore[arg-type]
        gateway,
        raw_store=raw,
        source=_Source(),
        now=clock,
    ).run(date(2026, 7, 29), True)
    assert result[0].code == "RAW_PERSIST_UNCERTAIN"
    assert raw.calls == 1 and gateway.calls == 1
    state.close()


def test_dataset_inactive_halts_remaining_sequences(tmp_path: Path) -> None:
    state = ShioajiStagingState(tmp_path / "inactive.sqlite")
    gateway = _Gateway()
    raw = _RawStore()
    source = _Source(inactive=True)
    result = Coordinator(
        state,
        {
            **_single_manifest(),
            "sequences": [dict(EXPECTED[0]), dict(EXPECTED[1])],
        },
        gateway,
        raw_store=raw,
        source=source,
        now=lambda: datetime(2026, 7, 29, 1, tzinfo=timezone.utc),
    ).run(date(2026, 7, 29), True)
    assert [item.code for item in result] == ["DATASET_INACTIVE"]
    assert gateway.calls == raw.calls == 1
    state.close()

    state = ShioajiStagingState(tmp_path / "inactive.sqlite")
    result = Coordinator(
        state,
        {
            **_single_manifest(),
            "sequences": [dict(EXPECTED[0]), dict(EXPECTED[1])],
        },
        gateway,
        raw_store=raw,
        source=source,
        now=lambda: datetime(2026, 7, 29, 1, tzinfo=timezone.utc),
    ).run(date(2026, 7, 29), True)
    assert [item.code for item in result] == ["DATASET_INACTIVE"]
    assert len(source.delivered) == 1
    state.close()


def test_contract_registry_is_enforced_and_cutoff_is_rechecked_between_calls(
    tmp_path: Path,
) -> None:
    class InvalidContracts:
        def validate(self, *_args: object) -> None:
            raise ContractValidationError("drift")

    state = ShioajiStagingState(tmp_path / "contract.sqlite")
    result = Coordinator(
        state,
        _single_manifest(),  # type: ignore[arg-type]
        _Gateway(),
        contracts=InvalidContracts(),  # type: ignore[arg-type]
        now=lambda: datetime(2026, 7, 29, 1, tzinfo=timezone.utc),
    ).run(date(2026, 7, 29))
    assert result[0].code == "CONTRACT_INVALID"
    state.close()

    times = iter(
        (
            datetime(2026, 7, 29, 16, 59, 57, tzinfo=timezone(timedelta(hours=8))),
            datetime(2026, 7, 29, 16, 59, 58, tzinfo=timezone(timedelta(hours=8))),
            datetime(2026, 7, 29, 16, 59, 59, tzinfo=timezone(timedelta(hours=8))),
            datetime(2026, 7, 29, 17, 0, tzinfo=timezone(timedelta(hours=8))),
            datetime(2026, 7, 29, 17, 0, 1, tzinfo=timezone(timedelta(hours=8))),
        )
    )
    state = ShioajiStagingState(tmp_path / "cross-cutoff.sqlite")
    gateway = _Gateway()
    result = Coordinator(
        state,
        {
            **_single_manifest(),
            "sequences": [dict(EXPECTED[0]), dict(EXPECTED[1])],
        },
        gateway,
        now=lambda: next(times),
    ).run(date(2026, 7, 29))
    assert [item.code for item in result] == ["VALIDATED", "CUTOFF_REACHED"]
    assert gateway.calls == 1
    state.close()

    race_clock = {
        "now": datetime(
            2026,
            7,
            29,
            16,
            59,
            59,
            tzinfo=timezone(timedelta(hours=8)),
        )
    }

    class RaceState(ShioajiStagingState):
        def acquire_attempt(self, *args: object, **kwargs: object) -> tuple[bool, int]:
            result = super().acquire_attempt(*args, **kwargs)  # type: ignore[arg-type]
            race_clock["now"] = datetime(
                2026,
                7,
                29,
                17,
                0,
                tzinfo=timezone(timedelta(hours=8)),
            )
            return result

    state = RaceState(tmp_path / "cutoff-race.sqlite")
    gateway = _Gateway()
    result = Coordinator(
        state,
        _single_manifest(),  # type: ignore[arg-type]
        gateway,
        now=lambda: race_clock["now"],
    ).run(date(2026, 7, 29))
    assert result[0].code == "CUTOFF_REACHED" and gateway.calls == 0
    state.close()


def test_source_polling_has_interval_call_cap_and_safe_deadline_result(
    tmp_path: Path,
) -> None:
    class Clock:
        value = 0.0

        def monotonic(self) -> float:
            return self.value

        def sleep(self, delay: float) -> None:
            assert 0 < delay <= 1
            self.value += delay

    class PendingSource:
        calls = 0

        def get_run_status(self, *_args: object, **_kwargs: object) -> object:
            self.calls += 1
            return SimpleNamespace(status="processing", failure_code=None)

    clock = Clock()
    source = PendingSource()
    state = ShioajiStagingState(tmp_path / "poll.sqlite")
    coordinator = Coordinator(
        state,
        _single_manifest(),  # type: ignore[arg-type]
        None,
        source=source,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    prepared = PreparedDelivery(b"{}", "id", "dataset", "market_minute", 1)
    assert coordinator._wait_terminal(uuid4(), prepared) == "SOURCE_TERMINAL_TIMEOUT"
    assert source.calls == 15 and clock.value == 15

    class DeadlineSource:
        def get_run_status(self, *_args: object, **_kwargs: object) -> object:
            raise SourceAPIDeadlineExceeded

    coordinator.source = DeadlineSource()
    assert coordinator._wait_terminal(uuid4(), prepared) == "SOURCE_TERMINAL_TIMEOUT"
    state.close()

    class PendingDeliverySource(_Source):
        def get_run_status(self, *_args: object, **_kwargs: object) -> object:
            return SimpleNamespace(status="processing", failure_code=None)

    clock = Clock()
    state = ShioajiStagingState(tmp_path / "poll-result.sqlite")
    result = Coordinator(
        state,
        _single_manifest(),  # type: ignore[arg-type]
        _Gateway(),
        raw_store=_RawStore(),
        source=PendingDeliverySource(),
        now=lambda: datetime(2026, 7, 29, 1, tzinfo=timezone.utc),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    ).run(date(2026, 7, 29), True)
    assert result[0].code == "SOURCE_TERMINAL_TIMEOUT" and result[0].retryable
    state.close()


def test_cutoff_and_cli_fail_closed_with_valid_secret_free_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = ShioajiStagingState(tmp_path / "cutoff.sqlite")
    gateway = _Gateway()
    result = Coordinator(
        state,
        _single_manifest(),  # type: ignore[arg-type]
        gateway,
        now=lambda: datetime(2026, 7, 29, 9, tzinfo=timezone.utc),
    ).run(date(2026, 7, 29))
    assert result[0].code == "CUTOFF_REACHED" and gateway.calls == 0
    assert state.db.execute("SELECT count(*) FROM daily_updates").fetchone()[0] == 0
    state.close()

    monkeypatch.setenv("SHIOAJI_SECRET_KEY", "must-never-escape")
    assert main(["--unknown"]) == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["code"] == "SAFE_FAILURE"
    assert output.err == "" and "must-never-escape" not in output.out

    offline_state = tmp_path / "must-not-exist.sqlite"
    manifest = Path(__file__).resolve().parents[1] / "configs/shioaji_tw_staging.v1.json"
    assert main(["--check", "--manifest", str(manifest), "--state-path", str(offline_state)]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {"code": "CHECK_OK", "stage": "manifest", "count": 4}
    assert output.err == "" and not offline_state.exists()


def test_cli_returns_nonzero_for_non_successful_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailedCoordinator:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def run(self, *_args: object, **_kwargs: object) -> list[Result]:
            return [Result("LOGIN_FAILED", "login")]

    monkeypatch.setattr(
        "findb_fetcher.shioaji_staging_cli.Coordinator",
        FailedCoordinator,
    )
    state_path = tmp_path / "cli.sqlite"
    manifest = Path(__file__).resolve().parents[1] / "configs/shioaji_tw_staging.v1.json"
    assert (
        main(
            [
                "--manifest",
                str(manifest),
                "--state-path",
                str(state_path),
                "--target-date",
                "2026-07-29",
            ]
        )
        == 1
    )
    output = capsys.readouterr()
    assert json.loads(output.out)["code"] == "RUN_FAILED"
    assert output.err == ""
