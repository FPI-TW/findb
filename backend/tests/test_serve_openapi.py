"""The Serve OpenAPI document is complete enough to generate LLM tools from.

Clients turn each Serve operation into a tool: the operation id becomes the tool
name, and the summaries and descriptions are all the agent knows. These tests
fail when a new route, parameter or response field ships without them.
"""

from collections.abc import Iterator
from typing import Any

import pytest

from app.main import app

SERVE_PREFIX = "/api/v1/serve"
ERROR_SCHEMA = "#/components/schemas/ServeErrorResponse"


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return app.openapi()


def _operations(spec: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    for path, item in spec["paths"].items():
        if path.startswith(SERVE_PREFIX):
            for method, operation in item.items():
                yield path, method, operation


def _refs(schema: Any) -> Iterator[str]:
    if isinstance(schema, dict):
        if "$ref" in schema:
            yield schema["$ref"]
        for value in schema.values():
            yield from _refs(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from _refs(value)


def _reachable_schemas(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    components = spec["components"]["schemas"]
    pending = [
        ref
        for _, _, operation in _operations(spec)
        for response in operation["responses"].values()
        for ref in _refs(response)
    ]
    seen: dict[str, dict[str, Any]] = {}
    while pending:
        name = pending.pop().rsplit("/", 1)[-1]
        if name in seen:
            continue
        seen[name] = components[name]
        pending.extend(_refs(components[name]))
    return seen


def test_serve_operations_have_stable_ids_summaries_and_descriptions(spec):
    ids = []
    for path, method, operation in _operations(spec):
        operation_id = operation.get("operationId", "")
        assert operation_id and "api_v1" not in operation_id, (path, method, operation_id)
        assert operation.get("summary"), (path, method)
        assert len(operation.get("description", "")) >= 40, (path, method)
        ids.append(operation_id)
    assert len(ids) == len(set(ids))
    assert {
        "list_datasets",
        "list_instruments",
        "get_instrument",
        "resolve_instruments",
        "list_eod",
        "get_latest_eod",
        "list_minute_bars",
        "list_intraday_bars",
        "list_intraday_ticks",
        "get_intraday_download_manifest",
        "list_futures_eod",
        "get_calendar_year",
        "list_calendar_days",
        "list_market_freshness",
    } <= set(ids)


def test_every_serve_parameter_is_described(spec):
    for path, method, operation in _operations(spec):
        for parameter in operation.get("parameters", []):
            assert parameter.get("description"), (path, method, parameter["name"])


def test_serve_errors_are_documented_with_the_coded_error_body(spec):
    for path, method, operation in _operations(spec):
        responses = operation["responses"]
        assert {"401", "422", "429"} <= set(responses), (path, method)
        for status, response in responses.items():
            if int(status) < 400:
                continue
            assert response.get("description"), (path, method, status)
            schema = response["content"]["application/json"]["schema"]
            assert schema == {"$ref": ERROR_SCHEMA}, (path, method, status)


def test_every_serve_response_field_is_described(spec):
    schemas = _reachable_schemas(spec)
    assert "ServeErrorResponse" in schemas and "EODResponse" in schemas
    assert "HTTPValidationError" not in schemas
    missing = [
        f"{name}.{field}"
        for name, schema in schemas.items()
        for field, value in schema.get("properties", {}).items()
        if not value.get("description")
    ]
    assert missing == []
