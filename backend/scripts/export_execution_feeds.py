"""Generate process bootstrap settings without any feed scheduling data."""

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    stale = []
    for target in ("local", "staging", "production"):
        path = root / f"fetcher/configs/bootstrap.{target}.v1.json"
        body = (
            json.dumps(
                {
                    "version": 1,
                    "deployment_target": target,
                    "poll_seconds": 30,
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
