.PHONY: setup test lint format typecheck audit serve ingest db-check docker-build check

PYTHON ?= python3
MANIFEST ?= demo/cedar-and-salt/restaurant.json
ASSET_BASE_URL ?= http://localhost:5173/demo/cedar-and-salt

setup:
	$(PYTHON) -m pip install --requirement requirements-dev.txt
	$(PYTHON) -m pre_commit install

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff format --check .
	$(PYTHON) -m ruff check .

format:
	$(PYTHON) -m ruff format .
	$(PYTHON) -m ruff check --fix .

typecheck:
	$(PYTHON) -m mypy

audit:
	$(PYTHON) -m pip_audit --requirement requirements.txt

serve:
	$(PYTHON) -m uvicorn mbd_api.app:app --host 0.0.0.0 --port 8000 --workers 1

ingest:
	$(PYTHON) -m mbd_api.ingest --manifest $(MANIFEST) --asset-base-url $(ASSET_BASE_URL)

db-check:
	psql "$(DATABASE_URL)" -v ON_ERROR_STOP=1 -f supabase/migrations/20260907000000_initial_schema.sql
	psql "$(DATABASE_URL)" -v ON_ERROR_STOP=1 -f supabase/seed.sql

docker-build:
	docker build --tag mintgen-api:local .

check: lint typecheck test audit docker-build
