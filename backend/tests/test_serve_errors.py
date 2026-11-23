"""Every Serve error carries a machine-readable code; other APIs keep their format."""

import pytest
from httpx import AsyncClient

from app.config import get_settings
from app.services.api_keys import create_api_key


async def _serve_key(test_session, **overrides) -> str:
    values = {
        "owner": "serve-errors-test",
        "tier": "standard",
        "scopes": ["serve"],
        "rate_limit_requests": 100,
        "rate_limit_window": 60,
        "page_size_limit": 1000,
        "kind": "serve",
        "name": "serve-errors-test",
        "role": None,
        "commit": False,
    }
    values.update(overrides)
    _, key = await create_api_key(test_session, **values)
    return key


def _detail(response) -> dict:
    detail = response.json()["detail"]
    assert isinstance(detail, dict), response.json()
    assert isinstance(detail["code"], str) and detail["code"]
    assert isinstance(detail["message"], str) and detail["message"]
    return detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "status", "code"),
    [
        ("/api/v1/serve/eod?start_date=not-a-date&market=TW", 422, "INVALID_PARAMETER"),
        ("/api/v1/serve/eod", 422, "QUERY_SCOPE_REQUIRED"),
        ("/api/v1/serve/instruments?page_size=0", 422, "INVALID_PARAMETER"),
        ("/api/v1/serve/instruments/not-a-uuid", 422, "INVALID_PARAMETER"),
        (
            "/api/v1/serve/calendar?market=TW&start_date=2000-01-01&end_date=2026-01-01",
            422,
            "INVALID_DATE_RANGE",
        ),
        ("/api/v1/serve/calendar?market=ZZ", 422, "INVALID_PARAMETER"),
        ("/api/v1/serve/no-such-route", 404, "NOT_FOUND"),
        (
            "/api/v1/serve/intraday/bars?instrument_id=11111111-1111-1111-1111-111111111111",
            401,
            "API_KEY_REQUIRED",
        ),
    ],
)
async def test_serve_errors_carry_a_code(client: AsyncClient, path, status, code):
    response = await client.get(path)

    assert response.status_code == status
    assert _detail(response)["code"] == code


@pytest.mark.asyncio
async def test_serve_validation_errors_name_each_bad_parameter(client: AsyncClient):
    response = await client.get("/api/v1/serve/eod?start_date=bad&page_size=0&market=TW")

    detail = _detail(response)
    assert detail["code"] == "INVALID_PARAMETER"
    errors = detail["details"]["errors"]
    assert {(error["location"], error["param"]) for error in errors} == {
        ("query", "start_date"),
        ("query", "page_size"),
    }
    assert all(error["reason"] for error in errors)
    assert "page_size" in detail["message"] and "start_date" in detail["message"]


@pytest.mark.asyncio
async def test_general_serve_rate_limit_reports_when_to_retry(
    client: AsyncClient, test_session, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "SERVE_REQUIRE_AUTH", True)
    key = await _serve_key(test_session, rate_limit_requests=1, rate_limit_window=30)
    headers = {settings.API_KEY_HEADER: key}

    assert (await client.get("/api/v1/serve/datasets", headers=headers)).status_code == 200
    limited = await client.get("/api/v1/serve/datasets", headers=headers)

    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "30"
    detail = _detail(limited)
    assert detail["code"] == "RATE_LIMITED"
    assert detail["details"] == {
        "bucket": "serve",
        "limit": 1,
        "window_seconds": 30,
        "retry_after_seconds": 30,
    }


@pytest.mark.asyncio
async def test_a_sent_key_is_limited_even_when_serve_auth_is_optional(
    client: AsyncClient, test_session, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "SERVE_REQUIRE_AUTH", False)
    key = await _serve_key(test_session, page_size_limit=10)

    assert (await client.get("/api/v1/serve/instruments?page_size=50")).status_code == 200
    limited = await client.get(
        "/api/v1/serve/instruments?page_size=50", headers={settings.API_KEY_HEADER: key}
    )
    assert limited.status_code == 400
    assert _detail(limited)["code"] == "PAGE_SIZE_EXCEEDS_KEY_LIMIT"


@pytest.mark.asyncio
async def test_serve_key_without_the_serve_scope_is_refused_with_a_code(
    client: AsyncClient, test_session, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "SERVE_REQUIRE_AUTH", True)
    key = await _serve_key(test_session, scopes=[])

    response = await client.get("/api/v1/serve/datasets", headers={settings.API_KEY_HEADER: key})

    assert response.status_code == 403
    detail = _detail(response)
    assert detail["code"] == "SCOPE_REQUIRED"
    assert detail["details"] == {"scope": "serve"}


@pytest.mark.asyncio
async def test_other_apis_keep_fastapi_error_formats(client: AsyncClient):
    invalid = await client.post("/api/v1/admin/auth/login", json={})
    assert invalid.status_code == 422
    assert isinstance(invalid.json()["detail"], list)

    missing = await client.get("/api/v1/admin/no-such-route")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Not Found"}
