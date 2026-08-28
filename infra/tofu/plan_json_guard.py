#!/usr/bin/env python3
"""Fail-closed validation for an OpenTofu ``show -json`` plan.

The guard deliberately does not deserialize or print plan values. By default
it rejects every delete. A narrowly bounded CLI allowlist can authorize only
the two legacy GHCR metadata retirements as pure resource changes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, NoReturn

MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_REPORTED_COUNT = 1_000_000
CORE_KEYS = ("format_version", "terraform_version", "planned_values", "configuration")
REQUIRED_COLLECTION_KEYS = ("resource_changes", "resource_drift", "output_changes")
ALLOWED_RETIREMENT_DELETE_ADDRESSES = frozenset(
    {
        'aws_secretsmanager_secret.runtime["findb/registry/ghcr-pull"]',
        'aws_secretsmanager_secret.runtime["fetcher/registry/ghcr-pull"]',
    }
)


class GuardError(Exception):
    """Expected malformed-plan or delete-action failure."""


class DeleteError(GuardError):
    """A delete falls outside the narrow retirement exception."""


class DuplicateKeyError(ValueError):
    """Raised when a JSON object repeats a key."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError
        result[key] = value
    return result


def _reject_non_json_constant(_: str) -> NoReturn:
    raise ValueError


def _fail() -> NoReturn:
    # Do not include a JSON path or value in an error. Plan details must never
    # be reflected in CI logs, including when a malformed fixture is supplied.
    raise GuardError


def _reject_delete() -> NoReturn:
    raise DeleteError


def _bounded_count(value: int) -> int:
    return min(value, MAX_REPORTED_COUNT)


class Counts:
    def __init__(self) -> None:
        self.action_lists = 0
        self.delete_actions = 0


def _record_actions(value: Any, counts: Counts) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _fail()
    counts.action_lists = _bounded_count(counts.action_lists + 1)
    counts.delete_actions = _bounded_count(
        counts.delete_actions + sum(item == "delete" for item in value)
    )
    return value


def _walk_future_actions(value: Any, counts: Counts, reject_deletes: bool) -> None:
    """Inspect unknown action-bearing plan sections without reading values."""

    if isinstance(value, dict):
        for key, child in value.items():
            if key == "actions":
                actions = _record_actions(child, counts)
                if reject_deletes and "delete" in actions:
                    _reject_delete()
            else:
                _walk_future_actions(child, counts, reject_deletes)
    elif isinstance(value, list):
        for child in value:
            _walk_future_actions(child, counts, reject_deletes)


def _validate_shape(
    plan: Any,
    allowed_delete_addresses: frozenset[str],
) -> tuple[dict[str, Any], Counts]:
    if not isinstance(plan, dict):
        _fail()
    for key in CORE_KEYS:
        if key not in plan:
            _fail()
    for key in REQUIRED_COLLECTION_KEYS:
        if key not in plan:
            _fail()

    if not isinstance(plan["format_version"], str) or not plan["format_version"]:
        _fail()
    if not isinstance(plan["terraform_version"], str) or not plan["terraform_version"]:
        _fail()
    for key in ("planned_values", "configuration"):
        if not isinstance(plan[key], dict):
            _fail()
        root_module = plan[key].get("root_module")
        if not isinstance(root_module, dict):
            _fail()

    counts = Counts()
    retirement_mode = bool(allowed_delete_addresses)
    resource_changes = plan["resource_changes"]
    if not isinstance(resource_changes, list):
        _fail()
    seen_delete_addresses: set[str] = set()
    for item in resource_changes:
        if not isinstance(item, dict):
            _fail()
        change = item.get("change")
        if not isinstance(change, dict) or "actions" not in change:
            _fail()
        actions = _record_actions(change["actions"], counts)
        if not retirement_mode or "delete" not in actions:
            continue
        if actions != ["delete"]:
            _reject_delete()
        if "previous_address" in item or "deposed" in item:
            _reject_delete()
        address = item.get("address")
        if (
            not isinstance(address, str)
            or address not in allowed_delete_addresses
            or address in seen_delete_addresses
        ):
            _reject_delete()
        seen_delete_addresses.add(address)

    if (
        retirement_mode
        and seen_delete_addresses
        and seen_delete_addresses != allowed_delete_addresses
    ):
        _reject_delete()

    resource_drift = plan["resource_drift"]
    if not isinstance(resource_drift, list):
        _fail()
    for item in resource_drift:
        if not isinstance(item, dict):
            _fail()
        change = item.get("change")
        if not isinstance(change, dict) or "actions" not in change:
            _fail()
        actions = _record_actions(change["actions"], counts)
        if retirement_mode and "delete" in actions:
            _reject_delete()

    output_changes = plan["output_changes"]
    if not isinstance(output_changes, dict):
        _fail()
    for item in output_changes.values():
        if not isinstance(item, dict) or "actions" not in item:
            _fail()
        actions = _record_actions(item["actions"], counts)
        if retirement_mode and "delete" in actions:
            _reject_delete()

    for key in ("checks", "deferred_changes", "relevant_attributes"):
        if key not in plan:
            continue
        collection = plan[key]
        if not isinstance(collection, list) or not all(
            isinstance(item, dict) for item in collection
        ):
            _fail()

    # Deferred changes and future top-level plan sections may introduce nested
    # change representations. Exclude configuration/state/value trees, where
    # a provider attribute can legitimately be named "actions".
    if "deferred_changes" in plan:
        _walk_future_actions(plan["deferred_changes"], counts, retirement_mode)
    known_top_level = {
        "checks",
        "configuration",
        "deferred_changes",
        "errored",
        "format_version",
        "output_changes",
        "planned_values",
        "prior_state",
        "relevant_attributes",
        "resource_changes",
        "resource_drift",
        "terraform_version",
        "timestamp",
        "variables",
    }
    for key, value in plan.items():
        if key not in known_top_level:
            _walk_future_actions(value, counts, retirement_mode)

    return plan, counts


def _parse_args(argv: list[str]) -> tuple[str, frozenset[str]]:
    allowed_addresses: set[str] = set()
    index = 1
    while index < len(argv) - 1:
        if argv[index] != "--allow-delete-address" or index + 1 >= len(argv) - 1:
            raise ValueError
        address = argv[index + 1]
        if address in allowed_addresses or address not in ALLOWED_RETIREMENT_DELETE_ADDRESSES:
            raise ValueError
        allowed_addresses.add(address)
        index += 2
    if index != len(argv) - 1:
        raise ValueError
    allowed = frozenset(allowed_addresses)
    if allowed and allowed != ALLOWED_RETIREMENT_DELETE_ADDRESSES:
        raise ValueError
    return argv[index], allowed


def _load_plan(path_arg: str) -> Any:
    if path_arg == "-":
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    else:
        raw = Path(path_arg).read_bytes()
    if len(raw) > MAX_INPUT_BYTES:
        _fail()
    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_non_json_constant,
    )


def _summary(plan: dict[str, Any], counts: Counts) -> str:
    resource_changes = plan.get("resource_changes", [])
    resource_drift = plan.get("resource_drift", [])
    output_changes = plan.get("output_changes", {})
    deferred_changes = plan.get("deferred_changes", [])
    return (
        "plan_guard=pass"
        f" action_lists={counts.action_lists}"
        f" delete_actions={counts.delete_actions}"
        f" resource_changes={_bounded_count(len(resource_changes))}"
        f" resource_drift={_bounded_count(len(resource_drift))}"
        f" output_changes={_bounded_count(len(output_changes))}"
        f" deferred_changes={_bounded_count(len(deferred_changes))}"
    )


def main(argv: list[str]) -> int:
    try:
        path_arg, allowed_delete_addresses = _parse_args(argv)
    except ValueError:
        print("plan_guard=reject reason=usage", file=sys.stderr)
        return 2

    try:
        plan, counts = _validate_shape(_load_plan(path_arg), allowed_delete_addresses)
        if not allowed_delete_addresses and counts.delete_actions:
            print(
                "plan_guard=reject reason=delete"
                f" action_lists={counts.action_lists}"
                f" delete_actions={counts.delete_actions}",
                file=sys.stderr,
            )
            return 1
        print(_summary(plan, counts))
        return 0
    except DeleteError:
        print("plan_guard=reject reason=delete", file=sys.stderr)
        return 1
    except GuardError:
        print("plan_guard=reject reason=malformed", file=sys.stderr)
        return 1
    except Exception:
        # Unexpected parser/runtime errors are also a rejection. Never expose
        # exception text because it could contain decoded plan values.
        print("plan_guard=reject reason=malformed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
