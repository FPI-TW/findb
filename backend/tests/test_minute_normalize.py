"""Focused runtime checks for the provider-neutral minute normalizer."""

import hashlib
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.models.canonical import Instrument, MarketDataMinute
from app.models.registry import DatasetRegistry, DQIssue, IngestionRun, NormalizationJob
from app.schemas.ingress import minute_sequence_key_digest
from app.services.ingestion import _select_normalizer_for_payload
from app.services.normalization_queue import execute_normalization
from app.services.normalize import MarketMinuteContractNormalizer
from app.utils import utc_now, uuid7


def _config(source: str | None = "shioaji") -> dict:
    config = {
        "defaults": {
            "market": "TW",
            "asset_class": "equity",
            "currency": "TWD",
        }
    }
    if source is not None:
        config["_ingest_source"] = source
    return config


def _payload() -> dict:
    return {
        "batch": {
            "anomalies": [
                {
                    "symbol": "2330",
                    "bar_start_time": "2026-07-30T09:00:00+08:00",
                    "field": "volume",
                    "reason": "provider_null",
                }
            ]
        },
        "data": [
            {
                "symbol": "2330",
                "source_symbol": "TSE:2330",
                "trade_date": "2026-07-30",
                "market_timezone": "Asia/Taipei",
                "bar_start_time": "2026-07-30T09:00:00+08:00",
                "bar_end_time": "2026-07-30T09:01:00+08:00",
                "signal_time": "2026-07-30T09:01:00+08:00",
                "price_adjustment": "none",
                "trade_count": None,
                "open": "1000",
                "high": "1010",
                "low": "999",
                "close": "1005",
                "volume": None,
                "turnover": "1000",
            }
        ],
    }


def _minute_request(
    dataset_key: str,
    *,
    symbol: str = "2330",
    source_symbol: str = "TSE:2330",
    close: str = "1005",
) -> dict:
    data_date = date(2026, 7, 30)
    digest = minute_sequence_key_digest(
        dataset_key=dataset_key,
        data_date=data_date,
        snapshot_id=f"{dataset_key}-snapshot",
        sequence=1,
    )
    return {
        "dataset_key": dataset_key,
        "schema_id": "market_minute",
        "schema_version": 1,
        "source": "shioaji",
        "request_key": f"mmr:{digest}",
        "idempotency_key": f"mms:{digest}",
        "fetched_at": "2026-07-30T02:00:00Z",
        "payload": {
            "batch": {
                "data_date": "2026-07-30",
                "delivery_mode": "sequenced_snapshot",
                "declared_record_count": 1,
                "coverage_start_date": "2026-07-30",
                "coverage_end_date": "2026-07-30",
                "snapshot_id": f"{dataset_key}-snapshot",
                "daily_update_id": f"{dataset_key}-update",
                "universe_id": f"{dataset_key}-universe",
                "symbols_sha256": hashlib.sha256(symbol.encode()).hexdigest(),
                "sequence": 1,
                "sequence_count": 1,
                "provider_usage_before": {"requests_used": 1, "requests_limit": 500},
                "provider_usage_after": {"requests_used": 2, "requests_limit": 500},
                "anomalies": [],
            },
            "symbol_statuses": [{"symbol": symbol, "outcome": "data"}],
            "data": [
                {
                    "symbol": symbol,
                    "source_symbol": source_symbol,
                    "trade_date": "2026-07-30",
                    "market_timezone": "Asia/Taipei",
                    "bar_start_time": "2026-07-30T09:00:00+08:00",
                    "bar_end_time": "2026-07-30T09:01:00+08:00",
                    "signal_time": "2026-07-30T09:01:00+08:00",
                    "price_adjustment": "none",
                    "trade_count": None,
                    "open": "1000",
                    "high": "1010",
                    "low": "999",
                    "close": close,
                    "volume": 10,
                    "turnover": "1000",
                }
            ],
        },
    }


def _contract_config(asset_class: str, *, precedence: list[str] | None = None) -> dict:
    config = _config()
    config.update(
        {
            "schema_id": "market_minute",
            "accepted_schema_versions": [1],
            "current_schema_version": 1,
            "schema_enforcement": "audit",
            "allowed_sources": ["shioaji"],
        }
    )
    config["defaults"]["asset_class"] = asset_class
    config["defaults"]["market_timezone"] = "Asia/Taipei"
    config["defaults"]["price_adjustment"] = "none"
    if precedence is not None:
        config["source_precedence"] = precedence
    return config


def test_market_minute_contract_maps_utc_taipei_and_identifier() -> None:
    normalizer = MarketMinuteContractNormalizer(None, _config())
    record = normalizer.map_fields(_payload())[0]

    assert record.market == "TW"
    assert record.asset_class == "equity"
    assert record.currency == "TWD"
    assert record.identifier_type == "shioaji"
    assert record.identifier_value == "TSE:2330"
    assert record.bar_start_time == datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc)
    assert record.close == Decimal("1005")


def test_market_minute_dq_accepts_anomaly_backed_null_and_rejects_missing_source() -> None:
    normalizer = MarketMinuteContractNormalizer(None, _config())
    record = normalizer.map_fields(_payload())[0]
    anomaly_fields = {
        item["field"] for item in normalizer._anomalies_for_record(_payload(), record)
    }
    assert not [
        issue
        for issue in normalizer.dq_validator.validate_minute(
            record, seen_keys=set(), anomaly_fields=anomaly_fields
        )
        if issue.severity == "error"
    ]

    no_source = MarketMinuteContractNormalizer(None, _config(None)).map_fields(_payload())[0]
    issues = normalizer.dq_validator.validate_minute(
        no_source, seen_keys=set(), anomaly_fields=set()
    )
    assert any(issue.issue_type == "MINUTE_SOURCE_INVALID" for issue in issues)


def test_market_minute_contract_routing_is_explicit() -> None:
    assert (
        _select_normalizer_for_payload(
            "tw_equity_minute", {}, schema_id="market_minute", schema_version=1
        )
        is MarketMinuteContractNormalizer
    )


@pytest.mark.asyncio
async def test_market_minute_process_persists_anomaly_lineage_and_uses_natural_key(
    test_session,
) -> None:
    """Minute writes are canonical, idempotent, and retain adapter DQ evidence."""
    run = IngestionRun(
        run_id=uuid7(),
        dataset_key="test_minute_equity",
        status="pending",
        raw_records=1,
        created_at=utc_now(),
    )
    dataset = DatasetRegistry(
        dataset_key="test_minute_equity",
        name="test minute equity",
        asset_class="equity",
        market="TW",
        frequency="minute",
        is_active=True,
        config=_config(),
    )
    test_session.add_all((dataset, run))
    await test_session.commit()

    normalizer = MarketMinuteContractNormalizer(test_session, _config())
    result = await normalizer.process(_payload(), run.run_id)
    assert result.success_records == 1
    assert result.failed_records == 0
    minute = (await test_session.execute(select(MarketDataMinute))).scalar_one()
    assert minute.trade_date.isoformat() == "2026-07-30"
    assert minute.volume is None
    assert minute.run_id == run.run_id
    anomaly = (
        await test_session.execute(
            select(DQIssue).where(
                DQIssue.run_id == run.run_id,
                DQIssue.issue_type == "MINUTE_ADAPTER_ANOMALY",
            )
        )
    ).scalar_one()
    assert anomaly.instrument_id == minute.instrument_id
    assert anomaly.trade_date == minute.bar_start_time

    repeat = await normalizer.process(_payload(), run.run_id)
    assert repeat.success_records == 1
    assert (await test_session.execute(select(MarketDataMinute))).scalars().all() == [minute]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["data"][0].update({"bar_end_time": "2026-07-30T09:02:00+08:00"}),
        lambda value: value["data"][0].update({"signal_time": "2026-07-30T09:00:00+08:00"}),
        lambda value: value["data"][0].update({"trade_date": "2026-07-29"}),
        lambda value: value["data"][0].update({"high": "999"}),
        lambda value: value["data"][0].update({"volume": None}),
        lambda value: value["data"].append(dict(value["data"][0])),
    ],
)
async def test_market_minute_dq_errors_block_canonical_writes(test_session, mutate) -> None:
    payload = _payload()
    payload["data"][0]["volume"] = 1
    payload["batch"]["anomalies"] = []
    mutate(payload)
    dataset = DatasetRegistry(
        dataset_key="minute_dq",
        name="minute dq",
        asset_class="equity",
        market="TW",
        frequency="minute",
        is_active=True,
        config=_contract_config("equity"),
    )
    run = IngestionRun(
        run_id=uuid7(),
        dataset_key="minute_dq",
        status="pending",
        raw_records=1,
        created_at=utc_now(),
    )
    test_session.add_all((dataset, run))
    await test_session.commit()

    result = await MarketMinuteContractNormalizer(test_session, _config()).process(
        payload, run.run_id
    )
    assert result.failed_records >= 1
    assert not (await test_session.execute(select(MarketDataMinute))).scalars().all()
    assert (
        (await test_session.execute(select(DQIssue).where(DQIssue.run_id == run.run_id)))
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_market_minute_source_precedence_and_fetched_at_control_upsert(test_session) -> None:
    dataset = DatasetRegistry(
        dataset_key="minute_precedence",
        name="minute precedence",
        asset_class="equity",
        market="TW",
        frequency="minute",
        is_active=True,
        config=_contract_config("equity"),
    )
    run = IngestionRun(
        run_id=uuid7(),
        dataset_key="minute_precedence",
        status="pending",
        raw_records=1,
        created_at=utc_now(),
    )
    test_session.add_all((dataset, run))
    await test_session.commit()
    payload = _payload()
    payload["data"][0]["volume"] = 1
    config = _config("preferred")
    config["source_precedence"] = ["preferred", "fallback"]
    config["_ingest_fetched_at"] = "2026-07-30T02:00:00Z"
    first = MarketMinuteContractNormalizer(test_session, config)
    assert (await first.process(payload, run.run_id)).success_records == 1

    stale_config = dict(
        config, _ingest_source="fallback", _ingest_fetched_at="2026-07-30T03:00:00Z"
    )
    stale_payload = _payload()
    stale_payload["data"][0].update({"volume": 1, "close": "1001"})
    stale = await MarketMinuteContractNormalizer(test_session, stale_config).process(
        stale_payload, run.run_id
    )
    assert stale.precedence_rejected_records == 1

    older_config = dict(config, _ingest_fetched_at="2026-07-30T01:00:00Z")
    older_payload = _payload()
    older_payload["data"][0].update({"volume": 1, "close": "1003"})
    assert (
        await MarketMinuteContractNormalizer(test_session, older_config).process(
            older_payload, run.run_id
        )
    ).precedence_rejected_records == 1

    newer_config = dict(config, _ingest_fetched_at="2026-07-30T04:00:00Z")
    newer_payload = _payload()
    newer_payload["data"][0].update({"volume": 1, "close": "1002"})
    assert (
        await MarketMinuteContractNormalizer(test_session, newer_config).process(
            newer_payload, run.run_id
        )
    ).success_records == 1
    minute = (await test_session.execute(select(MarketDataMinute))).scalar_one()
    assert minute.close == Decimal("1002")
    assert minute.source == "preferred"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dataset_key", "asset_class", "symbol", "source_symbol"),
    [
        ("tw_equity_minute", "equity", "2330", "2330"),
        ("tw_etf_minute", "etf", "0050", "0050"),
    ],
)
async def test_active_contract_ingest_and_queue_normalize_minute_dataset(
    client: AsyncClient,
    source_headers: dict,
    test_session,
    test_engine,
    dataset_key: str,
    asset_class: str,
    symbol: str,
    source_symbol: str,
) -> None:
    """Exercise Source acceptance and the real queue worker for both minute asset classes."""
    test_session.add(
        DatasetRegistry(
            dataset_key=dataset_key,
            name=dataset_key,
            asset_class=asset_class,
            market="TW",
            frequency="minute",
            is_active=True,
            config=_contract_config(asset_class),
        )
    )
    await test_session.commit()
    response = await client.post(
        "/api/v1/source/ingest",
        headers=source_headers,
        json=_minute_request(dataset_key, symbol=symbol, source_symbol=source_symbol),
    )
    assert response.status_code == 202, response.text
    run_id = UUID(response.json()["run_id"])
    persisted_run = await test_session.get(IngestionRun, run_id)
    assert persisted_run is not None
    assert persisted_run.snapshot_id == f"{dataset_key}-snapshot"
    assert persisted_run.daily_update_id == f"{dataset_key}-update"
    assert persisted_run.sequence == 1
    assert persisted_run.sequence_count == 1
    job = (
        await test_session.execute(
            select(NormalizationJob).where(NormalizationJob.run_id == run_id)
        )
    ).scalar_one()
    await execute_normalization(
        run_id, job.delivery_id, database_url=test_engine.url.render_as_string(hide_password=False)
    )
    test_session.expire_all()
    instrument = (
        await test_session.execute(
            select(Instrument).where(
                Instrument.asset_class == asset_class,
                Instrument.symbol == symbol,
            )
        )
    ).scalar_one()
    minute = (
        await test_session.execute(
            select(MarketDataMinute).where(
                MarketDataMinute.instrument_id == instrument.instrument_id
            )
        )
    ).scalar_one()
    assert instrument.market == "TW"
    assert instrument.asset_class == asset_class
    assert instrument.symbol == symbol
    assert minute.source == "shioaji"

    rerun_response = await client.post(
        f"/api/v1/source/runs/{run_id}/rerun",
        headers=source_headers,
    )
    assert rerun_response.status_code == 202, rerun_response.text
    rerun_id = UUID(rerun_response.json()["run_id"])
    rerun = await test_session.get(IngestionRun, rerun_id)
    assert rerun is not None
    assert rerun.is_rerun is True
    assert rerun.batch_data_date == date(2026, 7, 30)
    assert rerun.delivery_mode == "sequenced_snapshot"
    assert rerun.snapshot_id == f"{dataset_key}-snapshot"
    assert rerun.daily_update_id == f"{dataset_key}-update"
    assert rerun.sequence == 1
    assert rerun.sequence_count == 1
