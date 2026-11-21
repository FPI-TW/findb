"""A FinLab table is taken for a trade date only once FinLab published its final copy."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from findb_fetcher.provider_adapters.base import FailureKind
from findb_fetcher.provider_adapters.finlab import FinLabAdapter
from findb_fetcher.providers.finlab import (
    FinLabDatasetTable,
    FinLabSdkGateway,
    FinLabTargetNotReadyError,
    finlab_final,
)

TAIPEI = ZoneInfo("Asia/Taipei")
DAY = date(2026, 11, 20)
# A day the test clock never reaches, so its fallback time never passes.
LATER = date(2099, 1, 2)


def _at(day: date, clock: str) -> datetime:
    return datetime.combine(day, datetime.strptime(clock, "%H:%M").time(), TAIPEI)


@pytest.mark.parametrize(
    ("published", "now", "final"),
    [
        # The 16:32 run's copy, still without the day's block trades.
        (_at(DAY, "16:49"), _at(DAY, "18:04"), None),
        # The 18:32 run published at 18:48: final.
        (_at(DAY, "18:48"), _at(DAY, "19:00"), "published"),
        (_at(DAY, "18:32"), _at(DAY, "18:40"), "published"),
        # A later day's copy holds the final day too (catch-up, historical).
        (_at(date(2026, 11, 23), "16:49"), _at(date(2026, 11, 23), "17:00"), "published"),
        # Neither evening run published by 22:15: the table is taken as it is.
        (_at(DAY, "16:49"), _at(DAY, "22:14"), None),
        (_at(DAY, "16:49"), _at(DAY, "22:15"), "fallback"),
        # An unknown revision is never final, until the fallback time.
        (None, _at(DAY, "21:00"), None),
        (None, _at(DAY, "22:30"), "fallback"),
    ],
)
def test_only_the_runs_from_1832_publish_a_final_table(published, now, final) -> None:
    assert finlab_final(DAY, published, now.astimezone(timezone.utc)) == final


class _Storage:
    def __init__(self, published: datetime | None) -> None:
        self._cache: dict = {}
        self.use_cache = True
        self.published = published
        self.read: list[str] = []

    def get_time_created(self, name: str) -> datetime | None:
        self.read.append(name)
        return self.published


class _Loc:
    def __getitem__(self, key):
        return {"2330": 1000, "2317": 200}[key[1]]


def _gateway(day: date, published: datetime | None) -> tuple[FinLabSdkGateway, _Storage]:
    storage = _Storage(published)
    frame = SimpleNamespace(index=(day,), columns=("2330", "2317"), loc=_Loc())
    # The SDK's disk storage, as the gateway resolves it after the get.
    data = SimpleNamespace(get=lambda dataset: frame, FileStorage=_Storage, _storage=storage)
    sdk = SimpleNamespace(login=lambda token: None, data=data)
    return FinLabSdkGateway(api_token="test-token", _sdk=sdk), storage


def _logged(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]


def test_a_table_published_before_the_final_run_is_not_ready_yet(capsys) -> None:
    gateway, storage = _gateway(LATER, _at(LATER, "16:49"))

    with pytest.raises(FinLabTargetNotReadyError):
        gateway.fetch_dataset("price:開盤價", target_date=LATER, symbols=("2330", "2317"))

    assert storage.read == ["price:開盤價"]
    assert _logged(capsys) == [
        {
            "finlab_target_not_final": {
                "dataset": "price:開盤價",
                "target_date": "2099-01-02",
                "published_at": "2099-01-02T16:49:00+08:00",
            }
        }
    ]
    # Nothing describes the table's previous day: it was never taken.
    assert gateway.prior_rows == {}


def test_a_table_published_by_the_final_run_is_taken(capsys) -> None:
    gateway, _ = _gateway(LATER, _at(LATER, "18:48"))

    table = gateway.fetch_dataset("price:開盤價", target_date=LATER, symbols=("2330", "2317"))

    assert table == FinLabDatasetTable(("2099-01-02",), ("2330", "2317"), ((1000, 200),))
    assert _logged(capsys) == []


def test_past_the_fallback_time_the_table_is_taken_and_logged(capsys) -> None:
    gateway, _ = _gateway(DAY, _at(DAY, "16:49"))

    table = gateway.fetch_dataset("price:成交股數", target_date=DAY, symbols=("2330", "2317"))

    assert table.dates == ("2026-11-20",)
    assert _logged(capsys) == [
        {
            "finlab_final_publication_missing": {
                "dataset": "price:成交股數",
                "target_date": "2026-11-20",
                "published_at": "2026-11-20T16:49:00+08:00",
            }
        }
    ]


def test_the_catalogue_is_never_held_for_a_final_copy(capsys) -> None:
    gateway, storage = _gateway(LATER, _at(LATER, "16:49"))

    gateway.fetch_catalogue(symbols=("2330",))

    assert storage.read == [] and _logged(capsys) == []


def test_a_table_not_final_yet_waits_for_publication_like_a_missing_date() -> None:
    failure = FinLabAdapter().classify(
        FinLabTargetNotReadyError("FinLab has not published the final table yet"), "member"
    )

    assert failure.kind is FailureKind.ROUND_RETRY
    assert (failure.wait, failure.code) == ("publication", "finlab_target_not_ready")
