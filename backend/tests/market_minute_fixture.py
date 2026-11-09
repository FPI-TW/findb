"""Valid market_minute.v1 request, independent of any active provider adapter."""

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.schemas.ingress import minute_sequence_key_digest


def market_minute_request(
    symbol: str,
    *,
    dataset_key: str,
    target_date: date,
    fetched_at: datetime,
    snapshot_id: str,
    daily_update_id: str,
    universe_id: str,
    source: str = "shioaji",
    sequence_count: int = 1,
) -> dict[str, Any]:
    """One right-labelled 09:01 Taipei bar, which is what the contract was accepted with."""
    end = datetime.combine(target_date, datetime.min.time(), ZoneInfo("Asia/Taipei")) + timedelta(
        hours=9, minutes=1
    )
    start_utc = _utc_text(end - timedelta(minutes=1))
    end_utc = _utc_text(end)
    digest = minute_sequence_key_digest(
        dataset_key=dataset_key,
        data_date=target_date,
        snapshot_id=snapshot_id,
        sequence=1,
    )
    return {
        "dataset_key": dataset_key,
        "schema_id": "market_minute",
        "schema_version": 1,
        "source": source,
        "request_key": f"mmr:{digest}",
        "idempotency_key": f"mms:{digest}",
        "fetched_at": _utc_text(fetched_at),
        "payload": {
            "batch": {
                "data_date": target_date.isoformat(),
                "delivery_mode": "sequenced_snapshot",
                "declared_record_count": 1,
                "coverage_start_date": target_date.isoformat(),
                "coverage_end_date": target_date.isoformat(),
                "sequence": 1,
                "sequence_count": sequence_count,
                "snapshot_id": snapshot_id,
                "daily_update_id": daily_update_id,
                "universe_id": universe_id,
                "symbols_sha256": _sha256(symbol),
                "provider_usage_before": {"requests_used": 1, "requests_limit": 50},
                "provider_usage_after": {"requests_used": 2, "requests_limit": 50},
            },
            "data": [
                {
                    "symbol": symbol,
                    "source_symbol": symbol,
                    "trade_date": target_date.isoformat(),
                    "market_timezone": "Asia/Taipei",
                    "bar_start_time": start_utc,
                    "bar_end_time": end_utc,
                    "signal_time": end_utc,
                    "price_adjustment": "none",
                    "trade_count": None,
                    "open": "100",
                    "high": "101",
                    "low": "99",
                    "close": "100.5",
                    "volume": 2000,
                    "turnover": "200000",
                }
            ],
            "symbol_statuses": [{"symbol": symbol, "outcome": "data", "reason": None}],
        },
    }


def _sha256(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


async def legacy_source_client(db, **kwargs):
    """Reconstruct a pre-retirement identity solely for historical contract tests."""
    from app.models.registry import SourceClient
    from app.services.source_clients import hash_source_key
    from app.utils import uuid7

    kwargs.pop("commit", None)
    key = f"legacy-test-{uuid7()}"
    row = SourceClient(client_id=uuid7(), key_hash=hash_source_key(key), **kwargs)
    db.add(row)
    await db.flush()
    return row, key


@contextmanager
def legacy_acquisition_catalog():
    """Build pre-retirement frozen plans; restore all live admission rules afterwards."""
    from app.services import full_market_governance as governance
    from app.services.source_clients import PROVIDER_DATASET_SCOPE

    historical = {
        key: governance.FullMarketFeedDescriptor("market_minute", 1, "TW", asset, "minute")
        for key, asset in (("tw_equity_minute", "equity"), ("tw_etf_minute", "etf"))
    }
    with (
        patch.dict(PROVIDER_DATASET_SCOPE, {"shioaji": tuple(historical)}),
        patch.object(
            governance, "FULL_MARKET_FEEDS", {**governance.FULL_MARKET_FEEDS, **historical}
        ),
    ):
        yield
