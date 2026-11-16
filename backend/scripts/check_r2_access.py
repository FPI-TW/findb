"""Check what this role's R2 credentials can really do; run it inside the role's container.

    worker: python scripts/check_r2_access.py worker --raw-ref r2://... --raw-sha256 ... --keep
    serve:  python scripts/check_r2_access.py serve --canonical-key acceptance/r2-access-probe/...
    worker: python scripts/check_r2_access.py worker --delete acceptance/r2-access-probe/...

The worker check reads one existing raw object, then creates, replaces and deletes
one probe object below ``acceptance/r2-access-probe/`` in the canonical bucket. Both
checks also confirm that a read-only credential is refused when it tries to write.
Nothing is printed except step names and results; no credential, URL or object body.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from app.services import object_storage as storage
from app.utils import uuid7

PROBE_PREFIX = "acceptance/r2-access-probe/"
CONTENT_TYPE = "application/octet-stream"


def write_refused(client: Any, bucket: str, key: str) -> str:
    """``refused`` when a read-only credential cannot create ``key``; else what happened."""
    try:
        client.put_object(Bucket=bucket, Key=key, Body=b"probe", IfNoneMatch="*")
    except ClientError as exc:
        code = storage._error_code(exc)
        return "refused" if code in {"403", "AccessDenied"} else f"unexpected error {code}"
    except BotoCoreError as exc:
        return f"unexpected error {type(exc).__name__}"
    return "WRITTEN: this credential is not read-only"


def canonical_cycle(publisher: storage.CanonicalPublisher, key: str, *, keep: bool) -> dict:
    """Create, re-create, replace, stale-replace, read back and (unless kept) delete."""
    result: dict[str, Any] = {"key": key}
    first = publisher.write(key, b"first", content_type=CONTENT_TYPE, expected_etag=None)
    result["create"] = "ok"
    try:
        publisher.write(key, b"again", content_type=CONTENT_TYPE, expected_etag=None)
        result["create_again"] = "NOT REFUSED"
    except storage.ObjectChangedError:
        result["create_again"] = "refused"
    second = publisher.write(key, b"second", content_type=CONTENT_TYPE, expected_etag=first)
    result["replace_at_known_version"] = "ok" if second != first else "VERSION UNCHANGED"
    try:
        publisher.write(key, b"stale", content_type=CONTENT_TYPE, expected_etag=first)
        result["replace_at_stale_version"] = "NOT REFUSED"
    except storage.ObjectChangedError:
        result["replace_at_stale_version"] = "refused"
    stored = publisher.read(key, max_bytes=64)
    result["read_back"] = "ok" if stored == storage.StoredObject(b"second", second) else "MISMATCH"
    if not keep:
        publisher.delete(key)
        try:
            publisher.read(key, max_bytes=64)
            result["delete"] = "STILL PRESENT"
        except storage.ObjectNotFoundError:
            result["delete"] = "ok"
    return result


def passed(result: dict) -> bool:
    """Every step either succeeded or was refused as expected."""
    for name, value in result.items():
        if isinstance(value, dict):
            if not passed(value):
                return False
        elif name not in {"role", "key", "deleted"} and not (
            value == "refused" or str(value).startswith("ok")
        ):
            return False
    return True


def check_worker(arguments: argparse.Namespace) -> dict:
    result: dict[str, Any] = {"role": "worker"}
    publisher = storage.CanonicalPublisher()
    if arguments.delete:
        publisher.delete(arguments.delete)
        return {**result, "deleted": arguments.delete}
    raw_access = storage.raw_reader_access()
    if arguments.raw_ref:
        body = storage.RawObjectReader(raw_access).read(
            arguments.raw_ref, sha256=arguments.raw_sha256, max_bytes=arguments.raw_max_bytes
        )
        result["raw_read"] = f"ok, {len(body)} bytes match the checksum"
    probe = PROBE_PREFIX + str(uuid7())
    result["raw_reader_write"] = write_refused(
        storage._client(raw_access), raw_access.bucket, probe
    )
    result["canonical"] = canonical_cycle(publisher, probe, keep=arguments.keep)
    return result


def check_serve(arguments: argparse.Namespace) -> dict:
    access = storage.canonical_reader_access()
    result: dict[str, Any] = {"role": "serve"}
    if arguments.canonical_key:
        stored = storage.CanonicalReader(access).read(arguments.canonical_key, max_bytes=64)
        result["canonical_read"] = f"ok, {len(stored.body)} bytes"
    result["canonical_reader_write"] = write_refused(
        storage._client(access), access.bucket, PROBE_PREFIX + str(uuid7())
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    roles = parser.add_subparsers(dest="role", required=True)
    worker = roles.add_parser("worker")
    worker.add_argument("--raw-ref")
    worker.add_argument("--raw-sha256", default="")
    worker.add_argument("--raw-max-bytes", type=int, default=8 * 1024 * 1024)
    worker.add_argument("--keep", action="store_true", help="leave the probe object for Serve")
    worker.add_argument("--delete", metavar="KEY", help="only delete a kept probe object")
    serve = roles.add_parser("serve")
    serve.add_argument("--canonical-key")
    arguments = parser.parse_args()
    if getattr(arguments, "delete", None) and not arguments.delete.startswith(PROBE_PREFIX):
        parser.error(f"--delete only accepts keys below {PROBE_PREFIX}")
    try:
        result = check_worker(arguments) if arguments.role == "worker" else check_serve(arguments)
    except storage.ObjectStorageError as exc:
        print(json.dumps({"role": arguments.role, "failed": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if passed(result) else 1


if __name__ == "__main__":
    raise SystemExit(main())
