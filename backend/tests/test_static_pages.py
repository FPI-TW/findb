"""
Tests for static HTML pages.
"""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent


@pytest.mark.asyncio
async def test_instrument_lookup_page_is_served():
    """Ensure the legacy lookup page remains publicly available."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        static_response = await client.get("/static/instrument-lookup.html")
        page_response = await client.get("/instrument-lookup")

    assert static_response.status_code == 200
    assert page_response.status_code == 200
    assert "標的與宏觀查詢" in page_response.text
    assert "/static/data/instruments.json" in page_response.text
    assert "/static/data/macro-series.json" in page_response.text


@pytest.mark.asyncio
async def test_skill_install_page_and_archive_remain_public():
    """Ensure the legacy Skill page and downloadable archive remain public."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        page_response = await client.get("/skill-install")
        archive_response = await client.get("/static/findb-api.skill")

    assert page_response.status_code == 200
    assert "Skill 安裝教學" in page_response.text
    assert "/static/findb-api.skill" in page_response.text
    assert archive_response.status_code == 200


def test_instrument_cache_path_is_gitignored():
    """Ensure generated cache files are not tracked."""
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "backend/app/static/data/instruments.json" in gitignore
    assert "backend/app/static/data/macro-series.json" in gitignore
