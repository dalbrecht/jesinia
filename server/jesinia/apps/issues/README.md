<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Issues app

The Issues app helps you triage GitHub issues across the repos you watch. New issues go to an
Inbox and existing ones to a Backlog, and every issue is checked against a per-repo label
convention. Later it adds suggestions, epics and work queues. The design is in
`docs/specs/2026-10-16-issues-design.md`, and the plan is
`docs/superpowers/plans/2026-10-16-issues.md`.

This is PR 1 (plan Tasks 1–4). It adds:

- GitHub issue reads and writes (in the `github` service);
- the label schema and completeness rules;
- sync, with the Inbox and Backlog;
- the REST reads and Settings;
- the label setup and migration batches;
- the read-only MCP tools.

Suggestions, the apply engine and the pending-changes tray come in PR 2. Epics, queues and
themes come in PR 3, and the web app in PR 4.

## Layout

```
domain/           pure rules, 100% covered
  schema.py       the label schema, the lenient labels.yml parser, classify, write_name
  completeness.py when an issue (or an epic or initiative) is complete
  triage.py       the Inbox rule, views, saved filters, the resumable sync cursor
  labels.py       LabelOp (CreateLabel, DeleteLabel), setup and migration plans, plan checks
application/      orchestration over ports
  ports.py        what the app needs: IssueReads, LabelWrites, the store protocols
  sync.py         SyncService: watch or unwatch, sync, Settings schemas, recompute
  reads.py        IssueQueries: views, items and search, served from the cache only
  labels.py       LabelPlanner, LabelExecutor (ordered, paced), LabelSetupService
infrastructure/
  store.py        SqliteIssuesStore over issues.sqlite
api/
  routes.py       REST under /api/issues
  schemas.py      request and response bodies (the generated web client's types)
  tools.py        the MCP tools
migrations/       issues.sqlite schema
module.py         IssuesModule: wiring, jobs, the 503 guards
```

## Where data lives

`issues.sqlite` is a **history** database, so `jesinia backup` includes it. Queues, notes,
themes and drafts are written by the author and can't be rebuilt from GitHub. It holds:

- `repos`: each repo the author added, under the name GitHub reports for it. It keeps the
  effective schema and its source, the Settings schema (kept apart from labels.yml), the
  `migrated` flag and the sync cursor and status. Every repo column is `COLLATE NOCASE`.
- `issues`: GitHub's data, stored verbatim, plus the stored triage (`complete`, `missing`,
  `inbox`, and so on).
- `labels`, `milestones`, `comments`, `timeline` and `sub_issue_links`: the cache.
- `label_plans`: previewed label batches. Each one is consumed by its apply and pruned after
  15 minutes.
- `idempotency`: the keys of this module's write routes.

## Jobs

- `issues.sync` runs every 5 minutes, and once at start, when the `github` service exists.
  - It syncs each watched repo incrementally and can resume after an interruption.
  - On a repo's first sync, its existing issues go to the Backlog, and nothing is fetched per
    issue.
  - Any failure is recorded in that repo's `sync_error`, an unexpected one included, and the
    other repos still sync.
  - A rate limit pauses every sync until GitHub's reset time.
- `issues.idempotency-prune` runs daily and drops expired keys.

## The label schema and labels.yml

- A repo's schema comes from `.jesinia/labels.yml` on its default branch, when the file
  exists. Otherwise it comes from the schema made in Settings, or else from the default
  (spec §2).
- The file is untrusted, so the parser is lenient but bounded:
  - it caps the size, and refuses anchors, aliases and deep nesting;
  - every unusable entry is listed in `schema_errors`.
- A Settings schema follows the same rules but is refused outright if it has any error.
- Label names compare by `label_key`: Unicode NFC, trimmed, then casefolded. An alias that is
  its own canonical name in another form counts as a repeat, and is refused.

## API

- **REST, under `/api/issues`.** Every route is owner-only, like all module routes, so agent
  session tokens get a 403. Every write takes an `Idempotency-Key` header.
  - `GET /repos`: every added repo, with its schema and sync status.
  - `PUT /repos/{owner}/{repo}` with `{watched, settings_schema}`: `settings_schema` is
    required; send `null` to clear it.
  - `POST /repos/{owner}/{repo}/sync`: sync the repo now.
  - `GET /{inbox|backlog}?repo&filter`: a view, served from the cache, even without the
    `github` service.
  - `GET /item/{owner}/{repo}/{n}` and `POST …/refresh`: one issue, and a fetch of its
    comments and timeline.
  - `POST /labels/{owner}/{repo}/preview` with `{kind}`. It isn't keyed, on purpose: it writes
    nothing to GitHub.
  - `POST /labels/{owner}/{repo}/apply` with `{kind, plan_id}`: the approval.
  - Typed errors come back as `{error, detail}`. A GitHub rate limit is a 503 with
    `Retry-After`, so the response is never stored under the key.
- **MCP, read-only:** `issues_search {q, repo?}` and `issues_get {repo, n}`. Both read the
  cache only. Every value an issue's author wrote comes back inside `<untrusted-data>` fences.

## How the label batches stay safe

Jesinia never creates a label implicitly and never changes GitHub without an approval:

- **Plans:** a preview stores its plan for 15 minutes. An apply is refused with a 409 if the
  repo's labels or schema changed since the preview.
- **Refusals:** setup and migration refuse to run when labels.yml has errors
  (`SchemaHasErrorsError`, 400), so the default convention is never pushed onto a repo that has
  its own. Two GitHub labels that compare as one name are refused too
  (`LabelNameCollisionError`, 409).
- **Fresh labels:** the batch reads the repo's labels fresh before any write. It never asks
  GitHub to add a label the repo lacks, since GitHub would create that label silently.
- **Live membership:** a migration finds who carries an old label from GitHub, never from the
  cache.
  - First, every issue and pull request carrying each old label is listed live and moved to
    the canonical label.
  - Then, right before each delete, the label is listed again, and anything labelled since is
    moved too.
- **Deletes:** a delete runs only if no step of the batch failed. The result names the issues
  that failed.
- **Commas:** a label whose name contains a comma is never deleted, because GitHub's
  `labels=` filter can't list it. The preview says so (`skip_cause`).
- **Self-moves:** a label is never moved onto itself.
- **Pacing:** writes are spaced 1 second apart. A rate limit, or a token GitHub rejects, stops
  the batch, and the rest of it is skipped.
- **Cache:** after an apply, the label cache and the moved issues are updated at once, best
  effort.

## Development

```bash
make test-py      # includes tests/apps/issues and tests/services/github
make gen-client   # after changing api/schemas.py or a route
make ci           # the full gate
```

Tests use in-memory GitHub doubles (`tests/apps/issues/test_sync.py` `FakeIssues`,
`test_labels.py` `FakeGitHub`), so they make no network calls.
