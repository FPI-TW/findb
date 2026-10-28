"""Explicit, resumable plan-summary repair; dry-run unless --apply is provided."""

import argparse
import asyncio
import json

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import engine
from app.models.registry import DailyDeliveryPlan
from app.services.delivery_plan_summary import refresh_plan_summary


async def rebuild(args: argparse.Namespace) -> None:
    filters = []
    if args.dataset:
        filters.append(DailyDeliveryPlan.dataset_key == args.dataset)
    if args.provider:
        filters.append(DailyDeliveryPlan.provider == args.provider)
    if args.profile:
        filters.append(DailyDeliveryPlan.execution_profile == args.profile)
    try:
        async with AsyncSession(engine) as db:
            count = await db.scalar(
                select(func.count()).select_from(DailyDeliveryPlan).where(*filters)
            )
        if not args.apply:
            print(json.dumps({"apply": False, "matching_plans": count}))
            return
        cursor = None
        rebuilt = 0
        while True:
            async with AsyncSession(engine) as db:
                ids = list(
                    await db.scalars(
                        select(DailyDeliveryPlan.plan_id)
                        .where(
                            *filters,
                            *([DailyDeliveryPlan.plan_id > cursor] if cursor else []),
                        )
                        .order_by(DailyDeliveryPlan.plan_id)
                        .limit(args.batch_size)
                    )
                )
                if not ids:
                    break
                for plan_id in ids:
                    await refresh_plan_summary(db, plan_id)
                await db.commit()
                cursor = ids[-1]
                rebuilt += len(ids)
                print(
                    json.dumps({"apply": True, "rebuilt": rebuilt, "through_plan_id": str(cursor)}),
                    flush=True,
                )
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dataset")
    parser.add_argument("--provider")
    parser.add_argument("--profile", choices=("pilot", "full_market"))
    parser.add_argument("--batch-size", type=int, choices=range(1, 101), default=5)
    asyncio.run(rebuild(parser.parse_args()))


if __name__ == "__main__":
    main()
