"""Every independent operation ledger distinguishes Source wait from dispatch."""

from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from findb_fetcher import acquisition as acq
from findb_fetcher.config import FetcherConfig
from findb_fetcher.factset_eod import FactSetEOD
from findb_fetcher.full_market_client import FullMarketProtocolError
from findb_fetcher.historical_backfill import HistoricalControlClient, HistoricalControlError
from findb_fetcher.historical_runtime import _execute
from findb_fetcher.provider_work import ProviderWorkSourceUnavailableError
from findb_fetcher.shioaji_staging import Coordinator
from findb_fetcher.shioaji_staging_state import ShioajiStagingState
from findb_fetcher.taifex_historical import TaifexHistoricalRunner
from provider_work_support import owned_unit_work
from test_full_market import _runtime
from test_shioaji_staging import _single_manifest, _taipei_now


@pytest.mark.parametrize("lane", ["historical", "maintenance", "factset", "taifex", "shioaji"])
@pytest.mark.parametrize("dispatched", [False, True])
def test_source_wait_retains_only_undispatched_reservation(tmp_path, lane, dispatched):
    provider = lane if lane in {"factset", "taifex", "shioaji"} else "finlab"
    runtime, _, _ = _runtime(tmp_path, provider=provider)
    state = None
    try:
        with owned_unit_work(provider, tmp_path) as owner:

            def action(*args, **kwargs):
                if dispatched:
                    owner.dispatch_observations += 1
                raise ProviderWorkSourceUnavailableError("Source authorization unavailable")

            if lane == "historical":

                @dataclass
                class Job:
                    job_key: str = "historical-original"
                    attempt_count: int = 0

                key = "historical-original"
                owner.journal.reserve_attempt(key, 8)
                with pytest.raises(ProviderWorkSourceUnavailableError):
                    _execute(
                        SimpleNamespace(execute=action), SimpleNamespace(max_attempts=8), Job()
                    )
            elif lane == "maintenance":
                key = "maintenance:universe_refresh"
                owner.journal.reserve_attempt(key, 8)
                owner.work.update(state="open", dataset_key=runtime.feeds[0]["dataset_key"])
                runtime.sync_universes = action
                with pytest.raises(ProviderWorkSourceUnavailableError):
                    runtime._run_queued_maintenance(owner)
            elif lane == "factset":
                key = "factset-eod:snapshot"
                owner.journal.reserve_attempt(key, 3)
                with pytest.raises(ProviderWorkSourceUnavailableError):
                    FactSetEOD(None, None, None)._saved("snapshot", action)
            elif lane == "taifex":
                saved = []
                runner = SimpleNamespace(
                    clock=lambda: 1000,
                    _cooldown=lambda: 0,
                    sleep=lambda _: pytest.fail("unexpected delay"),
                    _save=lambda *args: saved.append(1),
                )
                progress = {"operations": {"fetch:original": {"attempts": 1, "next_at": 0}}}
                with pytest.raises(ProviderWorkSourceUnavailableError):
                    TaifexHistoricalRunner._operation(
                        runner,
                        SimpleNamespace(lease_expires_at=None),
                        progress,
                        "fetch:original",
                        action,
                    )
                assert progress["operations"]["fetch:original"]["attempts"] == 1 + int(dispatched)
                audit = progress["operation_history"][-1]
                assert audit["outcome"] == ("unknown" if dispatched else "source_wait")
                assert len(saved) == (1 if dispatched else 2)
                return
            else:
                state = ShioajiStagingState(tmp_path / "shioaji.sqlite3")
                coordinator = Coordinator(
                    state,
                    _single_manifest(),
                    SimpleNamespace(fetch_kbars=action),
                    now=_taipei_now,
                    terminal_on_exhaustion=True,
                )
                for _ in range(1 if dispatched else 12):
                    with pytest.raises(ProviderWorkSourceUnavailableError):
                        coordinator.run(date(2026, 7, 29))
                daily_id = "shioaji_tw_staging_v1:2026-07-29"
                assert state.budget_used(daily_id, "2330") == int(dispatched)
                assert state.get_terminal(daily_id, "2330") is None
                waiting = state.db.execute(
                    "SELECT count(*) FROM provider_attempts WHERE outcome='waiting' AND code='SOURCE_WAIT'"
                ).fetchone()[0]
                assert waiting == (0 if dispatched else 12)
                return
            with owner.journal.connection() as db:
                assert db.execute(
                    "SELECT attempts FROM work_budget WHERE operation_key=?", (key,)
                ).fetchone()[0] == 1 + int(dispatched)
    finally:
        if state is not None:
            state.close()
        runtime.close()


@pytest.mark.parametrize("dispatched", [False, True])
@pytest.mark.parametrize(
    "error",
    [
        FullMarketProtocolError("Source unavailable", status_code=521),
        HistoricalControlError("historical authorization unavailable", status_code=503),
    ],
)
def test_real_policy_callback_error_crosses_sdk_boundary_as_recovery(tmp_path, dispatched, error):
    with owned_unit_work("finlab", tmp_path):

        def unavailable():
            raise error

        with acq.authorization_scope(unavailable, state_path=tmp_path / "account.sqlite3"):
            with acq.Acquisition("finlab", credential="offline") as operation:
                if dispatched:
                    operation.possibly_started = True
                with pytest.raises(ProviderWorkSourceUnavailableError):
                    operation.authorize()


def test_actual_taifex_transport_authorization_wait_never_spends_operation_budget(tmp_path):
    calls, saved = [], []

    def unavailable(request):
        calls.append(request.url.path)
        raise httpx.ConnectError("private Source URL", request=request)

    item = SimpleNamespace(
        provider="taifex",
        dataset_key="tw_futures_eod",
        item_id=UUID(int=1),
        request_id=UUID(int=2),
        lease_token=UUID(int=3),
        lease_expires_at=None,
    )
    config = FetcherConfig("https://source.example", "private", max_attempts=3)
    with (
        owned_unit_work("taifex", tmp_path) as owner,
        httpx.Client(transport=httpx.MockTransport(unavailable)) as http,
        HistoricalControlClient(config, provider="taifex", client=http) as control,
        acq.authorization_scope(
            lambda: control.authorize(item), state_path=tmp_path / "account.sqlite3"
        ),
    ):
        clock = [1000.0]
        runner = SimpleNamespace(
            clock=lambda: clock[0],
            _cooldown=lambda: 0,
            sleep=lambda _: pytest.fail("unexpected delay"),
            _save=lambda *args: saved.append(1),
        )
        progress = {"operations": {"fetch:original": {"attempts": 1, "next_at": 0}}}

        def action():
            with acq.Acquisition("taifex", credential="offline") as operation:
                operation.authorize()
            pytest.fail("Source transport outage reached provider dispatch")

        for _ in range(8):
            with pytest.raises(ProviderWorkSourceUnavailableError) as error:
                TaifexHistoricalRunner._operation(runner, item, progress, "fetch:original", action)
            assert isinstance(error.value.__cause__, HistoricalControlError)
            assert error.value.__cause__.retryable
            assert error.value.__cause__.status_code is None
            assert progress["operations"]["fetch:original"]["attempts"] == 1
            clock[0] += 61
        assert owner.dispatch_observations == 0
        assert len(calls) == 8  # TAIFEX authorization retains its one-attempt RPC bound.
        assert len(saved) == 16
        assert all(row["outcome"] == "source_wait" for row in progress["operation_history"])
