from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from datetime import date

import pytest

from findb_fetcher import finlab_smoke_cli
from findb_fetcher.providers.finlab import FinLabDatasetTable, FinLabSdkError

TARGET_DATE = date(2026, 7, 29)
TOKEN = "token-that-must-not-appear"


class _Gateway:
    def __init__(self, table: FinLabDatasetTable | Exception) -> None:
        self.table = table
        self.calls: list[tuple[str, date, tuple[str, ...]]] = []

    def fetch_dataset(
        self, dataset: str, *, target_date: date, symbols: tuple[str, ...]
    ) -> FinLabDatasetTable:
        self.calls.append((dataset, target_date, symbols))
        if isinstance(self.table, Exception):
            raise self.table
        return self.table


class _NoisyGateway(_Gateway):
    def fetch_dataset(
        self, dataset: str, *, target_date: date, symbols: tuple[str, ...]
    ) -> FinLabDatasetTable:
        print(TOKEN)
        print(TOKEN, file=sys.stderr)
        return super().fetch_dataset(dataset, target_date=target_date, symbols=symbols)


class _FdNoisyGateway(_Gateway):
    def __init__(self, table: FinLabDatasetTable | Exception, *, fail: bool = False) -> None:
        super().__init__(table)
        self.fail = fail

    def fetch_dataset(
        self, dataset: str, *, target_date: date, symbols: tuple[str, ...]
    ) -> FinLabDatasetTable:
        print(TOKEN)
        print(TOKEN, file=sys.stderr)
        os.write(1, TOKEN.encode("utf-8"))
        os.write(2, TOKEN.encode("utf-8"))
        ctypes.CDLL(None).printf(TOKEN.encode("utf-8"))
        thread = threading.Thread(target=lambda: os.write(1, TOKEN.encode("utf-8")), daemon=True)
        thread.start()
        time.sleep(0.01)
        if self.fail:
            raise FinLabSdkError(TOKEN)
        return super().fetch_dataset(dataset, target_date=target_date, symbols=symbols)


def _table(*, values: tuple[object, ...] = (2200, 237)) -> FinLabDatasetTable:
    return FinLabDatasetTable(dates=("2026-07-29",), symbols=("2330", "2317"), values=(values,))


def _configure_success(monkeypatch: pytest.MonkeyPatch, gateway: _Gateway) -> None:
    monkeypatch.setattr(finlab_smoke_cli.importlib.metadata, "version", lambda _: "1.5.7")
    monkeypatch.setattr(
        finlab_smoke_cli.FinLabSdkGateway,
        "from_env",
        classmethod(lambda cls: gateway),
    )


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b'{"status":"ok","checksum":"not-a-checksum","count":2}',
        b"x" * (finlab_smoke_cli.MAX_IPC_BYTES + 1),
    ],
)
def test_smoke_rejects_malformed_or_oversized_private_ipc(payload: bytes) -> None:
    assert finlab_smoke_cli._decode_worker_result(payload) is None
