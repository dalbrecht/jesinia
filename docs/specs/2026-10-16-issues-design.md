# Jesinia — Issues Design

- **Status:** Approved (2026-10-16)
- **Date:** 2026-10-16
- **Author:** Don Albrecht (with Claude)
- **Builds on:**
  - the foundation spec (Implemented);
  - the code review spec (`2026-10-16-code-review-design.md`), for its GitHub reads and
    writes, repo workspace and idempotent posting;
  - the RSS spec's KB-wide tags and idempotency keys;
  - the design pass (`docs/design/wireframes/design-pass-2026-10-16.md`);
  - the Issues wireframe (`docs/design/wireframes/issues.html`).

Decisions marked **(user)** came from the author in the design conversation. Decisions marked
**(builder's call)** were made while writing this down and are open to change in review.

## 1. Intent

The author's issue work has three parts **(user)**:

1. **Triage new issues** as they arrive across their repos.
2. **Re-triage existing issues:** the backlog.
3. **Prioritise work and build work queues.**

Most work is done by **coding agents**, some by the author **(user)**. Today the author assigns
work as an **issue, a milestone, or usually an epic**, to be delivered as a **single PR**
**(user)**.

Handing work to agents (dispatch) is **a later, separate tool (user)**. Issues stops at a
prioritised, ready-to-hand-off queue.

The agent **suggests labels and priority. The author approves before anything changes.** The
agent also helps group issues into epics and prioritise and organise work queues **(user)**.

### A label convention to formalise (user)

amicus-platform has a working convention that isn't fully fleshed out (observed 2026-10-16
across 235 open issues):

- **Readiness:** `agent-ready`, `human-needed`, `information-needed`, `design needed`,
  `blocked`, `icebox`.
- **Priority:** `priority: high|medium|low`.
- **Type:** `bug`, `enhancement`, `tech-debt`, `test`, `performance`, `security`,
  `documentation`, …
- **Area:** inconsistently named. There are `area:*` and `platform:*` labels, bare app names
  (`wiki`, `tasks`…) and bare technologies (`rust`, `apple`…).
- **Hierarchy:** `initiative`, `epic`, with epic items kept as body checklists.
- **Origin:** `review:*` and `campaign:*`.

Gaps: no issue is assigned, 10 have no labels, 11 have no priority, 10 have no readiness, and
`agent-ready` issues often lack priority and area.

### Success

- Every open issue in a watched repo has a complete label set (§2), kept complete as new
  issues arrive.
- The author can turn a pile of issues into a few ordered queues of agent-ready epics and
  issues in one sitting.
- Nothing on GitHub changes without the author's approval.

### Non-goals

- Dispatching work to agents or tracking their progress. That's the later dispatch tool.
- Replacing GitHub's issue UI for other people.
- Project boards, sprints or estimates beyond priority and order.

### Anti-goals

- **Silent changes:** a label, epic or close on GitHub the author didn't approve.
- **Convention drift:** Jesinia applying labels outside the convention, or making up new label
  names.
- **A queue full of work an agent can't do:** non-ready items in an agent queue without a clear
  warning.

## 2. The label convention

Jesinia formalises the convention as a **per-repo label schema**, with a shared default
**(builder's call)**.

**Dimensions:**

| Dimension | Cardinality | Default values |
|---|---|---|
| Readiness | exactly 1 | `agent-ready`, `human-needed`, `information-needed`, `design-needed`, `blocked`, `icebox` |
| Priority | exactly 1 | `priority: high`, `priority: medium`, `priority: low` |
| Type | exactly 1 | `bug`, `enhancement`, `tech-debt`, `test`, `performance`, `security`, `documentation` |
| Area | 1 or more | per repo, prefixed `area:` |
| Parent | 0 or 1 | the epic (a GitHub sub-issue parent) |

Origin labels (`review:*`, `campaign:*`) are free-form and kept as they are.

**Where the schema lives:**

- in the repo at **`.jesinia/labels.yml`** when present (read-only to Jesinia, like
  `.jesinia/review.md`);
- otherwise in Jesinia's settings per repo, starting from the default above.

**Mapping existing labels:**

- Existing labels that mean the same thing are listed as aliases. For amicus-platform:
  `design needed` → `design-needed`, `platform:ios` and `apple` → `area:apple`, `wiki` →
  `area:wiki`.
- **Migrating labels** (renaming them on GitHub) is offered as one previewed, approved batch
  per repo, never done silently **(builder's call)**.

**Completeness:** an issue is *complete* when every exactly-1 dimension has one value and Area
has at least one. Incomplete issues are what triage works through.

## 3. Triage

### 3.1 Inbox (new issues)

- **What's in it:** new issues across watched repos, plus any open issue that becomes
  incomplete or changes after it was triaged.
- **Suggestions:** the agent suggests, for each issue:
  - a value for each missing dimension, each with a one-line reason;
  - likely duplicates, with links;
  - the epic it probably belongs to;
  - for readiness `agent-ready`, what's missing if it isn't ready yet.
- **Approval (user):** suggestions are **drafts**. The author can:
  - accept, edit or dismiss each suggestion;
  - **Approve all** for one issue;
  - approve in bulk across several issues, from the list's multi-select.
- **Applying:** approving applies the change to GitHub as a single write per issue. The
  author's manual actions (adding a label, closing an issue) are approvals too.
- **Suggestion timing (builder's call):** suggestions are generated in the background when a
  new issue arrives (concurrency 2, debounce 5 minutes for edits), and for older issues on
  demand.

### 3.2 Backlog (existing issues)

Re-triage works through the backlog in batches. **(builder's call):** it uses saved filters,
such as "incomplete", "stale: no activity in 90 days", "`human-needed` that could be
`agent-ready`" and "possible duplicates". Suggestions and approvals work as in the inbox.

## 4. Epics

- **Native sub-issues (builder's call):** epics use GitHub's native **sub-issues** for their
  children, so progress can be computed. Existing checklist epics (amicus-platform) can be
  converted:
  - the agent proposes the child issues from the checklist, matching existing issues where
    the items link to them, and drafting new issues for unlinked items;
  - the author approves the conversion as one batch;
  - the checklist stays in the body until then.
- **Grouping (user: assist):**
  - the agent proposes groups of related, open, ungrouped issues as candidate epics, with a
    title and a scope paragraph;
  - it proposes adding issues to existing epics;
  - it proposes splitting an issue that's too big for one PR.

  Every proposal is a draft to approve.
- **Readiness:** an epic is **ready** when every child is `agent-ready` and complete. Its page
  says which children block it and why.
- **One epic, one PR (user):** the epic page carries a **delivery brief**, the text a future
  dispatch tool or the author hands to an agent. It holds the scope, the children in order,
  acceptance criteria drawn from the children, and links. Jesinia generates it; the author
  edits and approves it. It's stored on the epic as a comment marked `<!-- jesinia:brief -->`
  **(builder's call)**.

## 5. Prioritising and work queues

### 5.1 Priority (user: assist)

- **Priority is a GitHub label** (§2).
- **Suggestions:** for a set of issues, the agent proposes priorities, plus an order within
  each priority. Each comes with its reasoning: dependencies, impact, age, and blockers it
  unblocks. The author can disagree with any single step.

### 5.2 Work queues (user: local, epics and issues, a few named)

A **work queue** is a named, ordered list of **epics and single issues**, stored **locally in
Jesinia** in `issues.sqlite`, not on GitHub.

- **Examples:** "Agents: this week", "Mine", "Launch". A few queues, each with an optional
  description.
- **Items:** any epic or issue from any watched repo. Each item keeps its order, an optional
  note, and when it was added.
- **Readiness warnings:** each item shows its readiness. A non-ready item in a queue gets a
  warning saying why (anti-goal). It isn't hidden or blocked.
- **The agent's help (user):**
  - "Suggest next": proposes items to add from ready, high-priority work, with reasons;
  - "Order this queue": proposes an order using dependencies (sub-issue and "blocked by"
    links) and priority;
  - "Split across queues": proposes which queue each item fits.

  All are drafts to approve.
- **Hand-off (later):** the queue is the input the future dispatch tool will read. Until then,
  each item's **Copy brief** copies its delivery brief for the author to give to an agent by
  hand **(builder's call)**.
- **Re-prioritising in a queue (user):**
  - A queue can be shown flat or **grouped by priority** (high, medium, low bands).
  - Dragging within a band reorders the item.
  - Dragging it to another band also changes its GitHub priority label, as a pending change
    (§6.1).
  - Queue order is local and applies at once.
- **Marking human tasks done (user):**
  - Items that are the author's own (`human-needed`, or in a queue like "Mine") have a **Done**
    action: a checkbox on the row, the `x` key, or dragging onto a Done zone.
  - Done marks the item done in the queue at once. It also stages **closing the issue on
    GitHub, with an optional comment**, as a pending change.
  - If the author's part is only one step, they can instead stage **"set readiness to
    `agent-ready`"**, so an agent can take over (builder's call).
  - A done item moves to the queue's collapsed Done group, with Undo.
- **Queue items close themselves:** when an item's issue or epic closes on GitHub, it's marked
  done and moves to a collapsed Done group **(builder's call)**.

## 6. The app (`/issues/`)

The app follows the design-pass patterns:

- **Workspace tabs:** **Inbox**, **Backlog**, **Epics** and **Queues** are fixed tabs, and each
  opened issue, epic or queue gets its own tab.
- **Inbox and Backlog:**
  - The default is the list view, a compact list with multi-select and a bulk bar ("Suggest
    with Claude", "Approve all"). The wireframe's **board** (columns by readiness) is a view
    toggle.
  - Selecting an issue opens it in the **book layout**:
    - outline on the left: summary, timeline, suggestions, related and duplicates, the epic;
    - a toolbar on top: readiness, priority, the repo, Open on GitHub;
    - suggestions with Accept, Edit and Dismiss;
    - **Approve** and **Dismiss all** along the bottom.
- **Epics:**
  - a list with progress (children done of total) and readiness;
  - an epic opens in the book layout, with the outline listing its children in order, then the
    delivery brief and then proposals.
- **Queues:**
  - section tabs, one per queue, with counts;
  - each queue is an ordered list you can drag to reorder (§6.1);
  - readiness chips on every item, warnings on non-ready items, and Copy brief per item;
  - the agent's proposals (suggest next, order) appear as a draft diff to approve.
- **Chat:** scoped to the open issue, epic or queue (foundation `agents`), as in the
  wireframe's triage drawer.
- **Settings → Issues** uses the Settings outline: watched repos, the label schema per repo
  with its aliases and migration, and the suggestion runner.

### 6.1 Drag and drop (user: robust)

Drag and drop is a primary way to work in Issues, not an extra.

**What can be dragged:** one issue or epic, or a multi-selection, dragged together.

**Drop targets:**

| Drop on | Effect |
|---|---|
| A board column (readiness, priority or status view) | sets that label |
| An epic (in the Epics list, or an epic's outline) | adds the issues as sub-issues |
| A position within an epic's outline | reorders its children |
| A queue tab or a position in a queue | adds to the queue at that position, or reorders it |
| Another priority band in a grouped queue | reorders it and sets that priority label |
| A queue's **Done** zone | marks the item done (§5.2) |
| A theme (below) | adds the theme to the issues |
| **Remove** zones (out of an epic or queue) | removes |

**Themes (user: group issues by dragging).** A theme is a named group the author builds by
dragging issues onto it, such as "Onboarding polish" or "Sync reliability". **(builder's
call):** themes are **tags from the KB-wide catalog** (RSS spec §5), stored locally per issue
in `issues.sqlite`, so the same `#tags` the author uses in notes and kept posts also group
issues. A theme can be turned into an epic (its issues become the children) or published as a
GitHub label, each as an approved change. Themes appear as a column of drop targets beside the
list and the board, and as a filter.

**Pending changes, not instant writes.** Drags are the author's own actions, but drops that
would change GitHub (labels, sub-issues, order within an epic) collect in a **Pending changes
tray** at the bottom. Undo works per change. **Apply (⌘↵)** sends them as one batch, one write
per issue, so a slip of the mouse never changes GitHub **(builder's call, from "I approve before
anything changes")**. Local-only drops apply at once, with Undo: queues, queue order and themes.

**Robustness requirements:**

- **Feedback while dragging:** a drag preview showing the count ("3 issues"), visible drop
  indicators (an insertion line for positions, a highlight for targets), and a "can't drop
  here" state with the reason, such as an epic can't contain itself.
- **Long lists:** auto-scroll near the edges of scrolling lists and boards.
- **Every input:** works with mouse, trackpad and touch (long-press to lift on narrow screens).
  Every drag has a **keyboard equivalent**: Space to pick up, arrows to move, Space to drop,
  Esc to cancel, announced to screen readers. Each also has a menu equivalent ("Move to epic…",
  "Add to queue…", "Add theme…").
- **Conflicts:** if GitHub changed underneath (the issue moved epics elsewhere), the pending
  change shows the conflict instead of overwriting it.

**Library (builder's call):** use a maintained, accessible drag-and-drop library with keyboard
and touch sensors, such as `@dnd-kit` (MIT), wrapped once in `@jesinia/design` as `Draggable`,
`DropTarget` and `SortableList`, so every app shares it.

## 6.2 Rules settled during the builder check (builder's calls)

- **Build order:** Issues is built after RSS PR 1 (the core idempotency keys) and review PR 1
  (GitHub writes with `require_owner`, recorded fixtures). It reuses both and adds only
  issue-specific writes. Agent JSON jobs reuse the review tool's job runner once that lands;
  until then Issues uses the same pattern in its own module.
- **Epics:**
  - An issue is an epic when it has the `epic` label **or** has sub-issues. When the two
    disagree, Jesinia suggests adding the label.
  - `initiative` sits above epics, with the same rule and its own label.
  - An epic needs **priority** but not type or area. Its readiness is derived: ready when every
    *direct* child is ready.
  - Children in another repo **under the same owner** are allowed but get a warning, "won't fit
    one PR". Children from another owner are refused, since GitHub's sub-issue API requires
    the same owner. A drop like that shows "can't drop here" with the reason.
- **Applying an approval:**
  - Each approval runs as one **ordered series** of calls for that issue, in this order:
    1. labels;
    2. parent epic link;
    3. sub-issue order (if any; a reorder within an epic, §6.1);
    4. close (if any);
    5. comment (if any).

    Step 3 is a spec-compatible extension recorded at plan acceptance (ruling IR13): §6.1 already
    stages reorders as GitHub writes. Before any issue's series, a batch runs its repo-level
    steps: creating a label an approval adds (a new area, created only by that approval), then
    creating any new issue the batch's other changes name.
  - Labels are applied as **add/remove against a fresh read**, never by replacing the whole
    set, so a change someone made on GitHub since the last sync isn't wiped.
  - The series stops at the first failure, keeps what succeeded, and shows exactly which steps
    applied and which didn't, with the cause, so it can be retried. Replace §3.1's "single
    write" with this.
- **Triage state:**
  - "Triaged" means **complete** (§2), however the labels got there, including directly on
    GitHub.
  - The **Inbox** holds issues opened after the repo was added, plus any issue that *becomes*
    incomplete. Comments and body edits don't send an issue back.
  - When a repo is first added, its existing issues go to the **Backlog**, not the Inbox.
- **Labels on GitHub:**
  - An alias counts toward completeness.
  - Jesinia writes the label name that already exists on GitHub until the repo is migrated.
  - Jesinia never implicitly creates a label. Canonical labels are created in an approved
    **setup batch** per repo.
  - **Migration** relabels affected issues and then deletes the old labels, as one previewed,
    approved batch.
- **New areas:** the agent may propose an area that isn't in the schema yet, marked *new*. It
  joins the schema, and is created on GitHub, only when the author approves (as with tags).
- **Order:** only queues hold an order. A priority suggestion's ordering is a draft the author
  turns into a queue, or discards.
- **Board views:**
  - **Readiness** and **priority** columns are drop targets, and dropping sets that label.
  - **Status** (Triage, Backlog, In progress (has a linked open PR), In review, Done) is
    derived and read-only, with no drops.
- **Which actions write at once:**
  - **Approving a suggestion** applies at once, since the approval is the go-ahead.
  - **Every other GitHub-changing action** stages in the Pending changes tray: drags, the
    toolbar pickers, menu actions and the bulk bar. One apply engine serves both.
- **Milestones:**
  - A milestone can be a **queue item and a filter**, standing for its open issues, with
    readiness rolled up and a brief built from them.
  - Jesinia never writes milestones.
- **Theme labels:**
  - Publishing a theme creates a `theme:<tag>` label. `theme:` is an allowed free-form prefix,
    like `review:*` and `campaign:*`.
  - A new theme passes the tag catalog's instant, catalog-only near-duplicate check, with no
    agent involved, so a drag never waits on a runner.
- **Assignees:** not suggested or written. Assignment belongs to the later dispatch tool.
- **Duplicates:** accepting a duplicate closes the issue as a duplicate (`state_reason: duplicate` with
  `duplicate_issue_id`) and a comment linking the original, as an approved change in the series above.

### 6.3 Always cached

Like the review queue, every synced issue, label, milestone, sub-issue link, comment and draft
lives in `issues.sqlite`. Views render from it immediately and refresh in the background, with
"Updated N min ago · refreshing…" shown. If GitHub is unreachable, the cached data stays
visible under a banner.

## 7. Architecture

- **The `issues` app module** (`server/jesinia/apps/issues/`, owning `issues.sqlite`), layered
  like the others:
  - sync of issues, labels, milestones and sub-issues per watched repo, with an ETag-aware
    cache via `github`;
  - suggestions as stored drafts;
  - the label schema per repo;
  - epics, briefs and queues.
  - REST under `/api/issues/`;
  - read-only MCP tools `issues_search`, `issues_get`, `queues_list` and `queue_get`.
  - Agents can't change GitHub or queues.
- **The `github` service** gains issue writes:
  - labels (add and remove) as one write per issue;
  - sub-issues (add, remove, reorder);
  - creating an issue, closing an issue, and posting or editing a comment.
  - Writes refuse session principals, and every mutating route uses idempotency keys.
- **Suggestions:**
  - agent jobs with JSON outputs validated against schemas, like the review tool's jobs;
  - issue text and comments are `UntrustedDoc`s;
  - agents never get write tools.
- **The PWA:** `web/apps/issues/`, using the Issues icons already exported, with
  `data-density="compact"` (ADR 0008).

## 8. Errors

- **GitHub auth missing:** the wireframe's blocking state.
- **A write rejected by GitHub:** for example the label doesn't exist, or there are no rights.
  The ordered series for that issue stops at the failing step (§6.2). Earlier steps that
  succeeded are kept and reported, the failed and remaining steps are shown with the cause,
  and Retry runs only those.
- **The runner is unavailable:** manual triage keeps working, and suggestions say they're
  waiting.

## 9. Testing

- **Python:**
  - schema completeness rules and alias mapping;
  - suggestion drafts and the approve-to-write path, with one write per issue;
  - converting checklist epics to sub-issues;
  - epic readiness;
  - queue ordering, done-on-close, and readiness warnings;
  - idempotent writes;
  - agent output validation and untrusted text.
- **Web:** list and board, the book layout, approve and dismiss, Copy brief, and drag and
  drop for every target in §6.1, by pointer and by keyboard. That covers the pending-changes
  tray with undo and apply, conflict display, auto-scroll, and multi-select drags.
- **e2e (drag and drop):** Playwright drags two issues onto an epic and one onto a theme,
  reorders a queue, and applies the pending changes in one batch.
- **e2e:** against a fake GitHub server and fake runner: a new issue arrives, suggestions are
  approved, and the labels land in one write; then two issues are grouped into an epic and
  added to a queue, and the queue is reordered.

## 10. Left to the plan

- The suggestion and grouping prompts and their schemas.
- Sync intervals.
- The exact saved-filter definitions.
- PR breakdown. Likely order: github issue writes and sync, then the label schema and triage,
  then epics, then queues, then the app.

## 11. Acceptance (2026-10-19)

Run on the author's Mac against the auto-updating install (`~/apps/jesinia`, port 5374), driven
by Claude in the browser; the author approved every GitHub write. Shipped in #55, #61, #65 and
#72.

| Step | Result |
|---|---|
| 1. Watch amicus-platform | 1648 open issues synced into the Backlog with no agent sessions started. |
| 2. Setup and alias mapping | Following §2's example, `design needed` → `design-needed`, `apple` and `platform:ios` → `area:apple` (new area) and `wiki` → `area:wiki` (new area) were saved to Jesinia's settings and survived a reload; setup then reported "Nothing to do: every canonical label exists", so no label was created on GitHub. |
| 3. Triage 5 Inbox issues | Not checked yet: the Inbox was empty (issues open at watch time land in the Backlog), and the 5 new issues it needs haven't been filed. |
| 4. Convert a checklist epic | #1760's 7 checklist items were previewed, approved and linked as sub-issues in one paced batch; the epic then listed them in order, "2 of 7 done · 5 blocking", each open child with its readiness warning. |
| 5. "Agents: this week" queue, Copy brief | The queue was made locally and filled with 3 Backlog issues, each showing what keeps it from ready; dragging #1763 to the top reordered it with Undo offered. *Copy brief* built the brief, but the browser pane refused the clipboard and the queue gives no way to read the text then (#75). |

Follow-ups: #75.
