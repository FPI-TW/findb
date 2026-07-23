"""One-shot ingest + completeness reporter for FinLab tw-updater JSONL output.

讀取 tw-updater 產生的 `<name>.jsonl`（以及同目錄的 `<name>_failures.jsonl`），
切片後 POST 到本機 `/api/v1/source/ingest/twstock/direct`，等待 normalize 完成，
再用 Serve API 抽查幾個 symbol；最後產生一份 markdown 完整性報告於 `artifacts/`。

Usage:
    uv run python scripts/ingest_finlab_jsonl.py --file <path-to-jsonl>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx

# Ensure project root is importable so we can pull SOURCE_API_KEY from app.config.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import get_settings  # noqa: E402

QUERY_DATE_FIELD = "query_date"
TRADE_DATE_FIELD = "date"


@dataclass
class ChunkOutcome:
    index: int
    run_id: str | None
    http_status: int
    raw_records: int
    returned_status: str | None = None
    duplicate: bool = False
    error: str | None = None
    run_final_status: str | None = None
    run_total: int = 0
    run_success: int = 0
    run_failed: int = 0
    run_error_message: str | None = None


@dataclass
class FileStats:
    total: int = 0
    fresh: int = 0
    stale: int = 0
    query_date: str | None = None
    stale_date_top: list[tuple[str, int]] = field(default_factory=list)
    stale_symbols: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, type=Path, help="FinLab JSONL 路徑")
    parser.add_argument(
        "--failures",
        type=Path,
        default=None,
        help="failures.jsonl 路徑（預設同目錄 <name>_failures.jsonl）",
    )
    parser.add_argument(
        "--asset-class",
        choices=["STOCK", "ETF"],
        default="STOCK",
        help="metadata.asset_class，影響 dataset_key 路由",
    )
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--chunk-size", type=int, default=500)
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=2.0,
        help="每次輪詢 run 狀態的間隔秒數",
    )
    parser.add_argument(
        "--poll-max-attempts",
        type=int,
        default=60,
        help="每個 run 最多輪詢幾次（避免無限等待）",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=BACKEND_ROOT / "artifacts",
    )
    parser.add_argument(
        "--skip-ingest",
        action="store_true",
        help="只做檔案掃描與報告，不真正 POST",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=5,
        help="Serve API 抽查樣本數",
    )
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{line_no} invalid JSON: {exc}") from exc
    return rows


def scan_file(rows: list[dict[str, Any]], failures_path: Path | None) -> FileStats:
    stats = FileStats(total=len(rows))
    if not rows:
        return stats

    query_dates = {r.get(QUERY_DATE_FIELD) for r in rows}
    query_dates.discard(None)
    if len(query_dates) == 1:
        stats.query_date = next(iter(query_dates))
    else:
        stats.query_date = max(query_dates) if query_dates else None

    date_counter: Counter[str] = Counter()
    for row in rows:
        trade_date = row.get(TRADE_DATE_FIELD)
        if not trade_date:
            continue
        date_counter[trade_date] += 1
        if stats.query_date and trade_date == stats.query_date:
            stats.fresh += 1
        else:
            stats.stale += 1
            stats.stale_symbols.append(
                {
                    "symbol": row.get("symbol"),
                    "trade_date": trade_date,
                    "close": row.get("close"),
                    "volume": row.get("volume"),
                }
            )

    stats.stale_date_top = date_counter.most_common(10)

    if failures_path and failures_path.exists():
        with failures_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    stats.failures.append(json.loads(line))
                except json.JSONDecodeError:
                    stats.failures.append({"_raw": line})
    return stats


def chunked(rows: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [rows[i : i + size] for i in range(0, len(rows), size)]


def build_payload(
    rows: list[dict[str, Any]],
    asset_class: str,
    file_name: str,
    query_time_iso: str,
) -> dict[str, Any]:
    return {
        "metadata": {
            "source": "finlab",
            "asset_class": asset_class,
            "file_name": file_name,
            "query_time": query_time_iso,
        },
        "data": [
            {
                "symbol": r["symbol"],
                "date": r["date"],
                "open": r.get("open"),
                "high": r.get("high"),
                "low": r.get("low"),
                "close": r.get("close"),
                "total_volume": r.get("volume"),
                "total_ticks": r.get("total_ticks"),
            }
            for r in rows
        ],
    }


async def _await_run_completion(
    client: httpx.AsyncClient,
    outcome: ChunkOutcome,
    headers: dict[str, str],
    poll_seconds: float,
    max_attempts: int,
) -> None:
    """Block until the background normalize for this run finishes (or attempts run out)."""
    if not outcome.run_id:
        return
    for _ in range(max_attempts):
        await asyncio.sleep(poll_seconds)
        try:
            response = await client.get(
                f"/api/v1/source/runs/{outcome.run_id}",
                headers=headers,
                timeout=15.0,
            )
        except httpx.RequestError as exc:
            outcome.run_error_message = f"polling error: {exc!r}"
            return
        data = _safe_json(response) or {}
        outcome.run_final_status = data.get("status")
        outcome.run_total = int(data.get("total_records") or 0)
        outcome.run_success = int(data.get("success_records") or 0)
        outcome.run_failed = int(data.get("failed_records") or 0)
        outcome.run_error_message = data.get("error_message")
        if outcome.run_final_status not in {"pending", "processing"}:
            return


async def post_chunks(
    client: httpx.AsyncClient,
    payload_chunks: list[list[dict[str, Any]]],
    asset_class: str,
    file_name: str,
    query_time_iso: str,
    headers: dict[str, str],
    poll_seconds: float,
    max_poll_attempts: int,
) -> list[ChunkOutcome]:
    """POST 每個 chunk，並等該 chunk 的 background normalize 完成後才送下一個。

    序列化避免 trading_calendar / instruments 在多個 chunk 之間發生 deadlock。
    """
    results: list[ChunkOutcome] = []
    for idx, chunk in enumerate(payload_chunks):
        payload = build_payload(chunk, asset_class, file_name, query_time_iso)
        try:
            response = await client.post(
                "/api/v1/source/ingest/twstock/direct",
                json=payload,
                headers=headers,
                timeout=60.0,
            )
        except httpx.RequestError as exc:
            results.append(
                ChunkOutcome(
                    index=idx,
                    run_id=None,
                    http_status=-1,
                    raw_records=len(chunk),
                    error=f"transport error: {exc!r}",
                )
            )
            continue

        data = _safe_json(response)
        if response.status_code != 200:
            results.append(
                ChunkOutcome(
                    index=idx,
                    run_id=data.get("run_id") if isinstance(data, dict) else None,
                    http_status=response.status_code,
                    raw_records=len(chunk),
                    returned_status=(data or {}).get("status"),
                    error=str(data),
                )
            )
            continue

        outcome = ChunkOutcome(
            index=idx,
            run_id=str(data["run_id"]),
            http_status=response.status_code,
            raw_records=len(chunk),
            returned_status=data.get("status"),
            duplicate=str(data.get("message", "")).lower().startswith("duplicate"),
        )
        results.append(outcome)
        print(
            f"  chunk {idx + 1}/{len(payload_chunks)}: "
            f"http={response.status_code} run_id={outcome.run_id} "
            f"records={len(chunk)} duplicate={outcome.duplicate}",
            flush=True,
        )
        await _await_run_completion(client, outcome, headers, poll_seconds, max_poll_attempts)
        print(
            f"    -> run {outcome.run_id} status={outcome.run_final_status} "
            f"success={outcome.run_success} failed={outcome.run_failed}",
            flush=True,
        )
    return results


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except json.JSONDecodeError:
        return {"_text": response.text[:500]}


async def refresh_run_status(
    client: httpx.AsyncClient,
    chunk_results: list[ChunkOutcome],
    headers: dict[str, str],
) -> None:
    """Final refresh after all chunks finished — picks up any late status flips."""
    for outcome in chunk_results:
        if not outcome.run_id:
            continue
        try:
            response = await client.get(
                f"/api/v1/source/runs/{outcome.run_id}",
                headers=headers,
                timeout=15.0,
            )
        except httpx.RequestError as exc:
            outcome.run_error_message = f"polling error: {exc!r}"
            continue
        data = _safe_json(response) or {}
        outcome.run_final_status = data.get("status")
        outcome.run_total = int(data.get("total_records") or 0)
        outcome.run_success = int(data.get("success_records") or 0)
        outcome.run_failed = int(data.get("failed_records") or 0)
        outcome.run_error_message = data.get("error_message")


async def sample_serve(
    client: httpx.AsyncClient,
    rows: list[dict[str, Any]],
    query_date: str | None,
    sample_size: int,
) -> list[dict[str, Any]]:
    if not query_date:
        return []
    fresh_rows = [r for r in rows if r.get("date") == query_date][:sample_size]
    out: list[dict[str, Any]] = []
    for row in fresh_rows:
        symbol = row["symbol"]
        params = {
            "market": "TW",
            "symbols": symbol,
            "start_date": query_date,
            "end_date": query_date,
        }
        try:
            response = await client.get("/api/v1/serve/eod", params=params, timeout=15.0)
        except httpx.RequestError as exc:
            out.append({"symbol": symbol, "ok": False, "error": str(exc)})
            continue
        if response.status_code != 200:
            out.append(
                {
                    "symbol": symbol,
                    "ok": False,
                    "http_status": response.status_code,
                    "body": response.text[:200],
                }
            )
            continue
        payload = response.json()
        data = payload.get("data") or []
        if not data:
            out.append({"symbol": symbol, "ok": False, "error": "no rows returned"})
            continue
        served = data[0]
        out.append(
            {
                "symbol": symbol,
                "ok": True,
                "served_close": served.get("close"),
                "file_close": row.get("close"),
                "served_volume": served.get("volume"),
                "file_volume": row.get("volume"),
                "served_total_ticks": served.get("total_ticks"),
                "file_total_ticks": row.get("total_ticks"),
            }
        )
    return out


def _classify_stale(trade_date: str, query_date: str | None) -> str:
    if not query_date:
        return "未知"
    try:
        td = date.fromisoformat(trade_date)
        qd = date.fromisoformat(query_date)
    except ValueError:
        return "未知"
    gap = (qd - td).days
    if gap <= 7:
        return "可能停牌"
    if gap <= 30:
        return "短期停牌"
    return "疑似下市"


def render_report(
    file_path: Path,
    stats: FileStats,
    chunks: list[ChunkOutcome],
    samples: list[dict[str, Any]],
    asset_class: str,
) -> str:
    lines: list[str] = []
    lines.append("# FinLab JSONL 匯入完整性報告\n")
    lines.append(f"- 來源檔案：`{file_path}`")
    lines.append(f"- asset_class：`{asset_class}`")
    lines.append(f"- 報告時間（UTC）：{datetime.now(timezone.utc).isoformat()}")
    lines.append(f"- 觀測 query_date：`{stats.query_date}`\n")

    lines.append("## 1. 檔案統計\n")
    lines.append("| 指標 | 數量 |")
    lines.append("|---|---:|")
    lines.append(f"| 總筆數 | {stats.total} |")
    lines.append(f"| Fresh（date == query_date） | {stats.fresh} |")
    lines.append(f"| Stale（date < query_date） | {stats.stale} |")
    lines.append(f"| Failures（_failures.jsonl） | {len(stats.failures)} |")
    lines.append("")

    lines.append("### 1.1 trade_date 分佈 top 10\n")
    lines.append("| trade_date | rows |")
    lines.append("|---|---:|")
    for d, count in stats.stale_date_top:
        lines.append(f"| {d} | {count} |")
    lines.append("")

    lines.append("## 2. 匯入結果（每個 chunk 對應一個 run）\n")
    if not chunks:
        lines.append("_未執行 ingest（--skip-ingest 或上游失敗）_\n")
    else:
        lines.append(
            "| # | run_id | HTTP | records | duplicate | run_status | success | failed | error |"
        )
        lines.append("|---|---|---:|---:|---|---|---:|---:|---|")
        for c in chunks:
            err = (c.error or c.run_error_message or "").replace("\n", " ")[:80]
            lines.append(
                f"| {c.index + 1} | `{c.run_id or ''}` | {c.http_status} | "
                f"{c.raw_records} | {c.duplicate} | "
                f"{c.run_final_status or c.returned_status or ''} | "
                f"{c.run_success} | {c.run_failed} | {err} |"
            )
        total_success = sum(c.run_success for c in chunks)
        total_failed = sum(c.run_failed for c in chunks)
        lines.append("")
        lines.append(f"- normalize 寫入成功合計：**{total_success}** 筆")
        lines.append(f"- normalize 失敗合計：**{total_failed}** 筆")
    lines.append("")

    lines.append("## 3. Serve API 抽查\n")
    if not samples:
        lines.append("_未執行抽查（沒有 query_date 或 sample_size=0）_\n")
    else:
        lines.append(
            "| symbol | OK | served close | file close | served volume | file volume | 備註 |"
        )
        lines.append("|---|---|---:|---:|---:|---:|---|")
        for s in samples:
            note = "" if s["ok"] else str(s.get("error") or s.get("body", ""))[:60]
            lines.append(
                f"| {s['symbol']} | {'✅' if s['ok'] else '❌'} | "
                f"{s.get('served_close', '')} | {s.get('file_close', '')} | "
                f"{s.get('served_volume', '')} | {s.get('file_volume', '')} | {note} |"
            )
    lines.append("")

    lines.append("## 4. 發現的缺失\n")

    lines.append("### 4.A. FinLab 抓取失敗（KeyError 等）\n")
    if stats.failures:
        lines.append("| target | step | error_type | error_message |")
        lines.append("|---|---|---|---|")
        for f in stats.failures:
            lines.append(
                f"| {f.get('target', '')} | {f.get('step', '')} | "
                f"{f.get('error_type', '')} | {str(f.get('error_message', ''))[:80]} |"
            )
    else:
        lines.append("_無_\n")
    lines.append("")

    lines.append("### 4.B. Stale rows（date < query_date）\n")
    if stats.stale_symbols:
        sorted_stale = sorted(stats.stale_symbols, key=lambda r: r["trade_date"], reverse=True)
        lines.append(f"共 {len(sorted_stale)} 筆。前 30 筆：\n")
        lines.append("| symbol | trade_date | close | volume | 分類 |")
        lines.append("|---|---|---:|---:|---|")
        for row in sorted_stale[:30]:
            lines.append(
                f"| {row['symbol']} | {row['trade_date']} | {row['close']} | "
                f"{row['volume']} | {_classify_stale(row['trade_date'], stats.query_date)} |"
            )
    else:
        lines.append("_無_\n")
    lines.append("")

    lines.append("### 4.C. normalize 失敗（failed_records > 0）\n")
    failed_chunks = [c for c in chunks if c.run_failed > 0 or c.error]
    if failed_chunks:
        for c in failed_chunks:
            lines.append(
                f"- run `{c.run_id}` chunk {c.index + 1}: failed={c.run_failed} "
                f"error={(c.error or c.run_error_message or '').strip()}"
            )
    else:
        lines.append("_無_\n")
    lines.append("")

    lines.append("## 5. 補救建議\n")
    if stats.failures:
        lines.append(
            "- **A. FinLab 抓取失敗**：對清單中每個 target 查 TWSE/OTC 公開資訊。"
            "若仍上市 → 手動構造 1 筆 payload POST 至 `/api/v1/source/ingest/twstock/direct` 補入；"
            "若已下市 → 在 `instruments` 表將 status 標記為 `delisted`。"
        )
    if stats.stale_symbols:
        ratio = stats.stale / stats.total if stats.total else 0.0
        if ratio < 0.05:
            lines.append(
                f"- **B. Stale rows**：佔比 {ratio:.1%} < 5%，分布以 ≤ 30 日為主時可視為合理。"
                "報告 Section 4.B 已列出「疑似下市」者，建議定期清理 `instruments` 標記。"
            )
        else:
            lines.append(
                f"- **B. Stale rows**：佔比 {ratio:.1%} 偏高，先確認 FinLab API 端是否有部分服務中斷，"
                "再決定是否重新 fetch；目前資料已寫入但需要關注。"
            )
    if failed_chunks:
        lines.append(
            "- **C. normalize 失敗**：對應 run 可用 `POST /api/v1/source/runs/<run_id>/rerun` 重新跑；"
            "重跑後若仍失敗，將 `error_message` 連同 raw_payload 開 ticket。"
        )
    if not (stats.failures or stats.stale_symbols or failed_chunks):
        lines.append("_本次無發現任何缺失，無需補救。_")
    lines.append("")

    return "\n".join(lines)


def write_report(report_dir: Path, body: str) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = report_dir / f"finlab_ingest_report_{ts}.md"
    path.write_text(body, encoding="utf-8")
    return path


async def run(args: argparse.Namespace) -> Path:
    file_path = args.file.resolve()
    if not file_path.exists():
        raise SystemExit(f"input file not found: {file_path}")
    failures_path = args.failures or file_path.with_name(file_path.stem + "_failures.jsonl")

    print(f"讀取 {file_path}")
    rows = load_jsonl(file_path)
    stats = scan_file(rows, failures_path)
    print(
        f"  總筆數={stats.total} fresh={stats.fresh} stale={stats.stale} "
        f"failures={len(stats.failures)} query_date={stats.query_date}"
    )

    chunk_results: list[ChunkOutcome] = []
    samples: list[dict[str, Any]] = []

    settings = get_settings()
    api_key = (settings.SOURCE_API_KEY or "").strip()
    headers = {"X-API-Key": api_key} if api_key else {}

    if not args.skip_ingest:
        if not api_key:
            raise SystemExit(
                "SOURCE_API_KEY 為空；請在 .env 設定後重試，或加 --skip-ingest 只跑報告。"
            )

        query_time_iso = datetime.now(timezone.utc).isoformat()
        chunks = chunked(rows, args.chunk_size)
        print(f"POST 切成 {len(chunks)} 個 chunk，每個最多 {args.chunk_size} 筆")
        async with httpx.AsyncClient(base_url=args.base_url) as client:
            chunk_results = await post_chunks(
                client,
                chunks,
                args.asset_class,
                file_path.name,
                query_time_iso,
                headers,
                args.poll_seconds,
                args.poll_max_attempts,
            )
            await refresh_run_status(client, chunk_results, headers)
            samples = await sample_serve(client, rows, stats.query_date, args.sample_size)

    body = render_report(file_path, stats, chunk_results, samples, args.asset_class)
    report_path = write_report(args.report_dir, body)
    print(f"\n報告寫入：{report_path}")
    print("=" * 70)
    print(body)
    return report_path


def main() -> None:
    args = parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
