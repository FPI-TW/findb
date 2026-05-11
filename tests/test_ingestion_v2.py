"""Tests for IngestionService.ingest_v2 (app/services/ingestion.py).

What is covered:
- Happy path: IngestionRun + RawMarketPayload persisted, normalization triggered
- Dataset validation: not found, inactive (failed run created)
- Market mismatch: wrong expected_market raises error; case-insensitive match passes
- Idempotency: duplicate message_id short-circuits, no second run, normalization not called again
- Payload validation: bad schema creates failed run; empty data list accepted
- Race condition (IntegrityError path): falls back to existing run

Strategy:
- All tests use the real `test_session` fixture (real DB, per-test teardown)
- `trigger_normalization_v2` is always mocked — we test service logic, not normalization
- DatasetRegistry rows are seeded via async fixtures within the same session
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.raw import RawMarketPayload
from app.models.registry import DatasetRegistry, IngestionRun
from app.schemas.source import IngestRequestV2
from app.services.ingestion import (
    DatasetInactiveError,
    DatasetNotFoundError,
    IngestionService,
    MarketMismatchError,
    PayloadValidationError,
)
from app.utils import utc_now, uuid7

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

DATASET_KEY = "us_stock_eod"

# Valid payload for us_stock_eod:
#   data_path = "data"
#   field_mapping.trade_date = "timestamp.query_time"
#   candidate trade_date paths: ["timestamp.query_time", "timestamp.last_update", "date"]
#   → item with "date" field satisfies validation
VALID_PAYLOAD = {
    "metadata": {"source": "bloomberg", "query_time": "2026-01-02T00:00:00Z"},
    "data": [
        {
            "ticker": "AAPL US Equity",
            "date": "2026-01-02",
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1_000_000,
        }
    ],
}

# Payload whose data items have no recognised date field → fails validation
PAYLOAD_MISSING_DATE = {
    "metadata": {"source": "bloomberg", "query_time": "2026-01-02T00:00:00Z"},
    "data": [{"ticker": "AAPL US Equity", "open": 100.0}],
}

# Payload where "data" is not a list → fails validation
PAYLOAD_BAD_SCHEMA = {
    "metadata": {"source": "bloomberg"},
    "data": "not-a-list",
}


def make_request(
    *,
    dataset_key: str = DATASET_KEY,
    source: str = "bloomberg",
    request_key: str = "test-req-001",
    message_id: str = "test-msg-001",
    payload: dict | None = None,
    fetched_at: datetime | None = None,
) -> IngestRequestV2:
    return IngestRequestV2(
        dataset_key=dataset_key,
        source=source,
        request_key=request_key,
        idempotency_key=message_id,
        message_id=message_id,
        payload=payload if payload is not None else VALID_PAYLOAD,
        fetched_at=fetched_at or datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def active_dataset(test_session: AsyncSession) -> DatasetRegistry:
    """Seed an active us_stock_eod DatasetRegistry row into the test DB."""
    ds = DatasetRegistry(
        dataset_key=DATASET_KEY,
        name="US Stock EOD (test)",
        description="Test dataset",
        asset_class="equity",
        market="US",
        frequency="daily",
        is_active=True,
        config={
            "source_format": "bloomberg_usstock_api",
            "data_path": "data",
            "field_mapping": {
                "trade_date": "timestamp.query_time",
                "open": "price.open",
            },
        },
    )
    test_session.add(ds)
    await test_session.commit()
    return ds


@pytest_asyncio.fixture
async def inactive_dataset(test_session: AsyncSession) -> DatasetRegistry:
    """Seed an inactive us_stock_eod DatasetRegistry row into the test DB."""
    ds = DatasetRegistry(
        dataset_key=DATASET_KEY,
        name="US Stock EOD (inactive test)",
        description="Test dataset",
        asset_class="equity",
        market="US",
        frequency="daily",
        is_active=False,
        config={
            "data_path": "data",
            "field_mapping": {"trade_date": "date"},
        },
    )
    test_session.add(ds)
    await test_session.commit()
    return ds


@pytest.fixture
def mock_normalize():
    """Prevent trigger_normalization_v2 from executing actual normalization."""
    with patch(
        "app.services.ingestion.trigger_normalization_v2",
        new_callable=AsyncMock,
    ) as m:
        yield m


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestIngestV2HappyPath:
    @pytest.mark.asyncio
    async def test_returns_run_id_and_not_duplicate(
        self, test_session: AsyncSession, active_dataset, mock_normalize
    ):
        svc = IngestionService(test_session)
        run_id, _, is_dup = await svc.ingest_v2(make_request())

        from uuid import UUID

        assert isinstance(run_id, UUID)
        assert is_dup is False

    @pytest.mark.asyncio
    async def test_ingestion_run_persisted_with_correct_fields(
        self, test_session: AsyncSession, active_dataset, mock_normalize
    ):
        msg_id = "msg-fields-check"
        svc = IngestionService(test_session)
        run_id, _, _ = await svc.ingest_v2(
            make_request(source="bloomberg", request_key="req-abc", message_id=msg_id)
        )

        await test_session.refresh(await test_session.get(IngestionRun, run_id))
        run = await test_session.get(IngestionRun, run_id)
        assert run is not None
        assert run.dataset_key == DATASET_KEY
        assert run.source == "bloomberg"
        assert run.request_key == "req-abc"
        assert run.message_id == msg_id

    @pytest.mark.asyncio
    async def test_raw_records_count_matches_payload_data_length(
        self, test_session: AsyncSession, active_dataset, mock_normalize
    ):
        items = [
            {**VALID_PAYLOAD["data"][0], "ticker": f"T{i} US Equity", "date": f"2026-01-{i+1:02d}"}
            for i in range(5)
        ]
        payload = {**VALID_PAYLOAD, "data": items}
        svc = IngestionService(test_session)
        run_id, _, _ = await svc.ingest_v2(make_request(payload=payload))

        run = await test_session.get(IngestionRun, run_id)
        assert run.raw_records == 5

    @pytest.mark.asyncio
    async def test_raw_market_payload_persisted_with_correct_fields(
        self, test_session: AsyncSession, active_dataset, mock_normalize
    ):
        msg_id = "msg-persist-raw"
        svc = IngestionService(test_session)
        run_id, _, _ = await svc.ingest_v2(make_request(message_id=msg_id))

        # message_id is stored as idempotency_key (PK) in RawMarketPayload
        stmt = select(RawMarketPayload).where(RawMarketPayload.idempotency_key == msg_id)
        result = await test_session.execute(stmt)
        raw = result.scalar_one_or_none()

        assert raw is not None
        assert raw.run_id == run_id
        assert raw.dataset_key == DATASET_KEY
        assert raw.source == "bloomberg"
        assert raw.payload == VALID_PAYLOAD

    @pytest.mark.asyncio
    async def test_trigger_normalization_called_with_correct_args(
        self, test_session: AsyncSession, active_dataset, mock_normalize
    ):
        svc = IngestionService(test_session)
        run_id, _, _ = await svc.ingest_v2(make_request())

        mock_normalize.assert_called_once()
        args = mock_normalize.call_args.args
        assert args[0] == DATASET_KEY  # dataset_key
        assert args[1] == VALID_PAYLOAD  # payload
        assert args[2] == run_id  # run_id

    @pytest.mark.asyncio
    async def test_empty_data_list_accepted_creates_run_with_zero_records(
        self, test_session: AsyncSession, active_dataset, mock_normalize
    ):
        payload = {"metadata": VALID_PAYLOAD["metadata"], "data": []}
        svc = IngestionService(test_session)
        run_id, _, is_dup = await svc.ingest_v2(make_request(payload=payload))

        assert is_dup is False
        run = await test_session.get(IngestionRun, run_id)
        assert run.raw_records == 0


# ---------------------------------------------------------------------------
# Dataset validation
# ---------------------------------------------------------------------------


class TestIngestV2DatasetValidation:
    @pytest.mark.asyncio
    async def test_dataset_not_found_raises_error(self, test_session: AsyncSession, mock_normalize):
        svc = IngestionService(test_session)
        with pytest.raises(DatasetNotFoundError):
            await svc.ingest_v2(make_request(dataset_key="nonexistent_dataset_xyz"))

    @pytest.mark.asyncio
    async def test_dataset_not_found_does_not_trigger_normalization(
        self, test_session: AsyncSession, mock_normalize
    ):
        svc = IngestionService(test_session)
        with pytest.raises(DatasetNotFoundError):
            await svc.ingest_v2(make_request(dataset_key="nonexistent_dataset_xyz"))
        mock_normalize.assert_not_called()

    @pytest.mark.asyncio
    async def test_inactive_dataset_raises_error(
        self, test_session: AsyncSession, inactive_dataset, mock_normalize
    ):
        svc = IngestionService(test_session)
        with pytest.raises(DatasetInactiveError):
            await svc.ingest_v2(make_request())

    @pytest.mark.asyncio
    async def test_inactive_dataset_creates_failed_run(
        self, test_session: AsyncSession, inactive_dataset, mock_normalize
    ):
        msg_id = "msg-inactive"
        svc = IngestionService(test_session)
        with pytest.raises(DatasetInactiveError):
            await svc.ingest_v2(make_request(message_id=msg_id))

        result = await test_session.execute(
            select(IngestionRun).where(IngestionRun.dataset_key == DATASET_KEY)
        )
        run = result.scalar_one_or_none()
        assert run is not None
        assert run.status == "failed"
        assert run.message_id == msg_id
        assert "inactive" in (run.error_message or "").lower()

    @pytest.mark.asyncio
    async def test_inactive_dataset_does_not_trigger_normalization(
        self, test_session: AsyncSession, inactive_dataset, mock_normalize
    ):
        svc = IngestionService(test_session)
        with pytest.raises(DatasetInactiveError):
            await svc.ingest_v2(make_request())
        mock_normalize.assert_not_called()


# ---------------------------------------------------------------------------
# Market mismatch
# ---------------------------------------------------------------------------


class TestIngestV2MarketMismatch:
    @pytest.mark.asyncio
    async def test_wrong_expected_market_raises_error(
        self, test_session: AsyncSession, active_dataset, mock_normalize
    ):
        # active_dataset.market = "US"; sending CRYPTO → mismatch
        svc = IngestionService(test_session)
        with pytest.raises(MarketMismatchError):
            await svc.ingest_v2(make_request(), expected_market="CRYPTO")

    @pytest.mark.asyncio
    async def test_wrong_market_does_not_trigger_normalization(
        self, test_session: AsyncSession, active_dataset, mock_normalize
    ):
        svc = IngestionService(test_session)
        with pytest.raises(MarketMismatchError):
            await svc.ingest_v2(make_request(), expected_market="CRYPTO")
        mock_normalize.assert_not_called()

    @pytest.mark.asyncio
    async def test_market_match_case_insensitive_succeeds(
        self, test_session: AsyncSession, active_dataset, mock_normalize
    ):
        # active_dataset.market = "US"; "us" (lowercase) should match
        svc = IngestionService(test_session)
        _, _, is_dup = await svc.ingest_v2(make_request(), expected_market="us")
        assert is_dup is False

    @pytest.mark.asyncio
    async def test_no_expected_market_skips_check(
        self, test_session: AsyncSession, active_dataset, mock_normalize
    ):
        svc = IngestionService(test_session)
        _, _, is_dup = await svc.ingest_v2(make_request(), expected_market=None)
        assert is_dup is False


# ---------------------------------------------------------------------------
# Worker audit trail for MarketMismatchError
# ---------------------------------------------------------------------------


class TestWorkerMarketMismatchAuditTrail:
    """Verify the failed IngestionRun that app/workers/tasks.py creates on MarketMismatchError.

    ingest_v2 raises MarketMismatchError without creating any run (service contract).
    The worker catches it, calls create_ingestion_run_v2, commits, then re-raises.
    These tests reproduce that catch-block logic using the same test_session so the
    DB assertions reflect what the worker writes in production.
    """

    @pytest.mark.asyncio
    async def test_ingest_v2_creates_no_run_on_market_mismatch(
        self, test_session: AsyncSession, active_dataset, mock_normalize
    ):
        """Service contract: ingest_v2 must NOT create a run — the worker is responsible."""
        svc = IngestionService(test_session)
        with pytest.raises(MarketMismatchError):
            await svc.ingest_v2(make_request(), expected_market="CRYPTO")

        result = await test_session.execute(select(IngestionRun))
        assert len(result.scalars().all()) == 0

    @pytest.mark.asyncio
    async def test_worker_handler_creates_failed_run(
        self, test_session: AsyncSession, active_dataset, mock_normalize
    ):
        msg_id = "msg-mm-worker-run"
        svc = IngestionService(test_session)

        try:
            await svc.ingest_v2(make_request(message_id=msg_id), expected_market="CRYPTO")
        except MarketMismatchError as exc:
            run = await svc.create_ingestion_run_v2(
                DATASET_KEY,
                source="bloomberg",
                request_key="test-req-001",
                message_id=msg_id,
                raw_records=0,
                metadata={"source": "bloomberg", "request_key": "test-req-001", "raw_records": 0},
                status="failed",
            )
            run.error_message = str(exc)
            run.completed_at = utc_now()
            await test_session.commit()

        result = await test_session.execute(
            select(IngestionRun).where(IngestionRun.message_id == msg_id)
        )
        run = result.scalar_one_or_none()
        assert run is not None
        assert run.status == "failed"
        assert run.dataset_key == DATASET_KEY
        assert run.message_id == msg_id
        assert run.completed_at is not None

    @pytest.mark.asyncio
    async def test_worker_failed_run_error_message_names_both_markets(
        self, test_session: AsyncSession, active_dataset, mock_normalize
    ):
        """error_message must name both the dataset's actual market and the mismatched one."""
        msg_id = "msg-mm-worker-msg"
        svc = IngestionService(test_session)

        try:
            await svc.ingest_v2(make_request(message_id=msg_id), expected_market="CRYPTO")
        except MarketMismatchError as exc:
            run = await svc.create_ingestion_run_v2(
                DATASET_KEY,
                source="bloomberg",
                request_key="test-req-001",
                message_id=msg_id,
                raw_records=0,
                metadata={"source": "bloomberg", "request_key": "test-req-001", "raw_records": 0},
                status="failed",
            )
            run.error_message = str(exc)
            run.completed_at = utc_now()
            await test_session.commit()

        result = await test_session.execute(
            select(IngestionRun).where(IngestionRun.message_id == msg_id)
        )
        run = result.scalar_one_or_none()
        assert run is not None
        error_msg = run.error_message or ""
        assert "US" in error_msg  # active_dataset.market = "US"
        assert "CRYPTO" in error_msg  # expected_market sent by the endpoint

    @pytest.mark.asyncio
    async def test_worker_failed_run_has_no_raw_payload(
        self, test_session: AsyncSession, active_dataset, mock_normalize
    ):
        """Market mismatch is rejected before store_raw_payload_v2 — no raw row written."""
        msg_id = "msg-mm-no-raw"
        svc = IngestionService(test_session)

        try:
            await svc.ingest_v2(make_request(message_id=msg_id), expected_market="CRYPTO")
        except MarketMismatchError as exc:
            run = await svc.create_ingestion_run_v2(
                DATASET_KEY,
                source="bloomberg",
                request_key="test-req-001",
                message_id=msg_id,
                raw_records=0,
                metadata={"source": "bloomberg", "request_key": "test-req-001", "raw_records": 0},
                status="failed",
            )
            run.error_message = str(exc)
            run.completed_at = utc_now()
            await test_session.commit()

        result = await test_session.execute(
            select(RawMarketPayload).where(RawMarketPayload.message_id == msg_id)
        )
        assert result.scalar_one_or_none() is None


# ---------------------------------------------------------------------------
# Worker audit trail for ValidationError
# ---------------------------------------------------------------------------


class TestWorkerValidationErrorAuditTrail:
    """Verify tasks.py's ValidationError handler creates a failed IngestionRun.

    ValidationError fires before IngestRequestV2 is built, so the worker reads
    dataset_key and message_id directly from the raw envelope and calls
    create_ingestion_run_v2 only when both are present.

    Most common real cause: metadata.query_time absent/malformed → fetched_at=None.
    In this case dataset_key and message_id are still valid, so a run CAN be created.
    """

    @pytest.mark.asyncio
    async def test_failed_run_created_when_envelope_has_valid_dataset_and_message_id(
        self, test_session: AsyncSession, active_dataset, mock_normalize
    ):
        msg_id = "msg-ve-run"
        with pytest.raises(ValidationError) as exc_info:
            IngestRequestV2(
                dataset_key=DATASET_KEY,
                source="bloomberg",
                request_key="test-req-001",
                idempotency_key=msg_id,
                message_id=msg_id,
                payload=VALID_PAYLOAD,
                fetched_at=None,  # common real case: metadata.query_time absent
            )
        exc = exc_info.value

        # Reproduce worker catch block
        svc = IngestionService(test_session)
        failed_run = await svc.create_ingestion_run_v2(
            DATASET_KEY,
            source="bloomberg",
            request_key="test-req-001",
            message_id=msg_id,
            raw_records=0,
            metadata={"raw_records": 0},
            status="failed",
        )
        failed_run.error_message = f"Envelope validation error: {str(exc)[:500]}"
        failed_run.completed_at = utc_now()
        await test_session.commit()

        result = await test_session.execute(
            select(IngestionRun).where(IngestionRun.message_id == msg_id)
        )
        run = result.scalar_one_or_none()
        assert run is not None
        assert run.status == "failed"
        assert run.dataset_key == DATASET_KEY
        assert run.message_id == msg_id
        assert run.completed_at is not None

    @pytest.mark.asyncio
    async def test_error_message_starts_with_envelope_validation_error(
        self, test_session: AsyncSession, active_dataset, mock_normalize
    ):
        msg_id = "msg-ve-msg"
        with pytest.raises(ValidationError) as exc_info:
            IngestRequestV2(
                dataset_key=DATASET_KEY,
                source="bloomberg",
                request_key="test-req-001",
                idempotency_key=msg_id,
                message_id=msg_id,
                payload=VALID_PAYLOAD,
                fetched_at=None,
            )
        exc = exc_info.value

        svc = IngestionService(test_session)
        failed_run = await svc.create_ingestion_run_v2(
            DATASET_KEY,
            source="bloomberg",
            request_key="test-req-001",
            message_id=msg_id,
            raw_records=0,
            metadata={"raw_records": 0},
            status="failed",
        )
        failed_run.error_message = f"Envelope validation error: {str(exc)[:500]}"
        failed_run.completed_at = utc_now()
        await test_session.commit()

        result = await test_session.execute(
            select(IngestionRun).where(IngestionRun.message_id == msg_id)
        )
        run = result.scalar_one_or_none()
        assert run is not None
        assert (run.error_message or "").startswith("Envelope validation error:")

    @pytest.mark.asyncio
    async def test_no_raw_payload_stored(
        self, test_session: AsyncSession, active_dataset, mock_normalize
    ):
        """ValidationError fires before store_raw_payload_v2 — no raw row written."""
        msg_id = "msg-ve-no-raw"
        with pytest.raises(ValidationError):
            IngestRequestV2(
                dataset_key=DATASET_KEY,
                source="bloomberg",
                request_key="test-req-001",
                idempotency_key=msg_id,
                message_id=msg_id,
                payload=VALID_PAYLOAD,
                fetched_at=None,
            )

        result = await test_session.execute(
            select(RawMarketPayload).where(RawMarketPayload.message_id == msg_id)
        )
        assert result.scalar_one_or_none() is None

    @pytest.mark.asyncio
    async def test_no_run_created_when_dataset_key_missing(
        self, test_session: AsyncSession, mock_normalize
    ):
        """dataset_key=None → worker guard (if raw_dataset_key and raw_message_id) is False
        → run creation skipped, nothing in DB."""
        with pytest.raises(ValidationError):
            IngestRequestV2(
                dataset_key=None,
                source="bloomberg",
                request_key="test-req-001",
                idempotency_key="msg-ve-no-dk",
                message_id="msg-ve-no-dk",
                payload=VALID_PAYLOAD,
                fetched_at=None,
            )

        result = await test_session.execute(select(IngestionRun))
        assert len(result.scalars().all()) == 0

    @pytest.mark.asyncio
    async def test_no_run_created_when_message_id_missing(
        self, test_session: AsyncSession, mock_normalize
    ):
        """message_id=None → same guard fails → no run in DB."""
        with pytest.raises(ValidationError):
            IngestRequestV2(
                dataset_key=DATASET_KEY,
                source="bloomberg",
                request_key="test-req-001",
                idempotency_key=None,
                message_id=None,
                payload=VALID_PAYLOAD,
                fetched_at=None,
            )

        result = await test_session.execute(select(IngestionRun))
        assert len(result.scalars().all()) == 0


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIngestV2Idempotency:
    @pytest.mark.asyncio
    async def test_duplicate_key_returns_original_run_id(
        self, test_session: AsyncSession, active_dataset, mock_normalize
    ):
        svc = IngestionService(test_session)
        run_id_1, _, _ = await svc.ingest_v2(make_request(message_id="msg-dup"))
        run_id_2, _, is_dup = await svc.ingest_v2(make_request(message_id="msg-dup"))

        assert is_dup is True
        assert run_id_2 == run_id_1

    @pytest.mark.asyncio
    async def test_duplicate_key_does_not_create_second_run(
        self, test_session: AsyncSession, active_dataset, mock_normalize
    ):
        svc = IngestionService(test_session)
        await svc.ingest_v2(make_request(message_id="msg-single"))
        await svc.ingest_v2(make_request(message_id="msg-single"))

        result = await test_session.execute(
            select(IngestionRun).where(IngestionRun.dataset_key == DATASET_KEY)
        )
        runs = result.scalars().all()
        assert len(runs) == 1

    @pytest.mark.asyncio
    async def test_duplicate_key_does_not_store_second_raw_payload(
        self, test_session: AsyncSession, active_dataset, mock_normalize
    ):
        msg_id = "msg-raw-once"
        svc = IngestionService(test_session)
        await svc.ingest_v2(make_request(message_id=msg_id))
        await svc.ingest_v2(make_request(message_id=msg_id))

        result = await test_session.execute(
            select(RawMarketPayload).where(RawMarketPayload.idempotency_key == msg_id)
        )
        rows = result.scalars().all()
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_duplicate_does_not_trigger_normalization_again(
        self, test_session: AsyncSession, active_dataset, mock_normalize
    ):
        svc = IngestionService(test_session)
        await svc.ingest_v2(make_request(message_id="msg-norm-once"))
        await svc.ingest_v2(make_request(message_id="msg-norm-once"))

        assert mock_normalize.call_count == 1

    @pytest.mark.asyncio
    async def test_different_message_ids_create_separate_runs(
        self, test_session: AsyncSession, active_dataset, mock_normalize
    ):
        svc = IngestionService(test_session)
        run_id_a, _, _ = await svc.ingest_v2(make_request(message_id="msg-a"))
        run_id_b, _, _ = await svc.ingest_v2(make_request(message_id="msg-b"))

        assert run_id_a != run_id_b
        result = await test_session.execute(
            select(IngestionRun).where(IngestionRun.dataset_key == DATASET_KEY)
        )
        assert len(result.scalars().all()) == 2


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------


class TestIngestV2PayloadValidation:
    @pytest.mark.asyncio
    async def test_data_not_a_list_raises_payload_validation_error(
        self, test_session: AsyncSession, active_dataset, mock_normalize
    ):
        svc = IngestionService(test_session)
        with pytest.raises(PayloadValidationError):
            await svc.ingest_v2(make_request(payload=PAYLOAD_BAD_SCHEMA))

    @pytest.mark.asyncio
    async def test_items_missing_required_date_field_raises_error(
        self, test_session: AsyncSession, active_dataset, mock_normalize
    ):
        svc = IngestionService(test_session)
        with pytest.raises(PayloadValidationError):
            await svc.ingest_v2(make_request(payload=PAYLOAD_MISSING_DATE))

    @pytest.mark.asyncio
    async def test_invalid_payload_creates_failed_run(
        self, test_session: AsyncSession, active_dataset, mock_normalize
    ):
        msg_id = "msg-bad-payload"
        svc = IngestionService(test_session)
        with pytest.raises(PayloadValidationError):
            await svc.ingest_v2(make_request(payload=PAYLOAD_BAD_SCHEMA, message_id=msg_id))

        result = await test_session.execute(
            select(IngestionRun).where(IngestionRun.dataset_key == DATASET_KEY)
        )
        run = result.scalar_one_or_none()
        assert run is not None
        assert run.status == "failed"
        assert run.message_id == msg_id
        assert run.error_message is not None

    @pytest.mark.asyncio
    async def test_invalid_payload_does_not_trigger_normalization(
        self, test_session: AsyncSession, active_dataset, mock_normalize
    ):
        svc = IngestionService(test_session)
        with pytest.raises(PayloadValidationError):
            await svc.ingest_v2(make_request(payload=PAYLOAD_BAD_SCHEMA))
        mock_normalize.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_payload_does_not_store_raw_payload(
        self, test_session: AsyncSession, active_dataset, mock_normalize
    ):
        msg_id = "msg-bad-no-raw"
        svc = IngestionService(test_session)
        with pytest.raises(PayloadValidationError):
            await svc.ingest_v2(make_request(payload=PAYLOAD_BAD_SCHEMA, message_id=msg_id))

        result = await test_session.execute(
            select(RawMarketPayload).where(RawMarketPayload.idempotency_key == msg_id)
        )
        assert result.scalar_one_or_none() is None


# ---------------------------------------------------------------------------
# Race condition: IntegrityError fallback
# ---------------------------------------------------------------------------


class TestIngestV2RaceCondition:
    @pytest.mark.asyncio
    async def test_integrity_error_on_commit_falls_back_to_existing_run(
        self, test_session: AsyncSession, active_dataset, mock_normalize
    ):
        """Simulate the race: message_id check misses, commit hits IntegrityError,
        re-check finds the winner's row → returns (existing_run_id, status, True)."""
        msg_id = "msg-race"

        # Seed the "winning" request's rows directly (simulates concurrent write)
        existing_run = IngestionRun(
            run_id=uuid7(),
            dataset_key=DATASET_KEY,
            source="bloomberg",
            request_key="winner-req",
            message_id=msg_id,
            raw_records=1,
            status="completed",
        )
        test_session.add(existing_run)
        await test_session.flush()

        existing_raw = RawMarketPayload(
            dataset_key=DATASET_KEY,
            source="bloomberg",
            request_key="winner-req",
            idempotency_key=msg_id,
            message_id=msg_id,
            payload=VALID_PAYLOAD,
            fetched_at=datetime.now(timezone.utc),
            expire_at=datetime.now(timezone.utc),
            run_id=existing_run.run_id,
        )
        test_session.add(existing_raw)
        await test_session.commit()

        # "Losing" request: first message_id check returns None (simulates race miss),
        # commit raises IntegrityError, re-check then finds the existing row.
        svc = IngestionService(test_session)
        original_check = svc.get_raw_payload_by_message_id
        call_count = 0

        async def race_check(mid: str):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return None  # miss → proceeds past idempotency guard
            return await original_check(mid)

        with patch.object(svc, "get_raw_payload_by_message_id", side_effect=race_check):
            with patch.object(
                svc,
                "store_raw_payload_v2",
                side_effect=IntegrityError("unique", {}, Exception()),
            ):
                run_id, status, is_dup = await svc.ingest_v2(make_request(message_id=msg_id))

        assert is_dup is True
        assert run_id == existing_run.run_id
        assert status == "completed"
