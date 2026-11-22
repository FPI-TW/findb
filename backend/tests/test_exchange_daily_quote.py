"""An exchange's daily quotation: its report is archived once, each line stored from it."""

from __future__ import annotations

import gzip
import hashlib
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from botocore.exceptions import EndpointConnectionError
from pydantic import ValidationError
from sqlalchemy import func, select

from app.models.canonical import ExchangeDailyQuote, ExchangeReportArchive, Instrument
from app.models.registry import DatasetRegistry, DeliveryMemberEvidence, DQIssue, IngestionRun
from app.schemas.ingress import ExchangeDailyQuotePayload
from app.services import exchange_reports
from app.services.ingestion import CONTRACT_NORMALIZER_MAP, IngestionService
from app.services.ingress_contracts import get_contract_json_schema, validate_ingress_request
from app.services.normalize import exchange_quote
from app.services.normalize.exchange_quote import ExchangeDailyQuoteNormalizer
from app.services.object_storage import ObjectStorageError
from tests.intraday_support import ACCOUNT, CANONICAL_BUCKET, RAW_BUCKET, Stores

DATASET = "hk_equity_official_eod"
DAY = date(2026, 11, 20)
FETCHED = datetime(2026, 11, 20, 13, 5, tzinfo=timezone.utc)
REPORT = b"<html><pre>  700 TENCENT  HKD  344.00 ...</pre></html>\n" * 50


def dataset_config() -> dict:
    return {
        "schema_id": "exchange_daily_quote",
        "accepted_schema_versions": [1],
        "current_schema_version": 1,
        "schema_enforcement": "enforce",
        "allowed_sources": ["hkex"],
        "defaults": {"market": "HK", "asset_class": "equity"},
    }


def quote(symbol: str = "00700", **values) -> dict:
    return {
        "symbol": symbol,
        "trade_date": DAY.isoformat(),
        "name": "TENCENT",
        "currency": "HKD",
        "trading_status": "traded",
        "previous_close": "352.00",
        "close": "344.00",
        "ask": "344.20",
        "bid": "344.00",
        "high": "349.60",
        "low": "339.60",
        "volume": 41094469,
        "turnover": "14089992865",
        **values,
    }


def payload(rows: list[dict], report: bytes = REPORT, *, day: date = DAY, **report_values):
    body = gzip.compress(report, mtime=0)
    digest = hashlib.sha256(body).hexdigest()
    return body, {
        "batch": {
            "data_date": day.isoformat(),
            "delivery_mode": "incremental",
            "declared_record_count": len(rows),
            "source_raw_ref": f"r2://{ACCOUNT}/{RAW_BUCKET}/hkex/{DATASET}/0123456789abcdef/{digest}.htm.gz",
            "source_raw_sha256": digest,
            "report": {
                "board": "main",
                "url": f"https://www.hkex.com.hk/eng/stat/smstat/dayquot/d{day:%y%m%d}e.htm",
                "original_sha256": hashlib.sha256(report).hexdigest(),
                "original_bytes": len(report),
                "gzip_bytes": len(body),
                "last_modified": "2026-11-20T12:00:04Z",
                "etag": '"0a7b6aee678dd1:0"',
                **report_values,
            },
        },
        "data": rows,
    }


def stub_stores(monkeypatch) -> Stores:
    """Point the normalizer's Raw reader and Canonical publisher at in-memory buckets."""
    stores = Stores()
    monkeypatch.setattr(exchange_quote, "raw_reader", lambda: stores.raw)
    monkeypatch.setattr(exchange_quote, "canonical_publisher", lambda: stores.canonical)
    return stores


async def seed_dataset(test_session) -> None:
    test_session.add(
        DatasetRegistry(
            dataset_key=DATASET,
            name="HK official EOD",
            asset_class="equity",
            market="HK",
            frequency="daily",
            is_active=True,
            config=dataset_config(),
        )
    )
    await test_session.commit()


@pytest.fixture
def stores(monkeypatch) -> Stores:
    return stub_stores(monkeypatch)


@pytest.fixture
async def dataset(test_session) -> None:
    await seed_dataset(test_session)


async def deliver(
    test_session,
    stores: Stores,
    rows: list[dict] | None = None,
    *,
    report: bytes = REPORT,
    fetched_at: datetime = FETCHED,
    stage: bool = True,
    day: date = DAY,
    commit: bool = True,
    **report_values,
):
    """Stage the gzip like the producer, then normalize one delivery citing it."""
    body, value = payload(rows or [quote()], report, day=day, **report_values)
    if stage:
        key = value["batch"]["source_raw_ref"].split(f"{RAW_BUCKET}/", 1)[1]
        stores.r2.objects[(RAW_BUCKET, key)] = body
    run = await IngestionService(test_session).create_ingestion_run(
        DATASET,
        source="hkex",
        raw_records=len(value["data"]),
        schema_id="exchange_daily_quote",
        schema_version=1,
        batch_data_date=day,
        delivery_mode="incremental",
    )
    await test_session.commit()
    run_id = run.run_id
    normalizer = ExchangeDailyQuoteNormalizer(
        test_session,
        {**dataset_config(), "_ingest_source": "hkex", "_ingest_fetched_at": fetched_at},
    )
    result = await normalizer.process(value, run_id, commit=commit)
    return run_id, result


async def _quotes(test_session) -> list[ExchangeDailyQuote]:
    test_session.expire_all()
    return list(
        await test_session.scalars(select(ExchangeDailyQuote).order_by(ExchangeDailyQuote.close))
    )


async def _archives(test_session) -> list[ExchangeReportArchive]:
    return list(await test_session.scalars(select(ExchangeReportArchive)))


async def _issues(test_session, run_id) -> list[tuple[str, str]]:
    return [
        (issue.issue_type, issue.severity)
        for issue in await test_session.scalars(select(DQIssue).where(DQIssue.run_id == run_id))
    ]


def test_contract_is_registered_and_carries_its_report():
    assert ("exchange_daily_quote", 1) in CONTRACT_NORMALIZER_MAP
    _, value = payload([quote()])
    request = {
        "dataset_key": DATASET,
        "schema_id": "exchange_daily_quote",
        "schema_version": 1,
        "source": "hkex",
        "request_key": "hkex-1",
        "idempotency_key": "hkex-1",
        "fetched_at": FETCHED.isoformat(),
        "payload": value,
    }
    validated = validate_ingress_request(request)
    assert validated.payload.batch.report.board == "main"
    rules = {
        rule["id"]
        for rule in get_contract_json_schema("exchange_daily_quote", 1)["x-findb-semantic-rules"]
    }
    assert "quote.report.raw_object" in rules and not any(
        rule.startswith("row.ohlc.") for rule in rules
    )

    for mutate in (
        lambda body: body["data"][0].update(volume=0),
        lambda body: body["data"][0].update(trade_date="2026-11-19"),
        lambda body: body["data"][0].update(symbol="700"),
        lambda body: body["batch"].pop("source_raw_ref"),
        lambda body: body["batch"]["report"].update(url="https://example.com/d261120e.htm"),
        lambda body: body["batch"]["report"].update(last_modified="2026-11-20T12:00:04"),
        lambda body: body["data"].append(deepcopy(body["data"][0])),
    ):
        broken = deepcopy(request)
        mutate(broken["payload"])
        broken["payload"]["batch"]["declared_record_count"] = len(broken["payload"]["data"])
        with pytest.raises(ValidationError):
            validate_ingress_request(broken)


@pytest.mark.asyncio
async def test_first_delivery_archives_the_report_and_stores_its_line(
    test_session, stores, dataset
):
    run_id, result = await deliver(test_session, stores)

    assert (result.success_records, result.failed_records) == (1, 0)
    (archive,) = await _archives(test_session)
    body, value = payload([quote()])
    key = (
        f"archive/v1/hkex/daily_quotation/board=main/year=2026/20261120/"
        f"{value['batch']['source_raw_sha256']}.htm.gz"
    )
    assert archive.object_key == key and archive.run_id == run_id
    assert archive.original_sha256 == hashlib.sha256(REPORT).hexdigest()
    assert stores.canonical_objects() == {key: body}
    assert archive.object_etag == stores.canonical.version(key)
    archive_id = archive.archive_id
    (stored,) = await _quotes(test_session)
    assert (stored.close, stored.high, stored.volume, stored.turnover) == (
        Decimal("344.00"),
        Decimal("349.60"),
        41094469,
        Decimal("14089992865"),
    )
    assert (stored.board, stored.source, stored.archive_id) == ("main", "hkex", archive_id)
    instrument = await test_session.get(Instrument, stored.instrument_id)
    assert (instrument.symbol, instrument.market) == ("00700", "HK")
    run = await test_session.get(IngestionRun, run_id)
    assert run.status == "completed"
    evidence = set(
        await test_session.scalars(
            select(DeliveryMemberEvidence.member_key).where(DeliveryMemberEvidence.run_id == run_id)
        )
    )
    assert evidence == {"00700"}


@pytest.mark.asyncio
async def test_later_members_of_the_same_report_do_not_archive_it_again(
    test_session, stores, dataset
):
    await deliver(test_session, stores, [quote()])
    writes = stores.writes()

    _, result = await deliver(test_session, stores, [quote("00005", name="HSBC", close="129.20")])

    assert result.success_records == 1
    assert stores.writes() == writes
    assert len(await _archives(test_session)) == 1
    assert len(await _quotes(test_session)) == 2


@pytest.mark.asyncio
async def test_an_identical_object_left_by_a_failed_attempt_is_adopted(
    test_session, stores, dataset
):
    body, value = payload([quote()])
    key = exchange_reports.archive_key(
        "hkex", ExchangeDailyQuotePayload.model_validate(value).batch
    )
    stores.r2.objects[(CANONICAL_BUCKET, key)] = body

    _, result = await deliver(test_session, stores)

    assert result.success_records == 1
    (archive,) = await _archives(test_session)
    assert archive.object_etag == stores.canonical.version(key)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "rule"),
    [
        ({"original_sha256": "0" * 64}, "original_sha256"),
        ({"original_bytes": len(REPORT) + 1}, "original_bytes"),
        ({"stage": False}, "raw_object"),
    ],
)
async def test_a_report_that_is_not_what_the_delivery_says_stores_nothing(
    test_session, stores, dataset, changes, rule
):
    run_id, result = await deliver(test_session, stores, [quote(), quote("00005")], **changes)

    assert (result.success_records, result.failed_records) == (0, 2)
    assert await _issues(test_session, run_id) == [("EXCHANGE_REPORT_REJECTED", "error")]
    issue = await test_session.scalar(select(DQIssue).where(DQIssue.run_id == run_id))
    assert issue.raw_data["rule"] == rule
    assert stores.canonical_objects() == {}
    assert await _archives(test_session) == [] and await _quotes(test_session) == []
    run = await test_session.get(IngestionRun, run_id)
    assert run.status == "completed_with_errors"


@pytest.mark.asyncio
async def test_the_same_report_cited_for_another_day_is_rejected(test_session, stores, dataset):
    await deliver(test_session, stores)

    run_id, result = await deliver(
        test_session, stores, [quote(trade_date="2026-11-19")], day=date(2026, 11, 19)
    )

    assert result.failed_records == 1
    issue = await test_session.scalar(select(DQIssue).where(DQIssue.run_id == run_id))
    assert issue.raw_data["rule"] == "report_identity"


@pytest.mark.asyncio
async def test_a_storage_failure_is_retried_and_indexes_nothing(test_session, stores, dataset):
    stores.r2.failure = EndpointConnectionError(endpoint_url="https://r2.invalid")

    with pytest.raises(ObjectStorageError):
        await deliver(test_session, stores)

    stores.r2.failure = None
    assert await _archives(test_session) == [] and await _quotes(test_session) == []


@pytest.mark.asyncio
async def test_an_older_fetch_never_replaces_a_newer_line(test_session, stores, dataset):
    await deliver(test_session, stores, [quote(close="344.00")])

    run_id, result = await deliver(
        test_session, stores, [quote(close="300.00")], fetched_at=FETCHED - timedelta(hours=1)
    )

    assert (result.success_records, result.precedence_rejected_records) == (0, 1)
    (stored,) = await _quotes(test_session)
    assert stored.close == Decimal("344.00")
    assert ("SOURCE_PRECEDENCE_REJECTED", "warning") in await _issues(test_session, run_id)


@pytest.mark.asyncio
async def test_only_contradictions_block_a_line(test_session, stores, dataset):
    rows = [
        # The close of a non-auction security can sit outside high/low.
        quote("00026", close="56.60", high="56.00", low="56.00", volume=1601, turnover="89656"),
        # Odd-lot trades put the average price outside high/low.
        quote("02028", close="4.00", high="4.66", low="3.80", volume=1000, turnover="940"),
        quote("00005", high="120.00", low="130.00", volume=100, turnover="12500"),
        quote("00001", trading_status="no_trade", volume=0, turnover="1000", high=None, low=None),
        quote("00002", trading_status="no_trade", volume=0, turnover="0", high=None, low=None),
    ]
    run_id, result = await deliver(test_session, stores, rows)

    assert (result.success_records, result.failed_records) == (3, 2)
    # A blocked line keeps all of its issues; a high below the low leaves no valid range.
    assert sorted(await _issues(test_session, run_id)) == [
        ("QUOTE_AVERAGE_PRICE_OUTSIDE_RANGE", "warning"),
        ("QUOTE_AVERAGE_PRICE_OUTSIDE_RANGE", "warning"),
        ("QUOTE_HIGH_BELOW_LOW", "error"),
        ("QUOTE_TURNOVER_WITHOUT_VOLUME", "error"),
    ]
    stored = await test_session.scalar(select(func.count()).select_from(ExchangeDailyQuote))
    assert stored == 3
