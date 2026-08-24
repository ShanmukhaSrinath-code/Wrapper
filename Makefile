# Common Application Base -- developer entrypoints.
# Every target is safe to run from a clean checkout.

SHELL := /usr/bin/env bash
COMPOSE := docker compose -f deploy/compose/docker-compose.yml
UV := uv
PY := $(UV) run

.DEFAULT_GOAL := help

.PHONY: help install lint fmt typecheck run test test-unit test-integration test-e2e \
        up down logs ps migrate revision smoke build scan clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Create the venv and install all dependencies
	$(UV) python install 3.12
	$(UV) sync --all-groups
	@# `uv sync` can report success over a venv a previous run left broken, so
	@# verify the interpreter and key imports before claiming the install worked.
	$(PY) python scripts/verify_venv.py

lint: ## ruff check + format check
	$(PY) ruff check .
	$(PY) ruff format --check .

fmt: ## Auto-fix lint + format
	$(PY) ruff check --fix .
	$(PY) ruff format .

typecheck: ## mypy
	$(PY) mypy app

run: ## Run the API locally (reload)
	$(PY) uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload \n	  --no-server-header --proxy-headers --forwarded-allow-ips '*'

test: ## Full test suite with coverage gate
	$(PY) pytest --cov=app --cov-report=term-missing --cov-fail-under=80

test-unit: ## Unit tests only
	$(PY) pytest tests/unit

test-integration: ## Integration tests (needs `make up`)
	$(PY) pytest tests/integration -m integration

test-e2e: ## Playwright e2e (needs `make up`)
	$(PY) pytest tests/e2e -m e2e

up: ## Start the full stack
	$(COMPOSE) up -d --build

down: ## Stop the stack and remove volumes
	$(COMPOSE) down -v

logs: ## Tail stack logs
	$(COMPOSE) logs -f

ps: ## Show stack status
	$(COMPOSE) ps

migrate: ## Apply Alembic migrations
	$(PY) alembic upgrade head

revision: ## Autogenerate a migration: make revision m="add thing"
	$(PY) alembic revision --autogenerate -m "$(m)"

build: ## Build the application image
	docker build -f deploy/docker/Dockerfile -t common-app-base:local .

scan: ## Trivy dependency + image scan
	trivy fs --scanners vuln,secret,misconfig --exit-code 1 --severity HIGH,CRITICAL .
	trivy image --exit-code 1 --severity HIGH,CRITICAL common-app-base:local

smoke: ## Full-stack correlation smoke test
	$(PY) python scripts/smoke.py

clean: ## Remove caches
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
