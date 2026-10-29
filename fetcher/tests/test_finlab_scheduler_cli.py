from __future__ import annotations

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


def test_parser_normalizes_aware_as_of_to_utc() -> None:
    parsed = finlab_scheduler_cli.build_parser().parse_args(
        ["--as-of", "2026-07-29T08:00:00+08:00"]
    )
    assert parsed.as_of.isoformat() == "2026-07-29T00:00:00+00:00"
