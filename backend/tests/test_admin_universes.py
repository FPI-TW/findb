"""Read-only Admin universe candidate and member-diff endpoints."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.models.registry import DatasetRegistry, UniverseMember, UniverseRelease
from app.utils import uuid7

DAY = date(2026, 10, 28)
NOW = datetime(2026, 10, 28, 8, 0, tzinfo=timezone.utc)


def _release(*, status: str, effective_date: date, created_at: datetime) -> UniverseRelease:
    return UniverseRelease(
        release_id=uuid7(),
        dataset_key="tw_futures_eod",
        provider="taifex",
        effective_date=effective_date,
        observed_at=created_at,
        source_timezone="Asia/Taipei",
        evidence=[{"url": "https://example.org/taifex", "sha256": "a" * 64}],
        member_sha256=uuid7().hex + uuid7().hex,
        semantic_member_sha256=uuid7().hex + uuid7().hex,
        member_count=3,
        change_count=2,
        change_ratio=Decimal("0.66666667"),
        status=status,
        published_at=created_at if status == "published" else None,
        published_by="test-owner" if status == "published" else None,
        created_at=created_at,
    )


def _member(
    release_id,
    key: str,
    *,
    mapping_status: str = "mapped",
    raw_evidence_ref: str = "r2://bucket/original",
) -> UniverseMember:
    contract_code = key.split("|")[1]
    session = key.split("|")[2]
    return UniverseMember(
        member_id=uuid7(),
        release_id=release_id,
        member_key=key,
        symbol="MTX",
        provider_symbol=contract_code,
        exchange="TAIFEX",
        currency="TWD",
        asset_class="future",
        classification="weekly" if "W" in contract_code else "monthly",
        contract_code=contract_code,
        product_code="MTX",
        contract_month=contract_code.split(":")[1],
        session=session,
        mapping_status=mapping_status,
        raw_evidence_ref=raw_evidence_ref,
    )


@pytest.mark.asyncio
async def test_admin_lists_candidates_and_returns_semantic_member_diff(
    client, test_session, admin_headers, monkeypatch
) -> None:
    test_session.add(
        DatasetRegistry(
            dataset_key="tw_futures_eod",
            name="TW futures EOD",
            asset_class="future",
            market="TW",
            is_active=True,
            config={},
        )
    )
    baseline = _release(
        status="published",
        effective_date=DAY.replace(day=27),
        created_at=NOW.replace(day=27),
    )
    candidate = _release(status="candidate", effective_date=DAY, created_at=NOW)
    test_session.add_all([baseline, candidate])
    await test_session.flush()
    unchanged = "MTX|MTX:202610|regular"
    removed = "MTX|MTX:202610W4|regular"
    changed = "MTX|MTX:202611|regular"
    added = "MTX|MTX:202611|after_hours"
    test_session.add_all(
        [
            _member(baseline.release_id, unchanged),
            _member(baseline.release_id, removed),
            _member(baseline.release_id, changed),
            _member(
                candidate.release_id,
                unchanged,
                raw_evidence_ref="r2://bucket/new-evidence-only",
            ),
            _member(candidate.release_id, changed, mapping_status="manual"),
            _member(candidate.release_id, added),
        ]
    )
    await test_session.commit()

    async def fail_per_release_comparison(*_args, **_kwargs):
        raise AssertionError("universe list must not query the comparison release per row")

    with monkeypatch.context() as patch:
        patch.setattr(
            "app.services.universe_queries._comparison_release",
            fail_per_release_comparison,
        )
        response = await client.get(
            "/api/v1/admin/universes",
            params={
                "status": "candidate",
                "dataset_key": "tw_futures_eod",
                "provider": "TAIFEX",
                "effective_date": DAY.isoformat(),
            },
            headers=admin_headers,
        )
    assert response.status_code == 200
    body = response.json()
    assert body["pagination"]["total_records"] == 1
    assert body["data"][0]["release_id"] == str(candidate.release_id)
    assert body["data"][0]["comparison_release_id"] == str(baseline.release_id)
    assert body["data"][0]["semantic_member_sha256"] == candidate.semantic_member_sha256

    diff_response = await client.get(
        f"/api/v1/admin/universes/{candidate.release_id}/diff",
        headers=admin_headers,
    )
    assert diff_response.status_code == 200
    diff = diff_response.json()
    assert diff["comparison_release"]["release_id"] == str(baseline.release_id)
    assert [member["member_key"] for member in diff["added"]] == [added]
    assert [member["member_key"] for member in diff["removed"]] == [removed]
    assert diff["unchanged_count"] == 1
    assert len(diff["changed"]) == 1
    assert diff["changed"][0]["member_key"] == changed
    assert diff["changed"][0]["changed_fields"] == ["mapping_status"]
    assert diff["changed"][0]["before"]["mapping_status"] == "mapped"
    assert diff["changed"][0]["after"]["mapping_status"] == "manual"


@pytest.mark.asyncio
async def test_admin_universe_reads_require_auth_and_unknown_release_is_404(
    client, admin_headers
) -> None:
    unauthorized = await client.get("/api/v1/admin/universes?status=candidate")
    assert unauthorized.status_code == 401
    missing = await client.get(
        f"/api/v1/admin/universes/{uuid7()}/diff",
        headers=admin_headers,
    )
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Universe release not found"}
