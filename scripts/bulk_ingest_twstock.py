#!/usr/bin/env python3
"""Bulk ingest TWStock MultiCharts CSVs via the FinDB Source API.

Scans <root>/{equity,index,future}/*-Day-Trade.csv and POSTs each file to
POST /api/v1/source/ingest/twstock/direct, chunking large files. Per-file
results are written under .ingest_log/<kind>/<SYMBOL>.json.

Required env (read via python-dotenv from repo root .env):
  SOURCE_API_KEY      preferred, singular form actually consumed by the app
  SOURCE_API_KEYS     fallback, comma-separated; first value is used
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

KINDS: tuple[str, ...] = ("equity", "index", "future")
KIND_ASSET_CLASS: dict[str, str] = {
    "equity": "equity",
    "index": "index",
    "future": "future",
}
HEADER_COLS: tuple[str, ...] = (
    "Symbol",
    "Date",
    "Open",
    "High",
    "Low",
    "Close",
    "UpVolume",
    "DownVolume",
    "TotalVolume",
    "UpTicks",
    "DownTicks",
    "TotalTicks",
)
NUMERIC_FLOAT_COLS = ("Open", "High", "Low", "Close")
NUMERIC_INT_COLS = (
    "UpVolume",
    "DownVolume",
    "TotalVolume",
    "UpTicks",
    "DownTicks",
    "TotalTicks",
)
ROW_FIELD_MAP = {
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "UpVolume": "up_volume",
    "DownVolume": "down_volume",
    "TotalVolume": "total_volume",
    "UpTicks": "up_ticks",
    "DownTicks": "down_ticks",
    "TotalTicks": "total_ticks",
}


@dataclass
class FileTarget:
    kind: str
    symbol: str  # for daily multi-symbol files: the file stem (e.g. "daily_2026-05-06")
    path: Path
    mode: str = "per_symbol_history"  # per_symbol_history | daily_mixed


@dataclass
class ChunkResult:
    chunk: int
    rows: int
    status: str  # ok | failed
    http_status: int | None = None
    elapsed_ms: float | None = None
    run_id: str | None = None
    error: str | None = None


@dataclass
class FileResult:
    target: FileTarget
    rows_total: int = 0
    rows_skipped: int = 0
    chunks: list[ChunkResult] = field(default_factory=list)
    status: str = "pending"  # ok | partial | failed | dry-run | skipped
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--root", default="/mnt/c/Users/User/Downloads/TWStock/history", type=Path)
    p.add_argument("--base-url", default="http://localhost:8080")
    p.add_argument(
        "--api-key",
        default=None,
        help="Override; otherwise read SOURCE_API_KEY then SOURCE_API_KEYS[0] from env",
    )
    p.add_argument("--only", choices=["equity", "index", "future", "all"], default="all")
    p.add_argument(
        "--symbols", default="", help="Comma-separated SYMBOL filter (matches filename prefix)"
    )
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--chunk-size", type=int, default=500)
    p.add_argument("--retry", type=int, default=3)
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--log-dir", default=".ingest_log", type=Path)
    return p.parse_args()


def resolve_api_key(arg: str | None) -> str:
    if arg:
        return arg.strip()
    key = os.environ.get("SOURCE_API_KEY", "").strip()
    if key:
        return key
    keys = os.environ.get("SOURCE_API_KEYS", "")
    first = next((k.strip() for k in keys.split(",") if k.strip()), "")
    if not first:
        sys.exit("ERROR: no API key. Set --api-key, SOURCE_API_KEY, or SOURCE_API_KEYS in .env")
    return first


def discover_files(root: Path, only: str, symbols_csv: str) -> list[FileTarget]:
    if not root.exists():
        sys.exit(f"ERROR: root does not exist: {root}")
    wanted = KINDS if only == "all" else (only,)
    symbol_filter = {s.strip() for s in symbols_csv.split(",") if s.strip()}
    targets: list[FileTarget] = []
    for kind in wanted:
        kind_dir = root / kind
        if not kind_dir.is_dir():
            continue
        # future/ has succeed|failed subdirs; recurse one level for any kind to be safe
        for path in sorted(kind_dir.rglob("*-Day-Trade.csv")):
            symbol = path.name.removesuffix("-Day-Trade.csv")
            if symbol_filter and symbol not in symbol_filter:
                continue
            targets.append(
                FileTarget(kind=kind, symbol=symbol, path=path, mode="per_symbol_history")
            )
        # Daily multi-symbol files: daily_YYYY-MM-DD.csv (or any daily_*.csv)
        for path in sorted(kind_dir.rglob("daily_*.csv")):
            stem = path.stem  # e.g. "daily_2026-05-06"
            # symbol_filter doesn't apply to daily files (they hold many symbols)
            targets.append(FileTarget(kind=kind, symbol=stem, path=path, mode="daily_mixed"))
    return targets


def parse_csv_rows(
    path: Path, expected_symbol: str, mode: str = "per_symbol_history"
) -> tuple[list[dict[str, Any]], int, list[str]]:
    """Return (rows, skipped_count, warnings).

    In ``per_symbol_history`` mode, the per-row Symbol column is informational
    (filename is authoritative). In ``daily_mixed`` mode, each row's Symbol is
    required and is included in the produced row dict so the caller can post
    payloads with mixed symbols (metadata.symbol left empty).
    """
    rows: list[dict[str, Any]] = []
    skipped = 0
    warnings: list[str] = []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None or set(reader.fieldnames) != set(HEADER_COLS):
            raise ValueError(f"unexpected CSV header: {reader.fieldnames}")
        seen_header_symbol = False
        for raw in reader:
            try:
                csv_symbol = (raw.get("Symbol") or "").strip()
                if mode == "daily_mixed":
                    if not csv_symbol:
                        raise ValueError("missing Symbol (required in daily_mixed mode)")
                else:
                    if not seen_header_symbol and csv_symbol and csv_symbol != expected_symbol:
                        warnings.append(
                            f"row Symbol={csv_symbol!r} differs from filename "
                            f"{expected_symbol!r}; using filename"
                        )
                        seen_header_symbol = True
                date_str = (raw.get("Date") or "").strip()
                if not date_str:
                    skipped += 1
                    continue
                date_iso = datetime.strptime(date_str, "%Y/%m/%d").date().isoformat()
                row: dict[str, Any] = {"date": date_iso}
                if mode == "daily_mixed":
                    row["symbol"] = csv_symbol
                for col in NUMERIC_FLOAT_COLS:
                    val = (raw.get(col) or "").strip()
                    if not val:
                        raise ValueError(f"missing {col}")
                    row[ROW_FIELD_MAP[col]] = float(val)
                for col in NUMERIC_INT_COLS:
                    val = (raw.get(col) or "").strip()
                    if not val:
                        raise ValueError(f"missing {col}")
                    row[ROW_FIELD_MAP[col]] = int(val)
                rows.append(row)
            except Exception as exc:
                skipped += 1
                if len(warnings) < 5:
                    warnings.append(f"skip row {reader.line_num}: {exc}")
    return rows, skipped, warnings


def chunked(rows: list[dict[str, Any]], n: int) -> list[list[dict[str, Any]]]:
    if n <= 0:
        return [rows]
    return [rows[i : i + n] for i in range(0, len(rows), n)]


def build_payload(
    symbol: str | None,
    file_name: str,
    rows: list[dict[str, Any]],
    asset_class: str | None = None,
) -> dict[str, Any]:
    """Build ingest payload. Pass symbol=None for daily_mixed mode (rows carry symbol)."""
    metadata: dict[str, Any] = {
        "source": "multicharts",
        "file_name": file_name,
        "query_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if symbol is not None:
        metadata["symbol"] = symbol
    if asset_class is not None:
        metadata["asset_class"] = asset_class
    return {"metadata": metadata, "data": rows}


async def post_chunk(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
    retry: int,
) -> tuple[int | None, dict[str, Any] | None, str | None, float]:
    """Return (http_status, json_body, error, elapsed_ms)."""
    last_err: str | None = None
    last_status: int | None = None
    backoff = 0.5
    started = time.perf_counter()
    for attempt in range(retry + 1):
        try:
            resp = await client.post(url, json=payload)
            last_status = resp.status_code
            if 200 <= resp.status_code < 300:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                try:
                    return resp.status_code, resp.json(), None, elapsed_ms
                except Exception as exc:
                    return resp.status_code, None, f"non-json body: {exc}", elapsed_ms
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
            else:
                # 4xx (not 429) — fatal, don't retry
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                return (
                    resp.status_code,
                    None,
                    f"HTTP {resp.status_code}: {resp.text[:200]}",
                    elapsed_ms,
                )
        except httpx.TransportError as exc:
            last_err = f"transport: {exc!r}"
        if attempt < retry:
            await asyncio.sleep(backoff)
            backoff *= 2
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return last_status, None, last_err or "unknown error", elapsed_ms


async def process_file(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    url: str,
    target: FileTarget,
    chunk_size: int,
    retry: int,
    dry_run: bool,
) -> FileResult:
    result = FileResult(target=target)
    result.started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        rows, skipped, warnings = parse_csv_rows(target.path, target.symbol, target.mode)
    except Exception as exc:
        result.status = "failed"
        result.error = f"csv parse error: {exc}"
        result.finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return result
    result.rows_total = len(rows)
    result.rows_skipped = skipped
    if warnings:
        result.error = "; ".join(warnings)
    if not rows:
        result.status = "skipped"
        result.finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return result

    chunks = chunked(rows, chunk_size)
    payload_symbol = None if target.mode == "daily_mixed" else target.symbol
    payload_asset_class = KIND_ASSET_CLASS.get(target.kind)
    if dry_run:
        for i, c in enumerate(chunks):
            result.chunks.append(ChunkResult(chunk=i, rows=len(c), status="dry-run"))
        result.status = "dry-run"
        result.finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return result

    async with sem:
        ok = True
        any_ok = False
        for i, c in enumerate(chunks):
            payload = build_payload(
                payload_symbol, target.path.name, c, asset_class=payload_asset_class
            )
            http_status, body, error, elapsed = await post_chunk(client, url, payload, retry)
            if error is None and http_status and 200 <= http_status < 300:
                any_ok = True
                result.chunks.append(
                    ChunkResult(
                        chunk=i,
                        rows=len(c),
                        status="ok",
                        http_status=http_status,
                        elapsed_ms=elapsed,
                        run_id=(body or {}).get("run_id") if isinstance(body, dict) else None,
                    )
                )
            else:
                ok = False
                result.chunks.append(
                    ChunkResult(
                        chunk=i,
                        rows=len(c),
                        status="failed",
                        http_status=http_status,
                        elapsed_ms=elapsed,
                        error=error,
                    )
                )
    if ok:
        result.status = "ok"
    elif any_ok:
        result.status = "partial"
    else:
        result.status = "failed"
    result.finished_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return result


def write_log(log_dir: Path, result: FileResult) -> None:
    out_dir = log_dir / result.target.kind
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{result.target.symbol}.json"
    payload = {
        "symbol": result.target.symbol,
        "kind": result.target.kind,
        "file": str(result.target.path),
        "rows_total": result.rows_total,
        "rows_skipped": result.rows_skipped,
        "status": result.status,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "error": result.error,
        "chunks": [c.__dict__ for c in result.chunks],
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))


def print_summary(results: list[FileResult], dry_run: bool) -> int:
    counts: dict[str, int] = {"ok": 0, "partial": 0, "failed": 0, "skipped": 0, "dry-run": 0}
    rows_ingested = 0
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
        if r.status in ("ok", "partial"):
            rows_ingested += sum(c.rows for c in r.chunks if c.status == "ok")
    print("\n== Summary ==")
    print(f"total files: {len(results)}")
    for k in ("ok", "partial", "failed", "skipped", "dry-run"):
        if counts.get(k):
            print(f"  {k:<10} {counts[k]}")
    print(f"rows ingested: {rows_ingested}")
    failed = [r for r in results if r.status in ("failed", "partial")]
    if failed:
        print("failed/partial files:")
        for r in failed:
            for c in r.chunks:
                if c.status != "ok":
                    print(
                        f"  {r.target.kind}/{r.target.symbol} chunk {c.chunk}: HTTP={c.http_status} {c.error}"
                    )
                    break
    if dry_run:
        return 0
    return 1 if (counts["failed"] or counts["partial"]) else 0


async def main_async(args: argparse.Namespace) -> int:
    api_key = resolve_api_key(args.api_key)
    targets = discover_files(args.root, args.only, args.symbols)
    if not targets:
        print("no files matched", file=sys.stderr)
        return 1
    print(
        f"discovered {len(targets)} files (only={args.only}, symbols={args.symbols or '*'}, dry_run={args.dry_run})"
    )

    url = f"{args.base_url.rstrip('/')}/api/v1/source/ingest/twstock/direct"
    headers = {"X-API-Key": api_key, "Content-Type": "application/json"}
    sem = asyncio.Semaphore(args.concurrency)

    results: list[FileResult] = []
    started_wall = time.perf_counter()
    async with httpx.AsyncClient(timeout=args.timeout, headers=headers) as client:
        coros = [
            process_file(client, sem, url, t, args.chunk_size, args.retry, args.dry_run)
            for t in targets
        ]
        done = 0
        total = len(coros)
        for fut in asyncio.as_completed(coros):
            r = await fut
            results.append(r)
            if not args.dry_run:
                write_log(args.log_dir, r)
            done += 1
            mark = {"ok": "✓", "partial": "~", "failed": "✗", "skipped": "·", "dry-run": "?"}.get(
                r.status, "?"
            )
            if done == 1 or done == total or done % 5 == 0:
                print(
                    f"[{done:>3}/{total}] {mark} {r.target.kind}/{r.target.symbol} ({r.rows_total} rows, {r.status})"
                )
    elapsed = time.perf_counter() - started_wall
    rc = print_summary(results, args.dry_run)
    print(f"elapsed: {elapsed:.1f}s")
    if not args.dry_run:
        print(f"log dir: {args.log_dir}")
    return rc


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    load_dotenv(dotenv_path=repo_root / ".env", override=False)
    args = parse_args()
    try:
        rc = asyncio.run(main_async(args))
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()
