"""Dry-run-first operator CLI for production EOD backfill requests."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any, Never, Sequence

import httpx

from findb_fetcher.finlab_universe import load_finlab_universe
from findb_fetcher.schedule import load_schedule_manifest
from findb_fetcher.universe import load_symbol_universe


class BackfillCLIError(RuntimeError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> Never:
        raise ValueError("invalid arguments")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="findb-fetch-backfill-plan")
    parser.add_argument("--provider", required=True, choices=("twelve_data", "finlab", "shioaji"))
    parser.add_argument("--dataset-key", required=True)
    parser.add_argument("--start-date", required=True, type=date.fromisoformat)
    parser.add_argument("--end-date", required=True, type=date.fromisoformat)
    parser.add_argument("--request-key")
    parser.add_argument("--schedule-file", type=Path, default=_default_schedule_file())
    parser.add_argument("--deliver", action="store_true")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=15)
    parser.add_argument("--wait-timeout-seconds", type=int, default=86400)
    return parser


def _default_schedule_file() -> Path:
    return Path(
        os.getenv(
            "FETCHER_HISTORICAL_SCHEDULE_FILE",
            "/app/configs/daily_scheduler.production.v3.json",
        )
    )


def _scope(path: Path, provider: str, dataset_key: str) -> tuple[int, int]:
    if provider == "shioaji":
        raise BackfillCLIError("Shioaji historical backfill is not supported")
    manifest = load_schedule_manifest(path)
    if manifest.deployment_target != "production":
        raise BackfillCLIError("backfill planner requires the production schedule")
    feeds = [
        feed
        for feed in manifest.feeds
        if feed.provider == provider and feed.dataset_key == dataset_key and feed.enabled
    ]
    if len(feeds) != 1:
        raise BackfillCLIError("requested production EOD scope is not enabled")
    feed = feeds[0]
    if provider == "twelve_data":
        symbols = len(load_symbol_universe(feed.universe_file).symbols)
        return symbols, symbols
    if provider == "finlab":
        symbols = len(load_finlab_universe(feed.universe_file).symbols)
        return 5, symbols
    raise BackfillCLIError("requested production EOD provider is unsupported")


def _request(
    client: httpx.Client,
    base_url: str,
    api_key: str,
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> dict[str, Any]:
    try:
        response = client.request(
            method,
            f"{base_url.rstrip('/')}{path}",
            headers={"X-API-Key": api_key, "Accept": "application/json"},
            json=body,
        )
        response.raise_for_status()
        value = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise BackfillCLIError("production backfill control request failed") from exc
    if not isinstance(value, dict):
        raise BackfillCLIError("production backfill control response is invalid")
    return value


def _find_request(value: dict[str, Any], request_id: str) -> dict[str, Any] | None:
    rows = value.get("data")
    if not isinstance(rows, list):
        raise BackfillCLIError("production backfill list response is invalid")
    return next(
        (row for row in rows if isinstance(row, dict) and row.get("request_id") == request_id),
        None,
    )


def main(argv: Sequence[str] | None = None, *, client: httpx.Client | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        if args.start_date > args.end_date or args.wait and not args.deliver:
            raise BackfillCLIError("invalid production backfill arguments")
        calls_per_day, rows_per_day = _scope(args.schedule_file, args.provider, args.dataset_key)
        base_url = os.environ["FINDB_ADMIN_API_URL"]
        api_key = os.environ["FINDB_BACKFILL_ADMIN_API_KEY"]
        request_key = args.request_key or (
            f"production:{args.provider}:{args.dataset_key}:{args.start_date}:{args.end_date}"
        )
        body = {
            "provider": args.provider,
            "dataset_key": args.dataset_key,
            "start_date": args.start_date.isoformat(),
            "end_date": args.end_date.isoformat(),
            "request_key": request_key,
        }
        owns_client = client is None
        active_client = client or httpx.Client(timeout=30)
        try:
            preview = _request(
                active_client,
                base_url,
                api_key,
                "POST",
                "/api/v1/admin/historical-backfills/preview",
                {key: body[key] for key in ("provider", "dataset_key", "start_date", "end_date")},
            )
            days = preview.get("days")
            if not isinstance(days, list):
                raise BackfillCLIError("production backfill preview is invalid")
            valid_days = sum(
                1 for item in days if isinstance(item, dict) and item.get("valid") is True
            )
            plan = {
                "mode": "deliver" if args.deliver else "dry_run",
                "request": body,
                "scope_valid": preview.get("scope_valid") is True,
                "valid_trade_dates": valid_days,
                "estimated_provider_calls": valid_days * calls_per_day,
                "estimated_rows": valid_days * rows_per_day,
                "preview": preview,
            }
            if not args.deliver:
                print(json.dumps(plan, sort_keys=True, separators=(",", ":")))
                return 0 if plan["scope_valid"] else 2
            if plan["scope_valid"] is not True or valid_days == 0:
                raise BackfillCLIError("production backfill preview did not pass")
            created = _request(
                active_client,
                base_url,
                api_key,
                "POST",
                "/api/v1/admin/historical-backfills",
                body,
            )
            plan["created"] = created
            if args.wait:
                deadline = time.monotonic() + args.wait_timeout_seconds
                request_id = str(created.get("request_id", ""))
                while time.monotonic() < deadline:
                    listing = _request(
                        active_client,
                        base_url,
                        api_key,
                        "GET",
                        "/api/v1/admin/historical-backfills?page=1&page_size=100",
                        None,
                    )
                    current = _find_request(listing, request_id)
                    if current is not None and current.get("status") in {
                        "completed",
                        "failed",
                        "cancelled",
                    }:
                        plan["terminal"] = current
                        print(json.dumps(plan, sort_keys=True, separators=(",", ":")))
                        return 0 if current["status"] == "completed" else 3
                    time.sleep(args.poll_seconds)
                raise BackfillCLIError("production backfill wait timed out")
            print(json.dumps(plan, sort_keys=True, separators=(",", ":")))
            return 0
        finally:
            if owns_client:
                active_client.close()
    except (BackfillCLIError, KeyError, ValueError):
        print('{"error":"production_backfill_failed"}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
