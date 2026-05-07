# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
uv sync

# Local development (cross-platform dev script)
make up-db                          # start PostgreSQL via Docker
make up-server                      # start FastAPI dev server
make up                             # both in one step
# Or manually:
docker compose up -d db
uv run alembic upgrade head         # apply migrations
uv run uvicorn app.main:app --reload

# Docker full stack
docker compose up -d --build
docker compose exec app python /app/scripts/seed_data.py
docker compose down

# Seed local DB from partial dump
make seed-upsert                    # upsert mode (safe)
make seed-upsert-truncate           # truncate + reload

# Code quality
uv run black app tests scripts
uv run ruff check .
uv run mypy app

# Tests
uv run pytest                                                          # all tests
uv run pytest tests/test_source_api.py                                # single file
uv run pytest tests/test_source_api.py::TestSourceAPI::test_ingest_without_api_key  # single test
uv run pytest -k "crypto"                                              # by keyword
uv run pytest --cov=app                                                # with coverage

# Run tests via dev script (creates findb_test DB automatically)
uv run python scripts/dev.py test-db

# Alembic migrations
uv run alembic current              # show current revision
uv run alembic upgrade head         # apply all pending migrations
uv run alembic revision --autogenerate -m "describe change"  # generate migration
uv run alembic downgrade -1         # rollback one step
```

## Architecture

FinDB is a three-layer financial data pipeline: **Fetch → Normalize → Serve**.

- **Fetch Layer** (external, not in this repo): pulls raw data from providers (Bloomberg, etc.) and POSTs to Source API
- **Source API** (`app/api/v1/source.py`): authenticated ingest endpoint — validates, deduplicates, writes to Raw layer, triggers Normalize
- **Normalize Layer** (`app/services/normalize/`): maps raw payloads to canonical models, runs DQ checks, upserts to Canonical layer
- **Serve API** (`app/api/v1/serve.py`): read-only query endpoints over the Canonical layer

### Data Layers

| Layer     | Storage                                                                                                             | Retention |
| --------- | ------------------------------------------------------------------------------------------------------------------- | --------- |
| Raw       | `raw.market_payload` (jsonb)                                                                                        | 14 days   |
| Canonical | `instruments`, `market_data_eod`, `corporate_action`, `macro_series/observation`, `futures_contract/continuous_eod` | Long-term |
| Registry  | `dataset_registry`, `ingestion_run`, `dq_issue`                                                                     | Long-term |

### Request Flow

1. Source API receives raw payload → deduplicates by `idempotency_key` → writes `raw.market_payload` → creates `ingestion_run`
2. `ingestion.py` routes to the appropriate normalizer via `NORMALIZER_MAP[dataset_key]`
3. Normalizer maps fields → runs DQ checks → upserts canonical rows → updates `ingestion_run`
4. Serve API reads only canonical tables

### Key Files

| File                                   | Purpose                                                                                         |
| -------------------------------------- | ----------------------------------------------------------------------------------------------- |
| `app/main.py`                          | FastAPI app, lifespan DB init, router mounts, health endpoint                                   |
| `app/config.py`                        | All settings from env via `get_settings()`                                                      |
| `app/api/deps.py`                      | Auth dependencies: `verify_source_api_key`, `verify_serve_api_key`, IP allowlist, rate limiting |
| `app/models/base.py`                   | SQLAlchemy Base, engine, session factory, Alembic version check in `init_db()`                  |
| `app/services/ingestion.py`            | `NORMALIZER_MAP` routing, run lifecycle, rerun support                                          |
| `app/services/normalize/base.py`       | `BaseNormalizer` — all normalizers subclass this                                                |
| `app/services/dq/validators.py`        | DQ rules; `severity="error"` blocks writes, `"warning"` does not                                |
| `app/models/canonical.py`              | All canonical ORM models                                                                        |
| `app/models/registry.py`               | `DatasetRegistry`, `IngestionRun`, `DQIssue`                                                    |
| `app/models/raw.py`                    | `raw.market_payload`                                                                            |
| `alembic.ini` + `migrations/`          | Alembic configuration and migration scripts                                                     |
| `scripts/dev.py`                       | Cross-platform dev commands (up-db, up-server, test-db, seed-upsert)                            |
| `scripts/seed_upsert.py`               | Load partial dump data into local DB                                                            |
| `scripts/partial_dump.py`              | Export partial data from remote DB for local dev                                                |
| `scripts/generate_instrument_cache.py` | Generate static instrument/macro lookup cache                                                   |
| `Makefile`                             | Shortcut targets wrapping `scripts/dev.py`                                                      |
| `app/static/test_page.html`            | Static `/test` API tester; only modify when explicitly requested                                |
| `app/static/instrument-lookup.html`    | Static `/instrument-lookup` page                                                                |
| `tests/conftest.py`                    | Async fixtures, DB override via dependency injection                                            |

## Conventions

- **Timestamps**: always UTC-aware; use `app.utils.utc_now()` and `ensure_utc()`. `DateTime(timezone=True)` on all ORM columns.
- **Primary keys**: UUIDv7 via `app.utils.uuid7()` — do not use UUIDv4.
- **DB access**: async only (`AsyncSession`, `await`, explicit `commit()`). Use `select()` for ORM; wrap raw SQL in `text()`.
- **Pydantic v2**: use `from_attributes=True` where ORM hydration is needed.
- **Router handlers**: keep thin — business logic belongs in services.
- **Config/secrets**: always from `app.config.Settings` via env — never hardcode.
- **Source provider names**: normalize to stable lowercase (e.g., `bloomberg`).
- **Schema changes**: all DDL through Alembic migrations only — runtime `init_db()` validates Alembic state, never runs `create_all()`.
- **Dependencies**: managed by `uv` (`pyproject.toml` + `uv.lock`). Python 3.13 required.
- **Pre-commit hooks**: Black formatting on commit, pytest on push (`.pre-commit-config.yaml`).
- **Frontend edits**: default to `frontend/` for UI/page changes; do not modify `app/static/test_page.html` unless explicitly requested.

## Adding a New Normalizer

1. Create `app/services/normalize/{market}.py` subclassing `BaseNormalizer`; implement `map_fields()`; set `dataset_key`, `asset_class`, `market` as class attributes
2. Export from `app/services/normalize/__init__.py`
3. Register in `NORMALIZER_MAP` in `app/services/ingestion.py`
4. Add dataset definition in `scripts/seed_data.py`
5. Add tests in `tests/test_normalize.py` or a market-specific test file

## Security

- **Source API**: requires `X-API-Key` header matching `SOURCE_API_KEY` env var
- **Admin API**: requires `X-API-Key` header matching `ADMIN_API_KEY` env var
- **IP allowlist**: `SOURCE_ALLOWLIST_CIDRS` (CIDR, comma-separated). Optional when `DEBUG=true`; **required when `DEBUG=false`** (app refuses to start without it)
- **Serve API**: auth optional, controlled by `SERVE_REQUIRE_AUTH`; Serve layer must remain read-only
- **Rate limiting**: in-process per (API key + client IP); resets on restart

## Test Environment Notes

- Tests use `findb_test` DB (override via `TEST_DATABASE_URL`)
- `SOURCE_API_KEY` must be set for auth tests to pass
- `ADMIN_API_KEY` must be set for admin API tests to pass
- Tables are auto-created and torn down per test session
- Rate limit state resets between tests automatically
- Preferred test runner: `uv run python scripts/dev.py test-db` (auto-creates test DB)

## Infrastructure

| Container           | Description                            | Port        |
| ------------------- | -------------------------------------- | ----------- |
| `findb-app`         | FastAPI app                            | 8080        |
| `findb-postgres`    | PostgreSQL 16                          | 5435 → 5432 |
| `findb-raw-cleanup` | Daily raw TTL cleanup (profile: tools) | —           |
| `findb-pgadmin`     | pgAdmin UI (profile: tools)            | 5056        |

Host DB port is `5435`; app container connects to `db:5432`. Default app port is `8080`.

### Git 行為

- merge時，使用--no-ff，永遠禁止 --no-verify。
- 分支命名格式：`<type>/<scope>/<summary-kebab-case>`，例如：`feat/app/platform-command-split`、`fix/server/auth-refresh-bug`。
- commit 訊息格式：`<type>(<scope>): <summary>`，例如：`feat(app): split package scripts by platform`、`fix(app): guard renderer process access`。
- 發 PR 時，PR 標題、描述、變更摘要與測試說明使用繁體中文。
- `type` 建議使用：`feat`、`fix`、`refactor`、`docs`、`test`、`chore`、`build`、`ci`。
