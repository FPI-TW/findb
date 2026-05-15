"""
Tests for Admin API endpoints.
"""

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.main import app
from app.models.canonical import Instrument, MarketDataEOD
from app.models.correction import CanonicalCorrection
from app.models.registry import DQIssue
from app.services import instrument_cache as instrument_cache_service
from app.utils import utc_now, uuid7

# ── Helpers ────────────────────────────────────────────────────────────────────


async def _create_instrument(session: AsyncSession) -> Instrument:
    instrument = Instrument(
        instrument_id=uuid7(),
        asset_class="equity",
        market="US",
        symbol="AAPL",
        status="active",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    session.add(instrument)
    await session.flush()
    return instrument


async def _create_eod(session: AsyncSession, instrument_id: UUID) -> MarketDataEOD:
    eod = MarketDataEOD(
        id=uuid7(),
        instrument_id=instrument_id,
        trade_date=date(2025, 1, 2),
        open=Decimal("150.00"),
        high=Decimal("155.00"),
        low=Decimal("149.00"),
        close=Decimal("153.00"),
        volume=1000000,
        turnover=Decimal("153000000.00"),
        asof_ts=utc_now(),
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    session.add(eod)
    await session.flush()
    return eod


async def _create_dq_issue(
    session: AsyncSession,
    instrument_id: UUID | None = None,
    resolved: bool = False,
) -> DQIssue:
    issue = DQIssue(
        id=uuid7(),
        instrument_id=instrument_id,
        issue_type="MISSING_OHLC",
        severity="warning",
        description="Missing OHLC fields: high",
        resolved=resolved,
        resolved_at=utc_now() if resolved else None,
        created_at=utc_now(),
    )
    session.add(issue)
    await session.flush()
    return issue


def _sample_instrument_cache() -> dict:
    return {
        "generated_at": "2026-04-09T07:08:40.626344Z",
        "total": 2,
        "markets": ["US", "HK"],
        "asset_classes": ["equity"],
        "data": [
            {
                "instrument_id": "instrument-us-aapl",
                "market": "US",
                "asset_class": "equity",
                "symbol": "AAPL",
                "name": None,
                "short_name": "Apple",
                "currency": "USD",
                "status": "active",
            },
            {
                "instrument_id": "instrument-hk-0700",
                "market": "HK",
                "asset_class": "equity",
                "symbol": "0700",
                "name": "Tencent",
                "short_name": "Tencent",
                "currency": "HKD",
                "status": "active",
            },
        ],
    }


def _write_instrument_cache(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _admin_api_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ── Auth Tests ─────────────────────────────────────────────────────────────────


class TestAdminAuth:
    @pytest.mark.asyncio
    async def test_patch_eod_without_api_key_returns_401(
        self, client: AsyncClient, test_session: AsyncSession
    ):
        instrument = await _create_instrument(test_session)
        await _create_eod(test_session, instrument.instrument_id)
        await test_session.commit()

        response = await client.patch(
            f"/api/v1/admin/eod/{instrument.instrument_id}/2025-01-02",
            json={"correction_reason": "test", "close": "152.00"},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_patch_eod_with_invalid_api_key_returns_403(
        self, client: AsyncClient, test_session: AsyncSession
    ):
        instrument = await _create_instrument(test_session)
        await _create_eod(test_session, instrument.instrument_id)
        await test_session.commit()

        settings = get_settings()
        response = await client.patch(
            f"/api/v1/admin/eod/{instrument.instrument_id}/2025-01-02",
            headers={settings.API_KEY_HEADER: "wrong-key"},
            json={"correction_reason": "test", "close": "152.00"},
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_resolve_dq_without_api_key_returns_401(
        self, client: AsyncClient, test_session: AsyncSession
    ):
        issue = await _create_dq_issue(test_session)
        await test_session.commit()

        response = await client.patch(
            f"/api/v1/admin/dq-issues/{issue.id}/resolve",
            json={"correction_reason": "test"},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_list_corrections_without_api_key_returns_401(self, client: AsyncClient):
        response = await client.get("/api/v1/admin/corrections")
        assert response.status_code == 401


# ── Instrument Cache Tests ────────────────────────────────────────────────────


class TestInstrumentCacheAdmin:
    @pytest.fixture
    def cache_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "static" / "data" / "instruments.json"
        monkeypatch.setattr(instrument_cache_service, "INSTRUMENT_CACHE_PATH", path)
        return path

    @pytest.mark.asyncio
    async def test_get_instrument_cache_without_api_key_returns_401(
        self,
        cache_path: Path,
    ):
        _write_instrument_cache(cache_path, _sample_instrument_cache())

        async with _admin_api_client() as client:
            response = await client.get("/api/v1/admin/instrument-cache")

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_get_instrument_cache_returns_generated_json(
        self,
        cache_path: Path,
        admin_headers: dict,
    ):
        _write_instrument_cache(cache_path, _sample_instrument_cache())

        async with _admin_api_client() as client:
            response = await client.get(
                "/api/v1/admin/instrument-cache",
                headers=admin_headers,
            )

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        assert data["data"][0]["instrument_id"] == "instrument-us-aapl"
        assert data["data"][0]["short_name"] == "Apple"

    @pytest.mark.asyncio
    async def test_get_instrument_cache_missing_file_returns_404(
        self,
        cache_path: Path,
        admin_headers: dict,
    ):
        async with _admin_api_client() as client:
            response = await client.get(
                "/api/v1/admin/instrument-cache",
                headers=admin_headers,
            )

        assert response.status_code == 404
        assert str(cache_path) in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_put_instrument_cache_replaces_and_normalizes_json(
        self,
        cache_path: Path,
        admin_headers: dict,
    ):
        payload = _sample_instrument_cache()
        payload["total"] = 99
        payload["markets"] = ["WRONG"]

        async with _admin_api_client() as client:
            response = await client.put(
                "/api/v1/admin/instrument-cache",
                headers=admin_headers,
                json=payload,
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 2
        assert data["markets"] == ["HK", "US"]

        stored = json.loads(cache_path.read_text(encoding="utf-8"))
        assert stored["total"] == 2
        assert stored["markets"] == ["HK", "US"]
        assert stored["data"][0]["instrument_id"] == "instrument-hk-0700"
        assert stored["data"][0]["short_name"] == "Tencent"

    @pytest.mark.asyncio
    async def test_patch_instrument_cache_item_updates_json(
        self,
        cache_path: Path,
        admin_headers: dict,
    ):
        _write_instrument_cache(cache_path, _sample_instrument_cache())

        async with _admin_api_client() as client:
            response = await client.patch(
                "/api/v1/admin/instrument-cache/items/instrument-us-aapl",
                headers=admin_headers,
                json={"name": "Apple Inc.", "status": "inactive"},
            )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["name"] == "Apple Inc."
        assert data["status"] == "inactive"

        stored = json.loads(cache_path.read_text(encoding="utf-8"))
        stored_item = next(
            item for item in stored["data"] if item["instrument_id"] == "instrument-us-aapl"
        )
        assert stored_item["name"] == "Apple Inc."
        assert stored_item["status"] == "inactive"
        assert stored_item["short_name"] == "Apple"

    @pytest.mark.asyncio
    async def test_patch_instrument_cache_item_not_found_returns_404(
        self,
        cache_path: Path,
        admin_headers: dict,
    ):
        _write_instrument_cache(cache_path, _sample_instrument_cache())

        async with _admin_api_client() as client:
            response = await client.patch(
                "/api/v1/admin/instrument-cache/items/missing-instrument",
                headers=admin_headers,
                json={"name": "Missing"},
            )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_patch_instrument_cache_item_without_fields_returns_400(
        self,
        cache_path: Path,
        admin_headers: dict,
    ):
        _write_instrument_cache(cache_path, _sample_instrument_cache())

        async with _admin_api_client() as client:
            response = await client.patch(
                "/api/v1/admin/instrument-cache/items/instrument-us-aapl",
                headers=admin_headers,
                json={},
            )

        assert response.status_code == 400
        assert "No instrument fields" in response.json()["detail"]


# ── Patch EOD Tests ────────────────────────────────────────────────────────────


class TestPatchEOD:
    @pytest.mark.asyncio
    async def test_patch_eod_record_not_found_returns_404(
        self, client: AsyncClient, admin_headers: dict
    ):
        fake_id = uuid7()
        response = await client.patch(
            f"/api/v1/admin/eod/{fake_id}/2025-01-02",
            headers=admin_headers,
            json={"correction_reason": "test", "close": "152.00"},
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_patch_eod_single_field_close(
        self, client: AsyncClient, test_session: AsyncSession, admin_headers: dict
    ):
        instrument = await _create_instrument(test_session)
        eod = await _create_eod(test_session, instrument.instrument_id)
        await test_session.commit()

        response = await client.patch(
            f"/api/v1/admin/eod/{instrument.instrument_id}/2025-01-02",
            headers=admin_headers,
            json={"correction_reason": "Wrong close from vendor", "close": "152.50"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "correction_id" in data
        assert data["trade_date"] == "2025-01-02"

        await test_session.refresh(eod)
        assert eod.close == Decimal("152.50")

    @pytest.mark.asyncio
    async def test_patch_eod_multiple_fields(
        self, client: AsyncClient, test_session: AsyncSession, admin_headers: dict
    ):
        instrument = await _create_instrument(test_session)
        eod = await _create_eod(test_session, instrument.instrument_id)
        await test_session.commit()

        response = await client.patch(
            f"/api/v1/admin/eod/{instrument.instrument_id}/2025-01-02",
            headers=admin_headers,
            json={
                "correction_reason": "Correcting OHLC",
                "open": "148.00",
                "high": "156.00",
                "low": "147.00",
                "close": "154.00",
            },
        )
        assert response.status_code == 200

        await test_session.refresh(eod)
        assert eod.open == Decimal("148.00")
        assert eod.high == Decimal("156.00")
        assert eod.low == Decimal("147.00")
        assert eod.close == Decimal("154.00")

    @pytest.mark.asyncio
    async def test_patch_eod_no_ohlcv_fields_returns_400(
        self, client: AsyncClient, test_session: AsyncSession, admin_headers: dict
    ):
        instrument = await _create_instrument(test_session)
        await _create_eod(test_session, instrument.instrument_id)
        await test_session.commit()

        response = await client.patch(
            f"/api/v1/admin/eod/{instrument.instrument_id}/2025-01-02",
            headers=admin_headers,
            json={"correction_reason": "Nothing to change"},
        )
        assert response.status_code == 400
        assert "No OHLCV fields" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_patch_eod_identical_value_returns_400(
        self, client: AsyncClient, test_session: AsyncSession, admin_headers: dict
    ):
        instrument = await _create_instrument(test_session)
        await _create_eod(test_session, instrument.instrument_id)
        await test_session.commit()

        response = await client.patch(
            f"/api/v1/admin/eod/{instrument.instrument_id}/2025-01-02",
            headers=admin_headers,
            json={"correction_reason": "Same value", "close": "153.00"},
        )
        assert response.status_code == 400
        assert "No changes" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_patch_eod_creates_correction_audit_row(
        self, client: AsyncClient, test_session: AsyncSession, admin_headers: dict
    ):
        from sqlalchemy import select

        instrument = await _create_instrument(test_session)
        await _create_eod(test_session, instrument.instrument_id)
        await test_session.commit()

        response = await client.patch(
            f"/api/v1/admin/eod/{instrument.instrument_id}/2025-01-02",
            headers=admin_headers,
            json={"correction_reason": "Audit trail test", "close": "152.50"},
        )
        assert response.status_code == 200
        correction_id = response.json()["correction_id"]

        result = await test_session.execute(
            select(CanonicalCorrection).where(CanonicalCorrection.id == UUID(correction_id))
        )
        correction = result.scalar_one_or_none()
        assert correction is not None
        assert correction.table_name == "market_data_eod"
        assert correction.correction_reason == "Audit trail test"

    @pytest.mark.asyncio
    async def test_patch_eod_before_after_snapshot(
        self, client: AsyncClient, test_session: AsyncSession, admin_headers: dict
    ):
        from sqlalchemy import select

        instrument = await _create_instrument(test_session)
        await _create_eod(test_session, instrument.instrument_id)
        await test_session.commit()

        response = await client.patch(
            f"/api/v1/admin/eod/{instrument.instrument_id}/2025-01-02",
            headers=admin_headers,
            json={"correction_reason": "Snapshot test", "close": "152.50"},
        )
        assert response.status_code == 200
        correction_id = response.json()["correction_id"]

        result = await test_session.execute(
            select(CanonicalCorrection).where(CanonicalCorrection.id == UUID(correction_id))
        )
        correction = result.scalar_one()
        assert Decimal(correction.before_snapshot["close"]) == Decimal("153.00")
        assert Decimal(correction.after_snapshot["close"]) == Decimal("152.50")

    @pytest.mark.asyncio
    async def test_patch_eod_null_field_clears_value(
        self, client: AsyncClient, test_session: AsyncSession, admin_headers: dict
    ):
        instrument = await _create_instrument(test_session)
        eod = await _create_eod(test_session, instrument.instrument_id)
        await test_session.commit()

        response = await client.patch(
            f"/api/v1/admin/eod/{instrument.instrument_id}/2025-01-02",
            headers=admin_headers,
            json={"correction_reason": "Clear turnover", "turnover": None},
        )
        assert response.status_code == 200

        await test_session.refresh(eod)
        assert eod.turnover is None

    @pytest.mark.asyncio
    async def test_patch_eod_total_ticks_update_and_clear(
        self, client: AsyncClient, test_session: AsyncSession, admin_headers: dict
    ):
        from sqlalchemy import select

        instrument = await _create_instrument(test_session)
        eod = await _create_eod(test_session, instrument.instrument_id)
        eod.total_ticks = 221
        await test_session.commit()

        response = await client.patch(
            f"/api/v1/admin/eod/{instrument.instrument_id}/2025-01-02",
            headers=admin_headers,
            json={
                "correction_reason": "Correct TW FinLab tick count",
                "total_ticks": None,
            },
        )
        assert response.status_code == 200

        await test_session.refresh(eod)
        assert eod.total_ticks is None

        correction_id = response.json()["correction_id"]
        result = await test_session.execute(
            select(CanonicalCorrection).where(CanonicalCorrection.id == UUID(correction_id))
        )
        correction = result.scalar_one()
        assert correction.before_snapshot["total_ticks"] == 221
        assert correction.after_snapshot["total_ticks"] is None


# ── Resolve DQ Issue Tests ────────────────────────────────────────────────────


class TestResolveDQIssue:
    @pytest.mark.asyncio
    async def test_resolve_dq_issue_not_found_returns_404(
        self, client: AsyncClient, admin_headers: dict
    ):
        fake_id = uuid7()
        response = await client.patch(
            f"/api/v1/admin/dq-issues/{fake_id}/resolve",
            headers=admin_headers,
            json={"correction_reason": "test"},
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_resolve_dq_issue_success(
        self, client: AsyncClient, test_session: AsyncSession, admin_headers: dict
    ):
        issue = await _create_dq_issue(test_session)
        await test_session.commit()

        response = await client.patch(
            f"/api/v1/admin/dq-issues/{issue.id}/resolve",
            headers=admin_headers,
            json={"correction_reason": "Confirmed as expected behaviour"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["issue_id"] == str(issue.id)
        assert "correction_id" in data
        assert "resolved_at" in data

        await test_session.refresh(issue)
        assert issue.resolved is True
        assert issue.resolved_at is not None

    @pytest.mark.asyncio
    async def test_resolve_dq_issue_creates_correction_audit_row(
        self, client: AsyncClient, test_session: AsyncSession, admin_headers: dict
    ):
        from sqlalchemy import select

        issue = await _create_dq_issue(test_session)
        await test_session.commit()

        response = await client.patch(
            f"/api/v1/admin/dq-issues/{issue.id}/resolve",
            headers=admin_headers,
            json={"correction_reason": "Audit test"},
        )
        assert response.status_code == 200
        correction_id = response.json()["correction_id"]

        result = await test_session.execute(
            select(CanonicalCorrection).where(CanonicalCorrection.id == UUID(correction_id))
        )
        correction = result.scalar_one_or_none()
        assert correction is not None
        assert correction.table_name == "dq_issue"
        assert correction.record_id == issue.id
        assert correction.before_snapshot == {"resolved": False, "resolved_at": None}
        assert correction.after_snapshot["resolved"] is True

    @pytest.mark.asyncio
    async def test_resolve_dq_issue_already_resolved_returns_409(
        self, client: AsyncClient, test_session: AsyncSession, admin_headers: dict
    ):
        issue = await _create_dq_issue(test_session, resolved=True)
        await test_session.commit()

        response = await client.patch(
            f"/api/v1/admin/dq-issues/{issue.id}/resolve",
            headers=admin_headers,
            json={"correction_reason": "Already done"},
        )
        assert response.status_code == 409
        assert "already resolved" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_resolve_dq_issue_sets_resolved_at_timestamp(
        self, client: AsyncClient, test_session: AsyncSession, admin_headers: dict
    ):
        issue = await _create_dq_issue(test_session)
        await test_session.commit()

        response = await client.patch(
            f"/api/v1/admin/dq-issues/{issue.id}/resolve",
            headers=admin_headers,
            json={"correction_reason": "Timestamp test"},
        )
        assert response.status_code == 200

        await test_session.refresh(issue)
        assert issue.resolved_at is not None
        assert issue.resolved_at.tzinfo is not None


# ── List Corrections Tests ────────────────────────────────────────────────────


class TestListCorrections:
    @pytest.mark.asyncio
    async def test_list_corrections_empty(self, client: AsyncClient, admin_headers: dict):
        response = await client.get("/api/v1/admin/corrections", headers=admin_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"] == []
        assert data["pagination"]["total_records"] == 0

    @pytest.mark.asyncio
    async def test_list_corrections_returns_all(
        self, client: AsyncClient, test_session: AsyncSession, admin_headers: dict
    ):
        instrument = await _create_instrument(test_session)
        await _create_eod(test_session, instrument.instrument_id)
        await test_session.commit()

        await client.patch(
            f"/api/v1/admin/eod/{instrument.instrument_id}/2025-01-02",
            headers=admin_headers,
            json={"correction_reason": "Fix 1", "close": "152.50"},
        )

        response = await client.get("/api/v1/admin/corrections", headers=admin_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["pagination"]["total_records"] == 1
        assert len(data["data"]) == 1

    @pytest.mark.asyncio
    async def test_list_corrections_filter_by_table_name(
        self, client: AsyncClient, test_session: AsyncSession, admin_headers: dict
    ):
        instrument = await _create_instrument(test_session)
        await _create_eod(test_session, instrument.instrument_id)
        issue = await _create_dq_issue(test_session)
        await test_session.commit()

        await client.patch(
            f"/api/v1/admin/eod/{instrument.instrument_id}/2025-01-02",
            headers=admin_headers,
            json={"correction_reason": "EOD fix", "close": "152.50"},
        )
        await client.patch(
            f"/api/v1/admin/dq-issues/{issue.id}/resolve",
            headers=admin_headers,
            json={"correction_reason": "DQ resolve"},
        )

        response = await client.get(
            "/api/v1/admin/corrections",
            headers=admin_headers,
            params={"table_name": "market_data_eod"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["pagination"]["total_records"] == 1
        assert data["data"][0]["table_name"] == "market_data_eod"

    @pytest.mark.asyncio
    async def test_list_corrections_filter_by_instrument_id(
        self, client: AsyncClient, test_session: AsyncSession, admin_headers: dict
    ):
        instrument = await _create_instrument(test_session)
        await _create_eod(test_session, instrument.instrument_id)
        await test_session.commit()

        await client.patch(
            f"/api/v1/admin/eod/{instrument.instrument_id}/2025-01-02",
            headers=admin_headers,
            json={"correction_reason": "Instrument filter test", "close": "152.50"},
        )

        response = await client.get(
            "/api/v1/admin/corrections",
            headers=admin_headers,
            params={"instrument_id": str(instrument.instrument_id)},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["pagination"]["total_records"] == 1

        other_id = uuid7()
        response2 = await client.get(
            "/api/v1/admin/corrections",
            headers=admin_headers,
            params={"instrument_id": str(other_id)},
        )
        assert response2.json()["pagination"]["total_records"] == 0

    @pytest.mark.asyncio
    async def test_list_corrections_pagination(
        self, client: AsyncClient, test_session: AsyncSession, admin_headers: dict
    ):
        instrument = await _create_instrument(test_session)
        await _create_eod(test_session, instrument.instrument_id)
        await test_session.commit()

        for i, close_val in enumerate(["152.00", "151.00", "150.50"]):
            eod_i = MarketDataEOD(
                id=uuid7(),
                instrument_id=instrument.instrument_id,
                trade_date=date(2025, 1, 3 + i),
                open=Decimal("150.00"),
                high=Decimal("155.00"),
                low=Decimal("149.00"),
                close=Decimal("153.00"),
                asof_ts=utc_now(),
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            test_session.add(eod_i)
            await test_session.flush()
            await client.patch(
                f"/api/v1/admin/eod/{instrument.instrument_id}/{date(2025, 1, 3 + i)}",
                headers=admin_headers,
                json={"correction_reason": f"Fix {i}", "close": close_val},
            )

        # Also patch the original one
        await client.patch(
            f"/api/v1/admin/eod/{instrument.instrument_id}/2025-01-02",
            headers=admin_headers,
            json={"correction_reason": "Original fix", "close": "152.50"},
        )

        response = await client.get(
            "/api/v1/admin/corrections",
            headers=admin_headers,
            params={"page": 1, "page_size": 2},
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["data"]) == 2
        assert data["pagination"]["total_records"] == 4
        assert data["pagination"]["total_pages"] == 2

    @pytest.mark.asyncio
    async def test_list_corrections_corrected_by_is_masked(
        self, client: AsyncClient, test_session: AsyncSession, admin_headers: dict
    ):
        instrument = await _create_instrument(test_session)
        await _create_eod(test_session, instrument.instrument_id)
        await test_session.commit()

        await client.patch(
            f"/api/v1/admin/eod/{instrument.instrument_id}/2025-01-02",
            headers=admin_headers,
            json={"correction_reason": "Masking test", "close": "152.50"},
        )

        response = await client.get("/api/v1/admin/corrections", headers=admin_headers)
        assert response.status_code == 200
        item = response.json()["data"][0]
        assert item["corrected_by"] == "test****"
        assert "admin-key" not in item["corrected_by"]

    @pytest.mark.asyncio
    async def test_list_corrections_ordered_newest_first(
        self, client: AsyncClient, test_session: AsyncSession, admin_headers: dict
    ):
        instrument = await _create_instrument(test_session)
        await _create_eod(test_session, instrument.instrument_id)

        eod2 = MarketDataEOD(
            id=uuid7(),
            instrument_id=instrument.instrument_id,
            trade_date=date(2025, 1, 3),
            open=Decimal("150.00"),
            high=Decimal("155.00"),
            low=Decimal("149.00"),
            close=Decimal("153.00"),
            asof_ts=utc_now(),
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        test_session.add(eod2)
        await test_session.commit()

        await client.patch(
            f"/api/v1/admin/eod/{instrument.instrument_id}/2025-01-02",
            headers=admin_headers,
            json={"correction_reason": "First", "close": "152.50"},
        )
        await client.patch(
            f"/api/v1/admin/eod/{instrument.instrument_id}/2025-01-03",
            headers=admin_headers,
            json={"correction_reason": "Second", "close": "151.00"},
        )

        response = await client.get("/api/v1/admin/corrections", headers=admin_headers)
        assert response.status_code == 200
        items = response.json()["data"]
        assert len(items) == 2
        assert items[0]["correction_reason"] == "Second"
        assert items[1]["correction_reason"] == "First"


# ── Refresh instrument cache Tests ────────────────────────────────────────────────────


class TestRefreshInstrumentCache:
    @pytest.mark.asyncio
    async def test_refresh_instrument_cache_without_api_key_returns_401(self):
        async with _admin_api_client() as client:
            response = await client.post("/api/v1/admin/instrument-cache/refresh")
        assert response.status_code == 401

    @pytest.mark.asyncio
    @patch("app.api.v1.admin.run_cache_generation")
    async def test_refresh_instrument_cache_queues_task(self, mock_run_cache, admin_headers):
        mock_run_cache.return_value = 0

        async with _admin_api_client() as client:
            response = await client.post(
                "/api/v1/admin/instrument-cache/refresh",
                headers=admin_headers,
            )

        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "accepted"
        assert "queued" in data["message"]
        mock_run_cache.assert_called_once()
