"""Queue ownership setup for historical domain policy tests."""

from app.services import historical_backfill as history
from app.services import provider_work as queue


async def claim_next_item(db, *, provider, allowed_datasets=None, now=None):
    now = now or history.utc_now()
    await history.expire_requests(db, provider=provider, now=now)
    owner = await queue.lock_provider(db, provider)
    inode = f"{queue.environment()}:1:22"
    owner.host_lock_identity = inode
    claimed = await queue.claim(
        db,
        provider=provider,
        datasets=allowed_datasets or [],
        host_lock_identity=inode,
        require_cutover=True,
        now=now,
    )
    if claimed is None:
        return None
    work, _, owner = claimed
    db.info["provider_work"] = dict(
        work_id=work.work_id,
        generation=owner.generation,
        token=owner.token,
        member_key="historical",
    )
    from unittest.mock import patch

    with patch.object(queue, "utc_now", lambda: now):
        return await history.claim_next_item(
            db, provider=provider, allowed_datasets=allowed_datasets, now=now
        )


class OwnedHistoricalClient:
    def __init__(self, client, db):
        self.client, self.db = client, db

    def __getattr__(self, name):
        return getattr(self.client, name)

    async def _headers(self, headers, *, claim=False):
        from app.services.source_clients import find_active_source_client

        key = headers.get("X-API-Key")
        if key is None:
            return headers
        principal = await find_active_source_client(self.db, key)
        if principal is None:
            return headers
        if claim:
            await claim_next_item(
                self.db, provider=principal.source_name, allowed_datasets=principal.allowed_datasets
            )
            await self.db.commit()
        owner = await queue.lock_provider(self.db, principal.source_name)
        if owner.attempt_id is None:
            return headers
        from app.models.provider_work import ProviderWorkAttempt

        attempt = await self.db.get(ProviderWorkAttempt, owner.attempt_id)
        return {
            **headers,
            "X-FinDB-Work-Id": str(attempt.work_id),
            "X-FinDB-Work-Generation": str(owner.generation),
            "X-FinDB-Work-Token": str(owner.token),
            "X-FinDB-Work-Member": "historical",
        }

    async def post(self, url, **kwargs):
        if "/source/historical-backfills" in str(url):
            kwargs["headers"] = await self._headers(
                kwargs.get("headers", {}), claim=str(url).endswith("/claim")
            )
        return await self.client.post(url, **kwargs)

    async def get(self, url, **kwargs):
        if "/source/historical-backfills" in str(url):
            kwargs["headers"] = await self._headers(kwargs.get("headers", {}))
        return await self.client.get(url, **kwargs)
