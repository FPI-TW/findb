"""The Fetcher's daily loop delivers FinLab intraday partitions through the real Source.

Only FinLab's own answer and the R2 service are replaced: the Fetcher's runtime,
adapter, column mapping and raw store, the Source API, the queue's normalizer and
the plan's counting are the real ones, on the dataset exactly as seed declares it.
"""

import asyncio
import importlib.metadata
import io
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.canonical import (
    CalendarYearRevision,
    Instrument,
    IntradayPartition,
    MarketDataEOD,
)
from app.models.raw import RawMarketPayload
from app.models.registry import (
    DailyDeliveryMember,
    DatasetRegistry,
    IngestionRun,
    SchedulerControl,
    SchedulerDataset,
)
from app.schemas.full_market import DeliveryPlanCreateRequest
from app.services.full_market import create_plan, get_plan
from app.services.normalize import intraday
from app.services.source_clients import PROVIDER_DATASET_SCOPE, create_source_client
from app.utils import utc_now
from scripts.seed_data import DATASETS
from tests.full_market_fixtures import historical_admission
from tests.intraday_support import ACCOUNT, DAY, RAW_BUCKET, Stores, market_open
from tests.test_canonical_ingest_api import _execute_run
from tests.test_full_market import _baseline, _calendar

DATASET = "tw_equity_bar_1m"
# The columns of a FinLab 1-minute partition file, in FinLab's order.
BAR_COLUMNS = (
    "stock_id",
    "trade_date",
    "timestamp",
    "session",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "tick_count",
    "vwap",
)


def finlab_bars(symbol: str) -> bytes:
    """Three minutes of one symbol as FinLab publishes them: Taipei instants, text dates."""
    minutes = (0, 1, 2)
    table = pa.table(
        {
            "stock_id": [symbol] * 3,
            "trade_date": [DAY.isoformat()] * 3,
            "timestamp": pa.array(
                [market_open(DAY, minute) for minute in minutes],
                type=pa.timestamp("ns", tz="Asia/Taipei"),
            ),
            "session": ["regular"] * 3,
            "open": [100.0 + minute for minute in minutes],
            "high": [101.0 + minute for minute in minutes],
            "low": [99.5 + minute for minute in minutes],
            "close": [100.5 + minute for minute in minutes],
            "volume": [10 + minute for minute in minutes],
            "tick_count": [3, 3, 3],
            "vwap": [100.25 + minute for minute in minutes],
        }
    )
    sink = io.BytesIO()
    pq.write_table(table, sink)
    return sink.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("daily_volume", "absent_outcome"),
    [
        # No daily bar yet: the missing partition says nothing, so it stays retryable.
        (None, ("blocked", "empty_response")),
        # The daily bar shows a board lot traded: the partition must exist, keep trying.
        (2000, ("blocked", "empty_response")),
        # Odd lots only: FinLab has no partition because there was no board-lot trade.
        (300, ("no_data", "no_trade")),
    ],
    ids=["no daily bar", "board lot traded", "odd lots only"],
)
async def test_the_daily_loop_stores_each_answer_finlab_gives_for_a_member(
    client, test_session, test_engine, monkeypatch, tmp_path, daily_volume, absent_outcome
):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / "fetcher/src"))
    import findb_fetcher.full_market_runtime as daily_runtime
    from findb_fetcher import provider_process
    from findb_fetcher import provider_work as coordinator
    from findb_fetcher.client import SourceAPIClient
    from findb_fetcher.config import FetcherConfig
    from findb_fetcher.contracts import ContractRegistry
    from findb_fetcher.full_market_client import FullMarketSourceClient
    from findb_fetcher.full_market_runtime import FullMarketRuntime
    from findb_fetcher.full_market_state import FullMarketState
    from findb_fetcher.providers.finlab import FINLAB_SDK_VERSION, FinLabSdkGateway
    from findb_fetcher.providers.finlab_intraday import map_rows
    from findb_fetcher.raw_storage import R2RawPayloadStore, RawStorageConfig

    monkeypatch.setattr(get_settings(), "PROVIDER_WORK_REQUIRED", True)
    monkeypatch.setattr(get_settings(), "FINLAB_SKIP_NON_TRADING_DAYS", False)
    monkeypatch.setenv("PROVIDER_WORK_REQUIRED", "true")
    monkeypatch.setenv("APP_ENVIRONMENT", "local")
    monkeypatch.setenv("FETCHER_ACCOUNT_STATE_PATH", str(tmp_path / "account/governor.sqlite3"))
    monkeypatch.setenv("FETCHER_CONTRACTS_DIR", str(root / "contracts"))
    from tests.test_provider_work import attest_cutover

    inodes = {}
    for provider in ("factset", "finlab", "taifex", "twelve_data"):
        with coordinator.host_lock(provider) as (_, inode):
            inodes[provider] = inode
    await attest_cutover(test_session, inodes)

    # Both buckets as one service: what the Fetcher uploads is what the Worker reads.
    stores = Stores()
    monkeypatch.setattr(intraday, "raw_reader", lambda: stores.raw)
    monkeypatch.setattr(intraday, "canonical_publisher", lambda: stores.canonical)
    raw_store = R2RawPayloadStore(
        RawStorageConfig(ACCOUNT, RAW_BUCKET, "fetcher-id", "fetcher-secret"), client=stores.r2
    )

    # FinLab's whole scope exactly as seed declares it; only one dataset gets a baseline.
    finlab_scope = PROVIDER_DATASET_SCOPE["finlab"]
    for item in DATASETS:
        if item["dataset_key"] in finlab_scope:
            test_session.add(DatasetRegistry(**deepcopy(item)))
    await test_session.commit()
    dataset = await test_session.get(DatasetRegistry, DATASET)
    await _calendar(test_session, "TW", [DAY])
    _, release = await _baseline(test_session, DATASET, 3)
    revision = await test_session.scalar(
        select(CalendarYearRevision).where(CalendarYearRevision.market == "TW")
    )
    await historical_admission(test_session, dataset, release, DAY, revision.id)
    plan = await create_plan(
        test_session,
        DeliveryPlanCreateRequest(
            version=1,
            dataset_key=DATASET,
            provider="finlab",
            trade_date=DAY,
            release_id=release.release_id,
        ),
        allowed_datasets=[DATASET],
    )
    key = "full_market_finlab_v1"
    control = await test_session.get(SchedulerControl, key)
    control.observed_state = "running"
    for dataset_key in finlab_scope:
        test_session.add(SchedulerDataset(scheduler_key=key, dataset_key=dataset_key))
    _, secret = await create_source_client(
        test_session,
        name="intraday-runtime",
        source_name="finlab",
        allowed_datasets=list(finlab_scope),
        rate_limit_requests=100000,
        rate_limit_window=60,
        commit=False,
    )
    feed = {
        "provider": "finlab",
        "dataset_key": DATASET,
        "market": "TW",
        "asset_class": "equity",
        "source_timezone": "Asia/Taipei",
        "scheduled_time": "00:00",
        "target_date_lag_days": 0,
        "completion_window_seconds": 86400,
        "slot_id": "taiwan_market_window",
    }
    if daily_volume is not None:
        instrument = Instrument(asset_class="equity", market="TW", symbol="0003")
        test_session.add(instrument)
        await test_session.flush()
        test_session.add(
            MarketDataEOD(
                instrument_id=instrument.instrument_id,
                trade_date=DAY,
                open=Decimal("10"),
                high=Decimal("10"),
                low=Decimal("10"),
                close=Decimal("10"),
                volume=daily_volume,
                source="finlab",
                asof_ts=utc_now(),
            )
        )
    await test_session.commit()

    config = FetcherConfig(
        "https://source.test", secret, contracts_dir=root / "contracts", max_attempts=1
    )
    monkeypatch.setattr(FetcherConfig, "from_env", classmethod(lambda cls: config))
    registry = ContractRegistry(config.contracts_dir)
    loop = asyncio.get_running_loop()
    refused: list[tuple[str, int, str]] = []

    def handle(request):
        async def invoke():
            response = await client.request(
                request.method,
                str(request.url),
                content=request.content,
                headers=dict(request.headers),
            )
            if response.status_code >= 400:
                refused.append((request.url.path, response.status_code, response.text[:300]))
            if request.url.path == "/api/v1/source/ingest" and response.status_code == 202:
                await _execute_run(test_session, test_engine, UUID(response.json()["run_id"]))
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=response.content,
                request=request,
            )

        return asyncio.run_coroutine_threadsafe(invoke(), loop).result(timeout=20)

    def http():
        # The governed dispatch boundary accepts httpx's own transports only.
        return httpx.Client(transport=httpx.MockTransport(handle))

    # What FinLab answers for each member: a published day, a day without trades, nothing.
    answers = {"0001": "ready", "0002": "no_trades", "0003": "absent"}
    native: list[dict] = []

    def supervised_call(operation, payload, *, timeout=None, checkpoint=None):
        assert operation == "finlab.intraday" and payload["token"] == "offline-finlab"
        if checkpoint is not None:
            checkpoint()
        with pytest.raises(coordinator.ProviderWorkWaitingError):
            with coordinator.host_lock("finlab"):
                pytest.fail("a FinLab request left the provider's work lock")
        native.append({k: payload[k] for k in ("record_kind", "provider_symbol", "trade_date")})
        answer = answers[payload["provider_symbol"]]
        entry = {"date": payload["trade_date"], "status": answer, "generation": "1794560332114812"}
        if answer == "no_trades":
            return "no_trades", entry, 0, b"", b""
        if answer == "absent":
            return "absent", {"date": payload["trade_date"], "status": "absent"}, 0, b"", b""
        original = finlab_bars(payload["provider_symbol"])
        rows = map_rows(
            original,
            record_kind=payload["record_kind"],
            provider_symbol=payload["provider_symbol"],
            symbol=payload["symbol"],
            trade_date=payload["trade_date"],
            row_count=3,
            layout=payload["layout"],
            source_columns=BAR_COLUMNS,
        )
        return "ready", entry | {"rows": 3, "size": len(original)}, 3, original, rows

    monkeypatch.setattr(provider_process, "supervised_call", supervised_call)
    monkeypatch.setattr(
        FinLabSdkGateway,
        "from_env",
        classmethod(
            lambda cls, **kwargs: FinLabSdkGateway(
                "offline-finlab", _sdk=SimpleNamespace(login=lambda token: None), **kwargs
            )
        ),
    )
    version = importlib.metadata.version
    monkeypatch.setattr(
        importlib.metadata,
        "version",
        lambda name: FINLAB_SDK_VERSION if name == "finlab" else version(name),
    )

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return (
                datetime(2026, 7, 21, 11, tzinfo=timezone.utc).astimezone(tz)
                if tz
                else datetime(2026, 7, 21, 11)
            )

    monkeypatch.setattr(daily_runtime, "datetime", Clock)

    def run_daily():
        state = FullMarketState(tmp_path / "full_market.sqlite3")
        state.bind_profile("full_market")
        state.refresh("finlab", now=0, result=[], next_at=2**40)
        with (
            FullMarketSourceClient(
                config, client=http(), execution_profile="full_market"
            ) as source,
            SourceAPIClient(config, registry, client=http()) as delivery,
        ):
            runtime = FullMarketRuntime(
                provider="finlab",
                config={"feeds": [feed]},
                state=state,
                source=source,
                delivery=delivery,
                raw_store=raw_store,
                execution_profile="full_market",
            )
            try:
                return runtime.cycle()
            finally:
                runtime.close()

    health = await asyncio.wait_for(asyncio.to_thread(run_daily), 25)

    corroborated = absent_outcome[0] == "no_data"
    # Source refuses only the claim it cannot corroborate, and nothing else.
    assert [(path.rsplit("/", 1)[-1], status) for path, status, _ in refused] == (
        [] if corroborated else [("outcomes", 409)]
    )
    assert all('"no_data_not_corroborated"' in body for _, _, body in refused)
    assert sorted(call["provider_symbol"] for call in native) == ["0001", "0002", "0003"]
    assert {(call["record_kind"], call["trade_date"]) for call in native} == {
        ("bar_1m", DAY.isoformat())
    }
    test_session.expire_all()
    summary = (
        await get_plan(test_session, plan.plan_id, provider="finlab", allowed_datasets=[DATASET])
    ).summary
    counts = (summary.expected, summary.data, summary.no_data, summary.missing, summary.blocked)
    assert counts == ((3, 1, 2, 0, 0) if corroborated else (3, 1, 1, 0, 1))
    members = {
        member.symbol: (member.outcome, member.outcome_reason)
        for member in await test_session.scalars(select(DailyDeliveryMember))
    }
    # A day without trades is settled by FinLab's own manifest entry. A symbol FinLab has
    # no partition for is no trade only when the daily bar agrees; otherwise it is reported
    # and kept for its bounded retries. Either way the other members went on.
    assert members == {
        "0001": (None, None),
        "0002": ("no_data", "no_trade"),
        "0003": absent_outcome,
    }
    # Every member settled, the plan leaves the runtime's open plans.
    assert [plan["counts"] for plan in health["plans"]] == (
        [] if corroborated else [{"complete": 2, "pending": 1}]
    )
    absent = (
        await test_session.scalars(
            select(DailyDeliveryMember).where(DailyDeliveryMember.symbol == "0003")
        )
    ).one()
    if corroborated:
        assert absent.outcome_evidence["source_status"] == "no_record"
        assert absent.outcome_history[-1]["corroboration"] == {
            "dataset_key": "tw_equity_eod",
            "board_lot_shares": 1000,
            "basis": "odd_lots_only",
            "volume_shares": 300,
        }

    # The published day is one canonical object, indexed, with FinLab's values in the layout.
    partition = (await test_session.scalars(select(IntradayPartition))).one()
    assert (partition.dataset_key, partition.symbol, partition.trade_date) == (DATASET, "0001", DAY)
    assert (partition.row_count, partition.source, partition.object_layout) == (3, "finlab", "day")
    assert partition.object_key == (
        f"intraday/v1/{DATASET}/symbol=0001/year=2026/month=07/day=21/part.parquet"
    )
    stored = stores.rows(partition.object_key)
    assert [row["bar_start_time"] for row in stored] == [market_open(DAY, m) for m in (0, 1, 2)]
    assert [(row["symbol"], row["trade_date"], row["volume"]) for row in stored] == [
        ("0001", DAY, 10),
        ("0001", DAY, 11),
        ("0001", DAY, 12),
    ]

    # Raw keeps FinLab's own file as the evidence and the mapped rows as what was delivered.
    run = await test_session.get(IngestionRun, partition.run_id)
    payload = (await test_session.get(RawMarketPayload, run.raw_payload_id)).payload
    batch, row = payload["batch"], payload["data"][0]
    prefix = f"r2://{ACCOUNT}/{RAW_BUCKET}/"
    original = stores.r2.objects[(RAW_BUCKET, batch["source_raw_ref"].removeprefix(prefix))]
    assert original == finlab_bars("0001")
    assert stores.r2.objects[(RAW_BUCKET, row["rows_ref"].removeprefix(prefix))] != original
    assert (row["record_kind"], row["row_count"], row["volume_unit"]) == ("bar_1m", 3, "lot")
    assert set(stores.canonical_objects()) == {partition.object_key}
