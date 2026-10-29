"""Non-acquiring tombstones for retired standalone provider commands."""

import json
import sys


def retired(command: str) -> int:
    print(
        json.dumps(
            {
                "status": "retired",
                "code": "PROVIDER_WORK_REQUIRED",
                "command": command,
                "runtime": "findb-fetch-runtime",
                "historical": "findb-fetch-historical-backfill",
            }
        ),
        file=sys.stderr,
    )
    return 2
