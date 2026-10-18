# Review app

The Review app helps you review pull requests: a queue of the PRs that need you, a guided walkthrough of each one, and AI findings that are ready before you look. The design is in `docs/specs/2026-10-16-code-review-design.md`.

This is the first part of it (PR 1). It adds:

- **The repo workspace:** a local mirror of each watched repo plus one checkout per reviewed head, so agents and Jesinia can read the full codebase. See `infrastructure/workspace.py`.
- **The repo tools:** four read-only MCP tools (`repo_read`, `repo_grep`, `repo_ls`, `repo_git`). Review agents read PR code only through these. See `application/repo_tools.py` and `api/tools.py`.
- **The auto-skip rules:** which files a review leaves out (lockfiles, generated, vendored and binary files). See `domain/skip.py`.

It uses the `github` service (`services/github/`) for pull request data and for posting reviews. PR 2 adds the queue, the pre-review pipeline, the REST API and posting (below). PR 3 adds the web app (`web/apps/review/`); PR 4 adds follow-ups, re-reviews and stacks (below); PR 5 adds the knowledge model (below).

## API

- **REST, under `/api/review`:** owner-only, like every module route. Every write takes an `Idempotency-Key` header.
  - `GET /queue` and `GET /prs/<owner>/<repo>/<n>` read `review.sqlite` only and never wait for GitHub. A running or failed sync is reported in the response (`syncing`, `sync_error`).
  - `POST /load`, `POST /queue/…/dismiss`, and `POST /prs/…/open`, `/retry` and `/review-anyway`.
  - `POST /prs/…/refresh` fetches a queued PR from GitHub now and checks its new head against your previous review ("Review the new head" opens it afterwards, under that head's own gates).
  - `GET|PUT /prs/…/drafts`, `POST /prs/…/post/preview`, `POST /prs/…/post` and `POST /prs/…/done`.
  - `GET /stacks/…`, `POST /stacks/…/open` and `POST /stacks/…/post-all` (below).
  - `GET|PUT /settings`.
  - `GET /knowledge` and `PUT /knowledge/<unit>` (below), and `POST /prs/…/quiz` (a finished quiz's result).
- **Stacks** (`domain/stacks.py`, `application/stacks.py`, spec §4.3): PRs whose base branch is another open PR's head branch, in the same repo (the head branch must live there), ordered bottom-up. A group that isn't one chain (a fork, two PRs on one branch, a cycle, more than 20 PRs) is never a stack: each PR is shown alone, with the reason.
  - A stack review is one Overview, one Background and one stack walkthrough (with cross-PR findings) for the PRs at their current heads. Each PR keeps its own round, walkthrough, findings, drafts and recommendation. Opening the stack opens every PR under its own gates; the stack's jobs start once no PR is held. A failed stack job, or one a stop cut short (marked `interrupted` at start), runs again only through Retry.
  - **Post all** posts each PR's confirmed review bottom-up through the normal posting path, and stops at the first that can't be posted: the answer is that PR's error with `stack_outcomes` (posted, already posted, failed, not attempted). Each PR's attempt is keyed from the request's `Idempotency-Key`, so a replay never posts a PR twice. A frozen or stale round anywhere refuses its PR before anything is posted. Every warning must be confirmed by the author for each PR: nothing is confirmed for them.
- **Follow-ups on a new head** (`domain/followup.py`, `application/followup.py`, spec §4.1 and §7):
  - **Rebase detection:** after each sync, every new head is compared with the author's latest review (their newest submitted review on GitHub, read as Jesinia's own round by its posting marker) by per-file `git patch-id --verbatim` (whitespace counts) over merge base to head. Same files as an approval: **still approved** (the queue's collapsed group; no pre-review). Some files differ: a re-review of only those files and their changed hunks. Otherwise: a re-review.
  - **Never a false still approved** (R75): CI is applied each time a follow-up is read (still approved only while CI is green, running or absent). Each classification records the author's latest review it used; a sync listing another one, and any Jesinia posting on the PR starting, settling or landing, drops the PR's follow-ups so they are classified again. While a posting of the PR may still be on GitHub, or when GitHub or git can't be read, the head is "unchecked" (the row says why) and checked again on the next sync. A loaded PR's new head waits for its check before its pre-review.
  - **Head changes:** a round on a new head freezes the PR's previous round (read-only: saves, posts and Done answer 409 `RoundFrozen`) and takes over its summary and drafts in the same transaction, unless a review of it was posted or may have been. Carried drafts are outdated until re-anchored to the lines their text is on now (renames followed); one whose text changed stays outdated and posts in the review body under its own heading, never inline. Drafts held back for an uncertain posting follow once it is known not to be on GitHub.
  - **Across rounds:** before a post, unsettled attempts on the PR's earlier rounds are reconciled first: one found on GitHub refuses the post (`EarlierReviewPosted`), one still within its grace answers "retry later". The preview notes when the round's review was already posted.
- **Incremental re-reviews** (`application/rereview.py`, spec §4.2): when the author requested changes or commented, or a changed rebase follows an approval, the `rereview` job maps each previous comment to addressed / partly / not addressed with the new hunks, and walks what is new since the last look. The previous review (stored round, or GitHub's threads) is shown aligned with this head's lines. Only an approval followed by a changed rebase narrows the round's own diff (R70).
- **Your own PRs** (spec §3.3): Background is minimised, the quiz is off unless asked for (`POST /prs/…/open` with `quiz: true`), and the recommendation reads "ready / not ready to request review" (`recommendation_label`). Only a comment can be posted.
- **The knowledge model** (`domain/knowledge.py`, `application/knowledge.py`, spec §6.1): what you know, per unit. A unit is a folder of a repo at most 3 deep (`repo/<owner>/<name>[/<path>]`, the root included) or a KB tag (`tag/<tag>`).
  - **Signals**, each stored once by its source: your commits reachable from each repo's default branch only (from the workspace's mirror, matched to the signed-in `gh` user's emails and your global git `user.email`, read incrementally from the last tip, page by page); rounds you posted or marked done (`review_deep` or `review_quick`, on the paths stored with the round, so a PR merged before the next ingest still counts); quiz results (`quiz_pass` with at least 60% of the answered questions right, else `quiz_fail`); and KB pages that declare `jesinia.code`, with their tags as topics (a page's newest revision replaces its last). Tagged pages without `jesinia.code` don't count yet: a narrowing of spec §6.1, by the plan's call. A file exposes every folder above it.
  - **Familiarity** in [0, 1]: each exposure weighs deep 1.0, commit 0.8, quiz pass 0.6, KB page 0.5, quick 0.4 (a failed quiz 0), halved every 90 days, combined as `1 - Π(1 - weight · decay)`. Commits together count at most 0.35, below the quick threshold: an author email is anyone's to claim, so commits alone never make a PR quick (R80). A unit with no evidence of its own gets a prior: its nearest best-known unit's score times `1 - distance`, the share of its path not shared with that unit (R82). Then times `1 - c / (c + 200)` for the `c` lines that landed in the unit since your last exposure. Marking a folder **known** makes it and everything beneath it 1.0, **unknown** 0.0 (the nearest mark wins).
  - **Use:** the proposed depth reads the lowest familiarity over the PR's paths (deep below 0.4); a depth you chose for the head still wins (R57). Every agent's ContextPack gets a bounded summary, fenced as untrusted (R79): it names units only in `[A-Za-z0-9._/-]` and short, anything else as an opaque `area-<hash>`. Until a repo's history has been read once (or you marked part of it), its familiarity is unknown and the depth follows the PR's size.
  - **Ingest** runs at start and every 30 minutes (`review.knowledge-ingest`). It is best-effort: each source runs on its own, and a failure is logged with its cause without stopping anything else. KB page content is data only, never run.
- **Posting** (`application/posting.py`) is the only GitHub write.
  - The preview shows exactly what will be posted, with its warnings (`blocking_comment`, `approve_against_evidence`, `newer_head`, `own_pr_comment_only`) and a digest. Approving an unfinished round, or one whose verdict isn't approve, needs `approve_against_evidence` confirmed: Jesinia recommends, the author decides.
  - A post must carry that digest and confirm every warning. It goes out as one atomic review on the round's head. Drafts GitHub can't anchor go in the body.
  - Each attempt is recorded before GitHub is called, and its body ends with an invisible marker (`<!-- jesinia-posting:<id> -->`).
  - A lost response, a timeout or a 5xx leaves the attempt `uncertain`. Until it is reconciled, nothing else is posted for that round. The next post first looks for a submitted review whose body ends with that marker and adopts it.
    - If the adopted review isn't the one being posted (another digest or event), the post is refused with `EarlierReviewPosted` (409, with the URL).
    - Within 3 minutes of the attempt, a missing review means "retry later" (`PostingStillUncertain`, 409 with `retry_after`). After that, the attempt is marked `not_posted` and the post goes on.
  - Drafts and summaries may not contain the marker's name in any spelling (`InvalidDraftContent`, 422), so a review posted by hand is never adopted.
  - `GET …/drafts` reads the cache only: `files_pending` says where drafts post isn't known yet.
  - Bodies and tokens are never logged.
- **MCP, read-only:** `review_queue`, `review_get` and `review_findings`. They read the cache only. Every PR-derived text comes back inside `<untrusted-data>` fences.
- **Correctness evidence:** the correctness pass's claimed `checked` ranges count only where its repo tools actually returned those lines (`ReadLog`). The evidence shows the claimed coverage beside the verified one.

## Setup

- **Requirements:** git 2.40 or later (the code uses `--attr-source`), and the GitHub CLI, signed in with `gh auth login`.
- **Credentials:** none to configure.
  - Fetches use your own git credential helper (on macOS, the Keychain).
  - API calls use the token from `gh auth token`, or a Keychain item named `jesinia-github`.
  - The token is held in memory for at most 5 minutes, and dropped and fetched again if GitHub rejects it. It is never written to disk or logged.
  - Jesinia never pushes.

## Where data lives

All under Jesinia's data directory (`~/Library/Application Support/jesinia/`):

| Path | What | Class |
|---|---|---|
| `review/repos/<owner>/<name>/mirror.git` | A bare mirror of the repo | rebuilt by fetching |
| `review/repos/<owner>/<name>/trees/<sha>` | A checkout of one reviewed head | rebuilt on demand |
| `review.sqlite` | The queue, rounds, artifacts, drafts, postings and the knowledge model | history: included in `jesinia backup` |

- **Kept commits:** commits fetched by SHA get a `refs/jesinia/keep/<sha>` ref, so `git gc` never drops them.
- **Pruning:** `GitWorkspace.prune(keep)` removes worktrees and keep refs that are no longer needed. Nothing schedules it yet; a later task will.

## How it stays safe

- **PR code is untrusted.** Agents get no shell. They read code only through the repo tools.
  - A tool works only inside a review session that Jesinia bound to a checkout (`ReviewSessionRegistry`). Any other caller is refused.
  - Each session can reach only its own checkout.
- **Paths:**
  - Paths resolve inside the checkout. Absolute paths, `..`, `.git`, and symlinks that point outside are refused.
  - Paths that aren't valid UTF-8 are shown with `\xNN` escapes.
- **Allowances:**
  - `repo_read` returns at most 2,000 lines (256 KB) per call.
  - Each file gives at most 4,000 lines per session, whatever path or tool reaches it. That includes grep matches, `show <commit>:<path>` and blame.
  - Output caps count UTF-8 bytes.
- **Skipped files:**
  - `repo_read` refuses lockfiles, generated, vendored and binary files.
  - `repo_grep` excludes them in git itself, using pathspecs generated from the same rules.
- **History:** `repo_git` runs only `log`, `show` and `blame`, with an allowlist of options.
  - Every revision must name a commit in the PR's own history.
  - Blame ignores the PR's `.mailmap`.
- **Regexes:** grep and `log --author=` accept only a small regex subset with a cost budget (`REGEX_SUBSET` in `domain/repo_access.py`). Searches time out after 20 s.
- **Hardened git:**
  - git runs as an argv list, never through a shell, in its own process group. It is killed when it times out or is cancelled.
  - The environment and `-c` overrides turn off hooks, LFS and other filters, submodules, fsmonitor, pagers, external diff drivers, the PR's own `.gitattributes`, and the system config (for reads).
  - Each review session runs one git process at a time.

## Configuration

Nothing is configured by the user yet. Limits and time-outs are constants in code:

- the caps in `domain/repo_access.py`;
- `SEARCH_TIMEOUT` in `application/repo_tools.py`;
- the command and fetch time-outs on `GitWorkspace`;
- the knowledge model's weights, half-life and limits in `domain/knowledge.py`, and `KNOWLEDGE_INTERVAL` in `module.py`.

## Development

Run the checks through the Makefile, which is the only entry point:

```bash
make test-py   # all Python tests, including tests/apps/review and tests/services/github
make ci        # everything CI runs, including the per-file coverage gate
```

- The tests run real git against temporary repositories. They never touch the network.
- Domain code (`domain/`) needs 100% coverage; everything else needs 95% per file.
