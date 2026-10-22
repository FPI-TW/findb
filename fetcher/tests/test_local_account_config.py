"""Load the documented local dotenv in fresh processes without provider/network access."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import nullcontext
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

import pytest

from findb_fetcher.acquisition import Acquisition, Authorization
from findb_fetcher.full_market_runtime import _governed
from findb_fetcher.full_market_state import FullMarketState, QuotaBlockedError
from findb_fetcher.historical_backfill import run_once

FETCHER_DIR = Path(__file__).resolve().parents[1]
PROFILES = ("pilot", "full_market", "maintenance", "historical")


@pytest.fixture
def local_setup(tmp_path):
    local = tmp_path / "fetcher"
    local.mkdir()
    shutil.copytree(FETCHER_DIR / "configs", local / "configs")
    template = (FETCHER_DIR / ".env.example").read_text()
    template = template.replace(
        "FETCHER_CONTRACTS_DIR=../contracts",
        f"FETCHER_CONTRACTS_DIR={FETCHER_DIR.parent / 'contracts'}",
    )
    template = template.replace("replace-with-32-character-account-id", "0123456789abcdef" * 2)
    (local / ".env").write_text(template)
    return local


def _process(local, provider, mode):
    # uv, rather than a test-only parser, loads the same --env-file used by every
    # documented local CLI. No project resolution or dependency sync is needed.
    if shutil.which("uv") is None:
        pytest.skip("uv is required to load the documented --env-file")
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("FETCHER_", "SOURCE_", "FINLAB_", "SHIOAJI_", "TWELVE_DATA_"))
        and key not in {"FULL_MARKET_ENABLED", "APP_ENVIRONMENT", "finlab_id_token"}
    }
    env["PYTHONPATH"] = str(FETCHER_DIR / "src")
    result = subprocess.run(
        [
            "uv",
            "run",
            "--no-project",
            "--env-file",
            ".env",
            sys.executable,
            str(Path(__file__).resolve()),
            provider,
            mode,
        ],
        cwd=local,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.splitlines()[-1])


@pytest.mark.parametrize("provider", ["twelve_data", "finlab", "shioaji", "taifex"])
def test_documented_local_profiles_share_writable_ledger_and_restart_state(local_setup, provider):
    first = _process(local_setup, provider, "acquire")
    restarted = _process(local_setup, provider, "restart")
    expected = local_setup / ".state/account/governor.sqlite3"
    assert first["path"] == restarted["path"] == str(expected)
    assert first["snapshot"] == restarted["snapshot"]
    assert first["snapshot"]["pins"]
    assert sum(row[2] for row in first["snapshot"]["quota"]) == 8  # day + minute, four calls
    assert first["snapshot"]["pacing"][0][1] > time.time()
    assert first["provider_calls"] == first["source_calls"] == 4
    assert restarted["provider_calls"] == restarted["source_calls"] == 0
    assert first["profiles"] == restarted["profiles"] == list(PROFILES)
    assert expected.stat().st_uid == os.geteuid()
    assert expected.parent.stat().st_uid == os.geteuid()
    assert not (local_setup / ".state/full-market/state.sqlite3").exists()


def test_documented_local_finlab_scheduler_reaches_governed_sdk_boundary(local_setup):
    result = _process(local_setup, "finlab", "finlab-cli")
    assert result["status"] == "completed"
    assert result["provider_calls"] == 5
    assert result["source_calls"] == 5  # One fresh grant covers local login and get.
    assert result["path"] == str(local_setup / ".state/account/governor.sqlite3")
    assert sum(row[2] for row in result["snapshot"]["quota"]) == 10


def test_documented_local_config_still_requires_current_source_authorization(local_setup):
    result = _process(local_setup, "finlab", "denied")
    assert result["provider_calls"] == 0
    assert result["source_calls"] == 1
    assert result["snapshot"]["quota"] == []


def _probe(provider, mode):
    events = {"provider_calls": 0, "source_calls": 0}
    expected = (Path.cwd() / ".state/account/governor.sqlite3").resolve()
    assert os.environ["APP_ENVIRONMENT"] == "local"
    assert Path(os.environ["FETCHER_ACCOUNT_STATE_PATH"]).resolve() == expected

    class SourceDouble:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def authorization(self, method, path, *_args, **_kwargs):
            assert method == "GET"
            assert path.startswith(("scheduler-controls/", "full-market/authorization?"))
            events["source_calls"] += 1
            if mode == "denied":
                raise QuotaBlockedError("Source acquisition denied")
            now = time.time()
            return Authorization(time.monotonic(), now, now, provider=provider)

    def acquire():
        with Acquisition(provider) as operation:
            assert not operation.offline
            assert operation.state.path.resolve() == expected
            operation.start()
            events["provider_calls"] += 1
        return operation

    class FullDouble:
        def __init__(self):
            self.provider = provider

        execution_profile = "full_market"
        state = SimpleNamespace(path=Path(".state/full-market/state.sqlite3"))
        source = SourceDouble()

        def _acquisition_allowed(self):
            return os.environ["FULL_MARKET_ENABLED"] == "true"

        @_governed
        def _acquire(self):
            return acquire()

    def current_profile():
        profile = os.environ["FETCHER_CONSUMER_PROFILE"]
        if profile in {"full_market", "maintenance"}:
            return FullDouble()._acquire()
        if profile == "historical":
            item = SimpleNamespace(provider=provider)
            control = SimpleNamespace(
                claim=lambda: item,
                authorize=lambda _item: SourceDouble().authorization(
                    "GET", "full-market/authorization?consumer=historical"
                ),
                complete=lambda *_args: None,
                fail=lambda *_args: pytest.fail("historical acquisition failed"),
            )
            operations = []

            def run(_item):
                operations.append(acquire())
                return UUID(int=1)

            assert run_once(control, SimpleNamespace(run=run))
            return operations[0]
        return acquire()

    os.environ["FULL_MARKET_ENABLED"] = "true" if mode == "acquire" else "false"
    profiles = []
    with patch("findb_fetcher.full_market_client.FullMarketSourceClient", SourceDouble):
        if mode == "acquire":
            for profile in PROFILES:
                os.environ["FETCHER_CONSUMER_PROFILE"] = profile
                profiles.append(profile)
                operation = current_profile()
            # Retain a real recorded cooldown, with the same pins and request debt.
            operation.rate_limited("120")
        elif mode == "restart":
            for profile in PROFILES:
                os.environ["FETCHER_CONSUMER_PROFILE"] = profile
                profiles.append(profile)
                operation = Acquisition(provider)
                assert not operation.offline
                assert operation.state.path.resolve() == expected
            with (
                operation,
                patch(
                    "findb_fetcher.acquisition.time.sleep",
                    side_effect=QuotaBlockedError("retained cooldown"),
                ),
                pytest.raises(QuotaBlockedError, match="retained cooldown"),
            ):
                operation.start()
                pytest.fail("provider started during retained cooldown")
        elif mode == "denied":
            os.environ["FETCHER_CONSUMER_PROFILE"] = "pilot"
            with pytest.raises(QuotaBlockedError, match="Source acquisition denied"):
                acquire()
        else:
            _finlab_cli_probe(events)

    state = FullMarketState(expected)
    with state.connection() as db:
        snapshot = {
            "pins": [
                tuple(row) for row in db.execute("SELECT * FROM full_account_identity ORDER BY 1,2")
            ],
            "quota": [tuple(row) for row in db.execute("SELECT * FROM full_quota ORDER BY 1,2")],
            "pacing": [tuple(row) for row in db.execute("SELECT * FROM full_pacing ORDER BY 1")],
        }
    return {"path": str(expected), "snapshot": snapshot, "profiles": profiles, **events}


def _finlab_cli_probe(events):
    from findb_fetcher import finlab_scheduler_cli as cli
    from findb_fetcher.market_calendar import CalendarDay
    from findb_fetcher.providers.finlab import FinLabSdkGateway
    from findb_fetcher.raw_storage import RawObject

    target = date(2026, 7, 29)

    def get(dataset):
        events["provider_calls"] += 1
        value = 1000 if dataset == "price:成交股數" else 10
        return SimpleNamespace(
            index=(target,),
            columns=("2330", "2317"),
            loc={(target, symbol): value for symbol in ("2330", "2317")},
        )

    gateway = FinLabSdkGateway.from_env()
    object.__setattr__(
        gateway, "_sdk", SimpleNamespace(login=lambda _token: None, data=SimpleNamespace(get=get))
    )
    calendar = SimpleNamespace(get_day=lambda _market, day: (CalendarDay(day, "open", True), 7))
    source = SimpleNamespace(
        prepare=lambda request: SimpleNamespace(
            body=b"prepared",
            idempotency_key=request["idempotency_key"],
            dataset_key=request["dataset_key"],
            schema_id="market_eod",
            schema_version=1,
        ),
        deliver=lambda *_args, **_kwargs: SimpleNamespace(
            attempt_id=UUID(int=1), run_id=UUID(int=2)
        ),
        get_run_status=lambda *_args, **_kwargs: SimpleNamespace(
            run_id=UUID(int=2),
            status="completed",
            total_records=2,
            success_records=2,
            failed_records=0,
        ),
    )
    raw = SimpleNamespace(
        persist=lambda body, **_kwargs: RawObject(
            "r2://0123456789abcdef0123456789abcdef/findb-raw/finlab.json",
            hashlib.sha256(body).hexdigest(),
            len(body),
        )
    )
    with (
        patch.object(cli, "PublishedCalendarClient", lambda *_args: nullcontext(calendar)),
        patch.object(cli, "SourceAPIClient", lambda *_args: nullcontext(source)),
        patch.object(cli, "R2RawPayloadStore", lambda *_args: raw),
        patch.object(FinLabSdkGateway, "from_env", lambda: gateway),
    ):
        assert cli.main(["--as-of", "2026-07-29T08:00:00Z"]) == cli.EXIT_OK
    events["status"] = "completed"


if __name__ == "__main__":
    print(json.dumps(_probe(sys.argv[1], sys.argv[2])))
