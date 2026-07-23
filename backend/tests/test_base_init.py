"""Tests for database initialization guards in app.models.base."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from app.models import base


class _FakeConnection:
    def __init__(self, run_sync_result: tuple[str, ...] = ()) -> None:
        self._run_sync_result = run_sync_result

    async def run_sync(self, _func):
        return self._run_sync_result


class _FakeConnectContext:
    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _FakeConnection:
        return self._connection

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _ConnectSequence:
    def __init__(self, connections: list[_FakeConnection]) -> None:
        self._connections: Iterator[_FakeConnection] = iter(connections)

    def __call__(self) -> _FakeConnectContext:
        return _FakeConnectContext(next(self._connections))


class _FakeEngine:
    def __init__(self, connections: list[_FakeConnection]) -> None:
        self._connect = _ConnectSequence(connections)

    def connect(self) -> _FakeConnectContext:
        return self._connect()


@pytest.mark.asyncio
async def test_init_db_warns_and_raises_when_no_alembic_stamp(monkeypatch, caplog):
    monkeypatch.setattr(base, "_expected_alembic_heads", lambda: ("head_rev",))
    monkeypatch.setattr(base, "engine", _FakeEngine([_FakeConnection(())]))

    async def _verify_should_not_run(_conn) -> None:
        raise AssertionError(
            "_verify_required_objects should not run when no revision stamp exists"
        )

    monkeypatch.setattr(base, "_verify_required_objects", _verify_should_not_run)

    caplog.set_level(logging.WARNING, logger=base.logger.name)
    with pytest.raises(RuntimeError, match="Database is not initialized"):
        await base.init_db()

    warning = caplog.text
    assert "no Alembic revision stamp" in warning
    assert base.ALEMBIC_UPGRADE_COMMAND in warning
    assert base.DB_INIT_COMMAND in warning


@pytest.mark.asyncio
async def test_init_db_warns_and_raises_when_revision_mismatch(monkeypatch, caplog):
    monkeypatch.setattr(base, "_expected_alembic_heads", lambda: ("new_head",))
    monkeypatch.setattr(
        base,
        "engine",
        _FakeEngine([_FakeConnection(("old_head",)), _FakeConnection()]),
    )

    verify_called = False

    async def _verify_required(_conn) -> None:
        nonlocal verify_called
        verify_called = True

    monkeypatch.setattr(base, "_verify_required_objects", _verify_required)

    caplog.set_level(logging.WARNING, logger=base.logger.name)
    with pytest.raises(RuntimeError, match="Database schema version mismatch"):
        await base.init_db()

    assert verify_called is True
    warning = caplog.text
    assert "current=old_head expected=new_head" in warning
    assert base.ALEMBIC_UPGRADE_COMMAND in warning


@pytest.mark.asyncio
async def test_init_db_passes_when_revision_matches_and_required_objects_exist(monkeypatch):
    monkeypatch.setattr(base, "_expected_alembic_heads", lambda: ("head_rev",))
    monkeypatch.setattr(
        base,
        "engine",
        _FakeEngine([_FakeConnection(("head_rev",)), _FakeConnection()]),
    )

    verify_called = False

    async def _verify_required(_conn) -> None:
        nonlocal verify_called
        verify_called = True

    monkeypatch.setattr(base, "_verify_required_objects", _verify_required)

    await base.init_db()
    assert verify_called is True


@pytest.mark.asyncio
async def test_verify_required_objects_warns_on_missing_relation(monkeypatch, caplog):
    class _ScalarConn:
        def __init__(self) -> None:
            self._calls = 0

        async def scalar(self, _stmt, _params):
            self._calls += 1
            return self._calls == 1

    conn = _ScalarConn()
    caplog.set_level(logging.WARNING, logger=base.logger.name)

    with pytest.raises(RuntimeError, match="Missing required relation 'public.instruments'"):
        await base._verify_required_objects(conn)

    warning = caplog.text
    assert "Database schema is incomplete" in warning
    assert base.ALEMBIC_UPGRADE_COMMAND in warning
    assert base.DB_INIT_COMMAND in warning


def test_expected_alembic_heads_returns_non_empty_tuple():
    heads = base._expected_alembic_heads()
    assert isinstance(heads, tuple)
    assert heads
