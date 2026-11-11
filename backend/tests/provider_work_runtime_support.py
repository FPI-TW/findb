"""Real fenced Fetcher/ASGI/worker harness; only native SDK/HTTP and R2 are fake."""

import asyncio
import hashlib
import importlib.metadata
import json
import os
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs
from uuid import UUID

import httpx
import pytest

from app.config import get_settings
from app.models.registry import DatasetRegistry, SchedulerControl, SchedulerDataset
from app.services.historical_backfill import create_request
from app.services.source_clients import create_source_client
from scripts.seed_data import DATASETS
from tests.provider_work_clock import recent_backfill_day
from tests.test_canonical_ingest_api import _execute_run
from tests.test_provider_work import attest_cutover

DAY = recent_backfill_day()
# Longer than one TAIFEX harness run, including CI scheduling delay.
MINUTE_ROLLOVER_MARGIN_SECONDS = 5
SCOPES = [
    ("twelve_data", "us_equity_eod"),
    ("finlab", "tw_equity_eod"),
    ("taifex", "tw_futures_eod"),
]


class RuntimeHarness:
    def __init__(
        self,
        client,
        session,
        engine,
        monkeypatch,
        path,
        provider,
        dataset,
        historical_days=1,
        *,
        environment="staging",
    ):
        self.client, self.db, self.engine, self.patch = client, session, engine, monkeypatch
        self.path, self.provider, self.dataset = path, provider, dataset
        self.environment = environment
        self.historical_days = historical_days
        self.loop = asyncio.get_running_loop()
        self.trace = path / "native-calls.jsonl"
        self.confirm_barrier = False
        self.prepared = asyncio.Event()
        self.allow_confirm = asyncio.Event()
        self.accepted = asyncio.Event()
        self.allow_terminal = asyncio.Event()
        self.hold_terminal = False
        self.drop_intent_ack = False
        self.crash_after_local_save = False
        self.saved_body = None
        self.runs = []
        self.source_events = []
        # ASGI overrides use this test's one AsyncSession. A real heartbeat
        # thread may overlap native delivery; serialize their DB use, not the
        # protocol barriers above each request or any product execution.
        self.source_session_lock = asyncio.Lock()
        self.source_errors = []
        self.failure = None
        self.deadline = time.monotonic() + 25
        self.dispatch_clock = None

    async def initialize(self):
        root = Path(__file__).resolve().parents[2]
        patch = self.patch
        patch.syspath_prepend(str(root / "fetcher/src"))
        from findb_fetcher import execution_metadata
        from findb_fetcher import provider_work as coordinator
        from findb_fetcher.config import FetcherConfig
        from findb_fetcher.contracts import ContractRegistry

        if self.provider == "taifex":
            from tests.provider_work_clock import dispatch_clock

            self.dispatch_clock = dispatch_clock(patch)
        patch.setattr(execution_metadata, "CONFIGS_DIR", root / "fetcher/configs")
        self.queue = coordinator
        patch.setattr(get_settings(), "PROVIDER_WORK_REQUIRED", True)
        patch.setattr(get_settings(), "APP_ENVIRONMENT", self.environment)
        for key, value in {
            "APP_ENVIRONMENT": self.environment,
            "PROVIDER_WORK_REQUIRED": "true",
            "FETCHER_ACCOUNT_STATE_PATH": str(self.path / "account/governor.sqlite3"),
            "FETCHER_HISTORICAL_STATE_DIR": str(self.path / "historical"),
            "FETCHER_HISTORICAL_SCHEDULE_FILE": str(self.path / "absent-daily.json"),
            "FETCHER_FINLAB_UNIVERSE_FILE": str(
                root / "fetcher/configs/finlab_tw_review_required.v1.json"
            ),
        }.items():
            patch.setenv(key, value)
        inodes = {}
        # Backend cutover proof still names Shioaji until its scope is retired.
        for provider in {provider for provider, _ in SCOPES} | {"factset"}:
            with coordinator.host_lock(provider) as (_, inode):
                inodes[provider] = inode
        await attest_cutover(self.db, inodes)
        definition = next(row for row in DATASETS if row["dataset_key"] == self.dataset)
        self.db.add(DatasetRegistry(**definition))
        key = f"full_market_{self.provider}_v1"
        self.db.add(
            SchedulerControl(
                scheduler_key=key,
                provider=self.provider,
                execution_profile="full_market",
                timezone="Asia/Taipei",
                slot_id="taiwan_market_window",
                desired_state="running",
                observed_state="running",
            )
        )
        await self.db.flush()
        self.db.add(SchedulerDataset(scheduler_key=key, dataset_key=self.dataset))
        _, secret = await create_source_client(
            self.db,
            name="native-matrix",
            source_name=self.provider,
            allowed_datasets=[self.dataset],
            rate_limit_requests=100000,
            rate_limit_window=60,
            commit=False,
        )
        await self.db.flush()
        request, _ = (
            await create_request(
                self.db,
                provider=self.provider,
                dataset_key=self.dataset,
                start_date=DAY,
                end_date=DAY + timedelta(days=self.historical_days - 1),
                request_key="native-matrix",
                created_by="owner",
            )
            if self.historical_days
            else (None, None)
        )
        self.item_id = request.items[0].item_id if request else None
        self.request_id = request.request_id if request else None
        self.item_ids = [item.item_id for item in request.items] if request else []
        await self.db.commit()
        self.headers = {"X-API-Key": secret}
        self.config = FetcherConfig(
            "https://source.test", secret, contracts_dir=root / "contracts", max_attempts=1
        )
        self.registry = ContractRegistry(self.config.contracts_dir)
        patch.setattr(FetcherConfig, "from_env", classmethod(lambda cls: self.config))
        self.install_native_fakes()
        return self

    def native(self, operation):
        with pytest.raises(self.queue.ProviderWorkWaitingError):
            with self.queue.host_lock(self.provider):
                pytest.fail("native I/O escaped whole-work flock")
        with self.trace.open("a") as stream:
            stream.write(json.dumps(operation) + "\n")

    def native_calls(self):
        return self.trace.read_text().splitlines() if self.trace.exists() else []

    def http(self):
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        if time.monotonic() > self.deadline:
            raise self.queue.ProviderWorkRecoveryBlockedError(
                "native harness exceeded bounded execution window"
            )
        body = json.loads(request.content) if request.content else {}

        async def invoke():
            if (
                request.url.path.endswith("/prepare")
                and body.get("stage") == "confirm"
                and self.crash_after_local_save
            ):
                self.crash_after_local_save = False
                self.saved_body = body["body"].encode()
                raise self.queue.ProviderWorkRecoveryBlockedError(
                    "simulated process death after local save"
                )
            if (
                request.url.path.endswith("/prepare")
                and body.get("stage") == "confirm"
                and self.confirm_barrier
                and not self.prepared.is_set()
            ):
                self.saved_body = body["body"].encode()
                self.prepared.set()
                await asyncio.wait_for(self.allow_confirm.wait(), 15)
            if (
                request.url.path.startswith("/api/v1/source/runs/")
                and request.method == "GET"
                and self.hold_terminal
            ):
                self.accepted.set()
                await asyncio.wait_for(self.allow_terminal.wait(), 15)
            async with self.source_session_lock:
                try:
                    response = await self.client.request(
                        request.method,
                        str(request.url),
                        content=request.content,
                        headers=dict(request.headers),
                    )
                    self.source_events.append(
                        (
                            request.url.path,
                            response.status_code,
                            response.text[:400] if response.status_code >= 400 else "",
                        )
                    )
                    if request.url.path == "/api/v1/source/ingest" and response.status_code == 202:
                        run_id = UUID(response.json()["run_id"])
                        if run_id not in self.runs:
                            self.runs.append(run_id)
                        if not self.hold_terminal:
                            await _execute_run(self.db, self.engine, run_id)
                except Exception as exc:
                    self.source_errors.append(
                        {"path": request.url.path, "exception_type": type(exc).__name__}
                    )
                    raise
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=response.content,
                request=request,
            )

        response = asyncio.run_coroutine_threadsafe(invoke(), self.loop).result(timeout=25)
        if (
            request.url.path.endswith("/prepare")
            and body.get("stage") == "intent"
            and self.drop_intent_ack
            and response.status_code == 200
        ):
            self.drop_intent_ack = False
            raise httpx.ReadError("lost durable intent acknowledgement")
        return response

    def install_native_fakes(self):
        from findb_fetcher import historical_backfill as history
        from findb_fetcher import historical_runtime as adapters
        from findb_fetcher.client import SourceAPIClient
        from findb_fetcher.full_market_client import FullMarketSourceClient
        from findb_fetcher.providers.finlab import FinLabSdkGateway
        from findb_fetcher.providers.twelve_data import TwelveDataClient, TwelveDataConfig
        from findb_fetcher.raw_storage import RawObject

        harness = self

        class Raw:
            def persist(self, data, **kwargs):
                digest = hashlib.sha256(data).hexdigest()
                return RawObject(
                    f"r2://{'a' * 32}/raw/{harness.provider}/{harness.dataset}/{digest}.json",
                    digest,
                    len(data),
                )

        def source_factory(config, contracts):
            return SourceAPIClient(config, contracts, client=self.http(), sleep=lambda _: None)

        self.source_factory = source_factory
        self.patch.setattr(
            history,
            "FullMarketSourceClient",
            lambda config, *, client=None: FullMarketSourceClient(
                config, client=client or self.http()
            ),
        )
        self.patch.setattr(adapters, "SourceAPIClient", source_factory)
        self.patch.setattr(adapters, "R2RawPayloadStore", lambda config: Raw())
        self.patch.setattr(adapters.RawStorageConfig, "from_env", classmethod(lambda cls: None))
        self.raw = Raw()

        def twelve_series(request, symbol):
            self.native(["daily", symbol])
            return {
                "meta": {
                    "symbol": symbol,
                    "currency": "USD",
                    "exchange": "NASDAQ",
                    "mic_code": "XNAS",
                    "exchange_timezone": "America/New_York",
                    "type": "Common Stock",
                    "interval": "1day",
                },
                "status": "ok",
                "values": [
                    {
                        "datetime": request.url.params.get("start_date", str(DAY)),
                        "open": "1",
                        "high": "1",
                        "low": "1",
                        "close": "1",
                        "volume": "10",
                    }
                ],
            }

        def twelve_response(request):
            symbols = request.url.params["symbol"].split(",")
            if len(symbols) == 1:
                return httpx.Response(200, json=twelve_series(request, symbols[0]))
            # A comma batch answers each requested symbol under its own key.
            return httpx.Response(
                200, json={symbol: twelve_series(request, symbol) for symbol in symbols}
            )

        self.patch.setattr(
            adapters,
            "TwelveDataClient",
            lambda config: TwelveDataClient(
                TwelveDataConfig("offline-twelve"),
                client=httpx.Client(transport=httpx.MockTransport(twelve_response)),
            ),
        )
        self.patch.setattr(
            TwelveDataConfig,
            "from_env",
            classmethod(lambda cls: TwelveDataConfig("offline-twelve")),
        )

        class Axis(tuple):
            @property
            def is_unique(self):
                return len(set(self)) == len(self)

        class Frame:
            columns = Axis(("0050", "0056") if self.dataset == "tw_etf_eod" else ("2330", "2317"))
            index = Axis(
                DAY + timedelta(days=index) for index in range(max(1, self.historical_days))
            )

            def __init__(self):
                self.iloc, self.loc = self, self

            def __getitem__(self, key):
                if isinstance(key[1], str):
                    return 1
                return SimpleNamespace(
                    to_json=lambda: json.dumps(
                        {name: {str(day): 1 for day in self.index} for name in self.columns[key[1]]}
                    )
                )

        def finlab_get(name):
            self.native(["dataset", name])
            return Frame()

        native_finlab = SimpleNamespace(
            login=lambda token: None, data=SimpleNamespace(get=finlab_get)
        )
        self.patch.setattr(
            FinLabSdkGateway,
            "from_env",
            classmethod(
                lambda cls, **kwargs: FinLabSdkGateway(
                    "offline-finlab", _sdk=native_finlab, **kwargs
                )
            ),
        )
        version = importlib.metadata.version
        self.patch.setattr(
            importlib.metadata,
            "version",
            lambda name: "1.5.7" if name == "finlab" else version(name),
        )

    def runner(self):
        from findb_fetcher import historical_runtime as adapters
        from findb_fetcher.taifex_historical import TaifexHistoricalRunner

        if self.provider == "twelve_data":
            return adapters.TwelveDataHistoricalRunner()
        if self.provider == "finlab":
            return adapters.FinLabHistoricalRunner()

        def report(request):
            query = parse_qs(request.content.decode())
            product = query["commodity_id"][0]
            self.native(["report", product])
            text = "交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,成交量,結算價,未沖銷契約數,交易時段\n"
            text += f"{query['queryStartDate'][0]},{product},202610F1,100,110,90,105,37,-,-,一般\n"
            return httpx.Response(200, content=text.encode("cp950"))

        return TaifexHistoricalRunner(
            path=self.path / "taifex.sqlite3",
            http=httpx.Client(transport=httpx.MockTransport(report)),
            source=self.source_factory(self.config, self.registry),
            raw_store=self.raw,
            registry=self.registry,
            feed={"slot_id": "taiwan_market_window"},
            identity="local:matrix",
        )

    async def run(self):
        from findb_fetcher import acquisition
        from findb_fetcher.historical_backfill import HistoricalControlClient, run_once

        if self.provider == "taifex":
            # TAIFEX gets one dispatch attempt, so a minute rollover between
            # prepare and authorize defers the work by design. A single run
            # must therefore not start near a boundary of the (possibly
            # shifted) account clock.
            remaining = 60 - acquisition.time.time() % 60
            if remaining < MINUTE_ROLLOVER_MARGIN_SECONDS:
                time.sleep(remaining + 0.01)
        self.deadline = time.monotonic() + getattr(self, "execution_timeout", 25)

        def execute():
            target = self.runner()
            harness = self

            class Diagnostic:
                def run(self, item):
                    try:
                        return target.run(item)
                    except BaseException:
                        import traceback

                        harness.failure = traceback.format_exc()
                        raise

                def complete(self, control, item, result):
                    finish = getattr(target, "complete", None)
                    return (
                        finish(control, item, result) if finish else control.complete(item, result)
                    )

            with HistoricalControlClient(
                self.config, client=self.http(), provider=self.provider
            ) as control:
                return run_once(control, Diagnostic())

        return await asyncio.to_thread(execute)

    async def normalize(self):
        for run in self.runs:
            await _execute_run(self.db, self.engine, run)

    def release_process_lock(self):
        # Tests model the death of their own Fetcher execution process after an
        # uncertain ACK; no provider/SDK process is running at this boundary.
        for fd in self.queue._quarantine[:]:
            os.close(fd)
            self.queue._quarantine.remove(fd)
