"""An exchange quote feed settles member by member, like a provider's EOD feed."""

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from app.models.provider_work import ProviderWork
from app.models.registry import DatasetRegistry, DeliveryMemberEvidence, DQIssue, IngestionRun
from app.schemas.eod_completion import EODCompletionEvidence, EODRawEvidence
from app.services import provider_metadata
from app.services import provider_work as queue
from app.services.eod_completion import eod_contract, validate_eod_completion
from app.services.historical_backfill import _source_symbol
from app.utils import uuid7

DAY = date(2026, 11, 20)
DATASET = "hk_equity_official_eod"
CONTRACT = {"schema_id": "exchange_daily_quote", "schema_version": 1}
GZIP = "c" * 64


@pytest.fixture(autouse=True)
def hkex_catalog(monkeypatch):
    """The catalog lists hkex only once it is activated; add its feed here."""
    catalog = provider_metadata.provider_catalog()
    taifex = next(p for p in catalog.providers if p.provider == "taifex")
    factset = next(f for f in catalog.feeds if f.provider == "factset")
    provider = taifex.model_copy(
        update={"provider": "hkex", "display_name": "HKEX", "container_suffix": "hkex"}
    )
    feed = factset.model_copy(
        update={
            "provider": "hkex",
            "dataset_key": DATASET,
            "contract": factset.contract.model_copy(
                update={
                    "schema_id": "exchange_daily_quote",
                    "current_version": 1,
                    "accepted_versions": [1],
                }
            ),
            "historical_cohort": "current_release",
        }
    )
    patched = catalog.model_copy(
        update={"providers": [*catalog.providers, provider], "feeds": [*catalog.feeds, feed]}
    )
    monkeypatch.setattr(provider_metadata, "provider_catalog", lambda: patched)


def proof(*outcomes, schema_id="exchange_daily_quote", raw=None):
    return EODCompletionEvidence(
        version=2,
        schema_id=schema_id,
        schema_version=1,
        trade_date=DAY,
        members=[
            {"member_key": key, "outcome": outcome, **(raw or {}).get(key, {})}
            for key, outcome in outcomes
        ],
    )


async def scope(db, contract=CONTRACT):
    db.add(
        DatasetRegistry(dataset_key=DATASET, name="HK official", market="HK", asset_class="equity")
    )
    await db.flush()
    work = ProviderWork(
        environment=queue.environment(),
        provider="hkex",
        dataset_key=DATASET,
        kind="historical",
        stable_key="test",
        trade_date=DAY,
        manifest={
            "contract": contract,
            "members": [
                {"key": key, "mapping_status": "mapped"}
                for key in ("00700", "00005", "00036", "09999")
            ],
        },
    )
    db.add(work)
    await db.flush()
    return work


async def run(db, **changes):
    value = IngestionRun(
        **{
            "run_id": uuid7(),
            "dataset_key": DATASET,
            "source": "hkex",
            "schema_id": "exchange_daily_quote",
            "schema_version": 1,
            "batch_data_date": DAY,
            "status": "completed_with_errors",
            "total_records": 2,
            "success_records": 1,
            "failed_records": 1,
            **changes,
        }
    )
    db.add(value)
    await db.flush()
    return value


def _report_raw(provider="hkex", dataset=DATASET):
    ref = f"r2://{'a' * 32}/raw/{provider}/{dataset}/0123456789abcdef/{GZIP}.htm.gz"
    return {"raw": {"ref": ref, "sha256": GZIP}}


def test_the_feed_freezes_its_own_contract():
    work = ProviderWork(provider="hkex", dataset_key=DATASET)
    assert eod_contract(work) == CONTRACT
    # The provider's EOD feed keeps market_eod.
    assert eod_contract(ProviderWork(provider="factset", dataset_key="hk_equity_eod")) == {
        "schema_id": "market_eod",
        "schema_version": 2,
    }


@pytest.mark.asyncio
async def test_a_current_release_date_settles_by_writes_halts_and_bad_rows(test_session):
    work = await scope(test_session)
    accepted = await run(test_session)
    test_session.add(DeliveryMemberEvidence(run_id=accepted.run_id, member_key="00700"))
    test_session.add(
        DQIssue(
            run_id=accepted.run_id,
            issue_type="QUOTE_HIGH_BELOW_LOW",
            severity="error",
            trade_date=datetime(2026, 11, 20, tzinfo=timezone.utc),
            raw_data={"symbol": "00005"},
        )
    )
    await test_session.flush()
    # The halted member, and the one neither report lists, cite the report itself.
    unquoted = {**_report_raw(), "reason": "no_source_row"}
    evidence = proof(
        ("00700", "data"),
        ("00005", "invalid"),
        ("00036", "no_data"),
        ("09999", "missing"),
        raw={"00036": _report_raw(), "09999": unquoted},
    )

    result = await validate_eod_completion(
        test_session, work, [accepted], evidence, outcome="partial"
    )

    assert result["data"] == ["00700"] and result["gaps"] == ["00005", "09999"]
    # Without the report a missing member is unproven: only dated cohorts allow that.
    unproven = proof(
        ("00700", "data"),
        ("00005", "invalid"),
        ("00036", "no_data"),
        ("09999", "missing"),
        raw={"00036": _report_raw()},
    )
    with pytest.raises(ValueError, match="gap claim"):
        await validate_eod_completion(test_session, work, [accepted], unproven, outcome="partial")
    # A report outside this feed's raw scope proves nothing.
    foreign = proof(
        ("00700", "data"),
        ("00005", "invalid"),
        ("00036", "no_data"),
        ("09999", "missing"),
        raw={"00036": _report_raw("factset", "hk_equity_eod"), "09999": unquoted},
    )
    with pytest.raises(ValueError, match="no-data"):
        await validate_eod_completion(test_session, work, [accepted], foreign, outcome="partial")


@pytest.mark.asyncio
async def test_a_contract_other_than_the_feeds_never_settles(test_session):
    work = await scope(test_session, contract={"schema_id": "market_eod", "schema_version": 1})
    evidence = proof(
        ("00700", "missing"), ("00005", "missing"), ("00036", "missing"), ("09999", "missing")
    )
    with pytest.raises(ValueError, match="restricted"):
        await validate_eod_completion(test_session, work, [], evidence, outcome="partial")

    correct = await _work_with(test_session, CONTRACT)
    mislabeled = proof(
        ("00700", "missing"),
        ("00005", "missing"),
        ("00036", "missing"),
        ("09999", "missing"),
        schema_id="market_eod",
    )
    with pytest.raises(ValueError, match="restricted"):
        await validate_eod_completion(test_session, correct, [], mislabeled, outcome="partial")
    # A run of the provider's EOD contract is not this feed's write.
    other = await run(test_session, schema_id="market_eod")
    sound = proof(
        ("00700", "missing"), ("00005", "missing"), ("00036", "missing"), ("09999", "missing")
    )
    with pytest.raises(ValueError, match="restricted"):
        await validate_eod_completion(test_session, correct, [other], sound, outcome="partial")


async def _work_with(db, contract):
    work = ProviderWork(
        environment=queue.environment(),
        provider="hkex",
        dataset_key=DATASET,
        kind="historical",
        stable_key="second",
        trade_date=DAY,
        manifest={
            "contract": contract,
            "members": [
                {"key": key, "mapping_status": "mapped"}
                for key in ("00700", "00005", "00036", "09999")
            ],
        },
    )
    db.add(work)
    await db.flush()
    return work


def test_raw_evidence_names_a_json_response_or_a_gzip_report():
    EODRawEvidence.model_validate(_report_raw()["raw"])
    EODRawEvidence.model_validate({"ref": f"r2://{'a' * 32}/raw/x/{GZIP}.json", "sha256": GZIP})
    for ref in (f"r2://{'a' * 32}/raw/x/{GZIP}.gz", f"r2://{'a' * 32}/raw/x/{'d' * 64}.htm.gz"):
        with pytest.raises(ValidationError):
            EODRawEvidence.model_validate({"ref": ref, "sha256": GZIP})


def test_an_exchange_quote_is_keyed_by_the_listed_code():
    from app.schemas.ingress import ExchangeDailyQuoteRow, MarketEODRow

    quote = ExchangeDailyQuoteRow(
        symbol="00700",
        trade_date=DAY,
        currency="HKD",
        trading_status="traded",
        volume=1,
        turnover="1",
    )
    assert _source_symbol(quote) == "00700"
    provider = MarketEODRow(symbol="00700", trade_date=DAY, close="1", source_symbol="J3TRKQ-L")
    assert _source_symbol(provider) == "J3TRKQ-L"
    assert _source_symbol(MarketEODRow(symbol="00700", trade_date=DAY, close="1")) is None
