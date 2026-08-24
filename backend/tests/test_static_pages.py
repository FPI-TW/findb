"""Tests for the remaining backend static surfaces."""

from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

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
        ]

    assert all(response.status_code == 404 for response in responses)


@pytest.mark.asyncio
async def test_remaining_static_pages_and_skill_archive_remain_public():
    """Ensure /test, its direct asset, and the Skill archive remain public."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        test_page_response = await client.get("/test")
        static_test_page_response = await client.get("/static/test_page.html")
        archive_response = await client.get("/static/findb-api.skill")

    assert test_page_response.status_code == 200
    assert static_test_page_response.status_code == 200
    assert archive_response.status_code == 200
    with ZipFile(BytesIO(archive_response.content)) as archive:
        assert set(archive.namelist()) == {
            "findb-api/",
            "findb-api/SKILL.md",
            "findb-api/assets/",
            "findb-api/assets/sample_payload.json",
            "findb-api/references/",
            "findb-api/references/endpoints.md",
            "findb-api/references/ingest-payloads.md",
            "findb-api/references/responses.md",
        }
        skill_text = archive.read("findb-api/SKILL.md").decode("utf-8")
    assert "/dashboard/lookup" in skill_text
    assert "/instrument-lookup" not in skill_text
    assert "/skill-install" not in skill_text


def test_instrument_cache_path_is_gitignored():
    """Ensure generated cache files are not tracked."""
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "backend/app/static/data/instruments.json" in gitignore
    assert "backend/app/static/data/macro-series.json" in gitignore
