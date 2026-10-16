<!-- Assembled by coding-standards setup.sh — do not edit above the PROJECT-SPECIFIC marker -->

# Jesinia — Claude Code Guide

## Project Overview

Jesinia is a personal knowledge base plus a small suite of local apps that feed it. A Python (FastAPI, uv) server under a per-user LaunchAgent serves React PWAs at `http://127.0.0.1:4870/`, mediates between the user's local AI agents (Claude Code, Codex) and shared services (KB, search, KB git sync, agents, MCP), and treats Markdown files on disk as the source of truth. License: AGPL-3.0-or-later.

## Key Commands

```bash
make install      # uv sync + pnpm install
make ci           # everything CI runs (ci-py + ci-js + lint-standards)
make test         # Python and JS unit tests
make fmt          # format everything
make lint         # lint everything
```

## Conventions

- **Commits:** Conventional Commits format: `type(scope): subject`
  - Types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `perf`, `ci`, `build`
- **Worktrees:** Use `git worktree add .claude/worktrees/<branch> -b <branch>` for isolated work
- **Work tracking:** GitHub Issues for all bugs, features, and tech debt
  - Every `TODO`/`FIXME` in code must reference an issue number
- **One task = one branch = one worktree = one PR.** Work is not done until a PR exists.
- Never modify the root checkout from an automated agent session.
- If sync-standards reports customized files, pending updates are in `.standards-pending/`. See `.standards/docs/reference/merge-standards-prompt.md` to merge.

## How to Work

1. **Read before modifying.** Understand the file's role and patterns before changing it.
2. **Prefer editing over creating.** Modify existing files rather than adding new ones.
3. **Minimal changes.** Make the smallest change that satisfies the requirement.
4. **Run tests.** Verify nothing is broken after each change (`make ci` or equivalent).
5. **Ask when blocked.** If requirements are unclear or a decision has significant trade-offs, stop and ask.

## Agent Workflow

1. Create worktree + branch: `git worktree add .claude/worktrees/<branch> -b <branch>`
2. Write tests first (TDD). Red → Green → Refactor.
3. Implement — minimum code to pass tests.
4. Run `make ci` — full pipeline must pass.
5. Push branch and open PR: `gh pr create --title "type(scope): description" --body "..."`
6. After merge, clean up: `git worktree remove .claude/worktrees/<branch>`

## Architecture

Dependencies point inward only: Domain ← Application ← Infrastructure ← Apps.

- **Domain:** Pure business logic. No external dependencies. Entities, value objects.
- **Application:** Orchestrates domain. Defines use cases and repository interfaces.
- **Infrastructure:** Implements interfaces. Handles DB, APIs, file systems.
- **Apps:** Entry points (CLI, web, workers). Depends on all inner layers.

SOLID principles mandatory. Violations require justification in code comments.
Validate inputs at boundaries. Fail fast; reject invalid state immediately.
Define typed custom errors in Domain. No generic exceptions.
Use structured logging with request ID, user ID, and operation context.
Secondary/derived subsystems (mirrors, indexes, sync, telemetry) are best-effort and error-isolated — a failure there must never break the primary write path.
When you catch-and-degrade, preserve and surface the original cause; never swallow it behind a generic "failed" message.

## Testing

TDD is mandatory. Red → Green → Refactor. Write failing tests before implementation code.
Coverage enforced in CI — blocks merge if violated:

- Domain / Core: 100%
- Application / Shell: 95%+
- Infrastructure: 95%+

Every bug fix must include a regression test.
Meet coverage with real tests — never add dead/unreachable branches to hit a number. For genuinely untestable boundaries (real filesystem/keychain/native APIs), keep the boundary thin, push logic into pure testable units, and flag the residual manual-verification step explicitly.
Run the build (e.g. `tsc -b`), not just typecheck — a narrow typecheck config often excludes test files and the project references that the full build compiles, so the build catches errors typecheck misses.
Mirror source structure: `src/domain/user.py` → `tests/domain/test_user.py`
Use descriptive test names: `test_should_raise_error_when_email_is_invalid`
Visual/snapshot baselines: regenerate via a CI dispatch job, never commit from a local run — rendered pixels are environment-specific and local baselines drift from CI.

## Security

P0/P1 block merge. P2 are warnings.

P0 (Critical): Never build SQL from user input — use parameterized queries. Never pass user input to `eval()`, `exec()`, `system()`. Never commit credentials. Never deserialize untrusted data via native serialization. Never ship default credentials. Verify auth on every request.

P1 (High): Encode all HTML output. No `innerHTML` with untrusted data. Validate URLs; block SSRF to internal IPs. Enable CSRF on state-changing endpoints. Secure cookie attributes (`Secure`, `HttpOnly`, `SameSite`). Regenerate session IDs after login. Use cryptographically secure random for tokens. Run dependency CVE scanning in CI. Enforce HTTPS/TLS. Configure CSP, HSTS, X-Content-Type-Options, X-Frame-Options. Never log passwords, tokens, or PII.

P2 (Warning): Missing rate limiting. Verbose errors in production. Unpinned dependencies. Missing encryption at rest.

Secrets scanning: run `git-secrets`, `truffleHog`, or `detect-secrets` in CI.
Add `.env` to `.gitignore`. Provide `.env.example` with placeholder values only.

## Naming Conventions

Use verbose, descriptive names. Avoid abbreviations. Functions are verbs. Classes are nouns.
Boolean variables: `is_`, `has_`, `should_`, `can_` prefixes.
Comments explain "why", never "what".

Python: `snake_case` variables/functions/modules, `PascalCase` classes, `UPPER_SNAKE_CASE` constants, `_leading_underscore` private.
JavaScript/TypeScript: `camelCase` variables/functions, `PascalCase` classes/components/types, `kebab-case` files/dirs, `UPPER_SNAKE_CASE` constants.
Java/Kotlin: `camelCase` methods/variables, `PascalCase` classes, `UPPER_SNAKE_CASE` constants, reverse-domain lowercase packages.
Ruby: `snake_case` methods/variables/files, `PascalCase` classes/modules, `?` suffix predicates, `!` suffix dangerous methods.
Rust/Zig: `snake_case` functions/variables, `PascalCase` types/structs/enums, `UPPER_SNAKE_CASE` constants.
Go: `camelCase` unexported identifiers, `PascalCase` exported functions/types/constants; `ALL_CAPS` not used (Go uses `PascalCase` for exported constants); short lowercase package names without underscores.
Elixir: `snake_case` functions/variables/atoms/files, `PascalCase` modules/aliases; `UPPER_SNAKE_CASE` not used (use module attributes `@name` instead); `?` suffix for predicates, `!` suffix for raising variants.

## Git Workflow

Conventional Commits: `type(scope): subject` — types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`, `perf`, `ci`.
Subject: imperative mood, ≤50 characters, no period.
Branch naming: `type/description` in kebab-case (e.g., `feature/user-authentication`, `fix/email-validation`).
One task = one branch = one PR. Work is not done until a PR exists. Prefer small, independently mergeable PRs, sequenced/stacked deliberately.
Feature branches from `main`. No direct pushes to `main`. Require one approval; all CI checks must pass.
PR merge loop: run the full local gate (`make ci`) first → open PR → wait for the automated reviewer → address every finding → all checks green (a partial pass is a fail) → squash-merge + delete branch. Never merge on a partial pass or before the automated review runs.
Run `gh pr create` from the feature worktree (or pass `--head`); the cwd's branch is what it opens.
Stacked PRs: retarget dependents to `main` before merging+deleting a base branch, or the dependents get closed.
Delete branches immediately after merge. Keep fewer than 10 active branches.
Never commit: secrets, build artifacts, `node_modules/`, `.env`, IDE files, OS files.
Always commit lock files (`package-lock.json`, `Gemfile.lock`, `Cargo.lock`, `uv.lock`).

## Documentation

Document all public APIs with language-standard formats (JSDoc, docstrings, rustdoc, JavaDoc, YARD). Include parameters, return values, errors, and examples.
Comments explain "why", never "what". Self-documenting code preferred.
Every package and app must have a README: purpose, installation, usage, configuration, development.
Create an ADR in `docs/adr/` for decisions affecting structure, dependencies, or significant technical choices.
Maintain `CHANGELOG.md` (Keep a Changelog format); update it for every user-facing change in a PR — UNLESS the project uses release automation (semantic-release, release-please, Changesets). With automation, release notes are generated (from Conventional Commits for commit-driven tools, or from per-PR changeset fragments for Changesets): do NOT hand-edit a shared `## [Unreleased]` block per PR (it causes merge conflicts under parallel work); rely on the generated notes or changeset fragments.
Track all work as GitHub Issues. Every `TODO`/`FIXME` must reference an issue: `# TODO(#42): description`.

## Language Standards

### Python

Use `uv` for dependency management. Commit `uv.lock`. `pyproject.toml` for all projects.
Format with `ruff format` (line length 100). Lint with `ruff check`. Never use `black` or `isort` separately.
Type-check with `mypy` or `pyright` strict mode — zero errors required.
All functions, methods, and class attributes require explicit type annotations (including private).
No `# type: ignore` without a comment and linked issue. No `Any` without justification.
Use `list[str]` over `List[str]` (3.9+). Use `|` union syntax over `Union` (3.10+).
Naming: `snake_case` variables/functions/modules, `PascalCase` classes, `UPPER_SNAKE_CASE` constants.
Test with `pytest` + `pytest-cov`. 95% minimum coverage, 100% for domain.
Custom exceptions inherit from a domain base exception. Never use bare `except:`. Use `raise ... from` for chaining.
Security: run `bandit -r src/` and `pip-audit` in CI. Banned: `eval()`, `exec()`, `pickle.loads()` (untrusted), `yaml.load()`, `os.system()`, `subprocess.call(..., shell=True)`.

### Typescript

Use `pnpm` (preferred) or `npm`. Commit lock files verbatim — they are generated artifacts, never hand-formatted. TypeScript 5.0+ with strict mode.
Pin the package manager: set an exact `packageManager` field in `package.json` and add `.npmrc` with `manage-package-manager-versions=true` so local pnpm self-switches to the pinned version (byte-identical to CI). CI installs from the lockfile without updating it (`pnpm install --frozen-lockfile`, or `npm ci` for npm).
Format with `prettier` (line length 120). Lint with `eslint` + `@typescript-eslint`. Run `tsc --noEmit` in CI. Ship a `.prettierignore` excluding lockfiles (`pnpm-lock.yaml`, `package-lock.json`, `yarn.lock`).
No `any`; use `unknown` when type is uncertain. Explicit types on public APIs; infer locally.
Use `Partial<T>`, `Pick<T>`, `Omit<T>`, `Record<K, V>` utility types.
ES modules (`import`/`export`). Avoid `require()`. Use `index.ts` barrel exports selectively.
Use `async`/`await` over `.then()`. Always handle promise rejections.
Naming: `camelCase` variables/functions, `PascalCase` classes/interfaces/types, `kebab-case` files, `UPPER_SNAKE_CASE` constants.
Test with `vitest` or `jest`. 95% coverage minimum, 100% for domain.
Extend `Error` with typed domain errors. Use `Result<T>` pattern for functional error handling.
Security: `eslint-plugin-security` and `pnpm audit` in CI. Banned: `eval()`, `Function()`, `setTimeout(string)`, `document.write()`. Use `crypto.randomUUID()` for security contexts.

## Project Type

Design around resources. Use RESTful conventions. Version APIs from the start (`/v1/`).
Error responses: consistent structured JSON with error code, message, and details. Never 200 with error body.
Never expose internal stack traces or DB errors to clients in production.
Expose `/health` (liveness) and `/ready` (readiness) endpoints. Health endpoints are exempt from authentication but must be restricted to internal networks in production.
Validate and sanitize all inputs at the API boundary before processing.
Implement idempotency keys for non-idempotent operations (POST, PATCH).
Apply rate limiting on all public-facing endpoints. Enforce HTTPS. Configure security headers.
Structured logging: include request ID, user ID, operation name, and duration on every log entry.
Emit metrics: request count, latency (p50/p95/p99), and error rate.
Propagate trace context headers (OpenTelemetry or equivalent).

<!-- BEGIN PROJECT-SPECIFIC — DO NOT EDIT THIS LINE -->

## Jesinia specifics

- **Read first:** the foundation spec `docs/specs/2026-10-16-jesinia-foundation-design.md`,
  the implementation plan `docs/superpowers/plans/2026-10-16-jesinia-foundation.md`, and the
  ADRs in `docs/adr/` (ADR 0008 is the accepted design system and shell).
- **Conventions:** `docs/conventions/` (KB format, architecture, adding an app, agents, kb-sync,
  security, design system) is the source of truth that agent skills cite.
- **Design:** `docs/design/` holds the brief, tokens, components, wireframes, and icons; names and
  values there are authoritative for `@jesinia/design`.
- **Licensing:** every source file starts with `SPDX-License-Identifier: AGPL-3.0-or-later`
  (`make ci` checks this via `scripts/check_spdx.py`).
- **Layering:** `core ← services ← apps`; apps never import apps; inside each module
  `domain ← application ← infrastructure ← api` (import-linter enforces both).
- **Entry point:** the Makefile only. CI never calls `uv`/`pnpm` directly, and the Python and JS
  toolchains never orchestrate each other.
<!-- Add project-specific instructions below this line. This section is preserved across re-assembly. -->
