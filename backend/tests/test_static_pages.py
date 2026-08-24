"""Tests for removed backend public/static surfaces."""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent


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


def test_instrument_cache_path_is_gitignored():
    """Ensure generated cache files are not tracked."""
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "backend/app/static/data/instruments.json" in gitignore
    assert "backend/app/static/data/macro-series.json" in gitignore
