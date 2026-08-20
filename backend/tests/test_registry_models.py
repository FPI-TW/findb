"""Registry model schema invariants."""

from sqlalchemy import CheckConstraint

from app.models.registry import (
    IngestionAttempt,
    IngestionRun,
    MissingDeliveryAlert,
    NormalizationOutbox,
)


def test_ingestion_run_indexes_raw_payload_cleanup_lookup() -> None:
    index_names = {index.name for index in IngestionRun.__table__.indexes}

    assert "idx_run_raw_payload" in index_names
    assert "idx_run_delivery_policy_baseline" in index_names


def test_ingestion_run_persists_bounded_coverage_with_guards() -> None:
    columns = IngestionRun.__table__.columns
    assert columns["coverage_start_date"].nullable is True
    assert columns["coverage_end_date"].nullable is True

    constraints = {
        constraint.name: str(constraint.sqltext)
        for constraint in IngestionRun.__table__.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name
    }
    assert "ck_ingestion_run_coverage_dates_paired" in constraints
    assert "ck_ingestion_run_coverage_date_order" in constraints
    assert "ck_ingestion_run_backfill_coverage_end_matches_batch_date" in constraints


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


def test_normalization_outbox_has_one_active_generation_per_job() -> None:
    index = next(
        index
        for index in NormalizationOutbox.__table__.indexes
        if index.name == "uq_normalization_outbox_active_job"
    )

    assert index.unique is True
    assert str(index.dialect_options["postgresql"]["where"]) == (
        "status IN ('pending', 'publishing')"
    )
