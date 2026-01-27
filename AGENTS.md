# AGENTS.md

This file guides coding agents working in this repository.
Keep it updated when commands or conventions change.

## Scope

- Backend service for Normalize + Serve layers (FastAPI + SQLAlchemy async).
- PostgreSQL is the canonical datastore.
- All timestamps are UTC and timezone-aware.
- UUID v7 is the primary identifier strategy.

## Repo Layout (high level)

- `app/` FastAPI app, models, services, schemas, utils.
- `scripts/` Utilities for seed/cleanup/init.
- `tests/` Pytest tests.
- `docker-compose.yml` Local Docker environment.
- `pyproject.toml` Poetry configuration and tooling.
- `plans/` Implementation plan and specs.

## Cursor/Copilot Rules

- No Cursor rules found (`.cursor/rules/` or `.cursorrules`).
- No Copilot rules found (`.github/copilot-instructions.md`).

## Build / Run Commands

### Local (Poetry)

1. Install dependencies

```bash
poetry install
```

2. Run API locally

```bash
poetry run uvicorn app.main:app --reload
```

3. Seed data locally

```bash
poetry run python scripts/seed_data.py
```

### Docker (recommended for integration)

1. Build and start services

```bash
docker-compose up -d --build
```

2. Seed data in container

```bash
docker-compose exec app python /app/scripts/seed_data.py
```

3. Stop services

```bash
docker-compose down
```

### Database Port

- Host port is `5435` mapped to container `5432`.
- App container uses `db:5432` internally.

## Lint / Format Commands

### Formatting (Black)

```bash
poetry run black app tests
```

### Linting (Ruff)

```bash
poetry run ruff check .
```

### Type Checks (Mypy)

```bash
poetry run mypy app
```

## Test Commands

### Run all tests

```bash
poetry run pytest
```

### Run a single file

```bash
poetry run pytest tests/test_source_api.py
```

### Run a single test

```bash
poetry run pytest tests/test_source_api.py::TestSourceAPI::test_ingest_without_api_key
```

### Run tests by keyword

```bash
poetry run pytest -k "crypto"
```

### Coverage (optional)

```bash
poetry run pytest --cov=app
```

### Test Environment Notes

- `SOURCE_API_KEYS` must be set or tests that hit auth will fail.
- Use `.env` (copied from `.env.example`) for local runs.

## Code Style Guidelines

### Formatting & Imports

- Use Black with line length 100 (see `pyproject.toml`).
- Use Ruff for linting and import ordering (select includes `I`).
- Group imports: standard library, third-party, local.
- Avoid unused imports; Ruff enforces this.

### Typing

- Use Python 3.11+ type syntax (`list[str]`, `str | None`).
- Prefer explicit types in public function signatures.
- Use `Optional[T]` only when needed for readability.

### Naming

- Modules/functions/variables: `snake_case`.
- Classes: `PascalCase`.
- Constants: `UPPER_SNAKE_CASE`.
- Database column names use snake_case to match SQL conventions.

### Date/Time Handling

- Always use UTC-aware datetimes.
- Use `app.utils.utc_now()` for timestamps.
- Use `ensure_utc()` when parsing external timestamps.
- DB columns storing datetimes should be `DateTime(timezone=True)`.

### UUIDs

- Use `app.utils.uuid7()` for IDs.
- Do not use random UUIDv4 unless explicitly required.

### FastAPI Patterns

- Use dependency injection via `app.dependencies.get_db`.
- Use `HTTPException` for API errors.
- Prefer `fastapi.status` as `http_status` to avoid name collisions.
- Keep router functions thin; use services for business logic.

### SQLAlchemy Async Patterns

- Use `AsyncSession` and `await` for all DB interactions.
- Use `select()` for queries; avoid raw SQL when possible.
- When raw SQL is needed, wrap in `text()`.
- Commit explicitly in service methods (do not rely on autocommit).

### Data Quality (DQ)

- DQ rules live in `app/services/dq/validators.py`.
- Errors (`severity="error"`) block writes; warnings do not.
- Record DQ issues in `dq_issue` when detected.

### Error Handling

- Catch specific exceptions; avoid bare `except` unless logging + rethrow.
- For API errors, return informative messages without exposing secrets.
- For ingestion, validate dataset existence before insert to avoid FK errors.

### Config & Secrets

- Load config via `app.config.Settings`.
- Do not hardcode secrets; use `.env` or env vars.
- API keys are comma-separated in `SOURCE_API_KEYS` and `SERVE_API_KEYS`.

### Serialization

- Pydantic v2 models should use `from_attributes = True` when needed.
- Response wrappers live in `app/schemas/common.py`.

### Testing Conventions

- Tests are in `tests/` and use pytest + pytest-asyncio.
- Use `httpx.AsyncClient` + `ASGITransport` for API tests.
- Prefer deterministic data and avoid reliance on external services.

## Common Operations

### Seed Data

```bash
docker-compose exec app python /app/scripts/seed_data.py
```

### Raw Cleanup Script

```bash
docker-compose exec app python /app/scripts/cleanup_raw.py
```

### Sample Ingest Payload

- `scripts/sample_ingest_payload.json` is the canonical test payload.

## When Adding New Features

- Update `dataset_registry` seed if adding new datasets.
- Add/extend Normalizers for new markets in `app/services/normalize/`.
- Extend DQ rules when introducing new fields.
- Keep Serve API read-only (no writes in Serve layer).

## Known Gaps (as of now)

- Normalize trigger from Source API is stubbed (TODO in ingestion service).
- Rate limiting and allowlist are not implemented yet.
- Alembic migrations are not set up; schema changes require rebuild.
