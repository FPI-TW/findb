"""Wire evidence for local fake Source owner authorization responses."""

from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone

from findb_fetcher import acquisition as acq


def work_authorization(provider, fence, *, work_id="original"):
    now = acq.time.time()
    return {
        "work_id": work_id,
        "provider": provider,
        "generation": fence["generation"],
        "token": fence["token"],
        "checked_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "expires_at": datetime.fromtimestamp(now + 120, timezone.utc).isoformat(),
    }


@contextmanager
def owned_unit_work(provider, root, *, lock=True):
    """A real local owner/session with an isolated fake Source control plane."""
    from findb_fetcher import provider_work as queue

    class Source:
        def request(self, method, route, body):
            if route.endswith("/authorize"):
                return work_authorization(provider, body, work_id="owned-unit-work")
            return {"bodies": [], "member_keys": []}

    journal = queue.WorkJournal(root / "unit-work.sqlite3", "owned-unit-work")
    owner = queue.ProviderWorkSession(
        Source(),
        provider,
        {
            "work_id": "owned-unit-work",
            "provider": provider,
            "generation": 1,
            "token": "fence",
            "kind": "daily",
            "state": "open",
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
        journal,
    )
    token = queue._current.set(owner)
    try:
        with queue.host_lock(provider, root=root) if lock else nullcontext():
            yield owner
    finally:
        queue._current.reset(token)
        owner.stop()


def account_boundary_unit(function):
    """Keep spawned account-ledger harnesses isolated from the queue layer."""
    from functools import wraps

    @wraps(function)
    def run(*args, **kwargs):
        import os
        import threading
        from pathlib import Path
        from unittest.mock import patch

        from findb_fetcher import provider_work

        root = Path(
            os.environ.get("FETCHER_ACCOUNT_STATE_PATH", "/private/tmp/findb-unit-governor.sqlite3")
        ).parent
        with (
            provider_work.host_lock(f"unit-{os.getpid()}-{threading.get_ident()}", root=root),
            patch.object(provider_work, "require_acquisition", lambda provider, **kw: True),
            patch.object(
                provider_work, "acquisition_dispatch_deadline", lambda provider, deadline: deadline
            ),
        ):
            return function(*args, **kwargs)

    return run
