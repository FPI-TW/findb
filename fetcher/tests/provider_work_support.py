"""Wire evidence for local fake Source owner authorization responses."""

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
