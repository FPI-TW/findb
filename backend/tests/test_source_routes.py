"""Boundary checks for the contract-only Source API surface."""

from app.main import app


def test_source_openapi_contains_only_retained_operational_routes() -> None:
    paths = app.openapi()["paths"]
    source_paths = {path for path in paths if path.startswith("/api/v1/source/")}
    assert source_paths == {
        "/api/v1/source/contracts/{schema_id}/versions/{schema_version}",
        "/api/v1/source/scheduler-controls/{scheduler_key}/poll",
        "/api/v1/source/ingest",
        "/api/v1/source/attempts/{attempt_id}",
        "/api/v1/source/runs/{run_id}",
        "/api/v1/source/runs/{run_id}/rerun",
        "/api/v1/source/datasets",
    }


def test_source_openapi_has_no_legacy_or_direct_routes() -> None:
    paths = app.openapi()["paths"]
    assert all("/direct" not in path for path in paths)
    assert all(
        path != "/api/v1/source/ingest/{market}" and not path.startswith("/api/v1/source/ingest/")
        for path in paths
    )
