.PHONY: up-db up-server up test-db down format lint partial-dump-validate partial-dump-run seed-upsert seed-upsert-truncate

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
	.venv/bin/ruff check --fix app tests scripts migrations
	.venv/bin/black app tests scripts

lint:
	.venv/bin/ruff check app tests scripts migrations
	.venv/bin/black --check app tests scripts

seed-upsert:
	uv run python scripts/dev.py seed-upsert

seed-upsert-truncate:
	uv run python scripts/dev.py seed-upsert --truncate
