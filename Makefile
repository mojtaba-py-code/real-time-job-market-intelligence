# Development shortcuts. Everything here also works as a plain command; the
# Makefile just spares you remembering the flags.

PYTHON ?= python
PYTEST ?= $(PYTHON) -m pytest
RUFF   ?= $(PYTHON) -m ruff
SOURCES = app tests benchmarks

.DEFAULT_GOAL := help
.PHONY: help install dev-install format lint typecheck test test-unit test-integration \
        test-security test-e2e perf coverage check bootstrap ingest report serve worker \
        migrate benchmark docker-build docker-up docker-down audit clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #
install: ## Install the package
	$(PYTHON) -m pip install -e .

dev-install: ## Install with development and optional extras
	$(PYTHON) -m pip install -e ".[dev,postgres,redis]"

# --------------------------------------------------------------------------- #
# Quality
# --------------------------------------------------------------------------- #
format: ## Format the code
	$(RUFF) format $(SOURCES)
	$(RUFF) check --fix $(SOURCES)

lint: ## Lint and check formatting
	$(RUFF) check $(SOURCES)
	$(RUFF) format --check $(SOURCES)

typecheck: ## Static type check
	$(PYTHON) -m mypy app

audit: ## Security scan of the code and the dependency tree
	$(PYTHON) -m bandit -c pyproject.toml -r app -ll
	$(PYTHON) -m pip_audit

check: lint typecheck test ## Everything CI runs

# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
test: ## Run the whole suite
	$(PYTEST)

test-unit: ## Unit tests only
	$(PYTEST) tests/unit -q

test-integration: ## Integration tests only
	$(PYTEST) tests/integration -q

test-e2e: ## End-to-end tests only
	$(PYTEST) tests/e2e -q

test-security: ## Security regression tests
	$(PYTEST) tests/security -q

perf: ## Performance tests (skipped by default)
	JOBINTEL_RUN_PERF=1 $(PYTEST) tests/performance -q -s

coverage: ## Test suite with a coverage report
	$(PYTEST) --cov=app --cov-report=term-missing --cov-report=html

benchmark: ## Throughput and latency benchmarks
	$(PYTHON) benchmarks/benchmark.py --scale 1000 10000

# --------------------------------------------------------------------------- #
# Running the platform
# --------------------------------------------------------------------------- #
migrate: ## Apply database migrations
	$(PYTHON) -m alembic upgrade head

bootstrap: migrate ## Migrate, load the taxonomy and ingest a first batch
	$(PYTHON) -m app taxonomy --sync
	$(PYTHON) -m app ingest --source synthetic --limit 2000
	$(PYTHON) -m app process

ingest: ## Run every enabled source once
	$(PYTHON) -m app ingest

report: ## Print the market report
	$(PYTHON) -m app report --window-days 90

serve: ## Run the API with auto-reload
	$(PYTHON) -m app serve --reload

worker: ## Run the scheduler and event worker
	$(PYTHON) -m app worker

# --------------------------------------------------------------------------- #
# Containers
# --------------------------------------------------------------------------- #
docker-build: ## Build the image
	docker build -t jobintel:local .

docker-up: ## Start the full stack
	docker compose up --build

docker-down: ## Stop the stack and remove volumes
	docker compose down -v

# --------------------------------------------------------------------------- #
# Housekeeping
# --------------------------------------------------------------------------- #
clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage coverage.xml build dist
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name "*.egg-info" -prune -exec rm -rf {} +
