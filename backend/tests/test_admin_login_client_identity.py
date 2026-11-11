"""Login throttling keys on the nginx-verified client, not the Dashboard proxy (#492)."""

import re
from pathlib import Path

import pytest
from httpx import AsyncClient

NGINX_CONFIG = Path(__file__).resolve().parents[2] / "infra" / "nginx" / "nginx.conf"


async def _failed_logins(client: AsyncClient, forwarded_for: str, attempts: int) -> list[int]:
    return [
        (
            await client.post(
                "/api/v1/admin/auth/login",
                headers={"X-Forwarded-For": forwarded_for},
                json={"username": "shared-operator", "password": "wrong password"},
            )
        ).status_code
        for _ in range(attempts)
    ]


@pytest.mark.asyncio
async def test_trusted_forwarded_client_gets_its_own_login_bucket(client: AsyncClient, monkeypatch):
    from app.api.deps import settings

    monkeypatch.setattr(settings, "SOURCE_TRUST_PROXY_HEADERS", True)
    limit = settings.ADMIN_LOGIN_RATE_LIMIT_REQUESTS

    attacker = await _failed_logins(client, "203.0.113.10", limit + 1)
    assert attacker == [401] * limit + [429]
    # Another visitor relayed by the same Dashboard container is not locked out.
    assert await _failed_logins(client, "198.51.100.20", 1) == [401]


@pytest.mark.asyncio
async def test_untrusted_forwarding_collapses_every_visitor_onto_the_proxy(
    client: AsyncClient, monkeypatch
):
    from app.api.deps import settings

    # Documents why deployments must set SOURCE_TRUST_PROXY_HEADERS=true.
    monkeypatch.setattr(settings, "SOURCE_TRUST_PROXY_HEADERS", False)
    limit = settings.ADMIN_LOGIN_RATE_LIMIT_REQUESTS

    await _failed_logins(client, "203.0.113.10", limit)
    assert await _failed_logins(client, "198.51.100.20", 1) == [429]


@pytest.mark.parametrize("location", ["location ^~ /dashboard/ {", "location ^~ /api/v1/admin/ {"])
def test_nginx_overwrites_client_address_headers(location: str):
    config = NGINX_CONFIG.read_text(encoding="utf-8")
    block = config.split(location, 1)[1].split("\n    }\n", 1)[0]

    # Browser-supplied values never reach the Dashboard or ingest unchanged.
    assert re.search(r"proxy_set_header X-Real-IP \$remote_addr;", block)
    assert re.search(r"proxy_set_header X-Forwarded-For \$remote_addr;", block)
