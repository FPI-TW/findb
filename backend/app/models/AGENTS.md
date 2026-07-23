# MODELS KNOWLEDGE BASE

Apply root `AGENTS.md` first. This file adds rules only for `app/models/`.

## OVERVIEW

ORM layer defining canonical market entities, ingestion registry tracking, and raw payload storage.

## WHERE TO LOOK

| Task | Location | Notes |
|------|----------|-------|
| Engine/session primitives | `base.py` | Async engine, sessionmaker, schema init |
| Canonical tables | `canonical.py` | Instruments, EOD, corporate actions, macro, futures |
| Ingestion registry tables | `registry.py` | Dataset registry, runs, DQ issues |
| Raw payload table | `raw.py` | `raw.market_payload` short-retention storage |

## CONVENTIONS (LOCAL)

- Datetime columns must use `DateTime(timezone=True)` for UTC-aware values.
- Primary identifiers use UUIDv7 defaults via `uuid7()` where applicable.
- Use explicit `Index` or `UniqueConstraint` for business keys and query paths.
- Keep run-tracking fields (`status`, counts, timestamps, error message) consistent in `IngestionRun`.
- Preserve `raw` schema isolation for raw payload persistence.
- Keep relationship names aligned with existing `back_populates` patterns.

## ANTI-PATTERNS (LOCAL)

- Mixing registry semantics into canonical entity tables.
- Removing uniqueness constraints that enforce idempotent ingest behavior.
- Using naive datetime defaults or non-UTC timestamps.
- Introducing UUIDv4 defaults without explicit reason.
- Breaking relationship and `back_populates` symmetry across linked models.

## CHANGE CHECKLIST

- Update model imports in `app/models/__init__.py` if new models are added.
- Ensure seed and test fixtures are updated for new required fields.
- Verify schema init still succeeds (`init_db`) and tests cover new constraints.
- If schema changes are substantial, document migration implications in root notes.
