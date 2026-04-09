"""
Tests for static HTML pages.
"""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.asyncio
async def test_instrument_lookup_page_is_served():
    """Ensure the static instrument lookup page is available."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/static/instrument-lookup.html")

    assert response.status_code == 200
    assert "FinDB" in response.text
    assert "標的查詢" in response.text


def test_instrument_cache_path_is_gitignored():
    """Ensure generated cache files are not tracked."""
    gitignore = Path(".gitignore").read_text(encoding="utf-8")
    assert "app/static/data/instruments.json" in gitignore
