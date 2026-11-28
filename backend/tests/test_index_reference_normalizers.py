"""Index reference deliveries: archived once, stored as published, history rebuilt."""

from __future__ import annotations

import gzip
import hashlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.canonical import Instrument
from app.models.index_reference import (
    IndexChangeEvent,
    IndexDefinition,
    IndexMembership,
    IndexMembershipBuild,
    IndexSnapshot,
    IndexSnapshotConstituent,
    ReferenceDocumentArchive,
)
from app.models.raw import RawMarketPayload
from app.models.registry import (
    DatasetRegistry,
    DeliveryMemberEvidence,
    DQIssue,
    IngestionRun,
)
from app.services.ingestion import CONTRACT_NORMALIZER_MAP, IngestionService
from app.services.normalize import index_reference
from app.services.normalize.index_reference import (
    IndexChangesNormalizer,
    IndexConstituentsNormalizer,
)
from app.utils import uuid7
from tests.index_reference_support import (
    BODIES,
    HSCI,
    HSCI_UTILITIES,
    HSI,
    HSI_FINANCE,
    change,
    changes_payload,
    constituent,
    constituents_payload,
)
from tests.intraday_support import RAW_BUCKET, Stores

CONSTITUENTS = "hk_index_constituents"
CHANGES = "hk_index_changes"
FETCHED = datetime(2026, 11, 24, 14, 30, tzinfo=timezone.utc)
HSI_UTILITIES = {
    "code": "00001.02",
    "name_en": "Hang Seng Index - Utilities",
    "parent_code": "00001.00",
}


def config(schema_id: str) -> dict:
    return {
        "schema_id": schema_id,
        "accepted_schema_versions": [1],
        "current_schema_version": 1,
        "schema_enforcement": "enforce",
        "allowed_sources": ["hkex"],
        "defaults": {"market": "HK", "asset_class": "index"},
    }


@pytest.fixture
def stores(monkeypatch) -> Stores:
    stores = Stores()
    monkeypatch.setattr(index_reference, "raw_reader", lambda: stores.raw)
    monkeypatch.setattr(index_reference, "canonical_publisher", lambda: stores.canonical)
    return stores


@pytest.fixture
async def datasets(test_session) -> None:
    for key, schema_id in (
        (CONSTITUENTS, "index_constituents"),
        (CHANGES, "index_constituent_changes"),
    ):
        test_session.add(
            DatasetRegistry(
                dataset_key=key,
                name=key,
                asset_class="index",
                market="HK",
                frequency="daily",
                is_active=True,
                config=config(schema_id),
            )
        )
    await test_session.commit()


async def deliver(
    test_session, stores: Stores, value: dict, *, stage: bool = True, raw: bool = False
):
    """Stage the gzip like the producer, then normalize one delivery citing it.

    ``raw`` also keeps the accepted payload, as Source does before normalizing.
    """
    changes = "index" in value["batch"]
    dataset, schema_id = (
        (CHANGES, "index_constituent_changes") if changes else (CONSTITUENTS, "index_constituents")
    )
    if stage:
        digest = value["batch"]["source_raw_sha256"]
        key = value["batch"]["source_raw_ref"].split(f"{RAW_BUCKET}/", 1)[1]
        stores.r2.objects[(RAW_BUCKET, key)] = BODIES[digest]
    raw_payload_id, run_id = (uuid7() if raw else None), uuid7()
    if raw_payload_id is not None:
        test_session.add(
            RawMarketPayload(
                raw_payload_id=raw_payload_id,
                dataset_key=dataset,
                source="hkex",
                request_key=f"{schema_id}-{raw_payload_id}",
                idempotency_key=f"{schema_id}-{raw_payload_id}",
                schema_id=schema_id,
                schema_version=1,
                payload=value,
                fetched_at=FETCHED,
                expire_at=FETCHED + timedelta(days=30),
                run_id=run_id,
            )
        )
        await test_session.flush()
    run = await IngestionService(test_session).create_ingestion_run(
        dataset,
        source="hkex",
        raw_records=len(value["data"]),
        schema_id=schema_id,
        schema_version=1,
        batch_data_date=date.fromisoformat(value["batch"]["data_date"]),
        delivery_mode="full_snapshot",
        run_id=run_id,
        raw_payload_id=raw_payload_id,
    )
    await test_session.commit()
    run_id = run.run_id
    normalizer_type = IndexChangesNormalizer if changes else IndexConstituentsNormalizer
    normalizer = normalizer_type(
        test_session, {**config(schema_id), "_ingest_source": "hkex", "_ingest_fetched_at": FETCHED}
    )
    result = await normalizer.process(value, run_id)
    test_session.expire_all()
    return run_id, result


async def issues(test_session, run_id) -> list[tuple[str, str]]:
    """The run's DQ issues, less the expected one about codes with no instrument."""
    return sorted(
        (issue.issue_type, issue.severity)
        for issue in await test_session.scalars(select(DQIssue).where(DQIssue.run_id == run_id))
        if issue.issue_type != "INDEX_MEMBER_UNRESOLVED"
    )


async def members(test_session, code: str) -> list[tuple[str, date | None, date | None]]:
    rows = await test_session.execute(
        select(IndexMembership.symbol, IndexMembership.valid_from, IndexMembership.valid_to)
        .join(IndexDefinition, IndexDefinition.index_id == IndexMembership.index_id)
        .where(IndexDefinition.code == code)
        .order_by(IndexMembership.symbol, IndexMembership.valid_from)
    )
    return [tuple(row) for row in rows]


async def count(test_session, model) -> int:
    return int(await test_session.scalar(select(func.count()).select_from(model)))


def hsi_report(trade_date: date = date(2026, 11, 23), **values) -> dict:
    rows = [
        constituent("00001.00", "00005", "60", trade_date=trade_date.isoformat()),
        constituent("00001.00", "00002", "40", trade_date=trade_date.isoformat()),
        constituent("00001.01", "00005", "100", trade_date=trade_date.isoformat()),
        constituent("00001.02", "00002", "100", trade_date=trade_date.isoformat()),
    ]
    indexes = [
        {**HSI, "constituent_count": 2},
        {**HSI_FINANCE, "constituent_count": 1},
        {**HSI_UTILITIES, "constituent_count": 1},
    ]
    return constituents_payload(rows, indexes, trade_date=trade_date, **values)


def test_both_contracts_route_to_their_normalizers():
    assert CONTRACT_NORMALIZER_MAP[("index_constituents", 1)] is IndexConstituentsNormalizer
    assert CONTRACT_NORMALIZER_MAP[("index_constituent_changes", 1)] is IndexChangesNormalizer


@pytest.mark.asyncio
async def test_a_report_is_archived_stored_and_anchors_the_family(test_session, stores, datasets):
    value = hsi_report()

    run_id, result = await deliver(test_session, stores, value)

    assert (result.success_records, result.failed_records) == (4, 0)
    assert await issues(test_session, run_id) == []
    (archive,) = await test_session.scalars(select(ReferenceDocumentArchive))
    digest = value["batch"]["source_raw_sha256"]
    key = f"archive/v1/hkex/hsil/constituent_report/series=hsi/year=2026/20261123/{digest}.pdf.gz"
    assert (archive.object_key, archive.publisher, archive.source_etag) == (key, "hsil", None)
    assert stores.canonical_objects() == {key: BODIES[digest]}
    definitions = {item.code: item for item in await test_session.scalars(select(IndexDefinition))}
    assert definitions["00001.01"].parent_index_id == definitions["00001.00"].index_id
    assert {item.series for item in definitions.values()} == {"hsi"}
    weights = {
        row.symbol: row.weight_pct
        for row in await test_session.scalars(
            select(IndexSnapshotConstituent)
            .join(IndexSnapshot)
            .where(IndexSnapshot.index_id == definitions["00001.00"].index_id)
        )
    }
    assert weights == {"00005": Decimal("60.0000"), "00002": Decimal("40.0000")}
    assert await members(test_session, "00001.00") == [("00002", None, None), ("00005", None, None)]
    (build,) = await test_session.scalars(select(IndexMembershipBuild))
    assert (build.status, build.anchor_trade_date, build.observed_from, build.findings) == (
        "consistent",
        date(2026, 11, 23),
        date(2026, 11, 23),
        0,
    )
    run = await test_session.get(IngestionRun, run_id)
    assert run.status == "completed"


@pytest.mark.asyncio
async def test_a_report_delivered_again_is_neither_archived_nor_stored_twice(
    test_session, stores, datasets
):
    await deliver(test_session, stores, hsi_report())
    writes = stores.writes()

    run_id, result = await deliver(test_session, stores, hsi_report())

    assert result.failed_records == 0
    assert stores.writes() == writes
    assert await count(test_session, ReferenceDocumentArchive) == 1
    assert await count(test_session, IndexSnapshot) == 3
    assert await issues(test_session, run_id) == []


@pytest.mark.asyncio
async def test_changes_roll_the_history_back_whichever_arrives_first(
    test_session, stores, datasets
):
    history = changes_payload(
        [
            change(1, "2015-03-09", "add", "00005", 2),
            change(2, "2015-03-09", "delete", "00017", 2),
        ],
        index=HSI,
        history_start="2015-03-09",
        document_date=date(2026, 9, 7),
        series="hsi",
    )

    _, early = await deliver(test_session, stores, history)
    (first_build,) = await test_session.scalars(select(IndexMembershipBuild))
    assert (early.failed_records, first_build.status) == (0, "no_anchor")
    assert await members(test_session, "00001.00") == []

    await deliver(test_session, stores, hsi_report())

    assert await members(test_session, "00001.00") == [
        ("00002", None, None),
        ("00005", date(2015, 3, 9), None),
        ("00017", None, date(2015, 3, 9)),
    ]
    (event,) = await test_session.scalars(
        select(IndexChangeEvent).where(IndexChangeEvent.symbol == "00005")
    )
    membership = await test_session.scalar(
        select(IndexMembership).where(IndexMembership.symbol == "00005")
    )
    assert membership.from_event_id == event.event_id
    definition = await test_session.scalar(
        select(IndexDefinition).where(IndexDefinition.code == "00001.00")
    )
    assert definition.history_start == date(2015, 3, 9)


@pytest.mark.asyncio
async def test_temporary_counters_meet_their_permanent_codes_by_name(
    test_session, stores, datasets
):
    # A change listed under a counter, and a counter already trading at the only report.
    history = changes_payload(
        [
            change(1, "2026-09-07", "add", "01440", 3, name_en="STAR SHINE HLDG"),
            change(2, "2026-03-09", "add", "02910", 2, name_en="ZHIDA TECH"),
        ],
        index=HSI,
        history_start="2026-03-09",
        document_date=date(2026, 9, 7),
        series="hsi",
    )
    await deliver(test_session, stores, history)
    rows = [
        constituent("00001.00", "00005", "50"),
        constituent("00001.00", "02963", "30", name_en="STAR SHINE HLDG"),
        constituent("00001.00", "02650", "20", name_en="ZHIDA TECH"),
        constituent("00001.01", "00005", "100"),
        constituent("00001.02", "02963", "60", name_en="STAR SHINE HLDG"),
        constituent("00001.02", "02650", "40", name_en="ZHIDA TECH"),
    ]
    indexes = [
        {**HSI, "constituent_count": 3},
        {**HSI_FINANCE, "constituent_count": 1},
        {**HSI_UTILITIES, "constituent_count": 2},
    ]

    run_id, result = await deliver(test_session, stores, constituents_payload(rows, indexes))

    assert result.failed_records == 0
    assert await issues(test_session, run_id) == []
    assert await members(test_session, "00001.00") == [
        ("00005", None, None),
        ("01440", date(2026, 9, 7), None),
        ("02650", date(2026, 3, 9), None),
    ]


@pytest.mark.asyncio
async def test_disagreements_are_warnings_and_the_history_is_still_rebuilt(
    test_session, stores, datasets
):
    history = changes_payload(
        [change(1, "2015-03-09", "add", "00005", 5)],
        index=HSI,
        history_start="2015-03-09",
        document_date=date(2026, 9, 7),
        series="hsi",
    )
    await deliver(test_session, stores, history)

    run_id, result = await deliver(test_session, stores, hsi_report())

    assert result.failed_records == 0
    assert await issues(test_session, run_id) == [("INDEX_MEMBERSHIP_FINDING", "warning")]
    issue = await test_session.scalar(
        select(DQIssue).where(
            DQIssue.run_id == run_id, DQIssue.issue_type == "INDEX_MEMBERSHIP_FINDING"
        )
    )
    assert issue.raw_data["rule"] == "count_after_change"
    assert issue.raw_data["sample"] == [{"day": "2015-03-09", "stated": 5, "derived": 2}]
    (build,) = await test_session.scalars(
        select(IndexMembershipBuild).where(IndexMembershipBuild.status == "consistent")
    )
    assert build.findings == 1


@pytest.mark.asyncio
async def test_a_change_no_longer_listed_is_retracted(test_session, stores, datasets):
    first = changes_payload(
        [change(1, "2026-10-23", "delete", "02555", 1), change(2, "2008-10-08", "add", "00005", 2)],
        document_date=date(2026, 10, 23),
    )
    await deliver(test_session, stores, first)
    later = changes_payload(
        [change(1, "2008-10-08", "add", "00005", 2)],
        document_date=date(2026, 11, 2),
        history_start="2008-10-08",
    )

    run_id, _ = await deliver(test_session, stores, later)

    statuses = {
        event.symbol: event.status for event in await test_session.scalars(select(IndexChangeEvent))
    }
    assert statuses == {"02555": "retracted", "00005": "effective"}
    assert ("INDEX_CHANGE_RETRACTED", "warning") in await issues(test_session, run_id)


@pytest.mark.asyncio
async def test_an_industry_index_child_needs_its_parent_only_by_code(
    test_session, stores, datasets
):
    value = changes_payload(
        [change(1, "2026-03-09", "add", "00002", 26)],
        index=HSCI_UTILITIES,
        history_start="2026-03-09",
    )

    _, result = await deliver(test_session, stores, value)

    assert result.failed_records == 0
    definitions = {item.code: item for item in await test_session.scalars(select(IndexDefinition))}
    assert definitions["00011.07"].parent_index_id == definitions["00011.00"].index_id
    assert definitions["00011.07"].classification_code == "40"
    assert definitions["00011.00"].name_en == "00011.00"

    await deliver(test_session, stores, changes_payload(index=HSCI))
    definition = await test_session.scalar(
        select(IndexDefinition).where(IndexDefinition.code == "00011.00")
    )
    assert definition.name_en == "Hang Seng Composite Index"


def test_weight_tolerance_grows_with_the_rounded_weights():
    # Four hundredth-rounded weights may miss 100 by 0.1; 580 HSCI weights were
    # published summing to 99.85.
    assert index_reference.weight_tolerance(4) == Decimal("0.1")
    assert Decimal("0.15") < index_reference.weight_tolerance(580) < Decimal("0.3")


@pytest.mark.parametrize(
    ("mutate", "rule"),
    [
        (lambda rows: rows[0].update(weight_pct="70"), "weights_do_not_sum_to_100"),
        (lambda rows: rows[3].update(symbol="00006"), "children_do_not_partition_parent"),
    ],
)
@pytest.mark.asyncio
async def test_a_report_that_contradicts_itself_stores_nothing(
    test_session, stores, datasets, mutate, rule
):
    value = hsi_report()
    mutate(value["data"])
    if rule == "weights_do_not_sum_to_100":
        value["data"][1]["weight_pct"] = "40"

    run_id, result = await deliver(test_session, stores, value)

    assert result.failed_records == result.total_records == 4
    issue = await test_session.scalar(select(DQIssue).where(DQIssue.run_id == run_id))
    assert (issue.issue_type, issue.severity, issue.raw_data["rule"]) == (
        "INDEX_DOCUMENT_REJECTED",
        "error",
        rule,
    )
    for model in (
        ReferenceDocumentArchive,
        IndexSnapshot,
        IndexDefinition,
    ):
        assert await count(test_session, model) == 0
    assert stores.canonical_objects() == {}
    run = await test_session.get(IngestionRun, run_id)
    assert run.status == "completed_with_errors"


@pytest.mark.asyncio
async def test_an_industry_column_must_match_the_industry_index(test_session, stores, datasets):
    industry = {"scheme": "hsics", "code": "50", "name_en": "Financials"}
    rows = [
        constituent("00011.00", "00002", "100", industry=industry),
        constituent("00011.07", "00002", "100"),
    ]
    indexes = [{**HSCI, "constituent_count": 1}, {**HSCI_UTILITIES, "constituent_count": 1}]

    run_id, result = await deliver(
        test_session, stores, constituents_payload(rows, indexes, series="hsci")
    )

    issue = await test_session.scalar(select(DQIssue).where(DQIssue.run_id == run_id))
    assert (result.failed_records, issue.raw_data["rule"]) == (
        2,
        "industry_differs_from_industry_index",
    )


@pytest.mark.asyncio
async def test_a_missing_raw_object_rejects_the_delivery(test_session, stores, datasets):
    run_id, result = await deliver(test_session, stores, hsi_report(), stage=False)

    issue = await test_session.scalar(select(DQIssue).where(DQIssue.run_id == run_id))
    assert (result.failed_records, issue.raw_data["rule"]) == (4, "raw_object")
    assert await count(test_session, IndexSnapshot) == 0


@pytest.mark.asyncio
async def test_current_members_resolve_to_instruments_and_the_rest_are_reported(
    test_session, stores, datasets
):
    test_session.add(Instrument(asset_class="equity", market="HK", symbol="00005", name="HSBC"))
    await test_session.commit()

    run_id, _ = await deliver(test_session, stores, hsi_report())

    resolved = {
        row.symbol: row.instrument_id is not None
        for row in await test_session.scalars(
            select(IndexMembership)
            .join(IndexDefinition, IndexDefinition.index_id == IndexMembership.index_id)
            .where(IndexDefinition.code == "00001.00")
        )
    }
    assert resolved == {"00005": True, "00002": False}
    issue = await test_session.scalar(
        select(DQIssue).where(
            DQIssue.run_id == run_id, DQIssue.issue_type == "INDEX_MEMBER_UNRESOLVED"
        )
    )
    assert issue.raw_data["symbols"] == ["00002"]


@pytest.mark.asyncio
async def test_a_document_recompressed_by_the_producer_is_proven_not_kept_twice(
    test_session, stores, datasets
):
    await deliver(test_session, stores, hsi_report())
    value = hsi_report()
    original = f"%PDF-1.4 hsi report {date(2026, 11, 23)}\n".encode()
    body = gzip.compress(original, compresslevel=1, mtime=1)
    digest = hashlib.sha256(body).hexdigest()
    BODIES[digest] = body
    value["batch"]["source_raw_sha256"] = digest
    value["batch"]["source_raw_ref"] = (
        value["batch"]["source_raw_ref"].rsplit("/", 1)[0] + f"/{digest}.pdf.gz"
    )
    value["batch"]["document"]["gzip_bytes"] = len(body)

    _, result = await deliver(test_session, stores, value)

    assert result.failed_records == 0
    assert await count(test_session, ReferenceDocumentArchive) == 1


@pytest.mark.asyncio
async def test_an_older_report_delivered_again_never_replaces_a_newer_one(
    test_session, stores, datasets
):
    first = hsi_report(original=b"%PDF-1.4 hsi report as first published\n")
    await deliver(test_session, stores, first)
    republished = hsi_report(original=b"%PDF-1.4 hsi report as republished\n")
    run_id, _ = await deliver(test_session, stores, republished)
    assert ("INDEX_SNAPSHOT_REPLACED", "warning") in await issues(test_session, run_id)
    kept = await test_session.scalar(
        select(ReferenceDocumentArchive.archive_id).where(
            ReferenceDocumentArchive.original_sha256
            == republished["batch"]["document"]["original_sha256"]
        )
    )
    builds = await count(test_session, IndexMembershipBuild)

    run_id, result = await deliver(test_session, stores, first)

    assert result.failed_records == 0
    assert ("INDEX_DOCUMENT_STALE", "warning") in await issues(test_session, run_id)
    assert set(await test_session.scalars(select(IndexSnapshot.archive_id))) == {kept}
    # Nothing it stores changed, so the family is not rebuilt.
    assert await count(test_session, IndexMembershipBuild) == builds


@pytest.mark.asyncio
async def test_an_older_workbook_delivered_again_retracts_nothing(test_session, stores, datasets):
    older = changes_payload(
        [change(1, "2008-10-08", "add", "00005", 2)], document_date=date(2026, 10, 2)
    )
    newer = changes_payload(
        [change(1, "2026-10-23", "delete", "02555", 1), change(2, "2008-10-08", "add", "00005", 2)],
        document_date=date(2026, 10, 23),
    )
    await deliver(test_session, stores, older)
    await deliver(test_session, stores, newer)

    run_id, _ = await deliver(test_session, stores, older)

    assert ("INDEX_DOCUMENT_STALE", "warning") in await issues(test_session, run_id)
    statuses = {
        event.symbol: event.status for event in await test_session.scalars(select(IndexChangeEvent))
    }
    assert statuses == {"02555": "effective", "00005": "effective"}


@pytest.mark.asyncio
async def test_a_document_that_states_no_industry_keeps_the_stored_one(
    test_session, stores, datasets
):
    await deliver(
        test_session,
        stores,
        changes_payload(
            [change(1, "2026-03-09", "add", "00002", 26)],
            index=HSCI_UTILITIES,
            history_start="2026-03-09",
        ),
    )
    bare = {
        key: value
        for key, value in HSCI_UTILITIES.items()
        if key not in {"classification", "parent_code"}
    }

    await deliver(
        test_session,
        stores,
        changes_payload(
            [change(1, "2026-03-09", "add", "00002", 26)],
            index=bare,
            history_start="2026-03-09",
            document_date=date(2026, 11, 2),
        ),
    )

    definitions = {item.code: item for item in await test_session.scalars(select(IndexDefinition))}
    assert definitions["00011.07"].classification_code == "40"
    assert definitions["00011.07"].parent_index_id == definitions["00011.00"].index_id


async def evidence(test_session, run_id) -> set[str]:
    return set(
        await test_session.scalars(
            select(DeliveryMemberEvidence.member_key).where(DeliveryMemberEvidence.run_id == run_id)
        )
    )


@pytest.mark.asyncio
async def test_a_kept_document_is_the_evidence_of_its_plan_member(test_session, stores, datasets):
    # A report stands for its series, whichever constituents it lists.
    report = hsi_report()
    first, _ = await deliver(test_session, stores, report, raw=True)
    assert await evidence(test_session, first) == {"HSI"}

    # Delivered again it stores nothing new, yet the member's document is kept.
    again, _ = await deliver(test_session, stores, report, raw=True)
    assert await evidence(test_session, again) == {"HSI"}

    # A sheet stands for its index.
    sheet, _ = await deliver(test_session, stores, changes_payload(), raw=True)
    assert await evidence(test_session, sheet) == {"00011.00"}

    # A document rejected whole stands for no member.
    rejected = hsi_report(trade_date=date(2026, 11, 20))
    rejected["data"][0]["weight_pct"] = "10"
    run_id, result = await deliver(test_session, stores, rejected, raw=True)
    assert result.failed_records == result.total_records
    assert await evidence(test_session, run_id) == set()
