.PHONY: help up start restart down build migrate seed up-server up-db status logs test format check

help:
	@echo "FinDB local workflow"
	@echo ""
	@echo "Stack lifecycle:"
	@echo "  make up         Migrate, seed, build, and start the complete stack"
	@echo "  make start      Start all containers from existing images after Docker restarts"
	@echo "  make restart    Stop and restart all core containers without rebuilding"
	@echo "  make down       Stop and remove containers without deleting data volumes"
	@echo ""
	@echo "Development iteration:"
	@echo "  make build      Build app, dispatcher, and worker images"
	@echo "  make migrate    Apply all pending Alembic migrations"
	@echo "  make seed       Migrate and seed the local dataset registry"
	@echo "  make up-server  Start FastAPI on the host with reload"
	@echo "  make up-db      Start PostgreSQL only"
	@echo ""
	@echo "Observe and verify:"
	@echo "  make status     Show complete stack status"
	@echo "  make logs       Follow RabbitMQ, dispatcher, and worker logs"
	@echo "  make test       Run the DB-backed test suite"
	@echo "  make format     Apply Ruff and Black formatting"
	@echo "  make check      Run lint, formatting, mypy, and tests"

up:
	uv run python scripts/dev.py up

start:
	uv run python scripts/dev.py start

restart:
	uv run python scripts/dev.py restart

build:
	uv run python scripts/dev.py build

migrate:
	uv run python scripts/dev.py migrate

seed:
	uv run python scripts/dev.py seed-data

up-server:
	uv run python scripts/dev.py up-server

up-db:
	uv run python scripts/dev.py up-db

down:
	uv run python scripts/dev.py down

status:
	uv run python scripts/dev.py queue-status

logs:
	uv run python scripts/dev.py queue-logs --follow

test:
	uv run python scripts/dev.py test-db

format:
	uv run ruff check --fix app tests scripts migrations
	uv run black app tests scripts migrations

check:
	uv run ruff check app tests scripts migrations
	uv run black --check app tests scripts migrations
	uv run mypy app
	uv run python scripts/dev.py test-db
