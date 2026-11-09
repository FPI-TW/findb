"""Generate identical environment manifests from the shared five-feed catalog."""

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from scripts.export_provider_catalog import (  # noqa: E402
    CATALOG_PATH,
    legacy_execution_catalog,
    load_catalog,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    catalog = legacy_execution_catalog(
        load_catalog(root / CATALOG_PATH, contracts_dir=root / "contracts")
    )
    if catalog["version"] != 1 or len(catalog["feeds"]) != 5:
        raise ValueError("Expected the versioned five-feed catalog")
    # Drain-only providers keep their Source scope, but Fetcher must not plan them.
    drain_only = set(catalog["drain_only_providers"])
    feeds = [feed for feed in catalog["feeds"] if feed["provider"] not in drain_only]
    policies = {
        provider: policy
        for provider, policy in catalog["daily_runtime_policies"].items()
        if provider not in drain_only
    }
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
                    "feeds": feeds,
                    "daily_runtime_policies": policies,
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
