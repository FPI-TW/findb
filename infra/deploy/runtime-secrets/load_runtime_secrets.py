#!/usr/bin/env python3
"""Load an allowlisted Secrets Manager consumer bundle into a tmpfs env file."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


class LoaderError(RuntimeError):
    """A bounded, secret-free loader failure."""


SecretFetcher = Callable[[str], str]
ENVIRONMENT_KEY = re.compile(r"[A-Z_][A-Z0-9_]*")
SECRET_RELATIVE_NAME = re.compile(r"[a-z0-9][a-z0-9/-]*")
STAGING_AWS_REGION = "ap-southeast-1"
RUNTIME_SECRET_ROOT = Path("/run/findb-runtime-secrets")


def _catalog(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LoaderError("catalog_invalid") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise LoaderError("catalog_invalid")
    unit = value.get("unit")
    secret_prefix = value.get("secret_prefix")
    if (
        not isinstance(unit, str)
        or re.fullmatch(r"[a-z0-9-]+", unit) is None
        or secret_prefix != f"findb/staging/{unit}/"
    ):
        raise LoaderError("catalog_invalid")
    if not isinstance(value.get("consumers"), dict):
        raise LoaderError("catalog_invalid")
    return value


def _aws_fetcher(region: str) -> SecretFetcher:
    if region != STAGING_AWS_REGION:
        raise LoaderError("region_invalid")

    def fetch(secret_id: str) -> str:
        try:
            completed = subprocess.run(
                [
                    "aws",
                    "secretsmanager",
                    "get-secret-value",
                    "--region",
                    region,
                    "--secret-id",
                    secret_id,
                    "--version-stage",
                    "AWSCURRENT",
                    "--query",
                    "SecretString",
                    "--output",
                    "text",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
        except FileNotFoundError as exc:
            raise LoaderError("aws_cli_missing") from exc
        except subprocess.TimeoutExpired as exc:
            raise LoaderError("secret_fetch_failed") from exc
        except (OSError, UnicodeDecodeError) as exc:
            raise LoaderError("secret_fetch_failed") from exc
        if completed.returncode != 0:
            raise LoaderError("secret_fetch_failed")
        return completed.stdout.rstrip("\n")

    return fetch


def _safe_value(value: object, *, required: bool) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise LoaderError("secret_schema_invalid")
    if required and not value:
        raise LoaderError("secret_required_value_empty")
    if not value and not required:
        return None
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise LoaderError("secret_value_unsafe")
    return value


def load_consumer(
    catalog: Mapping[str, Any],
    consumer_name: str,
    fetch_secret: SecretFetcher,
) -> dict[str, str]:
    if re.fullmatch(r"[a-z0-9-]+", consumer_name) is None:
        raise LoaderError("consumer_not_allowed")
    consumers = catalog.get("consumers")
    if not isinstance(consumers, Mapping) or consumer_name not in consumers:
        raise LoaderError("consumer_not_allowed")
    consumer = consumers[consumer_name]
    if not isinstance(consumer, Mapping) or not isinstance(consumer.get("secrets"), list):
        raise LoaderError("catalog_invalid")
    unit = catalog.get("unit")
    prefix = catalog.get("secret_prefix")
    if (
        not isinstance(unit, str)
        or re.fullmatch(r"[a-z0-9-]+", unit) is None
        or prefix != f"findb/staging/{unit}/"
    ):
        raise LoaderError("catalog_invalid")

    loaded: dict[str, str] = {}
    for secret in consumer["secrets"]:
        if not isinstance(secret, Mapping):
            raise LoaderError("catalog_invalid")
        relative_name = secret.get("name")
        required_keys = secret.get("required_keys", [])
        optional_keys = secret.get("optional_keys", [])
        if (
            not isinstance(relative_name, str)
            or SECRET_RELATIVE_NAME.fullmatch(relative_name) is None
            or "//" in relative_name
            or relative_name.endswith("/")
            or not isinstance(required_keys, list)
            or not isinstance(optional_keys, list)
            or not all(
                isinstance(key, str) and ENVIRONMENT_KEY.fullmatch(key) is not None
                for key in (*required_keys, *optional_keys)
            )
        ):
            raise LoaderError("catalog_invalid")
        allowed_keys = set((*required_keys, *optional_keys))
        if len(allowed_keys) != len(required_keys) + len(optional_keys):
            raise LoaderError("catalog_invalid")
        try:
            payload = json.loads(fetch_secret(f"{prefix}{relative_name}"))
        except (TypeError, json.JSONDecodeError) as exc:
            raise LoaderError("secret_json_invalid") from exc
        if not isinstance(payload, dict) or set(payload) - allowed_keys:
            raise LoaderError("secret_schema_invalid")
        if any(key not in payload for key in required_keys):
            raise LoaderError("secret_required_key_missing")
        for key in required_keys:
            value = _safe_value(payload[key], required=True)
            assert value is not None
            if key in loaded:
                raise LoaderError("secret_key_collision")
            loaded[key] = value
        for key in optional_keys:
            value = _safe_value(payload.get(key), required=False)
            if value is None:
                continue
            if key in loaded:
                raise LoaderError("secret_key_collision")
            loaded[key] = value

    constraints = consumer.get("distinct_keys", [])
    if not isinstance(constraints, list):
        raise LoaderError("catalog_invalid")
    for keys in constraints:
        if (
            not isinstance(keys, list)
            or len(keys) < 2
            or not all(isinstance(key, str) for key in keys)
        ):
            raise LoaderError("catalog_invalid")
        try:
            values = [loaded[key] for key in keys]
        except KeyError as exc:
            raise LoaderError("catalog_invalid") from exc
        if len(values) != len(set(values)):
            raise LoaderError("distinct_constraint_failed")
    return loaded


def _filesystem_type(path: Path) -> str:
    try:
        completed = subprocess.run(
            ["findmnt", "-n", "-o", "FSTYPE", "-T", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError as exc:
        raise LoaderError("findmnt_missing") from exc
    except (OSError, UnicodeDecodeError, subprocess.TimeoutExpired) as exc:
        raise LoaderError("tmpfs_check_failed") from exc
    filesystem_type = completed.stdout.strip()
    if completed.returncode != 0 or not filesystem_type:
        raise LoaderError("tmpfs_check_failed")
    return filesystem_type


def _parse_owner(value: str) -> tuple[int, int]:
    try:
        uid_text, gid_text = value.split(":", 1)
        uid = int(uid_text)
        gid = int(gid_text)
    except (ValueError, TypeError) as exc:
        raise LoaderError("owner_invalid") from exc
    if uid < 0 or gid < 0:
        raise LoaderError("owner_invalid")
    return uid, gid


def ensure_runtime_root(
    run_root: Path = RUNTIME_SECRET_ROOT,
    *,
    filesystem_type: Callable[[Path], str] = _filesystem_type,
    owner: tuple[int, int] = (0, 0),
) -> bool:
    """Create the empty runtime-secret root only on the existing /run tmpfs.

    Returns whether this invocation created the directory.  An existing root is
    never repaired: a symlink, non-directory, or unexpected owner/mode is a
    fail-closed condition so an unsafe path cannot become a secret destination.
    """

    run_root = Path(os.path.abspath(run_root))
    if run_root.parent == run_root:
        raise LoaderError("runtime_root_path_invalid")
    if (
        not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or os.open not in os.supports_dir_fd
        or os.mkdir not in os.supports_dir_fd
    ):
        raise LoaderError("platform_unsupported")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_fd: int | None = None
    root_fd: int | None = None
    created = False
    try:
        parent_fd = os.open(run_root.parent, directory_flags)
        parent_fd_path = Path(f"/proc/{os.getpid()}/fd/{parent_fd}")
        if filesystem_type(parent_fd_path) != "tmpfs":
            raise LoaderError("runtime_parent_not_tmpfs")

        try:
            os.mkdir(run_root.name, 0o700, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass

        root_fd = os.open(run_root.name, directory_flags, dir_fd=parent_fd)
        root_fd_path = Path(f"/proc/{os.getpid()}/fd/{root_fd}")
        if filesystem_type(root_fd_path) != "tmpfs":
            raise LoaderError("runtime_root_not_tmpfs")

        if created:
            os.fchmod(root_fd, 0o700)
            os.fchown(root_fd, *owner)
        metadata = os.fstat(root_fd)
        if stat.S_IMODE(metadata.st_mode) != 0o700 or (metadata.st_uid, metadata.st_gid) != owner:
            raise LoaderError("runtime_root_metadata_invalid")
        return created
    except LoaderError:
        raise
    except OSError as exc:
        raise LoaderError("runtime_root_path_invalid") from exc
    finally:
        if root_fd is not None:
            os.close(root_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def write_env_file(
    output: Path,
    values: Mapping[str, str],
    *,
    owner: tuple[int, int],
    filesystem_type: Callable[[Path], str] = _filesystem_type,
    run_root: Path = RUNTIME_SECRET_ROOT,
    runtime_root_owner: tuple[int, int] = (0, 0),
    remove_after_validation: bool = False,
) -> None:
    if not values or any(
        ENVIRONMENT_KEY.fullmatch(key) is None or _safe_value(value, required=True) is None
        for key, value in values.items()
    ):
        raise LoaderError("secret_schema_invalid")
    run_root = Path(os.path.abspath(run_root))
    output = Path(os.path.abspath(output))
    try:
        relative_output = output.relative_to(run_root)
    except ValueError as exc:
        raise LoaderError("output_outside_run") from exc
    if (
        len(relative_output.parts) != 2
        or re.fullmatch(r"[a-z0-9-]+", relative_output.parts[0]) is None
        or relative_output.parts[1] != "runtime.env"
    ):
        raise LoaderError("output_outside_run")

    ensure_runtime_root(
        run_root,
        filesystem_type=filesystem_type,
        owner=runtime_root_owner,
    )

    if (
        not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or os.open not in os.supports_dir_fd
        or os.mkdir not in os.supports_dir_fd
        or os.unlink not in os.supports_dir_fd
    ):
        raise LoaderError("platform_unsupported")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW

    root_fd: int | None = None
    directory_fd: int | None = None
    fd: int | None = None
    created = False
    try:
        root_fd = os.open(run_root, directory_flags)
        root_fd_path = Path(f"/proc/{os.getpid()}/fd/{root_fd}")
        if filesystem_type(root_fd_path) != "tmpfs":
            raise LoaderError("output_not_tmpfs")

        directory_name, file_name = relative_output.parts
        try:
            os.mkdir(directory_name, 0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        directory_fd = os.open(directory_name, directory_flags, dir_fd=root_fd)
        directory_fd_path = Path(f"/proc/{os.getpid()}/fd/{directory_fd}")
        if filesystem_type(directory_fd_path) != "tmpfs":
            raise LoaderError("output_not_tmpfs")
        os.fchmod(directory_fd, 0o700)
        os.fchown(directory_fd, *owner)
        parent_metadata = os.fstat(directory_fd)
        if stat.S_IMODE(parent_metadata.st_mode) != 0o700:
            raise LoaderError("output_directory_invalid")
        if (parent_metadata.st_uid, parent_metadata.st_gid) != owner:
            raise LoaderError("output_directory_invalid")

        fd = os.open(file_name, file_flags, 0o600, dir_fd=directory_fd)
        created = True
        os.fchmod(fd, 0o600)
        os.fchown(fd, *owner)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = None
            for key in sorted(values):
                handle.write(f"{key}={shlex.quote(values[key])}\n")
            handle.flush()
            os.fsync(handle.fileno())
            metadata = os.fstat(handle.fileno())
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                raise LoaderError("output_mode_invalid")
            if (metadata.st_uid, metadata.st_gid) != owner:
                raise LoaderError("output_owner_invalid")
        if remove_after_validation:
            os.unlink(file_name, dir_fd=directory_fd)
            created = False
    except FileExistsError as exc:
        raise LoaderError("output_exists") from exc
    except LoaderError:
        raise
    except OSError as exc:
        raise LoaderError("output_write_failed") from exc
    finally:
        if fd is not None:
            os.close(fd)
        if created and sys.exc_info()[0] is not None and directory_fd is not None:
            try:
                os.unlink(relative_output.parts[1], dir_fd=directory_fd)
            except OSError:
                pass
        if directory_fd is not None:
            os.close(directory_fd)
        if root_fd is not None:
            os.close(root_fd)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ensure-runtime-root", action="store_true")
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--consumer")
    parser.add_argument("--region")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--owner", default=f"{os.getuid()}:{os.getgid()}")
    parser.add_argument("--check-only", action="store_true")
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    if arguments.ensure_runtime_root:
        try:
            if any(
                value is not None
                for value in (arguments.catalog, arguments.consumer, arguments.region, arguments.output)
            ):
                raise LoaderError("runtime_root_arguments_invalid")
            if os.geteuid() != 0:
                raise LoaderError("root_required")
            created = ensure_runtime_root()
            state = "created" if created else "ready"
            print(f"runtime_secret_root={state}")
            return 0
        except LoaderError as exc:
            print(f"runtime_secret_root=failed reason={exc}", file=sys.stderr)
            return 1

    if (
        arguments.catalog is None
        or arguments.consumer is None
        or arguments.region is None
        or arguments.output is None
    ):
        _parser().error("--catalog, --consumer, --region, and --output are required")
    output = arguments.output
    unit = "unknown"
    consumer_marker = (
        arguments.consumer
        if re.fullmatch(r"[a-z0-9-]+", arguments.consumer) is not None
        else "invalid"
    )
    try:
        catalog = _catalog(arguments.catalog)
        unit = catalog["unit"]
        values = load_consumer(catalog, arguments.consumer, _aws_fetcher(arguments.region))
        write_env_file(
            output,
            values,
            owner=_parse_owner(arguments.owner),
            remove_after_validation=arguments.check_only,
        )
        if arguments.check_only:
            print(
                f"runtime_secret_loader=validated unit={unit} consumer={consumer_marker} output=removed"
            )
        else:
            print(f"runtime_secret_loader=ready unit={unit} consumer={consumer_marker}")
        return 0
    except LoaderError as exc:
        print(
            f"runtime_secret_loader=failed unit={unit} consumer={consumer_marker} reason={exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
