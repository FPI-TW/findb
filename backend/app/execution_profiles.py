"""Execution identity shared by acquisition, durable plans and monitoring.

Pilot is retired; ``full_market`` is the only daily profile. The value remains
on the wire and in durable keys so plans, work and controls stay addressable.
"""

from typing import Literal, cast

ExecutionProfile = Literal["full_market"]
EXECUTION_PROFILES: tuple[ExecutionProfile, ...] = ("full_market",)


def execution_profile(value: str) -> ExecutionProfile:
    if value not in EXECUTION_PROFILES:
        raise ValueError("Unsupported execution profile")
    return cast(ExecutionProfile, value)


def scheduler_key(provider: str, profile: ExecutionProfile = "full_market") -> str:
    return f"{profile}_{provider}_v1"


def unified_scheduler(key: str) -> bool:
    return key.startswith("full_market_") and key.endswith("_v1")
