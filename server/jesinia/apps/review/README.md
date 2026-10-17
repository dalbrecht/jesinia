# Review app

The Review app helps you review pull requests: a queue of the PRs that need you, a guided walkthrough of each one, and AI findings that are ready before you look. The design is in `docs/specs/2026-10-16-code-review-design.md`.

This is the first part of it (PR 1). It adds:

- **The repo workspace:** a local mirror of each watched repo plus one checkout per reviewed head, so agents and Jesinia can read the full codebase. See `infrastructure/workspace.py`.
- **The repo tools:** four read-only MCP tools (`repo_read`, `repo_grep`, `repo_ls`, `repo_git`). Review agents read PR code only through these. See `application/repo_tools.py` and `api/tools.py`.
- **The auto-skip rules:** which files a review leaves out (lockfiles, generated, vendored and binary files). See `domain/skip.py`.

It uses the `github` service (`services/github/`) for pull request data and for posting reviews. PR 2 adds the queue, the pre-review pipeline, the REST API and posting (below). The web app comes in a later PR.

## API

- **REST, under `/api/review`:** owner-only, like every module route. Every write takes an `Idempotency-Key` header.
  - `GET /queue` and `GET /prs/<owner>/<repo>/<n>` read `review.sqlite` only and never wait for GitHub. A running or failed sync is reported in the response (`syncing`, `sync_error`).
  - `POST /load`, `POST /queue/…/dismiss`, and `POST /prs/…/open`, `/retry` and `/review-anyway`.
  - `GET|PUT /prs/…/drafts`, `POST /prs/…/post/preview`, `POST /prs/…/post` and `POST /prs/…/done`.
  - `GET|PUT /settings`.
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
| `review.sqlite` | The queue, rounds, artifacts, drafts and postings | history: included in `jesinia backup` |

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
- the command and fetch time-outs on `GitWorkspace`.

## Development

Run the checks through the Makefile, which is the only entry point:

```bash
make test-py   # all Python tests, including tests/apps/review and tests/services/github
make ci        # everything CI runs, including the per-file coverage gate
```

- The tests run real git against temporary repositories. They never touch the network.
- Domain code (`domain/`) needs 100% coverage; everything else needs 95% per file.
