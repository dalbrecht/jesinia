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

PR 2 (Tasks 5–7) adds suggestions, the apply engine and the pending-changes tray. Task 5, here
so far, adds the suggestion drafts:

- agent triage of an issue, stored as drafts (labels for missing dimensions, new areas,
  duplicates, the likely epic, readiness gaps);
- background runs for Inbox issues and on-demand runs for the Backlog;
- listing, editing and dismissing drafts, and the two suggestion-backed saved filters.

Approving a draft (which writes to GitHub) arrives with Task 6's apply engine. Epics, queues and
themes come in PR 3, and the web app in PR 4.

## Layout

```
domain/           pure rules, 100% covered
  schema.py       the label schema, the lenient labels.yml parser, classify, write_name
  completeness.py when an issue (or an epic or initiative) is complete
  triage.py       the Inbox rule, views, saved filters, the resumable sync cursor
  labels.py       LabelOp (CreateLabel, DeleteLabel), setup and migration plans, plan checks
  suggestions.py  Suggestion drafts: what a triage reply may draft, edits, dismissals
  similarity.py   the local TF-IDF that pre-filters duplicate candidates
application/      orchestration over ports
  ports.py        what the app needs: IssueReads, LabelWrites, AgentAsker, the store protocols
  sync.py         SyncService: watch or unwatch, sync, Settings schemas, recompute
  reads.py        IssueQueries: views, items and search, served from the cache only
  labels.py       LabelPlanner, LabelExecutor (ordered, paced), LabelSetupService
  suggest.py      SuggestService: triage asks, background and on-demand runs, edits
  agent_output.py an agent's JSON reply, bounded and validated against schemas/*.json
infrastructure/
  store.py        SqliteIssuesStore over issues.sqlite
  suggestion_store.py  SqliteSuggestionStore: drafts, runs, the similarity corpus
api/
  routes.py       REST under /api/issues
  schemas.py      request and response bodies (the generated web client's types)
  tools.py        the MCP tools
migrations/       issues.sqlite schema
schemas/          JSON schemas of the agent jobs' replies (triage now; the rest for Tasks 7–9)
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
- `suggestions`: the drafts, with their kind, payload, one-line reason and state (`draft`,
  `edited`, `approved`, `dismissed`). A draft about one issue has `repo` and `n`; later tasks'
  drafts about several issues or a queue have `scope` instead.
- `suggestion_runs`: each issue's latest suggestion run, with its status, when it is due, and
  the cause of a failure.
- `pending` and `schema_overlay`: the pending-changes tray and the approved schema entries, used
  from Task 6 on.

## Jobs

- `issues.sync` runs every 5 minutes, and once at start, when the `github` service exists.
  - It syncs each watched repo incrementally and can resume after an interruption.
  - On a repo's first sync, its existing issues go to the Backlog, and nothing is fetched per
    issue.
  - Any failure is recorded in that repo's `sync_error`, an unexpected one included, and the
    other repos still sync.
  - A rate limit pauses every sync until GitHub's reset time.
- `issues.suggest` runs every minute, and once at start. It starts the suggestion runs that are
  due, two at a time.
  - Sync queues a run when an issue enters the Inbox (due at once). It queues one again when an
    Inbox issue's title, body or labels change, due five minutes after the last change, so a
    burst of edits costs one run. A comment alone queues nothing.
  - A queued issue that left the Inbox before its turn is skipped.
  - Backlog issues are never run in the background, only on demand, so a big first sync costs
    no agent time.
  - Each run's failure is logged with its cause and recorded on the run. It never breaks sync
    or other runs. Without a runner, runs wait.
  - At start, a run that a stop cut off is queued again.
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
  - `GET /suggestions?repo&n&state&kind`: drafts from the cache. `state` may repeat; the
    default is the open ones. With `repo` and `n` the answer carries the issue's latest run.
  - `POST /suggestions/run` with `{refs: [{repo, n}]}`: run suggestions for up to 100 cached
    issues now, in the background (202).
  - `PATCH /suggestions/{id}` with `{payload, reason?}` edits a draft, checked against the
    repo's schema. `POST /suggestions/{id}/dismiss` dismisses it. Neither touches GitHub.
  - Saved filters: `incomplete`, `stale_90d`, `human_needed_could_be_ready` and
    `possible_duplicates`. The last two read the open suggestions.
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

## How suggestions stay safe

Issue titles and bodies are written by whoever can open an issue, so they are untrusted:

- **No GitHub client:** `SuggestService` holds none. A run can only write drafts to
  `issues.sqlite`. A draft reaches GitHub only when the author approves it (Task 6).
- **Fenced as data:** the agent is asked through the agents service's one-shot ask, which has no
  tools. Jesinia's instructions are the role and the prompt. The issue, the repo's label schema,
  the duplicate candidates and the epics are `UntrustedDoc`s, fenced as data.
- **Validated:** the reply must be one JSON object valid against `schemas/triage.json`. It is
  bounded before parsing. An invalid reply gets one retry, which describes the problem without
  repeating the reply's own values. A second invalid reply fails the run with
  `SuggestionJobFailedError`.
- **Jesinia decides what a reply may draft:**
  - labels only for missing dimensions, as schema values (an area outside the schema is a
    `new_area`);
  - duplicates only among the candidates the local TF-IDF found;
  - a parent only among the open epics listed;
  - every reason on one line.
- **Reruns:** a rerun replaces untouched drafts. It never doubles advice the author edited. Advice
  they dismissed stays away only while the issue's title, body and labels are unchanged (a
  content hash). Approved advice never holds anything back, since what it set may be undone
  later (ruling IR38a).
- **Retiring:** whenever sync or a schema recompute stores an issue's triage, untouched drafts
  that lost their premise are deleted: a label for a dimension no longer missing, a parent for
  an issue that now has one, a readiness gap whose readiness changed, and every draft of a
  closed issue. Edited drafts are the author's; Task 6's approval re-checks them.

## Development

```bash
make test-py      # includes tests/apps/issues and tests/services/github
make gen-client   # after changing api/schemas.py or a route
make ci           # the full gate
```

Tests use in-memory GitHub doubles (`tests/apps/issues/test_sync.py` `FakeIssues`,
`test_labels.py` `FakeGitHub`) and a scripted agent (`test_suggest.py` `FakeAgent`), with a
fake clock, so they make no network calls and never sleep.
