from __future__ import annotations

import json
from pathlib import Path

from findb_fetcher import finlab_scheduler_cli

CONFIG_DIR = Path(__file__).resolve().parents[1] / "configs"
CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"


def _env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SOURCE_API_URL", "https://source.example")
    monkeypatch.setenv("SOURCE_CLIENT_KEY", "source-key")
    monkeypatch.setenv("FINDB_SERVE_BASE_URL", "https://serve.example")
    monkeypatch.setenv("FETCHER_CALENDAR_SERVE_API_KEY", "calendar-key")
    monkeypatch.setenv("CLOUDFLARE_R2_ACCOUNT_ID", "0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("CLOUDFLARE_R2_RAW_BUCKET", "findb-raw")
    monkeypatch.setenv("CLOUDFLARE_R2_RAW_ACCESS_KEY_ID", "r2-key")
    monkeypatch.setenv("CLOUDFLARE_R2_RAW_SECRET_ACCESS_KEY", "r2-secret")
    monkeypatch.setenv("FINLAB_API_TOKEN", "token")
    monkeypatch.setenv("FETCHER_CONTRACTS_DIR", str(CONTRACTS_DIR))
    monkeypatch.setenv("FETCHER_FINLAB_STATE_PATH", str(tmp_path / "state.sqlite3"))


def test_check_mode_does_not_construct_network_or_sdk_clients(
    monkeypatch, tmp_path, capsys
) -> None:
    _env(monkeypatch, tmp_path)

    class Unexpected:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("external client constructed in --check")

    monkeypatch.setattr(finlab_scheduler_cli, "PublishedCalendarClient", Unexpected)
    monkeypatch.setattr(finlab_scheduler_cli, "SourceAPIClient", Unexpected)
    monkeypatch.setattr(finlab_scheduler_cli, "R2RawPayloadStore", Unexpected)
    monkeypatch.setattr(finlab_scheduler_cli.FinLabSdkGateway, "from_env", Unexpected)

    result = finlab_scheduler_cli.main(
        [
            "--check",
            "--schedule-file",
            str(CONFIG_DIR / "daily_scheduler.v2.json"),
            "--state-path",
            str(tmp_path / "state.sqlite3"),
        ]
    )

    assert result == finlab_scheduler_cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "dataset_key": "tw_equity_eod",
        "mode": "finlab_scheduler_check",
        "slot_id": "tw_1430",
        "status": "ok",
        "work_item_count": 1,
    }


def test_disabled_or_mismatched_feed_is_rejected_before_runtime_config(
    monkeypatch, tmp_path, capsys
) -> None:
    schedule = json.loads((CONFIG_DIR / "daily_scheduler.v2.json").read_text(encoding="utf-8"))
    feed = next(item for item in schedule["feeds"] if item["slot_id"] == "tw_1430")
    feed["enabled"] = False
    path = tmp_path / "disabled.json"
    path.write_text(json.dumps(schedule), encoding="utf-8")

    # No environment is set: a disabled feed must fail before runtime config
    # (and before any provider/R2/Source/calendar client) is touched.
    result = finlab_scheduler_cli.main(["--check", "--schedule-file", str(path)])

    assert result == finlab_scheduler_cli.EXIT_CONFIG_ERROR
    assert json.loads(capsys.readouterr().err) == {"error": "finlab_scheduler_configuration_failed"}


def test_parser_supports_required_modes_and_aware_as_of() -> None:
    parsed = finlab_scheduler_cli.build_parser().parse_args(
        ["--as-of", "2026-07-29T08:00:00+08:00"]
    )
    assert parsed.as_of.isoformat() == "2026-07-29T00:00:00+00:00"
    assert finlab_scheduler_cli.build_parser().parse_args(["--run-forever"]).run_forever
