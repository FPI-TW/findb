"""Published provider limits, independent of deployment enrollment evidence."""

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from findb_fetcher.full_market_state import FullMarketState

SHIOAJI_DAILY_BYTES = 500 * 1024 * 1024


def limits(provider: str, datasets: list[str] | None = None) -> dict[str, Any]:
    if provider not in {"twelve_data", "finlab", "shioaji", "taifex"}:
        raise ValueError("unsupported provider")
    return {
        "provider": provider,
        "datasets": datasets or [],
        "requests_per_day": None,
        "requests_per_minute": 1597 if provider == "twelve_data" else None,
        "requests_per_second": 1597 / 60
        if provider == "twelve_data"
        else (5 if provider == "shioaji" else None),
        # FinLab enforces its 5000 MB daily quota in the authenticated SDK API.
        # Detached dataframe size is not a network usage measurement.
        "bytes_per_day": SHIOAJI_DAILY_BYTES if provider == "shioaji" else None,
        "max_response_bytes": 2 * 1024**3 if provider == "finlab" else 16 * 1024**2,
        "platform_limits": True,
    }


def interval(value: dict[str, Any]) -> float:
    rate = value.get("requests_per_second")
    return 1 / rate if rate else 0


def account_identity(provider: str, state: FullMarketState) -> str:
    name = {
        "twelve_data": "TWELVE_DATA_API_KEY",
        "finlab": "FINLAB_API_TOKEN",
        "shioaji": "SHIOAJI_API_KEY",
        "taifex": "",
    }[provider]
    credential = os.getenv(name, "") if name else "public"
    legacy_digest = hashlib.sha256(credential.encode()).hexdigest()
    # These providers strip surrounding whitespace before authenticating.
    # Shioaji forwards its API key unchanged.
    if provider in {"twelve_data", "finlab"}:
        credential = credential.strip()
    digest = hashlib.sha256(credential.encode()).hexdigest()
    initial = provider + ":" + digest
    # Keep an existing ledger identity without treating its evidence as permission.
    readiness = Path(
        os.getenv(
            "FETCHER_ACCOUNT_READINESS_FILE", f"/var/lib/findb-account/readiness/{provider}.json"
        )
    )
    path = Path(
        os.getenv("FETCHER_ACCOUNT_ALLOCATION_FILE", str(readiness.with_suffix(".account.json")))
    )
    try:
        if path.stat().st_size <= 65536:
            value = json.loads(path.read_bytes())
            account = value.get("account", {}).get("account_id")
            if value.get("provider") == provider and isinstance(account, str) and account:
                initial = account
    except (OSError, ValueError, AttributeError):
        pass
    return state.account_identity(provider, digest, initial, legacy_credential_sha256=legacy_digest)
