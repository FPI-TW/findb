"""Persistent provider-account permits shared by every opt-in consumer.

Checkpoint databases stay isolated. All consumer processes mount the same
account governor database; restarts never reset the daily reservation.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from findb_fetcher.config import app_environment
from findb_fetcher.full_market_state import FullMarketState, FullMarketStateError, QuotaBlockedError
from findb_fetcher.full_market_universe import canonical_bytes, checksum


def enabled() -> bool:
    return os.getenv("FULL_MARKET_ENABLED", "false").lower() == "true"


def consumer() -> str:
    return os.getenv("FETCHER_CONSUMER_PROFILE", "pilot")


@dataclass
class Permit:
    state: FullMarketState
    account: str
    window: str
    bound: int
    allocation: str | None = None
    allocation_sha256: str = ""
    platform_limits: bool = False
    byte_limit: int | None = None
    requests_before: int = 0
    requests_after: int = 0

    def observe(self, byte_count: int, *, declared: int = 0) -> None:
        if self.platform_limits:
            if self.byte_limit is not None:
                self.state.record_byte_overage(
                    account=self.account, window=self.window, byte_count=byte_count
                )
            if max(byte_count, declared) > self.bound:
                raise FullMarketStateError("provider response exceeds transport bound")
            return
        if max(byte_count, declared) > self.bound:
            self.state.observe_capacity(
                account=self.account,
                window=self.window,
                bound=self.bound,
                observed=byte_count,
                allocation_sha256=self.allocation_sha256,
                allocation=self.allocation,
                declared=declared,
            )
            raise QuotaBlockedError("observed provider response exceeds enrolled byte bound")


def load_declaration(provider: str) -> dict[str, Any]:
    from findb_fetcher.full_market_runtime import load_readiness

    path = Path(
        os.getenv(
            "FETCHER_ACCOUNT_READINESS_FILE", f"/var/lib/findb-account/readiness/{provider}.json"
        )
    )
    if consumer() == "pilot" or consumer() == "historical":
        allocation = Path(
            os.getenv("FETCHER_ACCOUNT_ALLOCATION_FILE", str(path.with_suffix(".account.json")))
        )
        if not allocation.is_file():
            # A bounded install has no opt-in shared account until installation.
            raise FileNotFoundError("shared account allocation not installed")
        value = json.loads(allocation.read_bytes())
    else:
        if not path.is_file() or path.stat().st_size > 65536:
            raise QuotaBlockedError("shared account readiness unavailable")
        scope = json.loads(path.read_bytes()).get("datasets", [])
        value = load_readiness(path, provider, scope, now=datetime.now(timezone.utc))
        allocation_path = Path(
            os.getenv("FETCHER_ACCOUNT_ALLOCATION_FILE", str(path.with_suffix(".account.json")))
        )
        allocation = json.loads(allocation_path.read_bytes())
        allocation["requests_per_second"] = float(allocation["requests_per_second"])
        allocation.setdefault("source_requests_per_minute", 120)
        if checksum(canonical_bytes(allocation)) != value.get("account_allocation_sha256"):
            raise QuotaBlockedError("installed account allocation changed after enrollment")
    if (
        value.get("provider") != provider
        or value.get("environment") != app_environment("local")
        or value.get("account", {}).get("governor_identity")
        != os.getenv("FETCHER_ACCOUNT_STATE_PATH", "/var/lib/findb-account/governor.sqlite3")
        or consumer() not in value["account"].get("consumers", [])
        or any(
            type(value.get(key)) is not int or value[key] <= 0
            for key in (
                "requests_per_day",
                "requests_per_minute",
                "bytes_per_day",
                "max_response_bytes",
            )
        )
        or not isinstance(value.get("requests_per_second"), (int, float))
        or value["requests_per_second"] <= 0
    ):
        raise QuotaBlockedError("shared account allocation identity invalid")
    return value


def runtime_identity_matches(runtime_id: str) -> bool:
    if app_environment("local") == "local":
        import hashlib

        from findb_fetcher import full_market_cli
        from findb_fetcher.installation_receipt import installed_artifact_digest

        config = Path(os.environ["FETCHER_FULL_MARKET_CONFIG"])
        governor = os.environ["FETCHER_ACCOUNT_STATE_PATH"]
        expected = (
            "local:"
            + hashlib.sha256(
                installed_artifact_digest(Path(full_market_cli.__file__).parent).encode()
                + config.read_bytes()
                + governor.encode()
            ).hexdigest()
        )
        return runtime_id == expected
    hostname = os.environ.get("HOSTNAME", "")
    return len(hostname) >= 12 and runtime_id.startswith(hostname)


def authorize(value: dict[str, Any]) -> dict[str, Any]:
    from findb_fetcher.config import FetcherConfig
    from findb_fetcher.full_market_client import FullMarketSourceClient

    if consumer() in {"pilot", "historical"}:
        return {"acquisition_allowed": True}  # Each bounded runtime retains its own control/auth.
    if not enabled():
        raise QuotaBlockedError("local full-market flag is disabled")
    with FullMarketSourceClient(FetcherConfig.from_env()) as source:
        result = source.request(
            "GET",
            "full-market/authorization?"
            + urlencode(
                {
                    "consumer": consumer(),
                }
            ),
        )
    if result.get("acquisition_allowed") is not True:
        raise QuotaBlockedError("Source account acquisition authorization denied")
    return result


def reserve(provider: str, *, force: bool = False) -> Permit | None:
    from findb_fetcher.platform_limits import account_identity, interval, limits

    if (
        consumer() in {"pilot", "historical"}
        and not force
        and not os.getenv("FETCHER_ACCOUNT_STATE_PATH")
    ):
        return None  # Existing bounded installations retain their provider enforcement.
    value = limits(provider)
    if consumer() not in {"pilot", "historical"}:
        authorize({})
    path = Path(os.getenv("FETCHER_ACCOUNT_STATE_PATH", "/var/lib/findb-account/governor.sqlite3"))
    state = FullMarketState(path)
    account = account_identity(provider, state)
    while True:
        if consumer() not in {"pilot", "historical"} and not enabled():
            raise QuotaBlockedError("local full-market flag is disabled")
        wait, before, after, window = state.reserve_acquisition(
            account=account,
            interval=interval(value),
            requests=1,
            byte_count=0,
            request_limit=None,
            byte_limit=value["bytes_per_day"],
            minute_limit=value["requests_per_minute"],
            enforce_capacity=False,
        )
        if wait <= 0:
            if consumer() not in {"pilot", "historical"}:
                authorize({})
            return Permit(
                state,
                account,
                window,
                value["max_response_bytes"],
                platform_limits=True,
                byte_limit=value["bytes_per_day"],
                requests_before=before,
                requests_after=after,
            )
        time.sleep(min(wait, 1))


def provider_permit(provider: str) -> Permit | None:
    # Acquisition is governed at the actual invocation boundary. Detached
    # reserve() remains a legacy ledger API, never a production provider gate.
    return None


def source_permit(source_key: str) -> None:
    """Pace every Source request, including cached-table delivery and stopped drain.

    Installed allocation is configuration, not Full acquisition authorization.
    Missing allocation keeps ordinary bounded installations unchanged.
    """
    # The authenticated Source API enforces its DB-backed client rate limit.
    # Its HTTP 429 / Retry-After handling remains in the delivery clients.
    # Obsolete account allocation budgets must not override that authority.
    return


def repair_capacity(
    *,
    provider: str,
    environment: str,
    governor: Path,
    readiness: Path,
    allocation: Path,
    enrollment: Path,
    violation_id: str,
) -> None:
    """Explicit trusted operator recovery after inspection and management reenrollment."""
    from findb_fetcher.config import FetcherConfig
    from findb_fetcher.full_market_client import FullMarketProtocolError, FullMarketSourceClient
    from findb_fetcher.full_market_runtime import load_readiness

    raw = json.loads(readiness.read_bytes())
    try:
        if (
            set(raw)
            != {
                "version",
                "provider",
                "status",
                "environment",
                "source_client_id",
                "runtime_id",
                "artifact_sha256",
                "config_sha256",
                "account_allocation_sha256",
                "datasets",
                "verified_at",
                "expires_at",
                "evidence_url",
                "evidence_sha256",
                "requests_per_day",
                "requests_per_minute",
                "bytes_per_day",
                "max_response_bytes",
                "requests_per_second",
                "provider_call_seconds",
                "source_requests_per_minute",
                "source_check_seconds",
                "completion_window_seconds",
                "account",
            }
            or type(raw["version"]) is not int
            or raw["version"] != 1
        ):
            raise ValueError
        for field in ("source_client_id",):
            if str(UUID(raw[field])) != raw[field]:
                raise ValueError
        for field in ("verified_at", "expires_at"):
            stamp = datetime.fromisoformat(raw[field])
            if (
                stamp.tzinfo is None
                or stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") != raw[field]
            ):
                raise ValueError
        for field in ("requests_per_second", "provider_call_seconds", "source_check_seconds"):
            if type(raw[field]) is not float or not math.isfinite(raw[field]) or raw[field] <= 0:
                raise ValueError
        for field in (
            "requests_per_day",
            "requests_per_minute",
            "bytes_per_day",
            "max_response_bytes",
            "source_requests_per_minute",
            "completion_window_seconds",
        ):
            if type(raw[field]) is not int or raw[field] <= 0:
                raise ValueError
        for field in ("artifact_sha256", "config_sha256", "account_allocation_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", raw[field]):
                raise ValueError
    except (KeyError, TypeError, ValueError):
        raise QuotaBlockedError("canonical management proof required for capacity repair") from None
    proof = load_readiness(readiness, provider, raw["datasets"], now=datetime.now(timezone.utc))
    installed = json.loads(allocation.read_bytes())
    installed["requests_per_second"] = float(installed["requests_per_second"])
    installed.setdefault("source_requests_per_minute", 120)
    expected = {
        key: proof[key]
        for key in (
            "provider",
            "environment",
            "requests_per_day",
            "requests_per_minute",
            "requests_per_second",
            "bytes_per_day",
            "max_response_bytes",
            "source_requests_per_minute",
            "account",
        )
    }
    digest = checksum(canonical_bytes(installed))
    declaration_digest = checksum(canonical_bytes(proof))
    envelope = json.loads(enrollment.read_bytes())
    try:
        if set(envelope) != {"enrollment_id", "declaration_sha256", "runtime_id", "datasets"}:
            raise ValueError
        if str(UUID(envelope["enrollment_id"])) != envelope["enrollment_id"]:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise QuotaBlockedError("canonical management enrollment UUID required") from None
    if (
        installed != expected
        or proof["environment"] != environment
        or proof["account"]["governor_identity"] != str(governor)
        or proof["account_allocation_sha256"] != digest
        or envelope["declaration_sha256"] != declaration_digest
        or envelope["runtime_id"] != proof["runtime_id"]
        or envelope["datasets"] != proof["datasets"]
    ):
        raise QuotaBlockedError("corrected management enrollment and installed allocation differ")
    # Run in the installed Full worker's environment. Arbitrary supplied files
    # cannot stand in for its actual configured files and installed runtime.
    try:
        config = Path(os.environ["FETCHER_FULL_MARKET_CONFIG"])
        installed_readiness = Path(
            os.getenv(
                "FETCHER_ACCOUNT_READINESS_FILE",
                f"/var/lib/findb-account/readiness/{provider}.json",
            )
        )
        installed_allocation = Path(
            os.getenv(
                "FETCHER_ACCOUNT_ALLOCATION_FILE",
                str(installed_readiness.with_suffix(".account.json")),
            )
        )
        if (
            consumer() != "full_market"
            or app_environment() != environment
            or str(governor) != os.environ["FETCHER_ACCOUNT_STATE_PATH"]
            or readiness.resolve() != installed_readiness.resolve()
            or allocation.resolve() != installed_allocation.resolve()
            or checksum(config.read_bytes()) != proof["config_sha256"]
            or not runtime_identity_matches(proof["runtime_id"])
        ):
            raise ValueError
        if environment == "local":
            from findb_fetcher import full_market_cli
            from findb_fetcher.installation_receipt import installed_artifact_digest

            if (
                installed_artifact_digest(Path(full_market_cli.__file__).parent)
                != proof["artifact_sha256"]
            ):
                raise ValueError
    except (KeyError, OSError, ValueError):
        raise QuotaBlockedError("corrected allocation/runtime is not installed") from None
    try:
        source_config = FetcherConfig.from_env()
        source_config = replace(
            source_config,
            request_timeout_seconds=min(source_config.request_timeout_seconds, 30),
            max_attempts=min(source_config.max_attempts, 3),
            max_retry_after_seconds=min(source_config.max_retry_after_seconds, 30),
        )
        with FullMarketSourceClient(source_config) as source:
            authority = source.request(
                "GET",
                "full-market/enrollment-verification?"
                + urlencode(
                    {
                        "enrollment_id": envelope["enrollment_id"],
                        "source_client_id": proof["source_client_id"],
                        "declaration_sha256": declaration_digest,
                        "runtime_id": proof["runtime_id"],
                    }
                ),
            )
        reported = datetime.fromisoformat(authority["reported_at"])
        if (
            authority["enrollment_id"] != envelope["enrollment_id"]
            or authority["source_client_id"] != proof["source_client_id"]
            or authority["declaration_sha256"] != declaration_digest
            or canonical_bytes(authority["declaration"]) != canonical_bytes(proof)
            or not re.fullmatch(r"[0-9a-f]{64}", authority["installation_sha256"])
            or reported.tzinfo is None
            or not 0 <= (datetime.now(timezone.utc) - reported).total_seconds() <= 90
        ):
            raise ValueError
    except (FullMarketProtocolError, KeyError, TypeError, ValueError):
        raise QuotaBlockedError("current Source installed enrollment verification failed") from None
    FullMarketState(governor).repair_capacity(
        account=proof["account"]["account_id"],
        violation_id=violation_id,
        corrected_bound=proof["max_response_bytes"],
        allocation_sha256=digest,
        declaration_sha256=declaration_digest,
    )


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Trusted account capacity correction; never resets usage"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect-acquisition")
    inspect.add_argument("--governor", type=Path, required=True)
    inspect.add_argument(
        "--provider", choices=("twelve_data", "finlab", "shioaji", "taifex"), required=True
    )
    repair = commands.add_parser("repair-capacity")
    repair.add_argument(
        "--provider", required=True, choices=("twelve_data", "finlab", "shioaji", "taifex")
    )
    repair.add_argument("--environment", required=True, choices=("local", "staging", "production"))
    for name in ("governor", "readiness", "allocation", "enrollment"):
        repair.add_argument("--" + name, type=Path, required=True)
    repair.add_argument("--violation-id", required=True)
    args = parser.parse_args()
    if args.command == "inspect-acquisition":
        import sqlite3

        now = time.time()
        with sqlite3.connect(args.governor.resolve().as_uri() + "?mode=ro", uri=True) as db:
            active = db.execute(
                "SELECT COUNT(*) FROM acquisition_intent WHERE provider=? AND (status='armed' OR (status='unknown' AND (completed_at>=? OR completed_at+cooldown_bound>=? OR (login_units=1 AND (actual_start>? OR (actual_start IS NULL AND armed_at<=? AND completed_at>?))))))",
                (args.provider, now - now % 86400, now, now - 86400, now, now - 86400),
            ).fetchone()[0]
        print(
            json.dumps(
                {
                    "acquisition_protocol_version": 2,
                    "legacy_rollback_safe": active == 0,
                    "current_unknown_or_armed": active,
                }
            )
        )
        return
    repair_capacity(**{key: value for key, value in vars(args).items() if key != "command"})
    print("account_capacity_repair=accepted usage_preserved=true")


if __name__ == "__main__":
    main()
