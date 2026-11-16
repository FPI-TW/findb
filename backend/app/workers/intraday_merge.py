"""Periodic month merge of intraday day objects, run by the Celery worker parent."""

import asyncio
import logging
from threading import Event, Thread

from celery.signals import worker_ready, worker_shutdown

from app.config import get_settings
from app.services.intraday_merge import merge_finished_months

logger = logging.getLogger(__name__)
settings = get_settings()
_stop = Event()
_thread: Thread | None = None


def _merge_loop(interval: int) -> None:
    while not _stop.wait(interval):
        try:
            merged = asyncio.run(merge_finished_months())
        except Exception:
            logger.exception("Intraday month merge pass failed")
            continue
        if merged:
            logger.info("Merged %s intraday symbol-months", merged)


@worker_ready.connect
def start_intraday_merge(**_: object) -> None:
    global _thread
    interval = settings.INTRADAY_MERGE_INTERVAL_SECONDS
    if interval <= 0 or (_thread is not None and _thread.is_alive()):
        return
    _stop.clear()
    _thread = Thread(target=_merge_loop, args=(interval,), name="findb-intraday-merge", daemon=True)
    _thread.start()


@worker_shutdown.connect
def stop_intraday_merge(**_: object) -> None:
    _stop.set()
