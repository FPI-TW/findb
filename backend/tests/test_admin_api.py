"""
Tests for Admin API endpoints.
"""

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.canonical import Instrument, InstrumentStats, MarketDataEOD
from app.models.correction import CanonicalCorrection
from app.models.raw import RawMarketPayload
from app.models.registry import APIKey, DatasetRegistry, DQIssue, IngestionRun
from app.services import instrument_cache as instrument_cache_service
from app.services.admin import eod_record_id
from app.services.ingestion import IngestionService
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


async def _create_raw_payload(
    session: AsyncSession,
    *,
    run_id: UUID | None = None,
    dataset_key: str = "tw_equity_eod",
    idempotency_key: str = "raw-idempotency",
    request_key: str = "raw-request",
    payload: dict | None = None,
    created_at: datetime | None = None,
) -> RawMarketPayload:
    created_at = created_at or utc_now()
    raw = RawMarketPayload(
        raw_payload_id=uuid7(),
        source_client_id=None,
        dataset_key=dataset_key,
        source="finlab",
        request_key=request_key,
        idempotency_key=idempotency_key,
        schema_id="market_eod",
        schema_version=1,
        payload=payload if payload is not None else {"data": []},
        fetched_at=created_at,
        expire_at=created_at + timedelta(days=1),
        run_id=run_id or uuid7(),
        created_at=created_at,
    )
    session.add(raw)
    await session.flush()
    return raw


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
                "first_trade_date": "2024-01-03",
                "latest_trade_date": "2025-01-02",
                "latest_price": "153.00",
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
                "first_trade_date": "2023-06-01",
                "latest_trade_date": "2025-01-02",
                "latest_price": "390.50",
            },
        ],
    }


def _write_instrument_cache(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


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
        self,
        client: AsyncClient,
        test_session: AsyncSession,
        admin_headers: dict,
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


class TestAPIKeyAdmin:
    @pytest.mark.asyncio
    async def test_api_key_lifecycle_controls_serve_access(
        self,
        client: AsyncClient,
        test_session: AsyncSession,
        admin_headers: dict,
    ):
        settings = get_settings()
        original_require_auth = settings.SERVE_REQUIRE_AUTH
        settings.SERVE_REQUIRE_AUTH = True
        try:
            create_response = await client.post(
                "/api/v1/admin/api-keys",
                headers=admin_headers,
                json={
                    "owner": "llm-client",
                    "tier": "llm",
                    "scopes": ["serve"],
                    "rate_limit_requests": 2,
                    "rate_limit_window": 60,
                    "page_size_limit": 2,
                },
            )
            assert create_response.status_code == 200
            created = create_response.json()
            plaintext_key = created["api_key"]
            key_id = created["data"]["key_id"]
            assert plaintext_key.startswith("findb_")
            assert "key_hash" not in created["data"]

            list_response = await client.get("/api/v1/admin/api-keys", headers=admin_headers)
            assert list_response.status_code == 200
            listed = list_response.json()["data"]
            assert listed[0]["key_id"] == key_id
            assert "api_key" not in listed[0]
            assert "key_hash" not in listed[0]

            serve_headers = {settings.API_KEY_HEADER: plaintext_key}
            ok_response = await client.get(
                "/api/v1/serve/instruments?page_size=2",
                headers=serve_headers,
            )
            assert ok_response.status_code == 200

            row = await test_session.get(APIKey, UUID(key_id))
            assert row is not None
            await test_session.refresh(row)
            assert row.usage_count == 0
            assert row.last_used_at is None

            too_large_response = await client.get(
                "/api/v1/serve/instruments?page_size=3",
                headers=serve_headers,
            )
            assert too_large_response.status_code == 400
            assert "page_size exceeds" in too_large_response.json()["detail"]

            default_page_size_response = await client.get(
                "/api/v1/serve/instruments",
                headers=serve_headers,
            )
            assert default_page_size_response.status_code == 400
            assert "page_size exceeds" in default_page_size_response.json()["detail"]

            second_ok_response = await client.get(
                "/api/v1/serve/instruments?page_size=2",
                headers=serve_headers,
            )
            assert second_ok_response.status_code == 200
            limited_response = await client.get(
                "/api/v1/serve/instruments?page_size=2",
                headers=serve_headers,
            )
            assert limited_response.status_code == 429

            revoke_response = await client.delete(
                f"/api/v1/admin/api-keys/{key_id}",
                headers=admin_headers,
            )
            assert revoke_response.status_code == 200
            assert revoke_response.json()["revoked_at"] is not None
        finally:
            settings.SERVE_REQUIRE_AUTH = original_require_auth

    @pytest.mark.asyncio
    async def test_serve_api_reports_server_misconfiguration(self, client: AsyncClient):
        settings = get_settings()
        original_require_auth = settings.SERVE_REQUIRE_AUTH
        settings.SERVE_REQUIRE_AUTH = True
        try:
            response = await client.get(
                "/api/v1/serve/instruments",
                headers={settings.API_KEY_HEADER: "anything"},
            )
            assert response.status_code == 500
            assert "no API keys configured" in response.json()["detail"]
        finally:
            settings.SERVE_REQUIRE_AUTH = original_require_auth

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
        client: AsyncClient,
    ):
        _write_instrument_cache(cache_path, _sample_instrument_cache())
        response = await client.get("/api/v1/admin/instrument-cache")

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_get_instrument_cache_returns_generated_json(
        self,
        cache_path: Path,
        client: AsyncClient,
        admin_headers: dict,
    ):
        _write_instrument_cache(cache_path, _sample_instrument_cache())

        response = await client.get("/api/v1/admin/instrument-cache", headers=admin_headers)

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 2
        assert data["data"][0]["instrument_id"] == "instrument-us-aapl"
        assert data["data"][0]["short_name"] == "Apple"

    @pytest.mark.asyncio
    async def test_get_instrument_cache_missing_file_returns_404(
        self,
        cache_path: Path,
        client: AsyncClient,
        admin_headers: dict,
    ):
        response = await client.get("/api/v1/admin/instrument-cache", headers=admin_headers)

        assert response.status_code == 404
        assert str(cache_path) in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_put_instrument_cache_replaces_and_normalizes_json(
        self,
        cache_path: Path,
        client: AsyncClient,
        admin_headers: dict,
    ):
        payload = _sample_instrument_cache()
        payload["total"] = 99
        payload["markets"] = ["WRONG"]

        response = await client.put(
            "/api/v1/admin/instrument-cache", headers=admin_headers, json=payload
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 2
        assert data["markets"] == ["HK", "US"]

        stored = json.loads(cache_path.read_text(encoding="utf-8"))
        assert stored["total"] == 2
        assert stored["markets"] == ["HK", "US"]
        assert stored["data"][0]["instrument_id"] == "instrument-hk-0700"
        assert stored["data"][0]["first_trade_date"] == "2023-06-01"
        assert stored["data"][0]["short_name"] == "Tencent"

    @pytest.mark.asyncio
    async def test_patch_instrument_cache_item_updates_json(
        self,
        cache_path: Path,
        client: AsyncClient,
        admin_headers: dict,
    ):
        _write_instrument_cache(cache_path, _sample_instrument_cache())

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
        assert stored_item["first_trade_date"] == "2024-01-03"
        assert stored_item["name"] == "Apple Inc."
        assert stored_item["status"] == "inactive"
        assert stored_item["short_name"] == "Apple"

    @pytest.mark.asyncio
    async def test_patch_instrument_cache_item_not_found_returns_404(
        self,
        cache_path: Path,
        client: AsyncClient,
        admin_headers: dict,
    ):
        _write_instrument_cache(cache_path, _sample_instrument_cache())

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
        client: AsyncClient,
        admin_headers: dict,
    ):
        _write_instrument_cache(cache_path, _sample_instrument_cache())

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
        assert data["record_id"] == str(eod_record_id(instrument.instrument_id, date(2025, 1, 2)))
        assert data["trade_date"] == "2025-01-02"

        await test_session.refresh(eod)
        assert eod.close == Decimal("152.50")

        stats = await test_session.get(InstrumentStats, instrument.instrument_id)
        assert stats is not None
        assert stats.latest_trade_date == date(2025, 1, 2)
        assert stats.latest_price == Decimal("152.50")

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
    async def test_patch_eod_rejects_invalid_ohlc_relationship(
        self, client: AsyncClient, test_session: AsyncSession, admin_headers: dict
    ):
        instrument = await _create_instrument(test_session)
        eod = await _create_eod(test_session, instrument.instrument_id)
        await test_session.commit()

        response = await client.patch(
            f"/api/v1/admin/eod/{instrument.instrument_id}/2025-01-02",
            headers=admin_headers,
            json={"correction_reason": "Invalid high", "high": "140.00"},
        )
        assert response.status_code == 400
        assert "less than max" in response.json()["detail"]

        await test_session.refresh(eod)
        assert eod.high == Decimal("155.00")

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
        assert correction.record_id == eod_record_id(instrument.instrument_id, date(2025, 1, 2))
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

    @pytest.mark.asyncio
    async def test_patch_eod_rejects_negative_volume_at_schema_level(
        self, client: AsyncClient, test_session: AsyncSession, admin_headers: dict
    ):
        instrument = await _create_instrument(test_session)
        await _create_eod(test_session, instrument.instrument_id)
        await test_session.commit()

        response = await client.patch(
            f"/api/v1/admin/eod/{instrument.instrument_id}/2025-01-02",
            headers=admin_headers,
            json={"correction_reason": "Invalid volume", "volume": -1},
        )
        assert response.status_code == 422


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
    async def test_list_corrections_corrected_by_uses_fingerprint(
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
        correction = (
            (
                await test_session.execute(
                    CanonicalCorrection.__table__.select().where(
                        CanonicalCorrection.id == UUID(item["id"])
                    )
                )
            )
            .mappings()
            .one()
        )
        assert item["corrected_by"] == correction["corrected_by"]
        assert item["corrected_by"].startswith("machine:")
        assert item["corrected_by"].endswith(":test-admin-machine")

    @pytest.mark.asyncio
    async def test_list_corrections_ordered_newest_first(
        self, client: AsyncClient, test_session: AsyncSession, admin_headers: dict
    ):
        instrument = await _create_instrument(test_session)
        await _create_eod(test_session, instrument.instrument_id)

        eod2 = MarketDataEOD(
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


class TestRawPayloadAdmin:
    @pytest.mark.asyncio
    async def test_list_raw_payloads_default_and_lightweight_projection(
        self,
        client: AsyncClient,
        test_session: AsyncSession,
        admin_headers: dict,
        monkeypatch: pytest.MonkeyPatch,
    ):
        raw = await _create_raw_payload(
            test_session,
            idempotency_key="raw-full",
            request_key="request-full",
            payload={"secret": "full-list-payload"},
        )
        await test_session.commit()

        full_response = await client.get(
            "/api/v1/admin/raw-payloads",
            headers=admin_headers,
            params={"page": 1, "page_size": 1},
        )
        assert full_response.status_code == 200
        full_item = full_response.json()["data"][0]
        assert full_item["raw_payload_id"] == str(raw.raw_payload_id)
        assert full_item["payload"] == {"secret": "full-list-payload"}

        executed_statements = []
        original_execute = AsyncSession.execute

        async def capture_execute(session, statement, *args, **kwargs):
            executed_statements.append(statement)
            return await original_execute(session, statement, *args, **kwargs)

        monkeypatch.setattr(AsyncSession, "execute", capture_execute)
        light_response = await client.get(
            "/api/v1/admin/raw-payloads",
            headers=admin_headers,
            params={"include_payload": "false", "page": 1, "page_size": 1},
        )
        assert light_response.status_code == 200
        light_item = light_response.json()["data"][0]
        assert light_item["raw_payload_id"] == str(raw.raw_payload_id)
        assert light_item["payload"] is None
        assert light_response.json()["pagination"] == {
            "page": 1,
            "page_size": 1,
            "total_records": 1,
            "total_pages": 1,
            "next_cursor": None,
        }

        # The last statement is the list query (the auth and count queries run
        # before it).  ``payload`` must not appear in its SELECT projection;
        # the table name and raw_payload_id are intentionally allowed.
        list_sql = str(executed_statements[-1].compile())
        select_clause = list_sql.split(" FROM ", 1)[0].lower()
        assert ".payload" not in select_clause

    @pytest.mark.asyncio
    async def test_list_raw_payloads_filters_and_pagination(
        self, client: AsyncClient, test_session: AsyncSession, admin_headers: dict
    ):
        await _create_raw_payload(
            test_session,
            dataset_key="tw_equity_eod",
            idempotency_key="raw-filter-1",
            request_key="request-filter-1",
            payload={"index": 1},
            created_at=datetime(2026, 1, 1, 8, tzinfo=timezone.utc),
        )
        newest = await _create_raw_payload(
            test_session,
            dataset_key="tw_equity_eod",
            idempotency_key="raw-filter-2",
            request_key="request-filter-2",
            payload={"index": 2},
            created_at=datetime(2026, 1, 2, 8, tzinfo=timezone.utc),
        )
        await _create_raw_payload(
            test_session,
            dataset_key="other_dataset",
            idempotency_key="raw-filter-3",
            request_key="request-filter-3",
            payload={"index": 3},
            created_at=datetime(2026, 1, 2, 9, tzinfo=timezone.utc),
        )
        await test_session.commit()

        response = await client.get(
            "/api/v1/admin/raw-payloads",
            headers=admin_headers,
            params={
                "dataset_key": "tw_equity_eod",
                "date_from": "2026-01-01",
                "date_to": "2026-01-02",
                "page": 1,
                "page_size": 1,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["pagination"] == {
            "page": 1,
            "page_size": 1,
            "total_records": 2,
            "total_pages": 2,
            "next_cursor": None,
        }
        assert data["data"][0]["raw_payload_id"] == str(newest.raw_payload_id)
        assert data["data"][0]["payload"] == {"index": 2}

        second_page = await client.get(
            "/api/v1/admin/raw-payloads",
            headers=admin_headers,
            params={
                "dataset_key": "tw_equity_eod",
                "date_from": "2026-01-01",
                "date_to": "2026-01-02",
                "page": 2,
                "page_size": 1,
            },
        )
        assert second_page.status_code == 200
        assert len(second_page.json()["data"]) == 1
        assert second_page.json()["data"][0]["payload"] == {"index": 1}

    @pytest.mark.asyncio
    async def test_raw_payload_by_id_is_exact_for_duplicate_run_rows_and_auth(
        self, client: AsyncClient, test_session: AsyncSession, admin_headers: dict
    ):
        run_id = uuid7()
        first = await _create_raw_payload(
            test_session,
            run_id=run_id,
            idempotency_key="raw-duplicate-1",
            request_key="request-duplicate-1",
            payload={"which": "first"},
        )
        second = await _create_raw_payload(
            test_session,
            run_id=run_id,
            idempotency_key="raw-duplicate-2",
            request_key="request-duplicate-2",
            payload={"which": "second"},
        )
        single = await _create_raw_payload(
            test_session,
            idempotency_key="raw-single",
            request_key="request-single",
            payload={"which": "single"},
        )
        await test_session.commit()

        first_response = await client.get(
            f"/api/v1/admin/raw-payloads/by-id/{first.raw_payload_id}",
            headers=admin_headers,
        )
        second_response = await client.get(
            f"/api/v1/admin/raw-payloads/by-id/{second.raw_payload_id}",
            headers=admin_headers,
        )
        assert first_response.status_code == 200
        assert second_response.status_code == 200
        assert first_response.json()["raw_payload_id"] == str(first.raw_payload_id)
        assert first_response.json()["payload"] == {"which": "first"}
        assert second_response.json()["raw_payload_id"] == str(second.raw_payload_id)
        assert second_response.json()["payload"] == {"which": "second"}

        # Keep the legacy run-id lookup available for runs with one raw row.
        run_response = await client.get(
            f"/api/v1/admin/raw-payloads/{single.run_id}", headers=admin_headers
        )
        assert run_response.status_code == 200
        assert run_response.json()["raw_payload_id"] == str(single.raw_payload_id)
        assert run_response.json()["payload"] == {"which": "single"}

        missing_response = await client.get(
            f"/api/v1/admin/raw-payloads/by-id/{uuid7()}", headers=admin_headers
        )
        assert missing_response.status_code == 404
        assert missing_response.json() == {"detail": "Raw payload not found"}

        unauthorized_response = await client.get(
            f"/api/v1/admin/raw-payloads/by-id/{first.raw_payload_id}"
        )
        assert unauthorized_response.status_code == 401


class TestDQIssueProvenance:
    @pytest.mark.asyncio
    async def test_dq_list_shapes_expired_raw_and_bounds_policy_evidence(
        self,
        client: AsyncClient,
        test_session: AsyncSession,
        admin_headers: dict,
    ):
        test_session.add(
            DatasetRegistry(
                dataset_key="tw_equity_eod",
                name="TW Equity",
                asset_class="equity",
                market="TW",
                frequency="daily",
                is_active=True,
                config={},
            )
        )
        run = IngestionRun(
            dataset_key="tw_equity_eod",
            source="finlab",
            schema_id="market_eod",
            schema_version=1,
            request_key="dq-request",
            batch_data_date=date(2026, 7, 21),
            raw_records=2,
            policy_details={
                "primary_code": "BATCH_RECORD_COUNT_DROP",
                "violations": [
                    {
                        "code": "BATCH_RECORD_COUNT_DROP",
                        "action": "warn",
                        "reason": "x" * 1000,
                        "observed": {"record_count": 2, "secret": "do-not-return"},
                        "expected": {"minimum_record_count": 2100},
                    }
                ]
                * 12,
                "baseline_status": "disabled",
                "baseline_counts": list(range(20)),
            },
        )
        test_session.add(run)
        await test_session.flush()
        raw = RawMarketPayload(
            raw_payload_id=uuid7(),
            source_client_id=None,
            dataset_key="tw_equity_eod",
            source="finlab",
            request_key="dq-request",
            idempotency_key="dq-idem",
            schema_id="market_eod",
            schema_version=1,
            payload={"secret": "full payload must never be listed"},
            fetched_at=datetime(2026, 7, 21, 8, tzinfo=timezone.utc),
            expire_at=datetime.now(timezone.utc) - timedelta(days=1),
            run_id=run.run_id,
        )
        test_session.add(raw)
        run.raw_payload_id = raw.raw_payload_id
        issue = DQIssue(
            id=uuid7(),
            run_id=run.run_id,
            issue_type="INGRESS_DELIVERY_POLICY_WARNING",
            severity="warning",
            description="bounded policy warning",
        )
        test_session.add(issue)
        await test_session.commit()

        response = await client.get("/api/v1/admin/dq-issues?page_size=1", headers=admin_headers)
        assert response.status_code == 200
        item = response.json()["data"][0]
        assert item["source"] == "finlab"
        assert item["provider"] == "finlab"
        assert item["raw_available"] is False
        assert item["fetched_at"] is None
        assert "raw_data" not in item
        assert "secret" not in str(item)
        detail = item["policy_detail"]
        assert len(detail["violations"]) <= 8
        assert detail["violation_count"] == 12
        assert detail["violations_truncated"] is True
        assert detail["baseline_counts_truncated"] is True

    @pytest.mark.asyncio
    async def test_dq_pagination_remains_unique_with_multiple_legacy_raw_rows(
        self,
        client: AsyncClient,
        test_session: AsyncSession,
        admin_headers: dict,
    ):
        test_session.add(
            DatasetRegistry(
                dataset_key="tw_equity_eod",
                name="TW Equity",
                asset_class="equity",
                market="TW",
                frequency="daily",
                is_active=True,
                config={},
            )
        )
        runs = [
            IngestionRun(
                run_id=uuid7(),
                dataset_key="tw_equity_eod",
                source="finlab",
                status="failed",
                request_key=f"page-request-{index}",
            )
            for index in (1, 2)
        ]
        test_session.add_all(runs)
        await test_session.flush()
        for index, run in enumerate(runs, start=1):
            for duplicate in (1, 2):
                test_session.add(
                    RawMarketPayload(
                        raw_payload_id=uuid7(),
                        source_client_id=None,
                        dataset_key="tw_equity_eod",
                        source="finlab",
                        request_key=f"page-request-{index}-{duplicate}",
                        idempotency_key=f"page-idem-{index}-{duplicate}",
                        payload={"data": []},
                        fetched_at=datetime(2026, 7, 21, 8, tzinfo=timezone.utc),
                        expire_at=datetime.now(timezone.utc) + timedelta(days=1),
                        run_id=run.run_id,
                    )
                )
            test_session.add(
                DQIssue(
                    id=uuid7(),
                    run_id=run.run_id,
                    issue_type="MISSING_OHLC",
                    severity="warning",
                    description=f"page issue {index}",
                )
            )
        await test_session.commit()

        first = await client.get(
            "/api/v1/admin/dq-issues?page=1&page_size=1", headers=admin_headers
        )
        second = await client.get(
            "/api/v1/admin/dq-issues?page=2&page_size=1", headers=admin_headers
        )
        assert first.status_code == 200
        assert second.status_code == 200
        first_id = first.json()["data"][0]["id"]
        second_id = second.json()["data"][0]["id"]
        assert first_id != second_id
        assert first.json()["pagination"]["total_records"] == 2
        assert second.json()["pagination"]["total_records"] == 2


# ── Bulk rerun Tests ──────────────────────────────────────────────────────────────────


class TestBulkRerun:
    @pytest.mark.asyncio
    async def test_bulk_rerun_limit_is_bounded(
        self,
        client: AsyncClient,
        admin_headers: dict,
    ):
        response = await client.post(
            "/api/v1/admin/runs/bulk-rerun?limit=1001",
            headers=admin_headers,
        )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_bulk_rerun_rolls_back_failed_item_before_continuing(
        self,
        client: AsyncClient,
        test_session: AsyncSession,
        admin_headers: dict,
        monkeypatch,
    ):
        dataset = DatasetRegistry(
            dataset_key="tw_equity_eod",
            name="TW Equity EOD",
            asset_class="equity",
            market="TW",
            frequency="daily",
            is_active=True,
            config={},
        )
        runs = [
            IngestionRun(
                dataset_key="tw_equity_eod",
                status="completed",
                completed_at=utc_now(),
                created_at=utc_now(),
            )
            for _ in range(2)
        ]
        test_session.add_all((dataset, *runs))
        await test_session.commit()
        calls = 0

        async def fail_once_then_succeed(self, run_id):
            nonlocal calls
            calls += 1
            if calls == 1:
                await self.db.execute(text("SELECT 1 / 0"))
            return uuid7(), "queued", "tw_equity_eod", {}

        monkeypatch.setattr(
            IngestionService,
            "rerun_from_raw",
            fail_once_then_succeed,
        )

        response = await client.post(
            "/api/v1/admin/runs/bulk-rerun?dataset_key=tw_equity_eod&limit=2",
            headers=admin_headers,
        )

        assert response.status_code == 200
        assert response.json()["queued"] == 1
        assert response.json()["errors"] == 1


# ── Refresh instrument cache Tests ────────────────────────────────────────────────────


class TestRefreshInstrumentCache:
    @pytest.mark.asyncio
    async def test_refresh_instrument_cache_without_api_key_returns_401(self, client: AsyncClient):
        response = await client.post("/api/v1/admin/instrument-cache/refresh")
        assert response.status_code == 401

    @pytest.mark.asyncio
    @patch("app.api.v1.admin.run_cache_generation")
    async def test_refresh_instrument_cache_queues_task(
        self, mock_run_cache, client: AsyncClient, admin_headers: dict
    ):
        mock_run_cache.return_value = 0

        response = await client.post(
            "/api/v1/admin/instrument-cache/refresh", headers=admin_headers
        )

        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "accepted"
        assert "queued" in data["message"]
        mock_run_cache.assert_called_once()
