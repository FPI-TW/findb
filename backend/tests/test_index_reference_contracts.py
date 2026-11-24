"""Index reference contracts: whole publisher documents, delivered as published."""

from __future__ import annotations

from copy import deepcopy
from datetime import date

import pytest
from pydantic import ValidationError

from app.schemas.ingress import (
    INDEX_REFERENCE_REQUESTS,
    INDEX_REFERENCE_SCHEMAS,
    IndexChangesIngressRequest,
    IndexConstituentsIngressRequest,
    IndexReviewIngressRequest,
)
from app.services.delivery_policy import DeliveryExpectation, evaluate_delivery_policy
from app.services.ingress_contracts import get_contract_json_schema, validate_ingress_request
from tests.index_reference_support import (
    HSCI_UTILITIES,
    HSI,
    HSI_FINANCE,
    change,
    changes_payload,
    constituent,
    constituents_payload,
    request,
    review_payload,
)


def _validate(schema_id: str, payload: dict):
    return validate_ingress_request(request(schema_id, "hk_index_reference", payload))


def _rejected(schema_id: str, payload: dict) -> str:
    with pytest.raises(ValidationError) as rejected:
        _validate(schema_id, payload)
    return str(rejected.value)


def test_each_contract_dispatches_to_its_request_model():
    assert isinstance(
        _validate("index_constituents", constituents_payload()), IndexConstituentsIngressRequest
    )
    assert isinstance(
        _validate("index_constituent_changes", changes_payload()), IndexChangesIngressRequest
    )
    assert isinstance(
        _validate("index_review_announcement", review_payload()), IndexReviewIngressRequest
    )
    assert set(INDEX_REFERENCE_SCHEMAS) == {
        model.model_fields["schema_id"].annotation.__args__[0]  # type: ignore[union-attr]
        for model in INDEX_REFERENCE_REQUESTS
    }


def test_document_evidence_accepts_a_publisher_without_cache_headers():
    validated = _validate("index_constituents", constituents_payload())

    evidence = validated.payload.batch.document  # type: ignore[union-attr]
    assert (evidence.publisher, evidence.etag, evidence.last_modified) == ("hsil", None, None)


def test_document_from_after_its_observation_is_rejected():
    payload = constituents_payload(trade_date=date(2026, 11, 25))
    for row in payload["data"]:
        row["trade_date"] = "2026-11-25"

    assert "document_date must not follow" in _rejected("index_constituents", payload)


def test_document_url_must_be_the_publishers():
    payload = constituents_payload()
    payload["batch"]["document"]["url"] = "https://example.com/con_23Nov26.pdf"

    assert "url" in _rejected("index_constituents", payload)


def test_review_release_has_no_series_and_reports_name_theirs():
    payload = constituents_payload()
    payload["batch"]["document"]["series"] = None
    assert "series" in _rejected("index_constituents", payload)

    release = review_payload()
    release["batch"]["document"]["series"] = "hsi"
    assert "series" in _rejected("index_review_announcement", release)


def test_constituent_report_lists_exactly_its_declared_counts():
    payload = constituents_payload()
    payload["batch"]["indexes"][0]["constituent_count"] = 3

    assert "declared constituent_count" in _rejected("index_constituents", payload)


def test_constituent_rows_are_unique_per_index():
    rows = [constituent("00001.00", "00005", "60"), constituent("00001.00", "00005", "40")]
    payload = constituents_payload(
        rows, [{"code": "00001.00", "name_en": "HSI", "constituent_count": 2}]
    )

    assert "duplicate (index_code, symbol)" in _rejected("index_constituents", payload)


def test_constituent_rows_are_of_the_reports_trading_day():
    payload = constituents_payload()
    payload["data"][0]["trade_date"] = "2026-11-20"

    assert "document.document_date" in _rejected("index_constituents", payload)


def test_constituent_parent_must_be_listed_and_report_is_whole():
    payload = constituents_payload()
    payload["batch"]["indexes"][1]["parent_code"] = "00001.09"
    assert "parent index" in _rejected("index_constituents", payload)

    payload = constituents_payload()
    payload["batch"]["document"]["part_key"] = "00001.00"
    assert "delivered whole" in _rejected("index_constituents", payload)


def test_constituent_weight_and_industry_are_bounded():
    payload = constituents_payload()
    payload["data"][0]["weight_pct"] = "100.5"
    assert "weight_pct" in _rejected("index_constituents", payload)

    payload = constituents_payload()
    payload["data"][0]["industry"] = {"scheme": "gics", "code": "40", "name_en": "Utilities"}
    assert "industry" in _rejected("index_constituents", payload)


def test_change_workbook_is_one_index_with_its_whole_history():
    payload = changes_payload(index=HSCI_UTILITIES)
    payload["batch"]["document"]["part_key"] = "00011.07"
    assert isinstance(_validate("index_constituent_changes", payload), IndexChangesIngressRequest)

    payload["batch"]["document"]["part_key"] = "document"
    assert "one index at a time" in _rejected("index_constituent_changes", payload)


def test_change_history_start_is_its_earliest_effective_date():
    payload = changes_payload(history_start="2008-09-08")

    assert "history_start" in _rejected("index_constituent_changes", payload)


def test_change_dates_do_not_follow_the_workbook():
    payload = changes_payload(document_date=date(2026, 10, 22))

    assert "follow document.document_date" in _rejected("index_constituent_changes", payload)


def test_change_rows_are_unique():
    rows = [
        change(1, "2026-10-23", "delete", "02555", 580),
        change(1, "2008-10-08", "add", "00005", 581),
    ]
    assert "duplicate ordinal" in _rejected("index_constituent_changes", changes_payload(rows))

    rows = [
        change(1, "2026-10-23", "delete", "02555", 580),
        change(2, "2026-10-23", "delete", "02555", 580),
        change(3, "2008-10-08", "add", "00005", 581),
    ]
    assert "duplicate (effective_date, symbol, action)" in _rejected(
        "index_constituent_changes", changes_payload(rows)
    )


def test_review_counts_must_balance_the_announced_changes():
    payload = review_payload()
    payload["batch"]["indexes"][1]["constituents_after"] = 585

    assert "constituents_before plus net changes" in _rejected("index_review_announcement", payload)


def test_review_without_changes_still_states_its_counts():
    indexes = review_payload()["batch"]["indexes"][:1]

    validated = _validate("index_review_announcement", review_payload([], indexes))

    assert validated.payload.data == []


def test_review_changes_name_a_decided_index_and_take_effect_later():
    payload = review_payload()
    payload["data"][0]["index_code"] = "00014.00"
    assert "decided index" in _rejected("index_review_announcement", payload)

    payload = review_payload()
    payload["batch"]["indexes"][0]["effective_date"] = "2026-12-04"
    assert "effective_date must follow" in _rejected("index_review_announcement", payload)

    payload = review_payload()
    payload["batch"]["indexes"][0]["implemented_after_close"] = "2026-11-19"
    payload["batch"]["indexes"][0]["effective_date"] = "2026-11-20"
    assert "after its publication date" in _rejected("index_review_announcement", payload)


@pytest.mark.parametrize("schema_id", sorted(INDEX_REFERENCE_SCHEMAS))
def test_published_schema_states_reference_rules(schema_id: str):
    schema = get_contract_json_schema(schema_id, 1)
    rules = {rule["id"]: rule for rule in schema["x-findb-semantic-rules"]}

    assert {"reference.document.raw_object", "reference.document.dates"} <= set(rules)
    assert (
        rules["reference.document.part"]["parameters"]["kind"] == INDEX_REFERENCE_SCHEMAS[schema_id]
    )
    assert not any(
        rule_id.startswith(("row.ohlc.", "payload.coverage.", "batch.sequence."))
        for rule_id in rules
    )
    paths = schema["x-findb-transformations"][0]["paths"]
    assert "payload.batch.document.url" in paths
    assert "payload.data[*].symbol" in paths


def test_payload_without_rows_is_rejected_for_documents_that_list_rows():
    payload = deepcopy(constituents_payload())
    payload["data"] = []
    payload["batch"]["declared_record_count"] = 0

    assert "data" in _rejected("index_constituents", payload)


def test_change_rows_of_a_date_agree_on_count_and_stated_changes():
    rows = [
        change(1, "2026-09-07", "add", "01347", 95, count_change=2),
        change(2, "2026-09-07", "add", "02338", 94, count_change=2),
        change(3, "2008-10-08", "add", "00005", 93),
    ]
    assert "same constituents_after" in _rejected(
        "index_constituent_changes", changes_payload(rows, document_date=date(2026, 9, 7))
    )

    rows = [
        change(1, "2026-09-07", "add", "01347", 95, count_change=1),
        change(2, "2026-09-07", "add", "02338", 95, count_change=1),
        change(3, "2026-09-07", "delete", "00017", 95, count_change=-1),
        change(4, "2008-10-08", "add", "00005", 94),
    ]
    assert "rows of its action" in _rejected(
        "index_constituent_changes", changes_payload(rows, document_date=date(2026, 9, 7))
    )

    rows[0]["count_change"] = rows[1]["count_change"] = 2
    validated = _validate(
        "index_constituent_changes", changes_payload(rows, document_date=date(2026, 9, 7))
    )
    assert len(validated.payload.data) == 4

    rows[2]["count_change"] = 1
    assert "positive for additions" in _rejected(
        "index_constituent_changes", changes_payload(rows, document_date=date(2026, 9, 7))
    )


def test_a_parent_index_is_itself_top_level():
    indexes = [
        {**HSI, "constituent_count": 2},
        {**HSI_FINANCE, "constituent_count": 1},
        {
            "code": "00001.09",
            "name_en": "Nested",
            "parent_code": "00001.01",
            "constituent_count": 1,
        },
    ]
    rows = [
        constituent("00001.00", "00005", "60"),
        constituent("00001.00", "00011", "40"),
        constituent("00001.01", "00005", "100"),
        constituent("00001.09", "00005", "100"),
    ]

    assert "top-level index" in _rejected("index_constituents", constituents_payload(rows, indexes))


def test_review_document_date_is_the_publisher_local_publication_date():
    payload = review_payload()
    payload["batch"]["published_at"] = "2026-11-19T17:30:00Z"  # 2026-11-20 01:30 in Hong Kong
    assert isinstance(_validate("index_review_announcement", payload), IndexReviewIngressRequest)

    payload["batch"]["published_at"] = "2026-11-19T09:45:00Z"
    assert "publisher-local date" in _rejected("index_review_announcement", payload)


def test_constituent_values_fit_their_stored_precision():
    payload = constituents_payload()
    payload["data"][0]["weight_pct"] = "59.99995"
    payload["data"][1]["weight_pct"] = "40.00005"
    assert "decimal" in _rejected("index_constituents", payload)

    payload = constituents_payload()
    payload["data"][0]["close"] = "10.1234567"
    assert "decimal" in _rejected("index_constituents", payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("schema_id", "payload"),
    [
        ("index_constituents", constituents_payload()),
        ("index_constituent_changes", changes_payload()),
        ("index_review_announcement", review_payload([], review_payload()["batch"]["indexes"][:1])),
    ],
)
async def test_delivery_policy_does_not_judge_a_document_by_market_baselines(schema_id, payload):
    if schema_id == "index_review_announcement":
        payload["batch"]["indexes"][0]["constituents_after"] = 95
    expectation = DeliveryExpectation.model_validate(
        {"record_count": {"minimum_record_count": 5, "action": "reject"}}
    )

    result = await evaluate_delivery_policy(None, _validate(schema_id, payload), expectation)  # type: ignore[arg-type]

    assert (result.outcome, result.baseline_status, result.violations) == ("pass", "disabled", [])
