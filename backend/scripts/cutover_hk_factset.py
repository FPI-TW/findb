"""Validate an explicit HK provider cutover; use --apply to commit the decision."""

import argparse
import asyncio
import json
from datetime import date

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.services.hk_provider_cutover import apply_cutover


async def run(effective_date: date, actor: str, apply: bool) -> None:
    engine = create_async_engine(get_settings().DATABASE_URL)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            decision = await apply_cutover(db, effective_date=effective_date, actor=actor)
            if apply:
                await db.commit()
            else:
                await db.rollback()
            print(json.dumps({"applied": apply, **decision}))
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--effective-date", required=True, type=date.fromisoformat)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.actor.strip():
        parser.error("--actor must identify the operator")
    asyncio.run(run(args.effective_date, args.actor.strip(), args.apply))


if __name__ == "__main__":
    main()
