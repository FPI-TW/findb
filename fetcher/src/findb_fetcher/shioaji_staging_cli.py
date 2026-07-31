"""CLI for the staging-only Shioaji coordinator."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Never, Sequence

from findb_fetcher.shioaji_staging import TAIPEI, Coordinator, _valid_target_date, load_manifest
from findb_fetcher.shioaji_staging_state import ShioajiStagingState

MAX_OUTPUT = 4096


class _Parser(argparse.ArgumentParser):
    """Argument failures are returned as the same bounded JSON envelope."""

    def error(self, _message: str) -> Never:
        raise ValueError("invalid arguments")


def main(argv: Sequence[str] | None = None) -> int:
    p = _Parser(add_help=False)
    p.add_argument("--check", action="store_true")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--deliver", action="store_true")
    mode.add_argument("--preflight", action="store_true")
    p.add_argument("--target-date", type=date.fromisoformat)
    p.add_argument(
        "--manifest",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "configs/shioaji_tw_staging.v1.json",
    )
    p.add_argument(
        "--state-path",
        type=Path,
        default=Path(
            os.getenv("FETCHER_SHIOAJI_STAGING_STATE_PATH", ".state/shioaji-staging.sqlite3")
        ),
    )
    try:
        a = p.parse_args(argv)
        m = load_manifest(a.manifest)
        if a.check:
            return _emit({"code": "CHECK_OK", "stage": "manifest", "count": len(m["sequences"])})
        # Validate before opening SQLite or constructing a gateway.  This is
        # deliberately repeated in Coordinator for direct callers.
        if a.target_date is None or not _valid_target_date(
            a.target_date, datetime.now(TAIPEI).date()
        ):
            return _emit({"code": "TARGET_DATE_INVALID", "stage": "setup", "count": 0}, 1)
        from findb_fetcher.providers.shioaji import IsolatedShioajiGateway

        state = ShioajiStagingState(a.state_path)
        try:
            if a.deliver:
                from findb_fetcher.client import SourceAPIClient
                from findb_fetcher.config import FetcherConfig
                from findb_fetcher.contracts import ContractRegistry
                from findb_fetcher.raw_storage import R2RawPayloadStore, RawStorageConfig

                config = FetcherConfig.from_env()
                raw_storage_config = RawStorageConfig.from_env()
                state.bind_raw_storage(raw_storage_config.account_id, raw_storage_config.bucket)
                with SourceAPIClient(
                    config,
                    ContractRegistry(config.contracts_dir),
                ) as source:
                    results = Coordinator(
                        state,
                        m,
                        IsolatedShioajiGateway.from_env(),
                        raw_store=R2RawPayloadStore(raw_storage_config),
                        source=source,
                    ).run(a.target_date, True)
            else:
                results = Coordinator(state, m, IsolatedShioajiGateway.from_env()).run(
                    a.target_date, preflight=a.preflight
                )
        finally:
            state.close()
        accepted = {"completed"} if a.deliver else {"VALIDATED"}
        successful = (
            bool(results)
            and all(result.code in accepted for result in results)
            and (not a.preflight or len(results) == 1 and results[0].code == "VALIDATED")
        )
        return _emit(
            {
                "code": "OK" if successful else "RUN_FAILED",
                "stage": "complete",
                "count": sum(x.count for x in results),
                "results": [
                    {
                        "code": x.code,
                        "stage": x.stage,
                        "count": x.count,
                        "retryable": x.retryable,
                        "request_id": x.request_id,
                        "run_id": x.run_id,
                        "symbol": x.symbol,
                        "reason": x.reason,
                    }
                    for x in results
                ],
            },
            0 if successful else 1,
        )
    except Exception:
        return _emit({"code": "SAFE_FAILURE", "stage": "setup", "count": 0}, 1)


def _emit(value: dict[str, object], status: int = 0) -> int:
    # Never slice JSON: a bounded result must remain a valid JSON document.
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=True)
    if len(raw) > MAX_OUTPUT:
        raw = json.dumps({"code": "SAFE_FAILURE", "stage": "output", "count": 0})
    sys.stdout.write(raw + "\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
