// SPDX-License-Identifier: AGPL-3.0-or-later
import { baseRoutes, type Routes } from "@jesinia/shell";
import type { PrView, Queue, QueueRow } from "../api";

export const MINUTE = 60_000;
export const ago = (minutes: number) => new Date(Date.now() - minutes * MINUTE).toISOString();

/** A queue row as `GET /api/review/queue` answers it; ready, quick and passing by default. */
export function row(number: number, over: Partial<QueueRow> = {}): QueueRow {
  return {
    repo: "acme/web",
    display_repo: "Acme/web",
    number,
    title: `PR ${number}`,
    url: `https://github.com/Acme/web/pull/${number}`,
    author: "dev-a",
    author_kind: "developer",
    created_at: ago(3 * 24 * 60),
    size_lines: 382,
    files: 9,
    kind: "new",
    depth: "quick",
    ci_state: "passing",
    auto_review_state: "landed",
    review_comment_count: 4,
    bot_review_count: 1,
    human_review_count: 2,
    review_requested: true,
    prereview_state: "ready",
    prereview_reason: "pre-review finished",
    recommendation: "approve",
    recommendation_stale: false,
    evidence: { ci: "passing", tests: "12 passed", findings: { important: 0, minor: 2 } },
    coverage_pct: 88,
    nothing_to_walk: false,
    loaded: false,
    dismissed: false,
    head_sha: "a".repeat(40),
    fetch_error: null,
    fetch_error_at: null,
    suggested: false,
    followup_kind: null,
    followup_note: "",
    differing_paths: [],
    ...over,
  };
}

export function queue(rows: QueueRow[], over: Partial<Queue> = {}): Queue {
  return { rows, synced_at: ago(20), syncing: false, sync_error: null, suggested_repos: [], ...over };
}

export function prView(r: QueueRow, over: Partial<PrView> = {}): PrView {
  return {
    pr: r,
    round: null,
    artifacts: {},
    stale: false,
    nothing_to_walk: r.nothing_to_walk,
    sync: { synced_at: ago(20), syncing: false, sync_error: null },
    files: null,
    ...over,
  };
}

export const UP_TO_DATE = { state: "up_to_date", reason: "this head's review is complete", round_id: 3, jobs: [] };

export const AUTH_MISSING = "no GitHub token: run `gh auth login`, or store a PAT in the Keychain as `jesinia-github`";

/** The shell's routes plus a queue; `GET /api/review/prs/…` answers from the queue's rows. */
export function routes(q: Queue | (() => Queue), over: Routes = {}): Routes {
  const current = typeof q === "function" ? q : () => q;
  return baseRoutes({
    "GET /api/review/queue": () => Response.json(current()),
    ...Object.fromEntries(
      current().rows.flatMap((r) => [
        [`GET /api/review/prs/${r.repo}/${r.number}`, () => Response.json(prView(r))],
        [`POST /api/review/prs/${r.repo}/${r.number}/open`, () => Response.json(UP_TO_DATE)],
      ]),
    ),
    ...over,
  });
}
