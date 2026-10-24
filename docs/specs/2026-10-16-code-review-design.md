# Jesinia — Code Review Design

- **Status:** Implemented (acceptance in §13)
- **Date:** 2026-10-16
- **Author:** Don Albrecht (with Claude)
- **Builds on:**
  - the foundation spec (`2026-10-16-jesinia-foundation-design.md`, Implemented);
  - the RSS spec's KB-wide tags (`2026-10-16-rss-reader-design.md` §5);
  - ADR 0008;
  - the Review wireframe (`docs/design/wireframes/review.html`).

Decisions marked **(user)** came from the author in the design conversation. Decisions marked
**(builder's call)** were made while writing this down and are open to change in review.

## 1. Intent

The author reviews about **100 pull requests a week** from three sources: themselves, coding
agents, and other developers. The volume has grown sharply with AI **(user)**. The tool should
let them keep up without the quality of what they approve slipping.

What they need **scales with the depth and complexity of the PR (user)**:

- **Simple PRs:** an approve/reject recommendation, a quick explanation of the change, and a
  simple guided walk through the code.
- **Larger PRs:**
  - why the PR is needed, with its purpose explained in business terms;
  - a walkthrough of how the new code works and how it interacts with the existing system,
    including how the existing system works when they don't already know it;
  - a walk through the changes;
  - the AI review's findings, ready before they look;
  - follow-up questions;
  - a quiz to make sure they understand how the PR works and how they'll interact with it.
- **Follow-ups (user):**
  - **Rebases** of something already approved need no action.
  - **Re-reviews** after the author addressed feedback build on the previous walkthrough: how
    the new changes address each request, with the previous review viewable in context.
- **Stacks (user):** a stack of PRs can be reviewed as a single unit.
- **Own PRs (user):** the author can review PRs they wrote themselves.

### Success

- The author can work through about 100 PRs a week. Rebases and small clean PRs take seconds,
  and attention goes to the deep ones.
- Every recommendation is backed by evidence the author can inspect.
- The author's understanding of the systems they own stays current. Walkthroughs adapt to
  what they already know.

### Non-goals

- Replacing GitHub's PR UI for authors or other reviewers. Jesinia is the author's own
  review cockpit.
- Auto-merging, or posting anything without an explicit click (foundation roadmap).
- Reviewing code hosts other than GitHub.

### Anti-goals: what would end the author's trust (user)

- **Approving PRs that break things, or missing obvious bugs.**
- **Code quality deteriorating** over time without anyone noticing.
- **The system drifting from the author's mental model.**
- **Dropping either kind of issue:** style and smell problems, or higher-level design
  problems.

Each has a safeguard in §5.

## 2. The queue

At this volume the entry point is an **inbox, not a PR page (builder's call, from the user's
volume)**. It extends the wireframe's PR picker:

- **Always cached (user):** every PR in the queue, and everything Jesinia has fetched or
  generated for it, is kept locally in `review.sqlite`, so the UI never appears empty while
  connecting to GitHub. That covers metadata, CI state, review counts, files and diff, rounds
  and artifacts.
  - The queue and any opened PR render from the cache at once, then refresh in the background.
  - The header shows "Updated N min ago · refreshing…", and rows update in place.
  - An opened PR whose head moved shows its cached review marked **stale** until the refresh
    lands.
  - If GitHub is unreachable or auth is missing, the cached queue stays visible under a
    banner; it is never replaced by an empty or error page.

- **What's listed:** every open PR across the author's watched repos where their review is
  requested, they are the author, or they've reviewed it before. The repo list on the left
  filters it.
- **Each row shows:**
  - the number, title, author, author kind (you, agent or developer), size and age;
  - the **kind**: new, re-review, rebase, or part of a stack;
  - the **proposed depth**: quick or deep;
  - **CI state:** running, passing or failing (user);
  - **reviews already on the PR (user):** whether automated reviewers (Copilot, Codex and
    similar) and humans have commented yet, with the number of review comments;
  - the **pre-review state**: waiting for CI, waiting for automated reviews, queued, ready,
    or failed with its cause;
  - the **recommendation** with its evidence summary (CI, tests, finding counts by group).
- **Pre-review timing:**
  - **Gates (user):**
    - **CI first:** no review, background or on open, starts until CI on the head is
      passing. Until then the row shows **waiting for CI**. A failing CI shows the failure
      and offers **Review anyway**.
    - **Automated reviews first (user):** automated reviewers often make the first pass.
      When a repo has one configured, Jesinia waits for its review to land on the head
      before reviewing, with a timeout of 30 minutes (builder's call). It then reads those
      comments as input: it doesn't repeat them, and it says whether it agrees.
  - **How much runs in the background is a setting (user):** Settings → Review → *Pre-review*.
    1. **Triage**, the default (builder's call): the recommendation, correctness pass,
       findings and depth run in the background. Overview, Background, Walkthrough, Quiz and
       stack walkthroughs generate when the PR is opened.
    2. **Everything:** the full review is generated in the background.
    3. **On open only:** nothing runs until the author opens the PR.

    The setting can be overridden per repo.
  - **Debounce (builder's call):** a new head waits for 10 quiet minutes, then the CI gate,
    before a background run starts, so rapid agent pushes cost one review.
  - **Concurrency (builder's call):** at most 2 pre-reviews run at once.
  - **Order:** requested reviews first, then agent PRs, then the rest.
- **Rebases** of approved PRs that pass the check in §4.1 drop into a collapsed **Still
  approved** group. No action is needed.
- **Loading** is described under *Load a PR* below. Loaded PRs still respect the CI gate,
  with a **Review anyway** override.
- **Watched repos** are chosen in Settings → Review, starting from the repos where the author
  has review requests.
- **Load a PR (user):** the author can load any PR for review on demand, whether or not it's
  in the queue or a watched repo.
  - Ways to load: paste a PR URL or `owner/repo#123` into a **Load PR** field at the top of
    the queue, use ⌘K, or open `/review/<owner>/<repo>/<n>` directly.
  - A loaded PR is reviewed at once, ahead of background pre-reviews, and then stays in the
    queue under **Loaded** until it is closed or dismissed **(builder's call)**.

## 3. Reviewing a PR

### 3.1 Depth (user: scales with the PR)

Jesinia **proposes** a depth, and the author can switch it either way at any time.

- **Quick:** a small change in an area the author knows well.
- **Deep:** anything large, cross-cutting, new to the author, or agent-written and touching
  core code.

The default thresholds are 200 changed lines or 10 files, or a low familiarity score (§6)
**(builder's call)**.

**Quick review:**

- the recommendation, with its evidence;
- a two-to-four-sentence explanation of the change;
- a **guided walk** through the diff, one step per meaningful hunk group, in reading order;
- findings.

**Deep review** follows the wireframe's tabs:

- **Overview:** why the PR exists and what it's for, in business terms. Drawn from the PR
  body, linked issues, commit messages and KB pages about the domain.
- **Background:** how the parts of the existing system that the PR touches work today, with
  the code areas involved and links to the KB pages that explain the domain. Its depth comes
  from the knowledge model (§6).
- **Walkthrough:** a narrative of how the new code works and interacts with the system. The
  diff is beside it (about 60/40), and the current step's hunk is highlighted.
- **Findings:** the AI review, ready before the author looks, grouped as in §5.3.
- **Metrics:** scores against the applied standards (§5.2).
- **Quiz:** described below.
- **Recommendation:** described below.

**Walkthrough coverage (user):** every walkthrough, quick or deep, shows plainly how much of
the change it covers. Leaving parts out is often right (lockfiles, generated code, renames,
mechanical edits), but it must never be silent.

- **The coverage line:** a header line on the walkthrough, such as
  `Walkthrough covers 82% of changed lines · 14 of 17 files`. It is computed by Jesinia from
  the steps' hunk ranges against the full diff, not reported by the agent.
- **Not walked through:** a list of every file or hunk left out, each with its reason:
  generated, lockfile, vendored, rename only, whitespace or format only, or "judged
  mechanical by the agent". Each can be opened in place with **Show**.
- **Full walk:** **Walk everything** adds steps for the rest.
- **Shown everywhere it matters:** the percentage also appears in the queue row and the
  recommendation's evidence. The correctness pass reports its own coverage the same way
  (which changed lines it read) **(builder's call)**.

**Chat** for follow-up questions (user) docks under the walkthrough and is available from
every tab. It runs as an agent session with the PR, the repository and the review as context
(foundation `agents`).

**Quiz (user):**

- 3–5 questions, one at a time, with feedback after each;
- about how the PR works and how the author will interact with it;
- the result is recorded and feeds the knowledge model.
- **(builder's call):** the quiz never blocks approval and can be skipped.

### 3.2 Recommendation

- **The verdict:** Approve, Request changes, or **Needs a closer look**.
- **(builder's call):** the third verdict covers cases where evidence is missing, so the tool
  never guesses an approve.
- **The reasoning:** a short explanation, with an **evidence list**:
  - CI status per check;
  - the tests that cover the changed code, as found by the agent;
  - the result of the agent's correctness pass;
  - the finding counts.
- **Advisory only:** nothing goes to GitHub until the author clicks (§7).

### 3.3 Reviewing your own PRs (user)

The author's own PRs get the same review. **(builder's call):**

- Background is minimised, because they wrote the code.
- The quiz is off by default.
- The recommendation is phrased as **ready / not ready to request review**.

It works as a pre-flight check before asking others or agents to review.

## 4. Follow-ups and stacks

### 4.1 Rebases (user: no action needed)

When the head of a PR the author already **approved** changes, Jesinia checks whether the
change is only a rebase:

- **Same content:** the PR's own diff against its new base equals the approved diff against
  its old base, compared as **per-file patch IDs** (`git patch-id --verbatim`, so whitespace
  counts) **(builder's call)**.
- **CI:** CI on the new head is green, or still running.

If both hold, the approval is shown as **still approved**, with a one-line note. Nothing is
posted, since GitHub keeps or dismisses approvals by its own rules.

If the content differs, for example conflict resolutions, the PR becomes a re-review covering
**only the differing hunks**.

**(builder's call):** approvals posted directly on GitHub count too, including ones from before
Jesinia was installed. The approved diff is rebuilt from the review's commit and its merge
base.

### 4.2 Re-reviews (user: build on the previous review)

When a PR the author requested changes on, or commented on, gets new commits, the review is
incremental. This also works when the earlier review was posted on GitHub outside Jesinia
**(builder's call)**; with no stored walkthrough, it starts from that review's comments.

- **Your requests:** each of the author's previous comments and requested changes, with a
  status: **addressed**, **partly addressed**, or **not addressed**. Each has an explanation
  and links to the new hunks.
- **New since your last look:** changes the PR author made that weren't asked for, walked
  through as usual.
- **Previous review in context (user):** the previous walkthrough, findings and the author's
  comments are viewable beside or inline with the new review, aligned by file and hunk.
- **Familiarity:** the knowledge model counts the previous review, so background isn't
  repeated.

Every review round is stored (§8), so any number of rounds builds on the last.

### 4.3 Stacks (user: review as a single unit)

PRs form a stack when each PR's base branch is another open PR's head branch, in the same
repo (the head branch must live in the base repository, so fork PRs never stack onto
same-named branches) **(builder's call)**.

**A stack review covers:**

- one Overview and Background for the whole stack;
- a walkthrough per PR in stack order, plus a **stack walkthrough** across them;
- findings grouped per PR, with cross-PR findings called out.

**Acting on a stack:** each PR keeps its own recommendation and drafts. The author can post
per PR, or **all at once** from the stack, with one confirmation listing every PR and action.

Follow-ups (§4.1–4.2) apply per PR within a stack.

## 5. Trust: evidence, standards, findings

### 5.1 Correctness (anti-goal: approving breakage)

**(builder's call):** every pre-review runs a **correctness pass**. The agent reads the
changed code and the code around it, and looks for bugs, broken contracts, missing error
handling and untested paths. It reports what it checked as well as what it found.

- **Evidence rule:** **Approve** is only allowed when CI is green and the correctness pass
  found no Critical or Important issue. Missing CI or a failed pass gives **Needs a closer
  look**.
- **Agent-written PRs** (from known bot accounts or agent co-author trailers) always get the
  correctness pass at deep level, whatever the proposed depth **(builder's call)**.

### 5.2 Standards (user)

The review judges a PR against layered standards:

1. **Base:** the `.standards` repo the author already maintains, the same one Jesinia's own
   repo follows. **(builder's call):**
   - It is the author's single checkout, whose location is set once in Settings, not the
     version each repo pins.
   - Only the documents for the repo's languages (per `standards.yml`) plus the common ones
     apply.
2. **Per repo:** overrides in the repo itself, at **`.jesinia/review.md`** (builder's call).
3. **Per user:** the author's own overrides, in Settings → Review.

Later layers override earlier ones. Every finding and metric names the rule and layer it came
from, so a judgment can always be traced and argued with.

**(builder's call):**

- **Rule IDs:** a rule is identified by its document and heading path, e.g.
  `python.md › Security › P1`. The agent applies the merged prose. No structured rule format
  is required.
- **Per-repo files are read-only inputs:** `.jesinia/review.md` in a repo is read, never
  written, since Jesinia never pushes. Edits the author makes in the app (Edit metrics,
  per-repo overrides) are stored in `review.sqlite` and apply on top of the repo file.

**Metrics** (the wireframe's Metrics tab):

- per-criterion scores from the merged standards;
- the set is editable per repo;
- **(builder's call):** scores are kept per repo over time, and a **trend** view shows
  whether quality is drifting (anti-goal: deterioration).

### 5.3 Findings (anti-goal: dropping either kind)

Findings are grouped and never merged across groups:

- **Correctness:** bugs, broken behaviour, security.
- **Design:** architecture, boundaries, the mental-model issues in §6.2, duplication.
- **Style:** naming, smells, conventions.

Each finding has a severity (Critical, Important or Minor), a location, an explanation and the
rule it relates to. A clean Style group can't hide Design findings, and the reverse.

## 6. The author's knowledge (user)

### 6.1 The knowledge model

Jesinia keeps a model of the author's **domain and code knowledge** that **evolves over time
(user)**.

**Signals:**

- the author's own commits per repo and path;
- PRs they reviewed, and how deep;
- quiz results;
- KB pages about a repo or domain they wrote or kept.

**Units:** repo → directory/module, plus **domain topics**. The topics come from the KB's tag
catalog (RSS spec §5), so knowledge about `#payments` in the KB counts.

**Linking KB pages to code (builder's call):**

- A KB page can declare what code it's about in an optional front-matter field,
  `jesinia.code: [{repo: owner/name, path: src/sync}]`. Review pages set it automatically.
- A deep review maps the PR's directories to catalog tags, then stores and shows that
  mapping, so it can be corrected.
- Where nothing is declared, the agent infers links. The model's own maths stays pure and
  testable over those links.

**Familiarity per unit:**

- how recently and how much the author has engaged with it;
- **how much the code has changed since** their last exposure (user);
- **how novel or distant** it is from their normal work (user), measured as the unit's
  distance from the units they touch most.

**Use:** familiarity sets the proposed depth (§3.1) and how much background each Background
section and walkthrough step includes.

**Visibility (builder's call):** the model is private derived data in `review.sqlite`. Settings
→ Review → **What Jesinia thinks you know** shows it, and the author can mark a unit as known
or unknown to correct it.

### 6.2 Mental-model drift (anti-goal)

**(builder's call):** per repo, Jesinia keeps a KB page, **`Reviews/<repo>/How it works.md`**,
describing the system's main parts and behaviours. Deep reviews:

- flag when a PR changes something the page states, as a **Design** finding: "This changes
  how sync handles conflicts; your system notes say …";
- offer, once the PR is approved, a drafted update to that page for the author to accept.
  Updates are never automatic.

The page is seeded from the first deep review of a repo, for the author to edit.

## 7. Comments and posting (user)

- **Drafts:** findings become **draft review comments**, anchored to their lines. The author
  edits every comment inside Jesinia, and can delete them or write their own (user).
- **Posting:** nothing is posted until the author clicks.
  - **Approve**, **Request changes** or **Comment** opens a confirmation showing exactly what
    will be posted: the review body and every inline comment.
  - Posting uses the GitHub review API, so the review lands as one atomic review.
- **When the head changes mid-review (builder's call):**
  - a round is tied to the head SHA it reviewed, and posting targets that commit;
  - if newer commits exist, the confirmation says so, and **Approve** requires an explicit
    "Approve anyway" or "Review the new commits first";
  - drafts carry over to the next round, re-anchored where their lines are unchanged and
    flagged as outdated otherwise.
- **Your own PRs (builder's call):** GitHub doesn't allow approving or requesting changes on
  your own PR, so only **Comment** is offered there.
- **Blocking warning (user):** if the author chooses **Approve** while any included comment
  contains `BLOCKING` (case-insensitive), the confirmation shows a warning and asks them to
  confirm or change the action.
- **Posted reviews** are stored with the round (§8), for re-reviews.

## 8. Keeping reviews

- **Rounds (builder's call):** every review round is stored in `review.sqlite`. That covers the
  walkthrough, findings, drafts, what was posted, quiz results and the head SHA. It's the
  memory for re-reviews and the knowledge model.
- **KB pages:** a **deep** review the author finishes (posts, or marks **Done**) is kept as a
  KB page,
  `Reviews/<repo>/#<n> <title>.md`, with `kind: review/pr`:
  - front matter: `source` (the PR URL), `repo`, `pr`, `head`, `verdict` and `reviewed`;
  - the body: the overview, the walkthrough summary, the findings that mattered, the
    author's notes, and tags.
  - Agent text is wrapped in provenance; tags follow the shared catalog.
  - **(builder's call):** quick reviews and rebases aren't kept as pages.

## 9. Architecture

- **The `review` app module** (`server/jesinia/apps/review/`), layered like Notes and RSS:
  - queue, pre-review jobs, rounds, rebase detection, stacks, the knowledge model, drafts and
    posting;
  - REST under `/api/review/`;
  - read-only MCP tools `review_get`, `review_findings` and `review_queue`.
  - Posting is never an agent tool.
- **The `github` service gains writes:** creating a review with inline comments, plus listing
  PRs, files, commits, check runs, review threads and timelines. Writes go through a separate
  method that refuses unless called from a user action.
- **A repository workspace:** each watched repo gets a local bare mirror plus worktrees per
  reviewed head, under `~/Library/Application Support/jesinia/review/repos/`, so agents can
  read the full codebase and Jesinia can compute patch IDs. They are fetched with the
  author's git credentials and never pushed.
- **Agent work:**
  - each pre-review is an agent session. Its ContextPack holds the standards, PR data and
    knowledge summary. PR text, comments and the code itself are untrusted
    (`UntrustedDoc`s).
  - **Reading code (builder's call):** agents never get a shell, and never read outside the
    PR's worktree. They read code only through new Jesinia MCP tools scoped to that review's
    worktree: `repo_read(path, lines?)`, `repo_grep(pattern, glob?)`, `repo_ls(path)` and
    `repo_git(log|show|blame, args)`. These tools refuse paths outside the worktree,
    including symlinks.
  - Both runners use these tools, so Codex keeps its no-shell setup and stays the default.
    This matches the foundation's agent safety rules (foundation §7.5), because the PR code
    is a prompt-injection channel.
  - Structured outputs (walkthrough steps, findings, quiz, recommendation) are produced as
    JSON validated against schemas.
  - Default runner: Codex, per the wireframe, configurable.
- **The PWA:** `web/apps/review/`, from the shared template, using the Review icons already
  exported. It contains the queue, the PR view, the stack view and Settings → Review.
  **Layout (design pass, `docs/design/wireframes/design-pass-2026-10-16.md`):** workspace tabs
  (the queue plus one tab per open review); queue sections as tabs with counts; the PR view as
  a book, with an outline on the left and a view pane with a toolbar on top and actions along
  the bottom; Settings in the same outline-and-view-pane layout.

## 10. Errors (foundation §13 applies)

- **GitHub auth missing:** the wireframe's inline danger state, showing the exact command.
- **A pre-review fails:** the row shows **failed** with its cause and a Retry. Other PRs
  continue.
- **CI unavailable:** the recommendation can't be Approve (§5.1), and it says why.
- **A posting failure:** nothing is partially posted, since the review API is atomic. The
  drafts stay, and the cause is shown.

## 11. Testing

- **Python:**
  - recorded GitHub fixtures;
  - a fixture git repo for patch-ID rebase detection (pure rebase, rebase with conflict
    edits);
  - stack detection;
  - standards layering;
  - the knowledge model's familiarity math;
  - the evidence rule;
  - the BLOCKING warning logic;
  - incremental re-review mapping of previous comments to new hunks.
- **Agents:** a fake runner emits schema-valid outputs, plus a malformed-output case.
- **e2e:** a fake GitHub server and fake runner drive two flows:
  - queue, then a deep review, editing a draft, approving with a BLOCKING comment, seeing the
    warning, and posting;
  - a rebase of an approved PR showing **still approved**.

## 12. Left to the plan

- JSON schemas for the agent outputs.
- The familiarity formula's exact weights.
- How the bare mirror and worktrees are pruned.
- PR breakdown and order. A likely order: github writes and the workspace first, then the
  queue and pre-review, then the PR view, then follow-ups, stacks and the knowledge model.

## 13. Acceptance (2026-10-18)

Run on the author's Mac against the auto-updating install (`~/apps/jesinia`, port 5374), driven
by Claude in the browser; the author confirmed or made every GitHub post. Reviewed PRs were on
`Tukaiz/tkdata` (the author's own repos only allow COMMENT reviews). Shipped in #33, #40, #58,
#63 and #68, with #71, #74, #76, #77 and #78 (fixes found during acceptance).

| Step | Result |
|---|---|
| 1. Watch repos, CI-gated pre-reviews | Watched repos (`Tukaiz/tkdata`, `c65llc/coding-standards`, `dalbrecht/dust3d-swiftui` and others) were pre-reviewed in the background once CI was green: quick for small PRs, deep for larger ones; queue cards show the CI state, automated and human reviews, and *Pre-review ready*. Agent sessions first ran with tool approval prompts and the author's config (A1/A2, fixed in #71); provider outages left opaque job errors and a Codex preamble broke JSON replies (fixed in #74). |
| 2. Quick and deep review, quiz | A quick review of tkdata#906 and a deep review of tkdata#865: 7 walkthrough steps covering 100% of 6 files, two minor correctness findings, verdict *request changes*. The quiz scored 5 of 5, each answer explained with the file it came from. |
| 3. Edit drafts, post with a BLOCKING warning | The finding drafts never appeared: the round's finding jobs had failed and the one-time seed was spent before Retry stored them (fixed in #76: seeding per kind; verified live on tkdata#919, where a retry added 4 drafts). The author's own draft marked BLOCKING made *Approve…* warn "You're approving, but the review says BLOCKING", next to the verdict-mismatch warning; *Request changes instead* switched the dialog with the drafts kept. The author posted it ([review](https://github.com/Tukaiz/tkdata/pull/865#pullrequestreview-5565989433)); reopening the dialog warned that posting again adds a second review. |
| 4. Rebase showing "still approved" | Not checked: no PR the author approved was rebased during acceptance, and the author chose to record it as not yet checked rather than wait. |
| 5. Re-review after changes | tkdata#919 (changes requested on GitHub at `013fd11`, 39 files changed since) showed as *Re-review* in the queue; the PR view's *Your requests* rated each request (*Addressed*, *Partly addressed*) with its explanation and hunks. Fixed during acceptance: newlines in quoted review text showed as `⟪U+000A⟫` (#77), and agent-numbered step titles read "1. 1." (#78). |
| 6. Review page in the KB | `Reviews/tukaiz-tkdata/#865 fix(db) eager_defaults on server-generated timestamps.md` was kept on posting, with `kind: review/pr`, source, repo, PR, head, verdict and outcome, the overview and walkthrough in provenance blocks, the author's notes and tags. It has no findings section, as both findings were minor. |

Follow-ups: #73 (closed by #74), #75.
