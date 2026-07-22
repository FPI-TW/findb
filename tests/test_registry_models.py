"""Registry model schema invariants."""

from app.models.registry import IngestionAttempt, IngestionRun


def test_ingestion_run_indexes_raw_payload_cleanup_lookup() -> None:
    index_names = {index.name for index in IngestionRun.__table__.indexes}

    assert "idx_run_raw_payload" in index_names


def test_ingestion_attempt_has_operational_lookup_indexes() -> None:
    index_names = {index.name for index in IngestionAttempt.__table__.indexes}

    assert "idx_ingestion_attempt_status_created" in index_names
    assert "idx_ingestion_attempt_dataset_created" in index_names
    assert "idx_ingestion_attempt_client_created" in index_names
    assert "idx_ingestion_attempt_idempotency" in index_names
