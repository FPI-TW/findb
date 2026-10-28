"""Generate identical environment manifests from the shared seven-feed catalog."""

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    catalog = json.loads((root / "backend/configs/execution_feeds.v1.json").read_text())
    if catalog["version"] != 1 or len(catalog["feeds"]) != 7:
        raise ValueError("Expected the versioned seven-feed catalog")
    stale = []
    for target in ("local", "staging", "production"):
        path = root / f"fetcher/configs/full_market.{target}.v1.json"
        body = (
            json.dumps(
                {
                    "version": 1,
                    "deployment_target": target,
                    "desired_state": "stopped",
                    "poll_seconds": 30,
                    "feeds": catalog["feeds"],
                    "daily_runtime_policies": catalog["daily_runtime_policies"],
                },
                indent=2,
            )
            + "\n"
        )
        if args.check:
            if not path.exists() or path.read_text() != body:
                stale.append(str(path.relative_to(root)))
        else:
            path.write_text(body)
    if stale:
        raise SystemExit("Stale execution feed manifests: " + ", ".join(stale))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
