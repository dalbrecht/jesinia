<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Issues app

The Issues app helps you triage GitHub issues across the repos you watch. New issues go to an
Inbox and existing ones to a Backlog, and every issue is checked against a per-repo label
convention. Later it adds suggestions, epics, work queues and themes. The design is in
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
  with a priority, a rank and a reason per issue (a reply is discarded, and the run says "inputs
  changed, ask again", when an issue's title, body, labels or open state, or the priority schema,
  changed during the ask); approving it sets each priority through the
  engine, every change guarded by the label the draft saw. Each item records its outcome (`applied`,
  `failed`, `skipped` or `unchanged`, with the cause); approving again tries only the items not
  applied, and the draft is `approved` once every item is. Turning the order into a queue is
  Task 9's;
- accepting a duplicate closes it as a duplicate of the original (by its database id) and
  comments `Duplicate of owner/repo#n`, always fully qualified.

PR 3 (Tasks 8–10) adds epics, work queues and themes. Task 8 adds epics:

- the Epics list and an epic's page, from the cache: progress (closed children of all),
  readiness from the direct children with what blocks each (a child that is itself an epic counts
  by its own children, ruling IR51), a "won't fit one PR" warning for
  children in another repo, and a suggestion to add the `epic` label to an issue that has
  children without it;
- drop checks: an issue can't contain itself or one of its ancestors, another owner's issue, or
  an initiative inside an epic (ruling IR17). A refused parent link is never staged;
- checklist conversion: a plan from the epic's task list (linked items matched, unlinked open
  items drafted as new issues with no labels), approved as **one batch** of `CreateIssue` then
  `SetParent` through the apply engine;
- agent proposals, all drafts: groups of related issues as candidate epics, additions to
  existing epics (`kind` `group`), splits of an issue too big for one PR (`split`) and an epic's
  delivery brief (`brief`). Approving a group or split applies it through the engine;
- the delivery brief: approving it posts the epic's `<!-- jesinia:brief -->` comment once and
  edits it in place afterwards;
- milestones as queue items: each stands for its open issues, with readiness rolled up and a
  brief built from them (Task 9's queues hold them).

Task 9 adds work queues (spec §5.2, ruling IR15):

- named, ordered queues of issues, epics and milestones, kept in `issues.sqlite` only. Creating,
  renaming and ordering queues, and adding, moving, noting and removing items, are local and
  apply at once; nothing of it is a GitHub change;
- each open item shows its readiness (an epic's rolled up from its children, a milestone's from
  its open issues) and a warning saying why it isn't ready. It is warned, never hidden;
- a queue grouped by priority (bands). Dropping an item into another band reorders it at once and
  stages the priority label in the pending tray (`source` `queue`);
- Done: the item moves to the Done group at once (with Undo), and closing the issue (with the
  author's optional comment) or setting it `agent-ready` is staged in the tray. Milestones
  can't be done or moved between bands (Jesinia never writes them);
- items close themselves: when sync (or an applied batch) sees an issue, epic or milestone
  closed, its open queue items are marked done. One already closed when it is added goes straight
  into the Done group;
- the agent's queue proposals, all drafts: what to add next (`queue_next`), an order
  (`queue_order`) and which queue each item fits (`queue_split`). Approving one changes only the
  local queues;
- a priority draft's order (Task 7) can make a new queue or fill one.

Task 10 adds themes (spec §5.1, §6.2, rulings IR15, IR22, IR27):

- a theme is a tag from the KB-wide catalog (the same `#tags` as notes and kept posts), kept
  locally per issue in `issues.sqlite`. Adding one (a drop onto a theme, one issue or a
  multi-selection) and removing one are local and immediate, never a staged change;
- a new tag passes the catalog's instant, catalog-only near-duplicate check
  (`TagService.check(tag, advise=False)`, no agent) and is added with `TagService.ensure`, as
  Notes' Save note does; a near-duplicate is refused and nothing is stored;
- Undo of an add (or removing the theme) takes the local theme off only: the catalog entry
  `ensure` added stays, as it does for a note's tags (IR22);
- publishing a theme in a repo stages in the pending tray (`source` `theme`) the label setup step
  (an `EnsureLabel` for `theme:<tag>`) and an `add_label` change for each of the theme's cached
  issues there that lacks it. They apply with the tray, through the write-ahead engine;
- turning a theme into an epic is one approved batch, planned like a checklist conversion
  (`ConversionPlan`, IR27): the epic is created in one of its issues' repos and the theme's
  issues are linked under it.

PR 4 (Tasks 11–14) adds the web app, `web/apps/issues/`, served at `/issues/`. Task 12 adds its
shell: the Inbox and Backlog (a compact list with multi-select and a bulk bar, or a board by
readiness, priority or status), an issue in the book layout, and the Pending changes tray, which
polls each batch it starts (an apply, a retry or an approval) to its per-issue results.

## Layout

```
domain/           pure rules, 100% covered
  schema.py       the label schema, the lenient labels.yml parser, classify, write_name
  completeness.py when an issue (or an epic or initiative) is complete
  triage.py       the Inbox rule, views, saved filters, the resumable sync cursor
  labels.py       LabelOp (CreateLabel, DeleteLabel), setup and migration plans, plan checks
  suggestions.py  Suggestion drafts: what a triage reply may draft, edits, dismissals
  changes.py      Change, ChangeBatch, the ordered series, label edits, results, approvals
  epics.py        is_epic, can_contain, readiness, checklist plans, proposals, briefs
  queues.py       queue items and their local order, bands, warnings, queue proposals
  themes.py       theme tags, the theme: label and its setup step, the theme-to-epic plan
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
  epics.py        EpicService (views, plans, milestones, asks) and BriefService (the comment)
  queues.py       QueueService: queues, items, Done and band moves (staged), auto-done, asks
  themes.py       ThemeService: themes (local, via the KB catalog), publishing (staged), epics
infrastructure/
  store.py        SqliteIssuesStore over issues.sqlite
  suggestion_store.py  SqliteSuggestionStore: drafts, runs, the similarity corpus
  apply_store.py  SqliteApplyStore: the pending tray and the applied batches
  brief_store.py  SqliteBriefStore: each epic's brief comment
  queue_store.py  SqliteQueueStore: the work queues and their items
  theme_store.py  SqliteThemeStore: each issue's themes and the theme epics' batches
api/
  routes.py       REST under /api/issues
  schemas.py      request and response bodies (the generated web client's types)
  tools.py        the MCP tools
migrations/       issues.sqlite schema
schemas/          JSON schemas of the agent jobs' replies (triage, priority, grouping, brief, queue)
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
- `pending`: the pending-changes tray, in the order staged. A row is a change to one issue, or
  (its `ref` names only the repo) a repo's label setup step, which a published theme stages.
- `apply_batches`: every batch (from the tray, an approval or a checklist conversion, its
  `origin`), stored `running` before its first
  GitHub write and updated after every step, then `finished` (or `interrupted` by a stop, or
  `abandoned` by the author), with the Idempotency-Key that started it, and when the author
  dismissed it from the tray (`dismissed_at`, cleared by a retry). Kept 30 days for Retry
  (pruned by the daily job). Pruning never forgets what a batch of an origin that creates
  issues did: that is in `batch_origins`, and a pruned unfinished one is abandoned through it.
- `schema_overlay`: schema entries the author approved (a new area). They are merged over the
  repo's schema on every read, a read-only labels.yml included, so a sync never drops them.
- A suggestion's `apply_error` says why its last approval didn't apply.
- `epic_briefs`: each epic's brief comment, by its GitHub comment id, with the body posted. It is
  `in_flight` from right before a new comment is posted until its id is stored (ruling IR8), with
  the attempt's nonce (IR48), and the body an edit is on its way with (`pending_body`).
- `batch_origins`: every batch that may create issues, by its origin, which is its semantic
  target, never a draft id (ruling IR57): `split:<repo>#<n>` (the issue split), `group:<hash of
  the sorted members>` (a new epic), `conversion:<repo>#<n>` (the epic) and `theme:<tag>:<repo>`.
  It is written in the same transaction as the batch's first save and kept current with each
  later save: the state (`running`, `complete`, `incomplete`, or `abandoned`, IR58), the issues
  created by temp id, and a conversion's items (each unlinked item's hash, by its normalised text
  and repeat, to its temp id: the checklist stays in the body, so this is what stops a second
  conversion creating the item again). Never pruned (IR56).
- `queues` and `queue_items`: the work queues, in the author's order, and each queue's items
  (`issue`, `epic` or `milestone`, by `owner/repo#n`) with their position, note, when they were
  added and, once done, when and the pending change Done staged. A done item keeps its position,
  so Undo puts it back where it was.
- `issue_themes`: each issue's themes, by the catalog's canonical tag. The catalog itself is the
  KB's `Tags.md`, which this module never writes except through `TagService.ensure`.
- `theme_epics`: the batches that turned a theme into an epic, with the epic each made or
  linked under, so issues added to the theme later link under it (an unfinished batch refuses a
  new approval through `batch_origins`: retry or abandon it).

## Jobs

- `issues.sync` runs every 5 minutes, and once at start, when the `github` service exists.
  - It syncs each watched repo incrementally and can resume after an interruption.
  - Each stored page marks done the open queue items whose issue closed, and those of the repo's
    closed milestones. This is best effort: a failure is logged with its cause and the sync goes
    on.
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
    `github` service. Each row carries GitHub's `state` and `linked_pr_state` (the linked PRs
    summed up as `merged`, `open` or `closed`), which the board's status view is derived
    from. Sync reads the linked PRs only for open issues it sees change, never on a repo's first
    sync (IR20), and an apply's fresh read keeps what sync found.
  - `GET /batches`: the batches that didn't finish (running, interrupted, or finished with a
    step not applied), newest first, so the tray offers Retry or Abandon after a reload. An
    `EarlierBatchError` names its batch in `batch_id`. `POST /pending/{batch}/dismiss` (keyed,
    204) takes a settled batch out of that list for good; a retry puts it back; a running one is
    a 409.
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
    `GET /suggestions?scope=` for its `ask_run` (`running`, `done`, `waiting` or `failed`
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
      (see below). A batch still running, or abandoned, is a 409.
    - `POST /pending/{batch}/abandon` with `{created?: [{temp_id, repo, n}]}` gives up on an
      unfinished batch of an origin that creates issues, even a pruned one (found through its
      origin's ledger): it never runs again, and the issues it created, plus `created` (issues
      found on GitHub for a create that was sent but not recorded done: cut off mid-write, or
      its answer lost to a timeout or a dropped connection; for a pruned batch, a temp id of
      its origin's kind), are reused by the next approval for
      that origin, which creates only what is missing (ruling IR58). Reused split parts must be
      the parts the abandoned batch made, in the same places (the refusal names them, to link
      by hand). It answers the origin and those
      issues. A running batch is a 409, a complete one a 400.
  - **Replays:** a replayed apply, approve or retry, with the same Idempotency-Key, answers the
    same batch. The batch keeps the key too, so a replay never applies the tray again, even
    across a crash (ruling IR46).
  - `GET /epics?repo`: the open epics and initiatives with progress and readiness.
    `GET /epics/{owner}/{repo}/{n}`: one epic, its children in order (each with what blocks it),
    the approved brief and a brief composed from the cache (`default_brief`, for Copy brief);
    404 for an issue that isn't an epic.
  - `GET /epics/{owner}/{repo}/{n}/accepts?child_repo&child_n`: whether a drop may happen, with
    the refusal's code and reason, or the "won't fit one PR" warning.
  - `GET /epics/{owner}/{repo}/{n}/conversion`: the checklist conversion plan and its `digest`;
    nothing is written. `POST` the same path with `{digest}` to approve it: a 202 with the batch
    (`origin` `epic`) and `Location` to poll. A plan changed since is a 409
    (`ConversionOutdatedError`), and so is one with nothing to do, or one while an earlier
    conversion of the epic is running or didn't finish (`EarlierBatchError`: retry that batch).
  - `POST /epics/proposals` with `{repo?}`, `POST /suggestions/split` with `{repo, n}` and
    `POST /epics/{owner}/{repo}/{n}/brief/draft` ask the agent in the background (202 with the
    `scope`); poll `GET /suggestions?scope=` for its `ask_run` and the drafts.
  - `POST /epics/{owner}/{repo}/{n}/brief` with `{text, suggestion_id?, overwrite?}` posts the
    brief, or edits the stored comment in place (a 409 when it was edited on GitHub since,
    unless `overwrite`), and marks the brief draft approved.
  - `GET /milestones?repo` and `GET /milestones/{owner}/{repo}/{number}`: milestones with their
    open issues, rolled-up readiness and brief.
  - `GET /queues`: the work queues with their open, done and warned counts.
    `POST /queues` with `{name, description?}` makes one (201; a name taken, without case, is a
    409). `GET /queues/{id}`: its open items in order (each with its readiness and warning), the
    Done group and the priority bands. `PATCH /queues/{id}` with `{name?, description?,
    position?}` and `DELETE /queues/{id}`. `GET /queues/{id}/warnings`: the items that aren't
    ready, with why.
  - `POST /queues/{id}/items` with `{kind, repo, n, position?, note?}` adds a cached issue, epic
    or milestone (201). Under `/queues/{id}/items/{kind}/{owner}/{repo}/{n}`: `PATCH` with
    `{position?, note?}` and `DELETE` (local, at once); `POST …/done` with `{then: close |
    agent_ready, comment?}` and `POST …/band` with `{band, position?}` answer the queue and the
    change staged in the tray (`staged`); `POST …/undo` reopens a done item and unstages its
    change.
  - `POST /queues/{id}/suggestions/next`, `POST /queues/{id}/suggestions/order` and
    `POST /queues/suggestions/split` with `{queue_ids}` ask the agent in the background (202
    with the `scope`). `POST /queues/suggestions/{id}/approve` approves a queue draft: the local
    queues change at once, and what changed since is listed in `skipped`. The engine's
    `/suggestions/{id}/approve` refuses queue drafts (400).
  - `POST /queues/from-priority` with `{suggestion_id, queue_id | name, description?}` (exactly
    one of `queue_id` and `name`, else 422) turns a priority draft's order into a new queue, or
    appends it to one.
  - `GET /themes`: every theme with its number of issues and `publishable`, by catalog tag.
    `GET /themes/{tag}`: its issues, from the cache (the theme as a filter; 404 when no issue
    has it; an old name of a renamed tag answers under the new one).
  - `POST /themes/{tag}/issues` with `{issues: [{repo, n}]}` gives cached issues the theme (201
    with the catalog `tag` and the issues `added`, for Undo). A near-duplicate of a catalog tag
    is a 409 (`ThemeNearDuplicateError`); without the kb service it is a 503
    (`ThemesUnavailableError`). `POST /themes/{tag}/issues/remove` with the same body takes it
    off (local; the catalog keeps the tag).
  - `POST /themes/{tag}/publish` with `{repo}` stages the label setup step and the label
    additions (201 with the staged changes; steps already in the tray are reused). A theme no
    issue has is a 404; a repo where no cached issue of the theme lacks the label is a 409
    (`NothingToPublishError`).
  - `GET /themes/{tag}/epic?repo&new_epic`: the plan turning the theme into an epic in `repo`,
    with its `epic` (`{kind: "create", title, body}`, or `{kind: "existing", repo, n}` when it
    reuses the theme's epic), children, what is skipped and `digest`; nothing is written. `POST`
    the same path with `{repo, digest, new_epic?}` approves it: a 202 with the batch (`origin`
    `epic`) and `Location`. A plan changed since, or with no children, is a 409, and so is an
    approval while an earlier epic batch for the theme is running or didn't finish
    (`EarlierBatchError`: retry or abandon that batch). Once the theme has an epic, the plan
    links under its latest one; a new epic is planned only with `new_epic` (ruling IR59), and
    without it a closed epic is a 409 (`ThemeEpicClosedError`: reopen it, or ask for a new one).
  - The tray (`GET /pending`) shows a label setup step with `n` `null` and
    `change.kind` `ensure_label`, and a label addition as `add_label`.
  - Saved filters: `incomplete`, `stale_90d`, `human_needed_could_be_ready` and
    `possible_duplicates`. The last two read the open suggestions.
  - Typed errors come back as `{error, detail}`. A GitHub rate limit met by a request itself
    (a sync or a preview, not a batch) is a 503 with `Retry-After`, so the response is never
    stored under the key.
- **MCP, read-only:** `issues_search {q, repo?}`, `issues_get {repo, n}`, `queues_list {}` and
  `queue_get {queue}` (an id or a name). They read the cache and the local queues only; agents
  can't change GitHub or queues. Every value an issue's author wrote comes back inside
  `<untrusted-data>` fences, and so do queue names, descriptions, notes and warnings.

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
  nothing moves (ruling IR39). A link already in place on GitHub ("already the parent") is
  done, never a conflict: nothing changes, so nothing is overwritten. A reorder whose child has
  left the epic is skipped with a cause, never sent to GitHub (IR41).
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
    cause, except a group or split draft, which creates issues: its batch is retried or abandoned
    (approving it, or a re-asked draft for the same target, again is a 409, `EarlierBatchError`,
    IR56, IR57).
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
  - A close is checkpointed the same way. A close that reached GitHub before the cut-off counts
    as the batch's own, so its comment still follows.
  - If that checkpoint can't be saved, nothing is sent and the batch stops ("couldn't record
    progress; nothing was sent"). A save after a step is best effort: it is logged and the
    batch goes on.
  - A replay of the key answers the batch as it stands and never runs it again. It resumes only
    a batch still `running` that no task holds.
  - A close's comment withheld because the issue was already closed stays withheld on Retry.
  - An unexpected error fails only its step, with its type, and the batch goes on.
- **Claims:** an approval's (or its Retry's) claim is released whatever happens: done, failed,
  cancelled or broken. While it is held, editing or dismissing the draft is a 409.
- **Cache:** afterwards the touched issues, the labels and (for a new area) the recomputed triage
  are written to the cache, best effort. A failure there, or in storing the batch, is logged with
  its cause and never fails the writes. Issues the batch created arrive with the next sync.

## How epics stay safe

- **Containment (IR17):** `can_contain` returns a refusal or nothing, never a string-or-bool. It
  refuses an issue into itself, into one of its own descendants (a cycle, from the cached parent
  chain), another owner's issue (GitHub's sub-issues need the same owner) and an initiative into
  an epic. Staging such a parent link is refused with the reason (`ContainmentRefusedError`), and
  nothing is staged.
- **One batch (IR27):** a conversion, an approved group and an approved split each apply as one
  batch through the engine: the new issues first (`CreateIssue`), then each `SetParent` naming
  them by temp id. Every link is made against "no parent", so a parent set on GitHub since is a
  conflict, never a move. New issues carry no labels: a label is never created implicitly (IR4).
- **Created once (IR56–IR58):** every origin that creates issues records its batches in
  `batch_origins`, keyed by the target (the issue split, the group's members, the epic, the theme
  and repo), so re-asking the agent for a new draft about the same target meets the same ledger.
  The row is written with the batch's first save, in one transaction. While an earlier batch for
  the target is running or didn't finish, approving again is refused (409, "retry batch X, or
  abandon it"): Retry resumes it without repeating a create, and Abandon gives up on it (for one
  that can't complete, or was pruned) while keeping what it created. A complete batch's issues
  are reused and the origin settled: the draft whose batch it was is marked approved, and
  another draft for the same target is refused ("split already by batch X") and left open. An
  abandoned batch's issues are reused by the next approval, which creates only what is missing.
- **One epic per group of issues (IR60):** a new group that shares issues with another group
  is refused while that group's batch is running or unfinished (409, `EarlierBatchError`: it may
  have made an epic; retry or abandon it), and when that group's epic exists and is open (409,
  `GroupEpicExistsError`: add the issues to that epic), so overlapping members never get a
  second epic. A blocking epic is read live first: one deleted or transferred on GitHub is
  forgotten and blocks no more. An abandoned group that made no epic, or whose epic is closed,
  doesn't block.
- **Never twice:** the checklist stays in the body, so each item a conversion creates is
  recorded, before any GitHub write. A later preview matches it to the issue it became ("already
  converted"), or holds it back while that is unknown: an earlier batch still running, one that
  didn't create it (retry that batch), or one whose record is gone (check GitHub). An item an
  abandoned batch never created is planned anew. Conversions of one epic run one at a time.
- **Approve what was shown:** a conversion is approved by the digest of the plan the author saw;
  a plan that changed since (an edited checklist, a linked issue moved) is refused. A group or
  split draft is re-checked against the cache when approved: an issue closed, gone or under a
  parent since makes it stale (409), and nothing is applied.
- **Untrusted text is posted as code (IR49):** every piece of text taken from issues or written
  by the agent that Jesinia posts (a brief's titles, scope, readiness and criteria; the title and
  body of an epic or part an approved group or split creates) goes inside a CommonMark code span
  (one line) or fenced code block (several), whose fence is longer than any backtick run in the
  text, so the text can't close it. GitHub makes no mention, link, issue reference or entity of
  anything in code, however it is encoded (`@x`, `&#64;x`, `&commat;x`). Checklist items keep
  their words as the titles of the issues a conversion creates (titles never notify anyone).
- **Drop checks see the cached chain:** when an ancestor isn't cached (or the chain is deeper than
  8), the drop check warns "ancestors beyond … aren't cached; GitHub will check".
- **Agent asks (IR37):** grouping, splitting and briefs ask through `SuggestService`'s one-shot
  facility, with the issues fenced as untrusted data. Replies are validated against
  `schemas/grouping.json` or `schemas/brief.json`, with one retry. Jesinia keeps only issues it
  listed, and a reply about issues that changed during the ask is discarded ("inputs changed,
  ask again"). A brief's children, their order and links are Jesinia's own; the agent writes only
  the scope and the acceptance criteria.
- **The brief comment (IR8, IR48):** each attempt's marker carries a fresh nonce
  (`<!-- jesinia:brief:<nonce> -->`). Posting a new brief comment is checkpointed `in_flight`,
  with that nonce, first. If that can't be saved, nothing is sent. A post a stop or crash cut off
  is found again among the epic's comments, and only a comment by the signed-in gh user carrying
  that exact nonce is adopted (the earliest), then edited. Another user's marker, or another
  attempt's, is never adopted. If the comments can't all be read, nothing is posted
  (`BriefRecoveryIncompleteError`, 409: check GitHub).
- **Briefs edited on GitHub (M1):** before editing the stored comment, it is read live. One
  edited on GitHub since it was posted is refused (409, `BriefEditedOnGitHubError`) unless the
  approval says `overwrite`; one gone is posted anew. The body an edit is on its way with is
  checkpointed first (if that can't be saved, nothing is sent), so an edit GitHub took whose
  answer was lost reads as ours, not as an edit on GitHub, and is settled on the next approval. A stored comment deleted on GitHub
  (404) is posted anew and its id kept. A rate limit refuses the write, backs syncs off, and is a
  503 with `Retry-After`.

## How queues stay safe

- **Local means local (IR15):** a queue, its order, its notes and approved queue proposals never
  reach GitHub. `QueueService` holds no GitHub client.
- **What reaches GitHub goes through the tray:** Done and band moves stage ordinary changes
  (`Close`, or a `SetDimension` of readiness or priority) with `source` `queue`. They apply only
  when the author applies the tray, through the write-ahead engine. A band move is made against
  the priority the cache shows (filling a gap, or replacing that one value), so a change on
  GitHub since is a conflict at apply time, not an overwrite. A second band move before the
  tray is applied replaces the first one's staged priority (keeping its precondition), and a
  move back to the issue's own band undoes it, so the issue gets one priority change. A band
  move into another band never overrides a priority the author staged from the tray (a picker,
  a menu): it is refused (409, `TrayChangeStagedError`: undo that change there first). A move
  within the item's band only reorders, locally, and is always allowed.
- **Undone from the queue:** the tray refuses to undo a change a queue item's Done holds (409,
  `PendingHeldError`), and "undo all" leaves those; undo the Done in its queue. If the tray's
  apply fails for that change (a conflict, a lost permission), the item still shows Done: undo
  it in its queue, and Done it again once the cause is fixed.
- **One action:** Done (and a band move) stages first and keeps the change only if the queue
  is saved; if it can't be, the change is unstaged.
- **One close per issue:** the engine applies one close and one comment per issue, so Done in a
  second queue shares the close another queue already staged for that issue (and agent-ready
  likewise). A second, different comment is refused (409) rather than silently replacing the
  first, and so is Done on an issue whose close the author staged from the tray. A queue only
  ever shares or unstages changes a queue staged (`source` `queue`).
- **Guards:** agent-ready is staged against the readiness the cache shows, like a band move's
  priority: a change on GitHub since is a conflict at apply time, not an overwrite.
- **Staged changes follow their items:** Undo, removing a done item and deleting a queue take the
  change Done staged out of the tray, unless another queue's done item still shares it.
- **Approvals are atomic:** an approved split saves every queue it touches in one transaction.
- **Posted text:** the only text a queue posts is the author's own Done comment, as written. No
  issue text or agent text is posted from a queue.
- **Auto-done never breaks sync:** it runs inside sync's (and the engine's) best-effort hook and
  catches its own failures, logging the cause.
- **Agent asks (IR37):** issue text and the author's queue names are fenced as untrusted data;
  replies are validated against `schemas/queue.json` with one retry; only items and queues
  Jesinia listed are kept, and a reply is discarded when a queue or one of its issues changed
  during the ask ("inputs changed, ask again").

## How themes stay safe

- **Local means local (IR15):** a theme is not a `Change`. Adding and removing one write only
  `issue_themes` (and, for a new tag, the KB catalog); `ThemeService` holds no GitHub client.
- **A drop never waits on an agent:** a new tag is checked against the catalog alone
  (`advise=False`); the tag advisor (an agent run) is never asked.
- **All or nothing:** every issue must be cached, and the tag must pass the check, before
  anything is stored or catalogued. A catalog failure keeps its cause
  (`ThemeCatalogError`, chained).
- **Undo is local (IR22):** it removes the theme from the issues only. The catalog entry stays,
  consistent with Notes.
- **Labels go through the tray:** publishing stages the `theme:<tag>` setup step and the label
  additions; nothing reaches GitHub until the author applies the tray. The write-ahead engine
  applies them with its guards (a fresh read per issue, an add/remove against it, the label read
  live before each add). The label is created only by the staged setup step: if the author undoes
  it, the additions fail with `MissingLabelError` and nothing is created (IR4). A publish that
  can't stage every change takes back what it staged.
- **One epic per theme (IR56, IR59):** a theme epic is one approved batch keyed by the digest of
  the plan the author saw (IR27, IR45, IR46). The epic is created with no labels. Another repo's
  issue gets the "won't fit one PR" warning and another owner's is refused (IR17). An earlier
  epic batch for the theme (in that repo) that is running or didn't finish refuses a new approval
  (409): it may have made the epic, so it is retried or abandoned (an abandon may confirm an epic
  found on GitHub, which is then reused). Once a batch made the epic (recorded in
  `theme_epics` and the ledger, so pruning doesn't forget it), issues added to the theme later
  link under the theme's latest epic, made by its newest complete or abandoned batch (read from
  the ledger, so a lost `theme_epics` record never hides it). A new epic is made only when the
  author asks for one (`new_epic`); a closed latest epic without it is refused, never replaced
  silently. Asking `new_epic` again after an abandoned batch made an epic that is still open
  reuses that epic: only what is missing is created. The batch is recorded for the theme on a replay of the
  key too, so a record the first request couldn't write isn't lost.
- **Catalog renames and merges (IR54):** every theme call resolves the stored tags through the
  KB catalog's aliases and rewrites the rows to the canonical name, so a renamed or merged tag
  lists and filters under its new name, and the old name still finds it. A theme's epic history
  (`theme_epics` and its ledger origins) is renamed too, even after its last issue left it, so a
  renamed theme reuses the epic it made. Without the catalog (or when it can't be read, logged
  with the cause) the stored names are used.
- **Long tags:** a tag whose `theme:<tag>` is longer than GitHub's 50-character label limit is a
  local theme and filter only: `publishable` is false in `GET /themes`, and publishing it is a
  400.
- **Posted text (IR49):** the epic's title and body carry the tag only as code.

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
`test_labels.py` `FakeGitHub`, `test_apply.py` `FakeWrites`) and a scripted agent
(`test_suggest.py` `FakeAgent`), with a fake clock, so they make no network calls and never
sleep.
