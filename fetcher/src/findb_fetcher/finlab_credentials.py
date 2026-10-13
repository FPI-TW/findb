"""Pinned FinLab 1.5.7 credential semantics and installed-environment snapshots."""

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager

_SDK_LOCK = threading.Lock()
_SDK_CONTEXT = threading.local()
_TOKEN_NAMES = ("FINLAB_API_TOKEN", "finlab_id_token")


def effective_token(credential: str) -> str:
    # The gateway strips surrounding whitespace before native LoginPanel.login;
    # both native login and get_token then select the first 64 characters.
    return credential.strip()[:64]


def installed_token() -> str:
    # Authorization/finalization callbacks on the dispatch thread may inspect
    # installed identity. Read the saved environment without taking the lock
    # again; other threads wait until selection/use/restoration has completed.
    previous = getattr(_SDK_CONTEXT, "previous", None)
    if previous is not None:
        return previous["FINLAB_API_TOKEN"] or ""
    from findb_fetcher.acquisition import thread_holds_account_lock

    if thread_holds_account_lock():
        # Do not wait account -> SDK while a dispatch waits SDK -> account.
        if not _SDK_LOCK.acquire(blocking=False):
            from findb_fetcher.full_market_state import QuotaBlockedError

            raise QuotaBlockedError("FinLab installed environment snapshot is busy")
        try:
            return os.getenv("FINLAB_API_TOKEN", "")
        finally:
            _SDK_LOCK.release()
    with _SDK_LOCK:
        return os.getenv("FINLAB_API_TOKEN", "")


@contextmanager
def sdk_credential_scope() -> Iterator[None]:
    if getattr(_SDK_CONTEXT, "previous", None) is not None:
        raise RuntimeError("FinLab SDK recursive dataset retrieval is unsupported")
    from findb_fetcher.acquisition import thread_holds_account_lock

    if thread_holds_account_lock():
        raise RuntimeError("FinLab SDK dispatch cannot nest inside an account lock")
    # Lock order is SDK scope -> identity mutex -> sorted account locks.
    with _SDK_LOCK:
        previous = {name: os.environ.get(name) for name in _TOKEN_NAMES}
        _SDK_CONTEXT.previous = previous
        try:
            yield
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            del _SDK_CONTEXT.previous
