# SPDX-License-Identifier: AGPL-3.0-or-later
# The Makefile is the only entry point for build, test, and lint (spec §8).
# Python (server/, uv) and JS (web/, pnpm) toolchains stay separate.

UV   := uv --directory server
PNPM := cd web && pnpm  # run inside web/ so Corepack reads its packageManager pin

.DEFAULT_GOAL := help
.PHONY: help install build test test-py test-js test-e2e lint fmt fmt-check coverage-check \
        typecheck spdx imports audit audit-py audit-js hooks ci-py ci-js ci lint-standards sync-standards doctor \
        gen-client e2e-browsers e2e-server docs icons lock

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Install Python and JS dependencies
	$(UV) sync --frozen
	$(PNPM) install --frozen-lockfile

lock: ## Refresh server/uv.lock (the release workflow runs this after a version bump)
	$(UV) lock

build: ## Build web apps into the server's static directory
	$(PNPM) run build

gen-client: ## Regenerate the typed web client from the server's OpenAPI document
	home="$$(mktemp -d)"; JESINIA_HOME="$$home" $(UV) run jesinia openapi > web/packages/client/openapi.json; rm -rf "$$home"
	$(PNPM) --filter @jesinia/client gen

test: test-py test-js ## Run Python and JS unit tests

test-py:
	$(UV) run pytest

test-js:
	$(PNPM) run test

icons: ## Export app icons from docs/design/icons into the apps (needs e2e-browsers)
	$(PNPM) exec node scripts/export-icons.mjs

e2e-browsers: ## Install the Playwright browser the e2e tests use (CI)
	$(PNPM) exec playwright install --with-deps chromium

test-e2e: ## Run end-to-end tests (Playwright)
	$(PNPM) run test:e2e

# Playwright's webServer runs this, so the JS toolchain never starts Python itself.
e2e-server: ## Build the apps and serve a throwaway e2e instance (needs E2E_HOME, E2E_PORT, E2E_FEEDS_PORT)
	@test -n "$(E2E_HOME)" && test -n "$(E2E_PORT)" && test -n "$(E2E_FEEDS_PORT)" || { echo "set E2E_HOME, E2E_PORT and E2E_FEEDS_PORT"; exit 2; }
	$(PNPM) --filter @jesinia/launcher --filter @jesinia/notes --filter @jesinia/rss --filter @jesinia/review --filter @jesinia/issues \
	  --filter @jesinia/design-demo build
	bash web/tests/e2e/fixture.sh
	JESINIA_HOME="$(E2E_HOME)/data" $(UV) run jesinia config set server.port "$(E2E_PORT)" >/dev/null
	JESINIA_HOME="$(E2E_HOME)/data" $(UV) run jesinia config set kbsync.quiet 1s >/dev/null
	@# The fixture feeds, on a second loopback port, and a fake GitHub on an ephemeral one
	@# (its port lands in $(E2E_HOME)/github.port). Only this throwaway server may fetch
	@# loopback or be pointed at the fake GitHub (fake_runner_app's temporary-home guard): the
	@# real `gh` config and credentials are never read. Both stop with the server.
	@# Then the server plus a scripted "fake" runner that calls kb_write through MCP (tests
	@# only). It commits to the fixture KB; CI runners have no git identity of their own.
	@# Its errors (a port in use, say) reach Playwright's log; uvicorn starts once it answers.
	$(UV) run python -m http.server "$(E2E_FEEDS_PORT)" --bind 127.0.0.1 \
	  --directory "$(CURDIR)/web/tests/e2e/fixtures/feeds" >/dev/null & feeds=$$!; \
	$(UV) run python "$(CURDIR)/web/tests/e2e/fixtures/github/server.py" \
	  --repo "$(E2E_HOME)/github-repos/e2e-dev/greeter.git" \
	  --port-file "$(E2E_HOME)/github.port" >/dev/null & github=$$!; \
	trap 'kill $$feeds $$github 2>/dev/null' EXIT; \
	curl -fsS --retry 30 --retry-connrefused --retry-delay 1 -o /dev/null \
	  "http://127.0.0.1:$(E2E_FEEDS_PORT)/fixture.xml" || exit 1; \
	for attempt in $$(seq 1 60); do test -s "$(E2E_HOME)/github.port" && break; sleep 0.5; done; \
	test -s "$(E2E_HOME)/github.port" || { echo "the fixture GitHub did not start"; exit 1; }; \
	GIT_AUTHOR_NAME="Jesinia E2E" GIT_AUTHOR_EMAIL="e2e@example.invalid" \
	  GIT_COMMITTER_NAME="Jesinia E2E" GIT_COMMITTER_EMAIL="e2e@example.invalid" \
	  JESINIA_HOME="$(E2E_HOME)/data" JESINIA_FAKE_RUNNER=1 \
	  JESINIA_FAKE_GITHUB_URL="http://127.0.0.1:$$(cat "$(E2E_HOME)/github.port")" \
	  JESINIA_FAKE_GITHUB_REPOS="$(E2E_HOME)/github-repos" $(UV) run uvicorn \
	  jesinia.testing.fake_runner_app:app --host 127.0.0.1 --port "$(E2E_PORT)"

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

docs: ## Check doc links, ADR sections, and the exact KB-format rules
	python3 scripts/check_docs.py .

audit: audit-py audit-js ## Scan Python and JS dependencies for known vulnerabilities

audit-py: # audits the exact locked set exported from uv.lock
	$(UV) export --frozen --no-emit-project --format requirements-txt --quiet -o .audit-requirements.txt
	$(UV) run pip-audit --strict --progress-spinner off --disable-pip --no-deps -r .audit-requirements.txt

audit-js:
	$(PNPM) audit --audit-level low

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
	python3 scripts/check_docs.py .
	$(MAKE) audit-py

ci-js: ## Everything CI runs for JS
	$(PNPM) run fmt:check
	$(PNPM) run lint
	$(PNPM) run typecheck
	$(PNPM) run test
	$(PNPM) run build
	$(MAKE) audit-js

ci: ci-py ci-js lint-standards ## Full CI pipeline

lint-standards: ## Run the coding-standards compliance linter (repo, server/, web/)
	./.standards/scripts/lint-standards.sh server
	./.standards/scripts/lint-standards.sh web

sync-standards: ## Sync coding standards to the latest version
	./.standards/scripts/sync-standards.sh

doctor: ## Audit the coding-standards setup
	./.standards/scripts/doctor.sh
