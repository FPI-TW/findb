"""After its last no-data retry a member reports official halt evidence or a final outcome (#593)."""

from datetime import date

import httpx
import pytest

from findb_fetcher.full_market_client import FullMarketProtocolError
from findb_fetcher.full_market_state import FULL_WORK_NO_DATA_MAX_ATTEMPTS
from findb_fetcher.full_market_universe import (
    NASDAQ_HALTS_URL,
    UniverseSnapshotError,
    nasdaq_halt,
    official_snapshot_dataset,
)
from provider_work_support import owned_unit_work
from test_full_market import _plan, _runtime
from test_full_market_error_outcomes import _members, _skip_waits, _statuses

HALTS = b"""\xef\xbb\xbf<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:ndaq="http://www.nasdaqtrader.com/">
  <channel>
    <item>
      <ndaq:HaltDate>02/22/2019</ndaq:HaltDate>
      <ndaq:HaltTime>16:02:01.000</ndaq:HaltTime>
      <ndaq:IssueSymbol>SVA</ndaq:IssueSymbol>
      <ndaq:IssueName>Sinovac Biotech, Ltd Ord Shrs</ndaq:IssueName>
      <ndaq:Market>NASDAQ</ndaq:Market>
      <ndaq:ReasonCode>T12</ndaq:ReasonCode>
      <ndaq:ResumptionDate />
    </item>
    <item>
      <ndaq:HaltDate>11/24/2026</ndaq:HaltDate>
      <ndaq:HaltTime>19:50:00.000</ndaq:HaltTime>
      <ndaq:IssueSymbol>PASG</ndaq:IssueSymbol>
      <ndaq:Market>NASDAQ</ndaq:Market>
      <ndaq:ReasonCode>T1</ndaq:ReasonCode>
      <ndaq:ResumptionDate />
    </item>
    <item>
      <ndaq:HaltDate>11/20/2026</ndaq:HaltDate>
      <ndaq:IssueSymbol>RSMD</ndaq:IssueSymbol>
      <ndaq:ReasonCode>T1</ndaq:ReasonCode>
      <ndaq:ResumptionDate>11/24/2026</ndaq:ResumptionDate>
    </item>
  </channel>
</rss>"""
DAY = date(2026, 11, 24)


def test_only_a_halt_covering_the_whole_session_counts():
    assert nasdaq_halt(HALTS, "SVA", DAY) == {
        "IssueSymbol": "SVA",
        "IssueName": "Sinovac Biotech, Ltd Ord Shrs",
        "Market": "NASDAQ",
        "HaltDate": "02/22/2019",
        "HaltTime": "16:02:01.000",
        "ReasonCode": "T12",
        "ResumptionDate": "",
    }
    # Halted after the trade date's own session, resumed on it, or never listed.
    assert nasdaq_halt(HALTS, "PASG", DAY) is None
    assert nasdaq_halt(HALTS, "RSMD", DAY) is None
    assert nasdaq_halt(HALTS, "RSMD", date(2026, 11, 23)) is not None
    assert nasdaq_halt(HALTS, "AAPL", DAY) is None
    assert official_snapshot_dataset(NASDAQ_HALTS_URL) == "us_equity_eod"


@pytest.mark.parametrize(
    "body", [b"not xml", b'<!DOCTYPE x [<!ENTITY a "b">]><rss/>', HALTS.replace(b"02/22", b"2/30")]
)
def test_an_unusable_halts_feed_is_refused(body):
    with pytest.raises(UniverseSnapshotError):
        nasdaq_halt(body, "SVA", DAY)


def test_twelve_data_reads_the_halts_feed_only_for_us_equities(tmp_path):
    runtime, _, _ = _runtime(tmp_path)
    fetched = []

    def snapshot(url, **_):
        fetched.append(url)
        return HALTS

    feed = {**runtime.feeds[0], "dataset_key": "us_equity_eod"}
    try:
        context = runtime._provider_context("us_equity_eod", DAY, feed, delivery={})
        context = context.__class__(**{**context.__dict__, "snapshot": snapshot})
        member = {"member_key": "SVA", "symbol": "SVA", "provider_symbol": "SVA"}
        evidence = runtime.adapter.no_data_evidence(member, context)
        assert evidence is not None
        assert {key: evidence[key] for key in ("url", "source_symbol", "trade_date")} == {
            "url": NASDAQ_HALTS_URL,
            "source_symbol": "SVA",
            "trade_date": "2026-11-24",
        }
        assert (
            evidence["source_status"] == "halted"
            and '"ReasonCode":"T12"' in (evidence["source_excerpt"])
        )
        other = context.__class__(**{**context.__dict__, "dataset_key": "hk_equity_eod"})
        assert runtime.adapter.no_data_evidence(member, other) is None
        assert fetched == [NASDAQ_HALTS_URL]
    finally:
        runtime.close()


def _exhaust(tmp_path, evidence, outcome=None):
    """Run ACA's last no-data retry with ``evidence`` from the adapter."""
    from findb_fetcher.providers.twelve_data import TwelveDataResponseError

    runtime, source, _ = _runtime(tmp_path)
    runtime._acquisition_allowed = lambda: True

    def acquire(member, *args):
        if member["member_key"] == "ACA":
            raise TwelveDataResponseError(
                "private-detail",
                dispatched=True,
                status_code=400,
                provider_code=400,
                category="no_data",
            )
        return []

    runtime._acquire = acquire
    runtime.adapter.no_data_evidence = lambda member, context: (
        evidence() if callable(evidence) else evidence
    )
    if outcome is not None:
        original = source.outcome
        source.outcome = lambda plan_id, **kwargs: outcome(original, plan_id, **kwargs)
    plan = _plan()
    plan["parts"][0]["member_keys"] = ["AAPL", "ACA"]
    runtime.state.record_plan(plan, runtime.provider)
    with runtime.state.connection() as db:
        db.execute(
            "UPDATE full_work SET attempts=? WHERE member_key='ACA'",
            (FULL_WORK_NO_DATA_MAX_ATTEMPTS - 1,),
        )
    _skip_waits(runtime)
    try:
        with owned_unit_work(runtime.provider, tmp_path):
            assert runtime._deliver_plan(plan, _members("AAPL", "ACA"), runtime.feeds[0])
        assert _statuses(runtime)["ACA"][:2] == ("manual", "no_data_exhausted")
        return [(row["member_key"], row["reason"], row.get("evidence")) for row in source.outcomes]
    finally:
        runtime.close()


EVIDENCE = {
    "url": NASDAQ_HALTS_URL,
    "sha256": "0" * 64,
    "observed_at": "2026-11-25T09:00:00+00:00",
    "source_symbol": "ACA",
    "trade_date": "2026-10-01",
    "source_status": "halted",
    "source_excerpt": "{}",
}


def test_official_halt_evidence_is_the_final_outcome(tmp_path):
    assert _exhaust(tmp_path, EVIDENCE)[-2:] == [
        ("ACA", "source_error", None),
        ("ACA", "halted", EVIDENCE),
    ]


@pytest.mark.parametrize(
    "evidence",
    [
        None,
        lambda: (_ for _ in ()).throw(httpx.ReadError("halts feed unreachable")),
        lambda: (_ for _ in ()).throw(UniverseSnapshotError("halts feed is not XML")),
    ],
    ids=["not_halted", "feed_unreachable", "feed_invalid"],
)
def test_without_evidence_the_member_reports_a_final_blocked_outcome(tmp_path, evidence):
    assert _exhaust(tmp_path, evidence)[-1] == ("ACA", "no_data_exhausted", None)


def test_evidence_source_refuses_falls_back_to_the_final_blocked_outcome(tmp_path):
    def outcome(original, plan_id, **kwargs):
        if kwargs.get("reason") == "halted":
            original(plan_id, **kwargs)
            raise FullMarketProtocolError("evidence refused", status_code=422)
        return original(plan_id, **kwargs)

    reports = _exhaust(tmp_path, EVIDENCE, outcome)
    assert [reason for _, reason, _ in reports[-2:]] == ["halted", "no_data_exhausted"]
