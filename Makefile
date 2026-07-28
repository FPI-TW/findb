.PHONY: help up start restart down build migrate seed up-server up-db status logs test \
	format check check-backend dashboard-install dashboard-dev dashboard-format \
	dashboard-test dashboard-check dashboard-build dashboard-up fetcher-test fetcher-check \
	fetcher-build contracts-export contracts-check

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
	@echo "  make dashboard-install  Install dashboard dependencies"
	@echo "  make dashboard-dev      Start the dashboard development server"
	@echo "  make dashboard-up       Build and start the dashboard container"
	@echo "  make fetcher-build      Build the standalone Fetcher image"
	@echo "  make contracts-export   Regenerate published ingress contracts"
	@echo ""
	@echo "Observe and verify:"
	@echo "  make status     Show complete stack status"
	@echo "  make logs       Follow RabbitMQ, dispatcher, and worker logs"
	@echo "  make test       Run the DB-backed backend test suite"
	@echo "  make dashboard-test   Run dashboard tests"
	@echo "  make fetcher-test     Run Fetcher tests"
	@echo "  make fetcher-check    Run Fetcher quality gates"
	@echo "  make contracts-check  Fail when published contracts drift"
	@echo "  make format     Format backend and dashboard code"
	@echo "  make check      Run all backend and dashboard quality gates"

up:
	uv --directory backend run python scripts/dev.py up

start:
	uv --directory backend run python scripts/dev.py start

restart:
	uv --directory backend run python scripts/dev.py restart

build:
	uv --directory backend run python scripts/dev.py build

migrate:
	uv --directory backend run python scripts/dev.py migrate

seed:
	uv --directory backend run python scripts/dev.py seed-data

up-server:
	uv --directory backend run python scripts/dev.py up-server

up-db:
	uv --directory backend run python scripts/dev.py up-db

down:
	uv --directory backend run python scripts/dev.py down

status:
	uv --directory backend run python scripts/dev.py queue-status

logs:
	uv --directory backend run python scripts/dev.py queue-logs --follow

test:
	uv --directory backend run python scripts/dev.py test-db

format:
	pnpm format

check-backend:
	uv --directory backend run ruff check app tests scripts migrations
	uv --directory backend run ruff format --check app tests scripts migrations
	uv --directory backend run mypy app
	uv --directory backend run python scripts/export_ingress_contracts.py --check
	uv --directory backend run python scripts/dev.py test-db

dashboard-install:
	pnpm install --frozen-lockfile

dashboard-dev:
	pnpm dev:dashboard

dashboard-up:
	pnpm container:dashboard

dashboard-format:
	pnpm format:dashboard

dashboard-test:
	pnpm test:dashboard

dashboard-check:
	pnpm check:dashboard

dashboard-build:
	pnpm build:dashboard

fetcher-test:
	uv --directory fetcher run pytest

fetcher-check:
	uv --directory fetcher run ruff check .
	uv --directory fetcher run ruff format --check .
	uv --directory fetcher run mypy src
	uv --directory fetcher run pytest

fetcher-build:
	docker build -f fetcher/Dockerfile -t findb-fetcher:local .

contracts-export:
	uv --directory backend run python scripts/export_ingress_contracts.py

contracts-check:
	uv --directory backend run python scripts/export_ingress_contracts.py --check

check: check-backend dashboard-check fetcher-check dashboard-build
