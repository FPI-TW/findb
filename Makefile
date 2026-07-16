.PHONY: help up-db up-rabbit migrate seed-data up-server up-app up queue-status queue-logs queue-logs-follow test-db down format lint type-check check partial-dump-validate partial-dump-run seed-upsert seed-upsert-truncate

help:
	@echo "FinDB local development commands:"
	@echo "  make up                 Start DB, RabbitMQ, app, dispatcher, and worker"
	@echo "  make up-app             Start DB and app only (without queue workers)"
	@echo "  make up-db              Start PostgreSQL only"
	@echo "  make up-rabbit          Start RabbitMQ only"
	@echo "  make migrate            Start DB and apply Alembic migrations"
	@echo "  make seed-data          Apply migrations and seed dataset registry"
	@echo "  make queue-status       Show queue service status"
	@echo "  make queue-logs         Show recent RabbitMQ/dispatcher/worker logs"
	@echo "  make queue-logs-follow  Follow RabbitMQ/dispatcher/worker logs"
	@echo "  make down               Stop containers without deleting volumes"
	@echo "  make test-db            Run the complete DB-backed test suite"

up-db:
	uv run python scripts/dev.py up-db

up-rabbit:
	uv run python scripts/dev.py up-rabbit

migrate:
	uv run python scripts/dev.py migrate

seed-data:
	uv run python scripts/dev.py seed-data

up-server:
	uv run python scripts/dev.py up-server

up-app:
	uv run python scripts/dev.py up-app

up:
	uv run python scripts/dev.py up

queue-status:
	uv run python scripts/dev.py queue-status

queue-logs:
	uv run python scripts/dev.py queue-logs

queue-logs-follow:
	uv run python scripts/dev.py queue-logs --follow

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
