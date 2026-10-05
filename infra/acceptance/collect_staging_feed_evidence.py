#!/usr/bin/env python3
"""Collect one sanitized staging active-feed acceptance evidence package.

The coordinator runs locally. It injects read-only probes into the existing
Fetcher and FinDB execution units through SSM; it neither adds a host nor
creates a cross-host runtime dependency.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import shlex
import subprocess
import time
import zlib
from datetime import datetime, timedelta, timezone
from datetime import time as clock_time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
FETCHER_PROBE = REPO_ROOT / "infra" / "acceptance" / "export_fetcher_feed_evidence.py"
FINDB_PROBE = REPO_ROOT / "backend" / "scripts" / "export_staging_feed_evidence.py"
FETCHER_CONTAINERS = {
    "twelve_data": "findb-fetcher-scheduler",
    "finlab": "findb-fetcher-finlab-scheduler",
    "shioaji": "findb-fetcher-shioaji-scheduler",
    "taifex": "findb-fetcher-taifex-scheduler",
}


def catalog_feeds() -> tuple[tuple[str, str], ...]:
    catalog = json.loads(
        (REPO_ROOT / "fetcher/configs/staging_provider_pilots.v1.json").read_bytes()
    )
    if set(catalog["providers"]) != set(FETCHER_CONTAINERS):
        raise ValueError("pilot provider readiness metadata missing")
    return tuple(
        (provider, feed["dataset_key"])
        for provider, spec in catalog["providers"].items()
        for feed in spec["feeds"]
    )


def _run(command: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _aws_json(arguments: list[str], *, timeout: int = 60) -> dict[str, Any]:
    result = _run(["aws", *arguments, "--output", "json"], timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"AWS CLI command failed: {' '.join(arguments[:3])}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("AWS CLI returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("AWS CLI returned an unexpected response")
    return value


# Keep each SSM stdout response below its 24,000-character truncation limit.
TRANSPORT_VERSION = "findb-evidence-gzip-v1"
TRANSPORT_CHUNK_CHARS = 16_000
TRANSPORT_MAX_RAW_BYTES = 8 * 1024 * 1024
TRANSPORT_MAX_COMPRESSED_BYTES = 2 * 1024 * 1024
TRANSPORT_MAX_CHUNKS = 175
TRANSPORT_PREFIX = "/tmp/findb-staging-evidence-"

# Trusted wrapper: exporter stdout is data, never executable code.
_TRANSPORT_SCRIPT = r"""
import base64, gzip, hashlib, json, os, re, resource, stat, subprocess, sys, tempfile, time
nonce = config["nonce"]
if not re.fullmatch(r"[0-9a-f]{32}", nonce):
    raise ValueError("invalid transport nonce")
path = config["prefix"] + nonce + ".gz"
operation = config["operation"]
if operation == "cleanup":
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    print(json.dumps({"cleaned": True}))
elif operation == "read":
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as artifact:
        info = os.fstat(artifact.fileno())
        if not (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and info.st_mode & 0o777 == 0o600 and info.st_size <= config["max_compressed"]):
            raise ValueError("invalid transport artifact")
        index = config["index"]
        if type(index) is not int or not 0 <= index < config["max_chunks"]:
            raise ValueError("invalid chunk index")
        size = config["chunk_chars"] // 4 * 3
        artifact.seek(index * size)
        data = artifact.read(size)
    print(json.dumps({"transport": config["version"], "nonce": nonce,
                      "index": index, "data": base64.b64encode(data).decode("ascii")}))
elif operation == "export":
    # Only expired owned regular artifacts are pruned. This 24-hour grace exceeds
    # the bounded collection deadline, so active collection artifacts are retained.
    for name in os.listdir("/tmp"):
        if re.fullmatch(r"findb-staging-evidence-[0-9a-f]{32}\.gz", name):
            stale = "/tmp/" + name
            try:
                info = os.lstat(stale)
                if (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                        and info.st_mtime < time.time() - 86400):
                    os.unlink(stale)
            except FileNotFoundError:
                pass
    def limit_output():
        resource.setrlimit(resource.RLIMIT_FSIZE, (config["max_raw"], config["max_raw"]))
    with tempfile.TemporaryFile() as output:
        result = subprocess.run([sys.executable, "-c", base64.b64decode(config["script"]).decode()],
                                stdout=output, stderr=subprocess.DEVNULL, timeout=120,
                                preexec_fn=limit_output)
        if result.returncode != 0:
            raise ValueError("evidence exporter failed")
        if output.tell() > config["max_raw"]:
            raise ValueError("evidence raw byte budget exceeded")
        output.seek(0)
        raw = output.read(config["max_raw"] + 1)
    if not isinstance(json.loads(raw), dict):
        raise ValueError("evidence exporter returned an invalid payload")
    compressed = gzip.compress(raw, mtime=0)
    if len(compressed) > config["max_compressed"]:
        raise ValueError("evidence compressed budget exceeded")
    data = base64.b64encode(compressed).decode("ascii")
    chunks = (len(data) + config["chunk_chars"] - 1) // config["chunk_chars"]
    if not 1 <= chunks <= config["max_chunks"]:
        raise ValueError("evidence chunk budget exceeded")
    envelope = {"transport": config["version"], "nonce": nonce, "raw_bytes": len(raw),
                "compressed_bytes": len(compressed), "chunks": chunks,
                "sha256": hashlib.sha256(compressed).hexdigest(),
                "raw_sha256": hashlib.sha256(raw).hexdigest()}
    if chunks == 1:
        envelope["data"] = data
    else:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as artifact:
            artifact.write(compressed)
    print(json.dumps(envelope, separators=(",", ":")))
else:
    raise ValueError("invalid transport operation")
"""


def _transport_command(
    container: str,
    nonce: str,
    operation: str,
    *,
    script: bytes = b"",
    index: int = 0,
    environment: dict[str, str] | None = None,
) -> str:
    if container not in {"findb-ingest", *FETCHER_CONTAINERS.values()} or not re.fullmatch(
        r"[0-9a-f]{32}", nonce
    ):
        raise ValueError("invalid evidence transport target")
    config = {
        "version": TRANSPORT_VERSION,
        "nonce": nonce,
        "operation": operation,
        "script": base64.b64encode(script).decode("ascii"),
        "index": index,
        "prefix": TRANSPORT_PREFIX,
        "chunk_chars": TRANSPORT_CHUNK_CHARS,
        "max_raw": TRANSPORT_MAX_RAW_BYTES,
        "max_compressed": TRANSPORT_MAX_COMPRESSED_BYTES,
        "max_chunks": TRANSPORT_MAX_CHUNKS,
    }
    source = "config = " + repr(config) + "\n" + _TRANSPORT_SCRIPT
    encoded = base64.b64encode(source.encode()).decode("ascii")
    environment_args = "".join(
        f"-e {shlex.quote(name)}={shlex.quote(value)} "
        for name, value in sorted((environment or {}).items())
    )
    return (
        "set -eu\n"
        f"printf '%s' '{encoded}' | base64 -d | "
        f"docker exec -i {environment_args}{container} python -"
    )


def _probe_command(
    script: Path,
    container: str,
    provider: str | None = None,
    environment: dict[str, str] | None = None,
    *,
    nonce: str | None = None,
) -> str:
    variables = dict(environment or {})
    if provider:
        variables["FINDB_EVIDENCE_PROVIDER"] = provider
    return _transport_command(
        container,
        nonce or secrets.token_hex(16),
        "export",
        script=script.read_bytes(),
        environment=variables,
    )


def _ssm_json(
    *, region: str, instance_id: str, command: str, deadline: float
) -> tuple[str, dict[str, Any]]:
    def request(arguments: list[str]) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("SSM evidence transport exceeded collection timeout")
        return _aws_json(arguments, timeout=max(1, min(60, int(remaining))))

    sent = request(
        [
            "ssm",
            "send-command",
            "--region",
            region,
            "--instance-ids",
            instance_id,
            "--document-name",
            "AWS-RunShellScript",
            "--comment",
            "FinDB staging active-feed read-only evidence probe",
            "--parameters",
            json.dumps({"commands": [command], "executionTimeout": ["130"]}, separators=(",", ":")),
        ]
    )
    command_id = str(sent["Command"]["CommandId"])
    while time.monotonic() < deadline:
        response = request(
            [
                "ssm",
                "get-command-invocation",
                "--region",
                region,
                "--command-id",
                command_id,
                "--instance-id",
                instance_id,
            ]
        )
        status = response.get("Status")
        if status == "Success":
            try:
                stdout = response["StandardOutputContent"]
                if not isinstance(stdout, str) or len(stdout) > 20_000:
                    raise ValueError("stdout budget exceeded")
                payload = json.loads(stdout.strip())
            except (KeyError, ValueError) as exc:
                raise RuntimeError(f"SSM probe {command_id} returned invalid JSON") from exc
            if not isinstance(payload, dict):
                raise RuntimeError(f"SSM probe {command_id} returned an invalid payload")
            return command_id, payload
        if status in {"Cancelled", "Failed", "TimedOut", "Undeliverable", "Terminated"}:
            raise RuntimeError(f"SSM probe {command_id} finished with status {status}")
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    raise RuntimeError(f"SSM probe {command_id} did not finish before timeout")


def _decode_transport(envelope: dict[str, Any], chunks: list[str], nonce: str) -> dict[str, Any]:
    try:
        if envelope["transport"] != TRANSPORT_VERSION or envelope["nonce"] != nonce:
            raise ValueError("transport identity mismatch")
        for field, maximum in (
            ("raw_bytes", TRANSPORT_MAX_RAW_BYTES),
            ("compressed_bytes", TRANSPORT_MAX_COMPRESSED_BYTES),
            ("chunks", TRANSPORT_MAX_CHUNKS),
        ):
            if type(envelope[field]) is not int or not 1 <= envelope[field] <= maximum:
                raise ValueError("transport budget exceeded")
        encoded_size = 4 * ((envelope["compressed_bytes"] + 2) // 3)
        if (
            envelope["chunks"]
            != (encoded_size + TRANSPORT_CHUNK_CHARS - 1) // TRANSPORT_CHUNK_CHARS
        ):
            raise ValueError("chunk count mismatch")
        if len(chunks) != envelope["chunks"] or any(
            not isinstance(chunk, str)
            or len(chunk)
            != min(TRANSPORT_CHUNK_CHARS, encoded_size - index * TRANSPORT_CHUNK_CHARS)
            for index, chunk in enumerate(chunks)
        ):
            raise ValueError("chunk size mismatch")
        compressed = base64.b64decode("".join(chunks), validate=True)
        if (
            len(compressed) != envelope["compressed_bytes"]
            or hashlib.sha256(compressed).hexdigest() != envelope["sha256"]
        ):
            raise ValueError("compressed checksum mismatch")
        decoder = zlib.decompressobj(wbits=31)
        raw = decoder.decompress(compressed, envelope["raw_bytes"] + 1)
        if (
            len(raw) != envelope["raw_bytes"]
            or not decoder.eof
            or decoder.unused_data
            or decoder.unconsumed_tail
            or hashlib.sha256(raw).hexdigest() != envelope["raw_sha256"]
        ):
            raise ValueError("raw checksum or decompression budget mismatch")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("unexpected payload")
        return payload
    except (KeyError, TypeError, ValueError, zlib.error) as exc:
        raise RuntimeError("invalid SSM evidence transport") from exc


def _ssm_probe(
    *,
    region: str,
    instance_id: str,
    script: Path,
    container: str,
    provider: str | None = None,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    nonce = secrets.token_hex(16)
    deadline = time.monotonic() + timeout_seconds
    try:
        command_id, envelope = _ssm_json(
            region=region,
            instance_id=instance_id,
            command=_probe_command(script, container, provider, nonce=nonce),
            deadline=deadline,
        )
        if envelope.get("transport") != TRANSPORT_VERSION or envelope.get("nonce") != nonce:
            raise RuntimeError("invalid SSM evidence transport identity")
        count = envelope.get("chunks")
        compressed_size = envelope.get("compressed_bytes")
        raw_size = envelope.get("raw_bytes")
        if (
            type(count) is not int
            or not 1 <= count <= TRANSPORT_MAX_CHUNKS
            or type(compressed_size) is not int
            or not 1 <= compressed_size <= TRANSPORT_MAX_COMPRESSED_BYTES
            or type(raw_size) is not int
            or not 1 <= raw_size <= TRANSPORT_MAX_RAW_BYTES
            or count
            != (4 * ((compressed_size + 2) // 3) + TRANSPORT_CHUNK_CHARS - 1)
            // TRANSPORT_CHUNK_CHARS
        ):
            raise RuntimeError("invalid SSM evidence transport budget")
        if count == 1:
            inline = envelope.get("data")
            if not isinstance(inline, str):
                raise RuntimeError("invalid SSM evidence transport inline data")
            chunks = [inline]
        else:
            if "data" in envelope:
                raise RuntimeError("unexpected inline chunk data")
            chunks = []
            for index in range(count):
                _, chunk = _ssm_json(
                    region=region,
                    instance_id=instance_id,
                    command=_transport_command(container, nonce, "read", index=index),
                    deadline=deadline,
                )
                if (
                    chunk.get("transport") != TRANSPORT_VERSION
                    or chunk.get("nonce") != nonce
                    or type(chunk.get("index")) is not int
                    or chunk["index"] != index
                    or not isinstance(chunk.get("data"), str)
                ):
                    raise RuntimeError("invalid SSM evidence transport chunk order")
                chunks.append(chunk["data"])
        return {"command_id": command_id, "payload": _decode_transport(envelope, chunks, nonce)}
    finally:
        # Cleanup cannot hide the original probe or decoding failure.
        try:
            _ssm_json(
                region=region,
                instance_id=instance_id,
                command=_transport_command(container, nonce, "cleanup"),
                deadline=time.monotonic() + 10,
            )
        except Exception:
            pass


def _expected_target(findb: dict[str, Any], provider: str, *, observed_at: datetime) -> str:
    """Derive eligibility from runtime schedule and official calendar, never outcomes."""
    try:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            return ""
        evidence = findb["target_calendar_evidence"]
        sampled_at = datetime.fromisoformat(evidence["observed_at_utc"])
        if (
            evidence["schema_version"] != 1
            or sampled_at.utcoffset() != timedelta(0)
            or abs((observed_at - sampled_at).total_seconds()) > 300
        ):
            return ""
        market = {"twelve_data": "US", "finlab": "TW", "shioaji": "TW", "taifex": "TAIFEX"}[
            provider
        ]
        timezone_name = "America/New_York" if market == "US" else "Asia/Taipei"
        calendar = evidence["markets"][market]
        if calendar["valid"] is not True or calendar["timezone"] != timezone_name:
            return ""
        revisions = {item["year"]: item for item in calendar["revisions"]}
        if len(revisions) != len(calendar["revisions"]) or any(
            item["status"] != "published"
            or item["coverage_complete"] is not True
            or type(item["revision"]) is not int
            or item["revision"] < 1
            or not item["revision_id"]
            or item["timezone"] != timezone_name
            for item in revisions.values()
        ):
            return ""
        days = {item["trade_date"]: item for item in calendar["days"]}
        if len(days) != len(calendar["days"]):
            return ""
        local = observed_at.astimezone(ZoneInfo("Asia/Taipei"))
        due = {
            "twelve_data": clock_time(8, 15),
            "finlab": clock_time(14, 30),
            "shioaji": clock_time(14, 30),
            "taifex": clock_time(18),
        }[provider]
        # US 09:00 is the delivery deadline; eligibility begins at its 08:15 trigger.
        candidate = local.date() - timedelta(days=int(local.time() < due))
        if provider == "twelve_data":
            candidate -= timedelta(days=1)
        for _ in range(14):
            day = days[candidate.isoformat()]
            revision = revisions[candidate.year]
            if (
                day["revision"] != revision["revision"]
                or day["revision_id"] != revision["revision_id"]
            ):
                return ""
            if day["day_status"] not in {"open", "closed", "settlement_only"} or (
                day["is_open"] is not (day["day_status"] == "open")
            ):
                return ""
            if day["day_status"] == "open":
                close = clock_time.fromisoformat(
                    day["session_close"]
                    or ("16:00" if market == "US" else "13:45" if market == "TAIFEX" else "13:30")
                )
                completed_at = datetime.combine(candidate, close, tzinfo=ZoneInfo(timezone_name))
                if observed_at >= completed_at:
                    return candidate.isoformat()
            candidate -= timedelta(days=1)
    except (KeyError, TypeError, ValueError):
        return ""
    return ""


# Kept identical in the standalone collector: both scripts execute on separate hosts.
PROJECTION_FIELDS = {
    "market_eod": ("total_ticks", "turnover"),
    "market_minute": (
        "bar_start_time",
        "bar_end_time",
        "signal_time",
        "market_timezone",
        "turnover",
        "trade_count",
        "price_adjustment",
    ),
    "futures_eod": (
        "contract_id",
        "product_code",
        "contract_code",
        "contract_month",
        "session",
        "settlement_price",
        "open_interest",
    ),
}
NUMERIC_FIELDS = {
    "open",
    "high",
    "low",
    "close",
    "volume",
    "total_ticks",
    "turnover",
    "trade_count",
    "settlement_price",
    "open_interest",
}
TIME_FIELDS = {"source_fetched_at", "bar_start_time", "bar_end_time", "signal_time"}


def _canonical_projection(schema_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """Normalize only public canonical values, requiring provenance and all fields."""
    fields = (
        "instrument_id",
        "trade_date",
        "source",
        "source_fetched_at",
        "open",
        "high",
        "low",
        "close",
        "volume",
        *PROJECTION_FIELDS[schema_id],
    )
    result: dict[str, Any] = {}
    for field in fields:
        value = record[field]
        if field in TIME_FIELDS:
            if value is None:
                raise ValueError("canonical timestamp missing")
            stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError("canonical timestamp must be aware")
            value = stamp.astimezone(timezone.utc).isoformat(timespec="microseconds")
        elif field in NUMERIC_FIELDS and value is not None:
            numeric = Decimal(str(value))
            if not numeric.is_finite():
                raise ValueError("canonical numeric value must be finite")
            value = format(numeric.normalize(), "f") if numeric else "0"
        elif value is not None:
            value = str(value)
        result[field] = value
    if not result["source"]:
        raise ValueError("canonical source missing")
    return result


def _projection_fingerprint(projection: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _valid_queue_receipts(row: dict[str, Any]) -> bool:
    receipts = row.get("outbox_receipts", [])
    current = [item for item in receipts if item.get("delivery_id") == row.get("job_delivery_id")]
    return bool(row.get("job_id") and row.get("job_delivery_id") and current) and (
        len({item.get("outbox_id") for item in receipts}) == len(receipts)
        and all(
            item.get("outbox_id")
            and item.get("run_id") == row.get("run_id")
            and item.get("job_id") == row.get("job_id")
            and item.get("delivery_id")
            and item.get("event_type") == "normalize_run"
            for item in receipts
        )
        and len({item.get("status") for item in current}) == 1
        and all(item.get("status") in {"pending", "publishing", "published"} for item in current)
        and row.get("outbox_status") == current[0].get("status")
    )


def _valid_serve_proof(feed: dict[str, Any], provider: str, latest: str, run_ids: set[str]) -> bool:
    probe = feed.get("serve_probe", {})
    sample = feed.get("canonical_sample") or {}
    try:
        expected = _canonical_projection(feed["schema_id"], sample["sample_record"])
        actual = _canonical_projection(feed["schema_id"], probe["returned_projection"])
        fingerprint = _projection_fingerprint(expected)
        return (
            probe.get("success") is True
            and probe.get("http_status") == 200
            and probe.get("projection_version") == 1
            and expected == actual == probe.get("canonical_projection")
            and fingerprint
            == probe.get("canonical_fingerprint")
            == probe.get("returned_fingerprint")
            and expected["source"] == provider
            and expected["trade_date"]
            == latest
            == sample.get("sample_trade_date")
            == probe.get("trade_date")
            and expected["instrument_id"]
            == sample.get("sample_instrument_id")
            == probe.get("instrument_id")
            and sample.get("sample_run_id") == probe.get("run_id")
            and sample.get("sample_run_id") in run_ids
            and str(sample["sample_record"]["run_id"]) == sample.get("sample_run_id")
        )
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return False


def _valid_receipts(row: dict[str, Any], provider: str, definition: dict[str, Any]) -> bool:
    receipts = row.get("attempt_receipts", [])
    return bool(receipts) and (
        len({item.get("attempt_id") for item in receipts}) == len(receipts)
        and all(
            item.get("attempt_id")
            and item.get("run_id") == row.get("run_id")
            and item.get("source") == provider
            and item.get("dataset_key") == definition["dataset_key"]
            and item.get("schema_id") == definition["schema_id"]
            and item.get("schema_version") == 1
            and item.get("http_status") == 202
            and item.get("status") in {"accepted", "duplicate"}
            and item.get("request_sha256")
            for item in receipts
        )
        and len({item.get("request_sha256") for item in receipts}) == 1
        and any(item.get("status") == "accepted" for item in receipts)
    )


def _functional_acceptance(
    findb: dict[str, Any], fetcher: dict[str, Any], *, observed_at: datetime | None = None
) -> dict[str, bool]:
    observed_at = observed_at or datetime.now(timezone.utc)
    catalog = json.loads(
        (REPO_ROOT / "fetcher/configs/staging_provider_pilots.v1.json").read_bytes()
    )
    feeds = {(item["source"], item["dataset_key"]): item for item in findb["feeds"]}
    result = {}
    for provider, spec in catalog["providers"].items():
        config = fetcher.get(provider, {}).get("config", {})
        file_name = {
            "twelve_data": "twelve_data_us_staging_pilot.v2.json",
            "finlab": "finlab_tw_review_required.v1.json",
            "shioaji": "shioaji_tw_staging_pilot.v3.json",
            "taifex": "taifex_tw_staging_pilot.v1.json",
        }[provider]
        config_bound = (
            config.get(
                "universe_sha256" if provider in {"twelve_data", "finlab"} else "manifest_sha256"
            )
            == catalog["files"][file_name]
        )
        if provider in {"twelve_data", "finlab"}:
            config_bound = (
                config_bound
                and config.get("schedule_sha256")
                == catalog["files"]["daily_scheduler.staging.v3.json"]
            )
        observations = fetcher.get(provider, {}).get("recent_trade_dates", [])
        for definition in spec["feeds"]:
            dataset = definition["dataset_key"]
            feed = feeds.get((provider, dataset), {})
            latest = _expected_target(findb, provider, observed_at=observed_at)
            ready = [
                row
                for row in observations
                if row.get("dataset_key", dataset) == dataset
                and str(row.get("target_data_date") or row.get("target_date")) == latest
            ]
            observations_complete = bool(ready) and all(
                (
                    row.get("status") == "completed"
                    and row.get("terminal_status") in {None, "completed"}
                )
                or (row.get("status") == "terminal" and row.get("terminal_status") == "completed")
                for row in ready
            )
            run_ids = set()
            for row in ready:
                if str(row.get("target_data_date") or row.get("target_date")) == latest:
                    run_ids.update(str(row.get("run_ids") or row.get("run_id") or "").split(","))
            run_ids.discard("")
            runs = [row for row in feed.get("runs", []) if row.get("batch_data_date") == latest]
            records = [record for row in runs for record in (row.get("canonical_coverage") or [])]
            expected = {
                "twelve_data": {"AAPL", "MSFT"},
                "finlab": {"2330", "2317"},
                "shioaji": {"2330"} if dataset == "tw_equity_minute" else {"0050"},
                "taifex": {"TX", "MTX"},
            }[provider]
            okay = (
                config_bound
                and bool(latest)
                and observations_complete
                and bool(runs)
                and len({row["run_id"] for row in runs}) == len(runs)
                and {row["run_id"] for row in runs} == run_ids
                and {record.get("symbol") for record in records} == expected
                and all(record.get("trade_date") == latest for record in records)
            )
            okay = okay and all(
                _valid_receipts(row, provider, definition)
                and _valid_queue_receipts(row)
                and row.get("source_http_status") == 202
                and row.get("attempt_status") in {"accepted", "duplicate"}
                and row.get("raw_persisted") is True
                and row.get("status") == "completed"
                and row.get("job_status") == "completed"
                and row.get("dq_errors") == 0
                and row.get("canonical_rows")
                == row.get("success_records")
                == row.get("total_records")
                and row.get("canonical_rows", 0) > 0
                for row in runs
            )
            if provider == "taifex":
                okay = (
                    okay
                    and len(records) == 4
                    and all(
                        record.get("row_count") == 1
                        and len(record.get("contract_month", "")) == 6
                        and record["contract_month"].isdigit()
                        and record.get("contract_code")
                        == f"{record.get('symbol')}:{record.get('contract_month')}"
                        for record in records
                    )
                    and {(record.get("symbol"), record.get("session")) for record in records}
                    == {
                        (product, session)
                        for product in expected
                        for session in ("regular", "after_hours")
                    }
                    and all(
                        len(
                            {
                                record["contract_month"]
                                for record in records
                                if record["symbol"] == product
                            }
                        )
                        == 1
                        for product in expected
                    )
                )
            okay = okay and _valid_serve_proof(feed, provider, latest, run_ids)
            result[f"{provider}/{dataset}"] = bool(okay)
    return result


def _assessment(
    findb: dict[str, Any], fetcher: dict[str, Any], *, observed_at: datetime | None = None
) -> dict[str, Any]:
    observed_at = observed_at or datetime.now(timezone.utc)
    backend_feeds = {(item["source"], item["dataset_key"]): item for item in findb["feeds"]}
    fetcher_by_provider = {item["provider"]: item for item in fetcher.values()}
    source_202 = {}
    for provider, dataset in catalog_feeds():
        rows = backend_feeds.get((provider, dataset), {}).get("runs", [])
        source_202[f"{provider}/{dataset}"] = any(
            row["source_http_status"] == 202 and row["attempt_status"] in {"accepted", "duplicate"}
            for row in rows
        )
    serve_boundaries = {
        f"{source}/{dataset}": item["lineage_contract"]["serve"]
        for (source, dataset), item in backend_feeds.items()
    }
    serve_probes = {
        f"{source}/{dataset}": _valid_serve_proof(
            item,
            source,
            _expected_target(findb, source, observed_at=observed_at),
            {row["run_id"] for row in item.get("runs", []) if row.get("run_id")},
        )
        for (source, dataset), item in backend_feeds.items()
    }
    backend_multi_date = all(item["multi_trade_date_ready"] for item in findb["feeds"])
    fetcher_multi_date = all(
        len(
            {
                str(row.get("target_data_date") or row.get("target_date"))
                for row in item["recent_trade_dates"]
            }
        )
        >= 2
        for item in fetcher_by_provider.values()
    )
    functional = _functional_acceptance(findb, fetcher, observed_at=observed_at)
    return {
        "pilot_target_dates": {
            f"{provider}/{dataset}": _expected_target(findb, provider, observed_at=observed_at)
            for provider, dataset in catalog_feeds()
        },
        "pilot_functional_acceptance": functional,
        "pilot_functional_complete": all(functional.values())
        and set(backend_feeds) == set(catalog_feeds())
        and set(fetcher_by_provider) == set(FETCHER_CONTAINERS),
        "five_feed_scope_complete": set(backend_feeds) == set(catalog_feeds()),
        "four_provider_scope_complete": set(fetcher_by_provider) == set(FETCHER_CONTAINERS),
        "backend_multi_trade_date_complete": backend_multi_date,
        "fetcher_multi_trade_date_complete": fetcher_multi_date,
        "persistent_source_202": source_202,
        "serve_boundary": serve_boundaries,
        "serve_probe": serve_probes,
        "serve_boundary_complete": set(serve_boundaries)
        == {f"{source}/{dataset}" for source, dataset in catalog_feeds()}
        and set(serve_boundaries.values()) == {"required"}
        and all(serve_probes.values()),
    }


def _git_sha() -> str:
    result = _run(["git", "rev-parse", "HEAD"])
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _upload(*, path: Path, region: str, bucket: str, key: str, kms_key_arn: str) -> dict[str, Any]:
    response = _aws_json(
        [
            "s3api",
            "put-object",
            "--region",
            region,
            "--bucket",
            bucket,
            "--key",
            key,
            "--body",
            str(path),
            "--content-type",
            "application/json",
            "--server-side-encryption",
            "aws:kms",
            "--ssekms-key-id",
            kms_key_arn,
            "--if-none-match",
            "*",
        ]
    )
    return {
        "bucket": bucket,
        "key": key,
        "version_id": response.get("VersionId"),
        "server_side_encryption": response.get("ServerSideEncryption"),
        "ssekms_key_id": response.get("SSEKMSKeyId"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="ap-southeast-1")
    parser.add_argument("--findb-instance-id", required=True)
    parser.add_argument("--fetcher-instance-id", required=True)
    parser.add_argument("--phase", choices=("pre", "post"), required=True)
    parser.add_argument("--pre-manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bucket")
    parser.add_argument("--kms-key-arn")
    parser.add_argument("--s3-key")
    args = parser.parse_args()
    if args.phase == "post" and args.pre_manifest is None:
        parser.error("--pre-manifest is required for post phase")
    if bool(args.bucket) != bool(args.kms_key_arn):
        parser.error("--bucket and --kms-key-arn must be supplied together")

    findb_result = _ssm_probe(
        region=args.region,
        instance_id=args.findb_instance_id,
        script=FINDB_PROBE,
        container="findb-ingest",
    )
    fetcher_results = {
        provider: _ssm_probe(
            region=args.region,
            instance_id=args.fetcher_instance_id,
            script=FETCHER_PROBE,
            container=container,
            provider=provider,
        )
        for provider, container in FETCHER_CONTAINERS.items()
    }
    captured_at = datetime.now(timezone.utc).isoformat()
    pre_sha256 = None
    if args.pre_manifest:
        pre_sha256 = hashlib.sha256(args.pre_manifest.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "environment": "staging",
        "phase": args.phase,
        "captured_at": captured_at,
        "repository_commit": _git_sha(),
        "pilot_catalog_sha256": hashlib.sha256(
            (REPO_ROOT / "fetcher/configs/staging_provider_pilots.v1.json").read_bytes()
        ).hexdigest(),
        "evidence_kind": "live_pipeline_observation",
        "topology": {
            "kind": "single_coordinator_unit_local_probes",
            "additional_hosts": 0,
            "runtime_cross_host_dependency": False,
        },
        "pre_manifest_sha256": pre_sha256,
        "probe_command_ids": {
            "findb": findb_result["command_id"],
            **{
                f"fetcher_{provider}": result["command_id"]
                for provider, result in fetcher_results.items()
            },
        },
        "findb": findb_result["payload"],
        "fetcher": {provider: result["payload"] for provider, result in fetcher_results.items()},
    }
    manifest["assessment"] = _assessment(
        manifest["findb"], manifest["fetcher"], observed_at=datetime.fromisoformat(captured_at)
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    args.output.write_text(serialized, encoding="utf-8")
    os.chmod(args.output, 0o600)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    upload = None
    if args.bucket:
        key = args.s3_key or (
            f"evidence/staging/active-feeds/{captured_at[:10]}/{args.phase}-{digest[:16]}.json"
        )
        upload = _upload(
            path=args.output,
            region=args.region,
            bucket=args.bucket,
            key=key,
            kms_key_arn=args.kms_key_arn,
        )
    print(json.dumps({"sha256": digest, "output": str(args.output), "upload": upload}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
