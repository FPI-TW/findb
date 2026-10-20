"""Published provider limits, independent of deployment enrollment evidence."""

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from findb_fetcher.full_market_state import FullMarketState

SHIOAJI_DAILY_BYTES = 500 * 1024 * 1024


def limits(provider: str, datasets: list[str] | None = None) -> dict[str, Any]:
    if provider not in {"twelve_data", "finlab", "shioaji", "taifex", "factset"}:
        raise ValueError("unsupported provider")
    return {
        "provider": provider,
        "datasets": datasets or [],
        "requests_per_day": None,
        "requests_per_minute": 1597 if provider == "twelve_data" else None,
        "requests_per_second": 1597 / 60
        if provider == "twelve_data"
        else (5 if provider == "shioaji" else None),
        # FinLab enforces its 5000 MB daily quota in the authenticated SDK API.
        # Detached dataframe size is not a network usage measurement.
        "bytes_per_day": SHIOAJI_DAILY_BYTES if provider == "shioaji" else None,
        "max_response_bytes": 2 * 1024**3 if provider == "finlab" else 16 * 1024**2,
        "platform_limits": True,
    }


def interval(value: dict[str, Any]) -> float:
    rate = value.get("requests_per_second")
    return 1 / rate if rate else 0


def account_identity(
    provider: str, state: FullMarketState, *, credential: str | None = None
) -> str:
    name = {
        "twelve_data": "TWELVE_DATA_API_KEY",
        "finlab": "FINLAB_API_TOKEN",
        "shioaji": "SHIOAJI_API_KEY",
        "taifex": "",
        "factset": "",
    }[provider]
    from findb_fetcher.finlab_credentials import effective_token, installed_token

    environment = (
        installed_token() if provider == "finlab" else (os.getenv(name, "") if name else "public")
    )
    if provider == "factset":
        # Credential identity excludes the password, so rotation preserves debt.
        environment = f"{os.getenv('FACTSET_HOST', '').lower()}:{os.getenv('FACTSET_PORT', '3306')}:{os.getenv('FACTSET_USER', '')}"
    actual: str = credential if credential is not None else environment
    legacy_digest = hashlib.sha256(actual.encode()).hexdigest()
    # The supported SDK boundaries normalize surrounding credential whitespace.
    if provider in {"twelve_data", "finlab", "shioaji"}:
        actual = actual.strip()
    known_credentials = {actual}
    if provider == "finlab":
        actual = effective_token(actual)
        # Only explicitly known aliases may be merged. Installed allocation
        # evidence belongs to the same account only for an SDK-equivalent key.
        if actual == effective_token(environment):
            known_credentials.update((environment, environment.strip()))
    digest = hashlib.sha256(actual.encode()).hexdigest()
    initial = provider + ":" + digest
    # Keep an existing ledger identity without treating its evidence as permission.
    readiness = Path(
        os.getenv(
            "FETCHER_ACCOUNT_READINESS_FILE", f"/var/lib/findb-account/readiness/{provider}.json"
        )
    )
    path = Path(
        os.getenv("FETCHER_ACCOUNT_ALLOCATION_FILE", str(readiness.with_suffix(".account.json")))
    )
    try:
        # An environment-installed allocation cannot identify a different explicit key.
        matches_environment = credential is None or actual == (
            effective_token(environment) if provider == "finlab" else environment.strip()
        )
        if matches_environment and path.stat().st_size <= 65536:
            value = json.loads(path.read_bytes())
            account = value.get("account", {}).get("account_id")
            if value.get("provider") == provider and isinstance(account, str) and account:
                initial = account
    except (OSError, ValueError, AttributeError):
        pass
    return state.account_identity(
        provider,
        digest,
        initial,
        legacy_credential_sha256=legacy_digest,
        known_credential_sha256=tuple(
            hashlib.sha256(key.encode()).hexdigest() for key in known_credentials
        ),
    )


def merge_identity(
    state: FullMarketState,
    provider: str,
    digest: str,
    legacy_digest: str,
    initial: str,
    known_digests: tuple[str, ...] = (),
) -> str:
    from findb_fetcher.acquisition import account_lock, thread_holds_account_lock

    digests = sorted({digest, legacy_digest, *known_digests})
    placeholders = ",".join("?" for _ in digests)
    query = f"SELECT account FROM full_account_identity WHERE provider=? AND credential_sha256 IN ({placeholders}) ORDER BY credential_sha256=? DESC,credential_sha256"
    parameters = (provider, *digests, digest)
    if thread_holds_account_lock(state.path):
        # Never take the identity mutex after an acquisition lock. Another
        # process may already hold that mutex while waiting to merge our debt.
        with state.connection() as db:
            rows = db.execute(query, parameters).fetchall()
        if len(rows) == len(digests) and len({str(row[0]) for row in rows}) == 1:
            return str(rows[0][0])
        from findb_fetcher.full_market_state import QuotaBlockedError

        raise QuotaBlockedError("account identity cannot be updated during active acquisition")

    with account_lock(state.path, "identity:" + provider + ":" + digest):
        with state.connection() as db:
            rows = db.execute(query, parameters).fetchall()
        target = str(rows[0][0]) if rows else initial
        # A current SDK callback may read the identity of the account whose
        # acquisition lock it already owns. Fully pinned reads need no second
        # flock descriptor (which would deadlock even with the thread RLock).
        if len(rows) == len(digests) and {str(row[0]) for row in rows} == {target}:
            return target
        from contextlib import ExitStack

        with ExitStack() as stack:
            for account in sorted({target, *(str(row[0]) for row in rows)}):
                stack.enter_context(account_lock(state.path, account))
            with state.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                for alias in {str(row[0]) for row in rows} - {target}:
                    quota = [
                        dict(row)
                        for row in db.execute(
                            "SELECT * FROM full_quota WHERE account=? OR substr(account,1,length(?))=?",
                            (alias, alias + ":allocation:", alias + ":allocation:"),
                        )
                    ]
                    pacing = db.execute(
                        "SELECT next_at FROM full_pacing WHERE account=?", (alias,)
                    ).fetchone()
                    inserted = db.execute(
                        "INSERT OR IGNORE INTO account_alias_audit VALUES(?,?,?,?)",
                        (alias, target, json.dumps(quota), pacing[0] if pacing else 0),
                    ).rowcount
                    if inserted:
                        for row in quota:
                            new_account = target + row["account"][len(alias) :]
                            db.execute(
                                "INSERT INTO full_quota(account,window,requests,bytes,blocked_until) VALUES(?,?,?,?,?) ON CONFLICT(account,window) DO UPDATE SET requests=requests+excluded.requests,bytes=bytes+excluded.bytes,blocked_until=MAX(blocked_until,excluded.blocked_until)",
                                (
                                    new_account,
                                    row["window"],
                                    row["requests"],
                                    row["bytes"],
                                    row["blocked_until"],
                                ),
                            )
                        if pacing:
                            db.execute(
                                "INSERT INTO full_pacing VALUES(?,?) ON CONFLICT(account) DO UPDATE SET next_at=MAX(next_at,excluded.next_at)",
                                (target, pacing[0]),
                            )
                        cooldown = db.execute(
                            "SELECT not_before,reason FROM full_account_cooldown WHERE account=?",
                            (alias,),
                        ).fetchone()
                        if cooldown:
                            state.record_floor(db, target, cooldown[0], reason=cooldown[1])
                        db.execute(
                            "UPDATE acquisition_intent SET account=? WHERE account=?",
                            (target, alias),
                        )
                    db.execute(
                        f"UPDATE full_account_identity SET account=? WHERE provider=? AND account=? AND credential_sha256 IN ({placeholders})",
                        (target, provider, alias, *digests),
                    )
                for known_digest in digests:
                    db.execute(
                        "INSERT INTO full_account_identity VALUES(?,?,?) ON CONFLICT(provider,credential_sha256) DO UPDATE SET account=excluded.account",
                        (provider, known_digest, target),
                    )
        return target
