"""Credential-safe, read-only current account observation; never upgrades a plan."""

from __future__ import annotations

import importlib.metadata
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from findb_fetcher.full_market_universe import NASDAQ_URLS, checksum, parse_nasdaq


def provider_environment(path: Path) -> dict[str, str]:
    allowed = {"TWELVE_DATA_API_KEY", "FINLAB_API_TOKEN", "SHIOAJI_API_KEY", "SHIOAJI_SECRET_KEY"}
    result = {}
    for line in path.read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() in allowed:
            result[key.strip()] = value.strip().strip("\"'")
    return result


def probe_twelve_data(key: str, *, client: httpx.Client | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "provider": "twelve_data",
        "status": "unknown",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "requests_made": 0,
    }
    owns = client is None
    client = client or httpx.Client(timeout=20)
    try:
        if not key:
            return {**result, "reason": "credentials_missing"}
        # Authorization header prevents secrets entering URL/access logs.
        response = client.get(
            "https://api.twelvedata.com/api_usage", headers={"Authorization": f"apikey {key}"}
        )
        result["requests_made"] = 1
        if response.status_code != 200 or len(response.content) > 65536:
            return {**result, "reason": "usage_unavailable", "http_status": response.status_code}
        usage = response.json()
        result["evidence_sha256"] = checksum(response.content)
        result["evidence_url"] = "https://api.twelvedata.com/api_usage"
        for field in ("current_usage", "plan_limit", "daily_usage", "plan_daily_limit"):
            value = usage.get(field)
            if type(value) is int and value >= 0:
                result[field] = value
        minimum = 0
        for index, url in enumerate(NASDAQ_URLS):
            official = client.get(url)
            if official.status_code != 200 or len(official.content) > 16 * 1024 * 1024:
                raise ValueError
            minimum += len(parse_nasdaq(official.content, other=bool(index)))
        result["us_expected_lower_bound"] = minimum
        daily = result.get("plan_daily_limit")
        if daily is not None and daily < minimum:
            return {
                **result,
                "status": "blocked",
                "reason": "daily_quota_insufficient_for_us_alone",
                "hk_permission": "unverified",
            }
        # Two single-symbol EOD permissions probes only if remaining limits prove headroom.
        remaining = result.get("plan_limit", 0) - result.get("current_usage", 0)
        daily_remaining = daily - result.get("daily_usage", daily) if daily is not None else 0
        if remaining < 2 or daily_remaining < 2:
            return {**result, "reason": "sample_budget_or_daily_limit_unknown"}
        samples = {}
        for market, symbol, exchange in (("US", "AAPL", "NASDAQ"), ("HK", "700", "HKEX")):
            sampled = client.get(
                "https://api.twelvedata.com/time_series",
                params={
                    "symbol": symbol,
                    "exchange": exchange,
                    "interval": "1day",
                    "outputsize": 1,
                },
                headers={"Authorization": f"apikey {key}"},
            )
            result["requests_made"] += 1
            if sampled.status_code == 200 and len(sampled.content) <= 65536:
                payload = sampled.json()
                samples[market] = (
                    "permitted"
                    if payload.get("status") == "ok" and payload.get("values")
                    else "unavailable"
                )
            else:
                samples[market] = "unavailable"
        return {
            **result,
            "sample_permissions": samples,
            "reason": "full_symbol_mapping_and_rate_window_not_yet_verified",
        }
    except Exception:
        return {**result, "reason": "readonly_probe_unavailable"}
    finally:
        if owns:
            client.close()


def probe_installed_sdk(provider: str) -> dict[str, Any]:
    pinned = {"finlab": "1.5.7", "shioaji": "1.7.1"}[provider]
    result = {
        "provider": provider,
        "status": "unknown",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "pinned_version": pinned,
    }
    try:
        version = importlib.metadata.version(provider)
    except importlib.metadata.PackageNotFoundError:
        return {**result, "reason": "pinned_sdk_not_installed"}
    return {
        **result,
        "installed_version": version,
        "reason": "account_permissions_and_full_coverage_not_verified"
        if version == pinned
        else "sdk_version_mismatch",
    }


def main() -> int:
    env_file = Path(os.environ.get("FINDB_PROBE_ENV_FILE", "fetcher/.env"))
    values = provider_environment(env_file)
    result = {
        "twelve_data": probe_twelve_data(values.get("TWELVE_DATA_API_KEY", "")),
        "finlab": probe_installed_sdk("finlab"),
        "shioaji": probe_installed_sdk("shioaji"),
    }
    output = Path(
        os.environ.get("FINDB_PROBE_OUTPUT_FILE", "/private/tmp/findb-full-market-readiness.json")
    )
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    output.chmod(0o600)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
