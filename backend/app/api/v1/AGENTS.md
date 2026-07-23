# API V1 KNOWLEDGE BASE

Apply root `AGENTS.md` first. This file adds rules only for `app/api/v1/`.

## OVERVIEW

HTTP boundary for Source ingest (write path), Serve query (read path), and Admin operations.

## WHERE TO LOOK

| Task | Location | Notes |
|------|----------|-------|
| Router registration context | `app/main.py` | Mounts v1 routers with `/api/v1/*` prefixes |
| Source ingest endpoints | `source.py` | Auth, idempotency, run creation/rerun |
| Serve read endpoints | `serve.py` | Query filters, pagination, response shaping |
| Admin endpoints | `admin.py` | DQ issue workflow, EOD patch, raw payload lookup, bulk rerun, instrument cache |
| API auth dependencies | `app/api/deps.py` | `verify_source_api_key`, `verify_serve_api_key`, `verify_admin_api_key` |
| DB dependency | `app/dependencies.py` | Async session injection |

## CONVENTIONS (LOCAL)

- Keep handlers thin; delegate business logic to services.
- Source routes are ingest/rerun write-paths; Serve routes are read-only; Admin routes are authenticated correction/maintenance paths.
- Use dependency-injected `AsyncSession`; do not create ad hoc engines in handlers.
- Return typed schema responses from `app/schemas/source.py`, `app/schemas/serve.py`, and `app/schemas/admin.py`.
- Preserve idempotency behavior for ingest requests and direct-format endpoints.
- Keep market-specific validation near route boundary before service invocation.

## ANTI-PATTERNS (LOCAL)

- Adding write methods to `serve.py`.
- Implementing heavy normalization logic directly in route handlers.
- Returning raw ORM objects without schema shaping.
- Leaking secrets or internal traces in API error payloads.
- Bypassing API key dependencies for protected source endpoints.
- Bypassing Admin API key checks for correction or maintenance endpoints.

## CHANGE CHECKLIST

- Update endpoint tests in `tests/test_source_api.py`, `tests/test_serve_api.py`, or `tests/test_admin_api.py`.
- Validate auth behavior (`401`/`403`) and market mismatch errors.
- Verify response fields match Pydantic schemas and pagination wrappers.
- Run end-to-end path (`tests/test_end_to_end.py`) if ingest behavior changes.
