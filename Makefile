.PHONY: up-db up-server up test-db down format partial-dump-validate partial-dump-run seed-upsert seed-upsert-truncate

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
	uv run black app tests scripts

seed-upsert:
	uv run python scripts/dev.py seed-upsert

seed-upsert-truncate:
	uv run python scripts/dev.py seed-upsert --truncate
