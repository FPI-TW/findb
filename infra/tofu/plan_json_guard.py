#!/usr/bin/env python3
"""Fail-closed validation for an OpenTofu ``show -json`` plan.

The guard deliberately does not deserialize or print plan values. It validates
the small structural contract needed by the CI delete gate, then recursively
checks every JSON member named ``actions`` so future plan sections cannot
silently bypass the gate.
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

# These are collection-valued members in the OpenTofu plan JSON contract. The
# walk is recursive because new/future sections may contain the same members.
COLLECTION_TYPES: dict[str, type[Any]] = {
    "checks": list,
    "child_modules": list,
    "configuration": dict,
    "deferred_changes": list,
    "module_calls": dict,
    "output_changes": dict,
    "outputs": dict,
    "planned_values": dict,
    "prior_state": dict,
    "relevant_attributes": list,
    "resource_changes": list,
    "resource_drift": list,
    "resources": list,
    "root_module": dict,
    "variables": dict,
}
OBJECT_LIST_KEYS = {
    "checks",
    "child_modules",
    "deferred_changes",
    "relevant_attributes",
    "resource_changes",
    "resource_drift",
    "resources",
}
ACTION_COLLECTION_KEYS = {"resource_changes", "resource_drift", "deferred_changes"}


class GuardError(Exception):
    """Expected malformed-plan or delete-action failure."""


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


def _bounded_count(value: int) -> int:
    return min(value, MAX_REPORTED_COUNT)


class Counts:
    action_lists = 0
    delete_actions = 0


def _walk(value: Any, counts: Counts) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            expected_type = COLLECTION_TYPES.get(key)
            if expected_type is not None and not isinstance(child, expected_type):
                _fail()

            if key == "actions":
                if not isinstance(child, list) or not all(isinstance(item, str) for item in child):
                    _fail()
                counts.action_lists = _bounded_count(counts.action_lists + 1)
                counts.delete_actions = _bounded_count(
                    counts.delete_actions + sum(item == "delete" for item in child)
                )

            if key in OBJECT_LIST_KEYS:
                if not isinstance(child, list) or not all(isinstance(item, dict) for item in child):
                    _fail()

            if key in ACTION_COLLECTION_KEYS:
                for item in child:
                    change = item.get("change")
                    if not isinstance(change, dict) or "actions" not in change:
                        _fail()

            if key == "output_changes":
                for item in child.values():
                    if not isinstance(item, dict) or "actions" not in item:
                        _fail()

            _walk(child, counts)
    elif isinstance(value, list):
        for child in value:
            _walk(child, counts)


def _validate_shape(plan: Any) -> dict[str, Any]:
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

    _walk(plan, Counts())
    return plan


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


def _summary(plan: dict[str, Any]) -> str:
    counts = Counts()
    _walk(plan, counts)
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
    if len(argv) != 2:
        print("plan_guard=reject reason=usage", file=sys.stderr)
        return 2

    try:
        plan = _validate_shape(_load_plan(argv[1]))
        counts = Counts()
        _walk(plan, counts)
        if counts.delete_actions:
            print(
                "plan_guard=reject reason=delete"
                f" action_lists={counts.action_lists}"
                f" delete_actions={counts.delete_actions}",
                file=sys.stderr,
            )
            return 1
        print(_summary(plan))
        return 0
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
