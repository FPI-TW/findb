# PROJECT KNOWLEDGE BASE

Updated: 2026-04-22
Commit: 2d283e3
Branch: main

## OVERVIEW

FinDB is a FastAPI backend for ingesting market payloads, normalizing into canonical models, and serving read-only query APIs.
Core stack: Python 3.13+, FastAPI, SQLAlchemy async, PostgreSQL, uv, pytest.

## STRUCTURE

```text
findb/
|- app/                      # API, services, models, schemas, utils
|  |- api/v1/                # Source (write ingest), Serve (read query), Admin routers
|  |- services/normalize/    # Market-specific normalizers and mapping logic
|  |- models/                # Canonical, raw, registry ORM models (base.py has Alembic check)
|  |- schemas/               # Pydantic request/response models
|  `- static/                # /test and /instrument-lookup static assets + data/ cache
|- migrations/               # Alembic migration scripts
|- configs/                  # YAML configs (partial_dump.yaml)
|- docs/                     # API guide, manual test flow, migration workflow
|- tests/                    # Async API/service integration and unit tests
|- scripts/                  # dev.py, seed_upsert.py, partial_dump.py, cleanup, cache gen
|- seed/                     # Partial dump data for local development
|- plans/                    # Architecture and development plans
|- docker-compose.yml        # Local app + postgres + pgadmin stack
|- Makefile                  # Dev workflow shortcuts (wraps scripts/dev.py)
`- pyproject.toml            # uv deps + black/ruff/mypy/pytest settings (Python 3.13)
```

## WHERE TO LOOK

| Task                         | Location                                                                     | Notes                                                                  |
| ---------------------------- | ---------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| App startup/lifecycle        | `app/main.py`                                                                | Registers routers, lifespan DB init, health endpoints                  |
| Source ingest flow           | `app/api/v1/source.py`                                                       | Auth, idempotency, ingestion route dispatch                            |
| Serve query flow             | `app/api/v1/serve.py`                                                        | Read-only query endpoints and filters                                  |
| Ingestion orchestration      | `app/services/ingestion.py`                                                  | Dataset validation, run tracking, normalizer map                       |
| Market normalization         | `app/services/normalize/`                                                    | Per-market mapping, DQ checks, canonical writes                        |
| DQ rules                     | `app/services/dq/validators.py`                                              | Error blocks writes; warning does not                                  |
| ORM/data model               | `app/models/`                                                                | Raw schema + canonical tables + run registry                           |
| API schemas                  | `app/schemas/`                                                               | Request and response contracts                                         |
| Static lookup and cache flow | `app/static/instrument-lookup.html` + `scripts/generate_instrument_cache.py` | `/instrument-lookup` UI and generated `app/static/data/*.json` cache   |
| Test dashboard               | `app/static/test_page.html`                                                  | Static `/test` API tester                                              |
| Tests and fixtures           | `tests/` + `tests/conftest.py`                                               | AsyncClient + ASGITransport + DB fixtures                              |
| Alembic migrations           | `alembic.ini` + `migrations/`                                                | Schema-as-code via Alembic; `init_db()` validates revision at startup  |
| Dev workflow                 | `scripts/dev.py` + `Makefile`                                                | Cross-platform local commands (up-db, up-server, test-db, seed-upsert) |
| Partial dump tooling         | `scripts/partial_dump.py` + `configs/partial_dump.yaml`                      | Export/import partial prod data for local dev                          |
| Seed upsert                  | `scripts/seed_upsert.py`                                                     | Load partial dump CSVs into local DB (upsert or truncate mode)         |
| Deploy workflow              | `.github/workflows/deploy.yml`                                               | CI/CD via GitHub Actions to EC2                                        |

## CODE MAP

LSP symbol map is unavailable in this environment (basedpyright not installed).
Use directory-local AGENTS files for deep module guidance:

- `app/services/normalize/AGENTS.md`
- `app/api/v1/AGENTS.md`
- `app/models/AGENTS.md`

## CONVENTIONS

- Timestamps are UTC-aware; use `app.utils.utc_now()` and `ensure_utc()`.
- UUID strategy is v7 via `app.utils.uuid7()`.
- DB access is async only (`AsyncSession`, `await`, explicit `commit()`).
- Use `select()` for ORM queries; wrap raw SQL in `text()` when required.
- Keep router handlers thin; business logic belongs in services.
- Source API is write-path ingest; Serve API is read-only query path.
- Pydantic v2 models use `from_attributes=True` where ORM hydration is needed.
- Secrets/config come from env via `app.config.Settings`; do not hardcode keys.

## ANTI-PATTERNS (THIS PROJECT)

- Writing via Serve endpoints (Serve layer must remain read-only).
- Using naive datetimes or non-UTC timestamps.
- Generating UUIDv4 for primary identifiers without explicit requirement.
- Skipping dataset existence checks before ingestion writes.
- Treating DQ `severity="error"` as non-blocking.
- Mixing request-layer concerns and normalization/database business logic.

## UNIQUE STYLES

- Raw payload persistence is separated into PostgreSQL schema `raw`.
- Normalizer routing uses an explicit `NORMALIZER_MAP` in `app/services/ingestion.py`.
- Direct-format source ingest endpoints exist for selected markets (`.../direct`) and auto-bootstrap built-in dataset registry rows when missing.
- Macro direct payloads default `market=MACRO` when the payload omits market.
- UI surfaces are static pages under `app/static/` and are served by `app/main.py` (`/test`, `/instrument-lookup`, and `/static/*`).
- Instrument and macro lookup data are generated cache files (`app/static/data/instruments.json`, `app/static/data/macro-series.json`), not committed source-of-truth data.
- Test suite heavily uses async fixtures and dependency overrides (`tests/conftest.py`).

## COMMANDS

```bash
# local
uv sync
uv run uvicorn app.main:app --reload

# docker
docker-compose up -d --build
docker-compose exec app python /app/scripts/seed_data.py
docker-compose down

# quality + tests
uv run black app tests
uv run ruff check .
uv run mypy app
uv run pytest
```

## NOTES

- Host DB port is `5435` -> container `5432`; app container uses `db:5432`. Default app port: `8080`.
- `SOURCE_API_KEYS` must be set for auth-covered tests and ingest endpoints.
- `ADMIN_API_KEYS` must be set for Admin API endpoints and admin-related tests.
- Schema evolution is managed by Alembic. Runtime `init_db()` validates Alembic revision and required tables — it does NOT run `create_all()`.
- All schema changes must go through Alembic revisions (`uv run alembic revision --autogenerate`).
- Pre-commit hooks: Black formatting on commit, pytest on push.
- Keep this file high-level; put domain specifics in nearest subdirectory AGENTS.

### Git 行為

- 禁止在本地Merge main，需提PR。
- 分支命名格式：`<type>/<summary-kebab-case>`，例如：`feat/platform-command-split`、`fix/auth-refresh-bug`。
- commit 訊息格式：`<type>: <summary>`，例如：`feat: split package scripts by platform`、`fix: guard renderer process access`。
- 發 PR 時，PR 標題、描述、變更摘要與測試說明使用繁體中文。
- `type` 建議使用：`feat`、`fix`、`refactor`、`docs`、`test`、`chore`、`build`、`ci`。
