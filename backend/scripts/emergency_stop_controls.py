"""Stop every active scheduler control directly in the database.

Replaces the retired FULL_MARKET_ENABLED kill switch. Dry-run unless --apply is
provided. Run it after any RDS restore, before starting ingest or Fetcher.
Observed state changes only when each runtime acknowledges the stop.
"""

import argparse
import asyncio
import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import engine
from app.services.scheduler_control import emergency_stop_controls


async def run(args: argparse.Namespace) -> list[dict]:
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            return await emergency_stop_controls(
                db, actor=args.actor, reason=args.reason, apply=args.apply
            )
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--actor", required=True, help="operator identity for the audit event")
    parser.add_argument("--reason", required=True, help="why every control is being stopped")
    args = parser.parse_args()
    stopped = asyncio.run(run(args))
    print(json.dumps({"apply": args.apply, "controls": stopped}, ensure_ascii=False))


if __name__ == "__main__":
    main()
