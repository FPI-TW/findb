"""One-shot, staging-only coordinator.  It is not a production scheduler."""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from findb_fetcher.client import PreparedDelivery, SourceAPIDeadlineExceeded
from findb_fetcher.contracts import ContractRegistry, ContractValidationError
from findb_fetcher.providers.shioaji import (
    ShioajiGateway,
    ShioajiPayloadError,
    ShioajiSdkError,
    build_market_minute_request,
)
from findb_fetcher.raw_storage import (
    RawObject,
    attach_raw_object,
    require_raw_provenance,
)
from findb_fetcher.shioaji_staging_state import ShioajiStagingState

TAIPEI = ZoneInfo("Asia/Taipei")
MAX_MANIFEST_BYTES = 16 * 1024
EXPECTED = (
    {
        "symbol": "2330",
        "dataset_key": "tw_equity_minute",
        "asset_class": "equity",
        "sequence": 1,
        "sequence_count": 1,
    },
    {
        "symbol": "0050",
        "dataset_key": "tw_etf_minute",
        "asset_class": "etf",
        "sequence": 1,
        "sequence_count": 3,
    },
    {
        "symbol": "0056",
        "dataset_key": "tw_etf_minute",
        "asset_class": "etf",
        "sequence": 2,
        "sequence_count": 3,
    },
    {
        "symbol": "006201",
        "dataset_key": "tw_etf_minute",
        "asset_class": "etf",
        "sequence": 3,
        "sequence_count": 3,
    },
)


class StagingManifestError(ValueError):
    pass


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise StagingManifestError("duplicate JSON key")
        out[k] = v
    return out


def load_manifest(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if len(raw) > MAX_MANIFEST_BYTES:
        raise StagingManifestError("manifest exceeds limit")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs)
    except Exception as exc:
        raise StagingManifestError("invalid manifest") from exc
    governance = {
        "max_requests": 50,
        "rolling_seconds": 60,
        "max_attempts_per_sequence": 3,
        "cutoff": "17:00",
        "timezone": "Asia/Taipei",
    }
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "universe_id", "governance", "sequences"}
        or value.get("version") != 1
        or value.get("universe_id") != "shioaji_tw_staging_v1"
        or value.get("governance") != governance
        or value.get("sequences") != list(EXPECTED)
    ):
        raise StagingManifestError("manifest is not the reviewed staging universe")
    return value


def snapshot_bytes(kbars: dict[str, tuple[object, ...]]) -> bytes:
    return json.dumps(
        {"kind": "shioaji_sdk_acquisition_snapshot.v1", "kbars": kbars},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode()


def decode_snapshot(raw: bytes) -> dict[str, tuple[object, ...]]:
    try:
        value = json.loads(raw)
    except Exception as exc:
        raise ShioajiPayloadError("acquisition snapshot invalid") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"kind", "kbars"}
        or value["kind"] != "shioaji_sdk_acquisition_snapshot.v1"
        or not isinstance(value["kbars"], dict)
    ):
        raise ShioajiPayloadError("acquisition snapshot invalid")
    if any(not isinstance(k, str) or not isinstance(v, list) for k, v in value["kbars"].items()):
        raise ShioajiPayloadError("acquisition snapshot invalid")
    return {k: tuple(v) for k, v in value["kbars"].items()}


@dataclass(frozen=True, slots=True)
class Result:
    code: str
    stage: str
    count: int = 0
    retryable: bool = False
    request_id: str | None = None
    run_id: str | None = None


class Coordinator:
    def __init__(
        self,
        state: ShioajiStagingState,
        manifest: dict[str, Any],
        gateway: ShioajiGateway | None,
        *,
        raw_store: Any = None,
        source: Any = None,
        contracts: ContractRegistry | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(TAIPEI),
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        (
            self.state,
            self.manifest,
            self.gateway,
            self.raw_store,
            self.source,
            self.contracts,
            self.now,
            self.monotonic,
            self.sleep,
        ) = (
            state,
            manifest,
            gateway,
            raw_store,
            source,
            contracts or ContractRegistry(_contracts_dir()),
            now,
            monotonic,
            sleep,
        )

    def run(self, target: date, deliver: bool = False) -> list[Result]:
        gov = self.manifest["governance"]
        if _past_cutoff(self.now(), str(gov["cutoff"])):
            return [Result("CUTOFF_REACHED", "acquisition")]
        daily_id = f"{self.manifest['universe_id']}:{target.isoformat()}"
        daily_update_id = _id(daily_id, "daily")
        rows = self.manifest["sequences"]
        self.state.ensure(
            daily_id, target.isoformat(), self.manifest["universe_id"], daily_update_id, rows
        )
        out = []
        for row in rows:
            symbol = str(row["symbol"])
            terminal = self.state.get_terminal(daily_id, symbol)
            if terminal is not None:
                out.append(Result(terminal, "state"))
                if terminal == "DATASET_INACTIVE":
                    break
                continue
            snap = self.state.get_snapshot(daily_id, symbol)
            if snap is None:
                now = self.now()
                if _past_cutoff(now, str(gov["cutoff"])):
                    out.append(Result("CUTOFF_REACHED", "acquisition"))
                    break
                if self.gateway is None:
                    out.append(Result("GATEWAY_UNAVAILABLE", "login"))
                    continue
                ok, attempt = self.state.acquire_attempt(
                    daily_id,
                    symbol,
                    now,
                    max_requests=gov["max_requests"],
                    rolling_seconds=gov["rolling_seconds"],
                    max_attempts=gov["max_attempts_per_sequence"],
                )
                if not ok:
                    out.append(Result("ATTEMPT_BLOCKED", "acquisition"))
                    continue
                if _past_cutoff(self.now(), str(gov["cutoff"])):
                    self.state.finish_attempt(
                        daily_id,
                        symbol,
                        attempt,
                        "failed",
                        "CUTOFF_REACHED",
                        retryable=False,
                    )
                    out.append(Result("CUTOFF_REACHED", "acquisition"))
                    break
                try:
                    acquired = self.gateway.fetch_kbars(symbol, target)
                    snap = snapshot_bytes(dict(acquired.kbars))
                    decode_snapshot(snap)
                    self.state.complete_attempt_with_snapshot(
                        daily_id,
                        symbol,
                        attempt,
                        snap,
                    )
                except (ShioajiPayloadError, TypeError, ValueError):
                    self.state.finish_attempt(
                        daily_id, symbol, attempt, "failed", "KBARS_PAYLOAD", retryable=False
                    )
                    out.append(Result("KBARS_PAYLOAD", "payload"))
                    continue
                except ShioajiSdkError as exc:
                    terminal_codes = {
                        "CREDENTIALS": ("CREDENTIALS_UNAVAILABLE", "login"),
                        "LOGIN": ("LOGIN_FAILED", "login"),
                        "SDK": ("SDK_UNAVAILABLE", "sdk"),
                        "CONTRACT": ("CONTRACT_UNAVAILABLE", "contract"),
                        "PAYLOAD": ("KBARS_PAYLOAD", "payload"),
                    }
                    code, stage = terminal_codes.get(
                        exc.code,
                        ("ACQUISITION_FAILED", "acquisition"),
                    )
                    retryable = exc.code not in terminal_codes
                    self.state.finish_attempt(
                        daily_id,
                        symbol,
                        attempt,
                        "failed",
                        code,
                        retryable=retryable,
                    )
                    out.append(
                        Result(
                            code,
                            stage,
                            retryable=retryable,
                        )
                    )
                    continue
                except Exception:
                    # Gateway implementations must already have stripped provider text.
                    self.state.finish_attempt(
                        daily_id, symbol, attempt, "failed", "ACQUISITION_FAILED", retryable=True
                    )
                    out.append(Result("ACQUISITION_FAILED", "acquisition", retryable=True))
                    continue
            now = self.now()
            try:
                seq = self.state.sequence(daily_id, symbol)
                if seq is None:
                    raise ShioajiPayloadError("state sequence missing")
                request = build_market_minute_request(
                    decode_snapshot(snap),
                    dataset_key=str(row["dataset_key"]),
                    target_date=target,
                    symbols=(symbol,),
                    fetched_at=now,
                    usage_before_requests=0,
                    usage_after_requests=1,
                    snapshot_id=str(seq[-1]),
                    daily_update_id=daily_update_id,
                    universe_id=self.manifest["universe_id"],
                    sequence=int(row["sequence"]),
                    sequence_count=int(row["sequence_count"]),
                )
                self.contracts.validate("market_minute", 1, request)
            except ShioajiPayloadError:
                self.state.mark_terminal(daily_id, symbol, "KBARS_PAYLOAD")
                out.append(Result("KBARS_PAYLOAD", "payload"))
                continue
            except ContractValidationError:
                self.state.mark_terminal(daily_id, symbol, "CONTRACT_INVALID")
                out.append(Result("CONTRACT_INVALID", "contract"))
                continue
            if not deliver:
                out.append(Result("VALIDATED", "payload", len(request["payload"]["data"])))
                continue
            if self.raw_store is None or self.source is None:
                out.append(Result("DELIVERY_UNAVAILABLE", "raw"))
                continue
            prepared_bytes = self.state.get_prepared(daily_id, symbol)
            try:
                if prepared_bytes is None:
                    raw_state = self.state.get_raw(daily_id, symbol)
                    if raw_state is None:
                        intent = self.state.begin_raw(daily_id, symbol, snap)
                        if intent == "uncertain":
                            self.state.mark_terminal(
                                daily_id,
                                symbol,
                                "RAW_PERSIST_UNCERTAIN",
                            )
                            out.append(
                                Result(
                                    "RAW_PERSIST_UNCERTAIN",
                                    "raw",
                                )
                            )
                            continue
                        raw = self.raw_store.persist(
                            snap,
                            dataset_key=str(row["dataset_key"]),
                            source_symbol=symbol,
                            provider="shioaji",
                        )
                        self.state.save_raw(
                            daily_id,
                            symbol,
                            raw.ref,
                            raw.sha256,
                            raw.size_bytes,
                        )
                    else:
                        raw = RawObject(
                            ref=raw_state[0],
                            sha256=raw_state[1],
                            size_bytes=raw_state[2],
                        )
                    attach_raw_object(request, raw, preserve_identity=True)
                    require_raw_provenance(request)
                    prepared_bytes = self.source.prepare(request).body
                    self.state.save_prepared(daily_id, symbol, prepared_bytes)
                prepared = _prepared(prepared_bytes)
                receipt = self.source.deliver(prepared)
                terminal = self._wait_terminal(receipt.run_id, prepared)
                self.state.save_source(
                    daily_id,
                    symbol,
                    str(receipt.attempt_id),
                    str(receipt.run_id),
                    None if terminal == "SOURCE_TERMINAL_TIMEOUT" else terminal,
                )
                out.append(
                    Result(
                        "DELIVERED" if terminal is None else terminal,
                        "source",
                        len(request["payload"]["data"]),
                        retryable=terminal == "SOURCE_TERMINAL_TIMEOUT",
                        request_id=str(receipt.attempt_id),
                        run_id=str(receipt.run_id),
                    )
                )
                if terminal == "DATASET_INACTIVE":
                    break
            except SourceAPIDeadlineExceeded:
                out.append(
                    Result(
                        "SOURCE_TERMINAL_TIMEOUT",
                        "source",
                        retryable=True,
                    )
                )
            except Exception as exc:
                code = (
                    "DATASET_INACTIVE"
                    if getattr(exc, "code", None) == "DATASET_INACTIVE"
                    else "DELIVERY_FAILED"
                )
                if code == "DATASET_INACTIVE":
                    self.state.mark_terminal(daily_id, symbol, code)
                out.append(Result(code, "source", retryable=code != "DATASET_INACTIVE"))
                if code == "DATASET_INACTIVE":
                    break
        return out

    def _wait_terminal(self, run_id: Any, prepared: PreparedDelivery) -> str | None:
        # A bounded poll is intentionally local to explicit --deliver only.
        deadline = self.monotonic() + 15
        for _ in range(15):
            try:
                status = self.source.get_run_status(
                    run_id,
                    expected=prepared,
                    deadline=deadline,
                    monotonic=self.monotonic,
                )
            except SourceAPIDeadlineExceeded:
                return "SOURCE_TERMINAL_TIMEOUT"
            if status.status in {"completed", "completed_with_errors", "failed"}:
                return (
                    status.failure_code
                    if status.failure_code == "DATASET_INACTIVE"
                    else status.status
                )
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                break
            self.sleep(min(1.0, remaining))
        return "SOURCE_TERMINAL_TIMEOUT"


def _prepared(body: bytes) -> PreparedDelivery:
    value = json.loads(body)
    return PreparedDelivery(
        body=body,
        idempotency_key=value["idempotency_key"],
        dataset_key=value["dataset_key"],
        schema_id=value["schema_id"],
        schema_version=value["schema_version"],
    )


def _id(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]


def _past_cutoff(now: datetime, cutoff: str) -> bool:
    return now.tzinfo is None or now.astimezone(TAIPEI).strftime("%H:%M") >= cutoff


def _contracts_dir() -> Path:
    configured = os.getenv("FETCHER_CONTRACTS_DIR")
    if configured:
        return Path(configured)
    container_path = Path("/app/contracts")
    if container_path.is_dir():
        return container_path
    repository_path = Path(__file__).resolve().parents[3] / "contracts"
    if repository_path.is_dir():
        return repository_path
    return Path.cwd() / "contracts"
