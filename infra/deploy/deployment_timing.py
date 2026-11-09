#!/usr/bin/env python3
"""Report deployment wall time without adding parallel jobs or hiding host checks."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path


def timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def union(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if result and start <= result[-1][1]:
            result[-1] = result[-1][0], max(result[-1][1], end)
        else:
            result.append((start, end))
    return result


def duration(intervals: list[tuple[float, float]]) -> float:
    return sum(end - start for start, end in union(intervals))


def subtract(
    intervals: list[tuple[float, float]], occupied: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    result = union(intervals)
    for left, right in union(occupied):
        pieces = []
        for start, end in result:
            if right <= start or left >= end:
                pieces.append((start, end))
            else:
                if start < left:
                    pieces.append((start, left))
                if end > right:
                    pieces.append((right, end))
        result = pieces
    return result


def report(jobs: list[dict]) -> dict:
    selected = [
        j
        for j in jobs
        if j.get("conclusion") != "skipped"
        and j.get("completed_at")
        and j["name"] not in ("deployment-summary",)
        and not j["name"].startswith("report-")
    ]
    if not selected:
        return {"status": "no_samples"}
    work, verify, waiting, stages = [], [], [], []
    for job in selected:
        span = timestamp(job["started_at"]), timestamp(job["completed_at"])
        if job["name"].startswith("verify /"):
            verify.append(span)
            continue
        waits = [
            (timestamp(s["started_at"]), timestamp(s["completed_at"]))
            for s in job.get("steps", [])
            if s["name"] == "Wait for same-revision FinDB staging rollout"
            and s.get("conclusion") != "skipped"
            and s.get("completed_at")
        ]
        waiting.extend(waits)
        work.extend(subtract([span], waits))
        phases = [
            {
                "name": step["name"],
                "seconds": timestamp(step["completed_at"]) - timestamp(step["started_at"]),
            }
            for step in job.get("steps", [])
            if step.get("started_at")
            and step.get("completed_at")
            and step.get("conclusion") != "skipped"
        ]
        stages.append({"name": job["name"], "seconds": span[1] - span[0], "steps": phases})
    has_rollout = any(" / deploy" in j["name"] for j in selected)
    total = max(timestamp(j["completed_at"]) for j in selected) - min(
        timestamp(j["started_at"]) for j in selected
    )
    verify_only = subtract(verify, work)
    wait_only = subtract(waiting, work + verify_only)
    net = total - duration(verify_only) - duration(wait_only)
    return {
        "status": ("success" if has_rollout else "skipped")
        if all(j.get("conclusion") == "success" for j in selected)
        else "failed",
        "wall_seconds": total,
        "verify_seconds": duration(verify_only),
        "dependency_wait_seconds": duration(wait_only),
        "deployment_seconds": net,
        "under_300_seconds": has_rollout
        and net < 300
        and all(j.get("conclusion") == "success" for j in selected),
        "stages": stages,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = subprocess.check_output(
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            f"repos/{args.repository}/actions/runs/{args.run_id}/jobs?per_page=100",
        ],
        text=True,
    )
    result = report([job for page in json.loads(raw) for job in page["jobs"]])
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    lines = ["### 部署耗時", "", "| 項目 | 秒 |", "| --- | ---: |"]
    for key, label in (
        ("wall_seconds", "總牆鐘"),
        ("verify_seconds", "僅 verify/test 阻塞"),
        ("dependency_wait_seconds", "僅跨單元等待"),
        ("deployment_seconds", "部署淨耗時"),
    ):
        if key in result:
            lines.append(f"| {label} | {result[key]:.0f} |")
    lines += [
        "",
        f"執行結果：{result['status']}。五分鐘為增量部署驗收目標，冷建置與 migration 需另行分類；失敗樣本不視為達標。",
        "",
    ]
    lines += ["| Job／階段（並行時間不累加） | 秒 |", "| --- | ---: |"]
    for stage in result.get("stages", []):
        lines.append(f"| {stage['name']} | {stage['seconds']:.0f} |")
        for phase in stage["steps"]:
            lines.append(f"| {stage['name']} / {phase['name']} | {phase['seconds']:.0f} |")
    lines.append("")
    if result.get("deployment_seconds", 0) >= 300:
        print("::warning title=部署超過五分鐘::請查看部署耗時與各階段紀錄。")
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as output:
        output.write("\n".join(lines))


if __name__ == "__main__":
    main()
