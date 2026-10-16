# SPDX-License-Identifier: AGPL-3.0-or-later
# The Makefile is the only entry point for build, test, and lint (spec §8).
# Python (server/, uv) and JS (web/, pnpm) toolchains stay separate.

UV   := uv --directory server
PNPM := pnpm --dir web

.DEFAULT_GOAL := help
.PHONY: help install build test test-py test-js test-e2e lint fmt fmt-check coverage-check \
        typecheck spdx imports hooks ci-py ci-js ci lint-standards sync-standards doctor

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Install Python and JS dependencies
	$(UV) sync --frozen
	$(PNPM) install --frozen-lockfile

build: ## Build web apps into the server's static directory
	$(PNPM) run build

test: test-py test-js ## Run Python and JS unit tests

test-py:
	$(UV) run pytest

test-js:
	$(PNPM) run test

test-e2e: ## Run end-to-end tests (Playwright)
	$(PNPM) run test:e2e

lint: ## Lint Python and JS
	$(UV) run ruff check . ../scripts
	$(PNPM) run lint

fmt: ## Format Python and JS
	$(UV) run ruff format . ../scripts
	$(UV) run ruff check --fix . ../scripts
	$(PNPM) run fmt

fmt-check: ## Check formatting without changing files
	$(UV) run ruff format --check . ../scripts
	$(PNPM) run fmt:check

typecheck: ## Type-check Python (mypy --strict) and TypeScript
	$(UV) run mypy
	$(PNPM) run typecheck

imports: ## Enforce layering contracts (import-linter)
	$(UV) run lint-imports

coverage-check: ## Python tests with the per-module coverage gate (>= 95% per file)
	$(UV) run pytest --cov --cov-report=term-missing --cov-report=json:.coverage.json
	python3 scripts/coverage_per_file.py server/.coverage.json

spdx: ## Check every source file has the SPDX header
	python3 scripts/check_spdx.py .

hooks: ## Install a pre-commit hook that runs fmt-check, lint, and spdx
	@hook="$$(git rev-parse --git-path hooks)/pre-commit"; \
	mkdir -p "$$(dirname "$$hook")"; \
	printf '#!/bin/sh\nexec make fmt-check lint spdx\n' > "$$hook"; \
	chmod +x "$$hook"; echo "installed $$hook"

ci-py: ## Everything CI runs for Python
	$(UV) run ruff format --check . ../scripts
	$(UV) run ruff check . ../scripts
	$(UV) run mypy
	$(UV) run lint-imports
	$(MAKE) coverage-check
	python3 scripts/check_spdx.py .

ci-js: ## Everything CI runs for JS
	$(PNPM) run fmt:check
	$(PNPM) run lint
	$(PNPM) run typecheck
	$(PNPM) run test
	$(PNPM) run build

ci: ci-py ci-js lint-standards ## Full CI pipeline

lint-standards: ## Run the coding-standards compliance linter (repo, server/, web/)
	./.standards/scripts/lint-standards.sh server
	./.standards/scripts/lint-standards.sh web

sync-standards: ## Sync coding standards to the latest version
	./.standards/scripts/sync-standards.sh

doctor: ## Audit the coding-standards setup
	./.standards/scripts/doctor.sh
