"""Preview or publish existing safe exact-date TAIFEX rollover candidates."""

from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.services.full_market import reconcile_taifex_rollover_candidates


async def _run(*, apply: bool, deployment_target: str) -> int:
    settings = get_settings()
    if deployment_target != settings.APP_ENVIRONMENT:
        raise ValueError("deployment target differs from APP_ENVIRONMENT")
    engine = create_async_engine(settings.DATABASE_URL, pool_size=1, max_overflow=0)
    try:
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            results = await reconcile_taifex_rollover_candidates(session, apply=apply)
        print(
            json.dumps(
                {
                    "mode": "apply" if apply else "preview",
                    "deployment_target": deployment_target,
                    "results": results,
                },
                sort_keys=True,
            )
        )
        return 0 if all(row["action"] != "manual_review" for row in results) else 3
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--deployment-target",
        required=True,
        choices=("local", "staging", "production"),
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        return asyncio.run(_run(apply=args.apply, deployment_target=args.deployment_target))
    except (OSError, RuntimeError, SQLAlchemyError, ValueError):
        print('{"error":"taifex_rollover_reconciliation_failed"}')
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
