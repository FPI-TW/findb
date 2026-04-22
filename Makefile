.PHONY: up-db up-server up test-db down format partial-dump-validate partial-dump-run

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

partial-dump-validate:
	uv run python scripts/dev.py partial-dump-validate

partial-dump-run:
	uv run python scripts/dev.py partial-dump-run
