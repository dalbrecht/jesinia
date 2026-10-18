<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Issues app

The Issues app helps you triage GitHub issues across the repos you watch. New issues go to an
Inbox and existing ones to a Backlog, and every issue is checked against a per-repo label
convention. Later it adds suggestions, epics and work queues. The design is in
`docs/specs/2026-10-16-issues-design.md`, and the plan is
`docs/superpowers/plans/2026-10-16-issues.md`.

PR 1 (plan Tasks 1–4) added:

- GitHub issue reads and writes (in the `github` service);
- the label schema and completeness rules;
- sync, with the Inbox and Backlog;
- the REST reads and Settings;
- the label setup and migration batches;
- the read-only MCP tools.

PR 2 (Tasks 5–7) adds suggestions, the apply engine and the pending-changes tray. Task 5 adds
the suggestion drafts:

- agent triage of an issue, stored as drafts (labels for missing dimensions, new areas,
  duplicates, the likely epic, readiness gaps);
- background runs for Inbox issues and on-demand runs for the Backlog;
- listing, editing and dismissing drafts, and the two suggestion-backed saved filters.

Task 6 adds the apply engine and the pending-changes tray:

- approving a suggestion applies it to GitHub at once, as one ordered series per issue;
- every other GitHub-changing action stages in the tray until Apply sends it as one batch;
- every batch is kept with its result, step by step, so Retry runs only what didn't apply.

Task 7 adds the priority and duplicate actions:

- a priority ask over a batch of issues becomes one draft (`kind` `priority`, `scope` the batch)
  with a priority, a rank and a reason per issue; approving it sets each priority through the
  engine, every change guarded by the label the draft saw. Each item records its outcome (`applied`,
  `failed`, `skipped` or `unchanged`, with the cause); approving again tries only the items not
  applied, and the draft is `approved` once every item is. Turning the order into a queue is
  Task 9's;
- accepting a duplicate closes it as a duplicate of the original (by its database id) and
  comments `Duplicate of owner/repo#n`, always fully qualified.

Epics, queues and themes come in PR 3, and the web app in PR 4.

## Layout

```
domain/           pure rules, 100% covered
  schema.py       the label schema, the lenient labels.yml parser, classify, write_name
  completeness.py when an issue (or an epic or initiative) is complete
  triage.py       the Inbox rule, views, saved filters, the resumable sync cursor
  labels.py       LabelOp (CreateLabel, DeleteLabel), setup and migration plans, plan checks
  suggestions.py  Suggestion drafts: what a triage reply may draft, edits, dismissals
  changes.py      Change, ChangeBatch, the ordered series, label edits, results, approvals
  similarity.py   the local TF-IDF that pre-filters duplicate candidates
application/      orchestration over ports
  ports.py        what the app needs: IssueReads, LabelWrites, AgentAsker, the store protocols
  sync.py         SyncService: watch or unwatch, sync, Settings schemas, recompute
  reads.py        IssueQueries: views, items and search, served from the cache only
  labels.py       LabelPlanner, LabelExecutor (ordered, paced), LabelSetupService
  suggest.py      SuggestService: triage asks, background and on-demand runs, edits
  agent_output.py an agent's JSON reply, bounded and validated against schemas/*.json
  apply.py        ApplyEngine: runs a batch's series against fresh reads (paced, rate-limit safe)
  pending.py      PendingService (the tray) and ApplyService (apply, retry, approve)
infrastructure/
  store.py        SqliteIssuesStore over issues.sqlite
  suggestion_store.py  SqliteSuggestionStore: drafts, runs, the similarity corpus
  apply_store.py  SqliteApplyStore: the pending tray and the applied batches
api/
  routes.py       REST under /api/issues
  schemas.py      request and response bodies (the generated web client's types)
  tools.py        the MCP tools
migrations/       issues.sqlite schema
schemas/          JSON schemas of the agent jobs' replies (triage and priority now; the rest for Tasks 8–9)
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
- `pending`: the pending-changes tray, in the order staged.
- `apply_batches`: every batch (from the tray or an approval), stored `running` before its first
  GitHub write and updated after every step, then `finished` (or `interrupted` by a stop), with
  the Idempotency-Key that started it. Kept 30
  days for Retry (pruned by the daily job).
- `schema_overlay`: schema entries the author approved (a new area). They are merged over the
  repo's schema on every read, a read-only labels.yml included, so a sync never drops them.
- A suggestion's `apply_error` says why its last approval didn't apply.

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
- `issues.idempotency-prune` runs daily and drops expired keys and applied batches older than
  30 days.

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
  - `POST /suggestions/priority` with `{refs: [{repo, n}]}`: ask the agent, in the background
    (202), to prioritise those open cached issues. The answer names the batch's `scope`; poll
    `GET /suggestions?scope=` for its `priority_run` (`running`, `done`, `waiting` or `failed`
    with a cause; kept until the server restarts) and the draft. Asking for a batch already being
    asked returns that run.
  - `PATCH /suggestions/{id}` with `{payload, reason?}` edits a draft, checked against the
    repo's schema. `POST /suggestions/{id}/dismiss` dismisses it. Neither touches GitHub.
  - `POST /suggestions/{id}/approve` applies a draft to GitHub at once, in the background. It
    answers 202 with the suggestion and its batch (`running`), and `Location` names the batch
    to poll. The suggestion is `approved` once the batch completes. Otherwise it stays
    approvable with `apply_error`, a conflict with GitHub's fresh read included. A premise
    already gone in the cache, a settled or claimed suggestion, or a duplicate of an uncached
    issue is a 409, and nothing is applied.
  - `GET /pending` lists the tray. `POST /pending` with `{repo, n, change, source}` stages a
    change (201). `DELETE /pending/{id}` undoes one, `DELETE /pending` all (204).
  - `POST /pending/apply` with `{ids?}` moves the tray (or those ids) into one batch and applies
    it in the background. It answers 202 with the batch and `Location` (an empty tray is a 409).
  - **Polling:** `GET /pending/{batch}` returns the batch step by step.
    - Its `status` is `running` (steps not reached yet are `skipped`, "not run yet"),
      `finished`, or `interrupted` (a stop cut it off).
    - A finished batch that GitHub stopped says so in `stopped` and `rate_limited_until`. There
      is no 503 and no `Retry-After`.
    - `POST /pending/{batch}/retry` (202) resumes a finished or interrupted batch, and never
      repeats a recorded step. A comment or new issue cut off mid-write is never re-sent blindly
      (see below). A batch still running is a 409.
  - **Replays:** a replayed apply, approve or retry, with the same Idempotency-Key, answers the
    same batch. The batch keeps the key too, so a replay never applies the tray again, even
    across a crash (ruling IR46).
  - Saved filters: `incomplete`, `stale_90d`, `human_needed_could_be_ready` and
    `possible_duplicates`. The last two read the open suggestions.
  - Typed errors come back as `{error, detail}`. A GitHub rate limit met by a request itself
    (a sync or a preview, not a batch) is a 503 with `Retry-After`, so the response is never
    stored under the key.
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

## How applying stays safe

One engine (`ApplyEngine`) serves approvals and the tray (spec §6.2):

- **The series:** a batch first runs its repo-level steps: `EnsureLabel` (an approved new area),
  then `CreateIssue`. Then each issue runs labels, parent, sub-issue order, close and comment, in
  that order. The order step is a spec-compatible extension (ruling IR13). Within one issue a
  later change of the same sort wins.
- **Fresh reads:** each issue is read from GitHub before its first step. Labels are applied as an
  add and removes against that read, never as a replacement, so labels someone else changed on
  GitHub survive, and a label already gone is a no-op.
- **No implicit labels:** the repo's labels are read fresh once per batch, and every label about
  to be added (to an issue, or to a new issue) is read again live right before the write. A
  missing label fails the step, and nothing is added or created. `EnsureLabel` reuses the label
  setup's executor (ruling IR12).
- **Conflicts:** a staged parent move records the parent the author saw. If GitHub's parent
  differs at apply time, the step fails as a conflict (`conflict: true`, "moved on GitHub") and
  nothing moves (ruling IR39). A reorder whose child has left the epic is skipped with a cause,
  never sent to GitHub (IR41).
- **Close comments:** a close's own comment is posted only when the close happened. A staged
  comment is always posted.
- **Failures:** an issue stops at its first failed step, and its other steps are skipped. Other
  issues carry on. A failed `EnsureLabel` skips the issues that need its label, with the cause. A
  failed `CreateIssue` skips every step that names its temp id.
- **Rate limits:** a rate limit or a rejected token stops the whole batch. The step at hand and
  all the rest are failed with the cause and GitHub's reset time, and syncs back off. While that
  back-off lasts, a new batch asks GitHub nothing. Writes are spaced 1 second apart.
- **Approvals:**
  - The premise is checked twice: against the cache first, then against the apply's fresh read.
    An approval only fills a gap, so any of these changes on GitHub since the last sync makes it a
    409 (`SuggestionStaleError`), with nothing of it written:
    - a dimension filled;
    - a readiness that is no longer `human-needed` (for "could be agent-ready");
    - a parent set;
    - the issue closed.
  - A duplicate takes the original's database id from the cache, and can't name itself.
  - While an approval (or its Retry) applies, its draft is claimed, so retiring and reruns leave
    it (IR40).
    Claims a stop cut off are released at start.
  - A complete apply marks the suggestion approved. A failed one leaves it approvable, with the
    cause.
- **Write-ahead, in the background (ruling IR45):**
  - A batch is stored `running` before its first GitHub write and updated after every step.
  - One task per batch runs it in the background, and batches still run one at a time, in the
    order started.
  - A stop cancels the running batches and marks them `interrupted`. Start marks those a crash
    left `running` the same way.
  - Retry resumes either from the recorded steps, so a recorded step is never repeated.
  - A comment or a new issue is recorded `in flight` right before its write. If the batch is cut
    off then, the write may have gone out, so the first Retry doesn't send it blindly. It fails
    that step, asking you to check GitHub, and the next Retry sends it.
  - A replay of the key answers the batch as it stands and never runs it again. It resumes only
    a batch still `running` that no task holds.
  - A close's comment withheld because the issue was already closed stays withheld on Retry.
  - An unexpected error fails only its step, with its type, and the batch goes on.
- **Claims:** an approval's (or its Retry's) claim is released whatever happens: done, failed,
  cancelled or broken. While it is held, editing or dismissing the draft is a 409.
- **Cache:** afterwards the touched issues, the labels and (for a new area) the recomputed triage
  are written to the cache, best effort. A failure there, or in storing the batch, is logged with
  its cause and never fails the writes. Issues the batch created arrive with the next sync.

## How suggestions stay safe

Issue titles and bodies are written by whoever can open an issue, so they are untrusted:

- **No GitHub client:** `SuggestService` holds none. A run can only write drafts to
  `issues.sqlite`. A draft reaches GitHub only when the author approves it.
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
  closed issue. Edited drafts are the author's; approving one re-checks it.

## Development

```bash
make test-py      # includes tests/apps/issues and tests/services/github
make gen-client   # after changing api/schemas.py or a route
make ci           # the full gate
```

Tests use in-memory GitHub doubles (`tests/apps/issues/test_sync.py` `FakeIssues`,
`test_labels.py` `FakeGitHub`, `test_apply.py` `FakeWrites`) and a scripted agent (`test_suggest.py` `FakeAgent`), with a
fake clock, so they make no network calls and never sleep.
