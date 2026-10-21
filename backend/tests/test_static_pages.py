"""Tests for removed backend public/static surfaces."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.asyncio
async def test_legacy_public_pages_and_html_assets_are_removed():
    """Removed public pages and their direct static assets must return 404."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        responses = [
            await client.get("/instrument-lookup"),
            await client.get("/skill-install"),
            await client.get("/static/instrument-lookup.html"),
            await client.get("/static/skill-install.html"),
            await client.get("/test"),
            await client.get("/static/test_page.html"),
            await client.get("/static/findb-api.skill"),
        ]

    assert all(response.status_code == 404 for response in responses)
