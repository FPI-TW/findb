.PHONY: up-db up-server up test-db down format lint type-check check partial-dump-validate partial-dump-run seed-upsert seed-upsert-truncate

up-db:
	uv run python scripts/dev.py up-db

up-server:
	uv run python scripts/dev.py up-server

up:
	uv run python scripts/dev.py up

test-db:
	uv run python scripts/dev.py test-db

down:
	uv run python scripts/dev.py down

format:
	uv run ruff check --fix app tests scripts migrations
	uv run black app tests scripts migrations

lint:
	uv run ruff check app tests scripts migrations
	uv run black --check app tests scripts migrations

type-check:
	uv run mypy app

check: lint type-check test-db

seed-upsert:
	uv run python scripts/dev.py seed-upsert

seed-upsert-truncate:
	uv run python scripts/dev.py seed-upsert --truncate
