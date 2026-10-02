"""Exercise fixture isolation with real PostgreSQL and independent pytest runs."""

import asyncio
import json
import os
import re
import signal
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.registry import DatasetRegistry
from tests import orm_database
from tests.conftest import TEST_DATABASE_URL

BACKEND_ROOT = Path(__file__).resolve().parents[1]

# Loaded outside backend/tests so -p loads the real conftest exactly once. The
# stdin protocol keeps both pytest sessions alive without scheduling sleeps.
CHILD_TEST = """
import asyncio
import json
import sys

from sqlalchemy import text
from app.models.registry import DatasetRegistry


async def test_transaction_rollback(test_session):
    test_session.add(DatasetRegistry(
        dataset_key="rolled_back", name="rollback probe", asset_class="equity", market="TW"
    ))
    await test_session.commit()


async def test_concurrent_session(test_engine, test_session):
    async with test_engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM dataset_registry")) == 0
        assert await connection.scalar(text("SELECT count(*) FROM normalization_job")) == 0
        database_name = await connection.scalar(text("SELECT current_database()"))
    test_session.add(DatasetRegistry(
        dataset_key="committed", name=database_name, asset_class="equity", market="TW"
    ))
    await test_session.commit()
    print("FINDB_EVENT " + json.dumps({"event": "ready", "database": database_name}), flush=True)
    command = (await asyncio.to_thread(sys.stdin.readline)).strip()
    if command == "check":
        async with test_engine.connect() as connection:
            assert await connection.scalar(text(
                "SELECT name FROM dataset_registry WHERE dataset_key = 'committed'"
            )) == database_name
            assert await connection.scalar(text("SELECT count(*) FROM normalization_job")) == 0
        print("FINDB_EVENT " + json.dumps({"event": "survived"}), flush=True)
        command = (await asyncio.to_thread(sys.stdin.readline)).strip()
    assert command == "finish"


async def test_direct_engine_cleanup(test_session):
    assert await test_session.scalar(text("SELECT count(*) FROM dataset_registry")) == 0
"""


async def _database_exists(database_name: str) -> bool:
    engine = create_async_engine(make_url(TEST_DATABASE_URL).set(database="postgres"))
    try:
        async with engine.connect() as connection:
            return bool(
                await connection.scalar(
                    text("SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname = :name)"),
                    {"name": database_name},
                )
            )
    finally:
        await engine.dispose()


@pytest.mark.parametrize("worker", ["gw0", "", 'A/很長";' * 100])
def test_database_identifiers_keep_unique_suffix(monkeypatch, worker):
    monkeypatch.setenv("PYTEST_XDIST_WORKER", worker)
    names = {orm_database._database_name() for _ in range(20)}
    assert len(names) == 20
    for name in names:
        assert len(name.encode("ascii")) <= 63
        assert re.fullmatch(r"findb_orm_[a-z0-9_]{0,8}_[0-9a-f]{32}", name)


@pytest.mark.parametrize("failure_stage", ["schema", "body"])
async def test_owned_database_is_removed_after_failure(monkeypatch, failure_stage):
    name = orm_database._database_name()
    monkeypatch.setattr(orm_database, "_database_name", lambda: name)

    async def fail_schema(engine):
        assert await _database_exists(name)
        # Leave a real connection in the engine's pool before failing.
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE partial_setup (id INTEGER)"))
        raise RuntimeError("schema setup failed")

    if failure_stage == "schema":
        monkeypatch.setattr(orm_database, "_initialize_schema", fail_schema)

    with pytest.raises(RuntimeError, match="failed"):
        async with orm_database.orm_test_database(TEST_DATABASE_URL):
            assert await _database_exists(name)
            raise RuntimeError("test body failed")
    assert not await _database_exists(name)


async def test_failed_create_does_not_drop_existing_database(monkeypatch):
    async with orm_database.orm_test_database(TEST_DATABASE_URL) as existing:
        name = existing.url.database
        monkeypatch.setattr(orm_database, "_database_name", lambda: name)
        with pytest.raises(DBAPIError):
            async with orm_database.orm_test_database(TEST_DATABASE_URL):
                pytest.fail("CREATE must reject the existing database")
        async with existing.connect() as connection:
            assert await connection.scalar(text("SELECT current_database()")) == name
            assert await connection.scalar(text("SELECT count(*) FROM normalization_job")) == 0
    assert not await _database_exists(name)


async def _start_pytest(test_file: Path, base_url: str) -> asyncio.subprocess.Process:
    environment = os.environ.copy()
    environment["TEST_DATABASE_URL"] = base_url
    environment["PYTEST_XDIST_WORKER"] = "gw0"
    environment.pop("PYTEST_ADDOPTS", None)
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-s",
        "-p",
        "tests.conftest",
        "-c",
        str(BACKEND_ROOT / "pyproject.toml"),
        str(test_file),
        cwd=BACKEND_ROOT,
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )


async def _event(process: asyncio.subprocess.Process, expected: str, output: list[str]) -> dict:
    async with asyncio.timeout(45):
        while line := await process.stdout.readline():
            decoded = line.decode()
            output.append(decoded)
            if "FINDB_EVENT " in decoded:
                event = json.loads(decoded.split("FINDB_EVENT ", 1)[1])
                assert event["event"] == expected, "".join(output)
                return event
    pytest.fail(f"Child exited before {expected}: {''.join(output)}")


async def _ready_events(processes, outputs):
    readers = [
        asyncio.create_task(_event(process, "ready", output))
        for process, output in zip(processes, outputs, strict=True)
    ]
    try:
        return await asyncio.gather(*readers)
    finally:
        # gather propagates the first failure without cancelling sibling readers.
        # Drain them before communicate() takes ownership of each stdout pipe.
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)


async def _close_processes(
    processes, *, primary_error=None, eof_timeout=45, terminate_timeout=5, kill_timeout=5
):
    diagnostics = []

    async def close_owned_child(process):
        process.stdin.close()
        reader = asyncio.create_task(process.communicate())
        try:
            for timeout, action in (
                (eof_timeout, "terminate"),
                (terminate_timeout, "kill"),
            ):
                try:
                    # Keep one reader across grace periods. Cancelling it at each
                    # timeout would discard output or race a new communicate().
                    return await asyncio.wait_for(asyncio.shield(reader), timeout)
                except TimeoutError:
                    diagnostics.append(f"owned child {process.pid}: grace expired; {action}")
                    if process.returncode is None:
                        try:
                            getattr(process, action)()
                        except ProcessLookupError:
                            pass
            return await asyncio.wait_for(asyncio.shield(reader), kill_timeout)
        finally:
            try:
                # Even a pipe-read failure must not leave this owned child alive.
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await asyncio.wait_for(process.wait(), kill_timeout)
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)

    # Attempt every owned child independently so a stalled child cannot prevent
    # the others from receiving EOF, draining stdout and being reaped.
    results = await asyncio.gather(
        *(close_owned_child(process) for process in processes), return_exceptions=True
    )
    for process, result in zip(processes, results, strict=True):
        if isinstance(result, BaseException):
            diagnostics.append(
                f"owned child {process.pid}: cleanup failed ({type(result).__name__})"
            )
    if diagnostics:
        message = "Subprocess cleanup: " + "; ".join(diagnostics)
        if primary_error is not None:
            primary_error.add_note(message)
        else:
            raise RuntimeError(message)
    return results


async def _command(process: asyncio.subprocess.Process, command: str) -> None:
    process.stdin.write((command + "\n").encode())
    await process.stdin.drain()


async def _finish(process: asyncio.subprocess.Process, output: list[str]) -> None:
    await _command(process, "finish")
    async with asyncio.timeout(45):
        output.append((await process.stdout.read()).decode())
        assert await process.wait() == 0, "".join(output)
    assert "3 passed" in "".join(output)


async def test_early_child_exit_cancels_reader_before_process_cleanup(monkeypatch):
    processes = []
    reader_started = asyncio.Event()
    original_event = _event
    release = None
    cleanup_output = []

    try:
        for source in (
            "import sys; sys.stdin.readline(); print('early-exit-marker', flush=True); sys.exit(7)",
            "import sys; sys.stdin.readline(); print('eof-cleanup-marker', flush=True)",
        ):
            processes.append(
                await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-u",
                    "-c",
                    source,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            )

        async def observe_event(process, expected, output):
            if process is processes[1]:
                reader_started.set()
            return await original_event(process, expected, output)

        monkeypatch.setattr(sys.modules[__name__], "_event", observe_event)

        async def release_early_child():
            await reader_started.wait()
            await _command(processes[0], "exit")

        release = asyncio.create_task(release_early_child())
        # A cannot exit until B's reader is waiting. Before the fix, cleanup of B
        # raised a competing-read RuntimeError and replaced A's original failure.
        with pytest.raises(
            pytest.fail.Exception, match="Child exited before ready: early-exit-marker"
        ):
            try:
                await _ready_events(processes, [[], []])
            finally:
                cleanup_output = await _close_processes(processes, primary_error=sys.exception())
        assert [process.returncode for process in processes] == [7, 0]
        assert cleanup_output[1][0] == b"eof-cleanup-marker\n"
    finally:
        if release is not None:
            release.cancel()
            await asyncio.gather(release, return_exceptions=True)
        if not cleanup_output:
            await _close_processes(processes, primary_error=sys.exception())


@pytest.mark.parametrize("ignore_terminate", [False, True])
@pytest.mark.parametrize("has_primary_error", [False, True])
async def test_stalled_owned_child_is_reaped_without_masking_failure(
    ignore_terminate, has_primary_error
):
    processes = []
    primary_error = RuntimeError("primary-probe-failure")
    try:
        signal_setup = "signal.signal(signal.SIGTERM, signal.SIG_IGN);" if ignore_terminate else ""
        for source in (
            "import sys, signal, threading;"
            + signal_setup
            + "print('ready', flush=True); sys.stdin.read(); threading.Event().wait()",
            "import sys; print('ready', flush=True); sys.stdin.read(); print('EOF-drained', flush=True)",
        ):
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-u",
                "-c",
                source,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            processes.append(process)
            async with asyncio.timeout(5):
                assert await process.stdout.readline() == b"ready\n"

        async def cleanup():
            return await _close_processes(
                processes,
                primary_error=sys.exception(),
                eof_timeout=0.1,
                terminate_timeout=0.1,
                kill_timeout=5,
            )

        if has_primary_error:
            with pytest.raises(RuntimeError, match="primary-probe-failure") as failure:
                try:
                    raise primary_error
                finally:
                    output = await cleanup()
            assert failure.value is primary_error
            assert any("grace expired; terminate" in note for note in primary_error.__notes__)
            assert output[1][0] == b"EOF-drained\n"
        else:
            with pytest.raises(RuntimeError, match="Subprocess cleanup:.*grace expired; terminate"):
                await cleanup()

        expected_signal = signal.SIGKILL if ignore_terminate else signal.SIGTERM
        assert [process.returncode for process in processes] == [-expected_signal, 0]
    finally:
        await _close_processes(processes, primary_error=sys.exception(), eof_timeout=0.1)


async def test_two_pytest_sessions_preserve_base_and_each_other(tmp_path):
    test_file = tmp_path / "test_session_probe.py"
    test_file.write_text(CHILD_TEST)
    processes = []
    names = []
    # This disposable database is the configured base of both subprocesses. The
    # actual shared findb_test database never receives regression setup or DDL.
    async with orm_database.orm_test_database(TEST_DATABASE_URL) as base:
        base_name = base.url.database
        maker = async_sessionmaker(base)
        async with maker() as session:
            session.add(
                DatasetRegistry(
                    dataset_key="base_sentinel",
                    name="must survive",
                    asset_class="equity",
                    market="TW",
                )
            )
            await session.commit()
        async with base.begin() as connection:
            await connection.execute(
                text("ALTER TABLE normalization_job ADD COLUMN preserved_marker TEXT")
            )

        try:
            # The former shared-database drop_all would block against this lock.
            # Hold it while both real fixtures initialize and complete their tests.
            async with base.begin() as lock_connection:
                await lock_connection.execute(
                    text("LOCK TABLE normalization_job IN ACCESS SHARE MODE")
                )
                # Demonstrate the old shared-DDL boundary safely: PostgreSQL
                # rejects DROP on this disposable base while its reader holds
                # the lock. A server timeout bounds the probe and rolls it back.
                with pytest.raises(DBAPIError) as blocked_drop:
                    async with base.begin() as ddl_connection:
                        await ddl_connection.execute(text("SET LOCAL lock_timeout = '100ms'"))
                        await ddl_connection.execute(text("DROP TABLE normalization_job"))
                assert blocked_drop.value.orig.sqlstate == "55P03"
                first = await _start_pytest(
                    test_file, base.url.render_as_string(hide_password=False)
                )
                processes.append(first)
                second = await _start_pytest(
                    test_file, base.url.render_as_string(hide_password=False)
                )
                processes.append(second)
                outputs = [[], []]
                first_ready, second_ready = await _ready_events(processes, outputs)
                names = [first_ready["database"], second_ready["database"]]
                assert len({base_name, *names}) == 3
                assert all([await _database_exists(name) for name in names])

                await _finish(first, outputs[0])
                assert not await _database_exists(names[0])
                assert await _database_exists(names[1])
                await _command(second, "check")
                await _event(second, "survived", outputs[1])
                await _finish(second, outputs[1])
                assert not await _database_exists(names[1])

                assert (
                    await lock_connection.scalar(
                        text(
                            "SELECT name FROM dataset_registry WHERE dataset_key = 'base_sentinel'"
                        )
                    )
                    == "must survive"
                )
                assert (
                    await lock_connection.scalar(text("SELECT count(*) FROM normalization_job"))
                    == 0
                )
                assert (
                    await lock_connection.scalar(
                        text(
                            "SELECT count(*) FROM information_schema.columns "
                            "WHERE table_name = 'normalization_job' AND column_name = 'preserved_marker'"
                        )
                    )
                    == 1
                )
        finally:
            await _close_processes(processes, primary_error=sys.exception())
    assert not await _database_exists(base_name)
    assert all([not await _database_exists(name) for name in names])
