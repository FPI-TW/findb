"""Whole-market metadata must cross ingress without relaxing ingestion limits."""

import json

import pytest

from app.config import get_settings
from tests.test_full_market import _dataset, _source_headers, _universe


@pytest.mark.parametrize("environment", ["staging", "production"])
async def test_large_universe_keeps_auth_scope_and_persists_members(
    client, test_session, monkeypatch, environment
):
    monkeypatch.setattr(get_settings(), "APP_ENVIRONMENT", environment)
    await _dataset(test_session, "us_equity_eod")
    body = _universe("us_equity_eod", count=3000).model_dump(mode="json")
    for member in body["members"]:
        member["raw_evidence_ref"] = "https://example.com/" + "a" * 384
    encoded = json.dumps(body).encode()
    assert 1024 * 1024 < len(encoded) < 16 * 1024 * 1024
    headers = {"Content-Type": "application/json"}
    unauthenticated = await client.post(
        "/api/v1/source/universes", content=encoded, headers=headers
    )
    assert unauthenticated.status_code == 401
    wrong_scope = await _source_headers(test_session, "finlab", ["tw_equity_eod"])
    denied = await client.post(
        "/api/v1/source/universes", content=encoded, headers={**headers, **wrong_scope}
    )
    assert denied.status_code == 403
    authorized = await _source_headers(test_session, "twelve_data", ["us_equity_eod"])
    accepted = await client.post(
        "/api/v1/source/universes?native=1", content=encoded, headers={**headers, **authorized}
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["member_count"] == 3000
    assert accepted.json()["status"] == "candidate"
    body["members"][0]["symbol"] = "invalid symbol"
    invalid = await client.post("/api/v1/source/universes", json=body, headers=authorized)
    assert invalid.status_code == 422


@pytest.mark.parametrize(
    ("method", "path", "length", "limit"),
    [
        ("POST", "/api/v1/source/universes", 16 * 1024 * 1024 + 1, 16 * 1024 * 1024),
        ("POST", "/api/v1/source/ingest", 1001, 1000),
        ("POST", "/api/v1/source/universes/", 1001, 1000),
        ("POST", "/api/v1/source/universes/anything", 1001, 1000),
        ("GET", "/api/v1/source/universes", 1001, 1000),
    ],
)
async def test_source_body_caps_reject_before_parsing(
    client, monkeypatch, method, path, length, limit
):
    monkeypatch.setattr(get_settings(), "SOURCE_MAX_PAYLOAD_BYTES", 1000)
    response = await client.request(
        method, path, content=b"invalid-json", headers={"Content-Length": str(length)}
    )
    assert response.status_code == 413
    assert response.json() == {"detail": f"Payload exceeds maximum size of {limit} bytes"}
