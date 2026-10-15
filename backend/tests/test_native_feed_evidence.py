"""Native acceptance must bind durable lineage to the requested profile."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from scripts import export_native_feed_evidence as evidence
from tests.test_execution_profiles import (
    test_shared_seven_feed_source_queue_canonical_serve as populate_pipeline,
)
from tests.test_full_market import DAY
from tests.test_staging_feed_evidence import _sample_record


@pytest.mark.parametrize("environment", ["staging", "production"])
@pytest.mark.parametrize(
    "key",
    [
        "us_equity_eod",
        "hk_equity_eod",
        "tw_equity_eod",
        "tw_etf_eod",
        "tw_equity_minute",
        "tw_etf_minute",
        "tw_futures_eod",
    ],
)
async def test_native_evidence_binds_profile_after_canonical_overwrite(
    test_session, test_engine, client, monkeypatch, environment, key
):
    original_post = client.post

    async def post_with_raw_provenance(url, **kwargs):
        if url == "/api/v1/source/ingest":
            request = kwargs["json"]
            batch = request["payload"]["batch"]
            batch["source_raw_ref"] = (
                "r2://"
                + "a" * 32
                + "/fixture-raw/"
                + request["source"]
                + "/"
                + request["dataset_key"]
                + "/fixture/"
                + "b" * 64
                + ".json"
            )
            batch["source_raw_sha256"] = "b" * 64
        return await original_post(url, **kwargs)

    monkeypatch.setattr(client, "post", post_with_raw_provenance)
    await populate_pipeline(test_session, test_engine, client, monkeypatch, environment, key)
    monkeypatch.setattr(evidence.get_settings(), "APP_ENVIRONMENT", environment)
    await test_session.execute(text("UPDATE normalization_outbox SET status='published'"))
    await test_session.commit()
    monkeypatch.setattr(
        evidence, "async_session_maker", async_sessionmaker(test_engine, expire_on_commit=False)
    )
    monkeypatch.setattr(
        evidence,
        "_serve_probe",
        lambda schema, sample: {
            "success": bool(evidence._canonical_projection(schema, sample["sample_record"]))
        },
    )
    results = []
    for profile in ("pilot", "full_market"):
        result = await evidence.export(
            {
                "environment": environment,
                "execution_profile": profile,
                "trade_date": DAY.isoformat(),
            }
        )
        assert len(result["feeds"]) == 7
        assert result["passed"] is False  # Other six feeds lack plans.
        feed = next(f for f in result["feeds"] if f["dataset_key"] == key)
        assert feed["passed"] is True, feed
        assert feed["plan"]["execution_profile"] == profile
        assert feed["plan"]["summary"]["data"] > 0
        assert feed["sample"]["success_records"] > 0
        assert feed["sample"]["source_request_sha256"]
        results.append(feed)
    assert results[0]["sample"]["run_id"] != results[1]["sample"]["run_id"]
    assert results[0]["sample"]["sample_run_id"] == results[1]["sample"]["run_id"]
    assert results[0]["plan"]["parts_sha256"] != results[1]["plan"]["parts_sha256"]
    if key.endswith("_minute"):
        await test_session.execute(
            text("UPDATE ingestion_run SET sequence_count=2 WHERE run_id=CAST(:run_id AS uuid)"),
            {"run_id": results[1]["sample"]["run_id"]},
        )
        await test_session.commit()
        incomplete = await evidence.export(
            {
                "environment": environment,
                "execution_profile": "pilot",
                "trade_date": DAY.isoformat(),
            }
        )
        feed = next(f for f in incomplete["feeds"] if f["dataset_key"] == key)
        assert feed["plan"]["summary"]["data"] > 0  # Original durable proof survives.
        assert feed["passed"] is False
        assert feed["sample"] is None
        assert feed["reason"] == "nonzero_native_sample_missing"


@pytest.mark.parametrize("schema", ["market_eod", "market_minute", "futures_eod"])
@pytest.mark.parametrize("mode", ["ok", "mismatch", "http_error", "oversize", "failure"])
def test_native_serve_exact_projection_and_failures(monkeypatch, schema, mode):
    import json
    from io import BytesIO

    record = _sample_record(schema)
    sample = {
        "sample_instrument_id": record["instrument_id"],
        "sample_trade_date": record["trade_date"],
        "sample_contract_code": record.get("contract_code"),
        "sample_session": record.get("session"),
        "sample_record": record,
    }
    returned = dict(record)
    if mode == "mismatch":
        returned["close"] = "999"
    raw = (
        b"x" * (4 * 1024 * 1024 + 1)
        if mode == "oversize"
        else json.dumps({"success": True, "data": [returned]}).encode()
    )
    stream = BytesIO(raw)
    stream.status = 503 if mode == "http_error" else 200

    def open_response(request, timeout):
        assert timeout == 10
        assert request.headers["X-api-key"] == "fixture-key"
        if mode == "failure":
            raise RuntimeError("sensitive failure must not escape")
        return stream

    monkeypatch.setenv("FINDB_STATIC_CACHE_SERVE_API_KEY", "fixture-key")
    monkeypatch.setattr(evidence, "urlopen", open_response)
    result = evidence._serve_probe(schema, sample)
    assert result["success"] is (mode == "ok")
    assert "sensitive" not in str(result)
    if mode != "failure":
        assert stream.closed
