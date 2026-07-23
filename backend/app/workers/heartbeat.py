"""Periodic DB heartbeat for an idle or busy Celery worker parent process."""

import asyncio
import logging
import os
import socket
from threading import Event, Thread

from celery.signals import worker_ready, worker_shutdown
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.services.normalization_queue import _heartbeat

logger = logging.getLogger(__name__)
settings = get_settings()
_stop = Event()
_thread: Thread | None = None


async def _write_heartbeat(worker_id: str) -> None:
    engine = create_async_engine(settings.DATABASE_URL, pool_size=1, max_overflow=0)
    try:
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            await _heartbeat(session, worker_id, None)
    finally:
        await engine.dispose()


def _heartbeat_loop(worker_id: str) -> None:
    while not _stop.is_set():
        try:
            asyncio.run(_write_heartbeat(worker_id))
        except Exception:
            logger.exception("Failed to persist Celery worker heartbeat")
        _stop.wait(30)


@worker_ready.connect
def start_worker_heartbeat(**_: object) -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    worker_id = f"{socket.gethostname()}:{os.getpid()}:parent"
    _thread = Thread(
        target=_heartbeat_loop,
        args=(worker_id,),
        name="findb-worker-heartbeat",
        daemon=True,
    )
    _thread.start()


@worker_shutdown.connect
def stop_worker_heartbeat(**_: object) -> None:
    _stop.set()
