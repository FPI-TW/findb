# Migration Workflow (Alembic)

## Goals

- Use Alembic as the source of truth for schema evolution.
- Support both empty databases and existing databases with live data.

## Local commands

```bash
# show current revision
uv run alembic current

# create a migration from model changes
uv run alembic revision --autogenerate -m "describe change"

# upgrade to latest
uv run alembic upgrade head

# downgrade one step
uv run alembic downgrade -1
```

## Existing remote database baseline strategy

For an environment that already has data/schema:

1. Back up the database first.
2. Create and review a baseline revision in Git.
3. Stamp the existing database to that baseline revision without replaying DDL:

```bash
uv run alembic stamp <baseline_revision>
```

4. From that point onward, apply incremental migrations with `upgrade head`.

## Developer local mismatch strategy

- If local data can be discarded: recreate local DB and run `uv run alembic upgrade head`.
- If local data must be kept: backup first, manually align schema, then `stamp` to baseline and continue with incremental migrations.

## Notes for this repository

- Runtime startup no longer performs `create_all()`.
- Application startup now checks:
  - database has Alembic revision stamp
  - current revision equals Alembic `head`
  - required core relations exist (`public.dataset_registry`, `public.instruments`, `raw.market_payload`)
- If check fails, app exits and you must run `uv run alembic upgrade head` (or stamp baseline first, then upgrade).
- All schema changes must be introduced through Alembic revisions only.
