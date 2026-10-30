"""Durable, bounded daily observations; independent of failure/account budgets."""

import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date

from findb_fetcher.full_market_state import QuotaBlockedError
from findb_fetcher.provider_work import (
    ProviderWorkAcquisitionStopped,
    ProviderWorkSourceUnavailableError,
    active,
)
from findb_fetcher.providers.shioaji import (
    SHIOAJI_SDK_VERSION,
    ShioajiEmptyResponseError,
    ShioajiSdkError,
    empty_kbars,
    kbars_target_valid,
)

RETRY_SECONDS = 900
PROBE_LIMIT = 3
_INVALID_PAYLOAD_REASONS = frozenset(
    {"kbars_shape", "kbars_scalar", "ipc_schema", "ipc_encode", "ipc_size", "snapshot"}
)
_probe: ContextVar[tuple | None] = ContextVar("shioaji_probe", default=None)


class ShioajiObservationWaitError(ShioajiEmptyResponseError):
    def __init__(self, *, delay: float = 0, exhausted: bool = False, observed_empty: bool = True):
        super().__init__("empty Kbars observation requires evidence")
        self.delay, self.exhausted = max(0, delay), exhausted
        self.observed_empty = observed_empty


def operation_key(symbol: str, day: date) -> str:
    return json.dumps([symbol, day.isoformat()], separators=(",", ":"))


def install_grant(journal, repair: dict, day: date) -> None:
    with journal.connection() as db:
        db.execute(
            "INSERT OR IGNORE INTO work_shioaji_grant VALUES(?,?,?,?)",
            (journal.key, operation_key(repair["provider_symbol"], day), "initial", PROBE_LIMIT),
        )
        db.execute(
            "INSERT OR IGNORE INTO work_shioaji_grant VALUES(?,?,?,?)",
            (
                journal.key,
                operation_key(repair["provider_symbol"], day),
                repair["repair_id"],
                repair["observation_limit"],
            ),
        )


def observation_status(journal, symbol: str, day: date) -> tuple[int, int]:
    key = operation_key(symbol, day)
    with journal.connection() as db:
        grant = db.execute(
            "SELECT grant_id,maximum FROM work_shioaji_grant WHERE work_key=? AND operation_key=? ORDER BY rowid DESC LIMIT 1",
            (journal.key, key),
        ).fetchone()
        if not grant:
            return 0, PROBE_LIMIT
        count = db.execute(
            "SELECT COUNT(*) FROM work_shioaji_probe WHERE work_key=? AND operation_key=? AND grant_id=? AND outcome IN ('empty','unknown')",
            (journal.key, key, grant[0]),
        ).fetchone()[0]
        return count, grant[1]


@contextmanager
def observation_guard(arguments: dict, rejected):
    owner = active()
    if owner is None or owner.work.get("kind") != "daily":
        yield
        return
    owner._check_local_fence()
    journal = owner.journal
    key = operation_key(arguments["symbol"], arguments["target_date"])
    now = time.time()
    with journal.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT OR IGNORE INTO work_shioaji_grant VALUES(?,?,?,?)",
            (journal.key, key, "initial", PROBE_LIMIT),
        )
        generation, maximum = db.execute(
            "SELECT grant_id,maximum FROM work_shioaji_grant WHERE work_key=? AND operation_key=? ORDER BY rowid DESC LIMIT 1",
            (journal.key, key),
        ).fetchone()
        if (
            empty_kbars(rejected)
            and not db.execute(
                "SELECT 1 FROM work_shioaji_probe WHERE work_key=? AND operation_key=? LIMIT 1",
                (journal.key, key),
            ).fetchone()
        ):
            db.execute(
                "INSERT INTO work_shioaji_probe(work_key,operation_key,grant_id,reserved_at,outcome,diagnostic) VALUES(?,?,?,?,?,?)",
                (
                    journal.key,
                    key,
                    "initial",
                    now,
                    "empty",
                    '{"origin":"legacy_cache","cause":"unknown"}',
                ),
            )
        rows = db.execute(
            "SELECT reserved_at,outcome FROM work_shioaji_probe WHERE work_key=? AND operation_key=? AND grant_id=? AND outcome IN ('unknown','empty')",
            (journal.key, key, generation),
        ).fetchall()
        observed_empty = (
            db.execute(
                "SELECT 1 FROM work_shioaji_probe WHERE work_key=? AND operation_key=? AND outcome='empty' LIMIT 1",
                (journal.key, key),
            ).fetchone()
            is not None
        )
        last = db.execute(
            "SELECT MAX(reserved_at) FROM work_shioaji_probe WHERE work_key=? AND operation_key=? AND outcome IN ('unknown','empty')",
            (journal.key, key),
        ).fetchone()[0]
    # Commit imported legacy evidence even when this invocation must wait.
    # Legacy caches lack observation timestamps, so start a conservative wait
    # at discovery rather than assuming their last dispatch was long ago.
    if len(rows) >= maximum:
        raise ShioajiObservationWaitError(exhausted=True, observed_empty=observed_empty)
    if last and now < last + RETRY_SECONDS:
        raise ShioajiObservationWaitError(
            delay=last + RETRY_SECONDS - now, observed_empty=observed_empty
        )
    # Remote authorization precedes reservation. The native operation retains
    # its own final account/owner dispatch checks and charging.
    if rows or generation != "initial":
        from findb_fetcher.provider_wait import CalendarDecisionRepairRequiredError
        from findb_fetcher.provider_work import ProviderWorkRecoveryBlockedError

        try:
            calendar = owner.request("calendar-status")
        except Exception as exc:
            from findb_fetcher.source_recovery import transient_source_error

            error = (
                ProviderWorkSourceUnavailableError
                if transient_source_error(exc)
                else ProviderWorkRecoveryBlockedError
            )
            raise error("calendar authorization is unavailable") from exc
        if (
            calendar.get("work_id") != owner.work["work_id"]
            or calendar.get("trade_date") != owner.work.get("trade_date")
            or type(calendar.get("repair_required")) is not bool
        ):
            raise ProviderWorkRecoveryBlockedError("calendar evidence differs from original work")
        if calendar["repair_required"]:
            raise CalendarDecisionRepairRequiredError("published calendar is now closed")
    # The member loop authorizes immediately before acquisition. Repeat the RPC
    # only when it is no longer fresh, e.g. after the calendar check; the native
    # dispatch boundary still performs its own remote and cooldown checks.
    if not owner.authorization_fresh():
        owner.authorize()
    with journal.connection() as db:
        cursor = db.execute(
            "INSERT INTO work_shioaji_probe(work_key,operation_key,grant_id,reserved_at,outcome,diagnostic) VALUES(?,?,?,?,?,?)",
            (journal.key, key, generation, now, "unknown", "{}"),
        )
        probe_id = cursor.lastrowid
    token = _probe.set((journal, probe_id, arguments))
    dispatched = owner.dispatch_observations
    try:
        yield
    except BaseException as exc:
        # Never refund an uncertain or observed dispatch; account charging is
        # independent and is never changed here.
        proven_wait = isinstance(
            exc, (ProviderWorkAcquisitionStopped, ProviderWorkSourceUnavailableError)
        ) or (isinstance(exc, QuotaBlockedError) and exc.quota_wait and not exc.dispatched)
        if (
            isinstance(exc, ShioajiSdkError)
            and exc.code == "PAYLOAD"
            and exc.reason in _INVALID_PAYLOAD_REASONS
        ):
            # Decoder failures can precede observe_snapshot. Only reviewed
            # format evidence clears uncertainty; transport/child failures
            # retain it. Native/account charging is never refunded here.
            diagnostic = {
                "sdk_version": SHIOAJI_SDK_VERSION,
                "symbol": arguments["symbol"],
                "target_date": arguments["target_date"].isoformat(),
                "origin": "native",
                "error_code": exc.code,
                "error_reason": exc.reason,
            }
            with journal.connection() as db:
                db.execute(
                    "UPDATE work_shioaji_probe SET outcome='invalid',diagnostic=?,reserved_at=? WHERE probe_id=? AND outcome='unknown'",
                    (json.dumps(diagnostic, sort_keys=True), time.time(), probe_id),
                )
        elif proven_wait and owner.dispatch_observations == dispatched:
            with journal.connection() as db:
                db.execute(
                    "UPDATE work_shioaji_probe SET outcome='predispatch' WHERE probe_id=? AND outcome='unknown'",
                    (probe_id,),
                )
        else:
            with journal.connection() as db:
                db.execute(
                    "UPDATE work_shioaji_probe SET reserved_at=? WHERE probe_id=? AND outcome='unknown'",
                    (time.time(), probe_id),
                )
        raise
    finally:
        _probe.reset(token)


def record_kbars_not_dispatched() -> None:
    """Record positive proof from the gateway's validation/bootstrap boundary."""
    current = _probe.get()
    if current is None:
        return
    journal, probe_id, _ = current
    with journal.connection() as db:
        db.execute(
            "UPDATE work_shioaji_probe SET outcome='predispatch' WHERE probe_id=? AND outcome='unknown'",
            (probe_id,),
        )


def observe_snapshot(snapshot) -> None:
    current = _probe.get()
    if current is None:
        return
    journal, probe_id, arguments = current
    diagnostic = {
        "sdk_version": SHIOAJI_SDK_VERSION,
        "symbol": arguments["symbol"],
        "target_date": arguments["target_date"].isoformat(),
        "cause": "unknown",
        "origin": "native",
        "vector_lengths": {key: len(value) for key, value in snapshot.kbars.items()},
        "requests_before": snapshot.requests_before,
        "requests_after": snapshot.requests_after,
        "usage_bytes_before": snapshot.usage_bytes_before,
        "usage_bytes_after": snapshot.usage_bytes_after,
    }
    outcome = (
        "empty"
        if empty_kbars(snapshot)
        else "valid"
        if kbars_target_valid(snapshot, arguments)
        else "invalid"
    )
    with journal.connection() as db:
        db.execute(
            "UPDATE work_shioaji_probe SET outcome=?,diagnostic=?,reserved_at=? WHERE probe_id=?",
            (outcome, json.dumps(diagnostic, sort_keys=True), time.time(), probe_id),
        )
