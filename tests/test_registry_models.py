"""Registry model schema invariants."""

from app.models.registry import IngestionRun


def test_ingestion_run_indexes_raw_payload_cleanup_lookup() -> None:
    index_names = {index.name for index in IngestionRun.__table__.indexes}

    assert "idx_run_raw_payload" in index_names
