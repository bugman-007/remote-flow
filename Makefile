# Remote Flow — developer entry points (OPS-1/OPS-3/OPS-7).
SHELL := /bin/bash
BACKEND := backend
FRONTEND := frontend
PY := $(BACKEND)/.venv/bin/python

.PHONY: help setup dev dev-api dev-web dev-workers test test-backend test-frontend lint migrate seed chaos build up down logs bench load-test backup
.DEFAULT_GOAL := help

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: ## Install backend and frontend dependencies
	python3 -m venv $(BACKEND)/.venv
	$(BACKEND)/.venv/bin/pip install -e "$(BACKEND)[dev,llm]"
	cd $(FRONTEND) && npm install

migrate: ## Apply database migrations
	cd $(BACKEND) && .venv/bin/python manage.py migrate

seed: ## Seed demo users, provider, theme and template
	cd $(BACKEND) && .venv/bin/python manage.py seed

dev: ## Run API + inline pipeline driver (no Celery) against SQLite
	cd $(BACKEND) && AUTO_CREATE_SCHEMA=true SEED_ON_START=true INLINE_PIPELINE=true .venv/bin/uvicorn app.main:app --reload --port 8000

dev-web: ## Run the Vite dev server (proxies /api to :8000)
	cd $(FRONTEND) && npm run dev

dev-workers: ## Run the three Celery queues against local Redis (PIPE-3)
	cd $(BACKEND) && .venv/bin/celery -A app.workers.celery_app.celery_app worker -Q llm -c 4 -P gevent -n llm@%h & \
	cd $(BACKEND) && .venv/bin/celery -A app.workers.celery_app.celery_app worker -Q render -c 1 -n render@%h & \
	cd $(BACKEND) && .venv/bin/celery -A app.workers.celery_app.celery_app worker -Q ops -c 2 -P gevent -B -n ops@%h

test: test-backend test-frontend ## Run every test suite

test-backend: ## pytest
	cd $(BACKEND) && .venv/bin/python -m pytest -q

test-frontend: ## typecheck + build the SPA
	cd $(FRONTEND) && npm run build

lint: ## Typecheck both sides
	cd $(FRONTEND) && npm run typecheck
	$(BACKEND)/.venv/bin/python -m compileall -q $(BACKEND)/app $(BACKEND)/manage.py

chaos: ## OPS-7: 50 JDs in a burst with LLM+render failures and random worker kills
	cd $(BACKEND) && .venv/bin/python manage.py chaos-run

bench: ## HW-7: record render and intake benchmarks into docs/benchmarks.md
	cd $(BACKEND) && .venv/bin/python manage.py bench-render --n 50
	cd $(BACKEND) && .venv/bin/python manage.py bench-intake --n 200

load-test: ## M4 load test: 200 concurrent POST /jobs against a running API (NFR-1/2)
	cd $(BACKEND) && .venv/bin/python manage.py load-test --n 200 --concurrency 25 --wait 900

backup: ## Dump the database and storage via the operator CLI
	cd $(BACKEND) && .venv/bin/python manage.py backup

build: ## Build the production images
	docker compose --env-file deploy/.env -f deploy/docker-compose.yml build

up: ## Start the production stack
	docker compose --env-file deploy/.env -f deploy/docker-compose.yml up -d

down: ## Stop the production stack
	docker compose --env-file deploy/.env -f deploy/docker-compose.yml down

logs: ## Tail the stack logs
	docker compose --env-file deploy/.env -f deploy/docker-compose.yml logs -f --tail 100
