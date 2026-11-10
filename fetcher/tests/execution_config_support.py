"""Source publication fixture; clocks are test inputs, never runtime defaults."""

import hashlib
import json
import os
from pathlib import Path


def signed(value):
    value.pop("schedule_digest", None)
    value["schedule_digest"] = hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return value


def publication(provider="twelve_data", *, profile="full_market", environment=None, **changes):
    body = json.loads((Path(__file__).parent / "fixtures/execution_config.v1.json").read_text())[
        provider
    ]
    body["environment"] = environment or os.getenv("APP_ENVIRONMENT", "local")
    body["execution_profile"] = profile
    for feed in body["feeds"]:
        feed.update(changes)
    return signed(body)
