from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.raw import RawMarketPayload
from app.models.registry import DatasetRegistry, IngestionRun
from app.schemas.source import IngestRequest
from app.services.ingestion import (
    DatasetInactiveError,
    DatasetNotFoundError,
    IngestionService,
    MarketMismatchError,
    PayloadValidationError,
    RawPayloadNotFoundError,
    trigger_normalization,
)


class AsyncContextManagerMock:
    def __init__(self, return_value):
        self.return_value = return_value

    async def __aenter__(self):
        return self.return_value

    async def __aexit__(self, exc_type, exc, tb):
        pass


class TestTriggerNormalization:
    @pytest.mark.asyncio
    async def test_trigger_normalization_process_success(self):
        """Should successfully execute the normalizer process with correct configuration"""
        mock_session = AsyncMock()
        mock_session_factory = MagicMock(return_value=AsyncContextManagerMock(mock_session))

        test_config = {"key": "val"}
        mock_dataset = MagicMock(config=test_config)
        mock_session.get.return_value = mock_dataset

        mock_normalizer_inst = AsyncMock()
        mock_normalizer_cls = MagicMock(return_value=mock_normalizer_inst)

        with patch("app.services.ingestion.NORMALIZER_MAP", {"test": mock_normalizer_cls}):
            await trigger_normalization("test", {"d": 1}, uuid4(), mock_session_factory)

        args, _ = mock_normalizer_cls.call_args
        assert args[0] == mock_session
        assert args[1] == test_config
        mock_normalizer_inst.process.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_trigger_normalization_no_normalizer(self):
        """Should fail the ingestion run if no normalizer is mapped to the dataset"""
        mock_session = AsyncMock()
        mock_session_factory = MagicMock(return_value=AsyncContextManagerMock(mock_session))

        mock_run = MagicMock(status="pending")
        mock_session.get.return_value = mock_run

        with patch("app.services.ingestion.NORMALIZER_MAP", {}):
            await trigger_normalization("none", {}, uuid4(), mock_session_factory)

        assert mock_run.status == "failed"
        assert "No normalizer configured" in mock_run.error_message

    @pytest.mark.asyncio
    async def test_trigger_normalization_dataset_not_found(self):
        """Should handle missing normalizers by failing the ingestion run"""
        mock_session = AsyncMock()
        mock_session_factory = MagicMock(return_value=AsyncContextManagerMock(mock_session))

        mock_run = MagicMock(status="pending")
        mock_session.get.side_effect = [None, mock_run]

        mock_normalizer_cls = MagicMock()

        with patch("app.services.ingestion.NORMALIZER_MAP", {"test": mock_normalizer_cls}):
            await trigger_normalization("test", {}, uuid4(), mock_session_factory)

        assert mock_run.status == "failed"
        assert "not found" in mock_run.error_message


class TestIngetionService:
    class TestGetRawPayloadByIdempotencyKey:
        @pytest.mark.asyncio
        async def test_get_raw_payload_by_idempotency_key(self):
            """Should retrieve a raw payload by its unique idempotency key."""
            mock_db = AsyncMock()
            service = IngestionService(mock_db)

            test_key = "unique_req_123"
            mock_payload = MagicMock(spec=RawMarketPayload)

            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = mock_payload
            mock_db.execute.return_value = mock_result

            result = await service.get_raw_payload_by_idempotency_key(test_key)

            assert result == mock_payload

    class TestGetDataset:
        @pytest.mark.parametrize(
            "include_inactive, expected_where_count",
            [
                (True, 1),
                (False, 2),
            ],
        )
        @pytest.mark.asyncio
        async def test_get_dataset_success(self, include_inactive, expected_where_count):
            """Should apply the correct active status filter based on include_inactive flag."""
            mock_db = AsyncMock()
            service = IngestionService(mock_db)

            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = MagicMock(spec=DatasetRegistry)
            mock_db.execute.return_value = mock_result

            await service.get_dataset("test_key", include_inactive=include_inactive)

            args, _ = mock_db.execute.call_args
            stmt = args[0]

            assert len(stmt._where_criteria) == expected_where_count

    class TestValidatePayloadSchema:
        @pytest.mark.parametrize(
            "asset_class, dataset_key, mapping, payload_data",
            [
                # 1. Commmon Trade Date logic
                ("equity", "us_equity", {"trade_date": "date"}, [{"date": "2026-04-28"}]),
                # 2. Macro Obs Date logic
                ("macro", "macro_gdp", {}, [{"obs_date": "2026-04-28"}]),
                # 3. Corporate Actions logic
                (
                    "equity",
                    "us_corporate_actions",
                    {},
                    [{"action_type": "DIV", "ex_date": "2026-04-28"}],
                ),
                # 4. Future Contracts logic
                ("future", "ice_future_contracts", {"contract_code": "code"}, [{"code": "TSLA1"}]),
            ],
            ids=["equity_trade_date", "macro_obs_date", "corp_actions", "futures"],
        )
        def test_validate_payload_schema_success(
            self, asset_class, dataset_key, mapping, payload_data
        ):
            """Should validate various asset types and field mappings against the payload schema"""
            service = IngestionService(MagicMock())
            mock_dataset = MagicMock(
                asset_class=asset_class, dataset_key=dataset_key, config={"field_mapping": mapping}
            )
            payload = {"data": payload_data}

            result = service.validate_payload_schema(payload, mock_dataset)

            assert len(result) == len(payload_data)
            assert result == payload_data

        def test_validate_payload_schema_failures(self):
            """Should raise PayloadValidationError when the payload format or required fields are invalid"""
            service = IngestionService(MagicMock())
            mock_dataset = MagicMock(asset_class="equity", dataset_key="test", config={})

            with pytest.raises(PayloadValidationError, match="Payload must be an object"):
                service.validate_payload_schema([], mock_dataset)

            with pytest.raises(PayloadValidationError, match="Payload missing data list"):
                service.validate_payload_schema({"wrong_key": []}, mock_dataset)

            with pytest.raises(PayloadValidationError, match="missing required field: trade_date"):
                service.validate_payload_schema({"data": [{"wrong_field": "val"}]}, mock_dataset)

    class TestCreateIngestionRun:
        @pytest.mark.asyncio
        async def test_create_ingestion_run_success(self):
            """Should correctly initialize an IngestionRun with default values and flush to DB."""
            mock_db = AsyncMock()
            service = IngestionService(mock_db)

            test_key = "us_equity_eod"
            test_metadata = {"env": "test"}

            run = await service.create_ingestion_run(
                dataset_key=test_key, source="bloomberg", metadata=test_metadata
            )

            assert isinstance(run, IngestionRun)
            assert run.dataset_key == test_key
            assert run.status == "pending"
            assert run.metadata_ == test_metadata
            assert run.run_id is not None

            mock_db.add.assert_called_once_with(run)
            mock_db.flush.assert_awaited_once()

    class TestRerunFromRaw:
        @pytest.mark.asyncio
        async def test_rerun_from_raw_success(self):
            """Should successfully create a new run using existing raw payload data"""
            mock_db = AsyncMock()
            service = IngestionService(mock_db)
            old_run_id = uuid4()

            mock_raw = MagicMock(spec=RawMarketPayload)
            mock_raw.dataset_key = "us_equity"
            mock_raw.source = "bloomberg"
            mock_raw.request_key = "req_123"
            mock_raw.idempotency_key = "idem_456"
            mock_raw.dataset_key = "us_equity_eod"
            mock_raw.payload = {"data": [{"date": "2026-04-29", "close": 150}]}

            service.get_raw_payload_by_run = AsyncMock(return_value=mock_raw)

            mock_dataset = MagicMock(spec=DatasetRegistry)
            mock_dataset.dataset_key = "us_equity_eod"
            mock_dataset.asset_class = "equity"
            mock_dataset.is_active = True
            mock_dataset.config = {}
            service.get_dataset = AsyncMock(return_value=mock_dataset)

            new_run = MagicMock(spec=IngestionRun)
            new_run.run_id = uuid4()
            new_run.status = "pending"
            service.create_ingestion_run = AsyncMock(return_value=new_run)

            new_id, status, d_key, payload = await service.rerun_from_raw(old_run_id)

            assert new_id == new_run.run_id
            assert status == "pending"
            assert d_key == "us_equity_eod"
            assert payload == mock_raw.payload

            mock_db.commit.assert_awaited_once()

            args, kwargs = service.create_ingestion_run.call_args
            sent_metadata = kwargs.get("metadata")
            assert sent_metadata["rerun_from_run_id"] == str(old_run_id)
            assert sent_metadata["rerun_from_idempotency_key"] == "idem_456"

        @pytest.mark.asyncio
        async def test_rerun_from_raw_not_found(self):
            """Should raise RawPayloadNotFoundError when no raw data exists for the run_id"""
            service = IngestionService(AsyncMock())
            service.get_raw_payload_by_run = AsyncMock(return_value=None)

            with pytest.raises(RawPayloadNotFoundError):
                await service.rerun_from_raw(uuid4())

        @pytest.mark.asyncio
        async def test_rerun_from_raw_dataset_inactive(self):
            """Should raise DatasetInactiveError when attempting to rerun a disabled dataset"""
            service = IngestionService(AsyncMock())

            mock_raw = MagicMock(dataset_key="old_data")
            service.get_raw_payload_by_run = AsyncMock(return_value=mock_raw)

            mock_dataset = MagicMock(is_active=False)
            service.get_dataset = AsyncMock(return_value=mock_dataset)

            with pytest.raises(DatasetInactiveError):
                await service.rerun_from_raw(uuid4())

    class TestIngest:
        @pytest.mark.asyncio
        async def test_ingest_success(self):
            """Should process ingestion and verify the IngestionRun creation (Merged)"""
            mock_db = AsyncMock()
            mock_db.add = MagicMock()
            service = IngestionService(mock_db)

            mock_dataset = MagicMock()
            mock_dataset.dataset_key = "us_equity_eod"
            mock_dataset.is_active = True
            mock_dataset.asset_class = "equity"
            mock_dataset.config = {"field_mapping": {"trade_date": "date"}}

            service.get_dataset = AsyncMock(return_value=mock_dataset)
            service.get_raw_payload_by_idempotency_key = AsyncMock(return_value=None)

            request = IngestRequest(
                dataset_key="us_equity_eod",
                source="bloomberg",
                request_key="req_123",
                idempotency_key="unique_key_456",
                payload={"data": [{"date": "2026-04-28", "price": 100}]},
                fetched_at=datetime.now(timezone.utc),
            )

            run_id, status, is_duplicate = await service.ingest(request)

            assert status == "pending"
            assert is_duplicate is False
            assert run_id is not None

            all_added_objects = [call[0][0] for call in mock_db.add.call_args_list]

            added_run = next(
                (obj for obj in all_added_objects if isinstance(obj, IngestionRun)), None
            )

            assert added_run is not None, "應該要有一個 IngestionRun 被加入資料庫"
            assert added_run.dataset_key == "us_equity_eod"
            assert added_run.status == "pending"
            assert added_run.source == "bloomberg"

            added_raw = next(
                (obj for obj in all_added_objects if isinstance(obj, RawMarketPayload)), None
            )
            assert added_raw is not None

            mock_db.commit.assert_awaited_once()
            assert mock_db.flush.await_count == 2

        @pytest.mark.asyncio
        async def test_ingest_dataset_not_found(self):
            """Should throw DatasetNotFoundError if dataset does not exist"""
            mock_db = AsyncMock()
            service = IngestionService(mock_db)

            service.get_dataset = AsyncMock(return_value=None)

            request = IngestRequest(
                dataset_key="invalid_key",
                source="test",
                request_key="req_1",
                idempotency_key="idem_1",
                payload={"data": []},
                fetched_at=datetime.now(timezone.utc),
            )

            with pytest.raises(DatasetNotFoundError):
                await service.ingest(request)

        @pytest.mark.asyncio
        async def test_ingest_duplicate_idempotency(self):
            """Should return is_duplicate=True when involving duplicate idempotency_keys"""
            mock_db = AsyncMock()
            service = IngestionService(mock_db)

            mock_dataset = MagicMock()
            mock_dataset.is_active = True
            service.get_dataset = AsyncMock(return_value=mock_dataset)

            existing_raw = MagicMock()
            existing_raw.run_id = uuid4()
            service.get_raw_payload_by_idempotency_key = AsyncMock(return_value=existing_raw)

            existing_run = MagicMock()
            existing_run.status = "completed"
            mock_db.get = AsyncMock(return_value=existing_run)

            request = IngestRequest(
                dataset_key="us_equity_eod",
                source="test",
                request_key="req_1",
                idempotency_key="duplicate_key",
                payload={"data": [{"trade_date": "2026-04-28"}]},
                fetched_at=datetime.now(timezone.utc),
            )

            run_id, status, is_duplicate = await service.ingest(request)

            assert is_duplicate is True
            assert run_id == existing_raw.run_id
            assert status == "completed"

        @pytest.mark.asyncio
        async def test_validate_payload_schema_failure(self):
            """Should throw PayloadValidationError when missing necessary field"""
            mock_db = AsyncMock()
            service = IngestionService(mock_db)

            mock_dataset = MagicMock()
            mock_dataset.dataset_key = "us_equity_eod"
            mock_dataset.config = {"field_mapping": {"trade_date": "date"}}

            bad_payload = {"data": [{"price": 100}]}

            with pytest.raises(PayloadValidationError) as excinfo:
                service.validate_payload_schema(bad_payload, mock_dataset)

            assert "missing required field: trade_date" in str(excinfo.value)

        @pytest.mark.asyncio
        async def test_ingest_dataset_inactive(self):
            """Should log failure and throw DatasetInactiveError when the dataset is disabled(is_active=False)"""
            mock_db = AsyncMock()
            service = IngestionService(mock_db)

            mock_dataset = MagicMock()
            mock_dataset.dataset_key = "us_equity_eod"
            mock_dataset.is_active = False
            service.get_dataset = AsyncMock(return_value=mock_dataset)

            mock_run = MagicMock()
            service.create_ingestion_run = AsyncMock(return_value=mock_run)

            request = IngestRequest(
                dataset_key="us_equity_eod",
                source="test",
                request_key="req_inactive",
                idempotency_key="idem_inactive",
                payload={"data": []},
                fetched_at=datetime.now(timezone.utc),
            )

            with pytest.raises(DatasetInactiveError):
                await service.ingest(request)

            assert mock_run.status == "failed"
            assert "is inactive" in mock_run.error_message
            assert mock_run.completed_at is not None
            mock_db.commit.assert_awaited_once()

        @pytest.mark.asyncio
        async def test_ingest_market_mismatch(self):
            """Should throw MarketMismatchError if the requested market does not match the market defined in the db"""
            mock_db = AsyncMock()
            service = IngestionService(mock_db)

            mock_dataset = MagicMock()
            mock_dataset.market = "US"
            mock_dataset.is_active = True
            service.get_dataset = AsyncMock(return_value=mock_dataset)

            request = IngestRequest(
                dataset_key="us_equity_eod",
                source="bloomberg",
                request_key="req_mismatch",
                idempotency_key="idem_mismatch",
                payload={"data": []},
                fetched_at=datetime.now(timezone.utc),
            )

            with pytest.raises(MarketMismatchError) as excinfo:
                await service.ingest(request, expected_market="HK")

            assert "belongs to market US, not HK" in str(excinfo.value)
            mock_db.commit.assert_not_called()

        @pytest.mark.asyncio
        async def test_ingest_payload_validation_failure_persistence(self):
            """Should correctly create and submit failed run record when payload verification fails"""
            mock_db = AsyncMock()
            service = IngestionService(mock_db)

            mock_dataset = MagicMock()
            mock_dataset.is_active = True
            service.get_dataset = AsyncMock(return_value=mock_dataset)

            service.get_raw_payload_by_idempotency_key = AsyncMock(return_value=None)

            mock_run = MagicMock()
            service.create_ingestion_run = AsyncMock(return_value=mock_run)

            service.validate_payload_schema = MagicMock(
                side_effect=PayloadValidationError("Missing required field: trade_date")
            )

            request = IngestRequest(
                dataset_key="us_equity_eod",
                source="bloomberg",
                request_key="req_bad_payload",
                idempotency_key="idem_bad_payload",
                payload={"invalid": "data"},
                fetched_at=datetime.now(timezone.utc),
            )

            with pytest.raises(PayloadValidationError):
                await service.ingest(request)

            assert mock_run.status == "failed"
            assert "Missing required field: trade_date" in mock_run.error_message
            assert mock_run.completed_at is not None

            mock_db.commit.assert_awaited_once()

        @pytest.mark.asyncio
        async def test_ingest_integrity_error_handling(self):
            """Should handle race conditions using IntegrityError during commit"""
            mock_db = AsyncMock()
            mock_db.add = MagicMock()

            service = IngestionService(mock_db)

            mock_dataset = MagicMock()
            mock_dataset.dataset_key = "us_equity_eod"
            mock_dataset.asset_class = "equity"
            mock_dataset.is_active = True
            mock_dataset.config = {"field_mapping": {"trade_date": "date"}}
            service.get_dataset = AsyncMock(return_value=mock_dataset)

            mock_run = MagicMock()
            mock_run.run_id = uuid4()
            service.create_ingestion_run = AsyncMock(return_value=mock_run)

            existing_raw = MagicMock()
            existing_raw.run_id = uuid4()
            service.get_raw_payload_by_idempotency_key = AsyncMock(side_effect=[None, existing_raw])

            mock_db.commit.side_effect = IntegrityError("duplicate key", params={}, orig=None)

            existing_run = MagicMock()
            existing_run.status = "completed"
            mock_db.get = AsyncMock(return_value=existing_run)

            request = IngestRequest(
                dataset_key="us_equity_eod",
                source="bloomberg",
                request_key="req_race",
                idempotency_key="idem_race",
                payload={"data": [{"date": "2026-04-28", "price": 100}]},
                fetched_at=datetime.now(timezone.utc),
            )

            run_id, status, is_duplicate = await service.ingest(request)

            assert is_duplicate is True
            assert status == "completed"
            assert run_id == existing_raw.run_id
            mock_db.rollback.assert_awaited_once()

        @pytest.mark.asyncio
        async def test_ingest_integrity_error_no_duplicate_found(self):
            """Should re-raise IntegrityError if no duplicate payload is found after rollback"""
            mock_db = AsyncMock()
            mock_db.add = MagicMock()
            mock_db.rollback = AsyncMock()

            service = IngestionService(mock_db)

            mock_dataset = MagicMock(is_active=True, asset_class="equity")
            mock_dataset.dataset_key = "us_equity_eod"
            mock_dataset.config = {"field_mapping": {"trade_date": "date"}}
            service.get_dataset = AsyncMock(return_value=mock_dataset)
            service.create_ingestion_run = AsyncMock(return_value=MagicMock())

            mock_db.commit.side_effect = IntegrityError("unexpected conflict", params={}, orig=None)

            service.get_raw_payload_by_idempotency_key = AsyncMock(return_value=None)

            request = IngestRequest(
                dataset_key="us_equity_eod",
                source="test",
                request_key="req_error",
                idempotency_key="idem_error",
                payload={"data": [{"date": "2026-04-28"}]},
                fetched_at=datetime.now(timezone.utc),
            )

            with pytest.raises(IntegrityError):
                await service.ingest(request)

            assert mock_db.rollback.await_count == 1
