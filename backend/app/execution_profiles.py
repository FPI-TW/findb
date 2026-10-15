"""Execution identity shared by acquisition, durable plans and monitoring."""

from typing import Literal, cast

ExecutionProfile = Literal["pilot", "full_market"]
EXECUTION_PROFILES: tuple[ExecutionProfile, ...] = ("pilot", "full_market")


def execution_profile(value: str) -> ExecutionProfile:
    if value not in EXECUTION_PROFILES:
        raise ValueError("Unsupported execution profile")
    return cast(ExecutionProfile, value)


def scheduler_key(provider: str, profile: ExecutionProfile) -> str:
    return f"{profile}_{provider}_v1"


def scheduler_profile(key: str) -> ExecutionProfile:
    return "full_market" if key.startswith("full_market_") else "pilot"


def unified_scheduler(key: str) -> bool:
    return key.startswith(("full_market_", "pilot_")) and key.endswith("_v1")
