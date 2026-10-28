"""Whole-work settlement distinguishes provider publication waits from failures."""

from contextlib import contextmanager
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from findb_fetcher.provider_wait import (
    CalendarDecisionRepairRequiredError,
    ProviderPublicationPendingError,
)
from findb_fetcher.provider_work import WorkJournal
from test_full_market import _runtime


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            ProviderPublicationPendingError("finlab_target_not_ready", retry_after_seconds=731),
            {
                "outcome": "retry",
                "failure_code": "DAILY_PROVIDER_NOT_READY",
                "retry_delay_seconds": 731,
            },
        ),
        (
            CalendarDecisionRepairRequiredError("published calendar is now closed"),
            {
                "outcome": "failed",
                "failure_code": "CALENDAR_DECISION_REPAIR_REQUIRED",
            },
        ),
    ],
)
def test_publication_wait_settles_one_round_with_explicit_policy(
    tmp_path, monkeypatch, error, expected
):
    from findb_fetcher import provider_work

    runtime, source, _ = _runtime(tmp_path, provider="finlab", execution_profile="pilot")
    feed = runtime.feeds[0]
    feed.update(
        dataset_key="tw_equity_eod",
        source_timezone="Asia/Taipei",
        scheduled_time="18:00",
        target_date_lag_days=0,
        completion_window_seconds=14400,
    )
    runtime.installed_feeds = [feed]
    runtime._ensure_proof = lambda: None
    source.request = lambda *args, **kwargs: {
        "acquisition_allowed": False,
        "admitted_dataset_keys": [],
    }
    owner = SimpleNamespace(
        work={
            "kind": "daily",
            "dataset_key": feed["dataset_key"],
            "state": "open",
            "trade_date": str(date(2026, 10, 23)),
            "scope": {"feed": feed},
        }
    )

    @contextmanager
    def claimed(*args, **kwargs):
        yield owner

    monkeypatch.setattr(provider_work, "claimed_work", claimed)
    runtime._plan_for_date = lambda *args: (_ for _ in ()).throw(error)
    settlements = []
    runtime._settle_queued_round = lambda *args, **kwargs: settlements.append(kwargs)

    runtime._queued_cycle()

    assert settlements == [expected]
    runtime.close()


def test_factset_publication_retry_checks_calendar_before_provider_io(tmp_path) -> None:
    runtime, _, _ = _runtime(tmp_path, provider="factset", execution_profile="full_market")
    feed = runtime.feeds[0]
    feed.update(
        dataset_key="hk_equity_eod",
        slot_id="asia_pacific_markets_window",
        completion_window_seconds=16200,
    )
    plan = {
        "plan_id": "plan:hk_equity_eod",
        "dataset_key": "hk_equity_eod",
        "trade_date": "2026-10-23",
        "parts": [{"work_item_id": "hk_equity_eod", "member_keys": ["00001"]}],
    }
    release = {
        "members": [
            {
                "member_key": "00001",
                "provider_symbol": "AAAAA1-L",
                "mapping_status": "mapped",
            }
        ]
    }
    runtime.state.record_plan(plan, "factset")
    calendar_requests: list[str] = []
    work = {
        "work_id": "daily-work",
        "kind": "daily",
        "trade_date": plan["trade_date"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "attempt_number": 2,
        "manifest": None,
    }

    def request(action: str) -> dict[str, object]:
        calendar_requests.append(action)
        return {
            "work_id": work["work_id"],
            "trade_date": work["trade_date"],
            "repair_required": True,
        }

    def freeze(manifest: dict) -> dict:
        work["manifest"] = manifest
        return manifest

    journal = WorkJournal(tmp_path / "provider-work.sqlite3", work["work_id"])
    journal.observe_response("factset-eod:snapshot", b"{}", origin="provider_not_ready")
    owner = SimpleNamespace(
        work=work,
        request=request,
        freeze=freeze,
        journal=journal,
    )

    with pytest.raises(CalendarDecisionRepairRequiredError):
        runtime._deliver_factset_plan(owner, plan, release, feed)

    with runtime.state.connection() as db:
        row = db.execute(
            "SELECT status,reason FROM full_work WHERE key=?", ("hk_equity_eod:00001",)
        ).fetchone()
    assert (row["status"], row["reason"]) == ("manual", "calendar_repair_required")
    assert calendar_requests == ["calendar-status"]
    runtime.close()
