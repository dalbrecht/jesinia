# ADR 0011: release-please writes the version and the changelog

- **Status:** Accepted
- **Date:** 2026-10-20
- **Related:** issue #83; [ADR 0009](0009-install-and-operations.md); the standards'
  Documentation section in `CLAUDE.md`/`AGENTS.md`

## Context

The standards require a `CHANGELOG.md` updated for every user-facing change, unless the
project uses release automation. Jesinia had neither, and the reviewer flagged it on #82.
Hand-editing a shared `## [Unreleased]` block in each PR causes merge conflicts when several
agent PRs run in parallel, and every PR title is already a Conventional Commit.

There was a second problem. Squash merges were titled `chore(merge): fix(review): … (#NN)`.
release-please (17.6, checked against its parser) reads that as type `chore`, scope `merge`,
and drops the inner `fix`/`feat`. Bullet lists of commits in the squash body are ignored too.
With that convention, no change would ever reach the changelog or bump the version.

## Decision

- **release-please on `main`**, from `.github/workflows/release-please.yml` with the action
  pinned to a commit SHA. It keeps one release PR open. Merging that PR tags `vX.Y.Z` and
  publishes a GitHub release.
- **One root package**, `release-type: simple`, in `release-please-config.json` with
  `.release-please-manifest.json` seeded at `0.1.0`, the version `server/pyproject.toml`
  already had. The web packages are private workspace internals and are not versioned.
  `bootstrap-sha` is the last commit before adoption, so the first release only covers what
  lands after this ADR.
- **The version lives in `server/pyproject.toml`**, which release-please bumps as an extra
  file. Its TOML updater cannot select the `jesinia` entry in `server/uv.lock` (filters do
  not match its wrapped values, and an array index would break as dependencies change).
  After release-please opens or updates the release PR, the workflow runs `make lock` on the
  release branch and commits the one-line lock change.
- **Keep a Changelog sections:** `feat` → Added, `fix` → Fixed, `perf` and `revert` →
  Changed; breaking changes get their own block. `docs`, `refactor`, `test`, `build`, `ci`
  and `chore` stay out. Before 1.0, a breaking change bumps the minor version.
- **Squash titles are the PR title, unwrapped**, for example
  `feat(auth): keep browsers signed in across restarts (#80)`. `chore(merge): …` stays only
  for `git merge` commits inside stacked branches, which the squash discards anyway.
  `CLAUDE.md` and `AGENTS.md` record this rule.

## Rejected

- **Keep the `chore(merge):` wrapper and make release-please read the inner type.** It has no
  option to strip a prefix, and it reads commits through the GitHub API, so the workflow
  cannot rewrite them first. Showing `chore` commits in the changelog would list the changes
  without the right section and turn every routine chore into a release.
- **A `BEGIN_NESTED_COMMIT` block in each squash body.** release-please does parse it, but
  every merge would need hand-written boilerplate, the same chore as a hand-edited changelog.
- **`release-type: python`.** It expects `pyproject.toml` at the package root, and Jesinia's
  lives in `server/`.
- **Hand-edited changelog.** Conflicts under parallel PRs, and it duplicates what the commit
  titles already say.

## Consequences

- PRs no longer touch `CHANGELOG.md`. A PR title's type decides whether and how a change
  appears, so it has to be accurate.
- A change merged under a wrong title can still be fixed: add a `BEGIN_COMMIT_OVERRIDE` …
  `END_COMMIT_OVERRIDE` block with the right Conventional Commit to the merged PR's
  description, and release-please uses it on its next run.
- The repository must allow GitHub Actions to create pull requests (Settings → Actions →
  General → Workflow permissions). Release PRs, and the lock commit, are made with
  `GITHUB_TOKEN`, so CI does not run on them. They only change the version, the changelog
  and the lock. If branch protection ever requires checks, the workflow needs a token that
  triggers them.
- Release headings use release-please's format: the version links to the compare view and
  the date follows in parentheses, not Keep a Changelog's `## [0.2.0] - date`. The sections and ordering follow Keep a Changelog.
