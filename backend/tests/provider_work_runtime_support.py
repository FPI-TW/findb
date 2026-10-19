"""Real fenced Fetcher/ASGI/worker harness; only native SDK/HTTP and R2 are fake."""

import asyncio
import hashlib
import json
import multiprocessing
import os
import time
from datetime import date, datetime, timedelta
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
from tests.test_canonical_ingest_api import _execute_run
from tests.test_provider_work import attest_cutover

DAY = date(2026, 10, 15)
SCOPES = [
    ("twelve_data", "us_equity_eod"),
    ("finlab", "tw_equity_eod"),
    ("shioaji", "tw_equity_minute"),
    ("shioaji", "tw_etf_minute"),
    ("taifex", "tw_futures_eod"),
]


class RuntimeHarness:
    def __init__(
        self, client, session, engine, monkeypatch, path, provider, dataset, historical_days=1
    ):
        self.client, self.db, self.engine, self.patch = client, session, engine, monkeypatch
        self.path, self.provider, self.dataset = path, provider, dataset
        self.environment = "staging"
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
        self.failure = None
        self.deadline = time.monotonic() + 25

    async def initialize(self):
        root = Path(__file__).resolve().parents[2]
        patch = self.patch
        patch.syspath_prepend(str(root / "fetcher/src"))
        from findb_fetcher import provider_work as coordinator
        from findb_fetcher.config import FetcherConfig
        from findb_fetcher.contracts import ContractRegistry

        self.queue = coordinator
        patch.setattr(get_settings(), "PROVIDER_WORK_REQUIRED", True)
        patch.setattr(get_settings(), "APP_ENVIRONMENT", self.environment)
        for key, value in {
            "APP_ENVIRONMENT": self.environment,
            "PROVIDER_WORK_REQUIRED": "true",
            "FETCHER_ACCOUNT_STATE_PATH": str(self.path / "account/governor.sqlite3"),
            "FETCHER_HISTORICAL_STATE_DIR": str(self.path / "historical"),
            "FETCHER_HISTORICAL_SCHEDULE_FILE": str(
                root / "fetcher/configs/daily_scheduler.v2.json"
            ),
            "FETCHER_FINLAB_UNIVERSE_FILE": str(
                root / "fetcher/configs/finlab_tw_review_required.v1.json"
            ),
            "SHIOAJI_API_KEY": "offline-shioaji",
            "SHIOAJI_SECRET_KEY": "offline-secret",
        }.items():
            patch.setenv(key, value)
        inodes = {}
        for provider, _ in SCOPES:
            with coordinator.host_lock(provider) as (_, inode):
                inodes[provider] = inode
        await attest_cutover(self.db, inodes)
        definition = next(row for row in DATASETS if row["dataset_key"] == self.dataset)
        self.db.add(DatasetRegistry(**definition))
        for profile in ("pilot", "full_market"):
            key = f"{profile}_{self.provider}_v1"
            self.db.add(
                SchedulerControl(
                    scheduler_key=key,
                    provider=self.provider,
                    execution_profile=profile,
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
        from findb_fetcher import shioaji_historical
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
        for module in (adapters, shioaji_historical):
            self.patch.setattr(module, "SourceAPIClient", source_factory)
            self.patch.setattr(module, "R2RawPayloadStore", lambda config: Raw())
        self.patch.setattr(adapters.RawStorageConfig, "from_env", classmethod(lambda cls: None))
        self.raw = Raw()

        def twelve_response(request):
            symbol = request.url.params["symbol"]
            self.native(["daily", symbol])
            return httpx.Response(
                200,
                json={
                    "meta": {
                        "symbol": symbol,
                        "currency": "HKD" if self.dataset == "hk_equity_eod" else "USD",
                        "exchange": "HKEX" if self.dataset == "hk_equity_eod" else "NASDAQ",
                        "mic_code": "XHKG" if self.dataset == "hk_equity_eod" else "XNAS",
                        "exchange_timezone": "Asia/Hong_Kong"
                        if self.dataset == "hk_equity_eod"
                        else "America/New_York",
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
                },
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
        self.install_shioaji_native()

    def install_shioaji_native(self):
        from multiprocessing.connection import Connection

        from findb_fetcher.providers import shioaji_session as sdk

        harness = self

        class API:
            def __init__(self, **kwargs):
                harness.native(["sdk_create"])
                self.Contracts = SimpleNamespace(
                    Stocks=SimpleNamespace(
                        TSE=[
                            SimpleNamespace(code=symbol, exchange="TSE")
                            for symbol in ("2330", "2317", "0050", "0056", "006201")
                        ],
                        OTC=[],
                    )
                )

            def login(self, **kwargs):
                harness.native(["sdk_login"])

            def logout(self):
                pass

            def usage(self):
                return {
                    "bytes": 0,
                    "limit_bytes": 500 * 1024 * 1024,
                    "remaining_bytes": 500 * 1024 * 1024,
                    "connections": 1,
                }

            def kbars(self, contract, **kwargs):
                harness.native(["kbars", contract.code])
                timestamp = (
                    int(datetime.fromisoformat(kwargs["start"] + "T09:01:00+00:00").timestamp())
                    * 10**9
                )
                return {
                    "ts": [timestamp],
                    "Open": [1],
                    "High": [1],
                    "Low": [1],
                    "Close": [1],
                    "Volume": [1],
                    "Amount": [1],
                }

        prepare = sdk.ShioajiSession.prepare_create
        self.patch.setattr(
            sdk.ShioajiSession,
            "prepare_create",
            lambda session, native=None: prepare(session, SimpleNamespace(Shioaji=API)),
        )
        version = sdk.importlib.metadata.version
        self.patch.setattr(
            sdk.importlib.metadata,
            "version",
            lambda name: (
                "1.7.1" if name == "shioaji" else "1.5.7" if name == "finlab" else version(name)
            ),
        )

        class NativeProcess:
            # Fork only injects an offline native SDK. The real isolated child,
            # FD transfer, READY/GO/current authorization and parent coordinator
            # all remain unchanged. No provider library/license/network is used.
            def __init__(self, fd):
                def entry():
                    os.setsid()
                    conn = Connection(fd)
                    settings = json.loads(conn.recv_bytes())
                    import sqlite3
                    import sys
                    import traceback

                    def trace_sqlite(frame, event, argument):
                        if frame.f_code is not sdk._child.__code__:
                            return None
                        if event == "exception" and isinstance(argument[1], sqlite3.Error):
                            # Only the offline fake launcher traces safe SQLite
                            # codes/frames; never SDK messages, values or keys.
                            error = argument[1]
                            with (harness.path / "sdk-boundary-errors.jsonl").open("a") as output:
                                output.write(
                                    json.dumps(
                                        {
                                            "sqlite_errorname": getattr(
                                                error, "sqlite_errorname", None
                                            ),
                                            "frames": [
                                                (Path(item.filename).name, item.lineno, item.name)
                                                for item in traceback.extract_tb(argument[2])
                                            ],
                                        }
                                    )
                                    + "\n"
                                )
                        return trace_sqlite

                    sys.settrace(trace_sqlite)
                    sdk._child(
                        conn, settings["api_key"], settings["secret_key"], settings["simulation"]
                    )

                self.child = multiprocessing.get_context("fork").Process(target=entry)
                self.child.start()
                self.pid = self.child.pid

            def is_alive(self):
                return self.child.is_alive()

            def join(self, timeout):
                self.child.join(timeout)

            def terminate(self):
                self.child.terminate()

            def kill(self):
                self.child.kill()

            def reap_descendants(self):
                self.child.join(2)
                assert not self.child.is_alive()

        self.patch.setattr(sdk, "_ProviderProcess", NativeProcess)
        from findb_fetcher.providers.shioaji import IsolatedShioajiGateway

        self.patch.setattr(
            IsolatedShioajiGateway,
            "from_env",
            classmethod(lambda cls: cls("offline-shioaji", "offline-secret", timeout_seconds=2)),
        )

    def runner(self):
        from findb_fetcher import historical_runtime as adapters
        from findb_fetcher.shioaji_historical import ExecutableShioajiHistoricalRunner
        from findb_fetcher.taifex_historical import TaifexHistoricalRunner

        if self.provider == "twelve_data":
            return adapters.TwelveDataHistoricalRunner()
        if self.provider == "finlab":
            return adapters.FinLabHistoricalRunner()
        if self.provider == "shioaji":
            return ExecutableShioajiHistoricalRunner(fetcher=self.config, contracts=self.registry)

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
        from findb_fetcher.historical_backfill import HistoricalControlClient, run_once

        self.deadline = time.monotonic() + 25

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
                    return target.complete(control, item, result)

            with HistoricalControlClient(
                self.config, client=self.http(), provider=self.provider
            ) as control:
                try:
                    return run_once(control, Diagnostic())
                finally:
                    Path(f"/tmp/findb-native-{self.provider}-{self.dataset}.json").write_text(
                        json.dumps(
                            {
                                "native": self.native_calls(),
                                "source": self.source_events,
                                "failure": self.failure,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )

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
