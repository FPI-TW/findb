"""Deterministic JSON Schema publication for archive-release contracts."""

from __future__ import annotations

from typing import Any

from app.schemas.archive import MarketMinuteArchiveManifest


def get_archive_contract_json_schema() -> dict[str, Any]:
    """Return the published release-manifest schema and semantic rules."""
    schema = MarketMinuteArchiveManifest.model_json_schema(mode="validation")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:findb:archive-contract:market_minute_archive:v1",
        "x-findb-contract": {"schema_id": "market_minute_archive", "schema_version": 1},
        "x-findb-contract-scope": {
            "artifact_kind": "immutable_archive_release_manifest",
            "runtime_ingest_support": "not_implemented",
            "finalization": "publish objects first, then atomically publish this finalized manifest",
        },
        "x-findb-semantic-rules": [
            {
                "id": "archive.coverage.continuous_months",
                "description": "covered_months is continuous and equals declared coverage",
            },
            {
                "id": "archive.trading_calendar.strict_no_gap_barrier",
                "description": "calendar revision checksum pins ascending expected dates that exactly equal covered dates across every covered month",
            },
            {
                "id": "archive.source.direct_daily_precedence",
                "description": "archive source is tw_recorder_archive with Shioaji upstream and direct daily Shioaji overlap precedence",
            },
            {
                "id": "archive.chunks.contiguous_sequences",
                "description": "chunk sequences are contiguous and count-matched",
            },
            {
                "id": "archive.chunks.max_rows",
                "description": "each chunk contains at most 5,000 rows",
            },
            {
                "id": "archive.checksums.instrument_and_object",
                "description": "instrument and object checksums are immutable release evidence",
            },
            {
                "id": "archive.finalization.atomic_publication",
                "description": "only finalized manifests are publishable",
            },
        ],
        **schema,
    }
