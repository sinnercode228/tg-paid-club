.PHONY: help install lint format test test-postgres check run web simulate demo up down

VENV ?= .venv
BIN := $(VENV)/bin
PG_TEST_URL ?= postgresql+asyncpg://paidclub:paidclub@localhost:5432/paidclub_test

help:             ## list the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/'

install:          ## create .venv and install the package with the dev tools
	python3 -m venv $(VENV)
	$(BIN)/pip install -e ".[dev]"

lint:             ## ruff (lint + format check) and mypy
	$(BIN)/ruff check src tests
	$(BIN)/ruff format --check src tests
	$(BIN)/mypy

format:           ## apply ruff formatting and safe fixes
	$(BIN)/ruff format src tests
	$(BIN)/ruff check --fix src tests

test:             ## the test suite on SQLite
	$(BIN)/pytest

test-postgres:    ## the test suite on PostgreSQL (PG_TEST_URL, every table in it is dropped)
	PAIDCLUB_TEST_DATABASE_URL=$(PG_TEST_URL) $(BIN)/pytest

check:            ## validate the settings in .env and the environment
	$(BIN)/paidclub check

run:              ## bot + web app + scheduler + outbox in one process (needs .env)
	$(BIN)/paidclub run

web:              ## only the web app: dashboard, checkout page, payment webhooks
	$(BIN)/paidclub web

simulate:         ## fill demo.db with 120 days of simulated club activity
	$(BIN)/paidclub simulate --db demo.db --reset

demo: simulate    ## simulate, then serve the dashboard on http://127.0.0.1:8000/admin (admin / demo)
	PAIDCLUB_DATABASE_URL=sqlite+aiosqlite:///demo.db PAIDCLUB_ADMIN_TOKEN=demo $(BIN)/paidclub web

up:               ## the app and PostgreSQL in Docker (reads .env)
	docker compose up --build

down:             ## stop the Docker services (the database volume is kept)
	docker compose down
