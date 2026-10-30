"""A stopped daily owner may replay its frozen scope, but cannot create it."""

from datetime import date

import pytest

from app.config import get_settings
from app.models.provider_work import ProviderWork
from app.models.registry import DailyExecutionDecision, SchedulerControl
from app.services import provider_work as queue
from app.services.source_clients import create_source_client
from tests.test_provider_work import attest_cutover, seed


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["pilot", "full_market"])
@pytest.mark.parametrize("already_frozen", [False, True])
async def test_authenticated_daily_stop_blocks_first_manifest_but_preserves_identical_replay(
    client, test_session, monkeypatch, profile, already_frozen
):
    db = test_session
    monkeypatch.setattr(get_settings(), "PROVIDER_WORK_REQUIRED", True)
    monkeypatch.setattr(get_settings(), "FULL_MARKET_ENABLED", True)
    dataset = await seed(db, "finlab")
    control = SchedulerControl(
        scheduler_key=f"{profile}_finlab_v1",
        provider="finlab",
        execution_profile=profile,
        desired_state="running",
        observed_state="running",
        timezone="Asia/Taipei",
        slot_id="taiwan_market_window",
    )
    db.add(control)
    day = date(2026, 10, 18)
    db.add(
        DailyExecutionDecision(
            dataset_key=dataset,
            provider="finlab",
            trade_date=day,
            execution_profile=profile,
            kind="skipped_calendar",
            snapshot={},
        )
    )
    _, secret = await create_source_client(
        db,
        name="daily-manifest",
        source_name="finlab",
        allowed_datasets=[dataset],
        rate_limit_requests=1000,
        rate_limit_window=60,
        commit=False,
    )
    work = await queue.enqueue(
        db,
        provider="finlab",
        stable_key=f"daily:{profile}:{dataset}:{day}",
        kind="daily",
        dataset_key=dataset,
        trade_date=day,
        execution_profile=profile,
    )
    work_id = work.work_id
    await attest_cutover(db)
    headers = {"X-API-Key": secret}
    claimed = await client.post(
        "/api/v1/source/provider-work/claim",
        headers=headers,
        json={
            "protocol": "provider-work-v1",
            "host_lock_identity": "local:1:22",
            "execution_profile": profile,
        },
    )
    assert claimed.status_code == 200, claimed.text
    owner = claimed.json()["work"]
    assert owner and owner["work_id"] == str(work_id)
    fence = {"generation": owner["generation"], "token": owner["token"]}
    route = f"/api/v1/source/provider-work/{work_id}"
    manifest = {"members": [{"key": "calendar_skip"}], "calendar_skip": True}
    if already_frozen:
        result = await client.post(
            route + "/manifest", headers=headers, json={**fence, "manifest": manifest}
        )
        assert result.status_code == 200, result.text
    control.desired_state = "stopped"
    await db.commit()
    result = await client.post(
        route + "/manifest", headers=headers, json={**fence, "manifest": manifest}
    )
    assert result.status_code == (200 if already_frozen else 409), result.text
    if not already_frozen:
        assert result.json()["detail"]["code"] == "PROVIDER_WORK_SUSPENDED"
    saved = await db.get(ProviderWork, work_id, populate_existing=True)
    assert saved.manifest == (manifest if already_frozen else None)
    assert (await client.post(route + "/close", headers=headers, json=fence)).status_code == 200
    assert (
        await client.post(
            route + "/settle", headers=headers, json={**fence, "outcome": "cancelled", "proof": {}}
        )
    ).status_code == 200
