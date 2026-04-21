.PHONY: up-db up-server up test-db down

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
