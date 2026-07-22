"""Registry model schema invariants."""

from app.models.registry import IngestionAttempt, IngestionRun, MissingDeliveryAlert


def test_ingestion_run_indexes_raw_payload_cleanup_lookup() -> None:
    index_names = {index.name for index in IngestionRun.__table__.indexes}

    assert "idx_run_raw_payload" in index_names
    assert "idx_run_delivery_policy_baseline" in index_names


def test_ingestion_attempt_has_operational_lookup_indexes() -> None:
    index_names = {index.name for index in IngestionAttempt.__table__.indexes}

    assert "idx_ingestion_attempt_status_created" in index_names
    assert "idx_ingestion_attempt_dataset_created" in index_names
    assert "idx_ingestion_attempt_client_created" in index_names
    assert "idx_ingestion_attempt_idempotency" in index_names


def test_missing_delivery_alert_has_identity_and_health_indexes() -> None:
    index_names = {index.name for index in MissingDeliveryAlert.__table__.indexes}
    constraint_names = {
        constraint.name for constraint in MissingDeliveryAlert.__table__.constraints
    }

    assert "idx_missing_delivery_status_detected" in index_names
    assert "idx_missing_delivery_dataset_source" in index_names
    assert "uq_missing_delivery_identity_date" in constraint_names
    assert "ck_missing_delivery_alert_status_valid" in constraint_names
