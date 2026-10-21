"""Every protected Admin and Source route carries its family's auth dependency."""

import pytest
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from httpx import AsyncClient

from app.api.deps import verify_admin_api_key, verify_source_api_key
from app.config import get_settings
from app.main import app

# Login and first-owner bootstrap are how an Admin credential is obtained.
ADMIN_UNAUTHENTICATED_PATHS = {"/api/v1/admin/auth/login", "/api/v1/admin/auth/bootstrap"}


def _dependency_calls(dependant: Dependant) -> set:
    calls = {dependant.call}
    for dependency in dependant.dependencies:
        calls |= _dependency_calls(dependency)
    return calls


@pytest.mark.parametrize(
    ("prefix", "dependency", "exempt"),
    [
        ("/api/v1/admin/", verify_admin_api_key, ADMIN_UNAUTHENTICATED_PATHS),
        ("/api/v1/source/", verify_source_api_key, set()),
    ],
)
def test_every_protected_route_depends_on_family_auth(prefix, dependency, exempt) -> None:
    routes = [
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith(prefix)
    ]
    assert routes
    assert exempt <= {route.path for route in routes}
    unprotected = {
        f"{sorted(route.methods)} {route.path}"
        for route in routes
        if route.path not in exempt and dependency not in _dependency_calls(route.dependant)
    }
    assert unprotected == set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/api/v1/admin/corrections", "/api/v1/source/datasets"], ids=["admin", "source"]
)
async def test_missing_or_invalid_key_is_rejected(
    client: AsyncClient, admin_headers: dict, source_headers: dict, path: str
) -> None:
    assert (await client.get(path)).status_code == 401
    invalid = {get_settings().API_KEY_HEADER: "wrong-key"}
    assert (await client.get(path, headers=invalid)).status_code == 403
