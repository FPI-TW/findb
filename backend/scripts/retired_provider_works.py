"""List a retired provider's closed work IDs for the Fetcher local-data purge.

Read-only. Exits non-zero, naming the reasons, while the provider is still in the
catalog, has a control that is not retired, or has work or attempts that are not
settled. Pass the printed JSON to ``findb-fetch-retired-purge --works``.
"""

import argparse
import asyncio
import json
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import engine
from app.services.retired_provider import RetiredProviderError, retired_provider_works


async def run(provider: str) -> dict:
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            await db.execute(text("SET TRANSACTION READ ONLY"))
            return await retired_provider_works(db, provider)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args.provider))
    except RetiredProviderError as exc:
        print(json.dumps({"provider": args.provider, "refused": exc.reasons}), file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
