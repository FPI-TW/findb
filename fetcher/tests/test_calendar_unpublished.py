"""A calendar-required date waits for its published calendar without holding siblings."""

from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest

from findb_fetcher.config import FetcherConfig
from findb_fetcher.full_market_client import (
    CALENDAR_UNPUBLISHED,
    FullMarketProtocolError,
    FullMarketSourceClient,
)
from findb_fetcher.full_market_errors import CalendarUnpublishedPendingError
from test_taifex_dated_universe import DAY, plan, setup_runtime


@pytest.mark.parametrize(
    "body,code",
    [
        ({"detail": {"code": CALENDAR_UNPUBLISHED}}, CALENDAR_UNPUBLISHED),
        ({"detail": {"code": CALENDAR_UNPUBLISHED, "token": "secret"}}, None),
        ({"detail": "secret"}, None),
    ],
)
def test_protocol_recognizes_only_the_exact_calendar_code(body, code):
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(409, json=body))
    ) as http:
        source = FullMarketSourceClient(
            FetcherConfig("https://source.example", "offline-key"), client=http
        )
        with pytest.raises(FullMarketProtocolError) as exc:
            source.plan(
                dataset="tw_futures_eod", provider="taifex", trade_date=str(DAY), release_id="old"
            )
        assert exc.value.code == code and "secret" not in str(exc.value)


def test_plan_refusal_becomes_a_calendar_wait(tmp_path, monkeypatch):
    runtime, source, feed = setup_runtime(tmp_path, monkeypatch, "full_market")

    def refuse(**kwargs):
        raise FullMarketProtocolError("unpublished", status_code=409, code=CALENDAR_UNPUBLISHED)

    source.plan = refuse
    with pytest.raises(CalendarUnpublishedPendingError) as pending:
        runtime._plan_for_date(feed, DAY)
    assert pending.value.retry_after_seconds == 3600
    runtime.close()


def test_queued_calendar_wait_settles_as_retry(tmp_path, monkeypatch):
    from findb_fetcher import provider_work

    runtime, _, feed = setup_runtime(tmp_path, monkeypatch, "full_market")
    owner = SimpleNamespace(
        work={
            "kind": "daily",
            "dataset_key": feed["dataset_key"],
            "state": "open",
            "trade_date": str(DAY),
            "scope": {"feed": feed},
        }
    )

    @contextmanager
    def claimed(*args, **kwargs):
        yield owner

    monkeypatch.setattr(provider_work, "claimed_work", claimed)
    runtime._plan_for_date = lambda *args: (_ for _ in ()).throw(CalendarUnpublishedPendingError())
    settlements = []
    runtime._settle_queued_round = lambda *args, **kwargs: settlements.append(kwargs)

    runtime._queued_cycle(plan_new_dates=False)

    assert settlements == [
        {
            "outcome": "retry",
            "failure_code": "DAILY_CALENDAR_UNPUBLISHED",
            "retry_delay_seconds": 3600,
        }
    ]
    runtime.close()


def test_sibling_without_calendar_does_not_hold_the_others(tmp_path, monkeypatch):
    runtime, _, feed = setup_runtime(tmp_path, monkeypatch, "full_market")
    waiting = {**feed, "dataset_key": "waiting_eod"}
    planned = []

    def plan_for_date(sibling, day):
        if sibling["dataset_key"] == "waiting_eod":
            raise CalendarUnpublishedPendingError()
        planned.append(sibling["dataset_key"])
        return plan("full_market", day)

    runtime._plan_for_date = plan_for_date
    recorded = []
    runtime.state.record_plan = lambda body, provider: recorded.append(body["dataset_key"])

    runtime._freeze_same_date_siblings(
        [(DAY, waiting), (DAY, feed)], DAY, [feed["dataset_key"], "waiting_eod"]
    )

    assert planned == recorded == [feed["dataset_key"]]
    runtime.close()
