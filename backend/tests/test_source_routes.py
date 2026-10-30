"""Boundary checks for the contract-only Source API surface."""

from app.main import app


def test_source_openapi_contains_only_retained_operational_routes() -> None:
    paths = app.openapi()["paths"]
    source_paths = {path for path in paths if path.startswith("/api/v1/source/")}
    assert source_paths == {
        "/api/v1/source/contracts/{schema_id}/versions/{schema_version}",
        "/api/v1/source/scheduler-controls/{scheduler_key}/poll",
        "/api/v1/source/scheduler-controls/{scheduler_key}/authorization",
        "/api/v1/source/historical-backfills/{item_id}/authorization",
        "/api/v1/source/universes",
        "/api/v1/source/universes/{release_id}",
        "/api/v1/source/delivery-plans",
        "/api/v1/source/delivery-plans/{plan_id}",
        "/api/v1/source/delivery-plans/{plan_id}/outcomes",
        "/api/v1/source/full-market/authorization",
        "/api/v1/source/full-market/readiness",
        "/api/v1/source/full-market/enrollment-verification",
        "/api/v1/source/ingest",
        "/api/v1/source/attempts/{attempt_id}",
        "/api/v1/source/runs/{run_id}",
        "/api/v1/source/runs/{run_id}/rerun",
        "/api/v1/source/datasets",
        "/api/v1/source/historical-backfills/claim",
        "/api/v1/source/historical-backfills/{item_id}/complete",
        "/api/v1/source/provider-work",
        "/api/v1/source/provider-work/enqueue",
        "/api/v1/source/provider-work/claim",
        "/api/v1/source/provider-work/{work_id}/authorize",
        "/api/v1/source/provider-work/{work_id}/calendar-status",
        "/api/v1/source/provider-work/{work_id}/shioaji-diagnostics",
        "/api/v1/source/provider-work/{work_id}/shioaji-repairs",
        "/api/v1/source/provider-work/{work_id}/shioaji-repair-ack",
        "/api/v1/source/provider-work/{work_id}/heartbeat",
        "/api/v1/source/provider-work/{work_id}/manifest",
        "/api/v1/source/provider-work/{work_id}/prepare",
        "/api/v1/source/provider-work/{work_id}/prepared",
        "/api/v1/source/provider-work/{work_id}/close",
        "/api/v1/source/provider-work/{work_id}/settle",
        "/api/v1/source/provider-work/{work_id}/execute-rerun",
    }
    assert all("/direct" not in path for path in paths)
